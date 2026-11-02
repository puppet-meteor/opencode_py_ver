"""Read-model projector for the R1 event subset (R1-S4; CORE-CONTRACTS §3.2/§3.3).

:class:`SessionProjector` implements the §3.2 ``Projector`` Protocol injected
into :class:`opencode.storage.SessionStore`: it writes the sessions/messages
read model inside the commit transaction and advances
``sessions.applied_seq`` (the projector advances, the store re-verifies A=H+n
before committing -- the T-B-008-confirmed split). All transition rules live
in :mod:`opencode.session.state` (the pure fold): this module folds each
event through :func:`opencode.session.state.apply` and translates the fold
output into rows; it never inlines fold rules. The R1-S8 rebuild reuses this
projector over the same fold.

TS mapping (``packages/core/src/session/projector.ts``):
- ``SessionEvent.Created`` handler (L541-568): insert the SessionTable row,
  ``onConflictDoNothing`` and ``SessionAlreadyProjected`` when the row exists
  -- Python maps that defect to ``StorageCorruptError`` (duplicate
  projection).
- ``insertMessage`` (L481-504): message order = ``event.durable.seq``; the
  Python ``messages`` table has no separate type/created columns, so the full
  :class:`~opencode.contracts.UserMessage` JSON is the ``data`` column and
  ``message_id`` is the embedded message id.
- ``run()``/SessionMessageUpdater (L261-476) and ``projectIdle`` (L506-535)
  are R2+ (assistant/idle folding); the ``bus.project`` registration
  (bus.ts L904-912) is deliberately absent -- the projector is injected into
  the store, the single write path (S3 design, no second entry point).

Projection-time failures raise ``StorageCorruptError(isolate=False)``: they
occur against a healthy pre-state (the store's write gate already rejected
non-active sessions before idempotency), so the batch is rejected and rolled
back without quarantining the session; isolation stays with the store's
detections over already-stored data (§3.2.1) and with the S8 rebuild.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    DurableEvent,
    JsonObject,
    canonical_json,
)
from opencode.session.state import (
    IllegalTransitionError,
    SessionState,
    SessionStatus,
    apply,
)
from opencode.storage import StorageCorruptError

#: Reasons recorded in StorageCorruptError details (sanitized; no raw row content).
REASON_DUPLICATE_SESSION_PROJECTION = "duplicate_session_projection"
REASON_PROMPT_WITHOUT_SESSION = "prompt_accepted_without_session"
REASON_ILLEGAL_TRANSITION = "illegal_projection_transition"
REASON_UNKNOWN_SESSION_STATUS = "unknown_session_status"
REASON_DUPLICATE_MESSAGE_ID = "duplicate_message_id"

_SELECT_SESSION = sa.text("SELECT status, applied_seq FROM sessions WHERE session_id = :sid")
_INSERT_SESSION = sa.text(
    "INSERT INTO sessions (session_id, status, config_snapshot, snapshot_hash, applied_seq) "
    "VALUES (:sid, :status, :config_snapshot, :snapshot_hash, :applied_seq)"
)
_INSERT_MESSAGE = sa.text(
    "INSERT INTO messages (session_id, message_id, seq, data) VALUES (:sid, :message_id, :seq, :data)"
)
_ADVANCE_APPLIED_SEQ = sa.text("UPDATE sessions SET applied_seq = :applied_seq WHERE session_id = :sid")


def _corrupt(reason: str, event: DurableEvent[Any], message: str) -> StorageCorruptError:
    """Caller-facing StorageCorrupt with sanitized, locatable details."""
    details: JsonObject = {
        "reason": reason,
        "session_id": event.session_id,
        "event_id": event.id,
        "seq": event.seq,
    }
    return StorageCorruptError(message, isolate=False, details=details)


def _fold(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """Apply the pure fold; illegal transitions become StorageCorrupt."""
    try:
        return apply(state, event)
    except IllegalTransitionError as error:
        raise _corrupt(REASON_ILLEGAL_TRANSITION, event, str(error)) from error


class SessionProjector:
    """The real §3.2 projector over sessions/messages (R1 event subset).

    Stateless -- no clock, no randomness, no external calls (§3.3) -- so one
    instance may serve any session or be shared across stores.
    """

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Project one durable event inside the event-insert transaction."""
        if event.type == PY_SESSION_CREATED:
            await self._project_created(connection, event)
        elif event.type == PY_PROMPT_ACCEPTED:
            await self._project_prompt(connection, event)
        else:
            raise _corrupt(REASON_ILLEGAL_TRANSITION, event, f"event type {event.type!r} is not projectable in R1")

    async def _project_created(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """``py.session.created`` -> INSERT the sessions row (active, §6.1 fields)."""
        projected = _fold(SessionState(), event)
        if projected.status is None or projected.config_snapshot is None or projected.snapshot_hash is None:
            # Unreachable for a legal created event; kept so every projector
            # failure surfaces as a StoreError (fail closed).
            raise _corrupt(REASON_ILLEGAL_TRANSITION, event, "fold output lacks the created fields")
        try:
            await connection.execute(
                _INSERT_SESSION,
                {
                    "sid": event.session_id,
                    "status": projected.status.value,
                    "config_snapshot": canonical_json(projected.config_snapshot.model_dump(mode="json")),
                    "snapshot_hash": projected.snapshot_hash,
                    "applied_seq": event.seq,
                },
            )
        except IntegrityError as error:
            # TS parity: onConflictDoNothing + SessionAlreadyProjected -> die.
            raise _corrupt(
                REASON_DUPLICATE_SESSION_PROJECTION,
                event,
                f"py.session.created projected onto the existing session {event.session_id}",
            ) from error

    async def _project_prompt(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """``py.prompt.accepted`` -> INSERT the messages row, then advance A."""
        row = (await connection.execute(_SELECT_SESSION, {"sid": event.session_id})).first()
        if row is None:
            # Through the commit path this is a batch whose first event is not
            # py.session.created (first-create is gate-exempt); the log it
            # would produce is transition-corrupt, so reject without
            # isolating (progress-file open question).
            raise _corrupt(
                REASON_PROMPT_WITHOUT_SESSION,
                event,
                f"py.prompt.accepted for session {event.session_id} without a sessions row",
            )
        try:
            status = SessionStatus(str(row.status))
        except ValueError as error:
            raise _corrupt(REASON_UNKNOWN_SESSION_STATUS, event, f"unknown session status {row.status!r}") from error
        projected = _fold(
            SessionState(session_id=event.session_id, status=status, applied_seq=int(row.applied_seq)),
            event,
        )
        message = projected.messages[-1]
        try:
            await connection.execute(
                _INSERT_MESSAGE,
                {
                    "sid": event.session_id,
                    "message_id": message.id,
                    "seq": event.seq,
                    "data": canonical_json(message.model_dump(mode="json")),
                },
            )
        except IntegrityError as error:
            raise _corrupt(
                REASON_DUPLICATE_MESSAGE_ID,
                event,
                f"message id {message.id} is already projected for session {event.session_id}",
            ) from error
        await self._advance_applied_seq(connection, event)

    async def _advance_applied_seq(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Advance ``sessions.applied_seq`` to the projected event's seq.

        A separate method so fault-injection tests can skip exactly this
        advance and prove the store's A=H+n re-verification catches it.
        """
        await connection.execute(_ADVANCE_APPLIED_SEQ, {"sid": event.session_id, "applied_seq": event.seq})
