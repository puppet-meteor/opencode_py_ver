"""Integration tests for the P3-S2 engine face (T-C1-077): real LangGraph execution.

Honest-label note: the injected ``llm_call``/``tool`` bodies below are fakes
(the S1 fake-executor discipline) -- they record calls and return fixed JSON
values; the real LLM-tier wiring lands with S4 and the worker face with S3.
Everything around them is real: a real migrated main jobs database, the real
``JobService``/``JobsStore`` commit spine, the real compiled LangGraph state
machine, and langgraph-checkpoint-sqlite's official ``AsyncSqliteSaver`` over
its own sqlite file (the sync ``SqliteSaver`` cannot serve an async graph --
its async face raises ``NotImplementedError``; disclosed in the progress file).

Coverage (the S2 acceptance judges):

* sequential execution over a fixed spec: the graph runs the chain, the job
  settles ``done`` with the structured node-result payload;
* the three-layer gate at registration (structure -> semantic -> rehearsal):
  a spec that parses but violates semantics is rejected with the structured
  error; a job naming an unregistered pipeline fails ``NotFound``;
* node failures fail the job with the §8 error face (``ToolFailed`` for tool
  nodes, ``ProviderUnavailable`` for llm nodes);
* interrupt/resume: an ``EnginePause`` mid-graph leaves the job ``running``
  with the advisory cursor holding the position; the §8 recovery scan (the
  engine as its checkpoint reader) resumes and the result is exactly the
  one-shot equivalent, with the completed node never re-executed; a plain
  re-drive (no ``resume`` flag) converges the same way (idempotent re-entry);
* the recovery matrix on the engine's reader: branch-2 resume, branch-5
  idempotent write-back with the stable derived event id, branch-4 fail-closed
  when no checkpoint exists; a re-registered spec never rebinds a running
  graph (hash mismatch -> ``ExecutionUnknown`` fail-closed);
* the dual-database boundary (§14/M-7): the checkpointer file holds only the
  saver's own tables and only graph-state channels -- no job-status claims;
  job status lives in the main DB, written only through ``py.job.*`` events;
* the App wiring: ``pipeline_engine=`` routes the jobs service onto the engine
  and the close order releases the cursor store after the jobs tasks.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.app import App
from opencode.contracts.messages import JsonObject
from opencode.orchestrator.engine import (
    EngineCheckpointer,
    EnginePause,
    PipelineEngine,
    PipelineSpecRejected,
)
from opencode.orchestrator.jobs import JobService, JobsProjector, job_stream_key
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobsReadRepository, JobsStore
from tests.contract.test_pipelines import _spec_raw

REPO_ROOT = Path(__file__).resolve().parents[2]

_T0 = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
_DIGEST_SPEC: JsonObject = _spec_raw(pipeline_id="digest")


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


# ---------------------------------------------------------------------------
# The honest fake bodies (recording, deterministic; the injection face is the
# single tested seam -- real tiers land with S3/S4).
# ---------------------------------------------------------------------------


class _FakeLlm:
    """Records per-run calls; optionally pauses once / fails on marked runs."""

    def __init__(self, *, pause_runs: frozenset[str] = frozenset(), fail_runs: frozenset[str] = frozenset()) -> None:
        self.calls: dict[str, list[str]] = {}
        self._pause_runs = set(pause_runs)
        self._fail_runs = set(fail_runs)

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("run", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        if marker in self._fail_runs:
            raise RuntimeError("fake llm provider exploded")
        if marker in self._pause_runs and len(self.calls[marker]) == 1:
            raise EnginePause("simulated gateway restart mid-graph")
        return {"summary": "ok", "model": node.model}


class _FakeTool:
    """Records per-run calls; optionally fails on marked runs."""

    def __init__(self, *, fail_runs: frozenset[str] = frozenset()) -> None:
        self.calls: dict[str, list[str]] = {}
        self._fail_runs = set(fail_runs)

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("run", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        if marker in self._fail_runs:
            raise RuntimeError("fake tool exploded")
        return {"collected": True}


class _Stack:
    """One migrated main DB + one engine over its own checkpointer file."""

    def __init__(self, tmp_path: Path, llm: _FakeLlm, tool: _FakeTool) -> None:
        self.db_path = tmp_path / "main.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.checkpoint_path = tmp_path / "advisory-cursor.sqlite3"
        self.llm = llm
        self.tool = tool
        self.checkpointer = EngineCheckpointer(self.checkpoint_path)
        self.engine = PipelineEngine(checkpointer=self.checkpointer, llm_call=llm, tool=tool)
        self.database = open_database(self.db_path, mode="create")
        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=self.engine,
            ids=AscendingIds(lambda: _T0),
            clock=lambda: _T0,
        )

    async def register_digest(self, raw: JsonObject | None = None) -> None:
        await self.engine.register(raw if raw is not None else _DIGEST_SPEC)

    async def submit(self, run: str, *, pipeline_id: str = "digest") -> str:
        handle = await self.service.submit(pipeline_id, {"run": run}, "api")
        return handle.job_id

    async def close(self) -> None:
        await self.service.close()
        await self.database.close()
        await self.checkpointer.close()


@pytest.fixture()
async def stack(tmp_path: Path) -> AsyncIterator[_Stack]:
    """The default stack: cooperative fakes, no pauses, no failures."""
    engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
    await engine_stack.register_digest()
    try:
        yield engine_stack
    finally:
        await engine_stack.close()


async def _await_status(stack: _Stack, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> Any:
    """Poll the read model until a spawned run settles the job."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await stack.service.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


