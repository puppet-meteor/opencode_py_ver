"""Event wire contracts (CORE-CONTRACTS §3.1): envelopes, type+version registry, R1+R2 event subsets.

Decode is fail closed: unknown type/version, wrong durability, malformed JSON, or any
schema violation raises and nothing is silently skipped or coerced. Field-level failures
surface as ``pydantic.ValidationError``; envelope/registry failures as ``EventDecodeError``
subclasses so S4/S6 can map them to ``StorageCorrupt`` and read-only diagnostics.

TS mapping (doc/parity.md §3, ``packages/schema/src/event.ts``): the envelope mirrors
``Event.durable()/ephemeral()`` with Python differences seq >= 1 (initial 1, not 0/-1)
and a ``py.*`` event domain that never claims TS wire compatibility.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from json import dumps, loads
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from opencode.contracts.errors import CoreError
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    JsonObject,
    ToolContent,
    ToolTextContent,
    UserMessage,
    UtcDateTime,
)
from opencode.contracts.tools import ToolRef

Durability = Literal["durable", "ephemeral"]

PY_SESSION_CREATED = "py.session.created"
PY_PROMPT_ACCEPTED = "py.prompt.accepted"

# R2 event domain (§3.1 L124-140): run/step/text/reasoning/tool durable rows
# plus the four ephemeral live-preview types.
PY_RUN_STARTED = "py.run.started"
PY_RUN_CANCEL_REQUESTED = "py.run.cancel_requested"
PY_RUN_FINISHED = "py.run.finished"
PY_STEP_STARTED = "py.step.started"
PY_STEP_GENERATED = "py.step.generated"
PY_STEP_FINISHED = "py.step.finished"
PY_TEXT_FINALIZED = "py.text.finalized"
PY_REASONING_FINALIZED = "py.reasoning.finalized"
PY_TOOL_REQUESTED = "py.tool.requested"
PY_TOOL_STARTED = "py.tool.started"
PY_TOOL_FINISHED = "py.tool.finished"
PY_TEXT_DELTA = "py.text.delta"
PY_REASONING_DELTA = "py.reasoning.delta"
PY_TOOL_INPUT_DELTA = "py.tool.input.delta"
PY_TOOL_PROGRESS = "py.tool.progress"

EVENT_VERSION = 1

#: Event types that belong to a run; their envelope metadata must carry run_id (§3.1).
#: Deliberately still R1-only in this slice: expanding to the R2 types would
#: retroactively require metadata.run_id on py.run.started/py.text.delta
#: envelopes that frozen R1 tests construct without it (test_bus.py
#: _ephemeral, test_eventlog.py _ephemeral, test_projector.py "future"), so the
#: expansion awaits arbitration on patching those helpers (T-C1-013 float
#: report, open question 1). R2 producers still write run_id into every new
#: type's metadata (fixtures pin this), and the whenever-present check below
#: stays active for any metadata.run_id that appears.
RUN_SCOPED_EVENT_TYPES: frozenset[str] = frozenset({PY_PROMPT_ACCEPTED})

_SHA256_HEX = r"^[0-9a-f]{64}$"

_EVT_ID = r"^evt_"
_SES_ID = r"^ses_"
_RUN_ID = r"^run_"
_MSG_ID = r"^msg_"


def _reject_bool(field: str, value: Any) -> Any:
    """§1 repo rule: integer-semantic fields reject bools before type conversion.

    Pydantic's bool→int/Literal equality would let True masquerade as 1; guard both
    model construction and decode paths with the same behavior.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    return value


def versioned_type(event_type: str, version: int) -> str:
    """Registry key format, mirrors TS ``versionedType`` (``${type}.${version}``)."""
    return f"{event_type}.{version}"


def canonical_json(value: JsonValue) -> str:
    """Deterministic JSON text for hashing: sorted keys, no whitespace, UTF-8, NaN rejected."""
    return dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compute_snapshot_hash(snapshot: ConfigSnapshot) -> str:
    """sha256 hex of the snapshot's canonical JSON (CORE-CONTRACTS §6.2).

    The R1 value domain makes the canonical text a constant:
    ``{"permission_rules":[],"schema_version":1}`` — UTF-8, no BOM, 64 lowercase hex.
    """
    return sha256(canonical_json(snapshot.model_dump(mode="json")).encode("utf-8")).hexdigest()


