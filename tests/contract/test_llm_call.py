"""Unit tests for the real llm_call node body (P4-S1, T-C1-082).

The provider seam is faked here (a recording async generator replaying
scripted ``ProviderChunk`` sequences -- the S1 fake-executor discipline at
the provider boundary); the real HTTP face over the ported MAAS adapter is
pinned by ``tests/integration/test_llm_call_e2e.py``. Everything else is
real: the tier resolution, the ``str.format`` prompt rendering with its
structured missing-key face, the stream aggregation (terminal error chunk
-> its own §8 code, no terminal chunk -> fail closed, tool_calls finish ->
rejected), the honest fail-closed tool body, the advisory-cursor path
derivation, and the host-only snapshot pin (the config snapshot face gains
no provider field).

v2 (T-C1-084) additions pinned here: the merged render context (submitted
params plus the non-skipped node results, index chains stepping into real
JSON values) and the structured output face (the ``response_format``
request, the fence-tolerant parse, the fallback degrade and the honest
no-fallback fail-closed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import ConfigSnapshotV3
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
)
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineState
from opencode.orchestrator.llm_call import (
    TOOL_BODY_UNSUPPORTED_MESSAGE,
    MaasLlmCall,
    checkpointer_path_for,
    render_prompt,
    unsupported_tool_body,
)
from opencode.orchestrator.workers import WorkerError

_DEFAULT_MODEL = ModelRef(provider="maas", model="default-model-id")


def _node(
    model: str = "small",
    template: str = "Summarize: {topic}",
    *,
    output_schema: JsonObject | None = None,
    fallback: JsonValue | None = None,
) -> PipelineNode:
    return PipelineNode(
        node_id="summarize",
        node_type="llm_call",
        model=model,
        prompt_template=template,
        output_schema=output_schema,
        fallback=fallback,
    )


def _state(params: JsonObject, node_results: dict[str, JsonValue] | None = None) -> EngineState:
    return EngineState(params=params, node_results=node_results or {})


class _ScriptedProvider:
    """A Provider seam fake: records requests, replays scripted chunks."""

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks
        self.requests: list[GenerationRequest] = []

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        for chunk in self._chunks:
            yield chunk


def _text(delta: str, index: int) -> TextDeltaChunk:
    return TextDeltaChunk(request_id="req_unit", index=index, type="text_delta", part_id="text-0", delta=delta)


def _finish(reason: FinishReason, index: int) -> FinishChunk:
    return FinishChunk(request_id="req_unit", index=index, type="finish", reason=reason, raw_reason=reason.value)


def _error(code: CoreErrorCode, index: int) -> ErrorChunk:
    return ErrorChunk(
        request_id="req_unit",
        index=index,
        type="error",
        error=CoreError(code=code, message="scripted provider failure", retryable=True),
    )


def _body(chunks: list[ProviderChunk], **kwargs: Any) -> tuple[MaasLlmCall, _ScriptedProvider]:
    provider = _ScriptedProvider(chunks)
    body = MaasLlmCall(provider=provider, default_model=_DEFAULT_MODEL, **kwargs)
    return body, provider


class TestRenderPrompt:
    def test_renders_named_placeholders_from_the_params(self) -> None:
        assert render_prompt("Summarize: {topic}", {"topic": "the notes"}) == "Summarize: the notes"

    def test_non_string_values_render_as_canonical_json(self) -> None:
        assert render_prompt("{n} {flag} {none}", {"n": 3, "flag": True, "none": None}) == "3 true null"
        assert render_prompt("{items}", {"items": [1, {"b": 2, "a": 1}]}) == '[1,{"a":1,"b":2}]'

    def test_escaped_braces_stay_literal_and_need_no_param(self) -> None:
        assert render_prompt("{{topic}}", {}) == "{topic}"
        assert render_prompt("{{}} done", {"x": "y"}) == "{} done"

    def test_format_specs_operate_on_the_inserted_string(self) -> None:
        assert render_prompt("[{count:>5}]", {"count": 3}) == "[    3]"

    def test_missing_param_key_is_a_structured_validation_error(self) -> None:
        with pytest.raises(WorkerError, match="missing param key 'topic'") as error:
            render_prompt("Summarize: {topic}", {})
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR

    def test_malformed_template_is_a_structured_validation_error(self) -> None:
        with pytest.raises(WorkerError, match="not a valid format template") as error:
            render_prompt("Summarize: {", {"topic": "x"})
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR

    def test_unsupported_access_forms_are_structured_validation_errors(self) -> None:
        with pytest.raises(WorkerError, match="does not support") as error:
            render_prompt("{}", {})
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR


class TestRenderContext:
    """The v2 render-context faces: index chains step into real JSON values."""

    def test_an_index_chain_steps_into_the_real_json_object(self) -> None:
        assert render_prompt("{step_1[sop_data]}", {"step_1": {"sop_data": {"b": 2, "a": 1}}}) == '{"a":1,"b":2}'

    def test_an_index_chain_steps_into_arrays_by_position(self) -> None:
        assert render_prompt("{step_2[0]}", {"step_2": ["x", "y"]}) == "x"
        assert render_prompt("{step_2[0][b]}", {"step_2": [{"b": 2}]}) == "2"

    def test_a_missing_field_in_an_index_chain_is_the_missing_key_face(self) -> None:
        with pytest.raises(WorkerError, match="missing param key 'ghost'") as error:
            render_prompt("{step_1[ghost]}", {"step_1": {"a": 1}})
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR

    def test_stepping_into_a_scalar_is_the_unsupported_access_face(self) -> None:
        with pytest.raises(WorkerError, match="does not support") as error:
            render_prompt("{step_1[field]}", {"step_1": "scalar"})
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR


class TestExecutionContext:
    """The v2 merged render context: submitted params plus non-skipped node results."""

    async def test_the_prompt_renders_over_params_and_step_results(self) -> None:
        body, provider = _body([_finish(FinishReason.STOP, 0)])
        state = _state({"topic": "t"}, {"step_1": {"data": "d"}})
        await body(_node(template="Use {step_1[data]} and {topic}."), state)
        assert [message.model_dump() for message in provider.requests[0].messages] == [
            {"role": "user", "content": [{"type": "text", "text": "Use d and t."}]}
        ]

    async def test_skip_records_never_enter_the_render_context(self) -> None:
        body, _ = _body([_finish(FinishReason.STOP, 0)])
        skipped: JsonValue = {"skipped": True, "condition": "c", "reason": "r"}
        state = _state({"topic": "t"}, {"step_2": skipped})
        with pytest.raises(WorkerError, match="missing param key 'step_2'") as error:
            await body(_node(template="Use {step_2}."), state)
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR


class TestTierResolution:
    async def test_without_a_tier_table_every_tier_falls_back_to_the_default_model(self) -> None:
        body, provider = _body([_finish(FinishReason.STOP, 0)])
        await body(_node(model="whatever"), _state({"topic": "x"}))
        assert provider.requests[0].model.model == "default-model-id"
        assert provider.requests[0].model.provider == "maas"

    async def test_the_tier_table_resolves_named_tiers(self) -> None:
        body, provider = _body([_finish(FinishReason.STOP, 0)], tiers={"small": "tier-small-model"})
        await body(_node(model="small"), _state({"topic": "x"}))
        assert provider.requests[0].model.model == "tier-small-model"
        assert provider.requests[0].model.provider == "maas"

    async def test_an_unknown_tier_with_a_table_is_provider_unavailable(self) -> None:
        body, _ = _body([], tiers={"small": "tier-small-model"})
        with pytest.raises(WorkerError, match="tier 'huge'") as error:
            await body(_node(model="huge"), _state({"topic": "x"}))
        assert error.value.code is CoreErrorCode.PROVIDER_UNAVAILABLE


class TestStreamAggregation:
    async def test_text_deltas_aggregate_into_the_pinned_result_shape(self) -> None:
        body, provider = _body([_text("sum", 0), _text("mary", 1), _finish(FinishReason.STOP, 2)])
        result = await body(_node(), _state({"topic": "the notes"}))
        assert result == {"text": "summary", "model": "small", "finish_reason": "stop"}
        request = provider.requests[0]
        assert [message.model_dump() for message in request.messages] == [
            {"role": "user", "content": [{"type": "text", "text": "Summarize: the notes"}]}
        ]
        assert request.tools == []
        assert request.response_format is None  # a v1 node requests no structured output
        assert request.model.model == "default-model-id"

    async def test_the_terminal_error_chunk_maps_to_its_own_core_code(self) -> None:
        body, _ = _body([_text("par", 0), _error(CoreErrorCode.PROVIDER_UNAVAILABLE, 1)])
        with pytest.raises(WorkerError, match="scripted provider failure") as error:
            await body(_node(), _state({"topic": "x"}))
        assert error.value.code is CoreErrorCode.PROVIDER_UNAVAILABLE

    async def test_a_stream_without_any_terminal_chunk_fails_closed(self) -> None:
        body, _ = _body([_text("par", 0)])
        with pytest.raises(WorkerError, match="without a terminal chunk") as error:
            await body(_node(), _state({"topic": "x"}))
        assert error.value.code is CoreErrorCode.PROVIDER_STREAM_INTERRUPTED

    async def test_a_tool_calls_finish_is_rejected_for_the_no_tools_request(self) -> None:
        body, _ = _body([_finish(FinishReason.TOOL_CALLS, 0)])
        with pytest.raises(WorkerError, match="no-tools") as error:
            await body(_node(), _state({"topic": "x"}))
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR

    async def test_a_length_finish_carries_the_truncation_fact(self) -> None:
        body, _ = _body([_text("cut shor", 0), _finish(FinishReason.LENGTH, 1)])
        result = await body(_node(), _state({"topic": "x"}))
        assert result == {"text": "cut shor", "model": "small", "finish_reason": "length"}


#: The structured-output schema the tests below declare: strict (exactly one
#: string "answer" key) so a scripted completion either clearly satisfies it
#: or clearly does not.
_V2_OUTPUT_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class TestStructuredOutput:
    """The v2 structured-output face over the provider seam fake."""

    async def test_a_schema_node_requests_the_json_schema_response_format(self) -> None:
        body, provider = _body([_text('{"answer": "ok"}', 0), _finish(FinishReason.STOP, 1)])
        node = _node(output_schema=_V2_OUTPUT_SCHEMA)
        result = await body(node, _state({"topic": "t"}))
        assert provider.requests[0].response_format == {
            "type": "json_schema",
            "json_schema": {"name": "summarize", "schema": _V2_OUTPUT_SCHEMA},
        }
        assert result == {
            "text": '{"answer": "ok"}',
            "model": "small",
            "finish_reason": "stop",
            "structured": {"answer": "ok"},
        }

    async def test_the_response_format_name_sanitizes_the_node_id(self) -> None:
        body, provider = _body([_text('{"answer": "ok"}', 0), _finish(FinishReason.STOP, 1)])
        node = PipelineNode(
            node_id="2nd summarize",
            node_type="llm_call",
            model="small",
            prompt_template="Summarize: {topic}",
            output_schema=_V2_OUTPUT_SCHEMA,
        )
        await body(node, _state({"topic": "t"}))
        # unsafe characters become "_", a leading digit gets the "schema_" prefix
        assert provider.requests[0].response_format == {
            "type": "json_schema",
            "json_schema": {"name": "schema_2nd_summarize", "schema": _V2_OUTPUT_SCHEMA},
        }

    async def test_a_fenced_completion_parses_into_structured(self) -> None:
        chunks: list[ProviderChunk] = [
            _text("```json\n", 0),
            _text('{"answer": "ok"}', 1),
            _text("\n```", 2),
            _finish(FinishReason.STOP, 3),
        ]
        body, _ = _body(chunks)
        result = await body(_node(output_schema=_V2_OUTPUT_SCHEMA), _state({"topic": "t"}))
        assert result == {
            "text": '```json\n{"answer": "ok"}\n```',
            "model": "small",
            "finish_reason": "stop",
            "structured": {"answer": "ok"},
        }

    async def test_an_unparseable_completion_degrades_onto_the_fallback(self) -> None:
        body, _ = _body([_text("not json", 0), _finish(FinishReason.STOP, 1)])
        node = _node(output_schema=_V2_OUTPUT_SCHEMA, fallback={"answer": "none"})
        result = await body(node, _state({"topic": "t"}))
        assert result == {
            "text": "not json",
            "model": "small",
            "finish_reason": "stop",
            "structured": {"answer": "none"},
            "fallback": True,
        }

    async def test_a_schema_invalid_completion_degrades_onto_the_fallback(self) -> None:
        body, _ = _body([_text('{"wrong": 1}', 0), _finish(FinishReason.STOP, 1)])
        node = _node(output_schema=_V2_OUTPUT_SCHEMA, fallback={"answer": "none"})
        result = await body(node, _state({"topic": "t"}))
        assert result == {
            "text": '{"wrong": 1}',
            "model": "small",
            "finish_reason": "stop",
            "structured": {"answer": "none"},
            "fallback": True,
        }

    async def test_an_unparseable_completion_without_a_fallback_fails_closed(self) -> None:
        body, _ = _body([_text("not json", 0), _finish(FinishReason.STOP, 1)])
        with pytest.raises(WorkerError, match="not parseable JSON") as error:
            await body(_node(output_schema=_V2_OUTPUT_SCHEMA), _state({"topic": "t"}))
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR

    async def test_a_schema_invalid_completion_without_a_fallback_fails_closed(self) -> None:
        body, _ = _body([_text('{"wrong": 1}', 0), _finish(FinishReason.STOP, 1)])
        with pytest.raises(WorkerError, match="structured output failed validation") as error:
            await body(_node(output_schema=_V2_OUTPUT_SCHEMA), _state({"topic": "t"}))
        assert error.value.code is CoreErrorCode.VALIDATION_ERROR


class TestConstructorFace:
    def test_provider_and_credentials_are_mutually_exclusive(self) -> None:
        provider = _ScriptedProvider([])
        with pytest.raises(ValueError, match="not both"):
            MaasLlmCall(provider=provider, base_url="http://maas", api_key="k", default_model=_DEFAULT_MODEL)

    def test_credentials_require_both_parts(self) -> None:
        with pytest.raises(ValueError, match="both base_url and api_key"):
            MaasLlmCall(base_url="http://maas", default_model=_DEFAULT_MODEL)


class TestUnsupportedToolBody:
    async def test_the_tool_body_refuses_with_the_unsupported_capability_face(self) -> None:
        node = PipelineNode(node_id="collect", node_type="tool", tool_ref="fs.read_text")
        with pytest.raises(WorkerError, match="later slice") as error:
            await unsupported_tool_body(node, _state({}))
        assert error.value.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert str(error.value) == TOOL_BODY_UNSUPPORTED_MESSAGE.format(node_id="collect")
        assert "node 'collect'" in str(error.value)


class TestHostOnlyFaces:
    def test_the_config_snapshot_face_gains_no_provider_fields(self) -> None:
        """The host-only pin: this slice adds no base_url/api_key/tier field."""
        assert set(ConfigSnapshotV3.model_fields) == {
            "schema_version",
            "model",
            "endpoint_ref",
            "roots",
            "permission_rules",
            "instructions",
            "limits",
            "mcp_servers",
        }

    def test_the_advisory_cursor_file_derives_from_the_db_path(self) -> None:
        assert checkpointer_path_for(Path("/data/app.db")) == Path("/data/app-checkpointer.sqlite3")
        assert checkpointer_path_for("bare") == Path("bare-checkpointer.sqlite3")
