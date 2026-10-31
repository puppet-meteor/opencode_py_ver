"""R0-A provider contract tests against the scripted fake MAAS server.

Covers the R0-A must-test set from doc/STEP-01-R0.md: text deltas, usage
before/after finish, two interleaved tool calls with argument sharding, EOF
without a terminal chunk, 429 with Retry-After (retry before first content,
no retry after first content), header/chunk/total timeouts, and cancellation.

R2 handover fixtures (error shards, duplicate tool IDs, incomplete JSON,
unknown tool names, schema-invalid arguments) are intentionally NOT here; see
doc/evidence/r0/provider.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tools.r0.provider_spike import (
    ChunkType,
    CoreError,
    GenerationRequest,
    LLMMessage,
    MaasSpikeAdapter,
    ModelRef,
    ProviderChunk,
    SpikeTimeouts,
    ToolSpec,
    Usage,
    retry_after_seconds,
)

from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
    usage_frame,
)

READ_TOOL = ToolSpec(
    namespace="fs",
    name="read",
    version="1",
    description="Read a file (R0 fixture; never executed)",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    side_effects="read",
)


def make_request(
    request_id: str = "req-1",
    *,
    tools: tuple[ToolSpec, ...] = (),
    timeout_s: float = 10.0,
) -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        model=ModelRef("maas", "fake-model"),
        messages=(LLMMessage(role="user", text="hi"),),
        tools=tools,
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


def make_adapter(server: FakeOpenAIServer, timeouts: SpikeTimeouts | None = None) -> MaasSpikeAdapter:
    # The OpenAI SDK appends "/chat/completions" to base_url, so the fake
    # (which serves /v1/chat/completions) must be mounted under /v1.
    return MaasSpikeAdapter(f"{server.base_url}/v1", api_key="r0-test-key", timeouts=timeouts)


async def consume(
    adapter: MaasSpikeAdapter, request: GenerationRequest
) -> tuple[list[ProviderChunk], CoreError | None]:
    chunks: list[ProviderChunk] = []
    try:
        async for chunk in adapter.stream(request):
            chunks.append(chunk)
    except CoreError as error:
        return chunks, error
    finally:
        await adapter.close()
    return chunks, None


REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "text_delta": ("part_id", "delta"),
    "reasoning_delta": ("part_id", "delta"),
    "tool_call_start": ("call_index",),
    "tool_call_args_delta": ("call_index", "delta"),
    "tool_call_end": ("call_index", "tool_call_id", "name", "raw_arguments"),
    "usage": ("usage",),
    "finish": ("reason",),
    "error": ("error",),
}


class SequenceViolation(Exception):
    """An emitted chunk sequence violates the provider contract."""


def validate_sequence(chunks: Sequence[ProviderChunk], request_id: str, *, require_terminal: bool = True) -> None:
    """Assert the adapter-level chunk contract on an emitted sequence.

    - every chunk carries the originating request_id;
    - index starts at 0 and increases by exactly one (no duplicates, no gaps);
    - per-type required fields are present;
    - at most one terminal chunk (finish or error) and it must be last.
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
        missing = [name for name in REQUIRED_FIELDS[chunk.type] if getattr(chunk, name) is None]
        if missing:
            raise SequenceViolation(f"position {position}: {chunk.type} missing required fields {missing}")
        if chunk.type in ("finish", "error"):
            terminal = chunk.type
        expected_index += 1
    if require_terminal and chunks and terminal is None:
        raise SequenceViolation("sequence ended without a terminal chunk")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_text_stream_usage_after_finish() -> None:
    """Wire order: finish frame, then usage frame. Both must survive."""
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
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is None
    validate_sequence(chunks, "req-1")
    assert "".join(c.delta or "" for c in chunks if c.type == "text_delta") == "Hello world"
    usage = next(c for c in chunks if c.type == "usage")
    assert usage.usage == Usage(input_tokens=12, output_tokens=34, reasoning_tokens=5, cache_read_tokens=2)
    finish = next(c for c in chunks if c.type == "finish")
    assert (finish.reason, finish.raw_reason) == ("stop", "stop")
    # The logical finish is held back to the end so usage survives both orders.
    assert chunks[-1].type == "finish"


