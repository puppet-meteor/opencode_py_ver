"""Integration: the production pipeline-engine composition's tool arm (P5-S5, T-C1-089).

The exact assembly the CLI and the HTTP server now wire -- ``EngineCheckpointer``
at the DB-derived sqlite path (M-7: its own file), the real
``run_command_tool_body`` over the §5.3 process primitive, an honestly
labeled llm body (this spec drives no ``llm_call`` node; the fake raises if
one ever reaches it), and ``App(pipeline_engine=...)`` routing the jobs
service onto the engine -- driven end to end against a real
alembic-migrated jobs database: register a pipeline with a ``tool``
command node, submit a job, and pin that ``node_results`` carries the
pinned envelope. A non-zero exit pins the §8 ``ToolFailed`` job face (the
engine keeps the body's ``WorkerError`` code instead of the generic
default).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

from opencode.app import App
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineCheckpointer, EngineState, PipelineEngine
from opencode.orchestrator.llm_call import checkpointer_path_for
from opencode.orchestrator.tool import run_command_tool_body
from tests.integration.test_engine import _upgrade_head


async def _no_llm_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The honest llm fake: this composition drives tool nodes only, so an
    ``llm_call`` node reaching this body is a wiring bug, never a made-up result."""
    raise AssertionError("the tool e2e composition wires no llm_call body; no llm node should reach it")


def _pipeline_spec(command: list[str]) -> JsonObject:
    """One single-node tool pipeline: the command arm, no params."""
    return {
        "schema_version": 1,
        "pipeline_id": "collect",
        "entry": "collect",
        "nodes": [{"node_id": "collect", "node_type": "tool", "command": cast("list[JsonValue]", command)}],
        "edges": [],
    }


async def _drive_one_job(tmp_path: Path, command: list[str]) -> tuple[str, JsonValue | None, JsonObject | None]:
    """The production composition over a real migrated DB: one job, driven to terminal."""
    db_path = tmp_path / "main.db"
    assert _upgrade_head(db_path).returncode == 0
    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
        llm_call=_no_llm_body,
        tool=run_command_tool_body,
    )
    await engine.register(_pipeline_spec(command))
    async with App(db_path, working_dir=str(tmp_path), pipeline_engine=engine) as app:
        handle = await app.jobs.submit("collect", {}, "api")
        await app.jobs.run(handle.job_id)
        record = await app.jobs.get_job(handle.job_id)
        return record.status, record.result, record.error


async def test_production_engine_drives_a_command_node_to_the_pinned_envelope(tmp_path: Path) -> None:
    status, result, error = await _drive_one_job(
        tmp_path, [sys.executable, "-c", "import sys; sys.stdout.write('tool-ok')"]
    )
    assert status == "done", f"error: {error}"
    assert result == {
        "pipeline_id": "collect",
        "nodes": {
            "collect": {
                "stdout": "tool-ok",
                "stderr": "",
                "exit_code": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
        },
    }


async def test_a_non_zero_exit_fails_the_job_with_the_tool_failed_face(tmp_path: Path) -> None:
    status, result, error = await _drive_one_job(
        tmp_path, [sys.executable, "-c", "import sys; sys.stderr.write('kaputt'); sys.exit(3)"]
    )
    assert status == "failed"
    assert result is None
    assert error is not None
    assert error["code"] == "ToolFailed"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["node_id"] == "collect"
    assert details["node_type"] == "tool"
