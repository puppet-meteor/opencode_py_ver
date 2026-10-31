"""R0-A provider spike: project-owned chunk types + OpenAI-compatible MAAS adapter.

Spike placement per doc/STEP-01-R0.md: this module lives under tools/r0 and is
NOT production code; R2 rebuilds the formal version under src/opencode/ai/
per CORE-CONTRACTS.md section 2. OpenAI SDK objects never cross the adapter
boundary - callers only see ProviderChunk / CoreError.

Behaviour anchors (see doc/evidence/r0/provider.md for the full mapping):
- packages/core/src/aisdk.ts:65-120  chunk-idle timeout around SSE reads
- packages/core/src/aisdk.ts:132-169 header timer cleared once headers arrive,
  combined abort signals for header/chunk/total
- packages/ai/src/route/transport/http.ts:94-140 distinct header/chunk/total
  timeouts with distinct messages
- packages/ai/src/route/executor.ts:22-35  Retry-After parsing (ms first, then
  seconds; HTTP-date not implemented in the spike)
- packages/core/src/model-retry.ts:62-103 retry only when the provider clearly
  did not run the request; never retry once content arrived
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx2
import openai
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Errors: spike subset of CORE-CONTRACTS section 8
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    "ValidationError",
    "ProviderUnavailable",
    "ProviderStreamInterrupted",
]


class CoreError(Exception):
    """Spike subset of the CORE-CONTRACTS error model."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# S1-shaped types (spike dataclasses; formal pydantic models arrive at R2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


ChunkType = Literal[
    "text_delta",
    "reasoning_delta",
    "tool_call_start",
    "tool_call_args_delta",
    "tool_call_end",
    "usage",
    "finish",
    "error",
]


@dataclass(frozen=True)
class ProviderChunk:
    """CORE-CONTRACTS section 2.3 provider chunk.

    Per-type required fields are enforced by sequence validation (tests).
    The adapter assigns ``index`` strictly increasing from 0 and stamps every
    chunk with the originating ``request_id``.
    """

    request_id: str
    index: int
    type: ChunkType
    part_id: str | None = None
    delta: str | None = None
    call_index: int | None = None
    tool_call_id: str | None = None
    name: str | None = None
    raw_arguments: str | None = None
    usage: Usage | None = None
    reason: str | None = None
    raw_reason: str | None = None
    error: CoreError | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str


@dataclass(frozen=True)
class ToolSpec:
    namespace: str
    name: str
    version: str
    description: str
    input_schema: Mapping[str, Any]
    side_effects: str  # "none" | "read" | "write"
    idempotency_support: bool = False

    @property
    def wire_name(self) -> str:
        # OpenAI function names must match ^[a-zA-Z0-9_-]{1,64}$: the namespace
        # dot cannot survive, so use a reversible double-underscore join.
        return f"{self.namespace}__{self.name}"


@dataclass(frozen=True)
class ToolCallData:
    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    text: str | None = None
    tool_calls: tuple[ToolCallData, ...] = ()
    tool_call_id: str | None = None
    tool_result: str | None = None


@dataclass(frozen=True)
class GenerationRequest:
    request_id: str
    model: ModelRef
    messages: tuple[LLMMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout_s: float = 120.0


class Provider(Protocol):
    def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]: ...


# ---------------------------------------------------------------------------
# Timeout / retry plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpikeTimeouts:
    header_s: float = 10.0
    chunk_s: float = 30.0


class _RetrySignal(Exception):
    """Raised before any content was produced for a retryable provider failure."""

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__("retryable provider failure before first content")
        self.retry_after_s = retry_after_s


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """TS executor.ts: prefer ``retry-after-ms``, then seconds.

    HTTP-date parsing is intentionally NOT implemented in the spike; the gap
    is recorded in doc/evidence/r0/provider.md.
    """
    raw_ms = headers.get("retry-after-ms")
    if raw_ms is not None:
        try:
            value = float(raw_ms)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value / 1000.0
    raw_seconds = headers.get("retry-after")
    if raw_seconds is not None:
        try:
            value = float(raw_seconds)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return None


def _map_finish_reason(raw: str) -> str:
    if raw == "stop":
        return "stop"
    if raw == "tool_calls":
        return "tool_calls"
    if raw == "length":
        return "length"
    if raw in ("content_filter", "filter"):
        return "filter"
    return "unknown"


