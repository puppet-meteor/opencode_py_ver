"""R4-S7 closeout e2e (T-C1-041): side-effect uncertainty + recovery over the full stack.

Seven tests over the genuine end-to-end composition -- scripted OpenAI server on
real loopback HTTP (owned by the test process) -> MaasAdapter SSE -> App ->
SessionService/runner/step engine -> Invoker -> a REAL stdio MCP fixture
server spawned across a process boundary (``McpManager.start_server`` +
``MCPToolAdapter`` assembled directly into the App ``tools=`` tuple at the
composition root). Three kill/restart windows, the multi-call plan-synthesis
face, the generation half-batch injection, and the App close-order race.

MCP composition disclosure (④): the production CLI registers MCP servers
without spawning them (the register-no-spawn face, ticket T8); this file
assembles ``MCPToolAdapter`` into the App composition root directly because
it is a test composition -- the production spawn+bind wiring is out of scope
here and lands later (R5+).

Every driver/server script is a string written at runtime into the pytest
``tmp_path`` (outside the worktree): zero temp files inside the worktree.

Per-test mapping (acceptance sentence + fault-matrix row + window
construction form; the ladder is 真实 kill / monkeypatch 注入 / 直拟, each
test states its rung honestly):

① ``test_w1_kill_while_approval_pending`` -- acceptance B "a kill before
   tool.started leaves the tool unexecuted" (PHASE1-SLIM L103 front half) +
   matrix L127 (恢复不重复副作用); form: 真实 kill -- the test kills the
   parked driver child at the stable approval-pending block; no monkeypatch,
   no fabricated rows.
② ``test_w2_kill_while_tool_executing`` -- acceptance B "a kill after the
   external side effect but before the terminal folds unknown, no rerun"
   (L103 + L219 semantics) + matrix L127/L131 (结果未知不自动重试); form:
   真实 kill at a stable mid-execution block, where the block itself is the
   task-book sanctioned test-side adapter wrapper (run the real MCP call,
   mark, await forever) -- the fixture server's call counter proves the
   external side effect happened exactly once.
③ ``test_w3_kill_while_generation_streaming`` -- acceptance B "a kill after
   tool.finished reuses the committed result verbatim, never reruns" (L103 +
   L220 semantics) + matrix L127; form: 真实 kill at the stable blocked
   provider stream (the fake server's registered ``hang_after_frames``
   button).
④ ``test_multi_call_plan_recovery_pairs_every_call`` -- acceptance C "after
   recovery a fresh prompt shows every legal call with exactly one result,
   including the later planned calls that were never invoked" (L104 + §3.3
   L214 second sentence); form: 真实 kill at a stable mid-plan block (call A
   finished, call B parked in the wrapper).
⑤ ``test_generation_half_batch_crash_leaves_no_orphan_rows`` -- acceptance B
   back half "the finalized->generated generation batch commits atomically;
   an injected half-batch fault leaves a complete batch or no batch" (L103)
   + matrix L129 (S1/S4 generation batch); form: monkeypatch 注入 -- a
   class-level ``EventCommitter.commit`` hook raises before the store call,
   the closest in-process analog of the process dying between generation and
   commit. The torn finalized-without-generated variant is a 直拟 face
   already closed by test_recovery.py (窗 2) and is deliberately not
   duplicated here.
⑥a ``test_close_folds_pending_approval_then_mcp_provider_db`` -- acceptance
   C back half "shutdown/crash race: close persists cancels and terminals
   first, then closes MCP/provider/DB" (L104 + §7 L339) + matrix L128;
   form: in-process race at the App composition root -- session A parked on
   a real ``fs__write`` approval, session B parked in the provider stream
   (the fake server's ``hang_after_frames`` button), ``close_timeout_s=1.0``
   injected through the App ctor (a test parameter, not a constant change).
⑥b ``test_close_timeout_folds_in_flight_mcp_call_unknown`` -- the same §7
   L339 order through the close-timeout face: one session parked
   mid-execution in the wrapper (the REAL fixture call already counted)
   past the 1.0s wait, so close hard-cancels it and the grace period
   carries the folded unknown/unknown tool terminal. The split is honest
   accounting (task clause 8): the registry's Phase-1 serial invoke lock
   means only one invoke can sit inside the pipeline at a time, so the task
   book's literal combined close (one App holding BOTH an approval-parked
   and a mid-execution-parked invoke) is structurally unreachable -- ④
   float report.

Planned exemption (task clause 7): the acceptance C front-half sentences
(pending-approval invalidation on restart, saved-decision audit, duplicate
prompt, concurrent cancel, slow subscribers, lost-notification catch-up) are
closed by the S4/S5/S6 suites (test_recovery.py, test_cli_r4.py, the service
faces) and are not re-woven here.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import PY_STEP_GENERATED, DurableEvent, EventDraft
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import JsonObject
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.mcp.bridge import MCPToolAdapter
from opencode.mcp.manager import McpManager, StdioServerDefinition
from opencode.session.context import RuntimeSettings
from opencode.session.runner import EventCommitter
from opencode.session.service import RunStatus, SessionService
from opencode.tools.builtin import FsWriteAdapter
from opencode.tools.invoker import ToolContext
from opencode.tools.policy import PermissionRule
from opencode.tools.registry import mcp_tool_spec
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The S2 stdio fixture server, reused read-only (the ③ ruling: its buttons
#: cover every window this file needs, so no S7-owned server asset is added).
SERVER_SCRIPT = REPO_ROOT / "tests" / "integration" / "mcp_stdio_server.py"

#: The §7 L339 close order this file pins: MCP (with the DB still readable
#: inside it), then the provider, and the database handle strictly after.
_CLOSE_ORDER = ["mcp:start", "mcp:db-readable", "mcp:done", "provider"]


# ---------------------------------------------------------------------------
# r3-reused idioms (read-only reuse: same shapes as test_e2e_r3.py).
# ---------------------------------------------------------------------------


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model="fake-model"),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


async def _await_terminal(service: SessionService, run_id: str, *, timeout_s: float = 10.0) -> RunStatus:
    """Poll the durable run status (§7 L314) until it reaches a terminal."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status.is_terminal:
            return status
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} did not reach a terminal status within {timeout_s}s")
        await asyncio.sleep(0.02)


