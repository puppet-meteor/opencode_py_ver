"""Read-only repositories over the S4 read model (R1-S6, CORE-CONTRACTS §3.2.1/§7).

Two query surfaces only -- no writes, no bus imports (storage stays bus-free,
asserted structurally by tests/integration/test_eventlog.py), no external
calls (no clock, no randomness, no network/subprocess, the S4 assertion
pattern): queries are deterministic functions of stored rows.

- :meth:`ReadRepositories.session` reads one sessions row (status/applied_seq
  and the other S4 read-model fields) for the EventLog diagnostic gate and the
  S7 status query. A missing row returns ``None`` (the caller decides whether
  that is NotFound or corruption); per §3.2.1 "session_status 仍可查" holds in
  every non-active status, ``read_only_diagnostic`` included.
- :meth:`ReadRepositories.messages` pages the projected messages in event-seq
  order (§2.1: message ownership and ordering live in the session projection,
  never in ID lexicographic order), cursor-paginated by ``after_message_id``
  (exclusive) with a clamped limit.

Fail-closed semantics (§3.2.1 "读取仍可用" boundary): a sessions row missing
while the event log exists is NOT faked as an empty history -- it raises
StorageCorrupt (``sessions_row_missing_with_log``); a message row whose data
no longer decodes into a :class:`~opencode.contracts.UserMessage` raises
StorageCorrupt locating the offending row. Unknown cursor message ids raise
InvalidCursor; a never-created session raises NotFound.

The JSON-column decode helper ``_json_column`` is imported from
:mod:`opencode.storage.txn` (private cross-module import -- a known tradeoff
accepted by the task book: the shared codec module extraction is deliberately
NOT done here; see the progress file for the reviewer note).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.contracts import CoreErrorCode, JsonObject, UserMessage
from opencode.storage.txn import (
    REASON_READ_MODEL_MISSING,
    StorageCorruptError,
    StoreError,
    _json_column,
)

#: Read-side limit bounds (symmetric with the EventLog read contract, §4).
MIN_PAGE_LIMIT = 1
MAX_PAGE_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 100

#: A projected message row that no longer decodes (sanitized reason).
REASON_MESSAGE_ROW_DECODE_FAILED = "message_row_decode_failed"

#: A sessions row whose JSON columns no longer decode (sanitized reason).
REASON_SESSION_ROW_DECODE_FAILED = "session_row_decode_failed"

_SELECT_SESSION = sa.text(
    "SELECT session_id, status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
    "FROM sessions WHERE session_id = :sid"
)
_SELECT_LOG_HIGH = sa.text("SELECT COALESCE(MAX(seq), 0) AS h FROM events WHERE session_id = :sid")
_SELECT_MESSAGE_SEQ = sa.text("SELECT seq FROM messages WHERE session_id = :sid AND message_id = :mid")
_SELECT_MESSAGE_PAGE = sa.text(
    "SELECT message_id, seq, data FROM messages WHERE session_id = :sid AND seq > :after ORDER BY seq LIMIT :limit"
)


class NotFoundError(StoreError):
    """The requested session was never created (§4: 未建 session 报 NotFound)."""

    code = CoreErrorCode.NOT_FOUND


class InvalidCursorError(StoreError):
    """A pagination cursor is malformed or does not belong to this session/query."""

    code = CoreErrorCode.INVALID_CURSOR


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One sessions row as the S4 read model sees it (status/applied_seq face)."""

    session_id: str
    status: str
    applied_seq: int
    active_run_id: str | None = None
    config_snapshot: JsonObject | None = None
    snapshot_hash: str | None = None
    diagnostic: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class MessagePage:
    """One ordered page of projected messages (cursor-paginated by message id)."""

    items: list[UserMessage]
    next_after_message_id: str | None
    has_more: bool


def _corrupt(reason: str, message: str, details: JsonObject) -> StorageCorruptError:
    """Sanitized StorageCorrupt over already-stored read-model data (§3.2.1)."""
    return StorageCorruptError(message, isolate=True, details=details)


