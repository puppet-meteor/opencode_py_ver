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

v2 (T-C1-084): the seam now legalizes the superface 083 rejected -- params
refs (inputs/step), conditions, extract and llm structured output convert
into PipelineSpec v2 node fields, and :class:`TestV2Execution` below drives
their engine semantics end to end (the resolved ``node_params`` view, the
condition skip record, the honest refs-onto-skipped failures, the extract
projection) through the same fake-body discipline.
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
    PYDAG_SCHEMA_VERSION,
    PydagImportError,
    convert_pydag_graph,
)
from opencode.orchestrator.engine import PipelineSpecRejected, is_skip_record, node_params_of
from opencode.orchestrator.llm_call import render_prompt
from tests.contract.test_pydag_import import _MINIFIED_GRAPH, _dep_edge, _node, _pydag_raw, _step_body
from tests.integration.test_engine import _checkpoint_state, _FakeLlm, _FakeTool, _Stack


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
            raw["nodes"][1]["data"]["step"]["params"]["summary"] = {"ref": "inputs.ghost"}
            # The pydag rejection propagates from the loading seam with its
            # node-level diagnostic face (no silent fallthrough to the
            # pydantic structural layer, which would reject the string
            # schema_version with a far less useful error). v2 note: a step
            # ref now converts, so the rejection face is pinned with an
            # inputs ref naming an input var the pipeline does not declare.
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


# ---------------------------------------------------------------------------
# The v2 execution semantics (T-C1-084): params refs, condition skips,
# extract projection -- the converted superface driven through the real
# compiled graph, same fake-body discipline as above.
# ---------------------------------------------------------------------------

#: The structured-output schema step_2 declares (strict: exactly one string
#: "answer" key -- a completion the scripted provider returns either
#: satisfies or clearly does not).
_V2_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _v2_raw(*, condition: str = "step_1.collected is true") -> dict[str, Any]:
    """One v2 graph: tool step_1 -> llm step_2 -> tool step_3.

    step_1 binds an ``inputs`` ref param, step_2 carries a condition, a
    prompt with a step ref, the structured-output schema and its fallback,
    and step_3 binds a step-ref param and projects its own result through
    ``extract`` (a first-hit-wins chain plus a plain path).
    """
    step_1 = _step_body(
        "step_1",
        params={"name": {"ref": "inputs.sop_name"}, "ticket_id": "T-1"},
        output="sop",
    )
    step_2 = _step_body(
        "step_2",
        tool="builtin.compute",
        params={
            "summary": {
                "llm_call": {
                    "prompt": "Summarize {{step_1.sop_data}} for {{inputs.sop_name}}.",
                    "schema": _V2_OUTPUT_SCHEMA,
                    "fallback": {"answer": "no summary"},
                }
            }
        },
        output="summary",
        depends_on=["step_1"],
        condition=condition,
        description="summarize the sop",
    )
    step_3 = _step_body(
        "step_3",
        tool="demo_publish",
        params={"payload": {"ref": "step_2.answer"}},
        output="published",
        depends_on=["step_2"],
        extract={"published": ["result.data", "result"], "answer": "result.answer"},
        description="publish the summary",
    )
    return {
        "schema_version": PYDAG_SCHEMA_VERSION,
        "skill_name": "demo_v2",
        "pipeline": {
            "name": "demo_v2",
            "description": "v2 demo pipeline",
            "input_vars": ["ticket_id", "sop_name"],
            "execution_mode": "full",
        },
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "nodes": [
            _node("step_1", step_1),
            _node("step_2", step_2),
            _node("step_3", step_3),
        ],
        "edges": [_dep_edge("step_1", "step_2"), _dep_edge("step_2", "step_3")],
    }


async def _run_v2(stack: _Stack, *, condition: str = "step_1.collected is true") -> Any:
    """Register the v2 graph and drive one job; returns the settled record."""
    await stack.engine.register(_v2_raw(condition=condition))
    handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
    await stack.service.run(handle.job_id)
    return await stack.service.get_job(handle.job_id)


