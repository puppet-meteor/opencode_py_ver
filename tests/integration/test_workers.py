"""Integration tests for the P3-S3 worker face (T-C1-078): real subprocesses.

Honest-label note: the ``llm_call``/``tool`` seams keep the S2-style recording
placeholders (the agent pipeline spec never executes them); everything under
test here is real -- a real migrated main jobs database, the real
``JobService`` commit spine with the S3 cancel hook, the real compiled graph
over the real :class:`~opencode.orchestrator.workers.WorkerManager`: ``git
worktree`` arenas under a tiny seeded repository, the real ``opencode`` CLI
subprocess for the end-to-end worker session (tests/r0's scripted SSE fake
over real HTTP), and scripted ``python -c`` workers for the faces the real
CLI cannot play on demand (budget overruns, tree kills, gate violations).

Coverage (the S3 acceptance judges; the brief-render and contract faces are
pinned by ``tests/contract/test_workers.py``):

* judge 5 -- a real worker session end to end: the CLI runs inside the
  worktree against the scripted provider, answers the structured ``[final]``
  JSON, and the clean success removes the worktree (brief unlink, then
  ``git worktree remove`` -- never a force);
* judge 4 -- the acceptance gate as the second line: a whitelisted-dirty
  success keeps the worktree for the merge-back face; writes outside the
  whitelist fail ``PermissionDenied``; claimed-but-absent files and artifacts
  fail ``ValidationError``; an artifact outside the whitelist fails
  ``PermissionDenied``; stdout without a ``[final]`` marker fails
  ``ToolFailed``;
* judge 3 -- the budget trio: the wall-clock timeout kills the tree and
  fails ``ToolTimeout``; a max-tokens line crossed before exit fails the
  deterministic post-hoc check; a max-tool-calls line crossed mid-run latches
  on the watchdog and the runner kills the process tree -- the scripted
  grandchild dies with it (JobObject membership) and the worktree is kept;
* judge 6 -- cancellation through the service: ``cancel`` fires the
  canceller before the terminal emit, the worker tree dies, and the drive's
  ``WorkerSuspended`` path leaves the folded ``cancelled`` terminal untouched;
* the S3 resume defenses (T-B-092 ①/②): a resume drive with no advisory
  cursor fails closed ``ExecutionUnknown``; a cursor with no pipeline spec
  hash fails the same way -- with real worker side effects a re-run can
  never be proven, so it is never attempted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from sys import platform as _PLATFORM
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

from opencode.contracts.events import PY_STEP_FINISHED, PY_TOOL_REQUESTED, JobBudget
from opencode.contracts.messages import JsonObject
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.jobs import JobHandle, JobService, JobsProjector, job_stream_key
from opencode.orchestrator.workers import WorkerManager
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobRecord, JobsReadRepository, JobsStore
from tests.integration.test_cli import _ServerHost
from tests.r0.fake_openai_server import Frame, ScriptedResponse, finish_frame, text_delta_frame, usage_frame

if _PLATFORM == "win32":
    import ctypes
    from ctypes import wintypes

REPO_ROOT = Path(__file__).resolve().parents[2]

_T0 = datetime(2026, 11, 13, 7, 30, 0, tzinfo=UTC)
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: The structured return every scripted worker claims (a valid WorkerReturn).
_WORKER_RETURN: dict[str, Any] = {
    "schema_version": 1,
    "summary": "assignment executed",
    "files_changed": [],
    "artifacts": [],
    "tests_passed": True,
}


def _final_line(**overrides: Any) -> str:
    """One single-line ``[final]`` JSON payload with per-test claim overrides."""
    return json.dumps({**_WORKER_RETURN, **overrides})


def _worker_script(
    *,
    rows: list[tuple[str, str]] | None = None,
    writes: dict[str, str] | None = None,
    final: str | None = None,
    spawn_child: bool = False,
    sleep_s: float = 0.0,
) -> str:
    """The scripted worker body (honestly labeled: not the real CLI).

    Speaks the manager's argv face (``--db``): the grandchild sleeper is
    spawned *before* the accounting rows are written (a visible overrun always
    has a live tree to kill) and its pid is recorded beside the db; the
    minimal ``events`` table carries the rows the watchdog reads read-only;
    files are written relative to the CWD (the worktree); the ``[final]``
    line (the CLI's stdout face the manager parses) is printed last, then the
    body sleeps.
    """
    config = {
        "rows": rows or [],
        "writes": writes or {},
        "final": final,
        "spawn_child": spawn_child,
        "sleep_s": sleep_s,
    }
    return (
        "import json, sqlite3, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"config = json.loads({json.dumps(config)!r})\n"
        "db = None\n"
        "argv = sys.argv[1:]\n"
        "for i, flag in enumerate(argv):\n"
        "    if flag == '--db' and i + 1 < len(argv):\n"
        "        db = argv[i + 1]\n"
        "if db is None:\n"
        "    raise SystemExit('scripted worker: no --db in argv')\n"
        "if config['spawn_child']:\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "    (Path(db).parent / 'child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        "conn = sqlite3.connect(db)\n"
        "conn.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, type TEXT, data TEXT)')\n"
        "for row in config['rows']:\n"
        "    conn.execute('INSERT INTO events (type, data) VALUES (?, ?)', row)\n"
        "conn.commit()\n"
        "conn.close()\n"
        "for rel, content in config['writes'].items():\n"
        "    path = Path(rel)\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_text(content, encoding='utf-8')\n"
        "if config['final'] is not None:\n"
        "    print('[final] ' + config['final'], flush=True)\n"
        "time.sleep(config['sleep_s'])\n"
    )


# ---------------------------------------------------------------------------
# process-level probe + async poll helpers (tests/integration/test_mcp_stdio.py precedent)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a still-running process (OpenProcess probe on win32)."""
    # Direct sys.platform: mypy narrows the win32-typed ctypes face on Linux.
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but is not ours to signal
    return True


async def _wait_pids_gone(pids: Sequence[int], *, timeout_s: float = 30.0) -> None:
    """Poll the probe until every pid is confirmed terminated (no child leak).

    The 30s grace widens the teardown window for loaded CI runners: a slow
    sweep is not a leak, so only the wait is widened -- the green path still
    early-exits on the first clean poll and a true leak still fails the same
    assert.
    """
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"processes survived the tree kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# the stack: one migrated main DB + a WorkerManager-wired engine
# ---------------------------------------------------------------------------


def _tiny_repo(path: Path) -> Path:
    """One seeded git repository (identity via ``-c`` flags; no global config touched)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q"), cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=path, check=True, capture_output=True)
    subprocess.run(
        ("git", "-c", "user.email=worker@example.com", "-c", "user.name=worker", "commit", "-q", "-m", "seed"),
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


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


#: One minimal valid agent spec: a single ``opencode_agent`` terminal node.
_AGENT_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "agent",
    "entry": "worker",
    "nodes": [{"node_id": "worker", "node_type": "opencode_agent", "prompt_template": "Execute the task brief."}],
    "edges": [],
}


class _UnusedBody:
    """A placeholder for the ``llm_call``/``tool`` seams (this spec never runs them)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        self.calls.append(node.node_id)
        raise AssertionError("the agent pipeline spec never executes llm_call/tool nodes")


class _WorkerStack:
    """One migrated main DB + a real WorkerManager-wired engine (the S3 stack)."""

    def __init__(
        self, tmp_path: Path, *, worker_command: list[str] | None = None, base_url: str = "http://127.0.0.1:9"
    ) -> None:
        self.db_path = tmp_path / "main.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.repo_root = _tiny_repo(tmp_path / "repo")
        self.runs_root = tmp_path / "runs"
        self.manager = WorkerManager(
            repo_root=self.repo_root,
            runs_root=self.runs_root,
            default_budget=JobBudget(timeout_s=60.0),
            base_url=base_url,
            api_key="worker-test-key",
            model="fake-model",
            context_limit=100000,
            worker_command=worker_command,
        )
        unused = _UnusedBody()
        self.checkpointer = EngineCheckpointer(tmp_path / "advisory-cursor.sqlite3")
        self.engine = PipelineEngine(
            checkpointer=self.checkpointer, llm_call=unused, tool=unused, opencode_agent=self.manager
        )
        self.database = open_database(self.db_path, mode="create")
        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=self.engine,
            ids=AscendingIds(lambda: _T0),
            clock=lambda: _T0,
            canceller=self.manager,
        )

    async def __aenter__(self) -> _WorkerStack:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def register_agent(self) -> None:
        await self.engine.register(_AGENT_SPEC)

    async def submit(self, run: str, *, budget: JobBudget | None = None, authorized: Sequence[str] = ("out/",)) -> str:
        params: JsonObject = {
            "run": run,
            "context": "the seeded repository checked out under the worktree",
            "acceptance": "the report exists and the worker's own checks pass",
            "authorized_files": list(authorized),
        }
        handle = await self.service.submit("agent", params, "api", budget=budget)
        return handle.job_id

    async def close(self) -> None:
        await self.service.close()
        await self.database.close()
        await self.checkpointer.close()