def clamp_page_limit(limit: int) -> int:
    """Clamp a page limit into 1..1000 (§4 limit semantics; bools/non-ints reject)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer (bools are rejected)")
    return max(MIN_PAGE_LIMIT, min(MAX_PAGE_LIMIT, limit))


class ReadRepositories:
    """Read-only query surface over sessions/messages (S4 read model).

    One instance may serve any session. Every method opens its own short read
    transaction; nothing here writes, locks, notifies, or performs external
    calls (§3.2.1: queries neither replay the log nor trigger recovery).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def session(self, session_id: str) -> SessionRecord | None:
        """Read one sessions row; ``None`` when the row does not exist.

        The status is returned as stored (unknown values included) -- deciding
        what an unknown status means is the caller's (commit-path) job. JSON
        columns are decoded; an undecodable column raises StorageCorrupt with
        the sanitized reason (no raw row content in details).
        """
        _require_session_id(session_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_SESSION, {"sid": session_id})).first()
        if row is None:
            return None
        try:
            config_snapshot = _json_column(row.config_snapshot)
            diagnostic = _json_column(row.diagnostic)
        except ValueError as error:
            raise _corrupt(
                REASON_SESSION_ROW_DECODE_FAILED,
                f"session {session_id} has an undecodable JSON column",
                {"reason": REASON_SESSION_ROW_DECODE_FAILED, "session_id": session_id},
            ) from error
        return SessionRecord(
            session_id=str(row.session_id),
            status=str(row.status),
            applied_seq=int(row.applied_seq),
            active_run_id=row.active_run_id,
            config_snapshot=config_snapshot if isinstance(config_snapshot, dict) else None,
            snapshot_hash=row.snapshot_hash,
            diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
        )

    async def messages(
        self,
        session_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> MessagePage:
        """Page the projected messages of one session in seq order.

        ``after_message_id`` is an exclusive cursor (``None`` = from the
        first message); ``limit`` is clamped into 1..1000. A message id that
        does not exist in this session raises InvalidCursor. A missing
        sessions row is NotFound for a never-created session and
        StorageCorrupt (``sessions_row_missing_with_log``) when the event log
        still exists -- a missing projection is never faked as empty history
        (§3.2.1). Message rows are decoded fail-closed; one bad row fails the
        whole page without returning a half page.
        """
        _require_session_id(session_id)
        if after_message_id is not None and (not isinstance(after_message_id, str) or not after_message_id):
            raise ValueError("after_message_id must be None or a non-empty string")
        page_limit = clamp_page_limit(limit)

        record = await self.session(session_id)
        if record is None:
            high = await self._log_high(session_id)
            if high > 0:
                raise _corrupt(
                    REASON_READ_MODEL_MISSING,
                    f"session {session_id} has an event log but no sessions row",
                    {"reason": REASON_READ_MODEL_MISSING, "session_id": session_id},
                )
            raise NotFoundError(f"session {session_id} does not exist", details={"session_id": session_id})

        async with self._engine.connect() as connection:
            after_seq = 0
            if after_message_id is not None:
                cursor_row = (
                    await connection.execute(_SELECT_MESSAGE_SEQ, {"sid": session_id, "mid": after_message_id})
                ).first()
                if cursor_row is None:
                    raise InvalidCursorError(
                        f"message id {after_message_id!r} does not exist in session {session_id!r}",
                        details={"session_id": session_id, "after_message_id": after_message_id},
                    )
                after_seq = int(cursor_row.seq)
            rows = (
                await connection.execute(
                    _SELECT_MESSAGE_PAGE, {"sid": session_id, "after": after_seq, "limit": page_limit + 1}
                )
            ).fetchall()

        items: list[UserMessage] = []
        for row in rows:
            items.append(self._decode_message_row(session_id, row))
        has_more = len(items) > page_limit
        if has_more:
            items = items[:page_limit]
        next_after = items[-1].id if items else after_message_id
        return MessagePage(items=items, next_after_message_id=next_after, has_more=has_more)

    async def _log_high(self, session_id: str) -> int:
        """H = COALESCE(MAX(events.seq), 0) -- the committed log watermark."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_LOG_HIGH, {"sid": session_id})).first()
        return int(row.h) if row is not None else 0

    def _decode_message_row(self, session_id: str, row: Any) -> UserMessage:
        """Decode one projected message row, fail closed (§3.2.1 read boundary)."""
        try:
            data = _json_column(row.data)
            message = UserMessage.model_validate(data)
        except (ValueError, TypeError) as error:
            raise _corrupt(
                REASON_MESSAGE_ROW_DECODE_FAILED,
                f"session {session_id} has an undecodable projected message row",
                {
                    "reason": REASON_MESSAGE_ROW_DECODE_FAILED,
                    "session_id": session_id,
                    "message_id": str(row.message_id),
                    "seq": int(row.seq),
                },
            ) from error
        if message.id != str(row.message_id):
            raise _corrupt(
                REASON_MESSAGE_ROW_DECODE_FAILED,
                f"session {session_id} message row id and payload id disagree",
                {
                    "reason": REASON_MESSAGE_ROW_DECODE_FAILED,
                    "session_id": session_id,
                    "message_id": str(row.message_id),
                    "seq": int(row.seq),
                },
            )
        return message


def _require_session_id(session_id: str) -> None:
    """State-independent caller-input check (rejected before any DB access)."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
