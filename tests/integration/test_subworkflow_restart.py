"""Integration tests for subworkflow restart idempotency (P5-S9, T-C1-093).

The v1 leftover this ticket closes: a process death while a ``subworkflow``
node was mid-flight (the nested job already submitted, the node not yet
completed) left the re-driven parent body re-submitting a duplicate nested
job. The body now carries an adoption lookahead at entry: it matches the
job's non-terminal children by lineage (parent = this job) and
``pipeline_id == workflow_ref`` -- exactly one match is adopted (re-driven
via the existing run/poll/terminal branches, never re-submitted), zero
matches is the unchanged first-entry fresh-submit face, and an ambiguous
match set (more than one, or a matched child whose depth violates the
parent+1 construction invariant) fails closed ``ExecutionUnknown``
mirroring the §8 branch-④ discipline: the node-to-child binding cannot be
proven, so only the parent fails and no matched child is touched.

Honest-label notes (mirroring tests/integration/test_subworkflow.py): the
``_Stack`` real spine is imported from that file -- a real migrated main
jobs database, the real ``JobService`` commit spine and fold, the real
compiled LangGraph state machine under the official sqlite checkpointer,
and the real ``subworkflow``/``human_gate`` node bodies -- with the
llm_call/tool seams as honest recording fakes. The death simulation uses
an UNMARKED ``task.cancel()`` (never ``service.cancel``): the drive task
ends cancelled with zero job-state touches, leaving exactly the
non-terminal in-flight state a process death leaves behind (the jobs.run
quiet-exit contract names only the marked interrupt as the absorbed one).
The depth test's wrong-depth child row is a fabricated invariant
violation (impossible through the public submit's lineage guard, which
enforces depth = parent + 1), labeled in situ -- the defense exists for
exactly such an unattributable row.

Coverage (the T-C1-093 judges):

* adoption: a mid-node restart (the drive cancelled while the nested job
  is gated) re-driven plainly adopts the in-flight child -- exactly one
  child row exists throughout, the node result carries the original
  child's identity, and the chain settles done over the child's re-cast
  fresh gate (the started->gated cycle re-mints it);
* fail-closed: two matching in-flight children fail the parent
  ``ExecutionUnknown`` naming the node, the count and the unattributable
  binding, with both children untouched and no third submit; a fabricated
  depth-invariant violation fails the same way;
* production assembly: the five-element composition (the T-C1-092 wiring
  plus ``subworkflow=True``) registers a subworkflow spec -- and a spec
  using the closed set's every node type -- with zero rejections.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from opencode.contracts.events import JobBudget, JobLineage
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineCheckpointer, EngineState, PipelineEngine
from opencode.orchestrator.gates import GateBroker
from opencode.orchestrator.llm_call import checkpointer_path_for
from opencode.orchestrator.workers import (
    WORKER_DEFAULT_TIMEOUT_S,
    WorkerManager,
    workers_runs_path_for,
)
from tests.integration.test_engine import _upgrade_head
from tests.integration.test_subworkflow import (
    _await_child,
    _gate_child_spec,
    _other_job_ids,
    _Stack,
    _sub_spec,
    _tool_spec,
)
from tests.integration.test_workers_e2e import _tiny_repo

#: One minimal subworkflow spec: a single ``subworkflow`` terminal node (the
#: registration face -- the production-reachability judge).
_SUBWORKFLOW_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "nested",
    "entry": "nested",
    "nodes": [{"node_id": "nested", "node_type": "subworkflow", "workflow_ref": "child"}],
    "edges": [],
}

#: A spec using the closed set's every node type: with the five-element
#: production composition there is no rejectable known type left (the
#: semantic gate's else message exists only for type-missing compositions).
#: Registration-only face: no node body ever runs from this file's assembly
#: tests, so the honest fakes below are never reached.
_FIVE_ELEMENT_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "full",
    "entry": "gate",
    "nodes": [
        {"node_id": "gate", "node_type": "human_gate"},
        {"node_id": "agent", "node_type": "opencode_agent", "prompt_template": "do the work"},
        {"node_id": "call", "node_type": "llm_call", "model": "fast", "prompt_template": "summarize it"},
        {"node_id": "work", "node_type": "tool", "command": ["true"]},
        {"node_id": "nested", "node_type": "subworkflow", "workflow_ref": "child"},
    ],
    "edges": [
        {"from_node": "gate", "to_node": "agent"},
        {"from_node": "agent", "to_node": "call"},
        {"from_node": "call", "to_node": "work"},
        {"from_node": "work", "to_node": "nested"},
    ],
}


async def _unreached_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The honest fake for the llm_call and the tool seams (registration
    only: any node reaching this body is a wiring bug, never a made-up
    result)."""
    raise AssertionError("the subworkflow assembly specs drive no llm_call/tool bodies; no node should reach them")