def _error_of(record: JobRecord) -> tuple[str, str]:
    """The serialized (code, message) of a failed job's §8 error payload (assert-checked)."""
    assert record.error is not None
    code = record.error["code"]
    message = record.error["message"]
    assert isinstance(code, str)
    assert isinstance(message, str)
    return code, message


def _find_child_pid(runs_root: Path) -> int | None:
    """The first recorded grandchild pid under the run dirs, or None (sync helper)."""
    matches = sorted(runs_root.glob("runs/*/child.pid"))
    if not matches:
        return None
    return int(matches[0].read_text(encoding="utf-8").strip())


async def _await_child_pid(runs_root: Path, *, timeout_s: float = 30.0) -> int:
    """Poll the run dirs until the scripted worker records its grandchild pid.

    The blocking filesystem probe is a sync helper run via ``asyncio.to_thread``
    (the test_tool_bash ``_wait_pid_file`` precedent): the async loop only polls.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pid = await asyncio.to_thread(_find_child_pid, runs_root)
        if pid is not None:
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError("the scripted worker never recorded its child pid")


class TestRealWorkerSession:
    """Judge 5: a real ``opencode`` CLI worker session, end to end."""

    async def test_clean_success_removes_the_worktree(self, tmp_path: Path) -> None:
        """The real CLI runs inside the worktree, answers the structured return;
        a clean success unlinks the brief and removes the worktree (no force)."""
        response = ScriptedResponse(
            frames=[
                Frame(text_delta_frame(_final_line())),
                Frame(usage_frame(50, 10)),
                Frame(finish_frame("stop")),
            ]
        )
        host = _ServerHost([response])
        base_url = host.start()
        try:
            # No worker_command override: the manager's default is the real CLI.
            async with _WorkerStack(tmp_path, base_url=base_url) as stack:
                await stack.register_agent()
                job_id = await stack.submit("e2e")
                await stack.service.run(job_id)
                record = await stack.service.get_job(job_id)
        finally:
            host.stop()
        assert record.status == "done"
        assert record.error is None
        assert record.result == {
            "pipeline_id": "agent",
            "nodes": {"worker": {"worker_return": dict(_WORKER_RETURN), "worktree": None, "worktree_kept": False}},
        }
        assert not any((stack.runs_root / "worktrees").iterdir())  # the arena is gone
        run_dirs = sorted((stack.runs_root / "runs").iterdir())
        assert len(run_dirs) == 1  # the run dir is always kept
        assert (run_dirs[0] / "worker.db").is_file()  # the CLI migrated its own db


class TestAcceptanceGate:
    """Judge 4: the second line -- porcelain evidence vs the whitelist and the claims."""

    async def test_whitelisted_dirty_success_keeps_the_worktree(self, tmp_path: Path) -> None:
        """A success with whitelisted changes keeps the worktree for the merge-back
        face (the transfer itself is out of S3 scope, disclosed)."""
        script = _worker_script(
            writes={"out/report.md": "report\n"},
            final=_final_line(files_changed=["out/report.md"], artifacts=["out/report.md"]),
        )
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("dirty")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        assert record.status == "done"
        assert record.error is None
        assert len(kept) == 1
        assert (kept[0] / "out" / "report.md").read_text(encoding="utf-8") == "report\n"
        assert record.result == {
            "pipeline_id": "agent",
            "nodes": {
                "worker": {
                    "worker_return": dict(_WORKER_RETURN, files_changed=["out/report.md"], artifacts=["out/report.md"]),
                    "worktree": str(kept[0]),
                    "worktree_kept": True,
                }
            },
        }

    async def test_writes_outside_the_whitelist_fail_permission_denied(self, tmp_path: Path) -> None:
        script = _worker_script(writes={"evil.txt": "outside\n"}, final=_final_line())
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("outside")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "PermissionDenied"
        assert "outside the authorized whitelist" in message
        assert "evil.txt" in message
        assert len(kept) == 1
        assert (kept[0] / "evil.txt").is_file()  # the evidence stays for diagnosis

    async def test_claimed_files_changed_absent_fail_validation_error(self, tmp_path: Path) -> None:
        script = _worker_script(final=_final_line(files_changed=["out/missing.md"]))
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("claimed-missing")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ValidationError"
        assert "absent from the worktree" in message
        assert "out/missing.md" in message
        assert len(kept) == 1

    async def test_artifact_outside_the_whitelist_fails_permission_denied(self, tmp_path: Path) -> None:
        script = _worker_script(
            writes={"out/report.md": "report\n"},
            final=_final_line(files_changed=["out/report.md"], artifacts=["secret/report.md"]),
        )
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("artifact-outside")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "PermissionDenied"
        assert "artifact outside the authorized whitelist" in message
        assert len(kept) == 1

    async def test_artifact_not_on_disk_fails_validation_error(self, tmp_path: Path) -> None:
        script = _worker_script(
            writes={"out/report.md": "report\n"},
            final=_final_line(files_changed=["out/report.md"], artifacts=["out/absent.md"]),
        )
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("artifact-missing")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ValidationError"
        assert "artifact that does not exist" in message
        assert len(kept) == 1

    async def test_stdout_without_a_final_marker_fails_tool_failed(self, tmp_path: Path) -> None:
        """A worker that exits 0 but never answers the structured return fails closed."""
        script = _worker_script()
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("no-final")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ToolFailed"
        assert "no [final] marker" in message
        assert len(kept) == 1


class TestBudgetLines:
    """Judge 3: the three budget lines -- timeout / max_tokens / max_tool_calls."""

    async def test_wall_clock_timeout_kills_the_worker_and_fails_tool_timeout(self, tmp_path: Path) -> None:
        """The job's submitted ``timeout_s`` line wins the merge; run_command's
        strong deadline kills the tree and the node fails ``ToolTimeout``."""
        script = _worker_script(sleep_s=6.0)
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("clock", budget=JobBudget(timeout_s=1.0))
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ToolTimeout"
        assert "wall-clock timeout budget (1s)" in message
        assert len(kept) == 1  # killed runs keep the worktree for diagnosis

    async def test_max_tokens_overrun_crossed_before_exit_fails_post_hoc(self, tmp_path: Path) -> None:
        """A worker that crosses the line and exits cleanly still fails: the
        post-hoc overrun check runs after process exit, before stdout parsing."""
        usage = json.dumps({"message": {"usage": {"input": 4000, "output": 1000}}})
        script = _worker_script(rows=[(PY_STEP_FINISHED, usage)], final=_final_line())
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("token-budget", budget=JobBudget(max_tokens=1000))
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ToolFailed"
        assert "max_tokens exceeded: consumed 5000 > budget 1000" in message
        assert record.result is None  # the parse never ran
        assert len(kept) == 1

    async def test_max_tool_calls_overrun_kills_the_process_tree(self, tmp_path: Path) -> None:
        """The watchdog latches the overrun on the cancel tick; the runner kills
        the tree -- the scripted grandchild dies with it (JobObject membership)."""
        rows = [(PY_TOOL_REQUESTED, "{}") for _ in range(3)]
        script = _worker_script(rows=rows, spawn_child=True, sleep_s=30.0)
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("tool-budget", budget=JobBudget(max_tool_calls=2))
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            pid_files = sorted(stack.runs_root.glob("runs/*/child.pid"))
            assert len(pid_files) == 1
            child_pid = int(pid_files[0].read_text(encoding="utf-8").strip())
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ToolFailed"
        assert "max_tool_calls exceeded: made 3 > budget 2" in message
        await _wait_pids_gone([child_pid])  # no grandchild leak: the tree kill reached it
        assert len(kept) == 1


class TestCancelTreeKill:
    """Judge 6: cancellation through JobService kills the live worker tree."""

    async def test_cancel_kills_the_worker_tree_and_settles_cancelled(self, tmp_path: Path) -> None:
        """``cancel`` fires the canceller before the terminal emit; the drive's
        WorkerSuspended path returns without touching the folded terminal."""
        script = _worker_script(spawn_child=True, sleep_s=30.0)
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", script]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("cancel")
            task = stack.service.spawn(job_id)
            child_pid = await _await_child_pid(stack.runs_root)
            assert _pid_alive(child_pid)  # the tree is live when the cancel lands
            await stack.service.cancel(job_id)
            await asyncio.wait_for(task, timeout=30.0)
            record = await stack.service.get_job(job_id)
            with sqlite3.connect(stack.db_path) as connection:
                types_seen = [
                    row[0]
                    for row in connection.execute(
                        "SELECT type FROM events WHERE session_id = ? ORDER BY seq", (job_stream_key(job_id),)
                    )
                ]
            kept = sorted(stack.runs_root.glob("worktrees/*"))
        assert record.status == "cancelled"
        assert record.error is None
        assert record.result is None
        assert types_seen == ["py.job.created", "py.job.spec_bound", "py.job.started", "py.job.finished"]
        await _wait_pids_gone([child_pid])
        assert len(kept) == 1  # cancelled runs keep the worktree for diagnosis


class TestResumeFailClosed:
    """The S3 resume defenses (T-B-092 ①/②): real side effects make re-runs unprovable."""

    async def test_a_resume_drive_with_no_cursor_fails_closed(self, tmp_path: Path) -> None:
        """Defense ①: a running job with no advisory cursor is never re-run."""
        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", _worker_script()]) as stack:
            await stack.register_agent()
            job_id = await stack.submit("resume-empty")
            await JobHandle(stack.service, job_id).started()
            await stack.service.run(job_id, resume=True)
            record = await stack.service.get_job(job_id)
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ExecutionUnknown"
        assert "no advisory cursor" in message
        assert not (stack.runs_root / "worktrees").exists()  # the worker body never ran

    async def test_a_resume_drive_with_an_unhashed_cursor_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense ②: a cursor without a pipeline spec hash cannot prove its binding."""

        async def fake_aget_tuple(_config: Any) -> Any:
            return SimpleNamespace(checkpoint={"channel_values": {}})

        async with _WorkerStack(tmp_path, worker_command=[sys.executable, "-c", _worker_script()]) as stack:
            saver = await stack.checkpointer.saver()
            monkeypatch.setattr(saver, "aget_tuple", fake_aget_tuple)
            await stack.register_agent()
            job_id = await stack.submit("resume-unhashed")
            await JobHandle(stack.service, job_id).started()
            await stack.service.run(job_id, resume=True)
            record = await stack.service.get_job(job_id)
        code, message = _error_of(record)
        assert record.status == "failed"
        assert code == "ExecutionUnknown"
        assert "no pipeline spec hash" in message
        assert not (stack.runs_root / "worktrees").exists()