def _text_response(*deltas: str) -> ScriptedResponse:
    """One text stream: a delta per fragment, then a STOP finish (the test_cli idiom)."""
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def _tool_calls_response(preamble: str, *calls: tuple[str, str, JsonObject]) -> ScriptedResponse:
    """One scripted tool_calls generation: preamble text, the calls with
    ascending wire call indexes, then the tool_calls finish."""
    frames = [Frame(text_delta_frame(preamble))]
    for index, (call_id, name, arguments) in enumerate(calls):
        frames.append(Frame(tool_call_start_frame(index, call_id, name)))
        frames.append(Frame(tool_call_args_frame(index, json.dumps(arguments))))
    frames.append(Frame(finish_frame("tool_calls")))
    return ScriptedResponse(frames=frames)


def _wire_messages(body: object) -> list[dict[str, object]]:
    """The request body's message list, narrowed for tolerant wire assertions."""
    assert isinstance(body, dict)
    messages = body.get("messages")
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, dict)]


def _content_str(content: object) -> str:
    """Wire content as text: a plain string or OpenAI content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts)
    return ""


# ---------------------------------------------------------------------------
# sqlite helpers: live reads retry the writer-lock OperationalError (the T7
# read-probe idiom); post-close reads reuse the same path harmlessly.
# ---------------------------------------------------------------------------


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _fetch_all_retry(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """A live-read fetch: the child process's writer lock surfaces as
    OperationalError and is retried before giving up."""
    last: sqlite3.OperationalError | None = None
    for _ in range(100):
        try:
            return _fetch_all(db_path, statement, params)
        except sqlite3.OperationalError as error:
            last = error
            time.sleep(0.02)
    raise AssertionError(f"sqlite read kept failing under the live writer lock: {last}")


def _event_rows(db_path: Path, session_id: str) -> list[tuple[str, str]]:
    """Durable EventLog (type, data) rows in seq order."""
    return [
        (str(row[0]), str(row[1]))
        for row in _fetch_all_retry(
            db_path, "SELECT type, data FROM events WHERE session_id = ? ORDER BY seq", (session_id,)
        )
    ]


def _event_types(db_path: Path, session_id: str) -> list[str]:
    return [kind for kind, _ in _event_rows(db_path, session_id)]


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [json.loads(data) for kind, data in _event_rows(db_path, session_id) if kind == event_type]


def _run_id_of(db_path: Path, session_id: str) -> str:
    """The session's first run id, read from the durable ``py.run.started`` row."""
    return str(_event_data(db_path, session_id, "py.run.started")[0]["run_id"])


async def _await_event_count(
    db_path: Path, session_id: str, event_type: str, count: int, *, timeout_s: float = 30.0
) -> None:
    """Live-poll the durable ``event_type`` rows until ``count`` exist."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            rows = _fetch_all(
                db_path,
                "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
                (session_id, event_type),
            )
        except sqlite3.OperationalError:
            rows = None
        if rows is not None and len(rows) >= count:
            return
        if time.monotonic() > deadline:
            seen = 0 if rows is None else len(rows)
            pytest.fail(f"only {seen}/{count} {event_type} rows appeared for {session_id}")
        await asyncio.sleep(0.05)


def _read_file_text(path: Path) -> str:
    """Tiny marker-file read as a sync helper (the r3 live-poll dodge)."""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


async def _await_file_text(path: Path, *, timeout_s: float = 30.0) -> str:
    """Poll a marker file until it holds non-empty text (driver artifacts)."""
    deadline = time.monotonic() + timeout_s
    while True:
        text = _read_file_text(path)
        if text:
            return text
        if time.monotonic() > deadline:
            pytest.fail(f"file {path} never held text")
        await asyncio.sleep(0.05)


async def _await_server_requests(server: FakeOpenAIServer, count: int, *, timeout_s: float = 30.0) -> None:
    """Poll the fake server's recorded requests until ``count`` exist."""
    deadline = time.monotonic() + timeout_s
    while len(server.requests) < count:
        if time.monotonic() > deadline:
            pytest.fail(f"only {len(server.requests)}/{count} provider requests were recorded")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# MCP fixture-log helpers: the S2 server's JSON-lines log is the L105
# side-effect counter ("call" events) and the pid registry ("start").
# ---------------------------------------------------------------------------


