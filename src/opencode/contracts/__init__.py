"""Core wire contracts (R1-S1): event envelopes + registry, UserMessage, CoreError.

Single source of truth for the shapes: doc/CORE-CONTRACTS.md §2/§3.1/§8, with
TS behavior mapping per doc/parity.md §3 and ``packages/schema/src/event.ts``.
Later slices (store/bus/service) import from here; they must not redeclare
wire models.
"""

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    R1_EVENT_DEFINITIONS,
    RUN_SCOPED_EVENT_TYPES,
    ConfigSnapshot,
    Durability,
    DurabilityMismatchError,
    DurableEvent,
    EphemeralEvent,
    EventDecodeError,
    EventDefinition,
    EventDraft,
    EventRegistry,
    PromptAcceptedData,
    SessionCreatedData,
    UnknownEventTypeError,
    UnknownEventVersionError,
    canonical_json,
    compute_snapshot_hash,
    default_event_registry,
    versioned_type,
)
from opencode.contracts.messages import JsonObject, UserMessage, UtcDateTime

__all__ = [
    "EVENT_VERSION",
    "PY_PROMPT_ACCEPTED",
    "PY_SESSION_CREATED",
    "R1_EVENT_DEFINITIONS",
    "RUN_SCOPED_EVENT_TYPES",
    "ConfigSnapshot",
    "CoreError",
    "CoreErrorCode",
    "Durability",
    "DurabilityMismatchError",
    "DurableEvent",
    "EphemeralEvent",
    "EventDecodeError",
    "EventDefinition",
    "EventDraft",
    "EventRegistry",
    "JsonObject",
    "PromptAcceptedData",
    "SessionCreatedData",
    "UnknownEventTypeError",
    "UnknownEventVersionError",
    "UserMessage",
    "UtcDateTime",
    "canonical_json",
    "compute_snapshot_hash",
    "default_event_registry",
    "versioned_type",
]
