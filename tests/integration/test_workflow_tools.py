"""Integration tests for the P3-S4 workflow tools + the gateway guards (T-C1-079).

Coverage (the S4 acceptance judges):

* the ``workflow__run`` contract: the submit is non-blocking by construction
  (a parked executor proves the tool returns while the job is still
  ``created``), the structured output carries the ``job_id`` immediately, the
  lineage is derived from the call context (origin ``session``), and a
  spawned fake drive settles ``done``;
* the params gate: zero-model strict JSON-Schema validation against the
  pipeline's declared ``params_schema`` -- a missing field, a type error and
  an unknown field are structured ``ValidationError`` terminals, an
  unregistered pipeline is ``NotFound``, a spec without a declared schema
  accepts only the empty object, a declared schema outside the strict subset
  fails closed, and the gate runs BEFORE the lineage guards (gateway order);
* the ``workflow__status`` poll: the job's read-model projection (status,
  result once terminal; neither while created) and ``NotFound`` for an
  unknown job;
* the M-9 recursion guard on the submit face: worker-origin rejection, the
  root depth-0 rule, a dangling parent, the legal ancestor chain up to
  depth 3 with the fourth rejected, the depth == parent + 1 rule, and the
  origin/anchor consistency both directions;
* registration isolation (C#5): both adapters register and resolve in a
  plain ToolRegistry (the main-brain direction) while a builtin-only catalog
  never exposes the workflow wire names (the worker-catalog direction);
* the App wiring: ``workflow_tools=True`` without the provider composition
  fails closed, and the default App carries no registry/brain.

Honest-label note: executors here are the S1/S2 fakes (``FakeJobExecutor``,
the parked executor) -- the real engine drive over LangGraph is pinned by
``test_engine.py`` and the full brain path by ``test_brain_e2e.py``.
Everything around them is real: a real migrated main jobs database, the real
``JobService``/``JobsStore`` commit spine, the real shared
``SpecParamsValidator``, and the real adapter/registry faces.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.contracts.events import JobLineage
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.pipelines import PipelineSpec
from opencode.contracts.tools import ToolResult
from opencode.orchestrator.jobs import (
    FakeJobExecutor,
    JobExecutor,
    JobHandle,
    JobService,
    JobsProjector,
    SpecParamsValidator,
)
from opencode.storage import open_database
from opencode.storage.jobs_store import JobRecord, JobsReadRepository, JobsStore
from opencode.storage.repos import NotFoundError
from opencode.storage.txn import StoreValidationError
from opencode.tools.builtin.read import FsReadAdapter
from opencode.tools.invoker import ToolContext
from opencode.tools.registry import ToolRegistry
from opencode.tools.workflow import WorkflowRunAdapter, WorkflowStatusAdapter
from tests.contract.test_pipelines import _validated

REPO_ROOT = Path(__file__).resolve().parents[2]

_PARAMS_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"run": {"type": "string", "description": "The run marker param."}},
    "required": ["run"],
    "additionalProperties": False,
}

#: The validator's lookup domain: one spec with a declared params schema, one
#: without (the empty-object default face), one declaring a schema outside the
#: strict subset (the fail-closed face).
_SPECS: dict[str, PipelineSpec] = {
    "params": _validated(pipeline_id="params", params_schema=_PARAMS_SCHEMA),
    "bare": _validated(pipeline_id="bare"),
    "weird": _validated(pipeline_id="weird", params_schema={"type": "array"}),
}

_SESSION_ID = "ses_wf_tools"


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


class _ParkedExecutor:
    """A drive that never settles: parked forever, so a returned tool call
    with the job still ``created`` proves the submit half is non-blocking."""

    async def drive(self, handle: JobHandle, *, resume: bool = False) -> None:
        await asyncio.Event().wait()


class _Harness:
    """One migrated main DB + a validator-wired JobService + the two adapters.

    The same shared ``SpecParamsValidator`` shape the App wires when
    ``workflow_tools=True``: strict validation over the registered-spec lookup
    seam, injected as the service's params gate.
    """

    def __init__(self, tmp_path: Path, *, executor: JobExecutor) -> None:
        self.db_path = tmp_path / "tools.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.database = open_database(self.db_path, mode="create")

        def lookup(pipeline_id: str) -> PipelineSpec | None:
            return _SPECS.get(pipeline_id)

        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=executor,
            params_validator=SpecParamsValidator(lookup=lookup),
        )
        self.run_tool = WorkflowRunAdapter(jobs=self.service)
        self.status_tool = WorkflowStatusAdapter(jobs=self.service)
        self.context = ToolContext(
            session_id=_SESSION_ID,
            run_id="run_wf_tools",
            message_id="msg_wf_tools",
            tool_call_id="call_wf_tools",
            working_dir=str(tmp_path),
        )

    async def run(self, pipeline_id: str, params: JsonObject) -> ToolResult:
        return await self.run_tool.execute(self.context, {"pipeline_id": pipeline_id, "params": params})

    async def close(self) -> None:
        await self.service.close()
        await self.database.close()


@pytest.fixture()
async def harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    """The default harness: the fake executor (settles done) over the shared specs."""
    instance = _Harness(tmp_path, executor=FakeJobExecutor())
    try:
        yield instance
    finally:
        await instance.close()


async def _await_status(service: JobService, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> JobRecord:
    """Poll the read model until the job settles into one of ``statuses``."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await service.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


