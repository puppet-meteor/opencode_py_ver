"""Integration tests for the S5a subworkflow node (T-C1-080): nested jobs as independent records.

Honest-label note: the injected ``llm_call``/``tool`` bodies below are fakes
(the S1 fake-executor discipline) -- plain recording bodies, a failing one
for the failure-propagation judge. Everything around them is real: a real
migrated main jobs database, the real ``JobService`` commit spine and fold,
the real compiled LangGraph state machine under the official sqlite
checkpointer, the real ``subworkflow`` node body (the nested submit with
lineage/depth, the in-process engine recursion, the terminal poll), and the
real ``human_gate`` body for the nested-gate composition.

Coverage (the S5a acceptance judges):

* the nested job is an independent job record: lineage parent = the parent
  job's id, depth = parent + 1, the session anchor inherited, and the
  parent's node result carries the nested job's identity, terminal status
  and result so downstream nodes (and tests) can walk the chain;
* a nested failure propagates its §8 code up to the parent (the parent
  fails naming the nested job's error); an unregistered ``workflow_ref``
  fails both jobs ``NotFound``;
* the self-referencing pipeline stops at the M-9 depth cap: exactly four
  jobs exist (depths 0..3), the depth-4 submission is rejected before any
  event is drafted, and the cap violation surfaces as the root job's
  failure message;
* a nested job parked on its own human gate parks the parent's node body
  too (the poll loop); resolving the nested gate resumes the nested job
  and then the parent settles ``done``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencode.contracts.messages import JsonObject
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.gates import GateBroker
from opencode.orchestrator.jobs import JobService, JobsProjector
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobsReadRepository, JobsStore

REPO_ROOT = Path(__file__).resolve().parents[2]
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
# The honest fakes (recording, deterministic; one deliberately failing).
# ---------------------------------------------------------------------------


class _FakeLlm:
    """Recording fake (honestly labeled); these specs never reach it."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"summary": "ok", "model": node.model}