async def _await_fresh_gate(stack: _Stack, child_id: str, stale_gate: str, *, timeout_s: float = 5.0) -> str:
    """Poll the gate registry until the re-parked child carries a fresh active gate.

    The adopted child's ``human_gate`` body re-parks it through the legal
    started->gated cycle, which re-mints the gate (a new ``gat_`` id replaces
    the stale one in the registry) -- the reply must target the fresh gate.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        gate = stack.service.active_gate(child_id)
        assert asyncio.get_running_loop().time() < deadline, (
            f"child {child_id} never re-parked on a fresh gate (last active: {gate})"
        )
        if gate is not None and gate != stale_gate:
            return gate
        await asyncio.sleep(0.01)


async def _assembled(tmp_path: Path) -> PipelineEngine:
    """The production assembly mirror (the T-C1-092 form plus the P5-S9
    ``subworkflow=True`` flag): one migrated main DB, the DB-sibling runs
    root, a production-shaped WorkerManager (never driven by these specs)
    and a fresh GateBroker -- the exact seam values the CLI/serving
    assemblies now pass (the closed set's full five elements)."""
    db_path = tmp_path / "main.db"
    assert _upgrade_head(db_path).returncode == 0
    runs_root = workers_runs_path_for(db_path)
    runs_root.mkdir(parents=True, exist_ok=True)
    manager = WorkerManager(
        repo_root=_tiny_repo(tmp_path / "repo"),
        runs_root=runs_root,
        default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S),
        base_url="http://127.0.0.1:9",
        api_key="subworkflow-restart-e2e-key",
        model="fake-model",
        context_limit=100_000,
    )
    return PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
        llm_call=_unreached_body,
        tool=_unreached_body,
        opencode_agent=manager,
        gate_broker=GateBroker(),
        subworkflow=True,
    )


class TestMidNodeRestartAdoption:
    async def test_a_mid_node_restart_adopts_the_in_flight_nested_job(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, gate=True)
        try:
            await stack.register(_gate_child_spec())
            await stack.register(_sub_spec(ref="gate_child"))
            parent = await stack.service.submit("parent", {}, "ses_0009")
            # The first drive parks in the node body's poll loop while the
            # nested job sits on its own human gate (the mid-node window).
            drive = asyncio.create_task(stack.service.run(parent.job_id))
            child_id = await _await_child(stack.db_path, parent.job_id, {"gated"})
            assert (await stack.service.get_job(parent.job_id)).status == "running"
            first_gate = stack.service.active_gate(child_id)
            assert first_gate is not None

            # Death simulation: an UNMARKED task cancel (never the marked
            # service.cancel interrupt) -- the drive task ends cancelled and
            # touches no job state. The parent stays running, the child
            # stays gated: exactly the in-flight state a process death
            # leaves behind.
            drive.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drive
            assert (await stack.service.get_job(parent.job_id)).status == "running"
            assert (await stack.service.get_job(child_id)).status == "gated"

            # The plain re-drive (no resume flag): the idempotent re-entry
            # law resumes the parent's graph from the advisory cursor, the
            # re-entered body adopts the gated child instead of submitting
            # a duplicate, and the adopted child's gate body re-parks it
            # (a fresh gate replaces the first one).
            redrive = asyncio.create_task(stack.service.run(parent.job_id))
            fresh_gate = await _await_fresh_gate(stack, child_id, first_gate)
            replied = await stack.service.reply_gate(child_id, fresh_gate, "approve")
            assert replied.status in {"running", "done"}
            await redrive

            # The judges: exactly one child row throughout (the adoption
            # never re-submitted), and the node result carries the ORIGINAL
            # child's identity down the chain.
            assert _other_job_ids(stack.db_path, parent.job_id) == [child_id]
            final = await stack.service.get_job(parent.job_id)
            assert final.status == "done"
            result = final.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            nested = nodes["nested"]
            assert isinstance(nested, dict)
            assert nested["job_id"] == child_id
            assert nested["status"] == "done"
            child = await stack.service.get_job(child_id)
            assert child.status == "done"
        finally:
            await stack.close()