class _V2Tool(_FakeTool):
    """The v2 fake tool: records the resolved ``node_params`` view per node."""

    def __init__(self) -> None:
        super().__init__()
        self.params: dict[str, dict[str, Any]] = {}

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("ticket_id", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        resolved = node_params_of(state)
        self.params[node.node_id] = dict(resolved)
        if node.node_id == "step_1":
            return {
                "collected": True,
                "name": resolved["name"],
                "sop_data": {"steps": ["stop", "start"], "title": "restart"},
            }
        payload = resolved["payload"]
        return {"result": {"answer": payload, "data": {"answer": payload, "ok": True}}}


class _V2Llm(_FakeLlm):
    """The v2 fake llm: renders through the production renderer over the merged context."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("ticket_id", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        params = dict(state.get("params", {}))
        results = {
            key: value for key, value in dict(state.get("node_results", {})).items() if not is_skip_record(value)
        }
        text = render_prompt(node.prompt_template, {**params, **results})
        self.rendered.append(text)
        return {"answer": text, "model": node.model}


class TestV2Execution:
    async def test_params_refs_condition_extract_flow_end_to_end(self, tmp_path: Path) -> None:
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            bound = await stack.engine.register(_v2_raw())
            assert bound.spec.nodes[0].params == {
                "name": {"ref": "inputs.sop_name"},
                "ticket_id": "T-1",
            }
            assert bound.spec.nodes[1].condition == "step_1.collected is true"
            assert bound.spec.nodes[1].output_schema == _V2_OUTPUT_SCHEMA
            assert bound.spec.nodes[1].fallback == {"answer": "no summary"}
            assert bound.spec.nodes[2].extract == {
                "published": ["result.data", "result"],
                "answer": "result.answer",
            }

            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # The step ref resolved into the real JSON object (canonical
            # JSON, sorted keys), the inputs ref into the submitted param.
            answer = 'Summarize {"steps":["stop","start"],"title":"restart"} for runbook-a.'
            assert record.result == {
                "pipeline_id": "demo_v2",
                "nodes": {
                    "step_1": {
                        "collected": True,
                        "name": "runbook-a",
                        "sop_data": {"steps": ["stop", "start"], "title": "restart"},
                    },
                    "step_2": {"answer": answer, "model": "small"},
                    # extract replaced step_3's downstream-visible result:
                    # the chain's first hit won and the plain path resolved.
                    "step_3": {"published": {"answer": answer, "ok": True}, "answer": answer},
                },
            }
            # The wrapper resolved the refs and handed the bodies the view.
            assert tool.params["step_1"] == {"name": "runbook-a", "ticket_id": "T-1"}
            assert tool.params["step_3"] == {"payload": answer}
            assert llm.rendered == [answer]
            assert tool.calls["T-9"] == ["step_1", "step_3"]
            assert llm.calls["T-9"] == ["step_2"]
            # node_params is the ephemeral body view, never a channel: the
            # pinned checkpoint channel set gains no v2 key.
            values = await _checkpoint_state(stack, handle.job_id)
            assert "node_params" not in values
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
        finally:
            await stack.close()

    async def test_a_false_condition_skips_the_node_and_downstream_refs_fail_honestly(self, tmp_path: Path) -> None:
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            record = await _run_v2(stack, condition="step_1.collected is false")
            assert record.status == "failed"
            assert record.error is not None
            # The WorkerError keeps its own §8 code out of the node failure.
            assert record.error["code"] == "ValidationError"
            assert record.error["message"] is not None
            assert "references skipped step 'step_2'" in str(record.error["message"])
            details = record.error["details"]
            assert isinstance(details, dict)
            assert details["node_id"] == "step_3"
            # The llm body never ran; the tool ran only its non-skipped part.
            assert llm.rendered == []
            assert tool.calls["T-9"] == ["step_1"]
        finally:
            await stack.close()

    async def test_a_condition_ref_to_a_skipped_step_fails_the_job_honestly(self, tmp_path: Path) -> None:
        """step_1's own condition is false; step_2's condition refs the skipped step."""
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            raw = _v2_raw()
            raw["nodes"][0]["data"]["step"]["condition"] = "inputs.skip_first is true"
            raw["nodes"][1]["data"]["step"]["condition"] = "step_1.collected is true"
            await stack.engine.register(raw)
            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ValidationError"
            assert "references skipped step 'step_1'" in str(record.error["message"])
            details = record.error["details"]
            assert isinstance(details, dict)
            assert details["node_id"] == "step_2"
            # Neither body ever ran: step_1 skipped, step_2 failed at its gate.
            assert tool.calls == {}
            assert llm.calls == {}
        finally:
            await stack.close()

    async def test_a_skipped_terminal_node_completes_with_the_skip_record(self, tmp_path: Path) -> None:
        """A false condition on the terminal node: outcome done, the explicit skip record."""
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            raw = _v2_raw(condition="step_1.collected is false")
            raw["nodes"].pop()  # step_3 gone: step_2 becomes the terminal node
            raw["edges"].pop()
            # An extract on the skipped node never projects: the skip record
            # is the marker, not a projectable result.
            raw["nodes"][1]["data"]["step"]["extract"] = {"answer": "answer"}
            await stack.engine.register(raw)
            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "demo_v2",
                "nodes": {
                    "step_1": {
                        "collected": True,
                        "name": "runbook-a",
                        "sop_data": {"steps": ["stop", "start"], "title": "restart"},
                    },
                    "step_2": {
                        "skipped": True,
                        "condition": "step_1.collected is false",
                        "reason": "condition evaluated to false: step_1.collected is false",
                    },
                },
            }
            assert llm.rendered == []
        finally:
            await stack.close()
