"""The pipeline-spec contract + the semantic-gate rejection face (T-C1-077, P3-S2).

CORE-CONTRACTS §1 L22 discipline: every slice's real types are locked by
positive/negative JSON fixtures plus behavior tests. The trio
``fixtures/pipelines_*.json`` pins the *structural* wire face (the committed
pipelines.json artifact + ``test_schema_lock.py``'s lock-diff class); this
module pins:

* the structural rules the fixtures cannot economically enumerate (the closed
  node-type set, the §1 bool-for-int guard on ``schema_version``/``depth``, the
  ``extra="forbid"`` envelope, the non-empty nodes list);
* the *semantic* gate layer -- :func:`opencode.orchestrator.engine.validate_pipeline_spec`:
  the v1 version gate, topology closure (duplicate ids, unknown edge references, the unique entry,
  orphans) and the per-type constraints (the llm_call tier/template pair, the
  tool command/tool_ref exactly-one rule, the subworkflow depth cap), plus the
  S2 execution face: the three recognized-but-unimplemented node types are
  rejected with the explicit S3/S5a delivery message;
* the §5.1 spec hash: byte-stability over equal specs and sensitivity to any
  semantic change (the canonical-JSON recipe, CORE-CONTRACTS §6.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.pipelines import (
    MAX_SUBWORKFLOW_DEPTH,
    PIPELINE_NODE_TYPES,
    PipelineNode,
    PipelineSpec,
    compute_spec_hash,
)
from opencode.orchestrator.engine import PipelineSpecRejected, validate_pipeline_spec

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _spec_raw(**overrides: Any) -> dict[str, Any]:
    """One minimal valid spec (tool -> llm_call chain); overrides splice in violations."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_id": "digest",
        "entry": "collect",
        "nodes": [
            {"node_id": "collect", "node_type": "tool", "tool_ref": "fs.read_text"},
            {
                "node_id": "summarize",
                "node_type": "llm_call",
                "model": "small",
                "prompt_template": "Summarize the collected input.",
            },
        ],
        "edges": [{"from_node": "collect", "to_node": "summarize"}],
    }
    raw.update(overrides)
    return raw


def _validated(**overrides: Any) -> PipelineSpec:
    spec = PipelineSpec.model_validate(_spec_raw(**overrides))
    validate_pipeline_spec(spec)
    return spec


class TestStructuralLayer:
    """Layer 1 (pydantic): the wire shape rejects unknowns and bool-for-int."""

    def test_valid_fixture_loads_and_passes_the_semantic_gate(self) -> None:
        raw = json.loads((FIXTURES_DIR / "pipelines_valid.json").read_text(encoding="utf-8"))
        spec = PipelineSpec.model_validate(raw)
        validate_pipeline_spec(spec)
        assert spec.pipeline_id == "digest"
        assert spec.nodes[1].node_type == "llm_call"
        assert spec.model_dump(mode="json")["entry"] == "collect"

    def test_the_node_type_set_is_closed(self) -> None:
        with pytest.raises(ValidationError, match="node_type"):
            PipelineNode.model_validate({"node_id": "x", "node_type": "shell"})

    def test_unknown_node_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            PipelineNode.model_validate({"node_id": "x", "node_type": "tool", "weight": 3})

    def test_unknown_spec_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            PipelineSpec.model_validate({**_spec_raw(), "budget": {}})

    def test_bool_schema_version_is_rejected_before_coercion(self) -> None:
        with pytest.raises(ValidationError, match="schema_version"):
            PipelineSpec.model_validate(_spec_raw(schema_version=True))

    def test_bool_node_depth_is_rejected_before_coercion(self) -> None:
        with pytest.raises(ValidationError, match="depth"):
            PipelineNode.model_validate(
                {"node_id": "nested", "node_type": "subworkflow", "workflow_ref": "x", "depth": True}
            )

    def test_zero_schema_version_and_empty_nodes_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PipelineSpec.model_validate(_spec_raw(schema_version=0))
        with pytest.raises(ValidationError, match="nodes"):
            PipelineSpec.model_validate(_spec_raw(nodes=[]))

    def test_edges_default_to_a_private_empty_list(self) -> None:
        raw = {
            "schema_version": 1,
            "pipeline_id": "single",
            "entry": "only",
            "nodes": [{"node_id": "only", "node_type": "llm_call", "model": "small", "prompt_template": "p"}],
        }
        first = PipelineSpec.model_validate(raw)
        second = PipelineSpec.model_validate(raw)
        assert first.edges == []
        assert first.edges is not second.edges  # factory default: no shared container