class TestAdoptionFailClosed:
    async def test_ambiguous_in_flight_children_fail_closed_execution_unknown(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_tool_spec())
            await stack.register(_sub_spec())
            parent = await stack.service.submit("parent", {}, "api")
            # Two in-flight children pre-planted through the PUBLIC submit
            # face (the lineage guard accepts both: the parent exists, the
            # ancestor chain stays within the cap, depth = parent + 1).
            planted = [
                await stack.service.submit(
                    "child",
                    {},
                    "api",
                    depth=1,
                    lineage=JobLineage(parent_job_id=parent.job_id, session_id=None, origin="api"),
                )
                for _ in range(2)
            ]
            await stack.service.run(parent.job_id)
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ExecutionUnknown"  # the branch-④ mirror
            message = str(record.error["message"])
            assert "nested" in message  # the node is named
            assert "2 in-flight children" in message  # the ambiguity is named
            assert "cannot be proven" in message  # the unattributable binding is named
            # Fail-closed touches no matched child: both stay non-terminal,
            # and no third child was ever submitted.
            for handle in planted:
                child = await stack.service.get_job(handle.job_id)
                assert child.status == "created"
            assert sorted(_other_job_ids(stack.db_path, parent.job_id)) == sorted(handle.job_id for handle in planted)
        finally:
            await stack.close()

    async def test_a_depth_invariant_violation_fails_closed_execution_unknown(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_tool_spec())
            await stack.register(_sub_spec())
            parent = await stack.service.submit("parent", {}, "api")
            planted = await stack.service.submit(
                "child",
                {},
                "api",
                depth=1,
                lineage=JobLineage(parent_job_id=parent.job_id, session_id=None, origin="api"),
            )
            # Fabricate the construction-invariant violation (honestly
            # labeled): the public submit face enforces depth = parent + 1,
            # so a matched child at a wrong depth is impossible through it.
            # The defense exists for exactly such an unattributable row.
            with sqlite3.connect(stack.db_path) as connection:
                connection.execute("UPDATE jobs SET depth = ? WHERE job_id = ?", (2, planted.job_id))
            await stack.service.run(parent.job_id)
            record = await stack.service.get_job(parent.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ExecutionUnknown"
            message = str(record.error["message"])
            assert "violates the parent+1 invariant" in message
            assert planted.job_id in message
            # The matched child is untouched (still non-terminal, still the
            # only child row -- nothing was re-submitted).
            child = await stack.service.get_job(planted.job_id)
            assert child.status == "created"
            assert _other_job_ids(stack.db_path, parent.job_id) == [planted.job_id]
        finally:
            await stack.close()


class TestFiveElementAssembly:
    async def test_the_production_assembly_registers_subworkflow_specs(self, tmp_path: Path) -> None:
        """P5-S9 production reachability: with ``subworkflow=True`` beside the
        T-C1-092 four-element wiring, a spec carrying a ``subworkflow`` node
        passes the engine's semantic gate, and the closed set's FULL
        five-element composition registers a spec using every known node
        type with zero rejections (the else dynamic message exists only for
        type-missing compositions now)."""
        engine = await _assembled(tmp_path)
        try:
            bound = await engine.register(_SUBWORKFLOW_SPEC)
            assert bound.spec.pipeline_id == "nested"
            full = await engine.register(_FIVE_ELEMENT_SPEC)
            assert full.spec.pipeline_id == "full"
        finally:
            # No App lifecycle in this test, so the close duty is ours: the
            # register rehearsal materializes the checkpointer's lazily-opened
            # aiosqlite connection (whose dedicated thread only its close
            # joins) -- the T-C1-091 exit-hang lesson, the ticket's §0-6 rule.
            await engine.close()
