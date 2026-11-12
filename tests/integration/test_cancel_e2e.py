"""Integration tests for the S5a cancel chain (T-C1-080): interrupt, terminal fold, descendants.

Honest-label note: the injected ``llm_call``/``tool`` bodies below are fakes
(the S1 fake-executor discipline) -- the tool body sleeps like a real long
tool and records whether it saw its ``CancelledError``. Everything around
them is real: a real migrated main jobs database, the real ``JobService``
commit spine and fold, the real compiled LangGraph state machine under the
official sqlite checkpointer, the real subworkflow engine recursion, and
the real HTTP cancel route.

Coverage (the S5a acceptance judges):

* cancelling a running job interrupts its drive task (the node body's side
  effects bounded by the checkpointer's last committed node boundary),
  commits ``py.job.finished(cancelled)`` through the fold, and is
  idempotent -- the second cancel is the no-op over the closed terminal
  set (no duplicate terminal event);
* cancelling a job with a live subworkflow descendant resolves the
  descendant first (leaves before the parent, each an independent job
  record whose lineage parent is the cancelled job) and then the job
  itself -- the descendant's terminal event precedes the parent's;
* the route face: ``POST /jobs/{job_id}/cancel`` answers the job wire and
  stays idempotent over HTTP.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencode.app import App
from opencode.contracts.messages import JsonObject
from opencode.httpapi import create_app
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.jobs import JobService, JobsProjector, job_stream_key
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobsReadRepository, JobsStore

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "test-token-1234"
_T0 = datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)


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


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


# ---------------------------------------------------------------------------
# The honest fakes: a sleeping tool body (records its interruption), a plain
# recording llm body the engine constructor requires but the specs never reach.
# ---------------------------------------------------------------------------


class _SlowTool:
    """Injected fake body (honestly labeled): sleeps like a real long tool.

    A cancellation surfacing here as ``CancelledError`` is exactly the drive
    interrupt the cancel face promises -- the body records it and re-raises.
    """

    def __init__(self, seconds: float = 5.0) -> None:
        self.seconds = seconds
        self.interrupted = False
        self.started = 0

    async def __call__(self, node: Any, state: Any) -> Any:
        self.started += 1
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.interrupted = True
            raise
        return {"collected": True}


class _FakeLlm:
    """Recording fake (honestly labeled); these specs never reach it."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"summary": "ok", "model": node.model}


def _slow_spec(pipeline_id: str = "slow") -> JsonObject:
    """One sleeping tool node (entry and terminal)."""
    return {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": "work",
        "nodes": [{"node_id": "work", "node_type": "tool", "tool_ref": "fs.read_text"}],
        "edges": [],
    }


def _parent_spec(pipeline_id: str = "parent", ref: str = "slow") -> JsonObject:
    """One subworkflow node (entry and terminal) driving the nested pipeline."""
    return {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": "nested",
        "nodes": [{"node_id": "nested", "node_type": "subworkflow", "workflow_ref": ref}],
        "edges": [],
    }


class _Stack:
    """One migrated main DB + one subworkflow-capable engine over its own cursor file."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = tmp_path / "main.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.tool = _SlowTool()
        self.engine = PipelineEngine(
            checkpointer=EngineCheckpointer(tmp_path / "cursor.sqlite3"),
            llm_call=_FakeLlm(),
            tool=self.tool,
            subworkflow=True,
        )
        self.database = open_database(self.db_path, mode="create")
        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=self.engine,
            ids=AscendingIds(lambda: _T0),
            clock=lambda: _T0,
            selfcheck_delay_s=-1.0,
        )

    async def close(self) -> None:
        await self.service.close()
        await self.database.close()
        await self.engine.close()


async def _await_status(stack: _Stack, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> Any:
    """Poll the read model until a spawned run settles the job."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await stack.service.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


async def _await_flag(flag: Callable[[], bool], message: str, *, timeout_s: float = 5.0) -> None:
    """Poll a plain boolean probe (an interrupted fake body) until it turns true."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not flag():
        assert asyncio.get_running_loop().time() < deadline, message
        await asyncio.sleep(0.01)


def _event_count(db_path: Path, stream: str) -> int:
    return int(_fetch_all(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (stream,))[0][0])


def _finished_rowid(db_path: Path, job_id: str) -> int:
    """The insertion rowid of the job's terminal event (streams are independent, the log is one table)."""
    rows = _fetch_all(
        db_path,
        "SELECT rowid FROM events WHERE session_id = ? AND type = 'py.job.finished'",
        (job_stream_key(job_id),),
    )
    assert rows, f"no py.job.finished event for {job_id}"
    return int(rows[0][0])