class TestSemanticLayer:
    """Layer 2: topology + type constraints + the S2 unimplemented-node face."""

    def test_minimal_llm_call_only_chain_passes(self) -> None:
        spec = _validated()
        assert [node.node_id for node in spec.nodes] == ["collect", "summarize"]

    def test_tool_command_variant_passes(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "run",
                "entry": "sh",
                "nodes": [{"node_id": "sh", "node_type": "tool", "command": ["git", "status"]}],
            }
        )
        validate_pipeline_spec(spec)

    @pytest.mark.parametrize(
        "node_type",
        ["opencode_agent", "human_gate", "subworkflow"],
    )
    def test_unimplemented_node_types_are_rejected_with_the_delivery_note(self, node_type: str) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "later",
                "entry": "n",
                "nodes": [{"node_id": "n", "node_type": node_type}],
            }
        )
        with pytest.raises(PipelineSpecRejected) as rejection:
            validate_pipeline_spec(spec)
        error: PipelineSpecRejected = rejection.value
        assert "unimplemented node type" in str(error)
        assert "S2 executes llm_call/tool nodes only" in str(error)
        assert "S3" in str(error) and "S5a" in str(error)
        assert error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert error.details == {"node_id": "n", "node_type": node_type}
        wire = error.to_core_error()
        assert wire.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert wire.retryable is False
        assert wire.details == {"node_id": "n", "node_type": node_type}

    def test_all_five_names_stay_recognized_structurally(self) -> None:
        for node_type in PIPELINE_NODE_TYPES:
            PipelineNode.model_validate({"node_id": "n", "node_type": node_type})

    def test_llm_call_without_a_model_tier_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "n",
                "nodes": [{"node_id": "n", "node_type": "llm_call", "prompt_template": "p"}],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="model tier"):
            validate_pipeline_spec(spec)

    def test_tool_without_command_or_ref_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {"schema_version": 1, "pipeline_id": "p", "entry": "n", "nodes": [{"node_id": "n", "node_type": "tool"}]}
        )
        with pytest.raises(PipelineSpecRejected, match=r"command.*tool_ref"):
            validate_pipeline_spec(spec)

    def test_tool_with_both_command_and_ref_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "n",
                "nodes": [
                    {
                        "node_id": "n",
                        "node_type": "tool",
                        "command": ["ls"],
                        "tool_ref": "fs.read_text",
                    }
                ],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="exactly one"):
            validate_pipeline_spec(spec)

    def test_depth_over_the_cap_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "n",
                "nodes": [
                    {
                        "node_id": "n",
                        "node_type": "subworkflow",
                        "workflow_ref": "other",
                        "depth": MAX_SUBWORKFLOW_DEPTH + 1,
                    }
                ],
            }
        )
        # The depth cap is checked even though the node is also unimplemented:
        # the depth guard is the spec-side second line (PHASE3 §5.2) and stays
        # operative for whichever type starts carrying depth fields.
        with pytest.raises(PipelineSpecRejected, match="hard cap"):
            validate_pipeline_spec(spec)

    def test_duplicate_node_ids_are_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "n",
                "nodes": [
                    {"node_id": "n", "node_type": "llm_call", "model": "s", "prompt_template": "p"},
                    {"node_id": "n", "node_type": "tool", "tool_ref": "x"},
                ],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="duplicate node id 'n'"):
            validate_pipeline_spec(spec)

    def test_edge_referencing_an_unknown_node_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "a",
                "nodes": [{"node_id": "a", "node_type": "llm_call", "model": "s", "prompt_template": "p"}],
                "edges": [{"from_node": "a", "to_node": "ghost"}],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="unknown node 'ghost'"):
            validate_pipeline_spec(spec)

    def test_schema_version_other_than_v1_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(_spec_raw(schema_version=2))  # parses: int >= 1
        with pytest.raises(PipelineSpecRejected, match="reads v1 specs only") as rejection:
            validate_pipeline_spec(spec)
        assert rejection.value.code is CoreErrorCode.UNSUPPORTED_CAPABILITY

    def test_undeclared_entry_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "ghost",
                "nodes": [{"node_id": "n", "node_type": "llm_call", "model": "s", "prompt_template": "p"}],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="entry node 'ghost' is not declared"):
            validate_pipeline_spec(spec)

    def test_entry_with_an_incoming_edge_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "b",
                "nodes": [
                    {"node_id": "a", "node_type": "llm_call", "model": "s", "prompt_template": "p"},
                    {"node_id": "b", "node_type": "llm_call", "model": "s", "prompt_template": "q"},
                ],
                "edges": [{"from_node": "a", "to_node": "b"}],
            }
        )
        with pytest.raises(PipelineSpecRejected, match="entry node 'b' must be unique"):
            validate_pipeline_spec(spec)

    def test_orphan_nodes_are_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "a",
                "nodes": [
                    {"node_id": "a", "node_type": "llm_call", "model": "s", "prompt_template": "p"},
                    {"node_id": "island", "node_type": "llm_call", "model": "s", "prompt_template": "q"},
                ],
            }
        )
        with pytest.raises(PipelineSpecRejected, match=r"orphan nodes.*\['island'\]"):
            validate_pipeline_spec(spec)

    def test_rejections_reuse_the_closed_error_code_registry(self) -> None:
        spec = PipelineSpec.model_validate(
            {
                "schema_version": 1,
                "pipeline_id": "p",
                "entry": "ghost",
                "nodes": [{"node_id": "n", "node_type": "llm_call", "model": "s", "prompt_template": "p"}],
            }
        )
        with pytest.raises(PipelineSpecRejected) as rejection:
            validate_pipeline_spec(spec)
        assert rejection.value.code is CoreErrorCode.VALIDATION_ERROR


