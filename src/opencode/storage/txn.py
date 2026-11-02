"""Private transaction writer for the unique commit path (R1-S3, CORE-CONTRACTS §3.2/§3.2.1).

TransactionWriter is the S4-private seam named by CORE-CONTRACTS §3.2 ("S4 的事务
writer 仅供此接口调用"): external modules depend only on
:class:`opencode.storage.store.SessionStore`. It executes the §3.2 commit
sequence in one SQLite write transaction (read H -> write gate -> idempotency ->
H/A invariants -> expected_seq -> ownership/version -> consecutive seq -> insert
-> projector -> verify A=H+n -> commit) and owns the §3.2.1 isolation entry:
after the failing business transaction rolled back entirely, an independent
short transaction persists ``sessions.status = read_only_diagnostic`` plus a
sanitized diagnostic (reason, locatable first bad seq/event_id, applied_seq at
discovery). The diagnostic write produces no business events and does not
advance seq/applied_seq; this module cannot depend on any notification
interface (R1-S5; asserted structurally by tests/integration/test_store_commit.py).

TS mapping (``packages/core/src/bus.ts``): ``commitDurableEvent`` L284-450
(read latest seq, replay compare, seq = latest+1, projector inside the same
transaction, notify only after commit) and ``publishAll`` L535-672
(same-aggregate check, KeyedMutex, consecutive seq, in-batch duplicate id
rejection, already-exists id rejection, single transaction).
``latestSequence``/``reserveSequence`` L22-49 are replaced by
``COALESCE(MAX(events.seq), 0)`` over the events table (CORE-CONTRACTS §3.2:
no separate sequence table); ``durableLocks`` L205 becomes per-session
asyncio locks plus one process-wide write lock (the OS file lock from
``opencode.storage.db`` already serializes writer processes). Python
differences per §3.2: idempotency key = EventBatch.id + event id + content,
checked BEFORE expected_seq; expected_seq is the caller's expected pre-commit
H (equality only, never "catch up"); TS owner/strictOwner replay semantics
are not implemented.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from opencode.contracts import (
    CoreError,
    CoreErrorCode,
    DurableEvent,
    EventDecodeError,
    EventDraft,
    EventRegistry,
    JsonObject,
    canonical_json,
)
from opencode.storage.db import StorageError

if TYPE_CHECKING:
    from opencode.storage.store import EventBatch, Projector

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)

#: Reasons recorded in ``sessions.diagnostic`` (sanitized; no raw row content).
REASON_READ_MODEL_MISSING = "sessions_row_missing_with_log"
REASON_APPLIED_SEQ_MISMATCH = "applied_seq_mismatch"
REASON_SEQ_GAP = "seq_gap"
REASON_DUPLICATE_SEQ = "duplicate_seq"
REASON_ILLEGAL_SEQ = "illegal_seq"
REASON_UNKNOWN_STATUS = "unknown_session_status"
REASON_STORED_DECODE_FAILED = "stored_event_decode_failed"
REASON_PROJECTION_INVARIANT = "applied_seq_not_advanced_after_projection"

_SELECT_SESSION_STATUS = sa.text("SELECT status, applied_seq FROM sessions WHERE session_id = :sid")
_SELECT_SESSION_APPLIED = sa.text("SELECT applied_seq FROM sessions WHERE session_id = :sid")
_SELECT_EVENT_IDS = sa.text("SELECT id, session_id FROM events WHERE id IN :ids").bindparams(
    sa.bindparam("ids", expanding=True)
)
_SELECT_COMMITTED_BATCH = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata, batch_ordinal "
    "FROM events WHERE session_id = :sid AND batch_id = :bid ORDER BY batch_ordinal"
)
_SELECT_LOG_STATS = sa.text(
    "SELECT COUNT(*) AS n, COUNT(DISTINCT seq) AS d, COALESCE(MIN(seq), 0) AS lo, "
    "COALESCE(MAX(seq), 0) AS hi FROM events WHERE session_id = :sid"
)
_SELECT_FIRST_DUPLICATE_SEQ = sa.text(
    "SELECT MIN(seq) AS s FROM (SELECT seq FROM events WHERE session_id = :sid GROUP BY seq HAVING COUNT(*) > 1)"
)
_SELECT_FIRST_GAP_SEQ = sa.text(
    "SELECT MIN(e.seq) + 1 AS s FROM events e WHERE e.session_id = :sid "
    "AND NOT EXISTS (SELECT 1 FROM events e2 WHERE e2.session_id = :sid AND e2.seq = e.seq + 1)"
)
_INSERT_EVENT = sa.text(
    "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
    "batch_id, batch_ordinal, dedupe_key) VALUES "
    "(:id, :session_id, :seq, :type, :version, :created, :data, :metadata, "
    ":batch_id, :batch_ordinal, :dedupe_key)"
)
_UPDATE_SESSION_DIAGNOSTIC = sa.text(
    "UPDATE sessions SET status = 'read_only_diagnostic', diagnostic = :diag WHERE session_id = :sid"
)
_INSERT_SESSION_DIAGNOSTIC = sa.text(
    "INSERT INTO sessions (session_id, status, applied_seq, diagnostic) VALUES (:sid, 'read_only_diagnostic', 0, :diag)"
)


def epoch_ms(value: datetime) -> int:
    """Exact integer epoch milliseconds (storage precision of ``events.created``).

    Millisecond truncation is the storage precision: sub-millisecond differences
    between a retry and the committed row are indistinguishable in the idempotency
    comparison, which is what "retries reuse content" (§3.2) requires (N-3).
    """
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def from_epoch_ms(value: int) -> datetime:
    """Inverse of :func:`epoch_ms` (datetimes carry millisecond precision only)."""
    return _EPOCH + value * _MILLISECOND


def _json_column(value: Any) -> Any:
    """Stored JSON column -> parsed value (``text()`` rows return raw driver strings)."""
    if isinstance(value, str | bytes | bytearray):
        return json.loads(value)
    return value


class StoreError(StorageError):
    """Store operation rejection; carries the §8 CoreError wire shape.

    Layering follows ``opencode.storage.db`` (StorageError base + specific
    subclasses); each subclass pins the locked §8 code so S6/S7 map errors onto
    CoreError without re-deciding.
    """

    code: ClassVar[CoreErrorCode]
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.core = CoreError(
            code=self.code,
            message=message,
            retryable=self.retryable,
            details=details if details is not None else {},
        )


class SessionReadOnlyError(StoreError):
    """The session is isolated (read_only_diagnostic); commits are rejected."""

    code = CoreErrorCode.SESSION_READ_ONLY


class SessionClosedError(StoreError):
    """The session is closed; writes are rejected."""

    code = CoreErrorCode.SESSION_CLOSED


class LifecycleCleanupFailedError(StoreError):
    """The session failed resource cleanup; writes are rejected."""

    code = CoreErrorCode.LIFECYCLE_CLEANUP_FAILED


class StoreValidationError(StoreError):
    """Caller input is invalid (bad expected_seq, ownership, version, batch shape)."""

    code = CoreErrorCode.VALIDATION_ERROR


class ConflictError(StoreError):
    """expected_seq does not equal the current log watermark H on a healthy log."""

    code = CoreErrorCode.CONFLICT


class IdempotencyConflictError(StoreError):
    """Batch identity clashes with committed rows (mixed ids / diverged content)."""

    code = CoreErrorCode.IDEMPOTENCY_CONFLICT


class StorageCorruptError(StoreError):
    """Stored state violates the §3.2 invariants.

    ``isolate`` marks detections over already-stored data (A≠H, seq gaps,
    duplicates, illegal seq, missing sessions row, undecodable stored events),
    which route into the §3.2.1 isolation entry. Transaction-local failures
    from a healthy pre-state (projector did not advance applied_seq) use
    ``isolate=False`` and only roll back.
    """

    code = CoreErrorCode.STORAGE_CORRUPT

    def __init__(self, message: str, *, isolate: bool = True, details: JsonObject | None = None) -> None:
        super().__init__(message, details=details)
        self.isolate = isolate


class IsolationPersistFailedError(StorageError):
    """The diagnostic row could not be persisted; the in-memory write gate holds."""


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Sanitized §3.2.1 diagnostic payload persisted into ``sessions.diagnostic``."""

    reason: str
    first_bad_seq: int | None = None
    first_bad_event_id: str | None = None
    applied_seq: int = 0

    def to_json(self) -> JsonObject:
        """Sanitized JSON object: reason + locatable first bad seq/event_id + A."""
        payload: JsonObject = {"reason": self.reason, "applied_seq": self.applied_seq}
        if self.first_bad_seq is not None:
            payload["first_bad_seq"] = self.first_bad_seq
        if self.first_bad_event_id is not None:
            payload["first_bad_event_id"] = self.first_bad_event_id
        return payload


