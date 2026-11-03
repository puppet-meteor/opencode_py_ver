"""R2-S4 contract tests: ContextBuilder (CORE-CONTRACTS §2.2 L78-80 / §7 L339).

Pure unit tests over the r2-slices §2 R2-S4 test list: history fixture ->
request field-by-field; budget boundaries (exactly-at / over ->
ContextLimitExceeded / output-reserve + reasoning margin, §8.1 L107);
tool_result pairing completeness (every legal tool_call_id exactly one
result, acceptance C back half); catalog snapshot immutability; and the ④-3
dotted-name pinning (T-A-006, 2026-11-03): request tool_call part names are
dotted ``namespace.name`` and round-trip ``fs.read <-> fs__read`` through the
frozen S3 wire mapping (read-only consumption of the ai/maas face).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from opencode.ai.maas import parse_tool_wire_name, tool_wire_name_from_dotted
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.llm import (
    GenerationRequest,
    LLMReasoningPart,
    LLMTextPart,
    LLMToolCallPart,
    LLMToolResultPart,
    ModelRef,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantPart,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolContent,
    ToolTextContent,
    UserMessage,
)
from opencode.contracts.tools import ToolSpec
from opencode.session.context import ContextBuilder, ContextLimitExceeded, RuntimeSettings
from opencode.storage import StoreValidationError

_CREATED_AT = datetime(2026, 11, 3, 8, 0, 0, tzinfo=UTC)

type _ToolState = Literal["pending", "running", "succeeded", "failed", "denied", "cancelled", "unknown"]
type _EffectStatus = Literal["not_started", "completed", "unknown"]
type _AssistantStatus = Literal["running", "completed", "failed", "interrupted"]

#: Default budget fixture: available = 1000 - 200 - 50 = 750 tokens, i.e. an
#: input estimate of exactly 3000 characters fits and one more token (3004
#: chars) overflows.
_SETTINGS_BUDGET_CHARS = 3000


def _settings(**overrides: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "model": ModelRef(provider="maas", model="glm-4.6"),
        "context_limit": 1000,
        "output_reserve": 200,
        "timeout_s": 60.0,
        "safety_margin": 50,
        "temperature": 0.2,
        "system_prompt": None,
    }
    values.update(overrides)
    return RuntimeSettings(**values)


def _read_tool() -> ToolSpec:
    return ToolSpec(
        namespace="fs",
        name="read",
        version=1,
        description="Read a file from the workspace roots.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=True,
    )


def _user(text: str, *, message_id: str = "msg_0000000000000000000000000001") -> UserMessage:
    return UserMessage(id=message_id, type="user", created_at=_CREATED_AT, metadata={}, text=text)


def _assistant(
    parts: Sequence[AssistantPart],
    *,
    message_id: str = "msg_0000000000000000000000000011",
    status: _AssistantStatus = "completed",
) -> AssistantMessage:
    return AssistantMessage(
        id=message_id,
        type="assistant",
        created_at=_CREATED_AT,
        metadata={},
        model=ModelRef(provider="maas", model="glm-4.6"),
        parts=list(parts),
        status=status,
    )


def _tool_part(
    tool_call_id: str = "call_0000000000000000000000000001",
    *,
    name: str = "fs.read",
    state: _ToolState = "succeeded",
    effect_status: _EffectStatus = "not_started",
    content: list[ToolContent] | None = None,
) -> AssistantToolPart:
    return AssistantToolPart(
        type="tool",
        tool_call_id=tool_call_id,
        execution_id="exec_0000000000000000000000000001",
        name=name,
        input={"path": "notes.md"},
        state=state,
        effect_status=effect_status,
        content=content if content is not None else [ToolTextContent(type="text", text="(1) # Project notes")],
    )


def _idle(*, message_id: str = "msg_0000000000000000000000000021") -> IdleMessage:
    return IdleMessage(
        id=message_id,
        type="idle",
        created_at=_CREATED_AT,
        metadata={},
        run_id="run_0000000000000000000000000001",
        outcome="succeeded",
    )


class TestRequestAssembly:
    """History fixture -> GenerationRequest, field by field (§2.2 L78-80)."""

    def test_history_fixture_maps_to_request_field_by_field(self) -> None:
        settings = _settings(system_prompt="You are a careful agent.", temperature=0.3, timeout_s=90.0)
        request = ContextBuilder(settings).build(
            request_id="req-0002",
            history=[
                _user("What is in notes.md?", message_id="msg_0000000000000000000000000001"),
                _assistant(
                    [
                        AssistantReasoningPart(
                            type="reasoning", part_id="part_0000000000000000000000000001", text="Need the file."
                        ),
                        AssistantTextPart(
                            type="text", part_id="part_0000000000000000000000000000002", text="Reading it now."
                        ),
                        _tool_part("call_0000000000000000000000000001", effect_status="completed"),
                    ],
                    message_id="msg_0000000000000000000000000011",
                ),
                _idle(message_id="msg_0000000000000000000000000021"),
                _user("Thanks.", message_id="msg_0000000000000000000000000002"),
            ],
            tools=(_read_tool(),),
        )
        assert request.request_id == "req-0002"
        assert request.model == ModelRef(provider="maas", model="glm-4.6")
        assert request.temperature == 0.3
        assert request.timeout_s == 90.0
        assert request.max_output_tokens == settings.output_reserve
        assert request.tools == [_read_tool()]
        assert [(message.role, [type(part).__name__ for part in message.content]) for message in request.messages] == [
            ("system", ["LLMTextPart"]),
            ("user", ["LLMTextPart"]),
            ("assistant", ["LLMReasoningPart", "LLMTextPart", "LLMToolCallPart"]),
            ("tool", ["LLMToolResultPart"]),
            ("user", ["LLMTextPart"]),
        ]
        system = request.messages[0].content[0]
        assert isinstance(system, LLMTextPart) and system.text == "You are a careful agent."
        user = request.messages[1].content[0]
        assert isinstance(user, LLMTextPart) and user.text == "What is in notes.md?"
        reasoning, text, call = request.messages[2].content
        assert isinstance(reasoning, LLMReasoningPart) and reasoning.text == "Need the file."
        assert isinstance(text, LLMTextPart) and text.text == "Reading it now."
        assert isinstance(call, LLMToolCallPart)
        assert call.tool_call_id == "call_0000000000000000000000000001"
        assert call.name == "fs.read"
        assert call.input == {"path": "notes.md"}
        result = request.messages[3].content[0]
        assert isinstance(result, LLMToolResultPart)
        assert result.tool_call_id == "call_0000000000000000000000000001"
        assert result.name == "fs.read"
        assert result.content == [ToolTextContent(type="text", text="(1) # Project notes")]
        tail = request.messages[4].content[0]
        assert isinstance(tail, LLMTextPart) and tail.text == "Thanks."
        # The whole request survives a wire round trip (S1 face).
        assert GenerationRequest.model_validate(json.loads(request.model_dump_json())) == request

    def test_no_system_message_when_prompt_unset(self) -> None:
        request = ContextBuilder(_settings()).build(request_id="req-1", history=[_user("hi")])
        assert [message.role for message in request.messages] == ["user"]

    def test_empty_system_prompt_omitted(self) -> None:
        request = ContextBuilder(_settings(system_prompt="")).build(request_id="req-1", history=[_user("hi")])
        assert [message.role for message in request.messages] == ["user"]

    def test_idle_message_excluded_from_request(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_user("hi"), _idle(), _assistant([AssistantTextPart(type="text", part_id="part_1", text="ok")])],
        )
        assert [message.role for message in request.messages] == ["user", "assistant"]

    def test_empty_user_text_dropped(self) -> None:
        request = ContextBuilder(_settings()).build(request_id="req-1", history=[_user(""), _user("hi")])
        assert [message.role for message in request.messages] == ["user"]

    def test_empty_assistant_text_and_reasoning_parts_dropped(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[
                _assistant(
                    [
                        AssistantTextPart(type="text", part_id="part_0000000000000000000000000001", text=""),
                        AssistantReasoningPart(type="reasoning", part_id="part_0000000000000000000000000002", text=""),
                        AssistantTextPart(type="text", part_id="part_0000000000000000000000000000003", text="kept"),
                    ]
                )
            ],
        )
        assert [message.role for message in request.messages] == ["assistant"]
        parts = request.messages[0].content
        assert [type(part).__name__ for part in parts] == ["LLMTextPart"]
        assert isinstance(parts[0], LLMTextPart) and parts[0].text == "kept"

    def test_assistant_message_with_only_empty_parts_contributes_nothing(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_assistant([AssistantTextPart(type="text", part_id="part_1", text="")])],
        )
        assert request.messages == []

    def test_empty_history_builds_empty_messages(self) -> None:
        request = ContextBuilder(_settings()).build(request_id="req-1", history=[])
        assert request.messages == []

    def test_no_tools_is_an_empty_list_state(self) -> None:
        request = ContextBuilder(_settings()).build(request_id="req-1", history=[_user("hi")], tools=())
        assert request.tools == []

    def test_emitted_messages_never_mix_part_kinds(self) -> None:
        """The assembly only produces single-kind messages (adapter drop face ③-9)."""
        request = ContextBuilder(_settings(system_prompt="sys")).build(
            request_id="req-1",
            history=[
                _user("hi"),
                _assistant(
                    [
                        AssistantTextPart(type="text", part_id="part_0000000000000000000000000001", text="t"),
                        _tool_part("call_0000000000000000000000000001"),
                    ]
                ),
                _user("and now?"),
            ],
        )
        for message in request.messages:
            if message.role == "tool":
                assert all(isinstance(part, LLMToolResultPart) for part in message.content)
            elif message.role in ("system", "user"):
                assert all(isinstance(part, LLMTextPart) for part in message.content)
            else:
                assert all(
                    isinstance(part, LLMTextPart | LLMReasoningPart | LLMToolCallPart) for part in message.content
                )

    def test_unknown_history_entry_rejected(self) -> None:
        entries: Any = [object()]
        with pytest.raises(StoreValidationError) as info:
            ContextBuilder(_settings()).build(request_id="req-1", history=entries)
        assert info.value.core.details == {"violation": "history_entry_type", "type": "object"}

    def test_request_id_must_be_non_empty(self) -> None:
        with pytest.raises(StoreValidationError):
            ContextBuilder(_settings()).build(request_id="", history=[_user("hi")])


class TestToolResultPairing:
    """Every legal tool_call_id carries exactly one tool_result (acceptance C back half)."""

    def test_every_legal_tool_call_id_has_exactly_one_result(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[
                _user("what is in notes.md and readme.md?"),
                _assistant(
                    [
                        _tool_part("call_0000000000000000000000000001"),
                        _tool_part(
                            "call_0000000000000000000000000002",
                            state="failed",
                            content=[ToolTextContent(type="text", text="boom")],
                        ),
                    ]
                ),
            ],
            tools=(_read_tool(),),
        )
        assert [message.role for message in request.messages] == ["user", "assistant", "tool", "tool"]
        call_ids = [
            part.tool_call_id
            for message in request.messages
            for part in message.content
            if isinstance(part, LLMToolCallPart)
        ]
        result_ids = [
            part.tool_call_id
            for message in request.messages
            for part in message.content
            if isinstance(part, LLMToolResultPart)
        ]
        assert call_ids == ["call_0000000000000000000000000001", "call_0000000000000000000000000002"]
        assert result_ids == call_ids  # exactly one result per call, in call order
        assert len(set(call_ids)) == len(call_ids)

    @pytest.mark.parametrize("state", ["succeeded", "failed", "denied", "cancelled", "unknown"])
    def test_every_terminal_tool_state_replays_call_and_result(self, state: _ToolState) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_assistant([_tool_part("call_0000000000000000000000000001", state=state)])],
        )
        assert [message.role for message in request.messages] == ["assistant", "tool"]

    @pytest.mark.parametrize("state", ["pending", "running"])
    def test_non_terminal_tool_part_rejected(self, state: _ToolState) -> None:
        with pytest.raises(StoreValidationError) as info:
            ContextBuilder(_settings()).build(
                request_id="req-1",
                history=[_assistant([_tool_part("call_0000000000000000000000000001", state=state)])],
            )
        assert info.value.core.details == {
            "violation": "tool_result_missing",
            "tool_call_id": "call_0000000000000000000000000001",
            "state": state,
        }

    def test_duplicate_tool_call_id_rejected(self) -> None:
        with pytest.raises(StoreValidationError) as info:
            ContextBuilder(_settings()).build(
                request_id="req-1",
                history=[
                    _assistant([_tool_part("call_0000000000000000000000000001")]),
                    _assistant(
                        [_tool_part("call_0000000000000000000000000001")],
                        message_id="msg_0000000000000000000000000012",
                    ),
                ],
            )
        assert info.value.core.details == {
            "violation": "duplicate_tool_call_id",
            "tool_call_id": "call_0000000000000000000000000001",
        }

    def test_terminal_tool_part_with_empty_content_carried_verbatim(self) -> None:
        """No synthesized result text: history is carried as stored (§2.1 L72 is upstream's rule)."""
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_assistant([_tool_part("call_0000000000000000000000000001", content=[])])],
        )
        assert [message.role for message in request.messages] == ["assistant", "tool"]
        result = request.messages[1].content[0]
        assert isinstance(result, LLMToolResultPart) and result.content == []