class ConfigSnapshot(BaseModel):
    """R1 minimal legal config snapshot (CORE-CONTRACTS §6.1, T-A-002 ruling).

    Exactly ``{"schema_version": 1, "permission_rules": []}``: both fields must be
    present, rules must be empty, and no extra keys (an embedded hash is rejected —
    the digest lives only in event data ``snapshot_hash``). Executable snapshots
    (R3+) use a new schema_version; version 1 decoding and old fixtures are kept.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    permission_rules: list[JsonObject] = Field(max_length=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    @classmethod
    def create(cls) -> ConfigSnapshot:
        """The unique legal R1 snapshot object."""
        return cls(schema_version=1, permission_rules=[])


class SessionCreatedData(BaseModel):
    """``py.session.created`` v1 data (§3.1/§6.1): working_dir + config_snapshot + snapshot_hash."""

    model_config = ConfigDict(extra="forbid")

    working_dir: str = Field(min_length=1)
    config_snapshot: ConfigSnapshot
    snapshot_hash: str = Field(pattern=_SHA256_HEX)

    @model_validator(mode="after")
    def _snapshot_hash_verified(self) -> SessionCreatedData:
        """Recompute the snapshot digest on every decode; a mismatch is ValidationError (§6.2.3)."""
        if compute_snapshot_hash(self.config_snapshot) != self.snapshot_hash:
            raise ValueError("snapshot_hash does not match the canonical sha256 of config_snapshot")
        return self


class PromptAcceptedData(BaseModel):
    """``py.prompt.accepted`` v1 data (§3.1): request_id + run_id + the accepted user message."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    message: UserMessage


# ---------------------------------------------------------------------------
# R2 event data models (§3.1 rows L124-132 + ephemeral L140; plan §2 R2-S1).
# ID rules: message_id carries the ^msg_ pattern (§3.1 L114); step_id/part_id/
# execution_id/tool_call_id are internal conventions — plain non-empty strings,
# no pattern (OP-3 conservative reading; generation conventions land in S6).
# ---------------------------------------------------------------------------


class RunStartedData(BaseModel):
    """``py.run.started`` v1 data (§3.1 L124): run identity only."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)


class RunCancelRequestedData(BaseModel):
    """``py.run.cancel_requested`` v1 data (§3.1 L125): user/shutdown intent; never overrides a result."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    reason: Literal["user", "shutdown"]


class RunFinishedData(BaseModel):
    """``py.run.finished`` v1 data (§3.1 L138): folded outcome; idle messages project from this."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    outcome: Literal["succeeded", "failed", "interrupted"]
    error: CoreError | None = None


class StepStartedData(BaseModel):
    """``py.step.started`` v1 data (§3.1 L126): one generation attempt begins.

    ``attempt`` >= 1; R2 always writes 1 (OP-6: no user-facing retry command,
    a retry is a new prompt; attempt > 1 is reserved for recovery/retry faces).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    step_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    model: ModelRef
    attempt: int = Field(ge=1)

    @field_validator("attempt", mode="before")
    @classmethod
    def _attempt_not_bool(cls, value: Any) -> Any:
        return _reject_bool("attempt", value)


class TextFinalizedData(BaseModel):
    """``py.text.finalized`` v1 data (§3.1 L127): full final text at message_id+part_id.

    ``ordinal`` is 0-based and unique within the generation batch; batch ordering
    and second-finalized idempotency are commit-time rules (§3.1 L144, R2-S2).
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    text: str

    @field_validator("ordinal", mode="before")
    @classmethod
    def _ordinal_not_bool(cls, value: Any) -> Any:
        return _reject_bool("ordinal", value)


class ReasoningFinalizedData(BaseModel):
    """``py.reasoning.finalized`` v1 data (§3.1 L127): same shape as text, batch-ordered by ordinal."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    text: str

    @field_validator("ordinal", mode="before")
    @classmethod
    def _ordinal_not_bool(cls, value: Any) -> Any:
        return _reject_bool("ordinal", value)