async def test_text_stream_usage_before_finish() -> None:
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
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is None
    validate_sequence(chunks, "req-1")
    assert [c.type for c in chunks] == ["text_delta", "usage", "finish"]


async def test_interleaved_tool_calls_with_argument_shards() -> None:
    """Two calls interleave on the wire; full args parse only at call end."""
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
        chunks, error = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    assert error is None
    validate_sequence(chunks, "req-1")
    starts = [c for c in chunks if c.type == "tool_call_start"]
    assert [c.call_index for c in starts] == [0, 1]
    shards = [c for c in chunks if c.type == "tool_call_args_delta"]
    assert [(c.call_index, c.delta) for c in shards] == [
        (0, '{"pa'),
        (1, '{"path": "/b"}'),
        (0, 'th": "/a"}'),
    ]
    ends = [c for c in chunks if c.type == "tool_call_end"]
    assert [c.call_index for c in ends] == [0, 1]  # aggregated by call_index
    assert ends[0].tool_call_id == "call-1"
    assert ends[0].name == "fs__read"
    assert ends[0].raw_arguments == '{"path": "/a"}'
    assert ends[1].tool_call_id == "call-2"
    assert ends[1].raw_arguments == '{"path": "/b"}'
    finish = next(c for c in chunks if c.type == "finish")
    assert (finish.reason, finish.raw_reason) == ("tool_calls", "tool_calls")
    assert not [c for c in chunks if c.type == "text_delta"]
    assert chunks[-1].type == "finish"


# ---------------------------------------------------------------------------
# Adapter-level wire-protocol rejection (STEP-01-R0 L86: duplicate or
# out-of-order chunks must be rejected, never normalized)
# ---------------------------------------------------------------------------


async def test_wire_double_finish_is_rejected() -> None:
    """A duplicated finish frame on the wire must fail the whole stream.

    STEP-01-R0 L86 rejects duplicate terminals instead of merging them, so
    the consumer sees a protocol error and no finish terminal at all.
    """
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
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is not None
    assert error.code == "ValidationError"
    assert error.details["violation"] == "duplicate_finish"
    validate_sequence(chunks, "req-1", require_terminal=False)
    assert not [c for c in chunks if c.type == "finish"]  # held-back finish discarded
    assert len(server.requests) == 1  # protocol violations are never retried


async def test_wire_text_after_finish_is_rejected() -> None:
    """Text arriving after a finish frame is an out-of-order violation.

    The stream fails with a protocol error instead of appending the late
    content; the held-back finish is discarded (error supersedes finish).
    """
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
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is not None
    assert error.code == "ValidationError"
    assert error.details["violation"] == "content_after_finish"
    validate_sequence(chunks, "req-1", require_terminal=False)
    assert [c.type for c in chunks] == ["text_delta"]  # only pre-finish content survives
    assert "".join(c.delta or "" for c in chunks if c.type == "text_delta") == "a"
    assert len(server.requests) == 1


async def test_wire_tool_call_after_finish_is_rejected() -> None:
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
        chunks, error = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    assert error is not None
    assert error.code == "ValidationError"
    assert error.details["violation"] == "content_after_finish"
    validate_sequence(chunks, "req-1", require_terminal=False)
    assert [c.type for c in chunks] == ["tool_call_start"]  # no tool_call_end, no finish
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# EOF and 429 retry rules
# ---------------------------------------------------------------------------


async def test_eof_without_terminal_is_interrupted() -> None:
    async with fake_server([ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)]) as server:
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is not None
    assert error.code == "ProviderStreamInterrupted"
    assert len(server.requests) == 1
    assert [c.type for c in chunks] == ["text_delta"]