class TestTokenBudget:
    """Budget boundaries: exactly-at / over -> ContextLimitExceeded / reserve + reasoning margin."""

    def test_exactly_at_budget_builds_with_full_output_reserve(self) -> None:
        settings = _settings()  # available = 750 tokens
        request = ContextBuilder(settings).build(
            request_id="req-1",
            history=[_user("a" * _SETTINGS_BUDGET_CHARS)],  # 3000/4 = 750 exactly
        )
        assert request.max_output_tokens == settings.output_reserve
        # §8.1 L107: the reserve is never shrunk to fit a larger history --
        # generated reasoning rides inside it (module docstring note).
        assert request.max_output_tokens == 200

    def test_over_budget_raises_context_limit_exceeded(self) -> None:
        history = [_user("a" * (_SETTINGS_BUDGET_CHARS + 4))]  # 751 tokens > 750
        with pytest.raises(ContextLimitExceeded) as info:
            ContextBuilder(_settings()).build(request_id="req-1", history=history)
        error = info.value.core
        assert error.code is CoreErrorCode.CONTEXT_LIMIT_EXCEEDED
        assert error.retryable is False
        assert "start a new session" in error.message
        assert error.details == {
            "context_limit": 1000,
            "output_reserve": 200,
            "safety_margin": 50,
            "estimated_input_tokens": 751,
            "available_input_tokens": 750,
        }
        # No silent trim, no summary: the history is left untouched.
        assert len(history) == 1 and history[0].text == "a" * (_SETTINGS_BUDGET_CHARS + 4)

    def test_replayed_reasoning_counts_toward_input_estimate(self) -> None:
        """Replayed reasoning is input-side; only generated reasoning rides the output reserve (§8.1)."""
        builder = ContextBuilder(_settings())
        fitting = _assistant(
            [AssistantReasoningPart(type="reasoning", part_id="part_0000000000000000000000000001", text="r" * 3000)]
        )
        overflowing = _assistant(
            [AssistantReasoningPart(type="reasoning", part_id="part_0000000000000000000000000000001", text="r" * 3004)]
        )
        assert builder.build(request_id="req-1", history=[fitting]).max_output_tokens == 200
        with pytest.raises(ContextLimitExceeded):
            builder.build(request_id="req-1", history=[overflowing])

    def test_catalog_counts_toward_estimate(self) -> None:
        """Providers count tool schemas too (TS WINDOW_HEADROOM rationale)."""
        builder = ContextBuilder(_settings())
        history = [_user("a" * _SETTINGS_BUDGET_CHARS)]  # alone: exactly at budget
        assert builder.build(request_id="req-1", history=history).tools == []
        bulky_tool = ToolSpec(
            namespace="fs",
            name="read",
            version=1,
            description="d" * 200,
            input_schema={"type": "object"},
            supported_content_types=["text"],
            side_effects=False,
            idempotency_support=True,
        )
        with pytest.raises(ContextLimitExceeded):
            builder.build(request_id="req-1", history=history, tools=(bulky_tool,))

    def test_ceil_rounding_overflows_on_a_non_multiple_of_four(self) -> None:
        """m-1 (R2-S4-review §2): pin the ceil rounding direction on a char
        count that is NOT a multiple of 4 -- 3001 chars = 750.25 tokens, so
        ceil -> 751 > 750. A floor (``//``) or round implementation would
        estimate exactly 750 and let this history through: this test must go
        red under any non-conservative rounding change."""
        with pytest.raises(ContextLimitExceeded) as info:
            ContextBuilder(_settings()).build(request_id="req-1", history=[_user("a" * 3001)])
        assert info.value.core.details["estimated_input_tokens"] == 751

    def test_ceil_rounding_reaches_exactly_at_budget_on_a_non_multiple_of_four(self) -> None:
        """m-1 (R2-S4-review §2): the conservative direction rounds UP to the
        boundary, never under it -- 2997 chars = 749.25 tokens, ceil -> 750,
        exactly at the available budget: the request builds with the full
        output reserve (never shrunk)."""
        request = ContextBuilder(_settings()).build(request_id="req-1", history=[_user("a" * 2997)])
        assert request.max_output_tokens == 200