class _FakeTool:
    """Recording fake (honestly labeled): returns a fixed JSON value."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"collected": True}


class _FailingTool:
    """Injected failing fake (honestly labeled): the nested-failure judge."""

    async def __call__(self, node: Any, state: Any) -> Any:
        raise RuntimeError("child tool exploded")


def _tool_spec(pipeline_id: str = "child") -> JsonObject:
    """One tool node (entry and terminal)."""
    return {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": "work",
        "nodes": [{"node_id": "work", "node_type": "tool", "tool_ref": "fs.read_text"}],
        "edges": [],
    }


def _sub_spec(pipeline_id: str = "parent", ref: str = "child") -> JsonObject:
    """One subworkflow node (entry and terminal) driving the nested pipeline."""
    return {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": "nested",
        "nodes": [{"node_id": "nested", "node_type": "subworkflow", "workflow_ref": ref}],
        "edges": [],
    }


def _gate_child_spec() -> JsonObject:
    """The nested pipeline with a human gate: tool -> human_gate (terminal)."""
    return {
        "schema_version": 1,
        "pipeline_id": "gate_child",
        "entry": "collect",
        "nodes": [
            {"node_id": "collect", "node_type": "tool", "tool_ref": "fs.read_text"},
            {"node_id": "approval", "node_type": "human_gate"},
        ],
        "edges": [{"from_node": "collect", "to_node": "approval"}],
    }


class _Stack:
    """One migrated main DB + one subworkflow-capable engine over its own cursor file."""

    def __init__(self, tmp_path: Path, *, tool: Any = None, gate: bool = False) -> None:
        self.db_path = tmp_path / "main.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.tool = tool if tool is not None else _FakeTool()
        self.broker = GateBroker() if gate else None
        self.engine = PipelineEngine(
            checkpointer=EngineCheckpointer(tmp_path / "cursor.sqlite3"),
            llm_call=_FakeLlm(),
            tool=self.tool,
            gate_broker=self.broker,
            subworkflow=True,
        )
        self.database = open_database(self.db_path, mode="create")
        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=self.engine,
            ids=AscendingIds(lambda: _T0),
            clock=lambda: _T0,
            gate_broker=self.broker,
            selfcheck_delay_s=-1.0,
        )

    async def register(self, raw: JsonObject) -> None:
        await self.engine.register(raw)

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


def _other_job_ids(db_path: Path, job_id: str) -> list[str]:
    return [str(row[0]) for row in _fetch_all(db_path, "SELECT job_id FROM jobs WHERE job_id != ?", (job_id,))]


class TestNestedExecution:
    async def test_nested_job_is_an_independent_record_with_lineage_and_result_chain(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_tool_spec())
            await stack.register(_sub_spec())
            parent = await stack.service.submit("parent", {"run": "chain"}, "ses_0007")
            await stack.service.run(parent.job_id)  # the in-process engine recursion
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "done"
            result = record.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            nested = nodes["nested"]
            assert isinstance(nested, dict)
            assert nested["status"] == "done"
            child_id = nested["job_id"]
            assert isinstance(child_id, str)
            assert child_id.startswith("job_")
            assert nested["result"] == {"pipeline_id": "child", "nodes": {"work": {"collected": True}}}

            child = await stack.service.get_job(child_id)
            assert child.status == "done"
            assert child.pipeline_id == "child"
            assert child.depth == 1  # the parent's depth + 1
            assert child.params == {}  # the engine submits the nested job with empty params
            assert child.session_anchor == "ses_0007"  # the anchor is inherited
            assert child.lineage is not None
            assert child.lineage["parent_job_id"] == parent.job_id
            assert child.lineage["origin"] == "session"
            assert child.lineage["session_id"] == "ses_0007"
        finally:
            await stack.close()


class TestNestedFailure:
    async def test_nested_failure_propagates_its_code_to_the_parent(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, tool=_FailingTool())
        try:
            await stack.register(_tool_spec())
            await stack.register(_sub_spec())
            parent = await stack.service.submit("parent", {}, "api")
            await stack.service.run(parent.job_id)
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ToolFailed"  # the nested §8 code, propagated
            assert "child tool exploded" in str(record.error["message"])
            # The nested job is an independent failed record of its own.
            child_ids = _other_job_ids(stack.db_path, parent.job_id)
            assert len(child_ids) == 1
            child = await stack.service.get_job(child_ids[0])
            assert child.status == "failed"
            assert child.error is not None
            assert child.error["code"] == "ToolFailed"
        finally:
            await stack.close()

    async def test_unregistered_workflow_ref_fails_both_jobs_not_found(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_sub_spec(ref="ghost"))  # the nested pipeline is never registered
            parent = await stack.service.submit("parent", {}, "api")
            await stack.service.run(parent.job_id)
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "NotFound"
            assert "ghost" in str(record.error["message"])
            child_ids = _other_job_ids(stack.db_path, parent.job_id)
            assert len(child_ids) == 1  # the nested job was still submitted (an independent record)
            child = await stack.service.get_job(child_ids[0])
            assert child.status == "failed"
            assert child.error is not None
            assert child.error["code"] == "NotFound"
        finally:
            await stack.close()


class TestRecursionCap:
    async def test_self_referencing_pipeline_stops_at_the_depth_cap(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_sub_spec(pipeline_id="rec", ref="rec"))  # the pipeline nests itself
            parent = await stack.service.submit("rec", {}, "api")
            await stack.service.run(parent.job_id)
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "failed"
            assert record.error is not None
            # The M-9 second line's rejection surfaces here as the root job's failure.
            assert "exceeds the hard cap" in str(record.error["message"])

            # Exactly four jobs exist (depths 0..3): the depth-4 submission was
            # rejected by the lineage guard before any event was drafted.
            rows = _fetch_all(stack.db_path, "SELECT job_id, depth, status FROM jobs", ())
            assert len(rows) == 4
            by_depth = {int(depth): str(status) for _, depth, status in rows}
            assert by_depth == {0: "failed", 1: "failed", 2: "failed", 3: "failed"}
            # The ancestor chain: every nested job's lineage parent is the previous depth's job.
            ids_by_depth = {int(depth): str(job_id) for job_id, depth, _ in rows}
            for _job_id, depth, raw_lineage in _fetch_all(stack.db_path, "SELECT job_id, depth, lineage FROM jobs", ()):
                lineage = json.loads(raw_lineage) if raw_lineage else {}
                if int(depth) == 0:
                    assert lineage.get("parent_job_id") is None
                else:
                    assert lineage.get("parent_job_id") == ids_by_depth[int(depth) - 1]
        finally:
            await stack.close()


class TestNestedHumanGate:
    async def test_a_nested_gate_parks_the_parent_until_the_reply_resolves(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, gate=True)
        try:
            await stack.register(_gate_child_spec())
            await stack.register(_sub_spec(ref="gate_child"))
            parent = await stack.service.submit("parent", {}, "ses_0009")
            stack.service.spawn(parent.job_id)
            # The nested job parks on its own human gate; the parent's node body
            # legitimately waits (the poll loop) without being gated itself.
            child_id = await _await_child(stack.db_path, parent.job_id, {"gated"})
            parent_mid = await stack.service.get_job(parent.job_id)
            assert parent_mid.status == "running"  # the parent is parked, not gated
            gate_id = stack.service.active_gate(child_id)
            assert gate_id is not None

            record = await stack.service.reply_gate(child_id, gate_id, "approve")
            assert record.status in {"running", "done"}
            parent_final = await _await_status(stack, parent.job_id, {"done"})
            result = parent_final.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            nested = nodes["nested"]
            assert isinstance(nested, dict)
            assert nested["job_id"] == child_id
            assert nested["status"] == "done"
            child = await stack.service.get_job(child_id)
            assert child.status == "done"
            assert child.depth == 1
        finally:
            await stack.close()
