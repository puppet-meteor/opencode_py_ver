"""OpenAI-compatible MAAS adapter (R2-S3 formal rebuild of the R0-A spike).

Formalizes the spike (``tools/r0/provider_spike.py``) onto the S1 contracts
face: requests arrive as pydantic ``GenerationRequest`` and chunks leave as
pydantic ``ProviderChunk``. Behaviour anchors (doc/evidence/r0/provider.md):

- ``packages/core/src/aisdk.ts:65-169``   SSE read loop, held-back logical finish
- ``packages/ai/src/route/transport/http.ts:94-140`` header/chunk/total timeouts
- ``packages/ai/src/route/executor.ts:22-35``  Retry-After parsing (ms first)
- ``packages/core/src/model-retry.ts:62-103``  retry only before first content
  (Python narrows this to 429 + connection-establishment failures, §8)

Spike differences formally adopted here (provider.md §6 #2/#3/#4, OP-4):

- errors are delivered as the terminal ``error`` ProviderChunk (§2.3 L98/L102),
  at most one terminal chunk per request, never raised across the seam —
  cancellation is the single exception and propagates as CancelledError;
- chunks are pydantic wire models, not dataclasses;
- wire tool names are reverse-parsed: ``fs__read`` on the wire becomes
  ``fs.read`` in chunk ``name`` fields (the reversible mapping of provider.md
  §6-2; ContextBuilder is expected to send dotted ``namespace.name`` in
  request tool_call parts, which this adapter maps back to the wire form).

Timeout model (provider.md §3 mapping table, unchanged from the spike):

- header: ``asyncio.timeout(header_s)`` wrapping the whole ``create()`` stage —
  any failure before response headers are in hand maps to ``timeout_kind=
  "header"``; ``httpx2.Timeout(connect=header_s)`` backstops the connect phase;
- chunk: SDK client ``httpx2.Timeout(read=chunk_s)`` — socket-read granularity,
  only in force once the stream is established (after ``create()`` returns);
- total: adapter-level explicit deadline derived from ``request.timeout_s``,
  checked before every read and wrapped around each single read; it also bounds
  retry waits (``min(retry_after, remaining)``).

Retry rules (§8, OP-7/OP-8 rulings of 2026-11-03):

- SDK ``max_retries=0`` — this adapter is the single retry owner;
- retry only when no content was produced yet AND the failure is clearly
  retryable: HTTP 429 or a connection-establishment failure; at most 2 retries;
- wait is deterministic ``min(retry_after, remaining)`` — no Retry-After means
  0, no jitter is added, and the total deadline always bounds the sleep;
- after partial content: no retry, no splicing (the stream ends in an
  ``error`` chunk with ProviderStreamInterrupted);
- 5xx becomes ProviderUnavailable with ``retryable=True`` and is NOT retried
  automatically (OP-8 conservative reading; upper layers decide).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
    ProviderChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import ToolContent, ToolFileContent, ToolTextContent

#: provider.md §6-2: OpenAI function names must match ^[a-zA-Z0-9_-]{1,64}$ —
#: the namespace dot cannot survive, hence the reversible double-underscore
#: join on the wire and the dotted form inside the Python seam.
_TOOL_WIRE_SEPARATOR = "__"
_TOOL_NAME_SEPARATOR = "."

#: §2.1 L72: with media disabled, a file tool-result content part must not
#: send its URI to the model — the model sees this explicit placeholder text.
ATTACHMENT_NOT_LOADED = "[attachment not loaded]"


# ---------------------------------------------------------------------------
# Wire tool-name mapping (reversible: ``fs__read`` <-> (fs, read) <-> ``fs.read``)
# ---------------------------------------------------------------------------


def tool_wire_name(namespace: str, name: str) -> str:
    """Forward mapping for the request catalog: ``(fs, read)`` -> ``fs__read``."""
    return f"{namespace}{_TOOL_WIRE_SEPARATOR}{name}"


def parse_tool_wire_name(wire_name: str) -> str:
    """Reverse mapping for stream chunks: ``fs__read`` -> ``fs.read``.

    Splits at the first separator, assuming a ``__``-free namespace (the
    registry's naming concern, R2-S5). A wire name that cannot be reverse-
    parsed (no separator, or an empty side) passes through unchanged — an
    unknown or malformed tool name is surfaced as-is and fails resolution
    downstream (unknown tool -> not_started failure, §2.3 L100), never guessed.
    """
    namespace, separator, name = wire_name.partition(_TOOL_WIRE_SEPARATOR)
    if separator and namespace and name:
        return f"{namespace}{_TOOL_NAME_SEPARATOR}{name}"
    return wire_name


def tool_wire_name_from_dotted(dotted: str) -> str:
    """Request-side inverse of :func:`parse_tool_wire_name`: ``fs.read`` -> ``fs__read``."""
    namespace, separator, name = dotted.partition(_TOOL_NAME_SEPARATOR)
    if separator and namespace and name:
        return f"{namespace}{_TOOL_WIRE_SEPARATOR}{name}"
    return dotted


# ---------------------------------------------------------------------------
# Retry-After parsing (TS executor.ts:22-35; ms header preferred over seconds)
# ---------------------------------------------------------------------------


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Prefer ``retry-after-ms``, then ``retry-after`` seconds; ignore invalid/negative.

    HTTP-date parsing is intentionally not implemented (recorded gap,
    provider.md §6-5); an unparsable date header is ignored like any other
    invalid value, and the adapter then waits 0 (bounded by the total deadline).
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


# ---------------------------------------------------------------------------
# Timeouts / internal failure carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaasTimeouts:
    """Header/chunk budgets (provider.md §3); the total budget rides the request."""

    header_s: float = 10.0
    chunk_s: float = 30.0


class _RetrySignal(Exception):
    """Raised only by the create() phase — i.e. strictly before any content.

    Structural invariant (§8): a failed attempt that already yielded
    chunks can never raise this, so partial streams are never restarted or
    spliced; ``MaasAdapter.stream`` converts exhaustion into the terminal
    error chunk.
    """

    def __init__(self, retry_after_s: float | None) -> None:
        super().__init__("retryable provider failure before first content")
        self.retry_after_s = retry_after_s


class _StreamFailure(Exception):
    """Terminal provider failure; ``stream`` delivers it as the final error chunk."""

    def __init__(self, error: CoreError) -> None:
        super().__init__(error.message)
        self.error = error


def _failure(
    code: CoreErrorCode,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> _StreamFailure:
    return _StreamFailure(CoreError(code=code, message=message, retryable=retryable, details=details or {}))


def _map_finish_reason(raw: str) -> FinishReason:
    if raw == "stop":
        return FinishReason.STOP
    if raw == "tool_calls":
        return FinishReason.TOOL_CALLS
    if raw == "length":
        return FinishReason.LENGTH
    if raw in ("content_filter", "filter"):
        return FinishReason.FILTER
    return FinishReason.UNKNOWN


def _map_usage(raw: Any) -> Usage:
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    completion_details = getattr(raw, "completion_tokens_details", None)
    return Usage(
        input=getattr(raw, "prompt_tokens", None),
        output=getattr(raw, "completion_tokens", None),
        reasoning=getattr(completion_details, "reasoning_tokens", None) if completion_details else None,
        cache_read=getattr(prompt_details, "cached_tokens", None) if prompt_details else None,
        cache_write=None,  # §8.1 L109: no OpenAI-compatible field — null, never a disguised 0
    )


def _tool_result_text(content: list[ToolContent]) -> str:
    """Flatten a tool-result content union to the OpenAI tool-message string."""
    parts: list[str] = []
    for item in content:
        if isinstance(item, ToolTextContent):
            parts.append(item.text)
        elif isinstance(item, ToolFileContent):
            parts.append(ATTACHMENT_NOT_LOADED)  # §2.1 L72: the URI never reaches the model
    return "\n".join(parts)


@dataclass
class _ToolAccumulator:
    call_index: int
    tool_call_id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass
class _Sequencer:
    """Request-scoped chunk ``index`` source: strictly increasing from 0 (§2.3 L88).

    Kept at the ``stream`` level (not per attempt) so the terminal error chunk
    continues the sequence of an interrupted partial stream.
    """

    next_index: int = 0

    def take(self) -> int:
        value = self.next_index
        self.next_index += 1
        return value


@dataclass
class _AttemptState:
    pending_finish: str | None = None
    tools: dict[int, _ToolAccumulator] = field(default_factory=dict)


def _error_chunk(request_id: str, sequencer: _Sequencer, error: CoreError) -> ErrorChunk:
    return ErrorChunk(request_id=request_id, index=sequencer.take(), type="error", error=error)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MaasAdapter:
    """OpenAI-compatible MAAS provider; SDK types stay inside this class."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeouts: MaasTimeouts | None = None,
        max_retries: int = 2,
    ) -> None:
        effective = timeouts or MaasTimeouts()
        self._timeouts = effective
        self._max_retries = max_retries
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,  # retry policy belongs to this adapter, not the SDK (§8)
            timeout=httpx2.Timeout(
                connect=effective.header_s,
                read=effective.chunk_s,
                write=effective.header_s,
                pool=effective.header_s,
            ),
        )

    async def close(self) -> None:
        """Close the underlying SDK client (app lifecycle, §7 L337)."""
        await self._client.close()

    # -- request assembly ---------------------------------------------------

    def _openai_messages(self, request: GenerationRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool":
                # One OpenAI tool message per tool_result part (each carries its
                # own paired tool_call_id, §2.2 L80). Other part types have no
                # wire slot in a tool-role message and are dropped.
                for part in message.content:
                    if part.type == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": part.tool_call_id,
                                "content": _tool_result_text(part.content),
                            }
                        )
            elif message.role == "assistant":
                # Reasoning parts have no OpenAI-compatible wire slot
                # (provider.md §6-6): replayed reasoning is not sent back.
                text = "".join(part.text for part in message.content if isinstance(part, LLMTextPart))
                entry: dict[str, Any] = {"role": "assistant", "content": text or None}
                tool_calls = [
                    {
                        "id": part.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_wire_name_from_dotted(part.name),
                            "arguments": json.dumps(part.input),
                        },
                    }
                    for part in message.content
                    if part.type == "tool_call"
                ]
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                out.append(entry)
            else:
                text = "".join(part.text for part in message.content if isinstance(part, LLMTextPart))
                out.append({"role": message.role, "content": text})
        return out

    def _openai_tools(self, request: GenerationRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in request.tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_wire_name(tool.namespace, tool.name),
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
            )
        return out

    # -- stream -------------------------------------------------------------

    # Annotated AsyncGenerator (a subtype of the Protocol's AsyncIterator,
    # §2.2 L84): the concrete stream is an async generator function, so
    # consumers may early-close it via ``aclose()``.
    async def stream(self, request: GenerationRequest) -> AsyncGenerator[ProviderChunk, None]:
        """Consume with ``async for``; supports ``aclose`` and cancellation.

        Terminal errors are delivered as the final ``error`` chunk (§2.3 L98);
        only CancelledError propagates as an exception (§2.3 L102).
        """
        deadline = time.monotonic() + request.timeout_s
        sequencer = _Sequencer()
        attempt = 0
        while True:
            attempt += 1
            try:
                async for chunk in self._attempt(request, deadline, sequencer):
                    yield chunk
                return
            except _RetrySignal as signal:
                # Raised only by the create() phase, so no content was yielded
                # yet — see the _RetrySignal invariant note.
                if attempt > self._max_retries:
                    yield _error_chunk(
                        request.request_id,
                        sequencer,
                        CoreError(
                            code=CoreErrorCode.PROVIDER_UNAVAILABLE,
                            message=f"provider failed after {attempt} attempts before producing content",
                            retryable=True,
                            details={"attempts": attempt},
                        ),
                    )
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield _error_chunk(
                        request.request_id,
                        sequencer,
                        CoreError(
                            code=CoreErrorCode.PROVIDER_UNAVAILABLE,
                            message="total deadline exceeded before retry",
                            retryable=False,
                            details={"timeout_kind": "total", "attempts": attempt},
                        ),
                    )
                    return
                delay = signal.retry_after_s if signal.retry_after_s is not None else 0.0
                # OP-7 (T-A-005, 2026-11-03): deterministic wait —
                # min(retry_after, remaining), no Retry-After means 0, no
                # jitter, always bounded by the total deadline.
                await asyncio.sleep(min(delay, remaining))
            except _StreamFailure as failure:
                yield _error_chunk(request.request_id, sequencer, failure.error)
                return

    async def _attempt(
        self, request: GenerationRequest, deadline: float, sequencer: _Sequencer
    ) -> AsyncIterator[ProviderChunk]:
        params: dict[str, Any] = {
            "model": request.model.model,
            "messages": self._openai_messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},  # §8.1 L109 passthrough
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            params["tools"] = self._openai_tools(request)
        if request.temperature is not None:
            params["temperature"] = request.temperature

        state = _AttemptState()
        # M-1 (R2-S3-review §2 M-1; pinned by T-C1-017a, 2026-11-03): a
        # dual-timer race, accepted as-is. This asyncio.timeout(header_s)
        # wrapper and the httpx2 connect timeout (httpx2.Timeout(connect=
        # header_s) in __init__) share one base and start together, so on a
        # real connect timeout (server never accepts) either may fire first
        # and the winner picks the failure surface: asyncio.timeout ->
        # TimeoutError -> header-timeout terminal (retryable=False, no
        # retry) vs httpx2 connect -> APIConnectionError(cause=
        # ConnectTimeout) -> _RetrySignal(None) -> up to 2 retries. Accepted
        # because (1) both surfaces are contract-legal failure exits,
        # terminally equivalent for the upper layer (each ends the request
        # with one error chunk — fail, never splice, never restart); (2) the
        # spike already had this structure (provider.md §3 L47 / §4 L56 —
        # both branches coexist since R0, not introduced in R2-S3); (3)
        # tested paths are unaffected (test_header_timeout's connection is
        # established, so it deterministically maps to header; an
        # immediately-refused ConnectError never waits on either timer).
        # Possible follow-up: decouple the header-wrapper budget from the
        # connect budget — annotated only, deliberately not implemented.
        try:
            async with asyncio.timeout(self._timeouts.header_s):
                stream = await self._client.chat.completions.create(**params)
        except openai.APITimeoutError as error:
            raise _failure(
                CoreErrorCode.PROVIDER_UNAVAILABLE,
                "timed out waiting for response headers",
                details={"timeout_kind": "header"},
            ) from error
        except TimeoutError as error:
            # asyncio.timeout(header_s) fired around create(): headers never
            # arrived within the header budget (TS header-timer equivalent).
            raise _failure(
                CoreErrorCode.PROVIDER_UNAVAILABLE,
                "timed out waiting for response headers",
                details={"timeout_kind": "header"},
            ) from error
        except openai.APIStatusError as error:
            if error.status_code == 429:
                lowered = {key.lower(): value for key, value in error.response.headers.items()}
                raise _RetrySignal(retry_after_seconds(lowered)) from None
            # OP-8: 5xx surfaces as retryable-but-not-auto-retried.
            raise _failure(
                CoreErrorCode.PROVIDER_UNAVAILABLE,
                f"provider returned HTTP {error.status_code}",
                retryable=error.status_code >= 500,
                details={"status": error.status_code},
            ) from error
        except openai.APIConnectionError as error:
            # Connect-phase failures (request never sent: clearly retryable)
            # are distinguished from read-phase breaks (the provider may have
            # processed the request: surfaced as an interruption, never retried).
            cause = error.__cause__
            cause_name = type(cause).__name__ if cause is not None else ""
            if cause_name in ("ConnectError", "ConnectTimeout"):
                raise _RetrySignal(None) from error
            raise _failure(
                CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
                "connection broke before response headers arrived",
                details={"cause": cause_name or "unknown"},
            ) from error

        iterator = stream.__aiter__()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _failure(
                        CoreErrorCode.PROVIDER_UNAVAILABLE,
                        "total deadline exceeded",
                        details={"timeout_kind": "total"},
                    )
                try:
                    async with asyncio.timeout(remaining):
                        raw = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError as error:
                    raise _failure(
                        CoreErrorCode.PROVIDER_UNAVAILABLE,
                        "total deadline exceeded",
                        details={"timeout_kind": "total"},
                    ) from error
                except openai.APITimeoutError as error:
                    raise _failure(
                        CoreErrorCode.PROVIDER_UNAVAILABLE,
                        "timed out waiting for response data",
                        details={"timeout_kind": "chunk"},
                    ) from error
                except openai.APIConnectionError as error:
                    raise _failure(
                        CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
                        "stream connection lost before a terminal chunk",
                    ) from error
                for mapped in self._map_chunk(raw, state, request.request_id, sequencer):
                    yield mapped
        finally:
            # N-2 (R2-S3-review §2 N-2; pinned by T-C1-017a, 2026-11-03): if
            # close() itself raised here it would mask the exception already
            # propagating out of the read loop (a raise inside finally
            # replaces the in-flight one). SDK stream cleanup is not expected
            # to raise, so this defensive edge is annotated only — wrapping
            # the close in try/except is the optional hardening,
            # deliberately not implemented.
            await stream.close()

        if state.pending_finish is None:
            # EOF or [DONE] without any finish frame (provider.md §5: covered
            # deterministically via omit_done, never via hard truncation).
            raise _failure(CoreErrorCode.PROVIDER_STREAM_INTERRUPTED, "stream ended without a terminal chunk")
        for call_index in sorted(state.tools):
            acc = state.tools[call_index]
            if not acc.tool_call_id:
                raise _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    "tool call ended without a tool_call_id",
                    details={"violation": "tool_call_without_id"},
                )
            if not acc.name:
                raise _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    "tool call ended without a name",
                    details={"violation": "tool_call_without_name"},
                )
            yield ToolCallEndChunk(
                request_id=request.request_id,
                index=sequencer.take(),
                type="tool_call_end",
                call_index=acc.call_index,
                tool_call_id=acc.tool_call_id,
                name=parse_tool_wire_name(acc.name),
                raw_arguments=acc.arguments,
            )
        yield FinishChunk(
            request_id=request.request_id,
            index=sequencer.take(),
            type="finish",
            reason=_map_finish_reason(state.pending_finish),
            raw_reason=state.pending_finish,
        )

    def _map_chunk(self, raw: Any, state: _AttemptState, request_id: str, sequencer: _Sequencer) -> list[ProviderChunk]:
        out: list[ProviderChunk] = []
        choices = raw.choices or []
        choice = choices[0] if choices else None
        delta = choice.delta if choice is not None else None
        content = getattr(delta, "content", None) if delta is not None else None
        tool_calls = (getattr(delta, "tool_calls", None) or []) if delta is not None else []

        # STEP-01-R0 L86: duplicate or out-of-order wire chunks are rejected,
        # never normalized. Once a finish frame has been seen, only usage
        # frames (usage after finish stays legal, §2.3 L102) and the stream
        # end may follow; content/tool-call frames or a second finish frame
        # are wire protocol violations that fail the whole stream.
        if state.pending_finish is not None:
            if content or tool_calls:
                raise _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    "provider stream sent content after the finish frame",
                    details={"violation": "content_after_finish"},
                )
            if choice is not None and choice.finish_reason is not None:
                raise _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    "provider stream sent a duplicate finish frame",
                    details={"violation": "duplicate_finish"},
                )

        if raw.usage is not None:
            # Emit usage as soon as it arrives. The logical finish is held
            # back until the stream ends, so usage survives in both wire
            # variants (usage frame before or after the finish_reason frame).
            out.append(
                UsageChunk(request_id=request_id, index=sequencer.take(), type="usage", usage=_map_usage(raw.usage))
            )
        if choice is None:
            return out
        if delta is not None:
            if content:
                # OpenAI-compatible streams carry no text part id: one
                # synthetic part per response (recorded difference vs TS).
                out.append(
                    TextDeltaChunk(
                        request_id=request_id,
                        index=sequencer.take(),
                        type="text_delta",
                        part_id="text-0",
                        delta=content,
                    )
                )
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
                        ToolCallStartChunk(
                            request_id=request_id,
                            index=sequencer.take(),
                            type="tool_call_start",
                            call_index=call_index,
                            tool_call_id=item.id,
                            name=parse_tool_wire_name(fn_name) if fn_name else None,
                        )
                    )
                if fn_name is not None and acc is not None:
                    acc.name = fn_name
                if fn_args:
                    if acc is None:
                        raise _failure(
                            CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
                            "tool arguments arrived without a tool_call start",
                        )
                    acc.arguments += fn_args
                    out.append(
                        ToolCallArgsDeltaChunk(
                            request_id=request_id,
                            index=sequencer.take(),
                            type="tool_call_args_delta",
                            call_index=call_index,
                            delta=fn_args,
                        )
                    )
        if choice.finish_reason is not None:
            # A second finish frame never reaches here: the rejection block
            # at the top of _map_chunk already failed the stream (L86).
            state.pending_finish = choice.finish_reason
        return out