class TestCatalogSnapshot:
    """§5.1 L253: the injected tool sequence is the fixed catalog for the request."""

    def test_mutating_input_tools_after_build_does_not_affect_request(self) -> None:
        builder = ContextBuilder(_settings())
        spec = _read_tool()
        catalog = [spec]
        request = builder.build(request_id="req-1", history=[_user("hi")], tools=catalog)
        catalog.append(_read_tool())  # caller keeps mutating the injected sequence
        spec.description = "mutated after build"
        assert len(request.tools) == 1
        assert request.tools == [_read_tool()]
        assert request.tools[0].description == "Read a file from the workspace roots."
        assert spec.description == "mutated after build"  # the caller's own mutation survives, isolated

    def test_mutating_nested_input_schema_after_build_does_not_affect_request(self) -> None:
        """m-2 (R2-S4-review §2): the deep in ``model_copy(deep=True)`` is
        load-bearing -- mutating the NESTED ``input_schema`` dict on the
        caller's spec after build must not reach the built request. A shallow
        copy (which still passes the scalar-mutation test above) shares the
        nested dict and fails this pin."""
        builder = ContextBuilder(_settings())
        spec = _read_tool()
        request = builder.build(request_id="req-1", history=[_user("hi")], tools=[spec])
        schema = spec.input_schema
        properties = schema["properties"]
        assert isinstance(properties, dict)  # narrows the JsonValue union for the mutation below
        properties["mutated"] = {"type": "string"}
        required = schema["required"]
        assert isinstance(required, list)
        required.append("mutated")
        assert request.tools[0].input_schema == _read_tool().input_schema
        assert request.tools[0].input_schema["properties"] == {"path": {"type": "string"}}
        # the caller's own nested mutation survives on the caller's object
        assert "mutated" in properties and "mutated" in required


