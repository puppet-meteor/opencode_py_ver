"""The P3-S3 worker face: ``opencode_agent`` execution as a real CLI subprocess (T-C1-078).

PHASE3 §2/§5.3 worker rows: an ``opencode_agent`` node does not run in-process.
The manager gives the assignment a three-part execution arena:

* a **detached git worktree** (``git worktree add --detach <path> HEAD``): the
  worker's only writable directory, created from the repository's immutable ref
  and removed only on a clean success (a dirty-but-whitelisted success keeps
  it for the merge-back face, a failure/cancel keeps it for diagnosis -- never
  a force remove, PHASE3 §12 known-pit 3);
* a **run directory** outside the worktree holding the per-worker host config
  (``host-config.json``: the proactive-interception trio -- roots pinned to the
  worktree, host-tier deny on ``git__worktree_add``/``git__worktree_remove``,
  allow on ``fs__write``/``fs__edit``, the register-saved preset equivalent)
  and the worker's own sqlite file (``worker.db``), the budget-watchdog's
  read-only accounting source;
* the **worker subprocess** itself: the real ``opencode`` CLI (``python -m
  opencode``) spawned via :func:`opencode.process.runner.run_command` -- the
  §13 process primitives (strong timeout, output cap, cooperative cancel,
  atomic tree kill) reused, never re-implemented -- with cwd=worktree and the
  first instruction "Read the task brief at TASK-BRIEF.md and execute it."

The task brief (seven fixed sections: goal/scope/context/constraints/outputs/
acceptance/budget, PHASE3 §5.3) is rendered in-process (a dataclass protocol,
not a wire contract -- the T-C1-078 ruling) and written into the worktree; the
worker's answer is a *wire* contract: the last ``[final] `` marker on stdout
must carry a single-line JSON object validating as
:class:`opencode.contracts.workers.WorkerReturn`.

Budget enforcement (PHASE3 §6 discipline 6) runs on all three lines:
``timeout_s`` through run_command's strong deadline, ``max_tokens``/
``max_tool_calls`` through a watchdog that re-polls the worker's own committed
``py.step.finished`` usage / ``py.tool.requested`` rows on the 20ms cancel tick
(throttled to one db read per ~250ms) and kills the tree mid-run, plus a
post-hoc overrun check after process exit -- max-tokens enforcement is a new
face here; the R0 Usage accounting only ever recorded, never enforced.

The acceptance gate (second line after the proactive interception): ``git
status --porcelain`` over the worktree must show only whitelisted paths
(TASK-BRIEF.md excluded); claimed ``files_changed`` must actually exist;
``artifacts`` must exist and be whitelisted. Any violation raises
:class:`WorkerError` carrying the CORE-CONTRACTS §8 code face and keeps the
worktree for diagnosis.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import PY_STEP_FINISHED, PY_TOOL_REQUESTED, JobBudget
from opencode.contracts.pipelines import PipelineNode
from opencode.contracts.workers import WorkerReturn
from opencode.process.runner import RunResult, run_command

#: The brief's fixed filename inside the worktree (also the first instruction).
BRIEF_FILENAME = "TASK-BRIEF.md"
#: The per-worker global config file (proactive-interception trio), in the run dir.
HOST_CONFIG_FILENAME = "host-config.json"
#: The worker's own sqlite file (``--db``), the watchdog's accounting source.
WORKER_DB_FILENAME = "worker.db"
#: The CLI marker the structured return rides on (cli.py's ``[final]`` face).
WORKER_RETURN_MARKER = "[final] "
#: The status line the CLI prints after the final answer (cut from the payload).
_RUN_MARKER = "\n[run] "
#: The watchdog's accounting re-poll cadence (the cancel tick runs at 20ms).
USAGE_POLL_INTERVAL_S = 0.25
#: Timeout for the git worktree bookkeeping calls (not the worker run itself).
_GIT_TIMEOUT_S = 60.0
#: The default worker wall-clock line (the budget ``timeout_s`` face) -- a
#: stop-loss quantity, deliberately NOT the LLM request deadline:
#: ``RuntimeSettings.timeout_s`` is the per-request line the adapter derives
#: its deadline ladder from (provider.md §3, the ~120s order), while one
#: ``opencode_agent`` node is a whole real-CLI worker drive (worktree setup,
#: the driven loop, the acceptance gate) that a request-sized line would
#: strangle. The production assemblies pass
#: ``JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S)`` as the default budget; a
#: per-job ``budget`` in the submit params overrides it (the existing submit
#: face). Independent-deadline discipline: TOOL_NODE_TIMEOUT_S (T-C1-089).
#: (P5-S7, T-C1-091.)
WORKER_DEFAULT_TIMEOUT_S = 1800.0


def workers_runs_path_for(db_path: str | Path) -> Path:
    """The workers runs-root directory derived from the main DB path.

    A sibling of the main database (``app.db`` -> ``app-workers``) -- the
    same sibling-derivation discipline as the advisory checkpointer
    (:func:`opencode.orchestrator.llm_call.checkpointer_path_for`, M-7: the
    workers arena is its own tree beside the main DB, never a table or a
    subdir inside it). The directory holds the per-worker run dirs and
    worktrees the manager creates beneath it: the host assemblies mkdir the
    base directory (the composition-root guarantee), everything below it
    belongs to the manager (P5-S7, T-C1-091).
    """
    resolved = Path(db_path)
    return resolved.with_name(f"{resolved.stem}-workers")


class WorkerError(Exception):
    """A worker-side failure carrying the CORE-CONTRACTS §8 code face.

    Raised by every fail-closed branch in this module (spawn/timeout/budget
    overrun, contract violations, acceptance-gate rejections); the engine maps
    it onto the node's ``CoreError`` by ``code`` instead of the generic
    ``ToolFailed`` default.
    """

    def __init__(self, message: str, *, code: CoreErrorCode) -> None:
        super().__init__(message)
        self.code = code


class WorkerSuspended(RuntimeError):
    """The worker run was cancelled: the drive must stop, the fold owns the outcome.

    Raised when ``JobService.cancel``'s canceller fired (the job's terminal
    ``py.job.finished(cancelled)`` is already committed by the service); the
    engine treats it exactly like :class:`opencode.orchestrator.engine.EnginePause`
    -- the drive returns without touching job status, so subsequent nodes never
    execute (no side effects after cancellation).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AgentJobContext:
    """The per-drive job facts the engine hands the node body via a ContextVar.

    The graph-state channel set is pinned (T-C1-077 tests), so the job id and
    the decoded budget ride a ``ContextVar`` set around ``graph.ainvoke`` in
    ``PipelineEngine.drive`` instead of new channels. ``budget`` is the job's
    submitted ``JobBudget`` (per-line values are still merged with the
    manager's default by :func:`resolve_budget`).
    """

    job_id: str
    budget: JobBudget