def _sqlite_tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _stream_events(db_path: Path, stream: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT id, type FROM events WHERE session_id = ? ORDER BY seq", (stream,)).fetchall()
    return [(row[0], row[1]) for row in rows]


def _error_details(error: JsonObject | None) -> JsonObject:
    """The nested ``details`` object of a job's error payload (assert-checked)."""
    assert error is not None
    details = error["details"]
    assert isinstance(details, dict)
    return details


async def _checkpoint_state(stack: _Stack, thread_id: str) -> dict[str, Any]:
    """The latest checkpoint's channel values, read through the saver's own face."""
    saver = await stack.checkpointer.saver()
    tuple_ = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert tuple_ is not None, f"no checkpoint for thread {thread_id}"
    return dict(tuple_.checkpoint["channel_values"])


class TestSequentialExecution:
    async def test_fixed_spec_runs_end_to_end_and_settles_done(self, stack: _Stack) -> None:
        job_id = await stack.submit("one-shot")
        await stack.service.run(job_id)
        record = await stack.service.get_job(job_id)
        assert record.status == "done"
        assert record.error is None
        assert record.result == {
            "pipeline_id": "digest",
            "nodes": {"collect": {"collected": True}, "summarize": {"summary": "ok", "model": "small"}},
        }
        assert stack.tool.calls["one-shot"] == ["collect"]
        assert stack.llm.calls["one-shot"] == ["summarize"]
        events = _stream_events(stack.db_path, job_stream_key(job_id))
        assert [event_type for _, event_type in events] == [
            "py.job.created",
            "py.job.spec_bound",
            "py.job.started",
            "py.job.finished",
        ]

    async def test_spawned_run_settles_through_the_same_spine(self, stack: _Stack) -> None:
        job_id = await stack.submit("spawned")
        stack.service.spawn(job_id)
        record = await _await_status(stack, job_id, {"done"})
        assert record.result is not None

    async def test_params_travel_into_the_node_bodies(self, stack: _Stack) -> None:
        job_id = await stack.submit("params-run")
        await stack.service.run(job_id)
        # The fake bodies keyed their call recording off params["run"]:
        assert "params-run" in stack.tool.calls
        assert "params-run" in stack.llm.calls


class TestRegistrationGate:
    async def test_semantic_rejection_at_registration_raises_structured(self, tmp_path: Path) -> None:
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            bad = _spec_raw(entry="ghost")
            with pytest.raises(PipelineSpecRejected, match="entry node 'ghost' is not declared"):
                await engine_stack.register_digest(bad)
            assert engine_stack.engine.bound("digest") is None
        finally:
            await engine_stack.close()

    async def test_structural_rejection_at_registration(self, tmp_path: Path) -> None:
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            with pytest.raises(ValidationError):
                await engine_stack.register_digest(_spec_raw(nodes=[]))
        finally:
            await engine_stack.close()

    async def test_a_dialect_shaped_wire_object_is_the_structural_rejection(self, tmp_path: Path) -> None:
        """T-C1-087 (ruling-18): the loading router is retired -- the structural
        layer is the native single path. A dialect ``pydag-editable-graph-v1``
        body reaching the engine is the plain pydantic rejection (no sniffing,
        no conversion), exactly the registration face's ValueError-family row.
        """
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            dialect: dict[str, object] = {
                "schema_version": "pydag-editable-graph-v1",
                "skill_name": "demo",
                "nodes": [],
                "edges": [],
            }
            with pytest.raises(ValidationError, match="schema_version"):
                await engine_stack.register_digest(dialect)  # type: ignore[arg-type]
        finally:
            await engine_stack.close()

    async def test_unregistered_pipeline_fails_the_job_not_found(self, stack: _Stack) -> None:
        job_id = await stack.submit("ghost-run", pipeline_id="ghost")
        await stack.service.run(job_id)
        record = await stack.service.get_job(job_id)
        assert record.status == "failed"
        assert record.error is not None
        assert record.error["code"] == "NotFound"
        assert _error_details(record.error)["pipeline_id"] == "ghost"


class TestNodeFailures:
    async def test_llm_node_failure_fails_the_job_provider_unavailable(self, tmp_path: Path) -> None:
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(fail_runs=frozenset({"boom"})), tool=_FakeTool())
        try:
            await engine_stack.register_digest()
            job_id = await engine_stack.submit("boom")
            await engine_stack.service.run(job_id)
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ProviderUnavailable"
            assert _error_details(record.error)["node_id"] == "summarize"
        finally:
            await engine_stack.close()

    async def test_tool_node_failure_fails_the_job_tool_failed(self, tmp_path: Path) -> None:
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool(fail_runs=frozenset({"boom"})))
        try:
            await engine_stack.register_digest()
            job_id = await engine_stack.submit("boom")
            await engine_stack.service.run(job_id)
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ToolFailed"
            assert _error_details(record.error)["node_id"] == "collect"
        finally:
            await engine_stack.close()


