"""LLM wire contracts (CORE-CONTRACTS §2.2/§2.3): requests, messages, provider chunks.

The S1 face: ``GenerationRequest`` in, ``ProviderChunk`` out (``Provider.stream``
returns an async iterator, §2.2 L83-84). SDK types never cross this boundary
(A-PROVIDER red line); adapters translate to/from provider wire formats.
TS behavior mapping: ``packages/ai/src/schema/messages.ts`` + ``events.ts``
per doc/parity.md — notably ``FinishReason`` uses the Python §2.3 L97 vocabulary
(stop/tool_calls/length/filter/unknown), which diverges from the TS raw set.

Import-order note: this module must be imported before any R2 event data model
is *used*, because its bottom call to ``complete_deferred_models`` finishes the
messages.py models that reference ModelRef/Usage/FinishReason/CoreError as
deferred strings. The ``contracts`` package ``__init__`` (errors -> messages ->
events) guarantees the ordering: events.py imports this module, so any import
of a contracts submodule loads llm.py and completes the deferred models first.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from opencode.contracts.errors import CoreError
from opencode.contracts.messages import CostUsd, JsonObject, ToolContent, _reject_bool, complete_deferred_models
from opencode.contracts.tools import ToolSpec


class ModelRef(BaseModel):
    """§2.1 L74: ``{provider, model}``; Phase 1 provider is fixed to maas."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class Usage(BaseModel):
    """§2.1 L74: five optional non-negative token counts; null means unknown.

    A count the endpoint did not report is ``null`` on the wire, never a
    disguised ``0`` — only an actual provider zero is zero. ``cost_usd`` is a
    sibling field on the carrying message/chunk (§2.1 L28, §2.3 L96), not a
    Usage member: usage is metered data, cost is priced data.
    """

    model_config = ConfigDict(extra="forbid")

    input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    reasoning: int | None = Field(default=None, ge=0)
    cache_read: int | None = Field(default=None, ge=0)
    cache_write: int | None = Field(default=None, ge=0)

    @field_validator("input", "output", "reasoning", "cache_read", "cache_write", mode="before")
    @classmethod
    def _token_counts_not_bool(cls, value: Any, info: ValidationInfo) -> Any:
        return _reject_bool(info.field_name or "token count", value)


class FinishReason(StrEnum):
    """§2.3 L97: logical finish reasons; raw provider strings ride in raw_finish/raw_reason."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    FILTER = "filter"
    UNKNOWN = "unknown"


class LLMTextPart(BaseModel):
    """§2.2 L80 request content: plain text part."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class LLMReasoningPart(BaseModel):
    """§2.2 L80 request content: reasoning part (replayed history)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["reasoning"]
    text: str


class LLMToolCallPart(BaseModel):
    """§2.2 L80: assistant-issued call — id + name + JSON object input."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: JsonObject


class LLMToolResultPart(BaseModel):
    """§2.2 L80: paired result — tool_call_id must match the originating call.

    Pairing completeness ("every legal tool_call_id has exactly one result",
    acceptance C) is ContextBuilder/runner validation (R2-S4/S6); the wire shape
    only carries the mandatory identity + name + content union. The MAAS adapter
    maps this to the OpenAI tool role.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"]
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: list[ToolContent]


#: §2.2 L80: request content union, tagged by ``type``.
LLMPart = Annotated[LLMTextPart | LLMReasoningPart | LLMToolCallPart | LLMToolResultPart, Field(discriminator="type")]


class LLMMessage(BaseModel):
    """§2.2 L80: role + content parts; only ContextBuilder produces these."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: list[LLMPart]