#: The S3 context seam (engine sets it around ainvoke; the node body reads it).
AGENT_JOB_CONTEXT: ContextVar[AgentJobContext | None] = ContextVar("opencode_agent_job_context", default=None)


def current_agent_job_context() -> AgentJobContext | None:
    """The drive's job context, or None outside a pipeline drive."""
    return AGENT_JOB_CONTEXT.get()


@dataclass(frozen=True, slots=True)
class TaskBrief:
    """The in-process brief protocol (PHASE3 §5.3): goal + boundary + budget.

    ``goal`` is the node's ``prompt_template`` (the engine's per-type semantic
    constraint requires it for ``opencode_agent`` nodes); ``context`` and
    ``acceptance`` ride the job params; ``authorized_files`` is the whitelist
    rendered both into the brief (the worker's honest boundary) and into the
    acceptance gate (the enforced one).
    """

    goal: str
    context: str
    acceptance: str
    authorized_files: tuple[str, ...]
    budget: JobBudget


#: The brief's fixed seven sections, in order (PHASE3 §5.3; pinned by tests).
BRIEF_SECTIONS: tuple[str, ...] = ("goal", "scope", "context", "constraints", "outputs", "acceptance", "budget")


def render_task_brief(brief: TaskBrief) -> str:
    """Render the seven-section markdown brief (pure; deterministic byte output)."""
    lines: list[str] = ["# Task Brief", "", "## goal", "", brief.goal, "", "## scope", ""]
    lines.append("Working directory: the root of the git worktree holding this brief; it is the only writable root.")
    lines.append(
        "Authorized files -- the hard boundary; nothing outside this list may be created, modified, or deleted:"
    )
    if brief.authorized_files:
        lines.extend(f"- {entry}" for entry in brief.authorized_files)
    else:
        lines.append("- (none: this is a read-only assignment)")
    lines.extend(
        [
            "",
            "## context",
            "",
            brief.context,
            "",
            "## constraints",
            "",
            "- No git write operations: never commit, branch, tag, or add/remove worktrees; "
            "the worktree is detached and owned by the orchestrator.",
            "- Never touch a file outside the authorized-files list; writes outside the whitelist "
            "are rejected and fail the assignment.",
            "- Stay inside the working directory.",
            "",
            "## outputs",
            "",
            "Answer with a structured return as your final message: one single-line JSON object with the fields",
            "schema_version (integer, currently 1), summary (non-empty string),",
            "files_changed (list of worktree-relative paths you created or modified),",
            "artifacts (list of worktree-relative report/verification paths), and",
            "tests_passed (boolean, your acceptance-check outcome). The orchestrator parses the final answer as this",
            "JSON object and validates it against the worker return contract.",
            "",
            "## acceptance",
            "",
            brief.acceptance,
            "",
            "## budget",
            "",
        ]
    )
    if brief.budget.max_tool_calls is not None:
        lines.append(f"- max tool calls: {brief.budget.max_tool_calls} (exceeding the line kills the run)")
    else:
        lines.append("- max tool calls: unlimited")
    if brief.budget.max_tokens is not None:
        lines.append(f"- max tokens: {brief.budget.max_tokens} (exceeding the line kills the run)")
    else:
        lines.append("- max tokens: unlimited")
    if brief.budget.timeout_s is not None:
        lines.append(f"- wall-clock timeout: {brief.budget.timeout_s:g}s (exceeding the line kills the run)")
    else:
        lines.append("- wall-clock timeout: unlimited")
    lines.append("")
    return "\n".join(lines)


