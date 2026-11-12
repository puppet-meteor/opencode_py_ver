"""Jobs-domain writer + read repository (P3-S1, PHASE3-ORCHESTRATOR §5.4/§6; T-C1-076).

PHASE3 M-7 "two writer domains" ruling: ``py.job.*`` events never pass through
:class:`~opencode.storage.store.SessionStore` -- the session fold
(``opencode.session.state``) is fail-closed on types outside its subset and
that module is frozen. Jobs keep their own writer over the SAME database and
the SAME ``events`` table, on synthesized ``ses_job_{job_id}`` stream keys
(the ``^ses_`` envelope pattern holds with zero contract change).
:meth:`JobsStore.commit` mirrors the minimal CORE-CONTRACTS §3.2 commit
sequence (read-model/A invariants -> idempotent whole-batch replay ->
expected_seq CAS -> ownership/version -> consecutive seq -> insert +
same-transaction projection -> A=H+n verify -> commit); the notification step
stays outside storage -- the service layer notifies the bus after commit,
the SessionStore precedent.

Differences from the session domain, both disclosed in progress ①:

- the ``jobs`` table has NO foreign key to ``sessions`` -- a synthetic
  ``ses_job_*`` stream has no sessions row (0002_runs precedent inverted);
- there is no §3.2.1 isolation face: a jobs row carries no
  ``read_only_diagnostic`` status, so corruption detections raise
  :class:`~opencode.storage.txn.StorageCorruptError` with ``isolate=False``
  and fail closed without marking anything (no sessions row to mark).

The §3.3-style rebuild (:meth:`JobsStore.rebuild`) replays one stream's log
through the injected projector (the fold's single source -- the same
projector the commit path runs), replacing the jobs row only after the whole
log validates and projects. The projector protocol is storage-owned
(:class:`JobsProjector`); the concrete implementation (fold + SQL) lives in
``opencode.orchestrator.jobs`` over this protocol, the
``session/projector.py`` precedent for a domain-owned projection seam.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from opencode.contracts import DurableEvent, EventRegistry, canonical_json, default_event_registry
from opencode.contracts.events import EventDecodeError
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.storage.store import EventBatch
from opencode.storage.txn import (
    ConflictError,
    IdempotencyConflictError,
    StorageCorruptError,
    StoreValidationError,
    _json_column,
    epoch_ms,
    from_epoch_ms,
)

#: Sanitized jobs-domain corruption reasons (no isolation face -- see above).
REASON_JOBS_READ_MODEL_MISSING = "jobs_row_missing_with_log"
REASON_JOBS_APPLIED_SEQ_MISMATCH = "jobs_applied_seq_mismatch"
REASON_JOBS_SEQ_GAP = "jobs_seq_gap"
REASON_JOBS_DUPLICATE_SEQ = "jobs_duplicate_seq"
REASON_JOBS_ILLEGAL_SEQ = "jobs_illegal_seq"
REASON_JOBS_PROJECTION_INVARIANT = "jobs_applied_seq_not_advanced_after_projection"
REASON_JOBS_REPLAY_ILLEGAL_TRANSITION = "jobs_replay_illegal_transition"

_JOB_COLUMNS = (
    "job_id, stream_id, pipeline_id, params, params_hash, session_anchor, status, depth, "
    "budget, checkpoint_ref, retry_count, attempt_id, stalled_reason, lineage, trace_id, "
    "result, error, applied_seq, created_epoch_ms, updated_epoch_ms"
)

_SELECT_LOG_STATS = sa.text(
    "SELECT COUNT(*) AS n, COUNT(DISTINCT seq) AS d, COALESCE(MIN(seq), 0) AS lo, "
    "COALESCE(MAX(seq), 0) AS hi FROM events WHERE session_id = :sid"
)
_SELECT_EVENT_IDS = sa.text("SELECT id, session_id FROM events WHERE id IN :ids").bindparams(
    sa.bindparam("ids", expanding=True)
)
_SELECT_COMMITTED_BATCH = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata, batch_ordinal "
    "FROM events WHERE session_id = :sid AND batch_id = :bid ORDER BY batch_ordinal"
)
_INSERT_EVENT = sa.text(
    "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
    "batch_id, batch_ordinal, dedupe_key) VALUES "
    "(:id, :session_id, :seq, :type, :version, :created, :data, :metadata, "
    ":batch_id, :batch_ordinal, :dedupe_key)"
)
_SELECT_JOBS_ROW_APPLIED = sa.text("SELECT applied_seq FROM jobs WHERE stream_id = :sk")
_SELECT_JOB_BY_ID = sa.text(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id = :jid")
_SELECT_NON_TERMINAL_JOBS = sa.text(
    f"SELECT {_JOB_COLUMNS} FROM jobs WHERE status IN ('created', 'running', 'stalled', 'gated')"
)
_SELECT_LOG_HIGH = sa.text("SELECT COALESCE(MAX(seq), 0) AS h FROM events WHERE session_id = :sid")
_SELECT_STREAM_EVENTS = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata "
    "FROM events WHERE session_id = :sid ORDER BY seq"
)
_DELETE_JOBS_ROW = sa.text("DELETE FROM jobs WHERE stream_id = :sk")


def job_stream_key(job_id: str) -> str:
    """The synthesized events-stream key of one job: ``ses_job_{job_id}``.

    The ``^ses_`` envelope pattern (§3.1) holds with zero contract change:
    ``EventLog.follow`` and the SSE transport accept the key as a session_id
    verbatim; jobs events ride ``metadata={}`` (not run-scoped).
    """
    return f"ses_job_{job_id}"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One ``jobs`` read-model row (the §5.4 job field set + projection watermarks)."""

    job_id: str
    stream_id: str
    pipeline_id: str
    params: JsonObject | None
    params_hash: str | None
    session_anchor: str | None
    status: str
    depth: int
    budget: JsonObject | None
    checkpoint_ref: str | None
    retry_count: int
    attempt_id: str | None
    stalled_reason: str | None
    lineage: JsonObject | None
    trace_id: str | None
    result: JsonValue | None
    error: JsonObject | None
    applied_seq: int
    created_epoch_ms: int
    updated_epoch_ms: int