class StepGeneratedData(BaseModel):
    """``py.step.generated`` v1 data (§3.1 L128): the last event of the generation batch.

    The embedded AssistantMessage is the complete call plan with every tool
    part pending/not_started; batch atomicity and consistency with the finalized
    parts are commit-time validation (§3.1 L144, R2-S2), not wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    message: AssistantMessage


class StepFinishedData(BaseModel):
    """``py.step.finished`` v1 data (§3.1 L132): self-contained terminal message."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    message: AssistantMessage


class ToolRequestedData(BaseModel):
    """``py.tool.requested`` v1 data (§3.1 L129): raw request, resolved ref, canonical hash.

    ``tool_ref`` is optional because the raw requested_name may fail catalog
    resolution (unknown tool); the failed/not_started terminal then lands on
    py.tool.finished. Identity/ownership constraints (§2.1 L37) are commit-time
    validation (R2-S2).
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    tool_call_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    requested_name: str = Field(min_length=1)
    tool_ref: ToolRef | None = None
    input: JsonObject
    input_hash: str = Field(pattern=_SHA256_HEX)


class ToolStartedData(BaseModel):
    """``py.tool.started`` v1 data (§3.1 L130): execution confirmed after the permission decision.

    permission_decision uses the §5.2 effect vocabulary; R2 has no policy layer
    (read defaults to allow), so R2 producers always write "allow" — ask/deny
    become producible only from R3+ (float-reported, T-C1-013).
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    permission_decision: Literal["allow", "ask", "deny"]
    input_hash: str = Field(pattern=_SHA256_HEX)


class ToolFinishedData(BaseModel):
    """``py.tool.finished`` v1 data (§3.1 L131): terminal state + model-readable content.

    ``state`` is terminal-only — pending/running live on AssistantToolPart and
    the fold, never on the finished event. §2.1 L72: every terminal state carries
    at least one text content, including denied/cancelled/unknown explanations;
    file-only content is rejected here, not at display time.
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    state: Literal["succeeded", "failed", "denied", "cancelled", "unknown"]
    effect_status: Literal["not_started", "completed", "unknown"]
    content: list[ToolContent] = Field(min_length=1)
    structured_output: JsonValue | None = None
    error: CoreError | None = None

    @model_validator(mode="after")
    def _has_model_readable_text(self) -> ToolFinishedData:
        if not any(isinstance(part, ToolTextContent) for part in self.content):
            raise ValueError("tool terminal state requires at least one text content (§2.1 L72)")
        return self


class TextDeltaData(BaseModel):
    """``py.text.delta`` ephemeral data (§3.1 L140): live preview only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    delta: str


class ReasoningDeltaData(BaseModel):
    """``py.reasoning.delta`` ephemeral data (§3.1 L140): live preview only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    delta: str


class ToolInputDeltaData(BaseModel):
    """``py.tool.input.delta`` ephemeral data (§3.1 L140): raw argument fragments by call_index."""

    model_config = ConfigDict(extra="forbid")

    call_index: int = Field(ge=0)
    delta: str

    @field_validator("call_index", mode="before")
    @classmethod
    def _call_index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("call_index", value)


class ToolProgressData(BaseModel):
    """``py.tool.progress`` ephemeral data (§3.1 L140): adapter progress metadata by execution."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)


def _require_run_metadata(event_type: str, metadata: JsonObject) -> None:
    """Run-scoped events must carry run_id in envelope metadata (§3.1).

    The run_id metadata key, whenever it appears, must be a string matching ^run_
    (§3.1); null, non-string, or wrongly-prefixed values are rejected.
    """
    if event_type in RUN_SCOPED_EVENT_TYPES and "run_id" not in metadata:
        raise ValueError(f"metadata.run_id is required for run-scoped event type {event_type!r}")
    if "run_id" in metadata:
        value = metadata["run_id"]
        if not isinstance(value, str) or not value.startswith("run_"):
            raise ValueError("metadata.run_id, when present, must be a string matching ^run_")