class TestSpecHash:
    """The §5.1 hash lock: canonical, byte-stable, change-sensitive."""

    def test_equal_specs_hash_equal(self) -> None:
        assert compute_spec_hash(_validated()) == compute_spec_hash(_validated())

    def test_rejected_specs_still_hash_deterministically(self) -> None:
        """The hash is a load-time fact: it does not need the semantic gate to pass."""
        left = PipelineSpec.model_validate(_spec_raw(entry="ghost"))
        right = PipelineSpec.model_validate(_spec_raw(entry="ghost"))
        assert compute_spec_hash(left) == compute_spec_hash(right)

    def test_any_semantic_change_moves_the_hash(self) -> None:
        base = compute_spec_hash(_validated())
        changed_id = compute_spec_hash(_validated(pipeline_id="digest2"))
        changed_tier = compute_spec_hash(
            _validated(
                nodes=[
                    {"node_id": "collect", "node_type": "tool", "tool_ref": "fs.read_text"},
                    {
                        "node_id": "summarize",
                        "node_type": "llm_call",
                        "model": "large",
                        "prompt_template": "Summarize the collected input.",
                    },
                ]
            )
        )
        assert len({base, changed_id, changed_tier}) == 3

    def test_hash_is_the_sha256_of_the_canonical_serialization(self) -> None:
        """The recipe (T-C1-084): canonical JSON of the dump, None v2 fields stripped."""
        from hashlib import sha256

        from opencode.contracts.events import canonical_json

        spec = _validated()
        data = spec.model_dump(mode="json")
        for node in data["nodes"]:
            for field_name in ("params", "condition", "extract", "output_schema", "fallback"):
                if node.get(field_name) is None:
                    node.pop(field_name, None)
        expected = sha256(canonical_json(data).encode("utf-8")).hexdigest()
        assert compute_spec_hash(spec) == expected
        assert len(compute_spec_hash(spec)) == 64

    def test_the_v1_hash_is_pinned_byte_stable_across_the_v2_extension(self) -> None:
        """The T-C1-084 stability pin: a pure-v1 spec hashes exactly what the
        pre-v2 engine hashed -- the five new None-valued fields never enter
        the canonical bytes, so v1 bindings survive the extension intact."""
        pinned = "a881d3f4c06e24878ba25cbad28c1892a4222f54039603ee119db402c6928772"
        assert compute_spec_hash(_validated()) == pinned
        raw = json.loads((FIXTURES_DIR / "pipelines_valid.json").read_text(encoding="utf-8"))
        assert compute_spec_hash(PipelineSpec.model_validate(raw)) == pinned

    def test_set_v2_fields_participate_in_the_hash(self) -> None:
        base = compute_spec_hash(_validated())
        conditioned = compute_spec_hash(
            _validated(
                nodes=[
                    {
                        "node_id": "collect",
                        "node_type": "tool",
                        "tool_ref": "fs.read_text",
                        "condition": "inputs.go is true",
                    },
                    {
                        "node_id": "summarize",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "Summarize the collected input.",
                    },
                ]
            )
        )
        assert conditioned != base
        assert len(conditioned) == 64