class TestInterruptResume:
    async def test_interrupt_then_recovery_resume_matches_one_shot(self, tmp_path: Path) -> None:
        engine_stack = _Stack(
            tmp_path,
            llm=_FakeLlm(pause_runs=frozenset({"paused"})),
            tool=_FakeTool(),
        )
        try:
            await engine_stack.register_digest()
            # The one-shot reference.
            one_shot_id = await engine_stack.submit("one-shot")
            await engine_stack.service.run(one_shot_id)
            one_shot = await engine_stack.service.get_job(one_shot_id)

            # The interrupted run: the llm node pauses once mid-graph.
            paused_id = await engine_stack.submit("paused")
            await engine_stack.service.run(paused_id)
            running = await engine_stack.service.get_job(paused_id)
            assert running.status == "running"  # EnginePause leaves the job non-terminal
            assert engine_stack.tool.calls["paused"] == ["collect"]
            assert engine_stack.llm.calls["paused"] == ["summarize"]  # the paused attempt

            # The §8 recovery scan over the engine's own reader: branch 2.
            actions = await engine_stack.service.recover(engine_stack.engine)
            assert [action.action for action in actions] == ["resumed"]
            record = await _await_status(engine_stack, paused_id, {"done", "failed"})
            assert record.status == "done"
            assert record.result == one_shot.result  # equivalent to the one-shot run
            assert engine_stack.tool.calls["paused"] == ["collect"]  # completed node never re-ran
            assert engine_stack.llm.calls["paused"] == ["summarize", "summarize"]  # only the failed node re-ran
        finally:
            await engine_stack.close()

    async def test_plain_re_drive_is_idempotent_re_entry(self, tmp_path: Path) -> None:
        engine_stack = _Stack(
            tmp_path,
            llm=_FakeLlm(pause_runs=frozenset({"reentry"})),
            tool=_FakeTool(),
        )
        try:
            await engine_stack.register_digest()
            job_id = await engine_stack.submit("reentry")
            await engine_stack.service.run(job_id)
            assert (await engine_stack.service.get_job(job_id)).status == "running"
            # A plain second drive (no resume flag): the start emit rejects over
            # running, so the engine continues from the advisory cursor.
            await engine_stack.service.run(job_id)
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "done"
            assert engine_stack.tool.calls["reentry"] == ["collect"]
            assert engine_stack.llm.calls["reentry"] == ["summarize", "summarize"]
        finally:
            await engine_stack.close()

    async def test_terminal_job_re_drive_is_a_silent_no_op(self, stack: _Stack) -> None:
        job_id = await stack.submit("settled")
        await stack.service.run(job_id)
        await stack.service.run(job_id)  # a cancel-race shape: already done
        events = _stream_events(stack.db_path, job_stream_key(job_id))
        assert [event_type for _, event_type in events].count("py.job.finished") == 1

    async def test_reregistered_spec_never_rebinds_a_running_graph(self, tmp_path: Path) -> None:
        engine_stack = _Stack(
            tmp_path,
            llm=_FakeLlm(pause_runs=frozenset({"rebound"})),
            tool=_FakeTool(),
        )
        try:
            await engine_stack.register_digest()
            job_id = await engine_stack.submit("rebound")
            await engine_stack.service.run(job_id)
            assert (await engine_stack.service.get_job(job_id)).status == "running"
            # The spec is revised and re-registered under the same id (new hash).
            revised = _spec_raw(pipeline_id="digest")
            revised["nodes"][1]["prompt_template"] = "Summarize differently."
            await engine_stack.register_digest(revised)
            await engine_stack.service.run(job_id, resume=True)
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ExecutionUnknown"
            message = record.error["message"]
            assert isinstance(message, str)
            assert "never rebound" in message
        finally:
            await engine_stack.close()


