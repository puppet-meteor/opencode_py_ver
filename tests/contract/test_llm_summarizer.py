"""M6-compaction slice-three adapter contract tests (CORE-CONTRACTS §2.4; T-C1-063).

The real ``SummaryGenerator`` adapter (``ProviderSummarizer``) over a scripted
provider stream -- the production half of the §2.4 L126 sentence 「释放锁后生
成摘要」. The contract pins only the ``SummaryResult`` three-field semantics
(text/model/usage), so these tests pin the adapter's own published behavior:

* 文本聚合 -- text deltas aggregate per ``part_id`` in first-seen order into
  the replacement body; reasoning deltas are not body material.
* usage 捕获 -- the LAST ``UsageChunk`` wins (it may arrive after finish,
  §2.3 L96); no usage chunk means unknown (all-null ``Usage``, §2.1), never a
  disguised zero.
* model 透传 + 请求形 -- the request rides the composition's
  ``RuntimeSettings`` face (model / max_output_tokens / temperature /
  timeout_s, the ⑥ same-source ruling) and carries one user message:
  the fixed instruction + a transcript of the covered settled history
  (user / assistant / tool result / idle rows all rendered).
* 非终流 raise -- EOF without a finish or error chunk raises
  ``PROVIDER_STREAM_INTERRUPTED`` (the runner L795-799 shape, retryable).
* 空流 raise -- zero chunks at all is the same interruption, distinctly
  reported.
* ④ 防御分支 -- the provider's terminal ``ErrorChunk`` error rides verbatim;
  tool-call chunks on the no-tools summary request and a finish with an empty
  body raise ``VALIDATION_ERROR`` (the runner ``_GenerationViolation``
  mapping); an empty covered interval is rejected before any model call;
  cancellation propagates untouched (§2.4 L132).

The wiring matrix below the adapter (engine capture/CAS, service trigger,
failure recording) is the slice-one/two face pinned by test_compaction_engine.py
and test_session_compaction.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
    ModelRef,
    ProviderChunk,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolTextContent,
    UserMessage,
)
from opencode.session.compaction import SettledMessage
from opencode.session.context import RuntimeSettings
from opencode.session.llm_summarizer import (
    _SUMMARY_INSTRUCTION,
    ProviderSummarizer,
    SummaryGenerationError,
)

_T0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
_MODEL = ModelRef(provider="maas", model="glm-4.6")


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=_MODEL,
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
        temperature=0.2,
    )


class _Chunks:
    """Chunk factory with auto-ascending stream indexes (the _Script precedent)."""

    def __init__(self) -> None:
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> TextDeltaChunk:
        return TextDeltaChunk(
            type="text_delta", request_id="req_summary", index=self._next(), part_id=part_id, delta=delta
        )

    def reasoning(self, part_id: str, delta: str) -> ReasoningDeltaChunk:
        return ReasoningDeltaChunk(
            type="reasoning_delta", request_id="req_summary", index=self._next(), part_id=part_id, delta=delta
        )

    def call_start(self, call_index: int = 0) -> ToolCallStartChunk:
        return ToolCallStartChunk(
            type="tool_call_start",
            request_id="req_summary",
            index=self._next(),
            call_index=call_index,
            tool_call_id="call_1",
            name="tst.write",
        )

    def usage(self, *, input_tokens: int = 5, output_tokens: int = 7) -> UsageChunk:
        return UsageChunk(
            type="usage",
            request_id="req_summary",
            index=self._next(),
            usage=Usage(input=input_tokens, output=output_tokens),
        )

    def finish(self) -> FinishChunk:
        return FinishChunk(type="finish", request_id="req_summary", index=self._next(), reason=FinishReason.STOP)

    def error(self, error: CoreError) -> ErrorChunk:
        return ErrorChunk(type="error", request_id="req_summary", index=self._next(), error=error)


class _ScriptedProvider:
    """Fake Provider (the test_httpapi_server._ScriptedProvider precedent).

    One scripted chunk stream per request; every request and close recorded.
    An exhausted script list yields an empty stream (the terminal-less shape).
    """

    def __init__(self, scripts: Sequence[Sequence[ProviderChunk]] | None = None) -> None:
        self._scripts = [list(script) for script in scripts] if scripts is not None else []
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self._scripts.pop(0) if self._scripts else []
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        self.close_count += 1


def _user(n: int) -> UserMessage:
    return UserMessage(id=f"msg_u{n:03d}", type="user", created_at=_T0, metadata={}, text=f"turn {n}")


def _assistant(n: int) -> AssistantMessage:
    return AssistantMessage(
        id=f"msg_a{n:03d}",
        type="assistant",
        created_at=_T0,
        metadata={},
        model=_MODEL,
        parts=[
            AssistantTextPart(type="text", part_id="pt", text=f"answer {n}"),
            AssistantToolPart(
                type="tool",
                tool_call_id=f"call_{n}",
                execution_id=f"exe_{n}",
                name="tst.write",
                input={"tag": f"t{n}"},
                state="succeeded",
                effect_status="completed",
                content=[ToolTextContent(type="text", text=f"wrote t{n}")],
            ),
        ],
        status="completed",
    )


def _idle(n: int) -> IdleMessage:
    return IdleMessage(
        id=f"msg_i{n:03d}",
        type="idle",
        created_at=_T0,
        metadata={},
        run_id=f"run_{n:04d}",
        outcome="succeeded",
    )


def _covered(count: int) -> tuple[SettledMessage, ...]:
    """A settled mix -- user -> assistant (tool round) -> idle -- in history order."""
    settled: list[SettledMessage] = []
    for n in range(1, count + 1):
        kind = n % 3
        message = _user(n) if kind == 1 else _assistant(n) if kind == 2 else _idle(n)
        settled.append(SettledMessage(message=message, seq=n))
    return tuple(settled)


# ---------------------------------------------------------------------------
# 文本聚合 + usage 捕获 + model 透传 + 请求形 (the green faces)
# ---------------------------------------------------------------------------


async def test_summarize_aggregates_text_deltas_per_part_and_ignores_reasoning() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider(
        [
            [
                chunks.text("p1", "Sum"),
                chunks.reasoning("r1", "hidden chain"),
                chunks.text("p1", "mary"),
                chunks.text("p2", " of the work"),
                chunks.finish(),
            ]
        ]
    )
    result = await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(3))
    assert result.text == "Summary of the work"
    assert "hidden chain" not in result.text


async def test_summarize_captures_last_usage_and_passes_model_and_request_shape() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider(
        [
            [
                chunks.text("p1", "Summary body."),
                chunks.usage(input_tokens=1, output_tokens=1),
                chunks.usage(input_tokens=31, output_tokens=17),
                chunks.finish(),
            ]
        ]
    )
    settings = _settings()
    result = await ProviderSummarizer(provider=provider, settings=settings).summarize(_covered(4))
    assert result.model == settings.model
    assert result.usage == Usage(input=31, output=17)
    (request,) = provider.requests
    assert request.model == settings.model
    assert request.tools == []
    assert request.max_output_tokens == settings.output_reserve
    assert request.temperature == settings.temperature
    assert request.timeout_s == settings.timeout_s
    assert request.request_id.startswith("compaction_")
    (message,) = request.messages
    assert message.role == "user"
    (part,) = message.content
    assert isinstance(part, LLMTextPart)
    assert part.text.startswith(_SUMMARY_INSTRUCTION)
    # the transcript renders every covered union member (user / assistant /
    # tool result / idle)
    assert "turn 1" in part.text
    assert "wrote t2" in part.text
    assert "run_0003" in part.text


async def test_summarize_reports_unknown_usage_without_a_usage_chunk() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider([[chunks.text("p1", "Summary."), chunks.finish()]])
    result = await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert result.text == "Summary."
    assert result.usage == Usage()
    assert result.usage.input is None


# ---------------------------------------------------------------------------
# 非终流 / 空流 / ErrorChunk / 工具调用块 / 空正文 / 空覆盖 (the raise faces)
# ---------------------------------------------------------------------------


async def test_summarize_raises_without_a_terminal_chunk() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider([[chunks.text("p1", "partial text")]])
    with pytest.raises(SummaryGenerationError) as raised:
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert raised.value.core.code == CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
    assert raised.value.core.retryable is True


async def test_summarize_raises_on_an_empty_stream() -> None:
    provider = _ScriptedProvider([[]])
    with pytest.raises(SummaryGenerationError) as raised:
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert raised.value.core.code == CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
    assert "empty stream" in raised.value.core.message
    assert provider.requests  # the request was still issued; its cost is the provider's


async def test_summarize_raises_the_provider_error_verbatim() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider(
        [[chunks.error(CoreError(code=CoreErrorCode.PROVIDER_UNAVAILABLE, message="upstream gone", retryable=True))]]
    )
    with pytest.raises(SummaryGenerationError) as raised:
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert raised.value.core.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert raised.value.core.message == "upstream gone"
    assert raised.value.core.retryable is True


async def test_summarize_raises_on_tool_call_chunks() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider([[chunks.text("p1", "partial"), chunks.call_start(0)]])
    with pytest.raises(SummaryGenerationError) as raised:
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert raised.value.core.code == CoreErrorCode.VALIDATION_ERROR
    assert raised.value.core.retryable is False


async def test_summarize_raises_when_the_body_is_empty() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider([[chunks.usage(input_tokens=1, output_tokens=1), chunks.finish()]])
    with pytest.raises(SummaryGenerationError) as raised:
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(_covered(2))
    assert raised.value.core.code == CoreErrorCode.VALIDATION_ERROR
    assert raised.value.core.retryable is False


async def test_summarize_rejects_an_empty_covered_interval_before_any_model_call() -> None:
    provider = _ScriptedProvider([[]])
    with pytest.raises(ValueError, match="covered history must not be empty"):
        await ProviderSummarizer(provider=provider, settings=_settings()).summarize(())
    assert provider.requests == []


async def test_summarize_propagates_cancellation() -> None:
    class _CancellingProvider:
        async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
            yield _Chunks().text("p1", "partial")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ProviderSummarizer(provider=_CancellingProvider(), settings=_settings()).summarize(_covered(2))