async def test_429_before_content_retries_then_succeeds() -> None:
    async with fake_server(
        [
            ScriptedResponse(status=429, headers={"retry-after-ms": "80"}),
            ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))]),
        ]
    ) as server:
        started = time.monotonic()
        chunks, error = await consume(make_adapter(server), make_request())
        elapsed = time.monotonic() - started

    assert error is None
    validate_sequence(chunks, "req-1")
    assert len(server.requests) == 2
    assert elapsed >= 0.07  # Retry-After (80 ms) was respected before the retry


async def test_429_before_content_retry_exhaustion() -> None:
    async with fake_server(
        [ScriptedResponse(status=429, headers={"retry-after-ms": "10"}) for _ in range(3)]
    ) as server:
        _chunks, error = await consume(make_adapter(server), make_request())

    assert error is not None
    assert error.code == "ProviderUnavailable"
    assert error.details["attempts"] == 3  # 1 initial + 2 retries
    assert len(server.requests) == 3


async def test_no_retry_after_first_content() -> None:
    """Content streamed, then the stream ends cleanly with no terminal frame.

    The second scripted 429 would be consumed only if the adapter wrongly
    re-issued the request after content (forbidden: no restart, no splicing).
    """
    async with fake_server(
        [
            ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True),
            ScriptedResponse(status=429, headers={"retry-after-ms": "1"}),
        ]
    ) as server:
        chunks, error = await consume(make_adapter(server), make_request())

    assert error is not None
    assert error.code == "ProviderStreamInterrupted"
    assert len(server.requests) == 1
    assert [c.type for c in chunks] == ["text_delta"]


# ---------------------------------------------------------------------------
# Timeout kinds: header / chunk / total
# ---------------------------------------------------------------------------


async def test_header_timeout() -> None:
    async with fake_server([ScriptedResponse(hang_headers=True)]) as server:
        chunks, error = await consume(
            make_adapter(server, SpikeTimeouts(header_s=0.3, chunk_s=5.0)),
            make_request(timeout_s=10.0),
        )

    assert error is not None
    assert error.code == "ProviderUnavailable"
    assert error.details["timeout_kind"] == "header"
    assert len(server.requests) == 1
    assert chunks == []


async def test_chunk_timeout() -> None:
    async with fake_server(
        [ScriptedResponse(frames=[Frame(text_delta_frame("a"))], terminate=False, hang_after_frames=True)]
    ) as server:
        chunks, error = await consume(
            make_adapter(server, SpikeTimeouts(header_s=5.0, chunk_s=0.4)),
            make_request(timeout_s=10.0),
        )

    assert error is not None
    assert error.code == "ProviderUnavailable"
    assert error.details["timeout_kind"] == "chunk"
    assert len(server.requests) == 1
    assert [c.type for c in chunks] == ["text_delta"]


async def test_total_deadline() -> None:
    frames = [Frame(text_delta_frame("x"), delay_s=0.15) for _ in range(40)]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        chunks, error = await consume(
            make_adapter(server, SpikeTimeouts(header_s=5.0, chunk_s=5.0)),
            make_request(timeout_s=0.5),
        )

    assert error is not None
    assert error.code == "ProviderUnavailable"
    assert error.details["timeout_kind"] == "total"
    assert len(server.requests) == 1
    delivered = [c for c in chunks if c.type == "text_delta"]
    assert 1 <= len(delivered) <= 4  # partial progress, then the deadline hit


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancellation_propagates_and_cleans_up() -> None:
    frames = [Frame(text_delta_frame("x"), delay_s=0.05) for _ in range(60)]
    async with fake_server([ScriptedResponse(frames=frames)]) as server:
        adapter = make_adapter(server)
        before = {id(t) for t in asyncio.all_tasks()}

        async def collect() -> list[ProviderChunk]:
            out: list[ProviderChunk] = []
            async for chunk in adapter.stream(adapter_request):
                out.append(chunk)
            return out

        adapter_request = make_request(timeout_s=30.0)
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.2)  # mid-stream: a few chunks already delivered
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await adapter.close()
        await server.aclose()
        await asyncio.sleep(0.15)  # let connection handlers settle

        leftovers = [t for t in asyncio.all_tasks() if id(t) not in before and t is not asyncio.current_task()]
        assert not leftovers  # no leaked reader/connection/background tasks