class TestDottedNames:
    """④-3 pinning (T-A-006): request tool_call names are dotted and wire-reversible."""

    def test_tool_call_part_name_is_dotted_and_matches_catalog(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_assistant([_tool_part("call_0000000000000000000000000001")])],
            tools=(_read_tool(),),
        )
        dotted = f"{_read_tool().namespace}.{_read_tool().name}"
        call = request.messages[0].content[0]
        assert isinstance(call, LLMToolCallPart) and call.name == "fs.read" == dotted
        result = request.messages[1].content[0]
        assert isinstance(result, LLMToolResultPart) and result.name == dotted

    def test_dotted_names_round_trip_through_wire_mapping(self) -> None:
        request = ContextBuilder(_settings()).build(
            request_id="req-1",
            history=[_assistant([_tool_part("call_0000000000000000000000000001")])],
            tools=(_read_tool(),),
        )
        names = [
            part.name
            for message in request.messages
            for part in message.content
            if isinstance(part, LLMToolCallPart | LLMToolResultPart)
        ]
        assert names == ["fs.read", "fs.read"]
        for dotted in names:
            wire = tool_wire_name_from_dotted(dotted)
            assert wire == "fs__read"
            assert parse_tool_wire_name(wire) == dotted


class TestRuntimeSettings:
    """OP-1 injection face validation; §7 L339: no guessed context limit."""

    def test_context_limit_has_no_default_host_must_configure(self) -> None:
        values: dict[str, Any] = {
            "model": ModelRef(provider="maas", model="glm-4.6"),
            "output_reserve": 200,
            "timeout_s": 60.0,
        }
        with pytest.raises(TypeError):
            RuntimeSettings(**values)

    def test_settings_require_positive_input_budget(self) -> None:
        with pytest.raises(ValueError, match="context_limit"):
            _settings(context_limit=250, output_reserve=200, safety_margin=50)  # 250 <= 250: zero input budget
        _settings(context_limit=251, output_reserve=200, safety_margin=50)  # one token of budget: legal

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("context_limit", 0),
            ("context_limit", True),
            ("context_limit", 100.0),
            ("output_reserve", 0),
            ("output_reserve", True),
            ("safety_margin", -1),
            ("safety_margin", True),
            ("timeout_s", 0),
            ("timeout_s", -5.0),
            ("timeout_s", True),
            ("temperature", True),
        ],
    )
    def test_invalid_settings_values_rejected(self, field: str, value: Any) -> None:
        with pytest.raises(ValueError):
            _settings(**{field: value})

    def test_timeout_s_must_be_finite(self) -> None:
        """n-2 (R2-S4-review §2): ``> 0`` alone lets ``+inf`` through (an
        unbounded deadline) and only rejected NaN by accident (``nan > 0`` is
        False). The ``math.isfinite`` guard rejects both on purpose."""
        with pytest.raises(ValueError, match="timeout_s"):
            _settings(timeout_s=float("inf"))
        with pytest.raises(ValueError, match="timeout_s"):
            _settings(timeout_s=float("nan"))
