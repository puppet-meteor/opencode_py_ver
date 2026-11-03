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

The R1-S8 rebuild (§3.3) also lives here: :meth:`TransactionWriter.rebuild`
replaces one session's read model under the same lock pair in one transaction
(clear -> fail-closed replay through the S4 fold -> re-project -> verify ->
commit; administrative marks carried over verbatim) and shares the §3.2.1
isolation entry with the commit path. The fold itself is imported at call
time from ``opencode.session.state`` -- a module-level import would cycle
(``opencode.session`` imports this package via the projector).
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
#: R1-S8 rebuild replay: the log cannot produce a legal session through the
#: fold (first event not py.session.created, prompt before create, a second
#: create) -- ownership/transition damage over the stored log (§3.2.1 L191).
REASON_REPLAY_ILLEGAL_TRANSITION = "replay_illegal_transition"
#: A message id repeated across replayed events (reachable only through a
#: hand-tampered log; the commit path rejects the second projection inline).
REASON_REBUILD_DUPLICATE_MESSAGE = "rebuild_duplicate_message_id"
#: The rebuild's post-projection invariant: the rebuilt sessions row must land
#: at applied_seq = H, the last replayed event seq (mirrors the commit check).
REASON_REBUILD_INVARIANT = "rebuild_applied_seq_mismatch"
#: The surviving projection is ahead of the log (A > H at discovery, an empty
#: log being H = 0): the log cannot prove the stored applied_seq -- tail
#: events are missing (append-only damage, §3.2 L183), or a row survives an
#: empty log without the §3.2.1 L193 minimal-record signature. Rebuild never
#: silently normalizes A := H; the session is isolated instead (§3.2 L165/
#: §3.2.1 L191, T-C1-012 scheduler ruling). first_bad_seq = H + 1 locates the
#: first event the projection applied that the log no longer has.
REASON_PROJECTION_AHEAD_OF_LOG = "projection_ahead_of_log"
#: Rebuild re-projection of the R2 read models: a tool_executions/runs row hit
#: an identity constraint the fold already forbids (unique(session_id,
#: execution_id)/unique(session_id, step_id, tool_call_id)/run_id PK/
#: unique(session_id, request_id)) -- reachable only through stored rows the
#: fold never produced (hand-tampered data).
REASON_REBUILD_DUPLICATE_TOOL_EXECUTION = "rebuild_duplicate_tool_execution"
REASON_REBUILD_DUPLICATE_RUN = "rebuild_duplicate_run"

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
# -- T-C1-022 (R2-S7 ④-3): the S7 close-timeout management mark ---------------
# Verbatim move of the service-owned UPDATE (session/service.py): scoped to
# still-active rows, no seq/applied_seq advance, no business events.
_UPDATE_SESSION_CLEANUP_FAILED = sa.text(
    "UPDATE sessions SET status = 'cleanup_failed', diagnostic = :diag WHERE session_id = :sid AND status = 'active'"
)
# -- R1-S8 rebuild (§3.3): the read-model replacement runs in one transaction --
_SELECT_SESSION_EVENTS = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata "
    "FROM events WHERE session_id = :sid ORDER BY seq"
)
_SELECT_REBUILD_SESSION = sa.text("SELECT status, applied_seq, diagnostic FROM sessions WHERE session_id = :sid")
_DELETE_SESSION_MESSAGES = sa.text("DELETE FROM messages WHERE session_id = :sid")
_DELETE_SESSION_TOOL_EXECUTIONS = sa.text("DELETE FROM tool_executions WHERE session_id = :sid")
_DELETE_SESSION_RUNS = sa.text("DELETE FROM runs WHERE session_id = :sid")
_DELETE_SESSION_ROW = sa.text("DELETE FROM sessions WHERE session_id = :sid")
_INSERT_REBUILT_SESSION = sa.text(
    "INSERT INTO sessions (session_id, status, config_snapshot, snapshot_hash, "
    "active_run_id, applied_seq, diagnostic) VALUES "
    "(:sid, :status, :config_snapshot, :snapshot_hash, :active_run_id, :applied_seq, :diagnostic)"
)
_INSERT_REBUILT_MESSAGE = sa.text(
    "INSERT INTO messages (session_id, message_id, seq, data) VALUES (:sid, :message_id, :seq, :data)"
)
_INSERT_REBUILT_TOOL_EXECUTION = sa.text(
    "INSERT INTO tool_executions (session_id, execution_id, run_id, step_id, message_id, tool_call_id, "
    "input_hash, state) VALUES (:sid, :execution_id, :run_id, :step_id, :message_id, :tool_call_id, "
    ":input_hash, :state)"
)
_INSERT_REBUILT_RUN = sa.text(
    "INSERT INTO runs (run_id, session_id, request_id, status, error) "
    "VALUES (:run_id, :sid, :request_id, :status, :error)"
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

    async def rebuild(self, session_id: str) -> None:
        """§3.3 rebuild: replay the session's log onto a fresh read model.

        R1-S8. One write transaction under the per-session lock plus the
        process-wide write lock (the commit pattern): clear the session's
        read-model rows, read the full log in seq order, decode + fold every
        event (fail closed on unknown type/version, undecodable JSON, seq
        gaps/duplicates/illegal values, snapshot-hash mismatch, illegal fold
        transitions), re-project the sessions/messages rows from the fold's
        final state, verify applied_seq = H, and only then commit. Any
        failure rolls the whole transaction back -- the clearing included, so
        the original projection survives untouched (§3.3: no half read model
        before a bad point, no bad-point skipping) -- then isolates the
        session (§3.2.1) in an independent short transaction and re-raises
        the StorageCorrupt. Rebuild appends no business events, never
        advances seq, and never notifies (§3.3; structurally guaranteed --
        this module cannot reach a bus). Idempotent: the same log replays to
        the same final state. A projection lag (A<H) is NOT corruption:
        applied_seq := H is exactly the rebuild's job. A surviving sessions
        row's administrative face (status, diagnostic) is carried over
        verbatim -- rebuild restores data, it never clears a management mark
        (§3.2.1 L196), so a read_only_diagnostic session rebuilds to a
        restored but still read-only state.
        """
        if not isinstance(session_id, str) or not session_id:
            raise StoreValidationError("session_id must be a non-empty string")
        async with self._session_lock(session_id):
            async with self._write_lock:
                try:
                    await self._rebuild_transaction(session_id)
                except StorageCorruptError as corruption:
                    if corruption.isolate:
                        # The rebuild transaction already rolled back entirely
                        # (the clearing included); isolation runs in an
                        # independent short transaction (§3.2.1/§3.3).
                        try:
                            await self._isolate_locked(session_id, _diagnostic_from(corruption))
                        except StorageError as persist_error:
                            # Same N-1 contract as the commit path: the
                            # in-memory write gate holds, the original
                            # corruption error stays caller-facing.
                            raise corruption from persist_error
                    raise

    async def cleanup_failed(self, session_id: str, run_id: str, reason: str) -> None:
        """Persist ``sessions.status = cleanup_failed`` (S7 management write).

        T-C1-022 (R2-S7 ④-3): consolidation of the service-owned direct
        UPDATE. Mirrors the S4 isolation-write discipline (§3.2.1): one short
        independent transaction scoped to still-active rows -- no business
        events, no seq or applied_seq advance, no bus notify (structurally:
        this module cannot reach a bus), and deliberately no asyncio locks:
        the pre-consolidation call sites (the close-timeout mark and the
        cancel-cleanup mark) held none, and taking the commit locks here
        would change close-timeout marking under a stuck writer. The
        ``status = 'active'`` guard makes repeats and already-marked rows
        no-ops. Service callers validate upstream, so the arg checks mirror
        :meth:`rebuild`'s defensiveness only.
        """
        if not isinstance(session_id, str) or not session_id:
            raise StoreValidationError("session_id must be a non-empty string")
        if not isinstance(run_id, str) or not run_id:
            raise StoreValidationError("run_id must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise StoreValidationError("reason must be a non-empty string")
        diagnostic = json.dumps({"reason": reason, "session_id": session_id, "run_id": run_id}, sort_keys=True)
        async with self._engine.connect() as connection:
            await connection.execute(_UPDATE_SESSION_CLEANUP_FAILED, {"sid": session_id, "diag": diagnostic})
            await connection.commit()

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
        replay = await self._idempotent_replay(connection, session_id, batch, applied)
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
        self, connection: AsyncConnection, session_id: str, batch: EventBatch, applied: int
    ) -> list[DurableEvent[Any]] | None:
        """Return the original events for an exact whole-batch retry, else None.

        Rejections (§3.2 step 2; never re-projected): a stored id committed in
        another session, mixed committed+new ids, or same-id-different-content
        under this batch id -- all IdempotencyConflict. A stored JSON column
        that no longer decodes is §3.2.1 "invalid JSON" over already-stored
        data (M-1): StorageCorrupt with isolation, the offending row located
        and A at discovery time (``applied``, the watermark the write gate
        read). Each row's JSON columns decode before the content comparison,
        so diverged-but-decodable content still rejects as IdempotencyConflict.
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
            # M-1: the comparison loop reads already-stored rows, so an
            # undecodable JSON column here is stored corruption (§3.2.1), not
            # a content divergence; locate the offending row itself.
            try:
                stored_data = _json_column(row.data)
                stored_metadata = _json_column(row.metadata)
            except ValueError as error:
                raise _corruption(
                    REASON_STORED_DECODE_FAILED,
                    Diagnostic(
                        reason=REASON_STORED_DECODE_FAILED,
                        first_bad_seq=int(row.seq),
                        first_bad_event_id=str(row.id),
                        applied_seq=applied,
                    ),
                ) from error
            if (
                int(row.batch_ordinal) != ordinal
                or row.id != event.id
                or row.type != event.type
                or int(row.version) != event.version
                or int(row.created) != epoch_ms(event.created_at)
                or stored_data != event.data.model_dump(mode="json")
                or stored_metadata != event.metadata
            ):
                raise IdempotencyConflictError(
                    f"batch {batch.id} content diverges from the committed batch at ordinal {ordinal}",
                    details={"session_id": session_id, "batch_id": batch.id, "batch_ordinal": ordinal},
                )
        decoded: list[DurableEvent[Any]] = []
        for row in committed:
            # Nit-2: decode per row so the diagnostic locates the bad row
            # (not the first committed row) and reports A at discovery.
            try:
                decoded.append(self._decode_stored(row))
            except (EventDecodeError, ValueError) as error:
                raise _corruption(
                    REASON_STORED_DECODE_FAILED,
                    Diagnostic(
                        reason=REASON_STORED_DECODE_FAILED,
                        first_bad_seq=int(row.seq),
                        first_bad_event_id=str(row.id),
                        applied_seq=applied,
                    ),
                ) from error
        return decoded

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

    async def _rebuild_transaction(self, session_id: str) -> None:
        """One transaction: clear + replay + re-project; commit only on success."""
        async with self._engine.connect() as connection:
            try:
                await self._rebuild_sequence(connection, session_id)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _rebuild_sequence(self, connection: AsyncConnection, session_id: str) -> None:
        """§3.3 replay: validate the log, clear the read model, fold, re-project.

        The log is read and checked against the surviving sessions row
        BEFORE any clearing, so no successful return ever commits a deleted
        row. An empty log has three branches: a missing row is a defensive
        no-op (the service face already answered NotFound); a surviving
        §3.2.1 L193 minimal diagnostic record (applied_seq == 0 +
        read_only_diagnostic + a persisted diagnostic) is preserved verbatim
        as a true no-op -- it is not a py.session.created and must not become
        an executable session, and rebuild never clears the management mark
        (L196); any other surviving row is A > H = 0 -- the log cannot prove
        the stored applied_seq -- and isolates. A non-empty log with A > H
        (tail events deleted; append-only damage, §3.2 L183) isolates the
        same way: the log cannot prove the stored applied_seq, and rebuild
        must not silently normalize A := H -- the mirror image of the blessed
        A < H repair (§3.2 L165/§3.2.1 L191; scheduler ruling T-C1-012;
        REASON_PROJECTION_AHEAD_OF_LOG, first_bad_seq = H + 1).

        Fail-closed replay validation (§3.2.1 L191 detection scope over the
        rebuild path): unknown type/version, invalid JSON or a snapshot-hash
        mismatch (§6.2 -- the ``DurableEvent[SessionCreatedData]`` envelope
        itself re-verifies the hash, so all three surface at decode), seq not
        starting at 1 and consecutive by 1 (gap/duplicate/illegal value), and
        fold-transition damage (the first event not being
        ``py.session.created`` included -- the fold is the single source of
        transition rules, so that check IS the fold raising) all raise
        StorageCorrupt with isolation; the transaction-local
        post-projection invariant only rolls back. ``state.apply`` is
        imported at call time on purpose: ``opencode.session`` imports this
        package (session.projector -> storage), so a module-level import
        would cycle; every rebuild caller (the session service / composition
        root) already has the session package loaded by then.
        """
        from opencode.session.state import IllegalTransitionError, SessionState, SessionStatus, apply

        # The administrative face of a surviving row is carried over verbatim
        # (§3.2.1 L196); an unknown stored status is stored-projection
        # corruption with the commit/read gates' classification (fail closed).
        management = (await connection.execute(_SELECT_REBUILD_SESSION, {"sid": session_id})).first()
        status = SessionStatus.ACTIVE.value
        diagnostic: str | None = None
        applied_at_discovery = 0
        if management is not None:
            try:
                SessionStatus(str(management.status))
            except ValueError as error:
                raise _corruption(
                    REASON_UNKNOWN_STATUS,
                    Diagnostic(reason=REASON_UNKNOWN_STATUS, applied_seq=int(management.applied_seq)),
                ) from error
            status = str(management.status)
            diagnostic = management.diagnostic
            applied_at_discovery = int(management.applied_seq)

        # The log is read and checked against the surviving row BEFORE any
        # clearing: no successful return may commit a deleted row (§3.3).
        rows = (await connection.execute(_SELECT_SESSION_EVENTS, {"sid": session_id})).fetchall()
        if not rows:
            # Empty log, three fail-closed branches: a missing row is the
            # never-created session the service-level gate already answered
            # (a defensive no-op here); a surviving §3.2.1 L193 minimal
            # diagnostic record (applied_seq == 0 + read_only_diagnostic + a
            # persisted diagnostic) is preserved as a true no-op -- it is not
            # a py.session.created and must not become an executable session,
            # and rebuild never clears the management mark (L196); any other
            # surviving row is A > H = 0 -- the log cannot prove the stored
            # applied_seq, which is stored corruption like every A > H case
            # (or a protocol-impossible row over an empty log), isolated.
            if management is None:
                return
            if applied_at_discovery == 0 and status == SessionStatus.READ_ONLY_DIAGNOSTIC.value and diagnostic:
                return
            raise _corruption(
                REASON_PROJECTION_AHEAD_OF_LOG,
                Diagnostic(
                    reason=REASON_PROJECTION_AHEAD_OF_LOG,
                    first_bad_seq=1,
                    applied_seq=applied_at_discovery,
                ),
            )

        # A > H over a non-empty log: tail events are missing (append-only
        # damage, §3.2 L183), so the log cannot prove the stored applied_seq.
        # Rebuild isolates instead of silently normalizing A := H -- the
        # mirror image of the blessed A < H repair, where the projection is
        # reconstructible from the log; integrity doubt goes to human
        # adjudication (§3.2 L165/§3.2.1 L191; scheduler ruling T-C1-012).
        # first_bad_seq = H + 1 locates the first event the projection
        # applied that the log no longer has.
        log_high = max(int(row.seq) for row in rows)
        if applied_at_discovery > log_high:
            raise _corruption(
                REASON_PROJECTION_AHEAD_OF_LOG,
                Diagnostic(
                    reason=REASON_PROJECTION_AHEAD_OF_LOG,
                    first_bad_seq=log_high + 1,
                    applied_seq=applied_at_discovery,
                ),
            )

        # Clear the read model inside the transaction: any later failure rolls
        # the clearing back with everything else (§3.3 全部回滚).
        await connection.execute(_DELETE_SESSION_MESSAGES, {"sid": session_id})
        await connection.execute(_DELETE_SESSION_TOOL_EXECUTIONS, {"sid": session_id})
        await connection.execute(_DELETE_SESSION_RUNS, {"sid": session_id})
        await connection.execute(_DELETE_SESSION_ROW, {"sid": session_id})

        state = SessionState()
        # Keyed by message id: appended messages keep their first-appearance
        # seq, a terminal replacement (step.finished) only rewrites the data.
        message_rows: dict[str, dict[str, Any]] = {}
        expected_seq = 1
        for row in rows:
            seq = int(row.seq)
            if seq < 1:
                raise _corruption(
                    REASON_ILLEGAL_SEQ,
                    Diagnostic(
                        reason=REASON_ILLEGAL_SEQ,
                        first_bad_seq=seq,
                        first_bad_event_id=str(row.id),
                        applied_seq=applied_at_discovery,
                    ),
                )
            if seq > expected_seq:
                # The first missing seq locates the gap (mirrors the commit
                # path's _SELECT_FIRST_GAP_SEQ semantics).
                raise _corruption(
                    REASON_SEQ_GAP,
                    Diagnostic(reason=REASON_SEQ_GAP, first_bad_seq=expected_seq, applied_seq=applied_at_discovery),
                )
            if seq < expected_seq:
                # Defensive: unique(session_id, seq) makes duplicate seqs
                # unreachable through storage; kept fail closed regardless.
                raise _corruption(
                    REASON_DUPLICATE_SEQ,
                    Diagnostic(
                        reason=REASON_DUPLICATE_SEQ,
                        first_bad_seq=seq,
                        first_bad_event_id=str(row.id),
                        applied_seq=applied_at_discovery,
                    ),
                )
            try:
                event = self._decode_stored(row)
            except (EventDecodeError, ValueError) as error:
                raise _corruption(
                    REASON_STORED_DECODE_FAILED,
                    Diagnostic(
                        reason=REASON_STORED_DECODE_FAILED,
                        first_bad_seq=seq,
                        first_bad_event_id=str(row.id),
                        applied_seq=applied_at_discovery,
                    ),
                ) from error
            try:
                previous_messages = state.messages
                state = apply(state, event)
                # Identity diff over the folded sequence: appends are new ids;
                # step.finished replaces a message by id (a new object at the
                # old position); unchanged messages keep the first-appearance
                # seq and their stored data verbatim.
                for index, message in enumerate(state.messages):
                    if index < len(previous_messages) and message is previous_messages[index]:
                        continue
                    data = canonical_json(message.model_dump(mode="json"))
                    message_row = message_rows.get(message.id)
                    if message_row is None:
                        message_rows[message.id] = {
                            "sid": session_id,
                            "message_id": message.id,
                            "seq": seq,
                            "data": data,
                        }
                    else:
                        message_row["data"] = data
            except IllegalTransitionError as error:
                raise _corruption(
                    REASON_REPLAY_ILLEGAL_TRANSITION,
                    Diagnostic(
                        reason=REASON_REPLAY_ILLEGAL_TRANSITION,
                        first_bad_seq=seq,
                        first_bad_event_id=str(row.id),
                        applied_seq=applied_at_discovery,
                    ),
                ) from error
            expected_seq += 1

        if (
            state.session_id is None
            or state.status is None
            or state.config_snapshot is None
            or state.snapshot_hash is None
        ):
            # Unreachable for a non-empty legal log (the fold created the
            # session above); kept so every failure surfaces as a StoreError.
            raise _corruption(
                REASON_REPLAY_ILLEGAL_TRANSITION,
                Diagnostic(reason=REASON_REPLAY_ILLEGAL_TRANSITION, applied_seq=applied_at_discovery),
            )
        await connection.execute(
            _INSERT_REBUILT_SESSION,
            {
                "sid": session_id,
                "status": status,
                "config_snapshot": canonical_json(state.config_snapshot.model_dump(mode="json")),
                "snapshot_hash": state.snapshot_hash,
                "active_run_id": state.active_run_id,
                "applied_seq": state.applied_seq,
                "diagnostic": diagnostic,
            },
        )
        for message_row in message_rows.values():
            try:
                # One INSERT per row so a violation locates the offending row.
                await connection.execute(_INSERT_REBUILT_MESSAGE, message_row)
            except IntegrityError as error:
                raise _corruption(
                    REASON_REBUILD_DUPLICATE_MESSAGE,
                    Diagnostic(
                        reason=REASON_REBUILD_DUPLICATE_MESSAGE,
                        first_bad_seq=int(message_row["seq"]),
                        applied_seq=applied_at_discovery,
                    ),
                ) from error

        # The R2 read models are re-projected from the final fold state: the
        # runs rows (accepted/running/terminal per the replayed transitions)
        # and the tool_executions rows (ownership + input_hash + the final
        # state) -- events stay the authority (§3.2 L185, §3.3).
        for tool in state.tools.values():
            try:
                await connection.execute(
                    _INSERT_REBUILT_TOOL_EXECUTION,
                    {
                        "sid": session_id,
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
                raise _corruption(
                    REASON_REBUILD_DUPLICATE_TOOL_EXECUTION,
                    Diagnostic(
                        reason=REASON_REBUILD_DUPLICATE_TOOL_EXECUTION,
                        first_bad_seq=state.applied_seq,
                        applied_seq=applied_at_discovery,
                    ),
                ) from error
        for run in state.runs.values():
            try:
                await connection.execute(
                    _INSERT_REBUILT_RUN,
                    {
                        "run_id": run.run_id,
                        "sid": session_id,
                        "request_id": run.request_id,
                        "status": run.status.value,
                        "error": canonical_json(run.error.model_dump(mode="json")) if run.error is not None else None,
                    },
                )
            except IntegrityError as error:
                raise _corruption(
                    REASON_REBUILD_DUPLICATE_RUN,
                    Diagnostic(
                        reason=REASON_REBUILD_DUPLICATE_RUN,
                        first_bad_seq=state.applied_seq,
                        applied_seq=applied_at_discovery,
                    ),
                ) from error

        # Post-projection invariant (mirrors the commit path's A=H+n check):
        # the rebuilt row must land at applied_seq = H, the last replayed seq.
        after = (await connection.execute(_SELECT_SESSION_APPLIED, {"sid": session_id})).first()
        if after is None or int(after.applied_seq) != state.applied_seq:
            raise StorageCorruptError(
                "rebuild did not set sessions.applied_seq to the log watermark",
                isolate=False,
                details={
                    "reason": REASON_REBUILD_INVARIANT,
                    "session_id": session_id,
                    "expected_applied_seq": state.applied_seq,
                },
            )

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
