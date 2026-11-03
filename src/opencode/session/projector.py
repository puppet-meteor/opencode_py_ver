"""Read-model projector for the R1+R2 event subsets (R2-S2; CORE-CONTRACTS §3.2/§3.3).

:class:`SessionProjector` implements the §3.2 ``Projector`` Protocol injected
into :class:`opencode.storage.SessionStore`: it writes the
sessions/messages/tool_executions/runs read model inside the commit transaction
and advances ``sessions.applied_seq`` (the projector advances, the store
re-verifies A=H+n before committing -- the T-B-008-confirmed split). All
transition rules live in :mod:`opencode.session.state` (the pure fold): this
module folds each event through :func:`opencode.session.state.apply` and
translates the fold output into rows; it never inlines fold rules. The R1-S8
rebuild reuses this projector over the same fold.

Statelessness (§3.3): the projector keeps no per-session cache. The R1 faces
(created/prompt) reconstruct the small pre-state from the sessions row; the R2
faces reconstruct the fold pre-state by replaying the session's stored log
prefix (events with ``seq < event.seq``, visible in the same transaction --
the writer inserts the whole batch before projecting) through the same fold,
then write the fold diff as rows. That replay is O(log length) per event --
correctness first; an incremental projection cache is a later optimization
(run_status queries read the runs table only and never replay, §3.2 L187).

TS mapping (``packages/core/src/session/projector.ts``):
- ``SessionEvent.Created`` handler (L541-568): insert the SessionTable row,
  ``onConflictDoNothing`` and ``SessionAlreadyProjected`` when the row exists
  -- Python maps that defect to ``StorageCorruptError`` (duplicate
  projection).
- ``insertMessage`` (L481-504): message order = ``event.durable.seq``; the
  ``messages`` table stores the full message JSON in ``data``. Assistant
  messages land at ``py.step.generated`` and are replaced (UPDATE by
  message_id) at ``py.step.finished``; idle messages land at
  ``py.run.finished``.
- ``run()``/SessionMessageUpdater (L261-476) and ``projectIdle`` (L506-535)
  map onto the fold + the diff writers below. The ``bus.project``
  registration (bus.ts L904-912) is deliberately absent -- the projector is
  injected into the store, the single write path (S3 design).

Projection-time failures raise ``StorageCorruptError(isolate=False)``: they
occur against a healthy pre-state, so the batch is rejected and rolled back
without quarantining the session; isolation stays with the store's detections
over already-stored data (§3.2.1) and with the S8 rebuild.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    DurableEvent,
    EventDecodeError,
    JsonObject,
    canonical_json,
    default_event_registry,
)
from opencode.contracts.events import (
    PY_REASONING_FINALIZED,
    PY_RUN_CANCEL_REQUESTED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_STEP_FINISHED,
    PY_STEP_GENERATED,
    PY_STEP_STARTED,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
)
from opencode.session.state import (
    IllegalTransitionError,
    RunStatus,
    SessionState,
    SessionStatus,
    apply,
)
from opencode.storage import StorageCorruptError
from opencode.storage.txn import from_epoch_ms

#: Reasons recorded in StorageCorruptError details (sanitized; no raw row content).
REASON_DUPLICATE_SESSION_PROJECTION = "duplicate_session_projection"
REASON_PROMPT_WITHOUT_SESSION = "prompt_accepted_without_session"
REASON_ILLEGAL_TRANSITION = "illegal_projection_transition"
REASON_UNKNOWN_SESSION_STATUS = "unknown_session_status"
REASON_DUPLICATE_MESSAGE_ID = "duplicate_message_id"
REASON_DUPLICATE_RUN = "duplicate_run_projection"
REASON_DUPLICATE_TOOL_EXECUTION = "duplicate_tool_execution_projection"
REASON_READ_MODEL_ROW_MISSING = "read_model_row_missing"
REASON_STORED_DECODE_FAILED = "stored_event_decode_failed"

#: The R2 durable subset this projector translates (§3.1 L124-132).
_R2_DURABLE_TYPES = frozenset(
    {
        PY_RUN_STARTED,
        PY_RUN_CANCEL_REQUESTED,
        PY_RUN_FINISHED,
        PY_STEP_STARTED,
        PY_STEP_GENERATED,
        PY_STEP_FINISHED,
        PY_TEXT_FINALIZED,
        PY_REASONING_FINALIZED,
        PY_TOOL_REQUESTED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
    }
)

#: Registry used to decode stored rows for the stateless pre-state replay.
_REPLAY_REGISTRY = default_event_registry()

_SELECT_SESSION = sa.text("SELECT status, applied_seq, active_run_id FROM sessions WHERE session_id = :sid")
_INSERT_SESSION = sa.text(
    "INSERT INTO sessions (session_id, status, config_snapshot, snapshot_hash, applied_seq) "
    "VALUES (:sid, :status, :config_snapshot, :snapshot_hash, :applied_seq)"
)
_INSERT_MESSAGE = sa.text(
    "INSERT INTO messages (session_id, message_id, seq, data) VALUES (:sid, :message_id, :seq, :data)"
)
_UPDATE_MESSAGE = sa.text("UPDATE messages SET data = :data WHERE session_id = :sid AND message_id = :message_id")
_SET_ACTIVE_RUN = sa.text("UPDATE sessions SET active_run_id = :run_id WHERE session_id = :sid")
_ADVANCE_APPLIED_SEQ = sa.text("UPDATE sessions SET applied_seq = :applied_seq WHERE session_id = :sid")
_SELECT_RUN = sa.text("SELECT session_id, request_id, status FROM runs WHERE session_id = :sid AND run_id = :run_id")
_INSERT_RUN = sa.text(
    "INSERT INTO runs (run_id, session_id, request_id, status, error) "
    "VALUES (:run_id, :sid, :request_id, :status, :error)"
)
_UPDATE_RUN = sa.text("UPDATE runs SET status = :status, error = :error WHERE session_id = :sid AND run_id = :run_id")
_INSERT_TOOL_EXECUTION = sa.text(
    "INSERT INTO tool_executions (session_id, execution_id, run_id, step_id, message_id, tool_call_id, "
    "input_hash, state) VALUES (:sid, :execution_id, :run_id, :step_id, :message_id, :tool_call_id, "
    ":input_hash, :state)"
)
_UPDATE_TOOL_EXECUTION = sa.text(
    "UPDATE tool_executions SET state = :state WHERE session_id = :sid AND execution_id = :execution_id"
)
_SELECT_EVENTS_BEFORE = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata "
    "FROM events WHERE session_id = :sid AND seq < :seq ORDER BY seq"
)


def _corrupt(reason: str, event: DurableEvent[Any], message: str) -> StorageCorruptError:
    """Caller-facing StorageCorrupt with sanitized, locatable details."""
    details: JsonObject = {
        "reason": reason,
        "session_id": event.session_id,
        "event_id": event.id,
        "seq": event.seq,
    }
    return StorageCorruptError(message, isolate=False, details=details)


def _corrupt_row(reason: str, session_id: str, seq: int, event_id: str, message: str) -> StorageCorruptError:
    """Corrupt over a stored row that could not be decoded/replayed."""
    details: JsonObject = {"reason": reason, "session_id": session_id, "event_id": event_id, "seq": seq}
    return StorageCorruptError(message, isolate=False, details=details)


def _fold(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """Apply the pure fold; illegal transitions become StorageCorrupt."""
    try:
        return apply(state, event)
    except IllegalTransitionError as error:
        raise _corrupt(REASON_ILLEGAL_TRANSITION, event, str(error)) from error


def _json_column(value: Any) -> Any:
    """Stored JSON column -> parsed value (``text()`` rows return driver strings)."""
    if isinstance(value, str | bytes | bytearray):
        return json.loads(value)
    return value


class SessionProjector:
    """The real §3.2 projector over sessions/messages/tool_executions/runs.

    Stateless -- no clock, no randomness, no external calls (§3.3) -- so one
    instance may serve any session or be shared across stores.
    """

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Project one durable event inside the event-insert transaction."""
        if event.type == PY_SESSION_CREATED:
            await self._project_created(connection, event)
        elif event.type == PY_PROMPT_ACCEPTED:
            await self._project_prompt(connection, event)
        elif event.type in _R2_DURABLE_TYPES:
            await self._project_r2(connection, event)
        else:
            raise _corrupt(REASON_ILLEGAL_TRANSITION, event, f"event type {event.type!r} is not projectable")

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
        """``py.prompt.accepted`` -> messages row + runs row + run slot + advance A.

        §7 L316: the same transaction that inserts the UserMessage also sets
        ``sessions.active_run_id``. §3.2 L185: the same transaction also
        projects the accepted run row of the runs read model. All values are
        taken from the fold output, keeping the transition-rule ownership in
        :mod:`opencode.session.state` -- the projector only translates fold
        output into rows.
        """
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
            SessionState(
                session_id=event.session_id,
                status=status,
                applied_seq=int(row.applied_seq),
                active_run_id=row.active_run_id,
            ),
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
        await self._write_accepted_run(connection, event)
        await self._set_active_run_id(connection, event, projected.active_run_id)
        await self._advance_applied_seq(connection, event)

    async def _project_r2(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Replay-fold the stored prefix, fold this event, write the diff as rows.

        The writer inserts the whole batch before projecting any of it, so the
        prefix read (``seq < event.seq``) sees the same-transaction rows of
        earlier batch events -- the finalized-before-generated consistency
        checks work inside one batch.
        """
        pre = await self._replay_state(connection, event.session_id, event.seq)
        post = _fold(pre, event)
        await self._write_message_diff(connection, event, pre, post)
        await self._write_tool_diff(connection, event, pre, post)
        await self._write_run_diff(connection, event, pre, post)
        if post.active_run_id != pre.active_run_id:
            await self._set_active_run_id(connection, event, post.active_run_id)
        await self._advance_applied_seq(connection, event)

    async def _replay_state(self, connection: AsyncConnection, session_id: str, before_seq: int) -> SessionState:
        """Fold the session's stored log prefix (stateless pre-state reconstruction)."""
        state = SessionState()
        rows = (await connection.execute(_SELECT_EVENTS_BEFORE, {"sid": session_id, "seq": before_seq})).fetchall()
        for row in rows:
            seq = int(row.seq)
            try:
                envelope: dict[str, Any] = {
                    "id": row.id,
                    "type": row.type,
                    "version": int(row.version),
                    "session_id": row.session_id,
                    "created_at": from_epoch_ms(int(row.created)),
                    "data": _json_column(row.data),
                    "metadata": _json_column(row.metadata),
                    "seq": seq,
                }
                stored = _REPLAY_REGISTRY.decode_durable(envelope)
            except (EventDecodeError, ValueError) as error:
                raise _corrupt_row(
                    REASON_STORED_DECODE_FAILED,
                    session_id,
                    seq,
                    str(row.id),
                    f"stored event {row.id} does not decode during projection replay",
                ) from error
            state = _fold(state, stored)
        return state

    async def _write_message_diff(
        self, connection: AsyncConnection, event: DurableEvent[Any], pre: SessionState, post: SessionState
    ) -> None:
        """Append new message ids; replace the terminal assistant message by id."""
        previous = {message.id: message for message in pre.messages}
        for message in post.messages:
            old = previous.get(message.id)
            if old is not None and old is message:
                continue
            data = canonical_json(message.model_dump(mode="json"))
            if old is None:
                try:
                    await connection.execute(
                        _INSERT_MESSAGE,
                        {"sid": event.session_id, "message_id": message.id, "seq": event.seq, "data": data},
                    )
                except IntegrityError as error:
                    raise _corrupt(
                        REASON_DUPLICATE_MESSAGE_ID,
                        event,
                        f"message id {message.id} is already projected for session {event.session_id}",
                    ) from error
            else:
                result = await connection.execute(
                    _UPDATE_MESSAGE, {"sid": event.session_id, "message_id": message.id, "data": data}
                )
                if not result.rowcount:
                    raise _corrupt(
                        REASON_READ_MODEL_ROW_MISSING,
                        event,
                        f"message row {message.id} vanished before its terminal replacement",
                    )

    async def _write_tool_diff(
        self, connection: AsyncConnection, event: DurableEvent[Any], pre: SessionState, post: SessionState
    ) -> None:
        """Insert new executions (pending); update the state column on transitions."""
        for execution_id, tool in post.tools.items():
            old = pre.tools.get(execution_id)
            if old is tool:
                continue
            if old is None:
                try:
                    await connection.execute(
                        _INSERT_TOOL_EXECUTION,
                        {
                            "sid": event.session_id,
                            "execution_id": tool.execution_id,
                            "run_id": tool.run_id,
                            "step_id": tool.step_id,
                            "message_id": tool.message_id,
                            "tool_call_id": tool.tool_call_id,
                            "input_hash": tool.input_hash,
                            "state": tool.state.value,
                        },
                    )
                except IntegrityError as error:
                    raise _corrupt(
                        REASON_DUPLICATE_TOOL_EXECUTION,
                        event,
                        f"execution {tool.execution_id} is already projected for session {event.session_id}",
                    ) from error
            elif old.state != tool.state:
                result = await connection.execute(
                    _UPDATE_TOOL_EXECUTION,
                    {"sid": event.session_id, "execution_id": execution_id, "state": tool.state.value},
                )
                if not result.rowcount:
                    raise _corrupt(
                        REASON_READ_MODEL_ROW_MISSING,
                        event,
                        f"tool_executions row {tool.execution_id} vanished before its state update",
                    )

    async def _write_run_diff(
        self, connection: AsyncConnection, event: DurableEvent[Any], pre: SessionState, post: SessionState
    ) -> None:
        """Update run status/error columns on the legal run transitions."""
        for run_id, run in post.runs.items():
            old = pre.runs.get(run_id)
            if old is run:
                continue
            if old is None:
                # Runs enter the read model at py.prompt.accepted (the R1
                # face); reaching here means the fold registered a run the R1
                # face did not write -- a projection-order defect, not stored
                # damage. Fail closed regardless (【protocol unreachable】).
                raise _corrupt(
                    REASON_READ_MODEL_ROW_MISSING,
                    event,
                    f"run {run_id} has no accepted projection row",
                )
            result = await connection.execute(
                _UPDATE_RUN,
                {
                    "sid": event.session_id,
                    "run_id": run_id,
                    "status": run.status.value,
                    "error": canonical_json(run.error.model_dump(mode="json")) if run.error is not None else None,
                },
            )
            if not result.rowcount:
                raise _corrupt(
                    REASON_READ_MODEL_ROW_MISSING,
                    event,
                    f"runs row {run_id} vanished before its status update",
                )

    async def _write_accepted_run(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Insert the accepted run row (idempotent for legacy re-accepts)."""
        data = event.data
        run_id: str = data.run_id
        request_id: str = data.request_id
        row = (await connection.execute(_SELECT_RUN, {"sid": event.session_id, "run_id": run_id})).first()
        if row is not None:
            # The fold already accepted a re-used run identity under the same
            # request (legacy R1 logs reuse one run id): the row must be the
            # same accepted projection, else it is an identity conflict.
            if str(row.request_id) != request_id or str(row.status) != RunStatus.ACCEPTED.value:
                raise _corrupt(
                    REASON_DUPLICATE_RUN,
                    event,
                    f"run {run_id} is already projected with a different identity",
                )
            return
        try:
            await connection.execute(
                _INSERT_RUN,
                {
                    "run_id": run_id,
                    "sid": event.session_id,
                    "request_id": request_id,
                    "status": RunStatus.ACCEPTED.value,
                    "error": None,
                },
            )
        except IntegrityError as error:
            # unique(session_id, request_id): the same request accepted under
            # a different run id (§2.1 L37 identity conflict).
            raise _corrupt(
                REASON_DUPLICATE_RUN,
                event,
                f"request {request_id} is already accepted under another run for session {event.session_id}",
            ) from error

    async def _set_active_run_id(
        self, connection: AsyncConnection, event: DurableEvent[Any], run_id: str | None
    ) -> None:
        """Set ``sessions.active_run_id`` from the fold output (§7 L316 wiring).

        A separate method (mirroring ``_advance_applied_seq``) so the write
        stays independently overridable for fault-injection tests.
        """
        await connection.execute(_SET_ACTIVE_RUN, {"sid": event.session_id, "run_id": run_id})

    async def _advance_applied_seq(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Advance ``sessions.applied_seq`` to the projected event's seq.

        A separate method so fault-injection tests can skip exactly this
        advance and prove the store's A=H+n re-verification catches it.
        """
        await connection.execute(_ADVANCE_APPLIED_SEQ, {"sid": event.session_id, "applied_seq": event.seq})