class TestRecoveryMatrixReader:
    async def test_started_without_a_checkpoint_fails_closed(self, tmp_path: Path) -> None:
        engine_stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            await engine_stack.register_digest()
            job_id = await engine_stack.submit("cold")
            await engine_stack.service.emit_started(job_id)  # running, graph never ran
            actions = await engine_stack.service.recover(engine_stack.engine)
            assert [action.action for action in actions] == ["failed_unknown"]
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ExecutionUnknown"
        finally:
            await engine_stack.close()

    async def test_graph_done_without_the_finish_commit_writes_back(self, tmp_path: Path) -> None:
        engine_stack = _Stack(
            tmp_path,
            llm=_FakeLlm(pause_runs=frozenset({"window"})),
            tool=_FakeTool(),
        )
        try:
            await engine_stack.register_digest()
            one_shot_id = await engine_stack.submit("reference")
            await engine_stack.service.run(one_shot_id)
            one_shot = await engine_stack.service.get_job(one_shot_id)

            job_id = await engine_stack.submit("window")
            await engine_stack.service.run(job_id)  # pauses at the terminal node
            assert (await engine_stack.service.get_job(job_id)).status == "running"
            # The crash window: the graph completes (terminal node returns) but
            # the py.job.finished commit never happens -- simulated by resuming
            # the graph directly, without the engine's drive.
            bound = engine_stack.engine.bound("digest")
            assert bound is not None
            await bound.graph.ainvoke(None, {"configurable": {"thread_id": job_id}})
            assert (await engine_stack.service.get_job(job_id)).status == "running"  # still no commit

            # Branch 5: the reader reports the completion, the writer replays.
            actions = await engine_stack.service.recover(engine_stack.engine)
            assert [action.action for action in actions] == ["written_back"]
            record = await engine_stack.service.get_job(job_id)
            assert record.status == "done"
            assert record.result == one_shot.result
            events = _stream_events(engine_stack.db_path, job_stream_key(job_id))
            finished = [(event_id, event_type) for event_id, event_type in events if event_type == "py.job.finished"]
            assert len(finished) == 1
            assert finished[0][0] == f"evt_wb{job_id[4:]}"  # the stable derived write-back id
        finally:
            await engine_stack.close()