def _structured(result: ToolResult) -> dict[str, Any]:
    """The structured output as a plain dict (assert-checked narrow)."""
    structured = result.structured_output
    assert isinstance(structured, dict)
    return structured


def _error_code(result: ToolResult) -> str:
    """The structured terminal's CoreError code (assert-checked present)."""
    error = result.error
    assert error is not None
    return error.code.value


def _text(result: ToolResult) -> str:
    """The first text content of a tool result (assert-checked present)."""
    first = result.content[0]
    assert isinstance(first, ToolTextContent)
    return first.text


class TestRunTool:
    async def test_submit_returns_immediately_with_the_structured_job_id(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path, executor=_ParkedExecutor())
        try:
            result = await asyncio.wait_for(harness.run("bare", {}), 2.0)
            assert result.error is None
            structured = _structured(result)
            job_id = structured["job_id"]
            assert isinstance(job_id, str) and job_id.startswith("job_")
            assert structured == {"job_id": job_id, "pipeline_id": "bare", "status": "created"}
            assert job_id in _text(result)
            # The parked drive proves the two-phase contract: the tool returned
            # and the job is still exactly at the submitted state.
            record = await harness.service.get_job(job_id)
            assert record.status == "created"
            assert record.pipeline_id == "bare"
            assert record.lineage == {"parent_job_id": None, "session_id": _SESSION_ID, "origin": "session"}
        finally:
            await harness.close()

    async def test_spawned_fake_execution_settles_done(self, harness: _Harness) -> None:
        result = await harness.run("params", {"run": "x"})
        assert result.error is None
        job_id = _structured(result)["job_id"]
        assert isinstance(job_id, str)
        record = await _await_status(harness.service, job_id, {"done"})
        assert record.result == {"executor": "fake"}
        assert record.params == {"run": "x"}


class TestParamsGate:
    async def test_missing_required_param_is_a_structured_rejection(self, harness: _Harness) -> None:
        result = await harness.run("params", {})
        assert _error_code(result) == "ValidationError"
        assert _text(result)

    async def test_type_error_is_a_structured_rejection(self, harness: _Harness) -> None:
        result = await harness.run("params", {"run": 5})
        assert _error_code(result) == "ValidationError"

    async def test_unknown_field_is_a_structured_rejection(self, harness: _Harness) -> None:
        result = await harness.run("params", {"run": "x", "extra": 1})
        assert _error_code(result) == "ValidationError"

    async def test_unregistered_pipeline_is_not_found(self, harness: _Harness) -> None:
        result = await harness.run("ghost", {})
        assert _error_code(result) == "NotFound"

    async def test_a_bare_spec_accepts_only_the_empty_object(self, harness: _Harness) -> None:
        accepted = await harness.run("bare", {})
        assert accepted.error is None
        rejected = await harness.run("bare", {"x": 1})
        assert _error_code(rejected) == "ValidationError"

    async def test_a_declared_schema_outside_the_strict_subset_fails_closed(self, harness: _Harness) -> None:
        result = await harness.run("weird", {})
        assert _error_code(result) == "ValidationError"

    async def test_the_params_gate_runs_before_the_lineage_guards(self, harness: _Harness) -> None:
        # The gateway order: an unregistered pipeline surfaces NotFound even
        # when the lineage is independently illegal (worker origin).
        with pytest.raises(NotFoundError):
            await harness.service.submit("ghost", {}, "api", lineage=JobLineage(origin="worker"))


class TestStatusTool:
    async def test_the_poll_reads_the_projection(self, harness: _Harness) -> None:
        submitted = await harness.run("params", {"run": "x"})
        job_id = _structured(submitted)["job_id"]
        assert isinstance(job_id, str)
        await _await_status(harness.service, job_id, {"done"})
        result = await harness.status_tool.execute(harness.context, {"job_id": job_id})
        assert result.error is None
        structured = _structured(result)
        assert structured["job_id"] == job_id
        assert structured["pipeline_id"] == "params"
        assert structured["status"] == "done"
        assert structured["result"] == {"executor": "fake"}
        assert "status=done" in _text(result)

    async def test_a_created_job_carries_neither_result_nor_error(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path, executor=_ParkedExecutor())
        try:
            submitted = await harness.run("bare", {})
            job_id = _structured(submitted)["job_id"]
            assert isinstance(job_id, str)
            result = await harness.status_tool.execute(harness.context, {"job_id": job_id})
            structured = _structured(result)
            assert structured["status"] == "created"
            assert "result" not in structured
            assert "error" not in structured
        finally:
            await harness.close()

    async def test_an_unknown_job_is_not_found(self, harness: _Harness) -> None:
        result = await harness.status_tool.execute(harness.context, {"job_id": "job_missing"})
        assert _error_code(result) == "NotFound"