@dataclass(frozen=True, slots=True)
class _LogStats:
    """Aggregates of one session's event log rows, for invariant classification."""

    count: int
    distinct: int
    low: int
    high: int


def _corruption(reason: str, diagnostic: Diagnostic) -> StorageCorruptError:
    """Caller-facing StorageCorrupt error; details mirror the sanitized diagnostic."""
    details: JsonObject = {"reason": reason}
    details.update(diagnostic.to_json())
    return StorageCorruptError(f"storage corruption detected: {reason}", isolate=True, details=details)


def _diagnostic_from(corruption: StorageCorruptError) -> Diagnostic:
    """Rebuild the sanitized diagnostic from a StorageCorruptError's details."""

    def as_int(key: str) -> int | None:
        value = corruption.core.details.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def as_str(key: str) -> str | None:
        value = corruption.core.details.get(key)
        return value if isinstance(value, str) else None

    reason = as_str("reason") or "unknown"
    return Diagnostic(
        reason=reason,
        first_bad_seq=as_int("first_bad_seq"),
        first_bad_event_id=as_str("first_bad_event_id"),
        applied_seq=as_int("applied_seq") or 0,
    )


class TransactionWriter:
    """S4-private writer: per-session locks, serialized write transactions, isolation.

    All commits and isolation writes run under a per-session asyncio.Lock plus
    one process-wide write lock (§3.2 进程内写事务串行); the OS file lock in
    ``opencode.storage.db`` already excludes other writer processes. The
    per-session lock registry is intentionally unbounded for R1 (one entry per
    quarantined/committed session id).
    """

    def __init__(self, engine: AsyncEngine, *, projector: Projector, registry: EventRegistry) -> None:
        self._engine = engine
        self._projector = projector
        self._registry = registry
        self._write_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._isolated: set[str] = set()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the per-session lock (sync, no await -> no race in asyncio)."""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def commit(self, session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]:
        """Execute the §3.2 commit sequence; corruption paths roll back then isolate."""
        _validate_arguments(session_id, batch, expected_seq)

        async with self._session_lock(session_id):
            async with self._write_lock:
                try:
                    return await self._commit_transaction(session_id, batch, expected_seq)
                except StorageCorruptError as corruption:
                    if corruption.isolate:
                        # The business transaction already rolled back entirely;
                        # isolation runs in an independent short transaction (§3.2.1).
                        try:
                            await self._isolate_locked(session_id, _diagnostic_from(corruption))
                        except StorageError as persist_error:
                            # The diagnostic row could not be persisted: the
                            # in-memory write gate holds and the original
                            # corruption error is re-raised, explicitly
                            # chained to the persistence failure (N-1).
                            raise corruption from persist_error
                    raise

    async def _commit_transaction(
        self, session_id: str, batch: EventBatch, expected_seq: int
    ) -> list[DurableEvent[Any]]:
        async with self._engine.connect() as connection:
            try:
                events = await self._commit_sequence(connection, session_id, batch, expected_seq)
                await connection.commit()
                return events
            except BaseException:
                await connection.rollback()
                raise

    async def _commit_sequence(
        self, connection: AsyncConnection, session_id: str, batch: EventBatch, expected_seq: int
    ) -> list[DurableEvent[Any]]:
        stats = await self._log_stats(connection, session_id)
        session_row = (await connection.execute(_SELECT_SESSION_STATUS, {"sid": session_id})).first()

        # §3.2 step 1: write gate BEFORE the idempotency query (first create exempt:
        # no sessions row with an empty log is a new session, not a missing read model).
        applied = 0
        if session_id in self._isolated:
            raise SessionReadOnlyError(
                f"session {session_id} is quarantined in memory", details={"session_id": session_id}
            )
        if session_row is not None:
            status = str(session_row.status)
            if status == "read_only_diagnostic":
                raise SessionReadOnlyError(
                    f"session {session_id} is read_only_diagnostic", details={"session_id": session_id}
                )
            if status == "cleanup_failed":
                raise LifecycleCleanupFailedError(
                    f"session {session_id} failed resource cleanup", details={"session_id": session_id}
                )
            if status == "closed":
                raise SessionClosedError(f"session {session_id} is closed", details={"session_id": session_id})
            if status != "active":
                raise _corruption(
                    REASON_UNKNOWN_STATUS,
                    Diagnostic(reason=REASON_UNKNOWN_STATUS, applied_seq=int(session_row.applied_seq)),
                )
            applied = int(session_row.applied_seq)
        elif stats.high > 0:
            # Log rows exist but the sessions row is missing: read model missing (§3.2).
            raise _corruption(REASON_READ_MODEL_MISSING, Diagnostic(reason=REASON_READ_MODEL_MISSING, applied_seq=0))

        # §3.2 step 2: idempotency BEFORE expected_seq -- replaying a whole
        # committed batch returns the original events even with a stale
        # expected_seq, with no H/A advance and no re-projection.
        replay = await self._idempotent_replay(connection, session_id, batch)
        if replay is not None:
            return replay

        # §3.2 step 3: all-new events -> verify the H/A invariants first
        # (violations are StorageCorrupt, not Conflict), then expected_seq.
        high = await self._healthy_high(connection, session_id, stats, applied)
        if expected_seq != high:
            raise ConflictError(
                f"expected_seq {expected_seq} does not match the current high watermark {high}",
                details={"session_id": session_id, "expected_seq": expected_seq, "high_watermark": high},
            )

        # §3.2 step 4: ownership/version, then consecutive seq H+1..H+n and insert.
        self._validate_ownership_and_version(session_id, batch)
        durables = await self._insert_and_project(connection, session_id, batch, high)

        # Post-projection invariant: the projection advanced A atomically to
        # H+n. The pre-state was healthy, so a violation only rolls back and
        # never isolates the session.
        after = (await connection.execute(_SELECT_SESSION_APPLIED, {"sid": session_id})).first()
        expected_applied = high + len(batch.events)
        if after is None or int(after.applied_seq) != expected_applied:
            raise StorageCorruptError(
                "projection did not advance sessions.applied_seq to H+n",
                isolate=False,
                details={
                    "reason": REASON_PROJECTION_INVARIANT,
                    "session_id": session_id,
                    "expected_applied_seq": expected_applied,
                },
            )
        return durables

    async def _log_stats(self, connection: AsyncConnection, session_id: str) -> _LogStats:
        """H (COALESCE(MAX(events.seq), 0)) plus the aggregates for invariant checks."""
        row = (await connection.execute(_SELECT_LOG_STATS, {"sid": session_id})).one()
        return _LogStats(count=int(row.n), distinct=int(row.d), low=int(row.lo), high=int(row.hi))

    async def _healthy_high(self, connection: AsyncConnection, session_id: str, stats: _LogStats, applied: int) -> int:
        """Classify A≠H / seq gaps / duplicates / illegal seq as StorageCorrupt."""
        if stats.count == 0:
            return 0
        if stats.low < 1:
            raise _corruption(
                REASON_ILLEGAL_SEQ,
                Diagnostic(reason=REASON_ILLEGAL_SEQ, first_bad_seq=stats.low, applied_seq=applied),
            )
        if stats.distinct != stats.count:
            duplicate = (await connection.execute(_SELECT_FIRST_DUPLICATE_SEQ, {"sid": session_id})).scalar()
            raise _corruption(
                REASON_DUPLICATE_SEQ,
                Diagnostic(
                    reason=REASON_DUPLICATE_SEQ,
                    first_bad_seq=int(duplicate) if duplicate is not None else None,
                    applied_seq=applied,
                ),
            )
        if stats.count != stats.high or stats.low != 1:
            if stats.low != 1:
                first_bad: int | None = 1
            else:
                gap = (await connection.execute(_SELECT_FIRST_GAP_SEQ, {"sid": session_id})).scalar()
                first_bad = int(gap) if gap is not None else None
            raise _corruption(
                REASON_SEQ_GAP,
                Diagnostic(reason=REASON_SEQ_GAP, first_bad_seq=first_bad, applied_seq=applied),
            )
        if applied != stats.high:
            raise _corruption(
                REASON_APPLIED_SEQ_MISMATCH,
                Diagnostic(reason=REASON_APPLIED_SEQ_MISMATCH, applied_seq=applied),
            )
        return stats.high

    def _validate_ownership_and_version(self, session_id: str, batch: EventBatch) -> None:
        """Ownership (all events belong to this session) and version registry checks."""
        for event in batch.events:
            if event.session_id != session_id:
                raise StoreValidationError(
                    f"event {event.id} belongs to session {event.session_id!r}, not {session_id!r}",
                    details={"session_id": session_id, "event_id": event.id},
                )
            try:
                self._registry.resolve_durable(event.type, event.version)
            except EventDecodeError as error:
                raise StoreValidationError(
                    f"event {event.id} has no durable definition for {event.type}.{event.version}",
                    details={"session_id": session_id, "event_id": event.id},
                ) from error

    async def _idempotent_replay(
        self, connection: AsyncConnection, session_id: str, batch: EventBatch
    ) -> list[DurableEvent[Any]] | None:
        """Return the original events for an exact whole-batch retry, else None.

        Rejections (§3.2 step 2; never re-projected): a stored id committed in
        another session, mixed committed+new ids, or same-id-different-content
        under this batch id -- all IdempotencyConflict.
        """
        existing = (
            await connection.execute(_SELECT_EVENT_IDS, {"ids": [event.id for event in batch.events]})
        ).fetchall()
        for row in existing:
            if row.session_id != session_id:
                raise IdempotencyConflictError(
                    f"event id {row.id} is already committed in another session",
                    details={"session_id": session_id, "event_id": str(row.id)},
                )
        committed = (await connection.execute(_SELECT_COMMITTED_BATCH, {"sid": session_id, "bid": batch.id})).fetchall()
        if not committed and not existing:
            return None  # all-new batch
        if len(committed) != len(batch.events):
            raise IdempotencyConflictError(
                "batch event ids do not match the committed batch under this batch id",
                details={"session_id": session_id, "batch_id": batch.id},
            )
        for ordinal, (row, event) in enumerate(zip(committed, batch.events, strict=True)):
            if (
                int(row.batch_ordinal) != ordinal
                or row.id != event.id
                or row.type != event.type
                or int(row.version) != event.version
                or int(row.created) != epoch_ms(event.created_at)
                or _json_column(row.data) != event.data.model_dump(mode="json")
                or _json_column(row.metadata) != event.metadata
            ):
                raise IdempotencyConflictError(
                    f"batch {batch.id} content diverges from the committed batch at ordinal {ordinal}",
                    details={"session_id": session_id, "batch_id": batch.id, "batch_ordinal": ordinal},
                )
        try:
            return [self._decode_stored(row) for row in committed]
        except (EventDecodeError, ValueError) as error:
            raise _corruption(
                REASON_STORED_DECODE_FAILED,
                Diagnostic(
                    reason=REASON_STORED_DECODE_FAILED,
                    first_bad_seq=int(committed[0].seq),
                    first_bad_event_id=str(committed[0].id),
                    applied_seq=0,
                ),
            ) from error

    def _decode_stored(self, row: Any) -> DurableEvent[Any]:
        """Decode one stored events row into a typed DurableEvent (fail closed)."""
        envelope: dict[str, Any] = {
            "id": row.id,
            "type": row.type,
            "version": int(row.version),
            "session_id": row.session_id,
            "created_at": from_epoch_ms(int(row.created)),
            "data": _json_column(row.data),
            "metadata": _json_column(row.metadata),
            "seq": int(row.seq),
        }
        return self._registry.decode_durable(envelope)

    async def _insert_and_project(
        self, connection: AsyncConnection, session_id: str, batch: EventBatch, high: int
    ) -> list[DurableEvent[Any]]:
        """Allocate consecutive seq H+1..H+n, insert rows, run the projector in-tx."""
        envelope_model: Any = DurableEvent
        durables: list[DurableEvent[Any]] = []
        rows: list[dict[str, Any]] = []
        for ordinal, event in enumerate(batch.events):
            seq = high + 1 + ordinal
            rows.append(
                {
                    "id": event.id,
                    "session_id": session_id,
                    "seq": seq,
                    "type": event.type,
                    "version": event.version,
                    "created": epoch_ms(event.created_at),
                    "data": canonical_json(event.data.model_dump(mode="json")),
                    "metadata": canonical_json(event.metadata),
                    "batch_id": batch.id,
                    "batch_ordinal": ordinal,
                    "dedupe_key": batch.dedupe_key,
                }
            )
            durables.append(
                envelope_model[type(event.data)].model_validate({**event.model_dump(mode="json"), "seq": seq})
            )
        try:
            await connection.execute(_INSERT_EVENT, rows)
        except IntegrityError as error:
            raise IdempotencyConflictError(
                "event id or dedupe key already committed",
                details={"session_id": session_id, "batch_id": batch.id},
            ) from error
        for event in durables:
            await self._projector.apply(connection, event)
        return durables

    async def _isolate_locked(self, session_id: str, diagnostic: Diagnostic) -> None:
        """Short independent transaction persisting the diagnostic state (§3.2.1).

        Preserves the last committed read model and its applied_seq when the
        sessions row exists; creates the minimal diagnostic record (applied_seq
        0, empty config/active_run_id) when it is missing. Writes no business
        events and does not advance seq/applied_seq. If the row cannot be
        persisted, the in-memory write gate still holds and the storage error
        is raised (isolation is never claimed as persisted).
        """
        payload = canonical_json(diagnostic.to_json())
        try:
            async with self._engine.connect() as connection:
                updated = (
                    await connection.execute(_UPDATE_SESSION_DIAGNOSTIC, {"sid": session_id, "diag": payload})
                ).rowcount
                if not updated:
                    await connection.execute(_INSERT_SESSION_DIAGNOSTIC, {"sid": session_id, "diag": payload})
                await connection.commit()
        except Exception as error:
            self._isolated.add(session_id)
            raise IsolationPersistFailedError(
                f"could not persist read-only diagnostic for session {session_id}"
            ) from error
        self._isolated.add(session_id)


def _validate_arguments(session_id: str, batch: EventBatch, expected_seq: int) -> None:
    """State-independent caller-input checks (rejected without any DB access)."""
    if not isinstance(session_id, str) or not session_id:
        raise StoreValidationError("session_id must be a non-empty string")
    if isinstance(expected_seq, bool) or not isinstance(expected_seq, int) or expected_seq < 0:
        raise StoreValidationError("expected_seq must be an integer >= 0 (bools are rejected)")
    if not isinstance(batch.id, str) or not batch.id:
        raise StoreValidationError("batch.id must be a non-empty string")
    if not batch.events:
        raise StoreValidationError("batch.events must not be empty")
    seen: set[str] = set()
    for index, event in enumerate(batch.events):
        if not isinstance(event, EventDraft):
            raise StoreValidationError(f"batch.events[{index}] is not an EventDraft")
        if event.id in seen:
            raise StoreValidationError(f"event id {event.id!r} appears more than once in the batch")
        seen.add(event.id)