class JobsProjector(Protocol):
    """Jobs read-model projection seam: called per event inside the write transaction.

    ``apply`` folds one ``py.job.*`` event onto the ``jobs`` row (the fold in
    ``opencode.orchestrator.jobs`` is the single transition source), raises
    :class:`~opencode.storage.txn.StoreValidationError` on an illegal
    transition, and advances ``jobs.applied_seq`` to the event's seq; the
    writer verifies A=H+n before committing and rolls the whole transaction
    back otherwise.
    """

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None: ...


def _record_from_row(row: Any) -> JobRecord:
    """Map one ``jobs`` row (raw ``text()`` columns; JSON columns decode lazily)."""
    return JobRecord(
        job_id=str(row.job_id),
        stream_id=str(row.stream_id),
        pipeline_id=str(row.pipeline_id),
        params=_json_column(row.params) if row.params is not None else None,
        params_hash=None if row.params_hash is None else str(row.params_hash),
        session_anchor=None if row.session_anchor is None else str(row.session_anchor),
        status=str(row.status),
        depth=int(row.depth),
        budget=_json_column(row.budget) if row.budget is not None else None,
        checkpoint_ref=None if row.checkpoint_ref is None else str(row.checkpoint_ref),
        retry_count=int(row.retry_count),
        attempt_id=None if row.attempt_id is None else str(row.attempt_id),
        stalled_reason=None if row.stalled_reason is None else str(row.stalled_reason),
        lineage=_json_column(row.lineage) if row.lineage is not None else None,
        trace_id=None if row.trace_id is None else str(row.trace_id),
        result=_json_column(row.result) if row.result is not None else None,
        error=_json_column(row.error) if row.error is not None else None,
        applied_seq=int(row.applied_seq),
        created_epoch_ms=int(row.created_epoch_ms),
        updated_epoch_ms=int(row.updated_epoch_ms),
    )


