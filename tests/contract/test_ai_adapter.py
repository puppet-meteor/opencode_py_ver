"""R2-S3 MAAS adapter contract tests (golden migration of the R0-A evidence).

Reuses the frozen ``tests/r0/fake_openai_server.py`` (import-only) and
migrates the R0-A must-test set onto the contracts face. Per OP-4 the
goldens here necessarily differ from ``doc/evidence/r0/fixtures/``:

- chunks are pydantic wire models (dataclass -> pydantic, provider.md §6-4);
- errors are delivered as the terminal ``error`` chunk instead of a raised
  CoreError (provider.md §6-3, §2.3 L98/L102);
- tool names are reverse-parsed: wire ``fs__read`` -> chunk ``fs.read``
  (provider.md §6-2, reversible mapping).

The deterministic-retry four points (OP-7) are pinned behaviourally:
Retry-After respected, no header -> 0 wait, wait truncated to the remaining
total deadline, and the two-retry cap.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest

from opencode.ai.maas import (
    ATTACHMENT_NOT_LOADED,
    MaasAdapter,
    MaasTimeouts,
    parse_tool_wire_name,
    retry_after_seconds,
    tool_wire_name,
    tool_wire_name_from_dotted,
)
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMMessage,
    LLMReasoningPart,
    LLMTextPart,
    LLMToolCallPart,
    LLMToolResultPart,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolSpec
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    sse_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
    usage_frame,
)

READ_TOOL = ToolSpec(
    namespace="fs",
    name="read",
    version=1,
    description="Read a file (test fixture; never executed)",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


def make_request(
    request_id: str = "req-1",
    *,
    tools: tuple[ToolSpec, ...] = (),
    timeout_s: float = 10.0,
    messages: Sequence[LLMMessage] = (),
    temperature: float | None = None,
) -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        model=ModelRef(provider="maas", model="fake-model"),
        messages=list(messages) or [LLMMessage(role="user", content=[LLMTextPart(type="text", text="hi")])],
        tools=list(tools),
        max_output_tokens=64,
        temperature=temperature,
        timeout_s=timeout_s,
    )


@asynccontextmanager
async def fake_server(responses: list[ScriptedResponse]) -> AsyncIterator[FakeOpenAIServer]:
    server = FakeOpenAIServer(responses)
    await server.start()
    try:
        yield server
    finally:
        await server.aclose()


def make_adapter(server: FakeOpenAIServer, timeouts: MaasTimeouts | None = None) -> MaasAdapter:
    # The OpenAI SDK appends "/chat/completions" to base_url, so the fake
    # (which serves /v1/chat/completions) must be mounted under /v1.
    return MaasAdapter(f"{server.base_url}/v1", api_key="r2s3-test-key", timeouts=timeouts)


async def consume(adapter: MaasAdapter, request: GenerationRequest) -> list[ProviderChunk]:
    chunks: list[ProviderChunk] = [chunk async for chunk in adapter.stream(request)]
    await adapter.close()
    return chunks


def dumps(chunks: Sequence[ProviderChunk]) -> list[dict[str, Any]]:
    return [chunk.model_dump(mode="json") for chunk in chunks]


# ---------------------------------------------------------------------------
# Adapter-level chunk contract (migrated from the R0-A spike test helper)
# ---------------------------------------------------------------------------


class SequenceViolation(Exception):
    """An emitted chunk sequence violates the provider contract."""


def validate_sequence(chunks: Sequence[ProviderChunk], request_id: str, *, require_terminal: bool = True) -> None:
    """Assert the adapter-level chunk contract on an emitted sequence.

    - every chunk carries the originating request_id;
    - index starts at 0 and increases by exactly one (no duplicates, no gaps);
    - at most one terminal chunk (finish or error) and it must be last.

    Per-type required fields are no longer checked here: the pydantic wire
    models enforce them at construction (OP-4 dataclass -> pydantic diff).
    """
    expected_index = 0
    terminal: str | None = None
    for position, chunk in enumerate(chunks):
        if chunk.request_id != request_id:
            raise SequenceViolation(f"position {position}: request_id {chunk.request_id!r} != {request_id!r}")
        if terminal is not None:
            raise SequenceViolation(f"position {position}: {chunk.type} chunk after terminal {terminal}")
        if chunk.index != expected_index:
            raise SequenceViolation(f"position {position}: index {chunk.index} != expected {expected_index}")
        if chunk.type in ("finish", "error"):
            terminal = chunk.type
        expected_index += 1
    if require_terminal and chunks and terminal is None:
        raise SequenceViolation("sequence ended without a terminal chunk")


def _text_chunk(index: int, delta: str = "a", *, request_id: str = "req-1") -> TextDeltaChunk:
    return TextDeltaChunk(request_id=request_id, index=index, type="text_delta", part_id="text-0", delta=delta)


def _finish_chunk(index: int, *, request_id: str = "req-1") -> FinishChunk:
    return FinishChunk(request_id=request_id, index=index, type="finish", reason=FinishReason.STOP)


def _usage_chunk(index: int) -> UsageChunk:
    return UsageChunk(request_id="req-1", index=index, type="usage", usage=Usage(input=1, output=2))


@pytest.mark.parametrize(
    ("chunks", "request_id"),
    [
        pytest.param([_text_chunk(0), _text_chunk(2)], "req-1", id="index-skip"),
        pytest.param([_text_chunk(0), _text_chunk(1), _finish_chunk(1)], "req-1", id="index-duplicate"),
        pytest.param([_text_chunk(0, request_id="req-2")], "req-1", id="request-id-mismatch"),
        pytest.param([_finish_chunk(0), _finish_chunk(1)], "req-1", id="double-terminal"),
        pytest.param([_finish_chunk(0), _text_chunk(1)], "req-1", id="chunk-after-terminal"),
        pytest.param([_text_chunk(0), _text_chunk(1)], "req-1", id="no-terminal"),
    ],
)
def test_validate_sequence_rejects_violations(chunks: list[ProviderChunk], request_id: str) -> None:
    with pytest.raises(SequenceViolation):
        validate_sequence(chunks, request_id)


def test_validate_sequence_accepts_contract_sequence() -> None:
    validate_sequence([_text_chunk(0), _usage_chunk(1), _finish_chunk(2)], "req-1")
    error = ErrorChunk(
        request_id="req-1",
        index=1,
        type="error",
        error=CoreError(code=CoreErrorCode.PROVIDER_STREAM_INTERRUPTED, message="x", retryable=False),
    )
    validate_sequence([_text_chunk(0), error], "req-1")  # an error terminal ends a partial stream legally


# ---------------------------------------------------------------------------
# Golden scenarios (R0 fixtures 01-07 migrated per OP-4)
# ---------------------------------------------------------------------------


async def test_golden_text_usage_after_finish() -> None:
    """Wire order: finish frame, then usage frame; both survive (fixture 01)."""
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(text_delta_frame("Hello")),
                    Frame(text_delta_frame(" world")),
                    Frame(finish_frame("stop")),
                    Frame(usage_frame(12, 34, reasoning=5, cached=2)),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "Hello",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": " world",
        },
        {
            "request_id": "req-1",
            "index": 2,
            "provider_metadata": {},
            "type": "usage",
            "usage": {"input": 12, "output": 34, "reasoning": 5, "cache_read": 2, "cache_write": None},
            "cost_usd": None,
        },
        {
            "request_id": "req-1",
            "index": 3,
            "provider_metadata": {},
            "type": "finish",
            "reason": "stop",
            "raw_reason": "stop",
        },
    ]
    assert len(server.requests) == 1


async def test_golden_text_usage_before_finish() -> None:
    """Wire order: usage frame before finish frame; both survive (fixture 02)."""
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(text_delta_frame("ok")),
                    Frame(usage_frame(3, 4)),
                    Frame(finish_frame("stop")),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "ok",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "usage",
            "usage": {"input": 3, "output": 4, "reasoning": None, "cache_read": None, "cache_write": None},
            "cost_usd": None,
        },
        {
            "request_id": "req-1",
            "index": 2,
            "provider_metadata": {},
            "type": "finish",
            "reason": "stop",
            "raw_reason": "stop",
        },
    ]


async def test_golden_interleaved_tool_calls() -> None:
    """Two interleaved calls with argument sharding (fixture 03); names reverse-parsed."""
    frames = [
        Frame(tool_call_start_frame(0, "call-1", "fs__read")),
        Frame(tool_call_args_frame(0, '{"pa')),
        Frame(tool_call_start_frame(1, "call-2", "fs__read")),
        Frame(tool_call_args_frame(1, '{"path": "/b"}')),
        Frame(tool_call_args_frame(0, 'th": "/a"}')),
        Frame(finish_frame("tool_calls")),
        Frame(usage_frame(9, 11)),
    ]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        chunks = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "tool_call_start",
            "call_index": 0,
            "tool_call_id": "call-1",
            "name": "fs.read",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "tool_call_args_delta",
            "call_index": 0,
            "delta": '{"pa',
        },
        {
            "request_id": "req-1",
            "index": 2,
            "provider_metadata": {},
            "type": "tool_call_start",
            "call_index": 1,
            "tool_call_id": "call-2",
            "name": "fs.read",
        },
        {
            "request_id": "req-1",
            "index": 3,
            "provider_metadata": {},
            "type": "tool_call_args_delta",
            "call_index": 1,
            "delta": '{"path": "/b"}',
        },
        {
            "request_id": "req-1",
            "index": 4,
            "provider_metadata": {},
            "type": "tool_call_args_delta",
            "call_index": 0,
            "delta": 'th": "/a"}',
        },
        {
            "request_id": "req-1",
            "index": 5,
            "provider_metadata": {},
            "type": "usage",
            "usage": {"input": 9, "output": 11, "reasoning": None, "cache_read": None, "cache_write": None},
            "cost_usd": None,
        },
        {
            "request_id": "req-1",
            "index": 6,
            "provider_metadata": {},
            "type": "tool_call_end",
            "call_index": 0,
            "tool_call_id": "call-1",
            "name": "fs.read",
            "raw_arguments": '{"path": "/a"}',
        },
        {
            "request_id": "req-1",
            "index": 7,
            "provider_metadata": {},
            "type": "tool_call_end",
            "call_index": 1,
            "tool_call_id": "call-2",
            "name": "fs.read",
            "raw_arguments": '{"path": "/b"}',
        },
        {
            "request_id": "req-1",
            "index": 8,
            "provider_metadata": {},
            "type": "finish",
            "reason": "tool_calls",
            "raw_reason": "tool_calls",
        },
    ]


async def test_golden_eof_without_terminal() -> None:
    """Clean body end but no [DONE] line and no finish frame (fixture 04): error chunk."""
    async with fake_server([ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)]) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "partial",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "error",
            "error": {
                "code": "ProviderStreamInterrupted",
                "message": "stream ended without a terminal chunk",
                "retryable": False,
                "details": {},
            },
        },
    ]
    assert len(server.requests) == 1


async def test_golden_429_retry_then_success() -> None:
    """429 before first content (retry-after-ms respected), success on retry (fixture 05)."""
    async with fake_server(
        [
            ScriptedResponse(status=429, headers={"retry-after-ms": "200"}),
            ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))]),
        ]
    ) as server:
        started = time.monotonic()
        chunks = await consume(make_adapter(server), make_request())
        elapsed = time.monotonic() - started

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "ok",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "finish",
            "reason": "stop",
            "raw_reason": "stop",
        },
    ]
    assert len(server.requests) == 2
    # OP-7 deterministic wait: at least the Retry-After budget elapsed, and no
    # jittered/multiplied backoff (which would overshoot well past 0.6 s).
    assert elapsed >= 0.19
    assert elapsed < 0.6


async def test_golden_wire_double_finish_rejected() -> None:
    """Two finish frames on the wire fail the whole stream (fixture 06); error chunk."""
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(text_delta_frame("a")),
                    Frame(finish_frame("stop")),
                    Frame(finish_frame("tool_calls")),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "a",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "error",
            "error": {
                "code": "ValidationError",
                "message": "provider stream sent a duplicate finish frame",
                "retryable": False,
                "details": {"violation": "duplicate_finish"},
            },
        },
    ]
    assert len(server.requests) == 1  # protocol violations are never retried


async def test_golden_wire_content_after_finish_rejected() -> None:
    """Text after a finish frame is rejected (fixture 07): held-back finish discarded."""
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(text_delta_frame("a")),
                    Frame(finish_frame("stop")),
                    Frame(text_delta_frame("b")),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert dumps(chunks) == [
        {
            "request_id": "req-1",
            "index": 0,
            "provider_metadata": {},
            "type": "text_delta",
            "part_id": "text-0",
            "delta": "a",
        },
        {
            "request_id": "req-1",
            "index": 1,
            "provider_metadata": {},
            "type": "error",
            "error": {
                "code": "ValidationError",
                "message": "provider stream sent content after the finish frame",
                "retryable": False,
                "details": {"violation": "content_after_finish"},
            },
        },
    ]
    assert len(server.requests) == 1


async def test_golden_wire_tool_call_after_finish_rejected() -> None:
    """Tool-call content after a finish frame is rejected like late text."""
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(tool_call_start_frame(0, "call-1", "fs__read")),
                    Frame(finish_frame("stop")),
                    Frame(tool_call_args_frame(0, '{"path": "/a"}')),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["tool_call_start", "error"]  # no tool_call_end, no finish
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.VALIDATION_ERROR
    assert terminal.error.details == {"violation": "content_after_finish"}
    assert len(server.requests) == 1


async def test_error_chunk_is_the_sole_terminal() -> None:
    """§2.3 L98/L102: every error scenario ends in exactly one error chunk, last."""
    scenarios: list[list[ScriptedResponse]] = [
        [ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)],
        [
            ScriptedResponse(
                frames=[Frame(text_delta_frame("a")), Frame(finish_frame("stop")), Frame(finish_frame("tool_calls"))]
            )
        ],
        [
            ScriptedResponse(
                frames=[Frame(text_delta_frame("a")), Frame(finish_frame("stop")), Frame(text_delta_frame("b"))]
            )
        ],
        [ScriptedResponse(status=429, headers={"retry-after-ms": "1"}) for _ in range(3)],
    ]
    for responses in scenarios:
        async with fake_server(responses) as server:
            chunks = await consume(make_adapter(server), make_request())

        validate_sequence(chunks, "req-1")  # error counts as the single terminal
        assert isinstance(chunks[-1], ErrorChunk)
        assert [chunk.type for chunk in chunks].count("error") == 1
        assert not [chunk for chunk in chunks if chunk.type == "finish"]  # held-back finish never lands


# ---------------------------------------------------------------------------
# Timeout kinds: header / chunk / total (provider.md §3 mapping table)
# ---------------------------------------------------------------------------


async def test_header_timeout() -> None:
    async with fake_server([ScriptedResponse(hang_headers=True)]) as server:
        chunks = await consume(
            make_adapter(server, MaasTimeouts(header_s=0.3, chunk_s=5.0)),
            make_request(timeout_s=10.0),
        )

    validate_sequence(chunks, "req-1")
    assert len(chunks) == 1
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert terminal.error.retryable is False
    assert terminal.error.details == {"timeout_kind": "header"}
    assert len(server.requests) == 1


async def test_chunk_timeout() -> None:
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("a"))], terminate=False, hang_after_frames=True)]
    ) as server:
        chunks = await consume(
            make_adapter(server, MaasTimeouts(header_s=5.0, chunk_s=0.4)),
            make_request(timeout_s=10.0),
        )

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["text_delta", "error"]
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert terminal.error.details == {"timeout_kind": "chunk"}
    assert len(server.requests) == 1


async def test_total_deadline_mid_stream() -> None:
    frames = [Frame(text_delta_frame("x"), delay_s=0.15) for _ in range(40)]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        chunks = await consume(
            make_adapter(server, MaasTimeouts(header_s=5.0, chunk_s=5.0)),
            make_request(timeout_s=0.5),
        )

    validate_sequence(chunks, "req-1")
    delivered = [chunk for chunk in chunks if chunk.type == "text_delta"]
    assert 1 <= len(delivered) <= 4  # partial progress, then the deadline hit
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert terminal.error.details == {"timeout_kind": "total"}
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Deterministic retry (§8 L350, OP-7 ruling: min(retry_after, remaining), no jitter)
# ---------------------------------------------------------------------------


async def test_retry_without_retry_after_header_waits_zero() -> None:
    """No Retry-After header: the retry wait is 0 (OP-7), bounded by nothing."""
    async with fake_server(
        [
            ScriptedResponse(status=429),
            ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))]),
        ]
    ) as server:
        started = time.monotonic()
        chunks = await consume(make_adapter(server), make_request())
        elapsed = time.monotonic() - started

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["text_delta", "finish"]
    assert len(server.requests) == 2
    assert elapsed < 0.5  # no Retry-After -> no wait (jitter/backoff would overshoot)


async def test_retry_wait_truncated_to_total_deadline() -> None:
    """Retry-After larger than the remaining budget sleeps only the remainder."""
    async with fake_server(
        [
            ScriptedResponse(status=429, headers={"retry-after-ms": "5000"}),
            ScriptedResponse(frames=[Frame(text_delta_frame("x"), delay_s=0.2), Frame(finish_frame("stop"))]),
        ]
    ) as server:
        started = time.monotonic()
        chunks = await consume(make_adapter(server), make_request(timeout_s=0.4))
        elapsed = time.monotonic() - started

    validate_sequence(chunks, "req-1")
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert terminal.error.details.get("timeout_kind") == "total"
    assert len(server.requests) == 2  # the retry was issued once; the deadline killed it
    assert elapsed < 1.0  # 5 s Retry-After was truncated to the remaining budget


async def test_retry_exhaustion_caps_at_two_retries() -> None:
    async with fake_server([ScriptedResponse(status=429, headers={"retry-after-ms": "1"}) for _ in range(3)]) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["error"]
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_UNAVAILABLE
    assert terminal.error.retryable is True  # the upper layer may still retry
    assert terminal.error.details == {"attempts": 3}  # 1 initial + 2 retries
    assert len(server.requests) == 3


async def test_no_retry_after_first_content() -> None:
    """Content streamed, then the stream ends with no terminal frame: no restart.

    The second scripted 429 would be consumed only if the adapter wrongly
    re-issued the request after content (forbidden: no restart, no splicing).
    """
    async with fake_server(
        [
            ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True),
            ScriptedResponse(status=429, headers={"retry-after-ms": "1"}),
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["text_delta", "error"]
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Wire tool-name mapping (provider.md §6-2: fs__read <-> (fs, read) <-> fs.read)
# ---------------------------------------------------------------------------


def test_wire_tool_name_mapping_round_trip() -> None:
    assert tool_wire_name("fs", "read") == "fs__read"
    assert parse_tool_wire_name("fs__read") == "fs.read"
    assert tool_wire_name_from_dotted("fs.read") == "fs__read"
    # Round trips for separator-free names on either side:
    assert tool_wire_name_from_dotted(parse_tool_wire_name("fs__read")) == "fs__read"
    assert parse_tool_wire_name(tool_wire_name_from_dotted("fs.read")) == "fs.read"
    # Un-parseable wire names pass through unchanged (never guessed):
    assert parse_tool_wire_name("read") == "read"
    assert parse_tool_wire_name("fs__") == "fs__"
    assert parse_tool_wire_name("__read") == "__read"
    assert tool_wire_name_from_dotted("read") == "read"


async def test_unparseable_wire_name_passes_through() -> None:
    """A wire name without the namespace separator is surfaced as-is."""
    frames = [
        Frame(tool_call_start_frame(0, "call-1", "read")),
        Frame(tool_call_args_frame(0, '{"path": "/a"}')),
        Frame(finish_frame("tool_calls")),
    ]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    ends = [chunk for chunk in chunks if chunk.type == "tool_call_end"]
    assert len(ends) == 1
    assert ends[0].name == "read"  # passthrough; unknown-tool handling is downstream (§2.3 L100)


async def test_tool_call_without_id_is_rejected() -> None:
    """A tool call that never receives an id cannot form tool_call_end: rejected."""

    def start_frame_without_id(call_index: int, name: str) -> bytes:
        body = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": call_index,
                                "id": None,
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        return sse_frame(body)

    frames = [
        Frame(start_frame_without_id(0, "fs__read")),
        Frame(tool_call_args_frame(0, '{"path": "/a"}')),
        Frame(finish_frame("tool_calls")),
    ]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        chunks = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    validate_sequence(chunks, "req-1")
    assert [chunk.type for chunk in chunks] == ["tool_call_start", "tool_call_args_delta", "error"]
    terminal = chunks[-1]
    assert isinstance(terminal, ErrorChunk)
    assert terminal.error.code == CoreErrorCode.VALIDATION_ERROR
    assert terminal.error.details == {"violation": "tool_call_without_id"}
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Cancellation, close and resource cleanup (§2.2 L84, §2.3 L102)
# ---------------------------------------------------------------------------


async def test_cancellation_propagates_and_cleans_up() -> None:
    frames = [Frame(text_delta_frame("x"), delay_s=0.05) for _ in range(60)]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        adapter = make_adapter(server)
        before = {id(task) for task in asyncio.all_tasks()}
        request = make_request(timeout_s=30.0)

        async def collect() -> list[ProviderChunk]:
            return [chunk async for chunk in adapter.stream(request)]

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)  # mid-stream: a few chunks already delivered
        task.cancel()
        with pytest.raises(asyncio.CancelledError):  # cancellation is never an error chunk
            await task
        await adapter.close()
        await server.aclose()
        await asyncio.sleep(0.15)  # let connection handlers settle

        leftovers = [t for t in asyncio.all_tasks() if id(t) not in before and t is not asyncio.current_task()]
        assert not leftovers  # no leaked reader/connection/background tasks


async def test_stream_generator_supports_early_close() -> None:
    frames = [Frame(text_delta_frame("x"), delay_s=0.05) for _ in range(40)]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        adapter = make_adapter(server)
        before = {id(task) for task in asyncio.all_tasks()}
        stream = adapter.stream(make_request(timeout_s=30.0))
        seen = 0
        async for _chunk in stream:
            seen += 1
            if seen >= 2:
                break
        await stream.aclose()  # §2.2 L84: implementations must support closing
        await adapter.close()

    # Settle after server close, like the spike's cancellation test: the fake
    # server's IOCP accept task needs loop passes to unwind after cancellation
    # (Windows proactor artifact of the test harness, not an adapter leak).
    await asyncio.sleep(0.15)

    leftovers = [t for t in asyncio.all_tasks() if id(t) not in before and t is not asyncio.current_task()]
    assert not leftovers
    assert seen >= 1


# ---------------------------------------------------------------------------
# SDK boundary (A-PROVIDER red line: SDK types never cross the adapter)
# ---------------------------------------------------------------------------


def _assert_json_primitives(value: Any) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, dict):
        for item in value.values():
            _assert_json_primitives(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_primitives(item)
        return
    raise AssertionError(f"non-JSON value crossed the adapter: {type(value).__module__}.{type(value).__name__}")


async def test_sdk_types_do_not_leak() -> None:
    async with fake_server(
        [
            ScriptedResponse(
                frames=[
                    Frame(text_delta_frame("ok")),
                    Frame(tool_call_start_frame(0, "call-1", "fs__read")),
                    Frame(tool_call_args_frame(0, '{"path": "/a"}')),
                    Frame(finish_frame("tool_calls")),
                    Frame(usage_frame(1, 2)),
                ]
            )
        ]
    ) as server:
        chunks = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    assert chunks
    for chunk in chunks:
        assert type(chunk).__module__ == "opencode.contracts.llm"
        dumped = chunk.model_dump(mode="json")
        json.dumps(dumped)  # JSON-round-trippable: no SDK objects ride along
        _assert_json_primitives(dumped)


# ---------------------------------------------------------------------------
# Request assembly (§2.2 L80: the adapter builds the OpenAI wire messages)
# ---------------------------------------------------------------------------


async def test_wire_body_message_assembly() -> None:
    messages = [
        LLMMessage(role="system", content=[LLMTextPart(type="text", text="You are terse.")]),
        LLMMessage(role="user", content=[LLMTextPart(type="text", text="hi")]),
        LLMMessage(
            role="assistant",
            content=[
                LLMReasoningPart(type="reasoning", text="pondering"),  # no wire slot: not sent back
                LLMTextPart(type="text", text="reading now"),
                LLMToolCallPart(type="tool_call", tool_call_id="call-1", name="fs.read", input={"path": "/a"}),
            ],
        ),
        LLMMessage(
            role="tool",
            content=[
                LLMToolResultPart(
                    type="tool_result",
                    tool_call_id="call-1",
                    name="fs.read",
                    content=[ToolTextContent(type="text", text="file body")],
                ),
            ],
        ),
        LLMMessage(role="user", content=[LLMTextPart(type="text", text="and now?")]),
    ]
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))])]
    ) as server:
        chunks = await consume(make_adapter(server), make_request(messages=messages, tools=(READ_TOOL,)))

    validate_sequence(chunks, "req-1")
    body = server.requests[0].body
    assert body is not None
    assert body["messages"] == [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "reading now",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "fs__read", "arguments": '{"path": "/a"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file body"},
        {"role": "user", "content": "and now?"},
    ]


async def test_file_content_replaced_with_placeholder() -> None:
    """§2.1 L72: with media disabled the file URI never reaches the model."""
    messages = [
        LLMMessage(
            role="tool",
            content=[
                LLMToolResultPart(
                    type="tool_result",
                    tool_call_id="call-1",
                    name="fs.read",
                    content=[
                        ToolFileContent(type="file", uri="file:///restricted/secret.txt", mime="text/plain"),
                        ToolTextContent(type="text", text="summary"),
                    ],
                ),
            ],
        ),
    ]
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))])]
    ) as server:
        chunks = await consume(make_adapter(server), make_request(messages=messages))

    validate_sequence(chunks, "req-1")
    body = server.requests[0].body
    assert body is not None
    assert body["messages"] == [
        {"role": "tool", "tool_call_id": "call-1", "content": f"{ATTACHMENT_NOT_LOADED}\nsummary"}
    ]


async def test_wire_body_request_params() -> None:
    """include_usage passthrough (§8.1 L109), max_tokens, temperature, wire tool names."""
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))])]
    ) as server:
        chunks = await consume(make_adapter(server), make_request(tools=(READ_TOOL,), temperature=0.2))

    validate_sequence(chunks, "req-1")
    body = server.requests[0].body
    assert body is not None
    assert body["model"] == "fake-model"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.2
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "fs__read",
                "description": READ_TOOL.description,
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            },
        }
    ]


async def test_temperature_omitted_when_unset() -> None:
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))])]
    ) as server:
        chunks = await consume(make_adapter(server), make_request())

    validate_sequence(chunks, "req-1")
    body = server.requests[0].body
    assert body is not None
    assert "temperature" not in body
    assert "tools" not in body  # no tools this generation is a state, not a field (§2.2 L78)


# ---------------------------------------------------------------------------
# Retry-After parsing unit (TS executor.ts:22-35)
# ---------------------------------------------------------------------------


def test_retry_after_seconds_prefers_ms_header() -> None:
    assert retry_after_seconds({"retry-after-ms": "150"}) == 0.15
    assert retry_after_seconds({"retry-after": "2"}) == 2.0
    assert retry_after_seconds({"retry-after-ms": "150", "retry-after": "2"}) == 0.15
    assert retry_after_seconds({"retry-after-ms": "abc", "retry-after": "3"}) == 3.0
    assert retry_after_seconds({"retry-after": "-1"}) is None
    assert retry_after_seconds({}) is None