class GenerationRequest(BaseModel):
    """§2.2 L78: the full request face; ``ContextBuilder`` is the only producer.

    Field set exactly per §2.2 L78: request_id (caller-chosen, non-empty, no
    prefix rule), model, messages, tools (both required keys; empty lists are
    legal — "no tools this generation" is a state, not an absent field),
    max_output_tokens, optional temperature, timeout_s.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    model: ModelRef
    messages: list[LLMMessage]
    tools: list[ToolSpec]
    max_output_tokens: int = Field(ge=1)
    temperature: float | None = None
    timeout_s: float = Field(gt=0)

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def _max_output_tokens_not_bool(cls, value: Any) -> Any:
        return _reject_bool("max_output_tokens", value)

    @field_validator("temperature", "timeout_s", mode="before")
    @classmethod
    def _floats_not_bool(cls, value: Any, info: ValidationInfo) -> Any:
        # §1 names integer fields; bool->float laxness (True == 1.0) is the same
        # trap, so the guard is extended to the numeric request fields too
        # (float-reported in the T-C1-013 progress file).
        return _reject_bool(info.field_name or "float field", value)


class _ChunkBase(BaseModel):
    """§2.3 L88: fields shared by every provider chunk (index ascends from 0).

    Monotonic per-request ordering of ``index`` (and per-call ``call_index``) is
    producer behavior the adapter/runner rely on (R2-S3/S6); the wire model only
    pins non-negativity.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    provider_metadata: JsonObject = Field(default_factory=dict)

    @field_validator("index", mode="before")
    @classmethod
    def _index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("index", value)


class TextDeltaChunk(_ChunkBase):
    """§2.3 L92: text fragment — part_id + delta."""

    type: Literal["text_delta"]
    part_id: str = Field(min_length=1)
    delta: str


class ReasoningDeltaChunk(_ChunkBase):
    """§2.3 L92: reasoning fragment — part_id + delta."""

    type: Literal["reasoning_delta"]
    part_id: str = Field(min_length=1)
    delta: str


class ToolCallStartChunk(_ChunkBase):
    """§2.3 L93: first fragment of a tool call; id/name may arrive later fragments."""

    type: Literal["tool_call_start"]
    call_index: int = Field(ge=0)
    tool_call_id: str | None = None
    name: str | None = None

    @field_validator("call_index", mode="before")
    @classmethod
    def _call_index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("call_index", value)


class ToolCallArgsDeltaChunk(_ChunkBase):
    """§2.3 L94: raw argument fragment by call_index; parsing waits for the end chunk."""

    type: Literal["tool_call_args_delta"]
    call_index: int = Field(ge=0)
    delta: str

    @field_validator("call_index", mode="before")
    @classmethod
    def _call_index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("call_index", value)


class ToolCallEndChunk(_ChunkBase):
    """§2.3 L95: complete call — id/name/raw_arguments are final here."""

    type: Literal["tool_call_end"]
    call_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    raw_arguments: str

    @field_validator("call_index", mode="before")
    @classmethod
    def _call_index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("call_index", value)


class UsageChunk(_ChunkBase):
    """§2.3 L96: metered usage — may arrive after finish; must not be lost (L102)."""

    type: Literal["usage"]
    usage: Usage
    cost_usd: CostUsd | None = None


class FinishChunk(_ChunkBase):
    """§2.3 L97: logical finish — reason + optional raw provider string."""

    type: Literal["finish"]
    reason: FinishReason
    raw_reason: str | None = None


class ErrorChunk(_ChunkBase):
    """§2.3 L98: terminal chunk — at most one of finish/error per request (L102)."""

    type: Literal["error"]
    error: CoreError


#: §2.3 L88-98: provider stream chunk union, tagged by ``type``.
ProviderChunk = Annotated[
    TextDeltaChunk
    | ReasoningDeltaChunk
    | ToolCallStartChunk
    | ToolCallArgsDeltaChunk
    | ToolCallEndChunk
    | UsageChunk
    | FinishChunk
    | ErrorChunk,
    Field(discriminator="type"),
]


# Deferred messages.py models (AssistantToolPart/AssistantMessage/IdleMessage)
# can only be completed now that ModelRef/Usage/FinishReason exist — see the
# messages.py docstring for why the rebuild lives here and not there.
complete_deferred_models(
    {
        "CoreError": CoreError,
        "FinishReason": FinishReason,
        "ModelRef": ModelRef,
        "Usage": Usage,
    }
)