class TestRecursionGuard:
    """The M-9 guards over the provider-less App's own jobs face."""

    def _app(self, tmp_path: Path, name: str) -> App:
        db_path = tmp_path / name
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        return App(db_path, working_dir="C:/work")

    async def test_worker_origin_submission_is_rejected(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-worker.db")
        try:
            with pytest.raises(StoreValidationError, match="worker-identity"):
                await app.jobs.submit("digest", {}, "api", lineage=JobLineage(origin="worker"))
        finally:
            await app.close()

    async def test_a_root_submission_must_carry_depth_zero(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-root.db")
        try:
            with pytest.raises(StoreValidationError, match="root submission"):
                await app.jobs.submit("digest", {}, "api", depth=1, lineage=JobLineage(origin="api"))
        finally:
            await app.close()

    async def test_a_dangling_parent_job_is_not_found(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-dangling.db")
        try:
            with pytest.raises(NotFoundError):
                await app.jobs.submit("digest", {}, "api", lineage=JobLineage(parent_job_id="job_ghost", origin="api"))
        finally:
            await app.close()

    async def test_the_chain_to_depth_three_is_legal_and_the_fourth_is_rejected(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-chain.db")
        try:
            chain: list[str] = []
            parent: str | None = None
            for depth in range(4):  # A(0) -> B(1) -> C(2) -> D(3): all legal
                handle = await app.jobs.submit(
                    "digest", {}, "api", depth=depth, lineage=JobLineage(parent_job_id=parent, origin="api")
                )
                chain.append(handle.job_id)
                parent = handle.job_id
            record = await app.jobs.get_job(chain[3])
            assert record.depth == 3
            # The second line: depth 4 exceeds the hard cap (the ancestor-chain
            # walk would also exceed it; the cap fires first).
            with pytest.raises(StoreValidationError, match="exceeds the hard cap"):
                await app.jobs.submit(
                    "digest", {}, "api", depth=4, lineage=JobLineage(parent_job_id=chain[3], origin="api")
                )
        finally:
            await app.close()

    async def test_depth_must_equal_the_parent_depth_plus_one(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-depth.db")
        try:
            root = await app.jobs.submit("digest", {}, "api", lineage=JobLineage(origin="api"))
            with pytest.raises(StoreValidationError, match="parent job's depth"):
                await app.jobs.submit(
                    "digest", {}, "api", depth=2, lineage=JobLineage(parent_job_id=root.job_id, origin="api")
                )
        finally:
            await app.close()

    async def test_origin_api_requires_the_api_anchor(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-api.db")
        try:
            with pytest.raises(StoreValidationError, match="origin 'api' requires"):
                await app.jobs.submit("digest", {}, "ses_one", lineage=JobLineage(origin="api"))
        finally:
            await app.close()

    async def test_origin_session_requires_the_matching_anchor(self, tmp_path: Path) -> None:
        app = self._app(tmp_path, "guard-session.db")
        try:
            with pytest.raises(StoreValidationError, match="origin 'session' requires"):
                await app.jobs.submit("digest", {}, "api", lineage=JobLineage(origin="session"))
            with pytest.raises(StoreValidationError, match="origin 'session' requires"):
                await app.jobs.submit(
                    "digest", {}, "ses_one", lineage=JobLineage(session_id="ses_two", origin="session")
                )
            handle = await app.jobs.submit(
                "digest", {}, "ses_one", lineage=JobLineage(session_id="ses_one", origin="session")
            )
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "created"
        finally:
            await app.close()


class TestRegistryIsolation:
    async def test_the_brain_registry_resolves_the_workflow_pair(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path, executor=FakeJobExecutor())
        try:
            registry = ToolRegistry()
            registry.register(harness.run_tool)
            registry.register(harness.status_tool)
            catalog = registry.snapshot("ses_registry")
            run_spec = catalog.resolve("workflow__run")
            assert run_spec is not None
            assert run_spec.namespace == "workflow"
            assert run_spec.side_effects is True
            status_spec = catalog.resolve("workflow__status")
            assert status_spec is not None
            assert status_spec.side_effects is False
        finally:
            await harness.close()

    def test_a_builtin_only_catalog_never_sees_the_workflow_tools(self) -> None:
        # The worker-catalog direction (C#5): a registry built from the
        # builtin set exposes no workflow wire names.
        registry = ToolRegistry()
        registry.register(FsReadAdapter())
        catalog = registry.snapshot("ses_worker")
        assert catalog.resolve("workflow__run") is None
        assert catalog.resolve("workflow__status") is None


class TestAppWiring:
    async def test_workflow_tools_without_the_provider_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(StoreValidationError, match="workflow_tools=True requires"):
            App(tmp_path / "nowhere.db", workflow_tools=True)

    async def test_the_default_app_carries_no_registry_or_brain(self, tmp_path: Path) -> None:
        db_path = tmp_path / "default.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        app = App(db_path, working_dir="C:/work")
        try:
            assert app.registry is None
            assert app.brain is None
            assert app.jobs is not None
        finally:
            await app.close()
