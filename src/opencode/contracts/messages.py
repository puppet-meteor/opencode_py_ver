"""Shared wire primitives for session messages (CORE-CONTRACTS §1/§2.1).

Holds the JSON extension types and the UTC timestamp rule that every wire model
in this package reuses, plus the message shapes: ``UserMessage`` (R1) and the
R2 union expansion (``ToolContent``, ``AssistantPart``, ``AssistantMessage``,
``IdleMessage``).

Import-order note: ``errors.py`` imports ``JsonObject`` from here, so this
module must stay a runtime leaf. The R2 models reference ``CoreError`` /
``ModelRef`` / ``Usage`` / ``FinishReason`` (defined in ``errors.py`` /
``llm.py``) as deferred string annotations; ``complete_deferred_models`` at the
bottom of ``llm.py`` finishes them once those types exist. Importing any
``contracts`` submodule first runs the package ``__init__``
(errors -> messages -> events -> llm), so consumers always see complete models.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    # Deferred references, resolved by complete_deferred_models() below: runtime
    # imports of errors/llm here would be circular (errors imports this module).
    from opencode.contracts.errors import CoreError
    from opencode.contracts.llm import FinishReason, ModelRef, Usage

type JsonObject = dict[str, JsonValue]


def _as_utc(value: datetime) -> datetime:
    """Reject naive timestamps, normalize aware ones to UTC (fail closed, §1)."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


def _reject_bool(field: str, value: Any) -> Any:
    """§1 repo rule: integer-semantic fields reject bools before type conversion.

    Shared with ``tools.py``/``llm.py`` (which cannot import ``events.py``'s R1
    copy without an import cycle); the duplication with ``events.py`` is a known
    drift point, float-reported in T-C1-013 (plan §4 归置决定 3, R5 candidate).
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    return value


#: §2.1 L74: cost as a decimal fixed-point string ("0.001234"), never a float —
#: binary floats cannot represent cents exactly and JSON floats drift on encode.
CostUsd = Annotated[str, Field(pattern=r"^\d+(\.\d+)?$")]


class UserMessage(BaseModel):
    """User-authored session message (§2.1 ``user`` row).

    ``files`` stays out until media lands (M6): the R1 v1 wire has no optional
    ``files`` field (CORE-CONTRACTS §2.1, T-A-003 option b) — a ``files`` key
    (even an empty list) on the wire is rejected here by ``extra="forbid"``
    rather than silently dropped; Phase 1 callers send plain text and a non-empty
    files list is rejected as UnsupportedCapability at the tool/config layer.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^msg_")
    type: Literal["user"]
    created_at: UtcDateTime
    metadata: JsonObject = Field(default_factory=dict)
    text: str


class ToolTextContent(BaseModel):
    """§2.1 L69: ``{type: "text", text}`` — the only content type R2 produces."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ToolFileContent(BaseModel):
    """§2.1 L70: ``{type: "file", uri, mime, name?}``.

    Encodable/decodable on the wire (history keeps a restricted reference), but
    with media disabled the URI must not be read or sent to the model — that
    gate is a runtime rule (§2.1 L72, M6), not a wire constraint.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    uri: str = Field(min_length=1)
    mime: str = Field(min_length=1)
    name: str | None = None


#: §2.1 L67-70: tool result content union, tagged by ``type``.
ToolContent = Annotated[ToolTextContent | ToolFileContent, Field(discriminator="type")]


class AssistantTextPart(BaseModel):
    """§2.1 L33: text part — ``part_id`` + ``text``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    part_id: str = Field(min_length=1)
    text: str


class AssistantReasoningPart(BaseModel):
    """§2.1 L33: reasoning part — optional per endpoint, never assumed present."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["reasoning"]
    part_id: str = Field(min_length=1)
    text: str


class AssistantToolPart(BaseModel):
    """§2.1 L33: tool call part — the full plan/result state.

    ``content`` is a required key (possibly ``[]`` while the call is pending);
    the "at least one model-readable text" rule applies to tool terminal states
    (py.tool.finished, §2.1 L72), not to pending plans. State/effect vocabulary
    per §2.1 L35; transition legality is fold-owned (R2-S2), not wire-owned.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool"]
    tool_call_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: JsonObject
    state: Literal["pending", "running", "succeeded", "failed", "denied", "cancelled", "unknown"]
    effect_status: Literal["not_started", "completed", "unknown"]
    content: list[ToolContent]
    structured_output: JsonValue | None = None
    error: CoreError | None = None


#: §2.1 L33: assistant part union, tagged by ``type``.
AssistantPart = Annotated[AssistantTextPart | AssistantReasoningPart | AssistantToolPart, Field(discriminator="type")]


class AssistantMessage(BaseModel):
    """§2.1 L28 assistant row: model/parts/status plus optional terminal facts.

    The optional fields are the v1 wire set named by the contract itself
    (finish/raw_finish/usage/cost_usd/error/completed_at); *new* fields later
    mean a version bump + upcaster (§8 L348), never same-version optionals.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^msg_")
    type: Literal["assistant"]
    created_at: UtcDateTime
    metadata: JsonObject = Field(default_factory=dict)
    model: ModelRef
    parts: list[AssistantPart]
    status: Literal["running", "completed", "failed", "interrupted"]
    finish: FinishReason | None = None
    raw_finish: str | None = None
    usage: Usage | None = None
    cost_usd: CostUsd | None = None
    error: CoreError | None = None
    completed_at: UtcDateTime | None = None


class IdleMessage(BaseModel):
    """§2.1 L29 idle row: run terminal facts; projected only from py.run.finished."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^msg_")
    type: Literal["idle"]
    created_at: UtcDateTime
    metadata: JsonObject = Field(default_factory=dict)
    run_id: str = Field(min_length=1, pattern=r"^run_")
    outcome: Literal["succeeded", "failed", "interrupted"]
    error: CoreError | None = None


#: §2.1 L23-29 message union (user / assistant / idle); the canonical alias
#: lives here and ``opencode.session.context`` imports and re-exports it for
#: the builder faces. An alias over the models above, not a new wire model.
type SessionMessage = UserMessage | AssistantMessage | IdleMessage


def complete_deferred_models(types: Mapping[str, Any]) -> None:
    """Finish building the models that reference errors/llm types as strings.

    Called once from the bottom of ``llm.py`` (after ModelRef/Usage/FinishReason
    exist) with the deferred names. Member-before-parent order matters: the
    AssistantPart discriminated union embeds AssistantToolPart, so the member
    is rebuilt first. Idempotent per process: pydantic skips complete models.
    """
    namespace = dict(types)
    for model in (AssistantToolPart, AssistantMessage, IdleMessage):
        model.model_rebuild(_types_namespace=namespace)