def host_config_document(worktree: Path) -> dict[str, Any]:
    """The worker's global config (the proactive-interception trio, PHASE3 §5.3).

    Passed to the CLI as ``--config`` (the global tier): ``roots`` pins the
    fs adapters to the worktree (global-only host field, §6 L285/L287), and
    ``permission.rules`` carries the trio -- host-tier deny on the worktree
    mutating actions (the CLI would otherwise ask and a worker cannot answer),
    allow on the file-mutation actions (the register-saved preset equivalent:
    without it the worker's writes would hang on an approval nobody grants).
    ``source`` is deliberately absent (loader validated; a config-file rule is
    project tier by construction, §5.2 L266).
    """
    return {
        "roots": [str(worktree)],
        "permission": {
            "rules": [
                {
                    "action": "git__worktree_add",
                    "effect": "deny",
                    "reason": "worker worktrees are orchestrator-owned",
                },
                {
                    "action": "git__worktree_remove",
                    "effect": "deny",
                    "reason": "worker worktrees are orchestrator-owned",
                },
                {
                    "action": "fs__write",
                    "effect": "allow",
                    "reason": "worker writes inside the authorized whitelist",
                },
                {
                    "action": "fs__edit",
                    "effect": "allow",
                    "reason": "worker edits inside the authorized whitelist",
                },
            ]
        },
    }


def whitelist_allows(authorized: Sequence[str], path: str) -> bool:
    """The acceptance-gate whitelist match: exact file or directory prefix.

    An entry matches a changed path when it equals it or is its directory
    prefix (an untracked directory shows up collapsed as ``dir/`` in
    porcelain). An empty whitelist allows nothing (a read-only worker).
    Pure string logic on worktree-relative POSIX paths -- the contract layer
    already rejected absolute/backslash/``..`` shapes structurally.
    """
    normalized = path.replace("\\", "/").rstrip("/")
    if not normalized:
        return False
    for entry in authorized:
        allowed = entry.replace("\\", "/").rstrip("/")
        if not allowed:
            continue
        if normalized == allowed or normalized.startswith(allowed + "/"):
            return True
    return False


def _covered_by(actual: Sequence[str], claimed: str) -> bool:
    """A claimed file is covered by the porcelain evidence (file or collapsed dir)."""
    return whitelist_allows(list(actual), claimed)