def _map_usage(raw: Any) -> Usage:
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    completion_details = getattr(raw, "completion_tokens_details", None)
    return Usage(
        input_tokens=getattr(raw, "prompt_tokens", None),
        output_tokens=getattr(raw, "completion_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None) if completion_details else None,
        cache_read_tokens=getattr(prompt_details, "cached_tokens", None) if prompt_details else None,
        cache_write_tokens=None,  # no OpenAI-compatible field; MAAS evidence pending
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@dataclass
class _ToolAccumulator:
    call_index: int
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass
class _AttemptState:
    next_index: int = 0
    pending_finish: str | None = None
    tools: dict[int, _ToolAccumulator] = field(default_factory=dict)

    def take_index(self) -> int:
        value = self.next_index
        self.next_index += 1
        return value


class MaasSpikeAdapter:
    """OpenAI-compatible MAAS adapter; SDK types stay inside this class.

    Timeout model mapping (TS header/chunk/total -> Python, see provider.md):
    - header: asyncio timeout around the create() call (headers are in hand
      once it returns);
    - chunk:  httpx read timeout on the SDK client (socket-read idle gap,
      including waiting for the first byte - recorded as a known difference
      vs the TS event-granular deadline);
    - total:  per-read asyncio deadline derived from request.timeout_s and
      applied to every iteration, which also bounds 429 retries.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeouts: SpikeTimeouts | None = None,
        max_retries: int = 2,
    ) -> None:
        effective = timeouts or SpikeTimeouts()
        self._timeouts = effective
        self._max_retries = max_retries
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,  # retry policy belongs to this adapter, not the SDK
            timeout=httpx2.Timeout(
                connect=effective.header_s,
                read=effective.chunk_s,
                write=effective.header_s,
                pool=effective.header_s,
            ),
        )

    async def close(self) -> None:
        await self._client.close()

    def _openai_messages(self, request: GenerationRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                out.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.tool_result or ""})
            elif message.role == "assistant" and message.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": message.text,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments_json},
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                out.append({"role": message.role, "content": message.text or ""})
        return out

    def _openai_tools(self, request: GenerationRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.wire_name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                },
            }
            for tool in request.tools
        ]

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        deadline = time.monotonic() + request.timeout_s
        attempt = 0
        while True:
            attempt += 1
            try:
                async for chunk in self._attempt(request, deadline):
                    yield chunk
                return
            except _RetrySignal as signal:
                if attempt > self._max_retries:
                    raise CoreError(
                        "ProviderUnavailable",
                        f"provider failed after {attempt} attempts before producing content",
                        retryable=True,
                        details={"attempts": attempt},
                    ) from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoreError(
                        "ProviderUnavailable",
                        "total deadline exceeded before retry",
                        details={"timeout_kind": "total", "attempts": attempt},
                    ) from None
                delay = signal.retry_after_s if signal.retry_after_s is not None else 0.0
                await asyncio.sleep(min(delay, remaining))

    async def _attempt(self, request: GenerationRequest, deadline: float) -> AsyncIterator[ProviderChunk]:
        params: dict[str, Any] = {
            "model": request.model.model,
            "messages": self._openai_messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            params["tools"] = self._openai_tools(request)
        if request.max_output_tokens is not None:
            params["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            params["temperature"] = request.temperature

        state = _AttemptState()
        try:
            async with asyncio.timeout(self._timeouts.header_s):
                stream = await self._client.chat.completions.create(**params)
        except openai.APITimeoutError as error:
            raise CoreError(
                "ProviderUnavailable", "timed out waiting for response headers", details={"timeout_kind": "header"}
            ) from error
        except TimeoutError as error:
            # asyncio.timeout(header_s) fired around create(): headers never
            # arrived within the header budget (TS header timer equivalent).
            raise CoreError(
                "ProviderUnavailable", "timed out waiting for response headers", details={"timeout_kind": "header"}
            ) from error
        except openai.APIStatusError as error:
            if error.status_code == 429:
                lowered = {key.lower(): value for key, value in error.response.headers.items()}
                raise _RetrySignal(retry_after_seconds(lowered)) from None
            raise CoreError(
                "ProviderUnavailable",
                f"provider returned HTTP {error.status_code}",
                retryable=error.status_code >= 500,
                details={"status": error.status_code},
            ) from error
        except openai.APIConnectionError as error:
            # Distinguish connect-phase failures (request never sent: clearly
            # retryable, mirrors TS model-retry.ts "provider did not run") from
            # read-phase breaks after the request was sent (the provider may
            # have processed it: surface as an interruption, never a retry).
            cause = error.__cause__
            cause_name = type(cause).__name__ if cause is not None else ""
            if cause_name in ("ConnectError", "ConnectTimeout"):
                raise _RetrySignal(None) from error
            raise CoreError(
                "ProviderStreamInterrupted",
                "connection broke before response headers arrived",
                details={"cause": cause_name or "unknown"},
            ) from error

        iterator = stream.__aiter__()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoreError("ProviderUnavailable", "total deadline exceeded", details={"timeout_kind": "total"})
                try:
                    async with asyncio.timeout(remaining):
                        raw = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError as error:
                    raise CoreError(
                        "ProviderUnavailable", "total deadline exceeded", details={"timeout_kind": "total"}
                    ) from error
                except openai.APITimeoutError as error:
                    raise CoreError(
                        "ProviderUnavailable", "timed out waiting for response data", details={"timeout_kind": "chunk"}
                    ) from error
                except openai.APIConnectionError as error:
                    raise CoreError(
                        "ProviderStreamInterrupted", "stream connection lost before a terminal chunk"
                    ) from error
                for mapped in self._map_chunk(raw, state, request.request_id):
                    yield mapped
        finally:
            await stream.close()

        if state.pending_finish is None:
            # EOF or [DONE] without any finish frame: the TS testing helper
            # dies on exactly this ("response ended without a terminal finish
            # event"); we surface it as a stream interruption, never as success.
            raise CoreError("ProviderStreamInterrupted", "stream ended without a terminal chunk")
        for call_index in sorted(state.tools):
            acc = state.tools[call_index]
            yield ProviderChunk(
                request_id=request.request_id,
                index=state.take_index(),
                type="tool_call_end",
                call_index=acc.call_index,
                tool_call_id=acc.tool_call_id,
                name=acc.name,
                raw_arguments=acc.arguments,
            )
        yield ProviderChunk(
            request_id=request.request_id,
            index=state.take_index(),
            type="finish",
            reason=_map_finish_reason(state.pending_finish),
            raw_reason=state.pending_finish,
        )

    def _map_chunk(self, raw: Any, state: _AttemptState, request_id: str) -> list[ProviderChunk]:
        out: list[ProviderChunk] = []
        if raw.usage is not None:
            # Emit usage as soon as it arrives. The logical finish is held
            # back until the stream ends, so usage survives in both wire
            # variants (usage frame before or after the finish_reason frame).
            out.append(
                ProviderChunk(
                    request_id=request_id,
                    index=state.take_index(),
                    type="usage",
                    usage=_map_usage(raw.usage),
                )
            )
        choices = raw.choices or []
        if not choices:
            return out
        choice = choices[0]
        delta = choice.delta
        if delta is not None:
            content = getattr(delta, "content", None)
            if content:
                # OpenAI-compatible streams carry no text part id: one
                # synthetic part per response (recorded difference vs TS).
                out.append(
                    ProviderChunk(
                        request_id=request_id,
                        index=state.take_index(),
                        type="text_delta",
                        part_id="text-0",
                        delta=content,
                    )
                )
            tool_calls = getattr(delta, "tool_calls", None) or []
            for item in tool_calls:
                call_index = item.index
                function = item.function
                fn_name = function.name if function is not None else None
                fn_args = function.arguments if function is not None else None
                acc = state.tools.get(call_index)
                if acc is None and (item.id is not None or fn_name is not None):
                    acc = _ToolAccumulator(call_index=call_index, tool_call_id=item.id, name=fn_name)
                    state.tools[call_index] = acc
                    out.append(
                        ProviderChunk(
                            request_id=request_id,
                            index=state.take_index(),
                            type="tool_call_start",
                            call_index=call_index,
                            tool_call_id=item.id,
                            name=fn_name,
                        )
                    )
                if fn_name is not None and acc is not None:
                    acc.name = fn_name
                if fn_args:
                    if acc is None:
                        raise CoreError("ProviderStreamInterrupted", "tool arguments arrived without a tool_call start")
                    acc.arguments += fn_args
                    out.append(
                        ProviderChunk(
                            request_id=request_id,
                            index=state.take_index(),
                            type="tool_call_args_delta",
                            call_index=call_index,
                            delta=fn_args,
                        )
                    )
        if choice.finish_reason is not None:
            state.pending_finish = choice.finish_reason
        return out


# ---------------------------------------------------------------------------
# Explicit MAAS smoke CLI (never run by pytest or CI)
# ---------------------------------------------------------------------------


def _digest(text: str) -> dict[str, Any]:
    """Hash-only evidence: model output and prompts never enter the repo verbatim."""
    return {"len": len(text), "sha256_12": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}


def _summarize(chunks: Sequence[ProviderChunk], error: CoreError | None) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    text_parts: list[str] = []
    tool_plan: list[dict[str, Any]] = []
    usage_summary: dict[str, Any] | None = None
    finish_summary: dict[str, Any] | None = None
    for chunk in chunks:
        by_type[chunk.type] = by_type.get(chunk.type, 0) + 1
        if chunk.type == "text_delta" and chunk.delta:
            text_parts.append(chunk.delta)
        elif chunk.type == "usage" and chunk.usage is not None:
            usage_summary = {
                "input_tokens": chunk.usage.input_tokens,
                "output_tokens": chunk.usage.output_tokens,
                "reasoning_tokens": chunk.usage.reasoning_tokens,
                "cache_read_tokens": chunk.usage.cache_read_tokens,
                "cache_write_tokens": chunk.usage.cache_write_tokens,
            }
        elif chunk.type == "finish":
            finish_summary = {"reason": chunk.reason, "raw_reason": chunk.raw_reason}
        elif chunk.type == "tool_call_end":
            tool_plan.append(
                {
                    "call_index": chunk.call_index,
                    "tool_call_id": chunk.tool_call_id,
                    "name": chunk.name,
                    "arguments": _digest(chunk.raw_arguments or ""),
                }
            )
    return {
        "chunk_count": len(chunks),
        "by_type": by_type,
        "text": _digest("".join(text_parts)) if text_parts else None,
        "usage": usage_summary,
        "finish": finish_summary,
        "tool_plan": tool_plan,
        "error": None if error is None else {"code": error.code, "message": error.message, "details": error.details},
    }


async def _collect(
    adapter: MaasSpikeAdapter, request: GenerationRequest
) -> tuple[list[ProviderChunk], CoreError | None]:
    chunks: list[ProviderChunk] = []
    try:
        async for chunk in adapter.stream(request):
            chunks.append(chunk)
    except CoreError as error:
        return chunks, error
    return chunks, None


async def _run_smoke(base_url: str, api_key: str, model: str) -> int:
    adapter = MaasSpikeAdapter(base_url, api_key)
    try:
        # 1) one plain text stream
        text_request = GenerationRequest(
            request_id="r0-smoke-text-001",
            model=ModelRef("maas", model),
            messages=(LLMMessage(role="user", text="Reply with the single word: ok"),),
            max_output_tokens=64,
            timeout_s=90.0,
        )
        text_chunks, text_error = await _collect(adapter, text_request)

        # 2) one stream with two interleaved tool calls (synthetic prompt,
        #    no side effects; the adapter only parses the plan, never executes)
        tools = (
            ToolSpec(
                namespace="fs",
                name="read",
                version="1",
                description="Read a file",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                side_effects="read",
            ),
        )
        tool_request = GenerationRequest(
            request_id="r0-smoke-tools-001",
            model=ModelRef("maas", model),
            messages=(
                LLMMessage(
                    role="user",
                    text="Call fs__read twice in one reply: once with path r0-a.txt and once with path r0-b.txt.",
                ),
            ),
            tools=tools,
            max_output_tokens=512,
            timeout_s=90.0,
        )
        tool_chunks, tool_error = await _collect(adapter, tool_request)
    finally:
        await adapter.close()

    report = {
        # Evidence sanitization: never echo the real endpoint or model name
        # (STEP-01-R0 evidence rules). Correlate via run log outside the repo.
        "endpoint": "<REDACTED_ENDPOINT>",
        "model": "<REDACTED_MODEL>",
        "text_stream": _summarize(text_chunks, text_error),
        "tool_call_stream": _summarize(tool_chunks, tool_error),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if text_error is None and tool_error is None else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="R0-A MAAS smoke (explicit opt-in, never part of pytest/CI)")
    parser.parse_args(argv)
    base_url = os.environ.get("OPENCODE_MAAS_BASE_URL")
    api_key = os.environ.get("OPENCODE_MAAS_API_KEY")
    model = os.environ.get("OPENCODE_MAAS_MODEL")
    if not (base_url and api_key and model):
        print(
            "R0-A external evidence pending: set OPENCODE_MAAS_BASE_URL, "
            "OPENCODE_MAAS_API_KEY and OPENCODE_MAAS_MODEL to run the smoke."
        )
        return 0
    return asyncio.run(_run_smoke(base_url, api_key, model))


if __name__ == "__main__":
    raise SystemExit(main())