async def _await_child(db_path: Path, parent_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> str:
    """Poll the jobs read model until a non-parent job reaches one of the statuses."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = _fetch_all(db_path, "SELECT job_id, status FROM jobs WHERE job_id != ?", (parent_id,))
        for job_id, status in rows:
            if str(status) in statuses:
                return str(job_id)
        assert asyncio.get_running_loop().time() < deadline, f"no nested child ever reached {statuses}: {rows}"
        await asyncio.sleep(0.01)


async def _asgi_request(
    api: Any,
    method: str,
    path: str,
    *,
    token: str | None = _TOKEN,
    body: bytes | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_root": "",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51236),
        "server": ("127.0.0.11", 4096),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}] if body is not None else []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await api(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    status = int(start["status"])
    response_headers = {bytes(name).lower(): bytes(value) for name, value in start["headers"]}
    response_body = b"".join(
        bytes(message.get("body", b"")) for message in sent if message["type"] == "http.response.body"
    )
    return status, response_headers, response_body


class TestCancelRunningJob:
    async def test_cancel_interrupts_the_drive_commits_the_fold_and_is_idempotent(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.engine.register(_slow_spec())
            handle = await stack.service.submit("slow", {"run": "one"}, "api")
            stack.service.spawn(handle.job_id)
            running = await _await_status(stack, handle.job_id, {"running"})
            assert running.status == "running"
            await _await_flag(lambda: stack.tool.started == 1, "the sleeping node body never started")

            record = await stack.service.cancel(handle.job_id)
            assert record.status == "cancelled"
            assert record.result is None
            # The interrupted drive task saw its CancelledError (the node body's
            # frontier is the checkpointer's last committed node boundary).
            await _await_flag(lambda: stack.tool.interrupted, "the drive task was never interrupted")
            # Idempotent: the second cancel is the no-op over the terminal set.
            stream = job_stream_key(handle.job_id)
            events_before = _event_count(stack.db_path, stream)
            record = await stack.service.cancel(handle.job_id)
            assert record.status == "cancelled"
            assert _event_count(stack.db_path, stream) == events_before  # no duplicate terminal event
        finally:
            await stack.close()


class TestCancelPropagatesToDescendants:
    async def test_route_cancel_resolves_the_descendant_first_then_the_job(self, tmp_path: Path) -> None:
        db_path = tmp_path / "api.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        tool = _SlowTool()
        engine = PipelineEngine(
            checkpointer=EngineCheckpointer(tmp_path / "api-cursor.sqlite3"),
            llm_call=_FakeLlm(),
            tool=tool,
            subworkflow=True,
        )
        await engine.register(_slow_spec())
        await engine.register(_parent_spec())
        app = App(db_path, working_dir="C:/work", pipeline_engine=engine)
        api = create_app(app, token=_TOKEN)
        try:
            handle = await app.jobs.submit("parent", {"run": "tree"}, "api")
            app.jobs.spawn(handle.job_id)
            # The nested job exists and is running its sleeping tool node; the
            # parent's node body is parked polling the nested job's read model.
            child_id = await _await_child(db_path, handle.job_id, {"running"})
            parent_mid = await app.jobs.get_job(handle.job_id)
            assert parent_mid.status == "running"

            status, _headers, raw = await _asgi_request(api, "POST", f"/jobs/{handle.job_id}/cancel")
            assert status == 200
            assert json.loads(raw)["status"] == "cancelled"

            # The descendant resolved first: its terminal event precedes the parent's.
            child = await app.jobs.get_job(child_id)
            deadline = asyncio.get_running_loop().time() + 5.0
            while child.status != "cancelled":
                assert asyncio.get_running_loop().time() < deadline, f"nested job never cancelled: {child.status}"
                await asyncio.sleep(0.01)
                child = await app.jobs.get_job(child_id)
            parent = await app.jobs.get_job(handle.job_id)
            assert parent.status == "cancelled"
            assert parent.result is None
            assert _finished_rowid(db_path, child_id) < _finished_rowid(db_path, handle.job_id)
            await _await_flag(lambda: tool.interrupted, "the parent's drive task was never interrupted")
            # The lineage face: the nested job is an independent record whose
            # lineage parent is the cancelled job, one depth level down.
            assert child.depth == 1
            assert child.lineage is not None
            assert child.lineage["parent_job_id"] == handle.job_id
            # Idempotent over the route: the second cancel answers the same wire
            # without a second terminal event.
            events_before = _event_count(db_path, job_stream_key(handle.job_id))
            status, _headers, raw = await _asgi_request(api, "POST", f"/jobs/{handle.job_id}/cancel")
            assert status == 200
            assert json.loads(raw)["status"] == "cancelled"
            assert _event_count(db_path, job_stream_key(handle.job_id)) == events_before
        finally:
            await app.close()