# ---------------------------------------------------------------------------
# Unit tests: sequence validator + Retry-After parsing
# ---------------------------------------------------------------------------


def _chunk(index: int, chunk_type: ChunkType, *, request_id: str = "req-1", **fields: Any) -> ProviderChunk:
    return ProviderChunk(request_id=request_id, index=index, type=chunk_type, **fields)


@pytest.mark.parametrize(
    ("chunks", "request_id"),
    [
        pytest.param(
            [
                _chunk(0, "text_delta", part_id="text-0", delta="a"),
                _chunk(2, "text_delta", part_id="text-0", delta="b"),
            ],
            "req-1",
            id="index-skip",
        ),
        pytest.param(
            [
                _chunk(0, "text_delta", part_id="text-0", delta="a"),
                _chunk(1, "text_delta", part_id="text-0", delta="b"),
                _chunk(1, "finish", reason="stop"),
            ],
            "req-1",
            id="index-duplicate",
        ),
        pytest.param(
            [_chunk(0, "text_delta", request_id="req-2", part_id="text-0", delta="a")],
            "req-1",
            id="request-id-mismatch",
        ),
        pytest.param(
            [_chunk(0, "finish", reason="stop"), _chunk(1, "finish", reason="stop")], "req-1", id="double-terminal"
        ),
        pytest.param(
            [_chunk(0, "finish", reason="stop"), _chunk(1, "text_delta", part_id="text-0", delta="a")],
            "req-1",
            id="chunk-after-terminal",
        ),
        pytest.param([_chunk(0, "usage")], "req-1", id="missing-required-field"),
        pytest.param([_chunk(0, "text_delta", part_id="text-0", delta="a")], "req-1", id="no-terminal"),
    ],
)
def test_validate_sequence_rejects_violations(chunks: list[ProviderChunk], request_id: str) -> None:
    with pytest.raises(SequenceViolation):
        validate_sequence(chunks, request_id)


def test_validate_sequence_accepts_contract_sequence() -> None:
    chunks = [
        _chunk(0, "text_delta", part_id="text-0", delta="a"),
        _chunk(1, "usage", usage=Usage(input_tokens=1, output_tokens=2)),
        _chunk(2, "finish", reason="stop"),
    ]
    validate_sequence(chunks, "req-1")


def test_retry_after_seconds_prefers_ms_header() -> None:
    assert retry_after_seconds({"retry-after-ms": "150"}) == 0.15
    assert retry_after_seconds({"retry-after": "2"}) == 2.0
    assert retry_after_seconds({"retry-after-ms": "150", "retry-after": "2"}) == 0.15
    assert retry_after_seconds({"retry-after-ms": "abc", "retry-after": "3"}) == 3.0
    assert retry_after_seconds({"retry-after": "-1"}) is None
    assert retry_after_seconds({}) is None


# ---------------------------------------------------------------------------
# Boundary: SDK types must not leak above the adapter
# ---------------------------------------------------------------------------


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
        chunks, error = await consume(make_adapter(server), make_request(tools=(READ_TOOL,)))

    assert error is None
    assert chunks
    for chunk in chunks:
        assert type(chunk) is ProviderChunk
        for field in dataclasses.fields(chunk):
            value = getattr(chunk, field.name)
            module = type(value).__module__
            assert not module.startswith(("openai", "httpx")), f"{field.name} leaks {module}"
    assert all(isinstance(c.provider_metadata, dict) for c in chunks)