class JobsReadRepository:
    """Read-only faces over the ``jobs`` read model + the jobs streams' log high watermark."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def job_by_id(self, job_id: str) -> JobRecord | None:
        """The job's read-model row, or None when no such job exists."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_JOB_BY_ID, {"jid": job_id})).first()
        return None if row is None else _record_from_row(row)

    async def non_terminal_jobs(self) -> list[JobRecord]:
        """Every job in a non-terminal status (the §8 restart-recovery scan set)."""
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_NON_TERMINAL_JOBS)).fetchall()
        return [_record_from_row(row) for row in rows]

    async def log_high(self, stream_key: str) -> int:
        """H = COALESCE(MAX(events.seq), 0) over the job stream's committed rows."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_LOG_HIGH, {"sid": stream_key})).first()
        return int(row.h) if row is not None else 0


@dataclass(frozen=True, slots=True)
class _LogStats:
    """Aggregates of one job stream's event log rows, for invariant classification."""

    count: int
    distinct: int
    low: int
    high: int


class JobsStore:
    """The jobs-domain writer: per-stream locks, one process-wide write lock, §3.2-minimal commits.

    Mirrors :class:`~opencode.storage.txn.TransactionWriter` structure minus the
    sessions write-gate/isolation faces (no sessions row exists for a
    ``ses_job_*`` stream; corruption fails closed without a mark). The OS file
    lock from :mod:`opencode.storage.db` still excludes other writer processes.
    """

    def __init__(self, engine: AsyncEngine, *, projector: JobsProjector, registry: EventRegistry | None = None) -> None:
        self._engine = engine
        self._projector = projector
        self._registry = registry if registry is not None else default_event_registry()
        self._write_lock = asyncio.Lock()
        self._stream_locks: dict[str, asyncio.Lock] = {}

    def _stream_lock(self, stream_key: str) -> asyncio.Lock:
        """Get-or-create the per-stream lock (sync, no await -> no race in asyncio)."""
        lock = self._stream_locks.get(stream_key)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[stream_key] = lock
        return lock

    async def commit(self, stream_key: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]:
        """Execute the minimal §3.2 commit sequence for one job stream.

        ``expected_seq`` is the caller's expected pre-commit H (equality only);
        an exact whole-batch retry returns the original events with no
        re-projection and no watermark advance, even with a stale
        ``expected_seq``. Fold violations raise
        :class:`~opencode.storage.txn.StoreValidationError` and roll the whole
        transaction back; stored-log/row invariant violations raise
        :class:`~opencode.storage.txn.StorageCorruptError` (no isolation face).
        """
        if not isinstance(stream_key, str) or not stream_key:
            raise StoreValidationError("stream_key must be a non-empty string")
        if not isinstance(expected_seq, int) or isinstance(expected_seq, bool) or expected_seq < 0:
            raise StoreValidationError("expected_seq must be a non-negative integer")
        async with self._stream_lock(stream_key):
            async with self._write_lock:
                async with self._engine.connect() as connection:
                    try:
                        events = await self._commit_sequence(connection, stream_key, batch, expected_seq)
                        await connection.commit()
                        return events
                    except BaseException:
                        await connection.rollback()
                        raise

    async def rebuild(self, stream_key: str) -> None:
        """§3.3-style rebuild: replace one job stream's read-model row from its log.

        One write transaction under the same lock pair: read the full log in
        seq order, validate (seq run from 1, every row decodes), clear the
        jobs row, replay every event through the injected projector (the
        fold is the single transition source -- a stored-log transition
        violation is :class:`~opencode.storage.txn.StorageCorruptError` here,
        not the commit path's StoreValidationError), verify A=H, commit. Any
        failure rolls the clearing back; appends no events; idempotent.
        """
        if not isinstance(stream_key, str) or not stream_key:
            raise StoreValidationError("stream_key must be a non-empty string")
        async with self._stream_lock(stream_key):
            async with self._write_lock:
                async with self._engine.connect() as connection:
                    try:
                        await self._rebuild_sequence(connection, stream_key)
                        await connection.commit()
                    except BaseException:
                        await connection.rollback()
                        raise

    async def _commit_sequence(
        self, connection: AsyncConnection, stream_key: str, batch: EventBatch, expected_seq: int
    ) -> list[DurableEvent[Any]]:
        if not batch.events:
            raise StoreValidationError("batch must carry at least one event")
        stats = await self._log_stats(connection, stream_key)
        row = (await connection.execute(_SELECT_JOBS_ROW_APPLIED, {"sk": stream_key})).first()
        applied = 0
        if row is None:
            if stats.high > 0:
                raise StorageCorruptError(
                    f"jobs read model missing for stream {stream_key!r}",
                    isolate=False,
                    details={"reason": REASON_JOBS_READ_MODEL_MISSING, "stream_id": stream_key},
                )
        else:
            applied = int(row.applied_seq)

        # Idempotency BEFORE expected_seq: an exact whole-batch retry returns
        # the original events even with a stale expected_seq (§3.2 step 2).
        replay = await self._idempotent_replay(connection, stream_key, batch)
        if replay is not None:
            return replay

        high = self._healthy_high(stream_key, stats, applied)
        if expected_seq != high:
            raise ConflictError(
                f"expected_seq {expected_seq} does not match the current high watermark {high}",
                details={"stream_id": stream_key, "expected_seq": expected_seq, "high_watermark": high},
            )
        self._validate_ownership_and_version(stream_key, batch)
        durables = await self._insert_and_project(connection, stream_key, batch, high)

        after = (await connection.execute(_SELECT_JOBS_ROW_APPLIED, {"sk": stream_key})).first()
        expected_applied = high + len(batch.events)
        if after is None or int(after.applied_seq) != expected_applied:
            raise StorageCorruptError(
                "projection did not advance jobs.applied_seq to H+n",
                isolate=False,
                details={
                    "reason": REASON_JOBS_PROJECTION_INVARIANT,
                    "stream_id": stream_key,
                    "expected_applied_seq": expected_applied,
                },
            )
        return durables

    async def _log_stats(self, connection: AsyncConnection, stream_key: str) -> _LogStats:
        row = (await connection.execute(_SELECT_LOG_STATS, {"sid": stream_key})).one()
        return _LogStats(count=int(row.n), distinct=int(row.d), low=int(row.lo), high=int(row.hi))

    def _healthy_high(self, stream_key: str, stats: _LogStats, applied: int) -> int:
        """Classify seq gaps/duplicates/illegal seq/A!=H as StorageCorrupt (fail closed)."""
        if stats.count == 0:
            return 0
        if stats.low < 1:
            raise StorageCorruptError(
                f"storage corruption detected: {REASON_JOBS_ILLEGAL_SEQ}",
                isolate=False,
                details={"reason": REASON_JOBS_ILLEGAL_SEQ, "stream_id": stream_key, "first_bad_seq": stats.low},
            )
        if stats.distinct != stats.count:
            raise StorageCorruptError(
                f"storage corruption detected: {REASON_JOBS_DUPLICATE_SEQ}",
                isolate=False,
                details={"reason": REASON_JOBS_DUPLICATE_SEQ, "stream_id": stream_key},
            )
        if stats.count != stats.high or stats.low != 1:
            raise StorageCorruptError(
                f"storage corruption detected: {REASON_JOBS_SEQ_GAP}",
                isolate=False,
                details={"reason": REASON_JOBS_SEQ_GAP, "stream_id": stream_key},
            )
        if applied != stats.high:
            raise StorageCorruptError(
                f"storage corruption detected: {REASON_JOBS_APPLIED_SEQ_MISMATCH}",
                isolate=False,
                details={"reason": REASON_JOBS_APPLIED_SEQ_MISMATCH, "stream_id": stream_key},
            )
        return stats.high

    def _validate_ownership_and_version(self, stream_key: str, batch: EventBatch) -> None:
        """Ownership (all events belong to this stream) and version registry checks."""
        for event in batch.events:
            if event.session_id != stream_key:
                raise StoreValidationError(
                    f"event {event.id} belongs to stream {event.session_id!r}, not {stream_key!r}",
                    details={"stream_id": stream_key, "event_id": event.id},
                )
            try:
                self._registry.resolve_durable(event.type, event.version)
            except EventDecodeError as error:
                raise StoreValidationError(
                    f"event {event.id} has no durable definition for {event.type}.{event.version}",
                    details={"stream_id": stream_key, "event_id": event.id},
                ) from error

    async def _idempotent_replay(
        self, connection: AsyncConnection, stream_key: str, batch: EventBatch
    ) -> list[DurableEvent[Any]] | None:
        """Return the original events for an exact whole-batch retry, else None.

        Rejections (§3.2 step 2; never re-projected): a stored id committed in
        another stream, mixed committed+new ids, or same-id-different-content
        under this batch id -- all IdempotencyConflict.
        """
        existing = (
            await connection.execute(_SELECT_EVENT_IDS, {"ids": [event.id for event in batch.events]})
        ).fetchall()
        for row in existing:
            if row.session_id != stream_key:
                raise IdempotencyConflictError(
                    f"event id {row.id} is already committed in another stream",
                    details={"stream_id": stream_key, "event_id": str(row.id)},
                )
        committed = (await connection.execute(_SELECT_COMMITTED_BATCH, {"sid": stream_key, "bid": batch.id})).fetchall()
        if not committed and not existing:
            return None  # all-new batch
        if len(committed) != len(batch.events):
            raise IdempotencyConflictError(
                "batch event ids do not match the committed batch under this batch id",
                details={"stream_id": stream_key, "batch_id": batch.id},
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
                    details={"stream_id": stream_key, "batch_id": batch.id, "batch_ordinal": ordinal},
                )
        decoded: list[DurableEvent[Any]] = []
        for row in committed:
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
            decoded.append(self._registry.decode_durable(envelope))
        return decoded

    async def _insert_and_project(
        self, connection: AsyncConnection, stream_key: str, batch: EventBatch, high: int
    ) -> list[DurableEvent[Any]]:
        """Allocate consecutive seq H+1..H+n, insert rows, run the projector in-tx."""
        durables: list[DurableEvent[Any]] = []
        rows: list[dict[str, Any]] = []
        for ordinal, event in enumerate(batch.events):
            seq = high + 1 + ordinal
            rows.append(
                {
                    "id": event.id,
                    "session_id": stream_key,
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
            envelope: dict[str, Any] = {**event.model_dump(mode="json"), "seq": seq}
            durables.append(self._registry.decode_durable(envelope))
        try:
            await connection.execute(_INSERT_EVENT, rows)
        except IntegrityError as error:
            raise IdempotencyConflictError(
                "event id or dedupe key already committed",
                details={"stream_id": stream_key, "batch_id": batch.id},
            ) from error
        for event in durables:
            await self._projector.apply(connection, event)
        return durables

    async def _rebuild_sequence(self, connection: AsyncConnection, stream_key: str) -> None:
        """One transaction: validate the log, clear the row, replay through the projector."""
        rows = (await connection.execute(_SELECT_STREAM_EVENTS, {"sid": stream_key})).fetchall()
        if not rows:
            await connection.execute(_DELETE_JOBS_ROW, {"sk": stream_key})
            return
        expected_seq = 1
        for row in rows:
            seq = int(row.seq)
            if seq != expected_seq:
                raise StorageCorruptError(
                    "storage corruption detected: jobs log seq run violation",
                    isolate=False,
                    details={
                        "reason": REASON_JOBS_SEQ_GAP if seq > expected_seq else REASON_JOBS_DUPLICATE_SEQ,
                        "stream_id": stream_key,
                        "first_bad_seq": min(seq, expected_seq),
                    },
                )
            expected_seq += 1
        await connection.execute(_DELETE_JOBS_ROW, {"sk": stream_key})
        for row in rows:
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
            try:
                event = self._registry.decode_durable(envelope)
            except Exception as error:
                raise StorageCorruptError(
                    "storage corruption detected: jobs log row failed to decode",
                    isolate=False,
                    details={
                        "reason": REASON_JOBS_REPLAY_ILLEGAL_TRANSITION,
                        "stream_id": stream_key,
                        "first_bad_seq": int(row.seq),
                        "first_bad_event_id": str(row.id),
                    },
                ) from error
            try:
                await self._projector.apply(connection, event)
            except StoreValidationError as error:
                raise StorageCorruptError(
                    f"storage corruption detected: {REASON_JOBS_REPLAY_ILLEGAL_TRANSITION}",
                    isolate=False,
                    details={
                        "reason": REASON_JOBS_REPLAY_ILLEGAL_TRANSITION,
                        "stream_id": stream_key,
                        "first_bad_seq": int(row.seq),
                        "first_bad_event_id": str(row.id),
                    },
                ) from error
        after = (await connection.execute(_SELECT_JOBS_ROW_APPLIED, {"sk": stream_key})).first()
        if after is None or int(after.applied_seq) != expected_seq - 1:
            raise StorageCorruptError(
                f"storage corruption detected: {REASON_JOBS_PROJECTION_INVARIANT}",
                isolate=False,
                details={"reason": REASON_JOBS_PROJECTION_INVARIANT, "stream_id": stream_key},
            )
