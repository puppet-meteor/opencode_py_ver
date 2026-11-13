"""The pydag import contract (T-C1-083): graph.json -> PipelineSpec mapping + diagnostics.

CORE-CONTRACTS §1 L22 discipline: the converter's real behavior is locked by
positive/negative fixtures. The positive face pins every mapping (header,
input_vars -> params_schema, tool nodes, llm prompt refs -> str.format keys,
edges, the computed entry, the ignored canvas metadata); the negative face
pins every diagnostic code (PYDAG_STRUCTURAL / PYDAG_UNSUPPORTED_FIELD /
PYDAG_UNKNOWN_TOOL / PYDAG_REF_UNSUPPORTED) with the node-level context the
error carries. All fixtures are inline (dict builders plus one minified
single-line JSON string, the dws-disk-diagnosis serialization form) -- no
dependency on the external framework repo.

The engine roundtrip over a converted spec (register -> compile -> execute
with the fake injection bodies) lives in
``tests/integration/test_pydag_engine.py``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.messages import JsonObject
from opencode.contracts.pipelines import PipelineSpec, compute_spec_hash
from opencode.contracts.pydag_import import (
    PYDAG_LLM_MODEL_TIER,
    PYDAG_REF_UNSUPPORTED,
    PYDAG_SCHEMA_VERSION,
    PYDAG_STRUCTURAL,
    PYDAG_UNKNOWN_TOOL,
    PYDAG_UNSUPPORTED_FIELD,
    PydagDiagnostic,
    PydagImportError,
    convert_pydag_graph,
    parse_pipeline_spec,
)


def _step_body(step_id: str, **overrides: Any) -> dict[str, Any]:
    """One dialect step payload: a tool step with literal params (the pass-through case)."""
    step: dict[str, Any] = {
        "id": step_id,
        "tool": "demo_fetch_ticket",
        "params": {"ticket_id": "T-1", "verbose": True},
        "output": "ticket",
        "output_mode": "replace",
        "depends_on": [],
        "extract": None,
        "transform": None,
        "output_format": None,
        "instance_binding": None,
        "condition": None,
        "description": f"step {step_id}",
    }
    step.update(overrides)
    return step


def _node(step_id: str, step: dict[str, Any]) -> dict[str, Any]:
    """One dialect node: the step wrapped in the editor's canvas metadata."""
    return {
        "id": step_id,
        "type": "pydagStep",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": step_id,
            "ui": {"collapsed": False, "color": "default"},
            "diagnostics": [],
            "provenance": {"source_file": "SKILL.md", "source_heading": step_id, "source_text": f"step {step_id}"},
            "param_resolution": {},
            "step": step,
        },
    }


def _dep_edge(source: str, target: str) -> dict[str, Any]:
    """One dialect dependency edge (the only topology edge face)."""
    return {
        "id": f"{source}->{target}:depends_on",
        "source": source,
        "target": target,
        "type": "pydagDependency",
        "data": {"kind": "depends_on"},
    }


def _pydag_raw() -> dict[str, Any]:
    """One minimal valid graph: tool step_1 -> llm step_2, two input vars.

    Carries the full canvas metadata face (position/ui/diagnostics/
    provenance/param_resolution plus the viewport and the edge id) so the
    ignored-metadata rule is exercised by every positive test.
    """
    llm_step = _step_body(
        "step_2",
        tool="builtin.compute",
        params={
            "summary": {
                "llm_call": {
                    "prompt": "Summarize ticket {{inputs.ticket_id}} in region {{region}}.",
                    "schema": None,
                    "fallback": None,
                }
            }
        },
        output="summary",
        depends_on=["step_1"],
        description="summarize the ticket",
    )
    return {
        "schema_version": PYDAG_SCHEMA_VERSION,
        "skill_name": "demo",
        "pipeline": {
            "name": "demo",
            "description": "demo pipeline",
            "input_vars": ["ticket_id", "region"],
            "execution_mode": "full",
        },
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "nodes": [_node("step_1", _step_body("step_1")), _node("step_2", llm_step)],
        "edges": [_dep_edge("step_1", "step_2")],
    }


