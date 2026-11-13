"""The pydag import seam end to end (T-C1-083): graph.json -> register -> execute.

The seam under test is ``PipelineEngine.register``'s loading router: a wire
object carrying ``schema_version: "pydag-editable-graph-v1"`` routes through
the pydag converter and the converted spec then flows the *unchanged* v1
path -- the three-layer registration gate (structure -> semantic ->
rehearsal), the §5.1 spec hash lock and the real compiled LangGraph
execution over the injected fake bodies (the S1 fake-executor discipline,
same as ``tests/integration/test_engine.py``).

Honest-label note: the ``llm_call``/``tool`` bodies below are fakes; the
``llm_call`` fake renders the converted prompt template through
``str.format`` with the job's submitted params -- the exact contract the
converted ``{{inputs.x}}`` -> ``{x}`` keys exist for -- so the roundtrip
proves the prompt conversion end to end without any real model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts.pipelines import compute_spec_hash
from opencode.contracts.pydag_import import (
    PYDAG_REF_UNSUPPORTED,
    PydagImportError,
    convert_pydag_graph,
)
from opencode.orchestrator.engine import PipelineSpecRejected
from tests.contract.test_pydag_import import _MINIFIED_GRAPH, _dep_edge, _node, _pydag_raw, _step_body
from tests.integration.test_engine import _FakeLlm, _FakeTool, _Stack


class _RenderingLlm(_FakeLlm):
    """The fake llm body: renders the converted template with the job params.

    Subclasses the cooperative fake only to satisfy the shared ``_Stack``
    injection pin (``llm: _FakeLlm``); the body below is entirely this
    file's rendering fake -- the parent's pause/fail machinery is unused.
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        self.seen.append(node.node_id)
        params = dict(state.get("params", {}))
        return {"rendered": node.prompt_template.format(**params), "model": node.model}


class _RecordingTool(_FakeTool):
    """The fake tool body: records the call and echoes the bound tool ref."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        self.seen.append(node.node_id)
        return {"tool_ref": node.tool_ref, "collected": True}


class TestPydagRoundtrip:
    async def test_converted_spec_registers_compiles_and_executes(self, tmp_path: Path) -> None:
        llm = _RenderingLlm()
        tool = _RecordingTool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            raw = _pydag_raw()
            bound = await stack.engine.register(raw)
            assert bound.spec.pipeline_id == "demo"
            assert bound.spec_hash == compute_spec_hash(convert_pydag_graph(raw))

            handle = await stack.service.submit("demo", {"ticket_id": "T-42", "region": "cn-north-4"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "demo",
                "nodes": {
                    "step_1": {"tool_ref": "demo_fetch_ticket", "collected": True},
                    "step_2": {"rendered": "Summarize ticket T-42 in region cn-north-4.", "model": "small"},
                },
            }
            assert tool.seen == ["step_1"]
            assert llm.seen == ["step_2"]
        finally:
            await stack.close()

    async def test_minified_graph_registers_with_the_identical_spec_hash(self, tmp_path: Path) -> None:
        llm = _RenderingLlm()
        tool = _RecordingTool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            pretty = await stack.engine.register(_pydag_raw())
            minified = await stack.engine.register(json.loads(_MINIFIED_GRAPH))
            assert minified.spec_hash == pretty.spec_hash
            assert minified.spec_hash == compute_spec_hash(convert_pydag_graph(_pydag_raw()))
        finally:
            await stack.close()

    async def test_import_rejections_surface_the_pydag_error_at_the_seam(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, llm=_RenderingLlm(), tool=_RecordingTool())
        try:
            raw = copy.deepcopy(_pydag_raw())
            raw["nodes"][1]["data"]["step"]["params"]["summary"] = {"ref": "step_1.ticket"}
            # The pydag rejection propagates from the loading seam with its
            # node-level diagnostic face (no silent fallthrough to the
            # pydantic structural layer, which would reject the string
            # schema_version with a far less useful error).
            with pytest.raises(PydagImportError) as excinfo:
                await stack.engine.register(raw)
            assert excinfo.value.diagnostics[0].code == PYDAG_REF_UNSUPPORTED
            assert excinfo.value.diagnostics[0].node_id == "step_2"
            assert stack.engine.bound("demo") is None
        finally:
            await stack.close()

    async def test_converted_specs_flow_the_existing_semantic_gate(self, tmp_path: Path) -> None:
        """A convertible graph that violates the ported topology is the gate's rejection."""
        stack = _Stack(tmp_path, llm=_RenderingLlm(), tool=_RecordingTool())
        try:
            raw = copy.deepcopy(_pydag_raw())
            raw["nodes"].append(_node("step_3", _step_body("step_3", depends_on=["step_4"])))
            raw["nodes"].append(_node("step_4", _step_body("step_4", depends_on=["step_3"])))
            raw["edges"].append(_dep_edge("step_3", "step_4"))
            raw["edges"].append(_dep_edge("step_4", "step_3"))
            with pytest.raises(PipelineSpecRejected, match="orphan nodes unreachable from the entry"):
                await stack.engine.register(raw)
        finally:
            await stack.close()