class TestV2NodeFields:
    """Layer 1 (pydantic) for the v2 additive node fields (T-C1-084)."""

    def test_params_literals_and_refs_parse(self) -> None:
        node = PipelineNode.model_validate(
            {
                "node_id": "n",
                "node_type": "tool",
                "tool_ref": "t",
                "params": {
                    "literal": 3,
                    "nested": {"k": "v"},
                    "from_inputs": {"ref": "inputs.x"},
                    "from_step": {"ref": "step_1.field.deep"},
                },
            }
        )
        assert node.params is not None
        assert node.params["literal"] == 3
        assert node.params["from_inputs"] == {"ref": "inputs.x"}
        assert node.params["from_step"] == {"ref": "step_1.field.deep"}

    @pytest.mark.parametrize(
        "bad",
        [
            {"ref": 3},
            {"ref": ""},
            {"ref": "a..b"},
            {"ref": ".x"},
            {"ref": "x."},
            {"ref": "ok", "extra": 1},
        ],
    )
    def test_malformed_ref_objects_are_rejected(self, bad: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            PipelineNode.model_validate({"node_id": "n", "node_type": "tool", "tool_ref": "t", "params": {"p": bad}})

    def test_a_dict_with_a_ref_key_among_others_is_rejected_never_guessed(self) -> None:
        with pytest.raises(ValidationError, match="single 'ref' key"):
            PipelineNode.model_validate(
                {
                    "node_id": "n",
                    "node_type": "tool",
                    "tool_ref": "t",
                    "params": {"p": {"ref": "inputs.x", "mode": "y"}},
                }
            )

    def test_extract_paths_and_fallback_chains_parse(self) -> None:
        node = PipelineNode.model_validate(
            {
                "node_id": "n",
                "node_type": "tool",
                "tool_ref": "t",
                "extract": {"a": "result.x", "b": ["result.y", "fallback"]},
            }
        )
        assert node.extract == {"a": "result.x", "b": ["result.y", "fallback"]}

    @pytest.mark.parametrize(
        "bad",
        [
            {},
            {"a": ""},
            {"a": []},
            {"a": ["ok", ""]},
            {"a": 3},
            {"a": ["x", 3]},
            {"a": "x..y"},
        ],
    )
    def test_malformed_extract_is_rejected(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            PipelineNode.model_validate({"node_id": "n", "node_type": "tool", "tool_ref": "t", "extract": bad})

    def test_an_empty_condition_is_rejected_and_whitespace_parses(self) -> None:
        with pytest.raises(ValidationError):
            PipelineNode.model_validate({"node_id": "n", "node_type": "tool", "tool_ref": "t", "condition": ""})
        node = PipelineNode.model_validate({"node_id": "n", "node_type": "tool", "tool_ref": "t", "condition": "  "})
        # Whitespace parses; the ported evaluator reads it as "no condition".
        assert node.condition == "  "

    def test_output_schema_and_fallback_parse(self) -> None:
        node = PipelineNode.model_validate(
            {
                "node_id": "n",
                "node_type": "llm_call",
                "model": "small",
                "prompt_template": "p",
                "output_schema": {"type": "object", "properties": {}},
                "fallback": {"a": 1},
            }
        )
        assert node.output_schema == {"type": "object", "properties": {}}
        assert node.fallback == {"a": 1}


def _v2_raw(**overrides: Any) -> dict[str, Any]:
    """One minimal valid v2 carrier spec (a single llm node); overrides splice."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_id": "p",
        "entry": "a",
        "nodes": [{"node_id": "a", "node_type": "llm_call", "model": "small", "prompt_template": "p"}],
    }
    raw.update(overrides)
    return raw


class TestV2SemanticGate:
    """Layer 2 for the v2 faces: reserved roots, condition syntax, ref closure, llm schema rules."""

    def test_a_node_named_inputs_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(entry="inputs", nodes=[{"node_id": "inputs", "node_type": "tool", "tool_ref": "t"}])
        )
        with pytest.raises(PipelineSpecRejected, match="reserved"):
            validate_pipeline_spec(spec)

    def test_a_node_id_colliding_with_a_declared_param_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(params_schema={"type": "object", "properties": {"a": {"type": "string"}}})
        )
        with pytest.raises(PipelineSpecRejected, match="collide with declared params"):
            validate_pipeline_spec(spec)

    def test_bad_condition_syntax_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "condition": "a.ready === true",
                    }
                ]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="not a supported condition expression"):
            validate_pipeline_spec(spec)

    def test_valid_condition_syntax_passes_the_gate(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "condition": "inputs.go is true",
                    }
                ]
            )
        )
        validate_pipeline_spec(spec)

    def test_step_rooted_params_refs_pass_the_gate(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {"node_id": "a", "node_type": "tool", "tool_ref": "t", "params": {"x": {"ref": "b.field"}}},
                    {"node_id": "b", "node_type": "llm_call", "model": "small", "prompt_template": "p"},
                ],
                edges=[{"from_node": "a", "to_node": "b"}],
            )
        )
        validate_pipeline_spec(spec)

    def test_unknown_params_ref_root_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(nodes=[{"node_id": "a", "node_type": "tool", "tool_ref": "t", "params": {"x": {"ref": "ghost.x"}}}])
        )
        with pytest.raises(PipelineSpecRejected, match="has unknown root 'ghost'"):
            validate_pipeline_spec(spec)

    def test_inputs_params_refs_without_a_schema_are_statically_unbindable(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[{"node_id": "a", "node_type": "tool", "tool_ref": "t", "params": {"x": {"ref": "inputs.x"}}}]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="statically unbindable"):
            validate_pipeline_spec(spec)

    def test_inputs_params_refs_must_name_a_declared_property(self) -> None:
        schema: dict[str, Any] = {"type": "object", "properties": {"known": {"type": "string"}}}
        bad = PipelineSpec.model_validate(
            _v2_raw(
                params_schema=schema,
                nodes=[
                    {"node_id": "a", "node_type": "tool", "tool_ref": "t", "params": {"x": {"ref": "inputs.unknown"}}}
                ],
            )
        )
        with pytest.raises(PipelineSpecRejected, match="does not name a declared params schema property"):
            validate_pipeline_spec(bad)
        good = PipelineSpec.model_validate(
            _v2_raw(
                params_schema=schema,
                nodes=[
                    {"node_id": "a", "node_type": "tool", "tool_ref": "t", "params": {"x": {"ref": "inputs.known"}}}
                ],
            )
        )
        validate_pipeline_spec(good)

    def test_output_schema_outside_the_subset_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "output_schema": {"type": "object", "pattern": "x"},
                    }
                ]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="outside the supported schema subset"):
            validate_pipeline_spec(spec)

    def test_non_object_output_schema_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "output_schema": {"type": "string"},
                    }
                ]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="outside the supported schema subset"):
            validate_pipeline_spec(spec)

    def test_output_schema_on_a_non_llm_node_is_rejected(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(nodes=[{"node_id": "a", "node_type": "tool", "tool_ref": "t", "output_schema": {"type": "object"}}])
        )
        with pytest.raises(PipelineSpecRejected, match="not an llm_call node"):
            validate_pipeline_spec(spec)

    def test_fallback_requires_an_output_schema(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "fallback": {"a": 1},
                    }
                ]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="without an output_schema"):
            validate_pipeline_spec(spec)

    def test_fallback_must_satisfy_the_schema(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "output_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                        "fallback": {"a": 3},
                    }
                ]
            )
        )
        with pytest.raises(PipelineSpecRejected, match="fallback does not satisfy its output_schema"):
            validate_pipeline_spec(spec)

    def test_a_schema_fallback_pair_passes_the_gate(self) -> None:
        spec = PipelineSpec.model_validate(
            _v2_raw(
                nodes=[
                    {
                        "node_id": "a",
                        "node_type": "llm_call",
                        "model": "small",
                        "prompt_template": "p",
                        "output_schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                        "fallback": {"a": "degraded"},
                    }
                ]
            )
        )
        validate_pipeline_spec(spec)