#: The same graph in the minified single-line serialization (the
#: dws-disk-diagnosis form): one JSON line, no whitespace.
_MINIFIED_GRAPH = (
    '{"schema_version":"pydag-editable-graph-v1","skill_name":"demo","pipeline":{"name":"demo",'
    '"description":"demo pipeline","input_vars":["ticket_id","region"],"execution_mode":"full"},'
    '"viewport":{"x":0,"y":0,"zoom":1},"nodes":['
    '{"id":"step_1","type":"pydagStep","position":{"x":0,"y":0},"data":{"label":"step_1",'
    '"ui":{"collapsed":false,"color":"default"},"diagnostics":[],"provenance":{"source_file":"SKILL.md",'
    '"source_heading":"step_1","source_text":"step step_1"},"param_resolution":{},"step":{"id":"step_1",'
    '"tool":"demo_fetch_ticket","params":{"ticket_id":"T-1","verbose":true},"output":"ticket",'
    '"output_mode":"replace","depends_on":[],"extract":null,"transform":null,"output_format":null,'
    '"instance_binding":null,"condition":null,"description":"step step_1"}}},'
    '{"id":"step_2","type":"pydagStep","position":{"x":0,"y":0},"data":{"label":"step_2",'
    '"ui":{"collapsed":false,"color":"default"},"diagnostics":[],"provenance":{"source_file":"SKILL.md",'
    '"source_heading":"step_2","source_text":"step step_2"},"param_resolution":{},"step":{"id":"step_2",'
    '"tool":"builtin.compute","params":{"summary":{"llm_call":{"prompt":'
    '"Summarize ticket {{inputs.ticket_id}} in region {{region}}.","schema":null,"fallback":null}}},'
    '"output":"summary","output_mode":"replace","depends_on":["step_1"],"extract":null,"transform":null,'
    '"output_format":null,"instance_binding":null,"condition":null,"description":"summarize the ticket"}}}],'
    '"edges":[{"id":"step_1->step_2:depends_on","source":"step_1","target":"step_2",'
    '"type":"pydagDependency","data":{"kind":"depends_on"}}]}'
)


def _rejected(raw: dict[str, Any]) -> PydagImportError:
    with pytest.raises(PydagImportError) as excinfo:
        convert_pydag_graph(raw)
    return excinfo.value


def _first_diagnostic(raw: dict[str, Any], *, code: str, field: str | None = None) -> PydagDiagnostic:
    error = _rejected(raw)
    assert error.diagnostics, "a rejection carries at least one diagnostic"
    first = error.diagnostics[0]
    assert first.code == code
    if field is not None:
        assert first.field == field
    return first


