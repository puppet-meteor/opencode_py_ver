"""Event wire contracts (CORE-CONTRACTS §3.1): envelopes, type+version registry, R1 event subset.

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

from opencode.contracts.messages import JsonObject, UserMessage, UtcDateTime

Durability = Literal["durable", "ephemeral"]

PY_SESSION_CREATED = "py.session.created"
PY_PROMPT_ACCEPTED = "py.prompt.accepted"

EVENT_VERSION = 1

#: Event types that belong to a run; their envelope metadata must carry run_id (§3.1).
RUN_SCOPED_EVENT_TYPES: frozenset[str] = frozenset({PY_PROMPT_ACCEPTED})

_SHA256_HEX = r"^[0-9a-f]{64}$"


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
    def _schema_version_must_not_be_bool(cls, value: Any) -> Any:
        """Literal[1] equality accepts True (bool is an int subclass); §6.1 forbids that."""
        if isinstance(value, bool):
            raise ValueError("schema_version must be the integer 1, not a boolean")
        return value

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
    run_id: str = Field(min_length=1)
    message: UserMessage


def _require_run_metadata(event_type: str, metadata: JsonObject) -> None:
    """Run-scoped events must carry run_id in envelope metadata (§3.1)."""
    if event_type in RUN_SCOPED_EVENT_TYPES and "run_id" not in metadata:
        raise ValueError(f"metadata.run_id is required for run-scoped event type {event_type!r}")


class EventDraft[DataT: BaseModel](BaseModel):
    """Pre-commit draft (§3.1): id/type/version/session_id/created_at/data/metadata, no seq.

    IDs are generated before the first retry attempt and reused across retries. The
    registry is not consulted at construction time; commit-time validation owns
    type/version/membership checks (§3.2).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    version: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    created_at: UtcDateTime
    data: DataT
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _run_metadata_present(self) -> EventDraft[DataT]:
        _require_run_metadata(self.type, self.metadata)
        return self


class DurableEvent[DataT: BaseModel](EventDraft[DataT]):
    """Committed event: draft + seq, allocated per session inside the DB transaction (seq >= 1)."""

    seq: int = Field(ge=1)


class EphemeralEvent[DataT: BaseModel](BaseModel):
    """Live-only envelope (§3.1): no version, no seq, no durable cursor."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
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


def default_event_registry() -> EventRegistry:
    """Fresh registry over the R1 event subset (the two §3.1 rows R1 implements)."""
    return EventRegistry(R1_EVENT_DEFINITIONS)