class EventDraft[DataT: BaseModel](BaseModel):
    """Pre-commit draft (§3.1): id/type/version/session_id/created_at/data/metadata, no seq.

    IDs are generated before the first retry attempt and reused across retries. The
    registry is not consulted at construction time; commit-time validation owns
    type/version/membership checks (§3.2).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=_EVT_ID)
    type: str = Field(min_length=1)
    version: int = Field(ge=1)
    session_id: str = Field(min_length=1, pattern=_SES_ID)
    created_at: UtcDateTime
    data: DataT
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def _version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("version", value)

    @model_validator(mode="after")
    def _run_metadata_present(self) -> EventDraft[DataT]:
        _require_run_metadata(self.type, self.metadata)
        return self


class DurableEvent[DataT: BaseModel](EventDraft[DataT]):
    """Committed event: draft + seq, allocated per session inside the DB transaction (seq >= 1)."""

    seq: int = Field(ge=1)

    @field_validator("seq", mode="before")
    @classmethod
    def _seq_not_bool(cls, value: Any) -> Any:
        return _reject_bool("seq", value)


class EphemeralEvent[DataT: BaseModel](BaseModel):
    """Live-only envelope (§3.1): no version, no seq, no durable cursor."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=_EVT_ID)
    type: str = Field(min_length=1)
    session_id: str = Field(min_length=1, pattern=_SES_ID)
    created_at: UtcDateTime
    data: DataT
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _run_metadata_present(self) -> EphemeralEvent[DataT]:
        _require_run_metadata(self.type, self.metadata)
        return self


class EventDecodeError(Exception):
    """Envelope could not be decoded; decode paths raise instead of silently skipping."""


class UnknownEventTypeError(EventDecodeError):
    """The envelope's event type is not in the registry."""


class UnknownEventVersionError(EventDecodeError):
    """The event type is registered, but not under this (type, version) pair."""


class DurabilityMismatchError(EventDecodeError):
    """The event type exists, but on the other bus (durable vs ephemeral)."""


@dataclass(frozen=True)
class EventDefinition:
    """One registry entry; adding an event type = adding one definition line.

    ``version`` keys the durable registry. For ephemeral definitions it is
    bookkeeping only: the ephemeral envelope carries no version field.
    """

    # Named event_type: a field literally named `type` shadows the builtin in
    # subsequent class-body annotations (data_model: type[BaseModel]).
    event_type: str
    durability: Durability
    data_model: type[BaseModel]
    version: int = EVENT_VERSION


def _reject_json_constant(token: str) -> None:
    """Reject NaN/Infinity/-Infinity literals: they are not valid JSON values."""
    raise ValueError(f"non-standard JSON constant {token!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Duplicate JSON object keys are ambiguous; reject instead of last-wins (§6.2.1)."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _load_envelope(raw: str | bytes | JsonObject) -> JsonObject:
    """Accept a dict, JSON text, or JSON bytes and return a JSON object envelope."""
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        # ValueError covers JSONDecodeError, UnicodeDecodeError, rejected NaN constants,
        # and duplicate object keys.
        parsed = loads(text, parse_constant=_reject_json_constant, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as error:
        raise EventDecodeError(f"malformed JSON event envelope: {error}") from error
    if not isinstance(parsed, dict):
        raise EventDecodeError("event envelope must be a JSON object")
    return parsed


def _parametrize(envelope_model: Any, data_model: type[BaseModel]) -> Any:
    """Parametrize a generic envelope model at runtime with the registered data model.

    mypy cannot express a ``type[X]`` value as a generic subscript argument, so the
    base class is routed through ``Any``; runtime behavior is the plain ``Base[Data]``.
    """
    return envelope_model[data_model]


class EventRegistry:
    """type+version -> data model registry; every decode path fails closed."""

    def __init__(self, definitions: Sequence[EventDefinition] = ()) -> None:
        self._durable: dict[tuple[str, int], EventDefinition] = {}
        self._durable_types: set[str] = set()
        self._ephemeral: dict[str, EventDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: EventDefinition) -> None:
        """Add one definition; duplicate keys are rejected loudly (TS durableMap parity)."""
        if definition.durability == "durable":
            key = (definition.event_type, definition.version)
            if key in self._durable:
                raise ValueError(f"Duplicate durable event definition for {versioned_type(*key)}")
            self._durable[key] = definition
            self._durable_types.add(definition.event_type)
            return
        if definition.event_type in self._ephemeral:
            raise ValueError(f"Duplicate ephemeral event definition for {definition.event_type}")
        self._ephemeral[definition.event_type] = definition

    def resolve_durable(self, event_type: str, version: int) -> EventDefinition:
        """Resolve a (type, version) pair to its durable definition."""
        definition = self._durable.get((event_type, version))
        if definition is not None:
            return definition
        if event_type in self._ephemeral:
            raise DurabilityMismatchError(f"Event type {event_type!r} is ephemeral, not durable")
        if event_type in self._durable_types:
            raise UnknownEventVersionError(f"Unknown version {version} for event type {event_type!r}")
        raise UnknownEventTypeError(f"Unknown event type {event_type!r}")

    def resolve_ephemeral(self, event_type: str) -> EventDefinition:
        """Resolve an ephemeral type to its definition; ephemeral envelopes carry no version."""
        definition = self._ephemeral.get(event_type)
        if definition is not None:
            return definition
        if event_type in self._durable_types:
            raise DurabilityMismatchError(f"Event type {event_type!r} is durable, not ephemeral")
        raise UnknownEventTypeError(f"Unknown event type {event_type!r}")

    def decode_durable(self, raw: str | bytes | JsonObject) -> DurableEvent:
        """Decode one committed durable event; unknown type/version raise, never skip."""
        envelope = _load_envelope(raw)
        event_type = envelope.get("type")
        version = envelope.get("version")
        if not isinstance(event_type, str) or isinstance(version, bool) or not isinstance(version, int):
            raise EventDecodeError("durable envelope requires string 'type' and integer 'version'")
        definition = self.resolve_durable(event_type, version)
        return _parametrize(DurableEvent, definition.data_model).model_validate(envelope)

    def decode_ephemeral(self, raw: str | bytes | JsonObject) -> EphemeralEvent:
        """Decode one live ephemeral event; 'version' is not part of the ephemeral envelope."""
        envelope = _load_envelope(raw)
        event_type = envelope.get("type")
        if not isinstance(event_type, str):
            raise EventDecodeError("ephemeral envelope requires string 'type'")
        definition = self.resolve_ephemeral(event_type)
        return _parametrize(EphemeralEvent, definition.data_model).model_validate(envelope)


R1_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedData),
    EventDefinition(event_type=PY_PROMPT_ACCEPTED, durability="durable", data_model=PromptAcceptedData),
)

