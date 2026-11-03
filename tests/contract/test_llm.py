"""R2-S1 contract tests: LLM request/chunk face (CORE-CONTRACTS §2.2/§2.3).

GenerationRequest / LLMMessage / ProviderChunk wire shapes: field sets exactly
per §2.2 L78 and the §2.3 L88-98 chunk table, fail-closed unknown/missing
fields, the §1 integer-bool rule on every numeric field (extended to the float
request fields, see progress notes), and the null-means-unknown usage rule.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMMessage,
    LLMPart,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.tools import ToolSpec

_PART_ADAPTER = TypeAdapter(LLMPart)
_CHUNK_ADAPTER = TypeAdapter(ProviderChunk)

_TOKEN_FIELDS = ["input", "output", "reasoning", "cache_read", "cache_write"]


def _spec() -> ToolSpec:
    return ToolSpec(
        namespace="fs",
        name="read",
        version=1,
        description="Read a file from the workspace roots.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=True,
    )


def _request_raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "request_id": "req_0000000000000000000000000002",
        "model": {"provider": "maas", "model": "glm-4.6"},
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "You are a careful agent."}]},
            {"role": "user", "content": [{"type": "text", "text": "What is in notes.md?"}]},
        ],
        "tools": [_spec().model_dump()],
        "max_output_tokens": 4096,
        "timeout_s": 120.0,
    }
    raw.update(overrides)
    return raw


def _fields(**fields: Any) -> dict[str, Any]:
    """Homogenize literal chunk-data dicts (mypy joins heterogeneous dict literals to object)."""
    return fields


def _chunk_raw(chunk_type: str, **overrides: Any) -> dict[str, Any]:
    """One chunk per §2.3 L88-98 row; call_index-bearing rows included where relevant."""
    data: dict[str, dict[str, Any]] = {
        "text_delta": _fields(part_id="part_1", delta="Rea"),
        "reasoning_delta": _fields(part_id="part_2", delta="Thi"),
        "tool_call_start": _fields(call_index=0),
        "tool_call_args_delta": _fields(call_index=0, delta='{"path"'),
        "tool_call_end": _fields(
            call_index=0,
            tool_call_id="call_0000000000000000000000000001",
            name="read",
            raw_arguments='{"path": "notes.md"}',
        ),
        "usage": _fields(usage={"input": 128, "output": 42}),
        "finish": _fields(reason="stop"),
        "error": _fields(error={"code": "ToolFailed", "message": "boom", "retryable": False}),
    }[chunk_type]
    raw: dict[str, Any] = {
        "request_id": "req_0000000000000000000000000002",
        "index": 0,
        "type": chunk_type,
        "provider_metadata": {},
    }
    raw.update(data)
    raw.update(overrides)
    return raw


class TestModelRef:
    """§2.1 L74: {provider, model}."""

    def test_round_trip(self) -> None:
        ref = ModelRef(provider="maas", model="glm-4.6")
        assert ModelRef.model_validate(json.loads(ref.model_dump_json())) == ref

    def test_missing_and_empty_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelRef.model_validate({"provider": "maas"})
        with pytest.raises(ValidationError):
            ModelRef.model_validate({"provider": "", "model": "glm-4.6"})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelRef.model_validate({"provider": "maas", "model": "glm-4.6", "endpoint": "https://x"})


class TestUsage:
    """§2.1 L74: five optional non-negative counts; null = unknown, never 0."""

    def test_all_fields_optional_and_null(self) -> None:
        usage = Usage()
        assert usage.input is None and usage.output is None
        assert usage.reasoning is None and usage.cache_read is None and usage.cache_write is None
        again = Usage.model_validate(json.loads(usage.model_dump_json()))
        assert again == usage

    def test_explicit_zero_is_zero_not_null(self) -> None:
        usage = Usage.model_validate({"input": 0, "output": 0})
        assert usage.input == 0 and usage.output == 0

    @pytest.mark.parametrize("field", _TOKEN_FIELDS)
    def test_negative_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            Usage.model_validate({field: -1})

    @pytest.mark.parametrize("field", _TOKEN_FIELDS)
    @pytest.mark.parametrize("value", [True, False])
    def test_bool_rejected(self, field: str, value: bool) -> None:
        with pytest.raises(ValidationError):
            Usage.model_validate({field: value})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Usage.model_validate({"input": 1, "total": 1})


class TestFinishReason:
    """§2.3 L97: the Python logical vocabulary (diverges from the TS raw set, parity note)."""

    def test_values_match_contract(self) -> None:
        assert {reason.value for reason in FinishReason} == {"stop", "tool_calls", "length", "filter", "unknown"}

    def test_ts_only_values_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FinishChunk.model_validate(_chunk_raw("finish", reason="content_filter"))


class TestGenerationRequest:
    """§2.2 L78: request_id/model/messages/tools/max_output_tokens/temperature?/timeout_s."""

    def test_full_round_trip_with_tools_and_temperature(self) -> None:
        request = GenerationRequest.model_validate(_request_raw(temperature=0.2))
        assert request.tools[0].name == "read"
        assert request.temperature == 0.2
        again = GenerationRequest.model_validate(json.loads(request.model_dump_json()))
        assert again == request

    def test_temperature_optional_timeout_required(self) -> None:
        assert GenerationRequest.model_validate(_request_raw()).temperature is None
        without_timeout = _request_raw()
        del without_timeout["timeout_s"]
        with pytest.raises(ValidationError):
            GenerationRequest.model_validate(without_timeout)

    def test_messages_and_tools_are_required_keys_but_may_be_empty_lists(self) -> None:
        for field in ("messages", "tools"):
            raw = _request_raw()
            del raw[field]
            with pytest.raises(ValidationError):
                GenerationRequest.model_validate(raw)
        request = GenerationRequest.model_validate(_request_raw(messages=[], tools=[]))
        assert request.messages == [] and request.tools == []

    def test_max_output_tokens_positive_and_bool_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GenerationRequest.model_validate(_request_raw(max_output_tokens=0))
        for bad in (True, False):
            with pytest.raises(ValidationError):
                GenerationRequest.model_validate(_request_raw(max_output_tokens=bad))

    def test_timeout_s_positive_and_bools_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GenerationRequest.model_validate(_request_raw(timeout_s=0))
        for bad in (True, False):
            with pytest.raises(ValidationError):
                GenerationRequest.model_validate(_request_raw(timeout_s=bad))
            with pytest.raises(ValidationError):
                GenerationRequest.model_validate(_request_raw(temperature=bad))

    def test_request_id_non_empty_without_prefix_rule(self) -> None:
        request = GenerationRequest.model_validate(_request_raw(request_id="client-chosen-id"))
        assert request.request_id == "client-chosen-id"
        with pytest.raises(ValidationError):
            GenerationRequest.model_validate(_request_raw(request_id=""))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GenerationRequest.model_validate(_request_raw(stream_options={"include_usage": True}))


class TestLLMMessage:
    """§2.2 L80: role + text/reasoning/tool_call/tool_result content union."""

    @pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
    def test_four_roles_accepted(self, role: str) -> None:
        message = LLMMessage.model_validate({"role": role, "content": [{"type": "text", "text": "x"}]})
        assert message.role == role

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMMessage.model_validate({"role": "developer", "content": []})

    def test_tool_call_part_shape(self) -> None:
        part = {
            "type": "tool_call",
            "tool_call_id": "call_0000000000000000000000000001",
            "name": "read",
            "input": {"path": "notes.md"},
        }
        assert _PART_ADAPTER.validate_python(part).type == "tool_call"
        with pytest.raises(ValidationError):
            _PART_ADAPTER.validate_python({**part, "input": ["not", "an", "object"]})

    def test_tool_result_part_carries_content_union(self) -> None:
        part = {
            "type": "tool_result",
            "tool_call_id": "call_0000000000000000000000000001",
            "name": "read",
            "content": [
                {"type": "text", "text": "(1) # Project notes"},
                {"type": "file", "uri": "file:///D:/work/demo/notes.md", "mime": "text/markdown"},
            ],
        }
        assert _PART_ADAPTER.validate_python(part).type == "tool_result"

    def test_missing_identity_fields_rejected(self) -> None:
        for raw in (
            {"type": "tool_call", "name": "read", "input": {}},
            {"type": "tool_result", "tool_call_id": "call_1", "name": "read"},
        ):
            with pytest.raises(ValidationError):
                _PART_ADAPTER.validate_python(raw)

    def test_unknown_part_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LLMMessage.model_validate({"role": "user", "content": [{"type": "audio", "text": "x"}]})

    def test_full_request_round_trip_exercises_all_four_part_types(self) -> None:
        raw = _request_raw(
            messages=[
                {"role": "system", "content": [{"type": "text", "text": "sys"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "reasoning", "text": "need the file"},
                        {
                            "type": "tool_call",
                            "tool_call_id": "call_0000000000000000000000000001",
                            "name": "read",
                            "input": {"path": "notes.md"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_call_id": "call_0000000000000000000000000001",
                            "name": "read",
                            "content": [{"type": "text", "text": "(1) # Project notes"}],
                        }
                    ],
                },
            ]
        )
        request = GenerationRequest.model_validate(raw)
        assert GenerationRequest.model_validate(json.loads(request.model_dump_json())) == request


class TestProviderChunk:
    """§2.3 L88-98: common fields + per-type required data, tagged by ``type``."""

    @pytest.mark.parametrize(
        "chunk_type",
        [
            "text_delta",
            "reasoning_delta",
            "tool_call_start",
            "tool_call_args_delta",
            "tool_call_end",
            "usage",
            "finish",
            "error",
        ],
    )
    def test_each_chunk_type_round_trips(self, chunk_type: str) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw(chunk_type))
        assert chunk.type == chunk_type
        assert _CHUNK_ADAPTER.validate_json(_CHUNK_ADAPTER.dump_json(chunk)) == chunk

    def test_common_fields_required(self) -> None:
        for field in ("request_id", "index"):
            raw = _chunk_raw("text_delta")
            del raw[field]
            with pytest.raises(ValidationError):
                _CHUNK_ADAPTER.validate_python(raw)

    def test_provider_metadata_defaults_to_empty_object(self) -> None:
        raw = _chunk_raw("text_delta")
        del raw["provider_metadata"]
        chunk = _CHUNK_ADAPTER.validate_python(raw)
        assert isinstance(chunk, TextDeltaChunk)
        assert chunk.provider_metadata == {}

    def test_provider_metadata_rejects_non_json_values(self) -> None:
        bad_metadata: dict[str, Any] = {"when": object()}  # deliberately non-JSON
        with pytest.raises(ValidationError):
            TextDeltaChunk(
                request_id="req_0000000000000000000000000002",
                index=0,
                type="text_delta",
                part_id="part_1",
                delta="x",
                provider_metadata=bad_metadata,
            )

    def test_index_non_negative_and_bool_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _CHUNK_ADAPTER.validate_python(_chunk_raw("text_delta", index=-1))
        for bad in (True, False):
            with pytest.raises(ValidationError):
                _CHUNK_ADAPTER.validate_python(_chunk_raw("text_delta", index=bad))

    @pytest.mark.parametrize("chunk_type", ["tool_call_start", "tool_call_args_delta", "tool_call_end"])
    def test_call_index_bool_rejected(self, chunk_type: str) -> None:
        for bad in (True, False):
            with pytest.raises(ValidationError):
                _CHUNK_ADAPTER.validate_python(_chunk_raw(chunk_type, call_index=bad))

    def test_tool_call_start_identity_optional(self) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw("tool_call_start"))
        assert isinstance(chunk, ToolCallStartChunk)
        assert chunk.tool_call_id is None and chunk.name is None

    def test_tool_call_end_requires_identity_and_raw_arguments(self) -> None:
        for field in ("tool_call_id", "name", "raw_arguments"):
            raw = _chunk_raw("tool_call_end")
            del raw[field]
            with pytest.raises(ValidationError):
                _CHUNK_ADAPTER.validate_python(raw)

    def test_usage_chunk_carries_usage_and_optional_cost(self) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw("usage"))
        assert isinstance(chunk, UsageChunk)
        assert chunk.usage.output == 42 and chunk.cost_usd is None
        priced = _CHUNK_ADAPTER.validate_python(_chunk_raw("usage", cost_usd="0.001234"))
        assert isinstance(priced, UsageChunk)
        assert priced.cost_usd == "0.001234"
        with pytest.raises(ValidationError):
            _CHUNK_ADAPTER.validate_python(_chunk_raw("usage", cost_usd="1e-3"))

    def test_finish_chunk_reason_enum_and_raw_reason_optional(self) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw("finish", raw_reason="end_turn"))
        assert isinstance(chunk, FinishChunk)
        assert chunk.reason is FinishReason.STOP and chunk.raw_reason == "end_turn"

    def test_error_chunk_carries_core_error(self) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw("error"))
        assert isinstance(chunk, ErrorChunk)
        assert chunk.error.code is CoreErrorCode.TOOL_FAILED
        assert CoreError(code=CoreErrorCode.TOOL_FAILED, message="boom", retryable=False) == chunk.error

    def test_unknown_chunk_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _CHUNK_ADAPTER.validate_python(_chunk_raw("text_delta", type="logprobs"))

    def test_unknown_chunk_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _CHUNK_ADAPTER.validate_python(_chunk_raw("text_delta", unexpected_chunk_field=True))

    def test_args_delta_shape(self) -> None:
        chunk = _CHUNK_ADAPTER.validate_python(_chunk_raw("tool_call_args_delta"))
        assert isinstance(chunk, ToolCallArgsDeltaChunk)
        assert chunk.delta == '{"path"' and chunk.call_index == 0
