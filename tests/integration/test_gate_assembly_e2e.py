"""Integration: the production gate-assembly composition (P5-S8, T-C1-092).

The exact assembly the CLI and the HTTP server now wire -- a fresh
:class:`~opencode.orchestrator.gates.GateBroker` (the S5a reply chain's pure
hand-off: the reply face resolves onto it, the ``human_gate`` node body takes
from it) injected as ``PipelineEngine(gate_broker=...)`` beside the
production-shaped :class:`~opencode.orchestrator.workers.WorkerManager` (the
P5-S7 face, mirrored), so a ``human_gate`` node passes the engine's semantic
gate in production (registration reachability -- the S5a refusal was a
wiring-gated face, not a hard one), and the reply chain closes through the
App's pre-seeded ``gate_broker`` pass-through with zero new code.

Honest scope (the ticket's §4-3 ruling): the full App-level reply chain
(submit -> gated -> reply -> done, the node result carrying the reply) is
already covered end-to-end by tests/integration/test_gate_e2e.py
(``TestGateCycle`` over the hand-assembled JobService, ``TestGateReplyRoute``
over the same ``App(pipeline_engine=...)`` shape) -- these assembly tests pin
the production wiring only, mirroring the T-C1-091 discipline: no worker
process is ever spawned, and the llm_call/tool seams are honest fakes that
raise (this spec drives a ``human_gate`` node only; a node reaching them is
a wiring bug, never a made-up result).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencode.app import App
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import JobBudget
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineCheckpointer, EngineState, PipelineEngine, PipelineSpecRejected
from opencode.orchestrator.gates import GateBroker
from opencode.orchestrator.llm_call import checkpointer_path_for
from opencode.orchestrator.workers import (
    WORKER_DEFAULT_TIMEOUT_S,
    WorkerManager,
    workers_runs_path_for,
)
from tests.integration.test_engine import _upgrade_head
from tests.integration.test_workers_e2e import _tiny_repo

#: One minimal valid gate spec: a single ``human_gate`` terminal node (the
#: tests/integration/test_gate_e2e.py face, trimmed -- no llm_call/tool/
#: opencode_agent node ever runs in this file, so the honest fakes below are
#: never reached and the real manager never spawns a worker).
_GATE_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "gated",
    "entry": "approval",
    "nodes": [{"node_id": "approval", "node_type": "human_gate"}],
    "edges": [],
}

#: The closed set's fifth name: a structurally valid ``subworkflow`` node the
#: four-element production composition rejects at the unimplemented-type face
#: (the pool-7 face -- restart idempotency -- owns its production wiring).
_SUBWORKFLOW_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "nested",
    "entry": "nested",
    "nodes": [{"node_id": "nested", "node_type": "subworkflow", "workflow_ref": "other"}],
    "edges": [],
}


async def _unreached_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The honest fake for the llm_call and the tool seams (this spec drives
    a ``human_gate`` node only, so any node reaching this body is a wiring
    bug, never a made-up result)."""
    raise AssertionError("the gate assembly spec wires no llm_call/tool bodies; no node should reach them")


async def _assembled(tmp_path: Path) -> tuple[Path, WorkerManager, PipelineEngine, GateBroker]:
    """The production assembly mirror (the T-C1-091 form plus the gate
    face): one migrated main DB, the DB-sibling runs root, a
    production-shaped WorkerManager (never driven by these specs) and a
    fresh GateBroker wired into a production-shaped engine -- the exact seam
    values the CLI/serving assemblies now pass."""
    db_path = tmp_path / "main.db"
    assert _upgrade_head(db_path).returncode == 0
    runs_root = workers_runs_path_for(db_path)
    runs_root.mkdir(parents=True, exist_ok=True)
    manager = WorkerManager(
        repo_root=_tiny_repo(tmp_path / "repo"),
        runs_root=runs_root,
        default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S),
        base_url="http://127.0.0.1:9",
        api_key="gate-assembly-e2e-key",
        model="fake-model",
        context_limit=100_000,
    )
    broker = GateBroker()
    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
        llm_call=_unreached_body,
        tool=_unreached_body,
        opencode_agent=manager,
        gate_broker=broker,
    )
    return db_path, manager, engine, broker


async def test_the_production_assembly_registers_a_gate_spec(tmp_path: Path) -> None:
    """(a) Production reachability: with the broker injected as
    ``gate_broker=``, a spec carrying a ``human_gate`` node passes the
    engine's semantic gate -- registration succeeds, the bound spec is
    retained, and the engine's public property hands the App's pass-through
    the very broker the host assembly constructed."""
    _, _, engine, broker = await _assembled(tmp_path)
    try:
        bound = await engine.register(_GATE_SPEC)
        assert bound.spec.pipeline_id == "gated"
        assert engine.gate_broker is broker  # the face the App's pass-through reads
    finally:
        # No App lifecycle in this test, so the close duty is ours: the
        # register rehearsal materializes the checkpointer's lazily-opened
        # aiosqlite connection (whose dedicated thread only its close joins)
        # -- the App.close/_WorkerStack.close discipline, applied directly
        # (the T-C1-091 exit-hang lesson, now a ticket §0-6 rule).
        await engine.close()


async def test_the_four_element_composition_names_its_executed_set(tmp_path: Path) -> None:
    """(b) The dynamic-message pin: the production composition executes
    exactly {human_gate, llm_call, opencode_agent, tool}, so the closed
    set's fifth name (subworkflow) is rejected with the else-branch message
    naming the executed set (the sorted join) and the §8 code
    UnsupportedCapability -- while the S2/S3 pinned variants stay
    byte-identical (tests/contract/test_pipelines.py locks both)."""
    _, _, engine, _ = await _assembled(tmp_path)
    try:
        with pytest.raises(PipelineSpecRejected) as raised:
            await engine.register(_SUBWORKFLOW_SPEC)
        error: PipelineSpecRejected = raised.value
        assert error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert error.details == {"node_id": "nested", "node_type": "subworkflow"}
        assert str(error) == (
            "unimplemented node type 'subworkflow' on node 'nested': "
            "this composition executes human_gate/llm_call/opencode_agent/tool nodes only "
            "(the closed set's other node types are not wired into this engine)"
        )
    finally:
        # A semantic-gate rejection never reaches the compile rehearsal (no
        # aiosqlite materialization), but the close duty is ours either way.
        await engine.close()


async def test_the_app_conducts_the_broker_into_the_jobs_service(tmp_path: Path) -> None:
    """(c) The App-level composition seam: ``App(pipeline_engine=...)``
    derives the JobService's gate hand-off from the engine's public
    ``gate_broker`` property (the pre-seeded pass-through, zero new code),
    so the broker the host assembly constructed is the one the reply face
    resolves onto and the ``human_gate`` node body takes from. White-box
    pin (``app._jobs._gate_broker``), choice recorded: the full App-level
    reply chain is already covered end-to-end by
    tests/integration/test_gate_e2e.py (``TestGateReplyRoute`` over the
    same ``App(pipeline_engine=...)`` shape), so this file pins the
    production conduction seam only -- duplicating the drive here would
    repeat covered behavior, not add a face."""
    db_path, _, engine, broker = await _assembled(tmp_path)
    await engine.register(_GATE_SPEC)
    async with App(db_path, working_dir=str(tmp_path), pipeline_engine=engine) as app:
        assert app.pipeline_engine is engine
        assert app._jobs._gate_broker is broker  # the pass-through's far end
    # The App lifecycle owns the engine's close (App.close closes the
    # pipeline engine), so no manual engine.close here.