def _porcelain_paths(stdout: str) -> list[str]:
    """Extract the changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # a rename carries "old -> new"; the new path is the evidence
            path = path.rsplit(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def resolve_budget(job_budget: JobBudget | None, default_budget: JobBudget) -> JobBudget:
    """Per-line merge: the job's submitted line wins, the manager's default fills.

    The manager's ``default_budget.timeout_s`` is required non-None at
    construction; a job may still explicitly narrow any line. Pure -- both
    tests and the engine face call it directly.
    """
    if job_budget is None:
        return default_budget
    return JobBudget(
        max_tool_calls=(
            job_budget.max_tool_calls if job_budget.max_tool_calls is not None else default_budget.max_tool_calls
        ),
        max_tokens=job_budget.max_tokens if job_budget.max_tokens is not None else default_budget.max_tokens,
        timeout_s=job_budget.timeout_s if job_budget.timeout_s is not None else default_budget.timeout_s,
    )


def parse_worker_return(stdout: str) -> WorkerReturn:
    """Parse the worker's structured return off the CLI's stdout contract.

    The *last* ``[final] `` marker carries the payload (a re-prompt cycle may
    print several); the trailing ``\\n[run] <status>`` line the CLI prints
    after the final answer is cut. Fails closed on every shape problem -- no
    best-effort fallback to raw text.
    """
    index = stdout.rfind(WORKER_RETURN_MARKER)
    if index == -1:
        raise WorkerError(
            "worker stdout carried no [final] marker: no structured return to parse",
            code=CoreErrorCode.TOOL_FAILED,
        )
    payload = stdout[index + len(WORKER_RETURN_MARKER) :]
    run_index = payload.rfind(_RUN_MARKER)
    if run_index != -1:
        payload = payload[:run_index]
    payload = payload.strip("\r\n")
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError as error:
        raise WorkerError(
            f"worker [final] payload is not a JSON object: {error}",
            code=CoreErrorCode.TOOL_FAILED,
        ) from error
    if not isinstance(raw, dict):
        raise WorkerError(
            "worker [final] payload is not a JSON object",
            code=CoreErrorCode.TOOL_FAILED,
        )
    try:
        return WorkerReturn.model_validate(raw)
    except ValidationError as error:
        raise WorkerError(
            f"worker [final] payload failed the WorkerReturn contract: {error}",
            code=CoreErrorCode.TOOL_FAILED,
        ) from error


def _check_whitelist_entry(entry: str) -> None:
    """Reject whitelist entries that could not match worktree-relative evidence."""
    if entry.startswith("/") or (len(entry) >= 2 and entry[1] == ":"):
        raise WorkerError(
            f"authorized_files entries must be worktree-relative, got {entry!r}",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    if "\\" in entry:
        raise WorkerError(
            f"authorized_files entries must use '/' separators, got {entry!r}",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    if any(part == ".." for part in entry.split("/")):
        raise WorkerError(
            f"authorized_files entries must not escape the worktree, got {entry!r}",
            code=CoreErrorCode.VALIDATION_ERROR,
        )


def _worker_inputs(node: PipelineNode, state: Any) -> tuple[str, str, tuple[str, ...], str]:
    """Read the node's goal and the job params face (context/authorized_files/acceptance)."""
    goal = node.prompt_template
    if goal is None:  # the engine's semantic gate enforces this; fail closed anyway
        raise WorkerError(
            f"opencode_agent node '{node.node_id}' carries no prompt template goal",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    params: Any = state.get("params") if isinstance(state, dict) else None
    if not isinstance(params, dict):
        raise WorkerError(
            "opencode_agent node found no job params on the graph state",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    context = params.get("context")
    if not isinstance(context, str) or not context:
        raise WorkerError(
            "opencode_agent params must carry a non-empty string 'context'",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    acceptance = params.get("acceptance")
    if not isinstance(acceptance, str) or not acceptance:
        raise WorkerError(
            "opencode_agent params must carry a non-empty string 'acceptance'",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    raw_authorized = params.get("authorized_files", [])
    if not isinstance(raw_authorized, list):
        raise WorkerError(
            "opencode_agent params 'authorized_files' must be a list of worktree-relative paths",
            code=CoreErrorCode.VALIDATION_ERROR,
        )
    authorized: list[str] = []
    for entry in raw_authorized:
        if not isinstance(entry, str) or not entry:
            raise WorkerError(
                "opencode_agent params 'authorized_files' entries must be non-empty strings",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
        _check_whitelist_entry(entry)
        authorized.append(entry)
    return goal, context, tuple(authorized), acceptance


class _BudgetWatchdog:
    """The max-tokens/max-tool-calls enforcement face over the worker's own db.

    ``poll`` is the run_command ``should_cancel`` face (called on its 20ms
    tick; the sqlite re-read is throttled to one per ~250ms): once a line is
    crossed it latches and the runner kills the process tree. ``overrun_reason``
    re-reads unthrottled for the post-hoc check after process exit. Reads are
    read-only URI opens; a missing/unmigratable/locked db reads as zero usage
    (the worker CLI runs its own migrations at startup -- a young db simply has
    nothing to count yet).
    """

    def __init__(self, *, db_path: Path, max_tokens: int | None, max_tool_calls: int | None) -> None:
        self._db_path = db_path
        self._max_tokens = max_tokens
        self._max_tool_calls = max_tool_calls
        self._last_poll = 0.0
        self._reason: str | None = None

    def poll(self) -> bool:
        """The should_cancel face: True once a budget line is crossed (latched)."""
        if self._reason is not None:
            return True
        now = time.monotonic()
        if now - self._last_poll < USAGE_POLL_INTERVAL_S:
            return False
        self._last_poll = now
        self._reason = self.overrun_reason()
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        """The latched overrun description (None while inside budget)."""
        return self._reason

    def overrun_reason(self) -> str | None:
        """Fresh (unthrottled) overrun check over the worker's committed rows."""
        tokens, tool_calls = self._account()
        if self._max_tokens is not None and tokens > self._max_tokens:
            return f"max_tokens exceeded: consumed {tokens} > budget {self._max_tokens}"
        if self._max_tool_calls is not None and tool_calls > self._max_tool_calls:
            return f"max_tool_calls exceeded: made {tool_calls} > budget {self._max_tool_calls}"
        return None

    def _account(self) -> tuple[int, int]:
        """Sum usage from ``py.step.finished`` and count ``py.tool.requested`` rows.

        Token sum excludes ``cache_read`` (a cache hit is not new spend); None
        fields (unknown) contribute zero rather than being guessed.
        """
        if not self._db_path.exists():
            return 0, 0
        try:
            connection = sqlite3.connect(f"file:{self._db_path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return 0, 0
        try:
            rows = connection.execute(
                "SELECT type, data FROM events WHERE type IN (?, ?)",
                (PY_STEP_FINISHED, PY_TOOL_REQUESTED),
            ).fetchall()
        except sqlite3.Error:
            return 0, 0  # not migrated yet / table absent / locked: nothing provable, nothing counted
        finally:
            connection.close()
        tokens = 0
        tool_calls = 0
        for event_type, data in rows:
            if event_type == PY_TOOL_REQUESTED:
                tool_calls += 1
                continue
            try:
                payload: Any = json.loads(data)
            except (TypeError, ValueError):
                continue
            usage = payload.get("message", {}).get("usage") if isinstance(payload, dict) else None
            if not isinstance(usage, dict):
                continue
            for key in ("input", "output", "reasoning", "cache_write"):
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens += value
        return tokens, tool_calls


class WorkerManager:
    """The ``opencode_agent`` node body: spawn a worker, enforce, gate, return.

    One instance serves one engine (the node body + the jobs cancel hook).
    The constructor takes the host faces the real CLI needs (provider
    credentials/model/context window) plus ``worker_command`` -- the argv
    prefix that defaults to ``[sys.executable, "-m", "opencode"]``; the
    budget/cancel integration tests inject ``[sys.executable, "-c", script]``
    scripts that speak the same ``--db`` flag face and write accounting rows
    into the worker db themselves.
    """

    def __init__(
        self,
        *,
        repo_root: str | Path,
        runs_root: str | Path,
        default_budget: JobBudget,
        base_url: str,
        api_key: str,
        model: str,
        context_limit: int,
        worker_command: Sequence[str] | None = None,
    ) -> None:
        if default_budget.timeout_s is None:
            raise ValueError(
                "WorkerManager default_budget.timeout_s is required (the worker run needs a wall-clock line)"
            )
        self._repo_root = Path(repo_root)
        self._runs_root = Path(runs_root)
        self._default_budget = default_budget
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._context_limit = context_limit
        self._worker_command: tuple[str, ...] = (
            tuple(worker_command) if worker_command is not None else (sys.executable, "-m", "opencode")
        )
        self._cancel_events: dict[str, threading.Event] = {}

    # -- the jobs cancel hook (JobService's RunCanceller face) ------------------

    def cancel(self, job_id: str) -> None:
        """Signal the in-flight worker of ``job_id``; the runner kills its tree.

        Sync and best-effort by contract (called before the service's terminal
        ``py.job.finished(cancelled)`` commit, so the kill happens while the
        job is still non-terminal). A signal for a not-yet-running worker
        latches: the next run of that job dies at its first cancel tick.
        """
        self._cancel_event(job_id).set()

    def _cancel_event(self, job_id: str) -> threading.Event:
        event = self._cancel_events.get(job_id)
        if event is None:
            event = threading.Event()
            self._cancel_events[job_id] = event
        return event

    # -- the node body (the engine's AgentFn face) ------------------------------

    async def __call__(self, node: PipelineNode, state: Any) -> dict[str, Any]:
        """Execute the assignment: worktree -> brief -> spawn -> gate -> return."""
        job = current_agent_job_context()
        if job is None:
            raise WorkerError(
                "opencode_agent node executed outside a job drive (no agent job context)",
                code=CoreErrorCode.TOOL_FAILED,
            )
        goal, context_text, authorized, acceptance = _worker_inputs(node, state)
        budget = resolve_budget(job.budget, self._default_budget)
        brief = TaskBrief(
            goal=goal,
            context=context_text,
            acceptance=acceptance,
            authorized_files=authorized,
            budget=budget,
        )

        stamp = uuid.uuid4().hex[:12]
        worktree = self._runs_root / "worktrees" / f"{node.node_id}_{stamp}"
        run_dir = self._runs_root / "runs" / f"{node.node_id}_{stamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        config_path = run_dir / HOST_CONFIG_FILENAME
        db_path = run_dir / WORKER_DB_FILENAME

        added = await self._git(("worktree", "add", "--detach", str(worktree), "HEAD"))
        if added.returncode != 0:
            raise WorkerError(
                f"git worktree add failed for node '{node.node_id}': {added.stderr.strip()}",
                code=CoreErrorCode.TOOL_FAILED,
            )
        try:
            (worktree / BRIEF_FILENAME).write_text(render_task_brief(brief), encoding="utf-8", newline="\n")
            config_path.write_text(
                json.dumps(host_config_document(worktree), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            cancel_event = self._cancel_event(job.job_id)
            watchdog = _BudgetWatchdog(
                db_path=db_path, max_tokens=budget.max_tokens, max_tool_calls=budget.max_tool_calls
            )

            def should_cancel() -> bool:
                return cancel_event.is_set() or watchdog.poll()

            argv: list[str] = [
                *self._worker_command,
                "--db",
                str(db_path),
                "--base-url",
                f"{self._base_url}/v1",
                "--api-key",
                self._api_key,
                "--model",
                self._model,
                "--context-limit",
                str(self._context_limit),
                "--config",
                str(config_path),
                *worker_prompt_words(),
            ]
            try:
                run = await asyncio.to_thread(
                    run_command,
                    argv,
                    cwd=str(worktree),
                    timeout_s=budget.timeout_s if budget.timeout_s is not None else 0.0,
                    should_cancel=should_cancel,
                )
            except asyncio.CancelledError:
                # The drive task itself is being torn down (jobs.close): the
                # to_thread worker keeps running run_command, so signal the
                # kill through the event the closure polls, then propagate.
                cancel_event.set()
                raise

            if run.timed_out:
                raise WorkerError(
                    f"worker exceeded the wall-clock timeout budget ({budget.timeout_s:g}s); process tree killed",
                    code=CoreErrorCode.TOOL_TIMEOUT,
                )
            if run.cancelled:
                if cancel_event.is_set():
                    raise WorkerSuspended(
                        f"worker run cancelled (job {job.job_id}); process tree killed, worktree kept for diagnosis"
                    )
                overrun = watchdog.reason or watchdog.overrun_reason()
                if overrun is not None:
                    raise WorkerError(
                        f"worker budget overrun, process tree killed: {overrun}",
                        code=CoreErrorCode.TOOL_FAILED,
                    )
                raise WorkerError(  # defensive: a cancel tick with neither signal nor overrun
                    "worker run stopped by the cancellation face without a budget overrun",
                    code=CoreErrorCode.TOOL_FAILED,
                )

            # Post-hoc overrun check BEFORE parsing stdout: a worker that
            # crossed a line and exited fast still fails -- budget lines hold
            # even when the kill lost the race with process exit.
            overrun = watchdog.overrun_reason()
            if overrun is not None:
                raise WorkerError(
                    f"worker budget overrun: {overrun}",
                    code=CoreErrorCode.TOOL_FAILED,
                )
            if run.returncode != 0:
                raise WorkerError(
                    f"worker exited with code {run.returncode}: {run.stderr.strip()[-512:]}",
                    code=CoreErrorCode.TOOL_FAILED,
                )

            returned = parse_worker_return(run.stdout)

            # -- the acceptance gate (the second line; the trio is the first) --
            status = await self._git(("status", "--porcelain"), cwd=worktree)
            if status.returncode != 0:
                raise WorkerError(
                    f"git status failed in the worktree: {status.stderr.strip()}",
                    code=CoreErrorCode.TOOL_FAILED,
                )
            actual = [path for path in _porcelain_paths(status.stdout) if path != BRIEF_FILENAME]
            outside = sorted({path for path in actual if not whitelist_allows(authorized, path)})
            if outside:
                raise WorkerError(
                    f"worker touched files outside the authorized whitelist: {outside} (worktree kept for diagnosis)",
                    code=CoreErrorCode.PERMISSION_DENIED,
                )
            unproven = [path for path in returned.files_changed if not _covered_by(actual, path)]
            if unproven:
                raise WorkerError(
                    f"worker claimed files_changed absent from the worktree: {unproven} (worktree kept for diagnosis)",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
            for artifact in returned.artifacts:
                if not whitelist_allows(authorized, artifact):
                    raise WorkerError(
                        f"worker claimed artifact outside the authorized whitelist: {artifact!r}",
                        code=CoreErrorCode.PERMISSION_DENIED,
                    )
                if not (worktree / artifact).exists():
                    raise WorkerError(
                        f"worker claimed artifact that does not exist: {artifact!r}",
                        code=CoreErrorCode.VALIDATION_ERROR,
                    )

            kept = bool(actual)
            if not kept:
                # Clean success: remove the brief, then the worktree (no force:
                # a refusal degrades to "kept" rather than a risky cleanup).
                try:
                    (worktree / BRIEF_FILENAME).unlink(missing_ok=True)
                except OSError:
                    kept = True
                if not kept:
                    removed = await self._git(("worktree", "remove", str(worktree)))
                    kept = removed.returncode != 0
            return {
                "worker_return": returned.model_dump(),
                "worktree": str(worktree) if kept else None,
                "worktree_kept": kept,
            }
        finally:
            # The event object stays alive in any in-flight closure; the
            # registry entry is dropped so cancel bookkeeping cannot grow
            # without bound (a cancel for a terminal job is a no-op anyway).
            self._cancel_events.pop(job.job_id, None)

    async def _git(self, args: tuple[str, ...], *, cwd: Path | None = None) -> RunResult:
        """One controlled git bookkeeping call (worktree add/status/remove)."""
        return await asyncio.to_thread(
            run_command,
            ("git", *args),
            cwd=str(cwd if cwd is not None else self._repo_root),
            timeout_s=_GIT_TIMEOUT_S,
        )


def worker_prompt_words() -> tuple[str, ...]:
    """The worker's first instruction: a fixed, structured sentence (PHASE3 §5.3).

    Joined by the CLI's positional-prompt face into "Read the task brief at
    TASK-BRIEF.md and execute it." -- the brief file is inside the worktree the
    process starts in, so the instruction is path-free by design.
    """
    return ("Read", "the", "task", "brief", "at", "TASK-BRIEF.md", "and", "execute", "it.")
