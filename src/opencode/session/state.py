"""Deterministic session-state fold (R1-S4; CORE-CONTRACTS §3.3/§2.1/§6.1).

:func:`apply` is the single source of the R1 fold rules: ``py.session.created``
builds the active state (working_dir + config_snapshot + snapshot_hash, §6.1),
``py.prompt.accepted`` appends the embedded user message to the message
sequence (§2.1 ``user`` row). The fold is a pure function (§3.3): it reads no
clock, generates no random ids and performs no external calls; read-model
writes live in :mod:`opencode.session.projector` and the R1-S8 rebuild replays
the log through this same fold. assistant/idle folding is R2 (the §2.1
transition table is the reference, not implemented early); event types outside
the R1 subset raise instead of being skipped (fail closed).

TS mapping (``packages/core/src/session/projector.ts``): TS keeps the state
machine inside the projector/updater adapters; Python extracts the pure fold
so commit-time projection, rebuild and tests share one rule set. The
``SessionEvent.Created`` handler (L541-568) and ``insertMessage`` (L481-504)
map onto the two fold cases below.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    DurableEvent,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
)


class SessionStatus(StrEnum):
    """The four §3.2 session statuses; ``.value`` is the stored wire text."""

    ACTIVE = "active"
    READ_ONLY_DIAGNOSTIC = "read_only_diagnostic"
    CLEANUP_FAILED = "cleanup_failed"
    CLOSED = "closed"


class IllegalTransitionError(ValueError):
    """The event cannot legally extend this state (§3.2 legal-transition checks).

    The fold raises instead of skipping or coercing; the projector maps this
    onto ``StorageCorruptError`` and the R1-S8 rebuild onto read-only
    diagnostics (§3.2.1).
    """


@dataclass(frozen=True, slots=True)
class SessionState:
    """Fold state of one session: empty until ``py.session.created`` is folded.

    ``session_id``/``status`` are ``None`` before the create event. The §3.2.1
    minimal diagnostic record (an administrative row for a quarantined session
    whose read model is gone) is not producible by the fold and is never
    represented here.
    """

    session_id: str | None = None
    status: SessionStatus | None = None
    applied_seq: int = 0
    working_dir: str | None = None
    config_snapshot: ConfigSnapshot | None = None
    snapshot_hash: str | None = None
    messages: tuple[UserMessage, ...] = ()

    @property
    def created(self) -> bool:
        """True once ``py.session.created`` has been folded."""
        return self.session_id is not None


def apply(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """Fold one durable event onto ``state`` (pure; same input -> same output).

    Fail closed on: ownership mismatch (event belongs to another session), a
    second ``py.session.created``, ``py.prompt.accepted`` without an active
    session, a data model that does not match the event type, and any event
    type outside the R1 subset.
    """
    if state.created and state.session_id != event.session_id:
        raise IllegalTransitionError(
            f"event {event.id} belongs to session {event.session_id!r}, not {state.session_id!r}"
        )
    if event.type == PY_SESSION_CREATED:
        return _apply_created(state, event)
    if event.type == PY_PROMPT_ACCEPTED:
        return _apply_prompt(state, event)
    raise IllegalTransitionError(f"event type {event.type!r} is not foldable in R1")


def _apply_created(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.session.created``: empty -> active with the §6.1 snapshot fields."""
    if not isinstance(event.data, SessionCreatedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected SessionCreatedData"
        )
    if state.created:
        raise IllegalTransitionError(f"session {event.session_id} is already created (event {event.id})")
    return SessionState(
        session_id=event.session_id,
        status=SessionStatus.ACTIVE,
        applied_seq=event.seq,
        working_dir=event.data.working_dir,
        config_snapshot=event.data.config_snapshot,
        snapshot_hash=event.data.snapshot_hash,
        messages=(),
    )


def _apply_prompt(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.prompt.accepted``: active -> active, user message appended (§2.1)."""
    if not isinstance(event.data, PromptAcceptedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected PromptAcceptedData"
        )
    if not state.created or state.status is not SessionStatus.ACTIVE:
        raise IllegalTransitionError(f"py.prompt.accepted requires an active session (event {event.id})")
    return replace(state, applied_seq=event.seq, messages=(*state.messages, event.data.message))
