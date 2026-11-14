"""Integration: the production workers-assembly composition (P5-S7, T-C1-091).

The exact assembly the CLI and the HTTP server now wire -- a production-shaped
:class:`~opencode.orchestrator.workers.WorkerManager` (the real
``opencode_agent`` node body: the default real-CLI ``worker_command``, the
``WORKER_DEFAULT_TIMEOUT_S`` wall-clock line as the default budget, the
DB-sibling runs root) injected as ``PipelineEngine(opencode_agent=...)`` and
mirrored through ``App(pipeline_engine=...)`` -- so an ``opencode_agent`` node
passes the engine's semantic gate in production (registration reachability),
and the App's isinstance probe closes the jobs cancel chain with zero new
code (a cancelled job latches the manager's cancel event, the observable side
effect a live worker tree kill rides on).

Honest scope (the ticket's §4 (c) ruling): real worker *drives* are already
covered end-to-end by tests/integration/test_workers.py (scripted subprocess
workers through the real manager face); these assembly tests never spawn a
worker process -- they pin the production wiring only. The llm_call/tool
seams are honest fakes that raise: this spec drives an ``opencode_agent`` node
only, and a node reaching them is a wiring bug, never a made-up result.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from opencode.app import App
from opencode.contracts.events import JobBudget
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineCheckpointer, EngineState, PipelineEngine
from opencode.orchestrator.llm_call import checkpointer_path_for
from opencode.orchestrator.workers import (
    WORKER_DEFAULT_TIMEOUT_S,
    WorkerManager,
    workers_runs_path_for,
)
from tests.integration.test_engine import _upgrade_head

#: One minimal valid agent spec: a single ``opencode_agent`` terminal node
#: (the tests/integration/test_workers.py face, mirrored).
_AGENT_SPEC: JsonObject = {
    "schema_version": 1,
    "pipeline_id": "agent",
    "entry": "worker",
    "nodes": [{"node_id": "worker", "node_type": "opencode_agent", "prompt_template": "Execute the task brief."}],
    "edges": [],
}


async def _unreached_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The honest fake for both the llm_call and the tool seams (this spec
    drives an ``opencode_agent`` node only, so any node reaching this body is
    a wiring bug, never a made-up result)."""
    raise AssertionError("the workers assembly spec wires no llm_call/tool bodies; no node should reach them")


def _tiny_repo(path: Path) -> Path:
    """One seeded git repository (the tests/integration/test_workers.py form,
    mirrored: identity via ``-c`` flags; no global config touched)."""
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


async def _assembled(tmp_path: Path) -> tuple[Path, WorkerManager, PipelineEngine]:
    """The production assembly mirror: one migrated main DB, the DB-sibling
    runs root mkdir'd by the host (the composition-root guarantee), and a
    production-shaped manager wired into a production-shaped engine.

    Every value mirrors the CLI/serving assembly sources: repo_root = the
    pinned project root (a real tiny repo stands in for the loader's root),
    runs_root = ``workers_runs_path_for(db_path)`` (the sibling derivation),
    the default budget = ``JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S)``
    (the constant -- no token/tool-call lines, the per-job override face
    stays with the submit params), and fake provider values for the
    host-only faces (the credentials ride the composition only).
    """
    db_path = tmp_path / "main.db"
    assert _upgrade_head(db_path).returncode == 0
    runs_root = workers_runs_path_for(db_path)
    runs_root.mkdir(parents=True, exist_ok=True)
    manager = WorkerManager(
        repo_root=_tiny_repo(tmp_path / "repo"),
        runs_root=runs_root,
        default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S),
        base_url="http://127.0.0.1:9",
        api_key="workers-e2e-key",
        model="fake-model",
        context_limit=100_000,
    )
    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
        llm_call=_unreached_body,
        tool=_unreached_body,
        opencode_agent=manager,
    )
    return db_path, manager, engine


def test_the_runs_root_derives_from_the_db_path() -> None:
    """The sibling-derivation pin (the checkpointer_path_for form): a
    directory named ``<stem>-workers`` beside the main DB, never inside it."""
    assert workers_runs_path_for(Path("/data/app.db")) == Path("/data/app-workers")
    assert workers_runs_path_for("/data/app.sqlite3") == Path("/data/app-workers")
    assert workers_runs_path_for("bare") == Path("bare-workers")


def test_the_default_worker_wall_clock_line_is_the_pinned_constant() -> None:
    """The production value pin: a 1800.0s stop-loss for a whole real-CLI
    worker drive -- an independent line from the LLM request deadline
    (RuntimeSettings.timeout_s), never derived from it."""
    assert WORKER_DEFAULT_TIMEOUT_S == 1800.0


async def test_the_production_assembly_registers_an_agent_spec(tmp_path: Path) -> None:
    """(a) Production reachability: with the real manager injected as
    ``opencode_agent=``, a spec carrying an ``opencode_agent`` node passes
    the engine's semantic gate (the S2 refusal is a wiring-gated face, not a
    hard one) -- registration succeeds and the bound spec is retained."""
    _, manager, engine = await _assembled(tmp_path)
    bound = await engine.register(_AGENT_SPEC)
    assert bound.spec.pipeline_id == "agent"
    assert engine.opencode_agent is manager  # the face the App's isinstance probe reads
    # No App lifecycle in this test, so the close duty is ours: the register
    # rehearsal materializes the checkpointer's lazily-opened aiosqlite
    # connection (whose dedicated thread only its close joins) -- the
    # _WorkerStack.close / App.close discipline, applied directly.
    await engine.close()


async def test_the_app_wires_the_manager_as_the_jobs_canceller(tmp_path: Path) -> None:
    """(b) The cancel chain closes through the App's pre-seeded seam: the
    isinstance probe sees the production manager and wires it as the jobs
    canceller with zero new code -- cancelling a submitted job latches the
    manager's cancel event (the observable side effect the worker tree kill
    rides on) before the terminal emit, and the job settles ``cancelled``
    (the fold's legal non-terminal-to-cancelled transition). Host-only
    discipline: no committed event row carries the composition's
    credentials."""
    db_path, manager, engine = await _assembled(tmp_path)
    await engine.register(_AGENT_SPEC)
    async with App(db_path, working_dir=str(tmp_path), pipeline_engine=engine) as app:
        assert app.pipeline_engine is engine
        handle = await app.jobs.submit("agent", {}, "api")
        record = await app.jobs.get_job(handle.job_id)
        assert record.status == "created"
        settled = await app.jobs.cancel(handle.job_id)
        assert settled.status == "cancelled"
        assert manager._cancel_event(handle.job_id).is_set()  # the hook fired before the terminal emit
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM events").fetchall()
    dump = repr(rows)
    assert "workers-e2e-key" not in dump
    assert "http://127.0.0.1:9" not in dump