def _fixture_log_events(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _fixture_call_count(log: Path) -> int:
    """The external side-effect counter: tools/call attempts the server saw."""
    return sum(1 for event in _fixture_log_events(log) if event.get("event") == "call")


def _fixture_server_pid(log: Path) -> int:
    for event in _fixture_log_events(log):
        if event.get("event") == "start":
            pid = event.get("pid")
            if isinstance(pid, int):
                return pid
    pytest.fail(f"no fixture server start event in {log}")


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` names a live process (the test_mcp_stdio.py probe idiom)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            answered = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(answered) and exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_pids_gone(pids: Sequence[int], *, timeout_s: float = 10.0) -> None:
    """No child-process leak after a kill: every pid must be gone."""
    deadline = time.monotonic() + timeout_s
    while any(_pid_alive(pid) for pid in pids):
        if time.monotonic() > deadline:
            pytest.fail(f"child processes still alive after the kill: {list(pids)}")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# The strict pairing audits (task clause 4; tests/r0 stays untouched).
# ---------------------------------------------------------------------------


def _assert_ordered_subsequence(sequence: Sequence[str], expected: Sequence[str]) -> None:
    """``expected`` appears in ``sequence`` in order (gaps allowed)."""
    iterator = iter(sequence)
    for want in expected:
        for got in iterator:
            if got == want:
                break
        else:
            pytest.fail(f"missing {want!r} in order within {list(sequence)} (expected {list(expected)})")


def _audit_tool_pairing(db_path: Path, session_id: str, expected: Sequence[tuple[str, str, str]]) -> None:
    """Durable-side strict pairing: every ``py.tool.requested`` execution joins
    exactly one ``py.tool.finished`` terminal by execution_id, every planned
    tool part of every ``py.step.generated`` message was requested (the §3.3
    L214 never-invoked planned-call face included), and the per-tool terminals
    match ``expected`` as (requested_name, state, effect_status)."""
    requested = _event_data(db_path, session_id, "py.tool.requested")
    finished = _event_data(db_path, session_id, "py.tool.finished")
    names = {str(row["execution_id"]): str(row["requested_name"]) for row in requested}
    terminals = {str(row["execution_id"]): row for row in finished}
    assert len(names) == len(requested), "duplicate execution ids among py.tool.requested rows"
    assert len(terminals) == len(finished), "duplicate execution ids among py.tool.finished rows"
    assert set(terminals) == set(names), f"unpaired executions: requested={sorted(names)} finished={sorted(terminals)}"
    for generated in _event_data(db_path, session_id, "py.step.generated"):
        message = generated.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool":
                execution_id = str(part.get("execution_id"))
                assert execution_id in names, f"planned call {execution_id} was never requested"
    observed = sorted(
        (names[str(row["execution_id"])], str(row["state"]), str(row["effect_status"])) for row in finished
    )
    assert observed == sorted(expected)


def _audit_wire_pairing(body: object, needles: dict[str, str], exact: dict[str, str] | None = None) -> None:
    """Wire-side strict pairing over one recorded request body: every legal
    assistant tool_call id has exactly one role:"tool" result -- zero missing,
    zero duplicates, zero extras -- and each result's content matches its
    window's nature (needles: substring; exact: verbatim reuse)."""
    messages = _wire_messages(body)
    call_ids: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    call_ids.append(str(call["id"]))
    assert len(call_ids) == len(set(call_ids)), f"duplicate assistant tool_call ids on the wire: {call_ids}"
    results: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        assert isinstance(tool_call_id, str), f"tool result without tool_call_id: {message}"
        assert tool_call_id not in results, f"duplicate tool result for {tool_call_id}"
        results[tool_call_id] = _content_str(message.get("content"))
    assert set(results) == set(call_ids), (
        f"unpaired wire tool results: calls={sorted(set(call_ids))} results={sorted(results)}"
    )
    for call_id, needle in needles.items():
        assert needle in results[call_id], f"{call_id}: expected {needle!r} within {results[call_id]!r}"
    for call_id, text in (exact or {}).items():
        assert results[call_id] == text, f"{call_id}: expected exactly {text!r}, got {results[call_id]!r}"


# ---------------------------------------------------------------------------
# Window-2 wrapper + close-order recorders (test-side assets; tests/r0 and
# every existing test file stay untouched).
# ---------------------------------------------------------------------------


class _ParkedAfterCallAdapter:
    """The task-book sanctioned window-2 injection: run the REAL MCP call
    (the server-side side effect really happens and is counted), drop the
    marker file, then park forever -- a stable, repeatable mid-execution
    window where ``py.tool.started`` is durable and ``py.tool.finished`` is
    not."""

    def __init__(self, inner: MCPToolAdapter, marker: Path) -> None:
        self._inner = inner
        self._marker = marker

    @property
    def spec(self) -> ToolSpec:
        return self._inner.spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        await self._inner.execute(context, input)
        self._marker.write_text("parked after the real call", encoding="utf-8")
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the park never resolves")  # pragma: no cover


class _OrderRecordingManager(McpManager):
    """A manager whose close() records the §7 L339 MCP slot and proves the
    database is still readable while MCP closes (the App's engine dispose and
    writer-lock release happen strictly later)."""

    def __init__(self, order: list[str], db_path: Path) -> None:
        super().__init__()
        self._order = order
        self._db_path = db_path

    async def close(self) -> None:
        self._order.append("mcp:start")
        _fetch_all(self._db_path, "SELECT count(*) FROM events")
        self._order.append("mcp:db-readable")
        await super().close()
        self._order.append("mcp:done")


class _OrderRecordingProvider(MaasAdapter):
    """A provider whose close() records the §7 L339 provider slot (and proves
    the DB is still readable inside the provider close -- the App closes the
    database handle strictly after)."""

    def __init__(self, base_url_v1: str, order: list[str], db_path: Path) -> None:
        super().__init__(base_url_v1, "test-key")
        self._order = order
        self._db_path = db_path

    async def close(self) -> None:
        _fetch_all(self._db_path, "SELECT count(*) FROM events")
        self._order.append("provider")


# ---------------------------------------------------------------------------
# The crash-window driver: a self-contained script written at runtime into
# the pytest tmp_path (zero temp files in the worktree), spawned as a real
# child process and killed by the test at its chosen window.
# ---------------------------------------------------------------------------

_DRIVER_SOURCE = '''"""R4 e2e crash-window driver (T-C1-041): the full stack in a child process.

Every input arrives through R4_DRIVER_* environment variables; every artifact
(session id, wrapper marker, crash traceback) lands in files the parent
polls. The child parks forever after the prompt is accepted, so the parent
can kill it inside a stable blocked window.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.llm import ModelRef
from opencode.mcp.bridge import MCPToolAdapter
from opencode.mcp.manager import McpManager, StdioServerDefinition
from opencode.session.context import RuntimeSettings
from opencode.tools.policy import PermissionRule
from opencode.tools.registry import mcp_tool_spec


class _ParkedAfterCallAdapter:
    """Run the REAL MCP call, drop the marker, park forever (window 2)."""

    def __init__(self, inner, marker):
        self._inner = inner
        self._marker = marker

    @property
    def spec(self):
        return self._inner.spec

    async def execute(self, context, input):
        await self._inner.execute(context, input)
        self._marker.write_text("parked after the real call", encoding="utf-8")
        await asyncio.Event().wait()


async def main() -> int:
    db_path = Path(os.environ["R4_DRIVER_DB"])
    base_url = os.environ["R4_DRIVER_BASE_URL"]
    working_dir = os.environ["R4_DRIVER_WORKDIR"]
    session_out = Path(os.environ["R4_DRIVER_SESSION_OUT"])
    prompt_text = os.environ["R4_DRIVER_PROMPT"]
    request_id = os.environ["R4_DRIVER_REQUEST_ID"]
    tool_names = [name for name in os.environ["R4_DRIVER_TOOLS"].split(",") if name]
    hang_tool = os.environ["R4_DRIVER_HANG_TOOL"]
    allow_actions = [action for action in os.environ["R4_DRIVER_ALLOW"].split(",") if action]
    marker = Path(os.environ["R4_DRIVER_MARKER"]) if os.environ["R4_DRIVER_MARKER"] else None
    mcp_log = os.environ["R4_DRIVER_MCP_LOG"]
    server_script = os.environ["R4_DRIVER_SERVER"]
    error_path = Path(os.environ["R4_DRIVER_ERROR"])
    try:
        manager = McpManager()
        manager.add_server(
            StdioServerDefinition(
                name="fixture",
                command=sys.executable,
                args=(server_script,),
                env={
                    "MCP_FIXTURE_MODE": "ok",
                    "MCP_FIXTURE_LOG": mcp_log,
                    "PYTHONUNBUFFERED": "1",
                },
            )
        )
        remote_tools = await manager.start_server("fixture")
        by_name = {tool.name: tool for tool in remote_tools}
        adapters = []
        for name in tool_names:
            remote = by_name[name]
            adapter = MCPToolAdapter(
                manager=manager,
                server="fixture",
                tool=remote,
                spec=mcp_tool_spec(remote.name, remote.description, remote.input_schema),
            )
            if marker is not None and name == hang_tool:
                adapter = _ParkedAfterCallAdapter(adapter, marker)
            adapters.append(adapter)
        app = App(
            db_path,
            working_dir=working_dir,
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            tools=adapters,
            settings=RuntimeSettings(
                model=ModelRef(provider="maas", model="fake-model"),
                context_limit=100_000,
                output_reserve=4096,
                timeout_s=30.0,
            ),
            rules=[PermissionRule(action=action, effect="allow") for action in allow_actions],
            mcp_manager=manager,
        )
        session = await app.service.create(ConfigSnapshot.create())
        session_out.write_text(session.session_id, encoding="utf-8")
        await app.service.prompt(session.session_id, prompt_text, request_id)
        await asyncio.Event().wait()  # parked: the parent kills us in its window
    except BaseException:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
'''


@asynccontextmanager
async def _driver(
    db_path: Path,
    tmp_path: Path,
    base_url: str,
    *,
    tools: Sequence[str],
    allow: Sequence[str],
    prompt_text: str,
    request_id: str,
    mcp_log: Path,
    hang_tool: str = "",
    marker: Path | None = None,
) -> AsyncIterator[subprocess.Popen[bytes]]:
    """Spawn the driver child over the shared DB + fake server.

    The test kills the child inside its window (``process.kill()``); this
    context manager is only the safety net (kill-if-alive on exit) plus the
    crash-traceback surface: if the child died on its own before the window,
    its written traceback fails the test with the real reason.
    """
    driver_path = tmp_path / "driver.py"
    session_out = tmp_path / "session_id.txt"
    error_path = tmp_path / "driver_error.txt"
    driver_path.write_text(_DRIVER_SOURCE, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "R4_DRIVER_DB": str(db_path),
        "R4_DRIVER_BASE_URL": base_url,
        "R4_DRIVER_WORKDIR": str(tmp_path),
        "R4_DRIVER_SESSION_OUT": str(session_out),
        "R4_DRIVER_PROMPT": prompt_text,
        "R4_DRIVER_REQUEST_ID": request_id,
        "R4_DRIVER_TOOLS": ",".join(tools),
        "R4_DRIVER_HANG_TOOL": hang_tool,
        "R4_DRIVER_ALLOW": ",".join(allow),
        "R4_DRIVER_MARKER": str(marker) if marker is not None else "",
        "R4_DRIVER_MCP_LOG": str(mcp_log),
        "R4_DRIVER_SERVER": str(SERVER_SCRIPT),
        "R4_DRIVER_ERROR": str(error_path),
    }
    process = subprocess.Popen[bytes]([sys.executable, str(driver_path)], cwd=str(tmp_path), env=env)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if error_path.is_file():
            pytest.fail(f"driver child crashed before the kill window:\n{error_path.read_text(encoding='utf-8')}")


# ---------------------------------------------------------------------------
# ① Window 1: a real kill while the MCP ask approval is pending.
# ---------------------------------------------------------------------------


async def test_w1_kill_while_approval_pending(tmp_path: Path) -> None:
    """① (真实 kill): the run parks on a real ``py.permission.requested`` for
    the MCP ask tool; the parent kills the driver child there. The window's
    durable self-proof holds (requested/permission.requested committed,
    tool.started absent, zero server-side calls), the restart recovers the
    pending approval to cancelled and the never-started tool to
    cancelled/not_started, and the post-recovery control prompt proves zero
    reruns and the strict pairing on the wire."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    mcp_log = tmp_path / "fixture.log"
    server = FakeOpenAIServer(
        [
            _tool_calls_response("let me echo", ("call_echo", "mcp__echo", {"text": "golden"})),
            _text_response("the control face"),
        ]
    )
    base_url = await server.start()
    try:
        async with _driver(
            db_path,
            tmp_path,
            base_url,
            tools=("echo",),
            allow=(),
            prompt_text="echo golden",
            request_id="req_1",
            mcp_log=mcp_log,
        ) as process:
            session_id = await _await_file_text(tmp_path / "session_id.txt")
            await _await_event_count(db_path, session_id, "py.permission.requested", 1)
            # window self-proof: the stable blocked state, before the kill
            types = _event_types(db_path, session_id)
            assert types.count("py.permission.requested") == 1
            assert types.count("py.tool.requested") == 1
            assert types.count("py.step.generated") == 1
            assert types.count("py.tool.started") == 0
            assert _fixture_call_count(mcp_log) == 0
            process.kill()
            process.wait(timeout=10)
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])
        assert _fixture_call_count(mcp_log) == 0  # L105: zero side effects, ever

        run_id = _run_id_of(db_path, session_id)
        permission_id = str(_event_data(db_path, session_id, "py.permission.requested")[0]["permission_id"])
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            report = await app.recovery_gate.wait()
            assert len(report.sessions) == 1
            entry = report.sessions[0]
            assert entry.session_id == session_id
            assert entry.outcome == "recovered"
            assert entry.closed_runs[0][0] == run_id
            assert entry.closed_permissions == (permission_id,)
            assert len(entry.tool_terminals) == 1
            assert len(entry.step_terminals) == 1

            types = _event_types(db_path, session_id)
            assert [str(row["outcome"]) for row in _event_data(db_path, session_id, "py.permission.closed")] == [
                "cancelled"
            ]
            assert types.count("py.tool.started") == 0  # never auto-rerun
            _assert_ordered_subsequence(
                types,
                [
                    "py.permission.requested",
                    "py.permission.closed",
                    "py.tool.finished",
                    "py.step.finished",
                    "py.run.finished",
                ],
            )

            # acceptance C back half: a fresh prompt after recovery
            run = await app.service.prompt(session_id, "control after recovery", "req_2")
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=30.0)
            assert terminal.status.value == "succeeded"
            assert _fixture_call_count(mcp_log) == 0  # the side effect still never ran
            _audit_tool_pairing(db_path, session_id, [("mcp__echo", "cancelled", "not_started")])
            _audit_wire_pairing(server.requests[-1].body, needles={"call_echo": "cancelled before it started"})
        # recovery idempotence: a third open finds nothing left to close
        async with App(db_path, working_dir=str(tmp_path)) as app_idle:
            assert (await app_idle.recovery_gate.wait()).sessions == ()
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# ② Window 2: a real kill while the MCP call is executing.
# ---------------------------------------------------------------------------


async def test_w2_kill_while_tool_executing(tmp_path: Path) -> None:
    """② (真实 kill; the blocked state itself is the sanctioned test-side
    adapter wrapper): the real MCP call completes server-side (counter==1),
    the wrapper parks, ``py.tool.started`` is durable and ``py.tool.finished``
    is not; the kill lands there. The restart synthesizes the unknown
    terminal, and the post-recovery control prompt proves the call is never
    re-executed (counter stays 1) with exactly one -- synthesized -- wire
    result for the call."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    mcp_log = tmp_path / "fixture.log"
    marker = tmp_path / "wrapper.marker"
    server = FakeOpenAIServer(
        [
            _tool_calls_response("let me echo", ("call_echo", "mcp__echo", {"text": "golden"})),
            _text_response("the control face"),
        ]
    )
    base_url = await server.start()
    try:
        async with _driver(
            db_path,
            tmp_path,
            base_url,
            tools=("echo",),
            allow=("mcp__echo",),
            prompt_text="echo golden",
            request_id="req_1",
            mcp_log=mcp_log,
            hang_tool="echo",
            marker=marker,
        ) as process:
            session_id = await _await_file_text(tmp_path / "session_id.txt")
            await _await_file_text(marker)
            await _await_event_count(db_path, session_id, "py.tool.started", 1)
            # window self-proof: one started in-flight call, no terminal yet
            types = _event_types(db_path, session_id)
            assert types.count("py.tool.requested") == 1
            assert types.count("py.tool.started") == 1
            assert types.count("py.tool.finished") == 0
            assert types.count("py.step.generated") == 1
            assert types.count("py.permission.requested") == 0  # allow rule: no approval
            assert _fixture_call_count(mcp_log) == 1  # the external side effect happened exactly once
            process.kill()
            process.wait(timeout=10)
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])

        run_id = _run_id_of(db_path, session_id)
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            report = await app.recovery_gate.wait()
            assert len(report.sessions) == 1
            entry = report.sessions[0]
            assert entry.session_id == session_id
            assert entry.outcome == "recovered"
            assert entry.closed_runs[0][0] == run_id
            assert entry.closed_permissions == ()
            assert len(entry.tool_terminals) == 1
            assert len(entry.step_terminals) == 1

            types = _event_types(db_path, session_id)
            assert types.count("py.tool.started") == 1
            assert types.count("py.tool.finished") == 1
            _assert_ordered_subsequence(
                types,
                ["py.tool.started", "py.tool.finished", "py.step.finished", "py.run.finished"],
            )

            run = await app.service.prompt(session_id, "control after recovery", "req_2")
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=30.0)
            assert terminal.status.value == "succeeded"
            assert _fixture_call_count(mcp_log) == 1  # pinned: no second execution
            _audit_tool_pairing(db_path, session_id, [("mcp__echo", "unknown", "unknown")])
            _audit_wire_pairing(server.requests[-1].body, needles={"call_echo": "cancelled while running"})
        async with App(db_path, working_dir=str(tmp_path)) as app_idle:
            assert (await app_idle.recovery_gate.wait()).sessions == ()
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# ③ Window 3: a real kill after tool.finished, during the next generation.
# ---------------------------------------------------------------------------


async def test_w3_kill_while_generation_streaming(tmp_path: Path) -> None:
    """③ (真实 kill): the first tool call completes and its ``py.tool.finished``
    + ``py.step.finished`` are durable; the second generation's provider
    stream parks on the fake server's ``hang_after_frames`` button. The kill
    lands there; the restart must reuse the committed real result verbatim
    (never re-run the tool) and fold the open step as interrupted."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    mcp_log = tmp_path / "fixture.log"
    server = FakeOpenAIServer(
        [
            _tool_calls_response("let me echo", ("call_echo", "mcp__echo", {"text": "golden"})),
            ScriptedResponse(frames=[], hang_after_frames=True),
            _text_response("the control face"),
        ]
    )
    base_url = await server.start()
    try:
        async with _driver(
            db_path,
            tmp_path,
            base_url,
            tools=("echo",),
            allow=("mcp__echo",),
            prompt_text="echo golden",
            request_id="req_1",
            mcp_log=mcp_log,
        ) as process:
            session_id = await _await_file_text(tmp_path / "session_id.txt")
            await _await_event_count(db_path, session_id, "py.tool.finished", 1)
            await _await_server_requests(server, 2)
            # window self-proof: step 1 closed, step 2 open mid-stream
            types = _event_types(db_path, session_id)
            assert types.count("py.run.started") == 1
            assert types.count("py.step.started") == 2
            assert types.count("py.step.generated") == 1
            assert types.count("py.tool.finished") == 1
            assert types.count("py.step.finished") == 1
            assert _fixture_call_count(mcp_log) == 1
            process.kill()
            process.wait(timeout=10)
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])

        run_id = _run_id_of(db_path, session_id)
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            report = await app.recovery_gate.wait()
            assert len(report.sessions) == 1
            entry = report.sessions[0]
            assert entry.session_id == session_id
            assert entry.outcome == "recovered"
            assert entry.closed_runs[0][0] == run_id
            assert entry.tool_terminals == ()  # no open tool: only the open step folds
            assert len(entry.step_terminals) == 1

            types = _event_types(db_path, session_id)
            assert types.count("py.step.generated") == 1  # no half-batch resurrection
            assert types.count("py.tool.finished") == 1  # the committed result stays as-is
            assert types.count("py.step.finished") == 2
            assert types.count("py.run.finished") == 1
            _assert_ordered_subsequence(
                types,
                [
                    "py.tool.started",
                    "py.tool.finished",
                    "py.step.finished",
                    "py.step.started",
                    "py.step.finished",
                    "py.run.finished",
                ],
            )

            run = await app.service.prompt(session_id, "control after recovery", "req_2")
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=30.0)
            assert terminal.status.value == "succeeded"
            assert _fixture_call_count(mcp_log) == 1  # verbatim reuse, never re-run
            _audit_tool_pairing(db_path, session_id, [("mcp__echo", "succeeded", "completed")])
            _audit_wire_pairing(server.requests[-1].body, needles={}, exact={"call_echo": "echo: golden"})
        async with App(db_path, working_dir=str(tmp_path)) as app_idle:
            assert (await app_idle.recovery_gate.wait()).sessions == ()
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# ④ The multi-call plan: every planned call pairs exactly once.
# ---------------------------------------------------------------------------


async def test_multi_call_plan_recovery_pairs_every_call(tmp_path: Path) -> None:
    """④ (真实 kill at a stable mid-plan block): one generation plans two MCP
    calls; call A (echo) executes and finishes, call B (structured) is started
    and parked in the wrapper when the kill lands. The recovery synthesizes
    exactly one terminal per planned execution (A keeps its real result, B
    folds unknown), and the post-recovery request body shows BOTH calls with
    exactly one result each -- zero missing, zero duplicates, zero extras
    (§3.3 L214 second sentence, including the not-yet-invoked planned call
    face -- here B was invoked but unfinished; the never-invoked variant is
    the W1 face where the whole plan folds not_started)."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    mcp_log = tmp_path / "fixture.log"
    marker = tmp_path / "wrapper.marker"
    server = FakeOpenAIServer(
        [
            _tool_calls_response(
                "let me plan",
                ("call_echo", "mcp__echo", {"text": "golden"}),
                ("call_structured", "mcp__structured", {}),
            ),
            _text_response("the control face"),
        ]
    )
    base_url = await server.start()
    try:
        async with _driver(
            db_path,
            tmp_path,
            base_url,
            tools=("echo", "structured"),
            allow=("mcp__echo", "mcp__structured"),
            prompt_text="run the plan",
            request_id="req_1",
            mcp_log=mcp_log,
            hang_tool="structured",
            marker=marker,
        ) as process:
            session_id = await _await_file_text(tmp_path / "session_id.txt")
            await _await_file_text(marker)
            await _await_event_count(db_path, session_id, "py.tool.started", 2)
            # window self-proof: A finished, B started and parked
            types = _event_types(db_path, session_id)
            assert types.count("py.step.generated") == 1
            assert types.count("py.tool.requested") == 2
            assert types.count("py.tool.started") == 2
            assert types.count("py.tool.finished") == 1
            assert _fixture_call_count(mcp_log) == 2  # both real calls reached the server
            process.kill()
            process.wait(timeout=10)
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])

        run_id = _run_id_of(db_path, session_id)
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            report = await app.recovery_gate.wait()
            assert len(report.sessions) == 1
            entry = report.sessions[0]
            assert entry.session_id == session_id
            assert entry.outcome == "recovered"
            assert entry.closed_runs[0][0] == run_id
            assert len(entry.tool_terminals) == 1  # exactly B's synthesis
            assert len(entry.step_terminals) == 1

            types = _event_types(db_path, session_id)
            assert types.count("py.tool.finished") == 2
            _assert_ordered_subsequence(
                types,
                [
                    "py.step.generated",
                    "py.tool.started",
                    "py.tool.finished",
                    "py.tool.started",
                    "py.tool.finished",
                    "py.step.finished",
                    "py.run.finished",
                ],
            )

            run = await app.service.prompt(session_id, "control after recovery", "req_2")
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=30.0)
            assert terminal.status.value == "succeeded"
            assert _fixture_call_count(mcp_log) == 2  # neither call re-executed
            _audit_tool_pairing(
                db_path,
                session_id,
                [("mcp__echo", "succeeded", "completed"), ("mcp__structured", "unknown", "unknown")],
            )
            _audit_wire_pairing(
                server.requests[-1].body,
                needles={"call_structured": "cancelled while running"},
                exact={"call_echo": "echo: golden"},
            )
        async with App(db_path, working_dir=str(tmp_path)) as app_idle:
            assert (await app_idle.recovery_gate.wait()).sessions == ()
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# ⑤ The generation half-batch injection (monkeypatch form).
# ---------------------------------------------------------------------------


async def test_generation_half_batch_crash_leaves_no_orphan_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⑤ (monkeypatch 注入): a class-level ``EventCommitter.commit`` hook
    raises BEFORE the store call when the batch carries ``py.step.generated``
    -- the closest in-process analog of the process dying between generation
    and its atomic commit (the run task's error is swallowed into the service
    schedule-error slot, exactly like a crashed task; nothing further
    commits). n-3 carried note: this injection deliberately bypasses the
    committer's per-session lock, the store call and the bus notify -- the
    real torn-state projection face stays with the store/projector suites.
    The interrupted batch must leave ZERO orphan rows (no finalized, no
    generated), the healthy contrast face lands finalized+generated under one
    batch_id, and the startup scan never resurrects the half batch."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    server = FakeOpenAIServer(
        [
            _text_response("the interrupted generation"),
            _text_response("the control face"),
        ]
    )
    base_url = await server.start()
    fired = asyncio.Event()
    armed = True
    original_commit = EventCommitter.commit

    async def _crash_before_store(
        self: EventCommitter, session_id: str, drafts: Sequence[EventDraft[Any]]
    ) -> list[DurableEvent[Any]]:
        nonlocal armed
        if armed and any(draft.type == PY_STEP_GENERATED for draft in drafts):
            armed = False  # armed once: the reopen + control face commit normally
            fired.set()
            raise RuntimeError("injected process death before the generation batch reached the store")
        return await original_commit(self, session_id, drafts)

    monkeypatch.setattr(EventCommitter, "commit", _crash_before_store)
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            run = await app.service.prompt(session_id, "write the report", "req_1")
            await fired.wait()
            await asyncio.sleep(0.05)  # let the run task finish dying before asserting
            types = _event_types(db_path, session_id)
            assert types.count("py.step.started") == 1
            assert types.count("py.text.finalized") == 0  # no orphan rows from the
            assert types.count("py.step.generated") == 0  # interrupted batch
            assert types.count("py.step.finished") == 0
            assert types.count("py.run.finished") == 0
            status = await app.service.run_status(run.run_id)
            assert status.status.value == "running"

        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=MaasAdapter(f"{base_url}/v1", "test-key"),
            settings=_settings(),
        ) as app:
            report = await app.recovery_gate.wait()
            assert len(report.sessions) == 1
            entry = report.sessions[0]
            assert entry.session_id == session_id
            assert entry.outcome == "recovered"
            assert entry.closed_runs[0][0] == run.run_id
            assert entry.closed_permissions == ()
            assert entry.tool_terminals == ()
            assert len(entry.step_terminals) == 1

            types = _event_types(db_path, session_id)
            assert types.count("py.text.finalized") == 0  # the scan does not resurrect
            assert types.count("py.step.generated") == 0  # the half batch
            assert types.count("py.step.finished") == 1
            assert types.count("py.run.finished") == 1
            assert [str(row["outcome"]) for row in _event_data(db_path, session_id, "py.run.finished")] == [
                "interrupted"
            ]
            _assert_ordered_subsequence(types, ["py.step.started", "py.step.finished", "py.run.finished"])

            # the healthy contrast face: one fresh prompt lands the full atomic batch
            control = await app.service.prompt(session_id, "control after recovery", "req_2")
            terminal = await _await_terminal(app.service, control.run_id, timeout_s=30.0)
            assert terminal.status.value == "succeeded"
            rows = _fetch_all(
                db_path,
                "SELECT type, batch_id FROM events WHERE session_id = ?"
                " AND type IN ('py.text.finalized', 'py.step.generated') ORDER BY seq",
                (session_id,),
            )
            assert [row[0] for row in rows] == ["py.text.finalized", "py.step.generated"]
            assert len({row[1] for row in rows}) == 1  # one atomic batch: finalized + generated
        async with App(db_path, working_dir=str(tmp_path)) as app_idle:
            assert (await app_idle.recovery_gate.wait()).sessions == ()
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# ⑥ The shutdown/crash race: §7 L339 close order + durable fold sequence.
# Split into ⑥a (approval-park + provider-stream park) and ⑥b (mid-execution
# park past the close timeout): the registry's Phase-1 serial invoke lock
# means only one invoke can sit inside the pipeline at a time, so the task
# book's literal combined close (one App holding BOTH an approval-parked and
# a mid-execution-parked invoke) is structurally unreachable -- ④ float
# report; each face is pinned deterministically instead.
# ---------------------------------------------------------------------------


async def test_close_folds_pending_approval_then_mcp_provider_db(tmp_path: Path) -> None:
    """⑥a (in-process race at the App composition root): two live sessions in
    one App -- A parks on a real ``fs__write`` approval, B parks in the
    provider stream (a pair that needs no second registry invoke, so the
    Phase-1 serial lock never blocks either park) -- then ``App.close()``
    with the injected short ``close_timeout_s=1.0``. The durable fold must
    read: shutdown intent -> closed approval -> not-started tool terminal ->
    interrupted step/run for A, and shutdown intent -> interrupted step/run
    for B (the hard-cancel + grace face: B is past the 1.0s wait); the
    resource order must be MCP (DB still readable inside it) -> provider
    (DB still readable) -> DB handle, and the never-approved write never
    happens."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    order: list[str] = []
    mcp_log = tmp_path / "fixture.log"
    server = FakeOpenAIServer(
        [
            _tool_calls_response(
                "let me write", ("call_write", "fs__write", {"path": "note.txt", "content": "side effect\n"})
            ),
            ScriptedResponse(frames=[], hang_after_frames=True),
        ]
    )
    base_url = await server.start()
    manager = _OrderRecordingManager(order, db_path)
    manager.add_server(
        StdioServerDefinition(
            name="fixture",
            command=sys.executable,
            args=(str(SERVER_SCRIPT),),
            env={"MCP_FIXTURE_MODE": "ok", "MCP_FIXTURE_LOG": str(mcp_log), "PYTHONUNBUFFERED": "1"},
        )
    )
    await manager.start_server("fixture")  # started but never invoked: no MCP tool is registered
    provider = _OrderRecordingProvider(f"{base_url}/v1", order, db_path)
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsWriteAdapter(),),
            settings=_settings(),
            mcp_manager=manager,
            close_timeout_s=1.0,
        ) as app:
            session_a = await app.service.create(ConfigSnapshot.create())
            await app.service.prompt(session_a.session_id, "write the note", "req_a")
            await _await_event_count(db_path, session_a.session_id, "py.permission.requested", 1)
            session_b = await app.service.create(ConfigSnapshot.create())
            await app.service.prompt(session_b.session_id, "tell me something", "req_b")
            await _await_event_count(db_path, session_b.session_id, "py.step.started", 1)

            await app.close()
            assert order == _CLOSE_ORDER
            assert app.database.closed is True
            assert not (tmp_path / "note.txt").exists()  # the never-approved write never ran
            assert _fixture_call_count(mcp_log) == 0  # the fixture was never invoked

            # session A: intent -> closed approval -> not-started terminal -> interrupted fold
            types_a = _event_types(db_path, session_a.session_id)
            _assert_ordered_subsequence(
                types_a,
                [
                    "py.run.cancel_requested",
                    "py.permission.closed",
                    "py.tool.finished",
                    "py.step.finished",
                    "py.run.finished",
                ],
            )
            assert [
                str(row["reason"]) for row in _event_data(db_path, session_a.session_id, "py.run.cancel_requested")
            ] == ["shutdown"]
            assert [
                str(row["outcome"]) for row in _event_data(db_path, session_a.session_id, "py.permission.closed")
            ] == ["cancelled"]
            assert [
                (str(row["state"]), str(row["effect_status"]))
                for row in _event_data(db_path, session_a.session_id, "py.tool.finished")
            ] == [("cancelled", "not_started")]
            assert [str(row["outcome"]) for row in _event_data(db_path, session_a.session_id, "py.run.finished")] == [
                "interrupted"
            ]

            # session B: mid-stream park -> hard cancel + grace -> interrupted fold
            types_b = _event_types(db_path, session_b.session_id)
            _assert_ordered_subsequence(types_b, ["py.run.cancel_requested", "py.step.finished", "py.run.finished"])
            assert [str(row["outcome"]) for row in _event_data(db_path, session_b.session_id, "py.run.finished")] == [
                "interrupted"
            ]
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])
    finally:
        await server.aclose()


async def test_close_timeout_folds_in_flight_mcp_call_unknown(tmp_path: Path) -> None:
    """⑥b (in-process race, the close-timeout face): one session parked
    mid-execution in the task-book sanctioned wrapper (the REAL fixture call
    already went out and is counted) past the injected ``close_timeout_s=1.0``
    wait, so ``App.close()`` hard-cancels the straggler and the grace period
    carries its CancelledError handler's final commits: the honest
    unknown/unknown terminal for a started call whose result could not be
    confirmed, then the interrupted step/run. The §7 L339 resource order
    holds and the external call is never re-executed."""
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    order: list[str] = []
    marker = tmp_path / "wrapper.marker"
    mcp_log = tmp_path / "fixture.log"
    server = FakeOpenAIServer([_tool_calls_response("let me echo", ("call_echo", "mcp__echo", {"text": "golden"}))])
    base_url = await server.start()
    manager = _OrderRecordingManager(order, db_path)
    manager.add_server(
        StdioServerDefinition(
            name="fixture",
            command=sys.executable,
            args=(str(SERVER_SCRIPT),),
            env={"MCP_FIXTURE_MODE": "ok", "MCP_FIXTURE_LOG": str(mcp_log), "PYTHONUNBUFFERED": "1"},
        )
    )
    remote_tools = await manager.start_server("fixture")
    echo = next(tool for tool in remote_tools if tool.name == "echo")
    echo_adapter = _ParkedAfterCallAdapter(
        MCPToolAdapter(
            manager=manager,
            server="fixture",
            tool=echo,
            spec=mcp_tool_spec(echo.name, echo.description, echo.input_schema),
        ),
        marker,
    )
    provider = _OrderRecordingProvider(f"{base_url}/v1", order, db_path)
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(echo_adapter,),
            settings=_settings(),
            rules=(PermissionRule(action="mcp__echo", effect="allow"),),
            mcp_manager=manager,
            close_timeout_s=1.0,
        ) as app:
            session_id = await app.service.create(ConfigSnapshot.create())
            await app.service.prompt(session_id.session_id, "echo golden", "req_b")
            await _await_file_text(marker)
            await _await_event_count(db_path, session_id.session_id, "py.tool.started", 1)

            await app.close()
            assert order == _CLOSE_ORDER
            assert app.database.closed is True
            assert _fixture_call_count(mcp_log) == 1  # close does not re-execute the call

            # the in-flight call folds unknown/unknown inside the grace window
            types = _event_types(db_path, session_id.session_id)
            _assert_ordered_subsequence(
                types,
                ["py.run.cancel_requested", "py.tool.finished", "py.step.finished", "py.run.finished"],
            )
            assert [
                (str(row["state"]), str(row["effect_status"]))
                for row in _event_data(db_path, session_id.session_id, "py.tool.finished")
            ] == [("unknown", "unknown")]
            assert [str(row["outcome"]) for row in _event_data(db_path, session_id.session_id, "py.run.finished")] == [
                "interrupted"
            ]
        await _wait_pids_gone([_fixture_server_pid(mcp_log)])
    finally:
        await server.aclose()