class TestHeaderMapping:
    def test_pipeline_name_becomes_the_pipeline_id(self) -> None:
        spec = convert_pydag_graph(_pydag_raw())
        assert spec.pipeline_id == "demo"
        assert spec.schema_version == 1
        assert spec.entry == "step_1"

    def test_input_vars_become_the_declared_params_schema(self) -> None:
        spec = convert_pydag_graph(_pydag_raw())
        assert spec.params_schema == {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}, "region": {"type": "string"}},
            "required": ["ticket_id", "region"],
            "additionalProperties": False,
        }

    def test_empty_input_vars_declare_the_empty_params_face(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["pipeline"]["input_vars"] = []
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Static analysis prompt."
        spec = convert_pydag_graph(raw)
        assert spec.params_schema == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    def test_canvas_metadata_is_ignored(self) -> None:
        """The full-metadata graph and a stripped variant convert to the same spec."""
        raw = copy.deepcopy(_pydag_raw())
        stripped = copy.deepcopy(_pydag_raw())
        del stripped["viewport"]
        del stripped["nodes"][0]["position"]
        del stripped["nodes"][0]["data"]["ui"]
        del stripped["nodes"][0]["data"]["diagnostics"]
        del stripped["nodes"][0]["data"]["provenance"]
        del stripped["nodes"][0]["data"]["param_resolution"]
        del stripped["edges"][0]["id"]
        assert compute_spec_hash(convert_pydag_graph(raw)) == compute_spec_hash(convert_pydag_graph(stripped))

    def test_conversion_is_deterministic(self) -> None:
        first = convert_pydag_graph(_pydag_raw())
        second = convert_pydag_graph(_pydag_raw())
        assert compute_spec_hash(first) == compute_spec_hash(second)


class TestNodeMapping:
    def test_tool_step_maps_to_a_tool_node(self) -> None:
        spec = convert_pydag_graph(_pydag_raw())
        assert spec.nodes[0].model_dump(mode="json") == {
            "node_id": "step_1",
            "node_type": "tool",
            "model": None,
            "prompt_template": None,
            "command": None,
            "tool_ref": "demo_fetch_ticket",
            "workflow_ref": None,
            "depth": None,
            "params": {"ticket_id": "T-1", "verbose": True},
            "condition": None,
            "extract": None,
            "output_schema": None,
            "fallback": None,
        }

    def test_tool_literal_params_pass_through(self) -> None:
        """Literal params never block the import (the pass-through rule) and land on node.params."""
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"] = {
            "ticket_id": "T-1",
            "verbose": True,
            "weight": 3,
            "tags": ["a", "b"],
            "nested": {"k": "v"},
            "none": None,
        }
        spec = convert_pydag_graph(raw)
        assert spec.nodes[0].tool_ref == "demo_fetch_ticket"
        assert spec.nodes[0].params == {
            "ticket_id": "T-1",
            "verbose": True,
            "weight": 3,
            "tags": ["a", "b"],
            "nested": {"k": "v"},
            "none": None,
        }

    def test_llm_step_maps_to_an_llm_call_node(self) -> None:
        spec = convert_pydag_graph(_pydag_raw())
        node = spec.nodes[1]
        assert node.node_type == "llm_call"
        assert node.model == PYDAG_LLM_MODEL_TIER
        assert node.prompt_template == "Summarize ticket {ticket_id} in region {region}."

    def test_prompt_bare_and_prefixed_input_refs_both_convert(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "{{ticket_id}} / {{inputs.region}}"
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].prompt_template == "{ticket_id} / {region}"

    def test_prompt_without_refs_converts_unchanged(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Static analysis prompt."
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].prompt_template == "Static analysis prompt."

    def test_prompt_literal_braces_are_escaped_for_str_format(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = (
            'Reply with {"pass": true} for {{ticket_id}}.'
        )
        spec = convert_pydag_graph(raw)
        template = spec.nodes[1].prompt_template
        assert template == 'Reply with {{"pass": true}} for {ticket_id}.'
        assert template.format(ticket_id="T-42") == 'Reply with {"pass": true} for T-42.'

    def test_edges_convert_to_spec_edges(self) -> None:
        spec = convert_pydag_graph(_pydag_raw())
        assert [(edge.from_node, edge.to_node) for edge in spec.edges] == [("step_1", "step_2")]

    def test_chain_of_three_converts_with_the_computed_entry(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"].append(_node("step_3", _step_body("step_3", depends_on=["step_2"])))
        raw["edges"].append(_dep_edge("step_2", "step_3"))
        spec = convert_pydag_graph(raw)
        assert spec.entry == "step_1"
        assert len(spec.nodes) == 3


class TestMinifiedForm:
    def test_minified_single_line_fixture_matches_the_pretty_form(self) -> None:
        assert json.loads(_MINIFIED_GRAPH) == _pydag_raw()

    def test_minified_graph_converts_to_the_identical_spec_hash(self) -> None:
        minified = convert_pydag_graph(json.loads(_MINIFIED_GRAPH))
        pretty = convert_pydag_graph(_pydag_raw())
        assert minified == pretty
        assert compute_spec_hash(minified) == compute_spec_hash(pretty)


class TestStructuralDiagnostics:
    def test_wrong_schema_version_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["schema_version"] = "pydag-editable-graph-v2"
        diagnostic = _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="schema_version")
        assert diagnostic.node_id is None

    def test_missing_pipeline_section_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        del raw["pipeline"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="pipeline")

    def test_empty_pipeline_name_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["pipeline"]["name"] = ""
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="pipeline.name")

    def test_non_array_input_vars_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["pipeline"]["input_vars"] = "ticket_id"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="pipeline.input_vars")

    @pytest.mark.parametrize("bad", ["ticket.id", "ticket id", "", 7, None])
    def test_invalid_input_var_names_are_rejected(self, bad: Any) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["pipeline"]["input_vars"] = [bad]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="pipeline.input_vars")

    def test_duplicate_input_vars_are_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["pipeline"]["input_vars"] = ["ticket_id", "ticket_id"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="pipeline.input_vars")

    def test_empty_nodes_array_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"] = []
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="nodes")

    def test_non_object_node_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0] = "step_1"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="nodes")

    def test_missing_node_id_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        del raw["nodes"][0]["id"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="nodes[].id")

    def test_wrong_node_type_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["type"] = "pydagNote"
        diagnostic = _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="nodes[].type")
        assert diagnostic.node_id == "step_1"

    def test_missing_step_payload_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        del raw["nodes"][0]["data"]["step"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step")

    def test_step_id_mismatching_node_id_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["id"] = "other"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.id")

    def test_missing_tool_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        del raw["nodes"][0]["data"]["step"]["tool"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.tool")

    def test_non_array_depends_on_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["depends_on"] = "step_2"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.depends_on")

    def test_depends_on_edge_mismatch_is_rejected(self) -> None:
        """depends_on must match the dependency edges exactly, per node."""
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["depends_on"] = []
        diagnostic = _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.depends_on")
        assert diagnostic.node_id == "step_2"

    def test_depends_on_without_the_edge_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"] = []
        diagnostic = _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.depends_on")
        assert diagnostic.node_id == "step_2"

    def test_multiple_roots_are_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"].append(_node("step_3", _step_body("step_3")))
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="entry")

    def test_full_cycle_graph_has_no_entry_candidate(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["depends_on"] = ["step_2"]
        raw["edges"].append(_dep_edge("step_2", "step_1"))
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="entry")

    def test_non_array_edges_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"] = {"source": "step_1"}
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges")

    def test_non_object_edge_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"][0] = "step_1->step_2"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges[0]")

    def test_unknown_edge_type_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"][0]["type"] = "pydagFlow"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges[0].type")

    def test_wrong_edge_kind_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"][0]["data"]["kind"] = "controls"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges[0].data.kind")

    def test_edge_unknown_endpoint_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"][0]["target"] = "ghost"
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges[0]")

    def test_duplicate_edge_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["edges"].append(_dep_edge("step_1", "step_2"))
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="edges[1]")

    def test_two_llm_call_params_on_one_step_are_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["extra"] = {"llm_call": {"prompt": "Another."}}
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.params.extra.llm_call")

    def test_non_object_llm_call_payload_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"] = {"llm_call": "prompt"}
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.params.summary.llm_call")