class TestDualDatabaseBoundary:
    async def test_the_checkpointer_file_holds_only_the_saver_tables(self, stack: _Stack) -> None:
        job_id = await stack.submit("boundary")
        await stack.service.run(job_id)
        assert _sqlite_tables(stack.checkpoint_path) == {"checkpoints", "writes"}
        main_tables = _sqlite_tables(stack.db_path)
        assert "jobs" in main_tables and "events" in main_tables
        assert not {"checkpoints", "writes"} & main_tables

    async def test_the_graph_state_carries_no_job_status_channel(self, stack: _Stack) -> None:
        job_id = await stack.submit("channels")
        await stack.service.run(job_id)
        values = await _checkpoint_state(stack, job_id)
        assert set(values) <= {
            "params",
            "params_hash",
            "pipeline_id",
            "pipeline_spec_hash",
            "attempt_id",
            "node_results",
            "outcome",
            "result",
        }
        assert "status" not in values  # the cursor makes no job-status claim
        assert values["outcome"] == "done"  # the graph-completion fact only

    async def test_job_status_lives_only_in_the_main_db_fold(self, stack: _Stack) -> None:
        job_id = await stack.submit("authority")
        await stack.service.run(job_id)
        record = await stack.service.get_job(job_id)
        assert record.status == "done"
        checkpoint = await stack.engine.read(job_id)
        assert checkpoint is not None
        assert checkpoint.outcome == "done"
        assert checkpoint.spec_hash == record.params_hash  # the binding hash, not a status
        assert not hasattr(checkpoint, "status")
        events = _stream_events(stack.db_path, job_stream_key(job_id))
        assert [event_type for _, event_type in events][-1] == "py.job.finished"


class TestAppWiring:
    async def test_app_routes_the_jobs_service_onto_the_engine(self, tmp_path: Path) -> None:
        db_path = tmp_path / "app.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        checkpointer = EngineCheckpointer(tmp_path / "app-advisory.sqlite3")
        engine = PipelineEngine(checkpointer=checkpointer, llm_call=_FakeLlm(), tool=_FakeTool())
        await engine.register(_DIGEST_SPEC)
        app = App(db_path, working_dir="C:/work", pipeline_engine=engine)
        try:
            assert app.pipeline_engine is engine
            handle = await app.jobs.submit("digest", {"run": "app"}, "api")
            await app.jobs.run(handle.job_id)
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "done"
            assert record.result is not None
        finally:
            await app.close()

    async def test_app_default_keeps_the_s1_fake_executor(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fake.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        app = App(db_path, working_dir="C:/work")
        try:
            assert app.pipeline_engine is None
            handle = await app.jobs.submit("research", {"query": "hi"}, "api")
            await app.jobs.run(handle.job_id)
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "done"
            assert record.result == {"executor": "fake"}
        finally:
            await app.close()