#: R2 additions (§3.1 R2 subset): 11 durable rows + 4 ephemeral live types.
#: py.permission.* stays out (R3, PHASE1-SLIM §4); registration is additive so
#: R1 logs and fixtures decode identically (plan §0.2).
R2_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_RUN_STARTED, durability="durable", data_model=RunStartedData),
    EventDefinition(event_type=PY_RUN_CANCEL_REQUESTED, durability="durable", data_model=RunCancelRequestedData),
    EventDefinition(event_type=PY_RUN_FINISHED, durability="durable", data_model=RunFinishedData),
    EventDefinition(event_type=PY_STEP_STARTED, durability="durable", data_model=StepStartedData),
    EventDefinition(event_type=PY_STEP_GENERATED, durability="durable", data_model=StepGeneratedData),
    EventDefinition(event_type=PY_STEP_FINISHED, durability="durable", data_model=StepFinishedData),
    EventDefinition(event_type=PY_TEXT_FINALIZED, durability="durable", data_model=TextFinalizedData),
    EventDefinition(event_type=PY_REASONING_FINALIZED, durability="durable", data_model=ReasoningFinalizedData),
    EventDefinition(event_type=PY_TOOL_REQUESTED, durability="durable", data_model=ToolRequestedData),
    EventDefinition(event_type=PY_TOOL_STARTED, durability="durable", data_model=ToolStartedData),
    EventDefinition(event_type=PY_TOOL_FINISHED, durability="durable", data_model=ToolFinishedData),
    EventDefinition(event_type=PY_TEXT_DELTA, durability="ephemeral", data_model=TextDeltaData),
    EventDefinition(event_type=PY_REASONING_DELTA, durability="ephemeral", data_model=ReasoningDeltaData),
    EventDefinition(event_type=PY_TOOL_INPUT_DELTA, durability="ephemeral", data_model=ToolInputDeltaData),
    EventDefinition(event_type=PY_TOOL_PROGRESS, durability="ephemeral", data_model=ToolProgressData),
)


def default_event_registry() -> EventRegistry:
    """Fresh registry over the R1+R2 event subsets (all §3.1 rows through R2-S1)."""
    return EventRegistry((*R1_EVENT_DEFINITIONS, *R2_EVENT_DEFINITIONS))