class TestUnsupportedFieldDiagnostics:
    @pytest.mark.parametrize(
        "field",
        ["transform", "output_format", "instance_binding"],
    )
    def test_step_feature_fields_are_rejected_when_set(self, field: str) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"][field] = {"any": "value"}
        diagnostic = _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field=f"data.step.{field}")
        assert diagnostic.node_id == "step_1"

    def test_fallback_tools_are_rejected_when_set(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["fallback_tools"] = [{"tool": "demo_other", "params": {}}]
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.fallback_tools")

    def test_unknown_step_key_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["for_each"] = {"source": "step_1.items", "item_var": "item"}
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.for_each")

    @pytest.mark.parametrize("mode", ["merge", "append", 1])
    def test_non_default_output_mode_is_rejected(self, mode: Any) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["output_mode"] = mode
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.output_mode")

    def test_llm_schema_converts_to_output_schema(self) -> None:
        """v2 (T-C1-084): the structured-output schema lands on the llm node."""
        raw = copy.deepcopy(_pydag_raw())
        schema = {"type": "object", "properties": {"pass": {"type": "boolean"}}, "required": ["pass"]}
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["schema"] = schema
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].output_schema == schema
        assert spec.nodes[1].fallback is None

    def test_llm_fallback_converts_with_a_schema(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        payload = raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]
        payload["schema"] = {"type": "object", "properties": {"pass": {"type": "boolean"}}, "required": ["pass"]}
        payload["fallback"] = {"pass": True}
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].fallback == {"pass": True}

    def test_llm_fallback_without_a_schema_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["fallback"] = {"pass": True}
        diagnostic = _first_diagnostic(
            raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.params.summary.llm_call.fallback"
        )
        assert diagnostic.node_id == "step_2"

    def test_a_non_object_llm_schema_is_a_structural_rejection(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["schema"] = ["not", "an", "object"]
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.params.summary.llm_call.schema")

    @pytest.mark.parametrize("prompt", [{"template": {"format": "f", "bindings": {}}}, "", 42])
    def test_non_string_llm_prompt_is_rejected(self, prompt: Any) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = prompt
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.params.summary.llm_call.prompt")

    def test_llm_call_on_a_non_compute_tool_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["smart"] = {"llm_call": {"prompt": "Be smart."}}
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.params.smart.llm_call")

    def test_context_bundle_param_mode_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["ctx"] = {"context_bundle": {"include": ["inputs"]}}
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.params.ctx")

    def test_template_param_mode_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["built"] = {"template": {"format": "f", "bindings": {}}}
        _first_diagnostic(raw, code=PYDAG_UNSUPPORTED_FIELD, field="data.step.params.built")


class TestRefDiagnostics:
    def test_step_output_param_ref_converts(self) -> None:
        """v2 (T-C1-084): a step-rooted ref binds onto the node's params."""
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["ticket"] = {"ref": "step_1.ticket"}
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].params == {"ticket": {"ref": "step_1.ticket"}}

    def test_inputs_param_ref_converts_for_a_declared_var(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["ticket_id"] = {"ref": "inputs.ticket_id"}
        spec = convert_pydag_graph(raw)
        assert spec.nodes[0].params == {
            "ticket_id": {"ref": "inputs.ticket_id"},
            "verbose": True,
        }

    def test_params_ref_to_an_undeclared_input_var_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["ticket_id"] = {"ref": "inputs.ghost"}
        diagnostic = _first_diagnostic(raw, code=PYDAG_REF_UNSUPPORTED, field="data.step.params.ticket_id")
        assert diagnostic.node_id == "step_1"

    def test_params_ref_with_an_unknown_root_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["ticket_id"] = {"ref": "ghost.field"}
        _first_diagnostic(raw, code=PYDAG_REF_UNSUPPORTED, field="data.step.params.ticket_id")

    def test_a_malformed_ref_object_is_a_structural_rejection(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["ticket_id"] = {"ref": "step_1.ticket", "mode": "x"}
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.params.ticket_id")

    def test_prompt_step_output_ref_converts_to_an_index_chain(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Summarize {{step_1.ticket}}."
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].prompt_template == "Summarize {step_1[ticket]}."

    def test_prompt_root_only_step_ref_converts_to_the_whole_result(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "All: {{step_1}}"
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].prompt_template == "All: {step_1}"

    def test_prompt_unknown_var_ref_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Summarize {{ghost}}."
        _first_diagnostic(raw, code=PYDAG_REF_UNSUPPORTED, field="data.step.params.summary.llm_call.prompt")

    def test_prompt_inputs_prefixed_unknown_var_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Summarize {{inputs.ghost}}."
        _first_diagnostic(raw, code=PYDAG_REF_UNSUPPORTED, field="data.step.params.summary.llm_call.prompt")

    def test_prompt_nested_input_field_ref_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = (
            "Summarize {{inputs.ticket_id.meta}}."
        )
        _first_diagnostic(raw, code=PYDAG_REF_UNSUPPORTED, field="data.step.params.summary.llm_call.prompt")

    def test_param_ref_edges_are_ignored_canvas_metadata(self) -> None:
        """v2: the pydagReference canvas mirror is ignored -- the refs bind on the nodes."""
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"]["llm_call"]["prompt"] = "Summarize {{step_1.ticket}}."
        with_edge = copy.deepcopy(raw)
        with_edge["edges"].append(
            {
                "id": "step_1->step_2:param_ref:step_1.ticket",
                "source": "step_1",
                "target": "step_2",
                "type": "pydagReference",
                "data": {"kind": "param_ref", "ref": "step_1.ticket"},
            }
        )
        assert convert_pydag_graph(with_edge) == convert_pydag_graph(raw)


class TestV2StepConversions:
    """The v2 step-field conversions (T-C1-084): condition, extract, params carrying."""

    def test_a_valid_condition_converts(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["condition"] = "step_1.output is ticket"
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].condition == "step_1.output is ticket"
        assert spec.nodes[0].condition is None

    def test_a_blank_condition_is_a_structural_rejection(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["condition"] = "   "
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.condition")

    def test_a_non_string_condition_is_a_structural_rejection(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["condition"] = 7
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field="data.step.condition")

    def test_extract_converts_paths_and_fallback_chains(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["extract"] = {
            "sop_data": ["result.data", "result"],
            "answer": "result.answer",
        }
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].extract == {"sop_data": ["result.data", "result"], "answer": "result.answer"}

    def test_an_empty_extract_object_converts_as_absent(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["extract"] = {}
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].extract is None

    @pytest.mark.parametrize(
        ("bad", "field"),
        [
            ("not-an-object", "data.step.extract"),
            ({"a": ""}, "data.step.extract.a"),
            ({"a": []}, "data.step.extract.a"),
            ({"a": ["ok", ""]}, "data.step.extract.a"),
            ({"a": 3}, "data.step.extract.a"),
            ({"a": "x..y"}, "data.step.extract.a"),
        ],
    )
    def test_malformed_extract_is_a_structural_rejection(self, bad: Any, field: str) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["extract"] = bad
        _first_diagnostic(raw, code=PYDAG_STRUCTURAL, field=field)

    def test_the_llm_payload_key_never_lands_on_params(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        spec = convert_pydag_graph(raw)
        assert spec.nodes[1].params is None  # the only param was the llm payload

    def test_params_refs_bind_against_all_declared_steps(self) -> None:
        """The ref pre-pass: a ref may name a step declared later in the file."""
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["params"]["next"] = {"ref": "step_2.summary"}
        spec = convert_pydag_graph(raw)
        assert spec.nodes[0].params == {
            "ticket_id": "T-1",
            "verbose": True,
            "next": {"ref": "step_2.summary"},
        }


class TestUnknownTool:
    def test_builtin_compute_without_an_llm_prompt_is_rejected(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"] = {"left": 1, "right": 2}
        diagnostic = _first_diagnostic(raw, code=PYDAG_UNKNOWN_TOOL, field="data.step.tool")
        assert diagnostic.node_id == "step_2"


class TestErrorFace:
    def test_the_error_carries_every_diagnostic(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["transform"] = {"any": "value"}
        raw["nodes"][1]["data"]["step"]["output_format"] = {"any": "value"}
        error = _rejected(raw)
        assert [diagnostic.code for diagnostic in error.diagnostics] == [
            PYDAG_UNSUPPORTED_FIELD,
            PYDAG_UNSUPPORTED_FIELD,
        ]
        assert [diagnostic.node_id for diagnostic in error.diagnostics] == ["step_1", "step_2"]
        assert [diagnostic.field for diagnostic in error.diagnostics] == [
            "data.step.transform",
            "data.step.output_format",
        ]

    def test_the_error_message_names_the_first_diagnostic(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][1]["data"]["step"]["params"]["summary"] = {"ref": "inputs.ghost"}
        error = _rejected(raw)
        # Two findings: the unbindable ref itself and the now prompt-less
        # compute step (the llm_call param was the ref); the message names the first.
        assert "pydag graph rejected with 2 diagnostic(s)" in str(error)
        assert "[PYDAG_REF_UNSUPPORTED]" in str(error)

    def test_the_error_is_a_value_error(self) -> None:
        raw = copy.deepcopy(_pydag_raw())
        raw["nodes"][0]["data"]["step"]["tool"] = ""
        with pytest.raises(ValueError):
            convert_pydag_graph(raw)


class TestSeamRouter:
    def test_the_pydag_marker_routes_through_the_converter(self) -> None:
        spec = parse_pipeline_spec(_pydag_raw())
        assert spec.pipeline_id == "demo"
        assert spec.nodes[1].node_type == "llm_call"

    def test_plain_v1_raws_keep_the_pydantic_path(self) -> None:
        raw: JsonObject = {
            "schema_version": 1,
            "pipeline_id": "digest",
            "entry": "collect",
            "nodes": [
                {"node_id": "collect", "node_type": "tool", "tool_ref": "fs.read_text"},
                {"node_id": "summarize", "node_type": "llm_call", "model": "small", "prompt_template": "p"},
            ],
            "edges": [{"from_node": "collect", "to_node": "summarize"}],
        }
        spec = parse_pipeline_spec(raw)
        assert isinstance(spec, PipelineSpec)
        assert spec.pipeline_id == "digest"

    def test_unknown_string_schema_version_stays_a_structural_rejection(self) -> None:
        raw: dict[str, Any] = {"schema_version": "pydag-editable-graph-v9", "nodes": []}
        with pytest.raises(ValidationError):
            parse_pipeline_spec(raw)
