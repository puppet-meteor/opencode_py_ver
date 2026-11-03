"""R2-S1 contract tests: message union expansion (CORE-CONTRACTS §2.1).

ToolContent / AssistantPart / AssistantMessage / IdleMessage wire shapes: round
trips are lossless, unknown or missing fields fail closed, ID prefix and
fixed-point rules hold, and the deferred-rebuild wiring (messages.py leaf +
llm.py completion, see messages module docstring) is pinned so the models can
never be consumed half-built.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import FinishReason, Usage
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantPart,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolContent,
    ToolFileContent,
    ToolTextContent,
)

_CREATED_AT = datetime(2026, 11, 3, 8, 0, 0)  # naive on purpose: the reject case below

_PART_ADAPTER = TypeAdapter(AssistantPart)
_CONTENT_ADAPTER = TypeAdapter(ToolContent)


def _assistant_raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "msg_0000000000000000000000000012",
        "type": "assistant",
        "created_at": "2026-11-03T08:00:00Z",
        "metadata": {},
        "model": {"provider": "maas", "model": "glm-4.6"},
        "parts": [
            {"type": "text", "part_id": "part_0000000000000000000000000001", "text": "Reading notes.md now."},
            {"type": "reasoning", "part_id": "part_0000000000000000000000000002", "text": "Use the read tool."},
            {
                "type": "tool",
                "tool_call_id": "call_0000000000000000000000000001",
                "execution_id": "exec_0000000000000000000000000001",
                "name": "read",
                "input": {"path": "notes.md", "limit": 50},
                "state": "pending",
                "effect_status": "not_started",
                "content": [],
            },
        ],
        "status": "running",
    }
    raw.update(overrides)
    return raw


class TestToolContent:
    """§2.1 L69-70: text/file union; file is wire-encodable, media stays a runtime gate."""

    def test_text_content_round_trip(self) -> None:
        content = ToolTextContent(type="text", text="(1) # Project notes")
        assert ToolTextContent.model_validate(json.loads(content.model_dump_json())) == content

    def test_file_content_round_trip_with_and_without_name(self) -> None:
        with_name = ToolFileContent(
            type="file", uri="file:///D:/work/demo/notes.md", mime="text/markdown", name="notes.md"
        )
        assert ToolFileContent.model_validate(json.loads(with_name.model_dump_json())) == with_name
        without_name = ToolFileContent(type="file", uri="file:///D:/work/demo/a.png", mime="image/png")
        assert without_name.name is None
        assert ToolFileContent.model_validate(json.loads(without_name.model_dump_json())) == without_name

    def test_file_content_requires_uri_and_mime(self) -> None:
        for raw in (
            {"type": "file", "mime": "text/markdown"},
            {"type": "file", "uri": "file:///D:/work/demo/notes.md"},
            {"type": "file", "uri": "", "mime": "text/markdown"},
        ):
            with pytest.raises(ValidationError):
                ToolFileContent.model_validate(raw)

    def test_unknown_content_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _CONTENT_ADAPTER.validate_python({"type": "image", "uri": "file:///x", "mime": "image/png"})

    def test_unknown_keys_rejected_on_both_variants(self) -> None:
        with pytest.raises(ValidationError):
            ToolTextContent.model_validate({"type": "text", "text": "hi", "encoding": "utf-8"})
        with pytest.raises(ValidationError):
            ToolFileContent.model_validate({"type": "file", "uri": "file:///x", "mime": "text/plain", "size": 3})


class TestAssistantParts:
    """§2.1 L33: text/reasoning/tool tagged union shapes."""

    def test_text_and_reasoning_parts_round_trip(self) -> None:
        text_part = AssistantTextPart(type="text", part_id="part_1", text="hello")
        reasoning_part = AssistantReasoningPart(type="reasoning", part_id="part_2", text="thinking")
        for part in (text_part, reasoning_part):
            assert _PART_ADAPTER.validate_json(part.model_dump_json()) == part

    def test_part_id_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            AssistantTextPart(type="text", part_id="", text="hello")

    def test_tool_part_pending_plan_with_empty_content(self) -> None:
        part = AssistantToolPart(
            type="tool",
            tool_call_id="call_0000000000000000000000000001",
            execution_id="exec_0000000000000000000000000001",
            name="read",
            input={"path": "notes.md"},
            state="pending",
            effect_status="not_started",
            content=[],
        )
        assert part.content == []
        assert part.structured_output is None
        assert part.error is None
        assert _PART_ADAPTER.validate_json(part.model_dump_json()) == part

    def test_tool_part_content_is_a_required_key(self) -> None:
        raw: dict[str, Any] = {
            "type": "tool",
            "tool_call_id": "call_0000000000000000000000000001",
            "execution_id": "exec_0000000000000000000000000001",
            "name": "read",
            "input": {"path": "notes.md"},
            "state": "pending",
            "effect_status": "not_started",
        }
        with pytest.raises(ValidationError):
            AssistantToolPart.model_validate(raw)

    @pytest.mark.parametrize(
        ("field", "bad_values"),
        [
            ("state", ["succeeding", "cancelled_by_user", ""]),
            ("effect_status", ["started", "partial", ""]),
        ],
    )
    def test_tool_part_vocabularies_are_closed(self, field: str, bad_values: list[str]) -> None:
        for bad in bad_values:
            raw = {
                "type": "tool",
                "tool_call_id": "call_0000000000000000000000000001",
                "execution_id": "exec_0000000000000000000000000001",
                "name": "read",
                "input": {"path": "notes.md"},
                "state": "pending",
                "effect_status": "not_started",
                "content": [],
            }
            raw[field] = bad
            with pytest.raises(ValidationError):
                AssistantToolPart.model_validate(raw)

    def test_tool_part_error_nests_core_error(self) -> None:
        part = AssistantToolPart(
            type="tool",
            tool_call_id="call_0000000000000000000000000001",
            execution_id="exec_0000000000000000000000000001",
            name="read",
            input={},
            state="failed",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text="input rejected")],
            error=CoreError(code=CoreErrorCode.TOOL_FAILED, message="schema mismatch", retryable=False),
        )
        assert part.error is not None and part.error.code is CoreErrorCode.TOOL_FAILED
        assert _PART_ADAPTER.validate_json(part.model_dump_json()) == part

    def test_unknown_part_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _PART_ADAPTER.validate_python({"type": "audio", "part_id": "part_9", "text": "?"})


class TestAssistantMessage:
    """§2.1 L28: common fields + model/parts/status + the contract-named optional set."""

    def test_running_message_minimal_round_trip(self) -> None:
        message = AssistantMessage.model_validate(_assistant_raw())
        assert message.status == "running"
        assert message.finish is None and message.usage is None and message.cost_usd is None
        assert message.completed_at is None
        again = AssistantMessage.model_validate(json.loads(message.model_dump_json()))
        assert again == message

    def test_completed_message_with_all_optional_fields(self) -> None:
        raw = _assistant_raw(
            status="completed",
            finish="tool_calls",
            raw_finish="tool_calls",
            usage={"input": 128, "output": 42, "reasoning": None, "cache_read": None, "cache_write": None},
            cost_usd="0.001234",
            completed_at="2026-11-03T08:00:18Z",
        )
        message = AssistantMessage.model_validate(raw)
        assert message.finish is FinishReason.TOOL_CALLS
        assert message.model.provider == "maas"
        assert message.usage is not None and message.usage.input == 128 and message.usage.output == 42
        assert message.cost_usd == "0.001234"
        assert AssistantMessage.model_validate(json.loads(message.model_dump_json())) == message

    def test_id_prefix_and_type_tag_enforced(self) -> None:
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(id="assistant_1"))
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(type="user"))

    def test_status_vocabulary_is_running_completed_failed_interrupted(self) -> None:
        for good in ("running", "completed", "failed", "interrupted"):
            assert AssistantMessage.model_validate(_assistant_raw(status=good)).status == good
        for bad in ("cancelling", "pending", ""):
            with pytest.raises(ValidationError):
                AssistantMessage.model_validate(_assistant_raw(status=bad))

    def test_model_field_is_a_model_ref(self) -> None:
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(model={"provider": "", "model": "glm-4.6"}))
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(model="maas/glm-4.6"))

    def test_cost_usd_is_decimal_fixed_point_only(self) -> None:
        for good in ("0", "12", "0.001234", "12345.67"):
            assert AssistantMessage.model_validate(_assistant_raw(cost_usd=good)).cost_usd == good
        for bad in ("1e-3", "-0.5", ".5", "1.", "0.001,234", ""):
            with pytest.raises(ValidationError):
                AssistantMessage.model_validate(_assistant_raw(cost_usd=bad))

    def test_usage_null_means_unknown_not_zero(self) -> None:
        message = AssistantMessage.model_validate(_assistant_raw(usage={"input": 10, "output": None}))
        assert message.usage is not None
        assert message.usage.input == 10
        assert message.usage.output is None  # unknown stays null, never a disguised 0
        dumped = json.loads(message.model_dump_json())["usage"]
        assert dumped["output"] is None
        zeroed = AssistantMessage.model_validate(_assistant_raw(usage={"input": 10, "output": 0}))
        assert zeroed.usage is not None and zeroed.usage.output == 0  # explicit provider zero is zero

    def test_naive_timestamps_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(created_at=_CREATED_AT))
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(completed_at="2026-11-03T08:00:18"))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AssistantMessage.model_validate(_assistant_raw(attachments=[]))

    def test_deferred_models_are_complete_after_package_import(self) -> None:
        """The llm.py rebuild wiring must finish every deferred model (see messages.py docstring)."""
        assert AssistantToolPart.__pydantic_complete__ is True
        assert AssistantMessage.__pydantic_complete__ is True
        assert IdleMessage.__pydantic_complete__ is True


class TestIdleMessage:
    """§2.1 L29: run terminal facts, projected only from py.run.finished."""

    def _idle_raw(self, **overrides: Any) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "id": "msg_0000000000000000000000000013",
            "type": "idle",
            "created_at": "2026-11-03T08:00:20Z",
            "metadata": {},
            "run_id": "run_0000000000000000000000000002",
            "outcome": "interrupted",
        }
        raw.update(overrides)
        return raw

    def test_round_trip_with_error(self) -> None:
        raw = self._idle_raw(error={"code": "ProviderStreamInterrupted", "message": "stream cut", "retryable": False})
        message = IdleMessage.model_validate(raw)
        assert message.error is not None and message.error.retryable is False
        assert IdleMessage.model_validate(json.loads(message.model_dump_json())) == message

    def test_error_optional_and_defaults_to_none(self) -> None:
        assert IdleMessage.model_validate(self._idle_raw()).error is None

    def test_outcome_vocabulary(self) -> None:
        for good in ("succeeded", "failed", "interrupted"):
            assert IdleMessage.model_validate(self._idle_raw(outcome=good)).outcome == good
        for bad in ("cancelled", "running", ""):
            with pytest.raises(ValidationError):
                IdleMessage.model_validate(self._idle_raw(outcome=bad))

    def test_run_id_prefix_and_id_prefix_enforced(self) -> None:
        with pytest.raises(ValidationError):
            IdleMessage.model_validate(self._idle_raw(run_id="rn_1"))
        with pytest.raises(ValidationError):
            IdleMessage.model_validate(self._idle_raw(id="idle_1"))

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IdleMessage.model_validate(self._idle_raw(duration_s=3))


class TestUsageShapeFromMessages:
    """Usage as embedded in messages: the five-count wire shape (§2.1 L74)."""

    def test_usage_round_trips_inside_assistant_message(self) -> None:
        raw = _assistant_raw(usage={"input": 3, "output": 4, "reasoning": 5, "cache_read": 6, "cache_write": 7})
        message = AssistantMessage.model_validate(raw)
        assert message.usage == Usage(input=3, output=4, reasoning=5, cache_read=6, cache_write=7)
