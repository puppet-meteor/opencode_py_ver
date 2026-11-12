"""The jobs domain core (P3-S1, PHASE3-ORCHESTRATOR §5.4/§6/§7 A fork-3/§8; T-C1-076).

Five faces over the jobs-domain writer
(:class:`~opencode.storage.jobs_store.JobsStore`):

* the **pure fold** (:func:`fold_job_event`) -- the single source of the
  ``py.job.*`` transition rules, run both by the commit-path projector (a
  violation on a NEW event is ``StoreValidationError`` -> rollback) and by
  the rebuild (a violation over STORED rows is ``StorageCorruptError``);
* the **projector** (:class:`JobsProjector`) -- the jobs read-model seam the
  writer injects; folds each event onto the ``jobs`` row inside the write
  transaction (the ``session/projector.py`` precedent: a domain-owned
  projection over a storage-owned Protocol);
* the **service** (:class:`JobService`) -- submit (A fork-3 order:
  ``py.job.created`` + ``py.job.spec_bound`` committed atomically before any
  execution), run/cancel, and the §8 restart-recovery scan;
* the **executor seam** (:class:`JobExecutor`) -- S1 ships the
  :class:`FakeJobExecutor` only (started -> finished(done)); real workers
  land in S3/S4;
* the **recovery** (:meth:`JobService.recover`) -- the §8 five-branch
  matrix over the non-terminal scan set, with the checkpoint reader as an
  injected protocol (App default None).

Transition table (v1; the fold's verdict, pinned by tests):

=====================  =====================  =========================================
event                  legal from             notes
=====================  =====================  =========================================
py.job.created         (no row)               identity first; second create rejected
py.job.spec_bound      created                once; params/params_hash/anchor bind
py.job.started         created                attempt identity; -> running
py.job.stalled         running                watchdog reason + attempt
py.job.redispatched    stalled                exactly once (event retry_count == 1 and
                                              state retry_count == 0); -> running
py.job.gated           running                gate id + expiry (m-3: 300 s default)
py.job.finished done   running                only from running
py.job.finished        any non-terminal        failed/cancelled may land from
failed/cancelled                              created/running/stalled/gated
(any py.job.*)         done/failed/cancelled  terminal absorbing: rejected
(non py.job.* type)    --                     not part of the fold: rejected
=====================  =====================  =========================================

``gated -> running`` has NO producer event in v1 -- the gate reply chain
(approve/reject/revise) lands in S5a; until then a gated job resolves only
through terminal transitions (disclosed, progress ①). The S1 transport's
gate-reply route is the 409 shell of that later chain.

The four trace fields ride every data model as the nested ``trace`` object
(PHASE3 §4 M-6: ``trace_id`` correlates the job stream with the session
stream; ``span_id``/``parent_id``/``depth`` are the span tree); the §5.4 job
field ``depth`` (workflow nesting, hard cap 3 in gateway policy) is a
separate ``py.job.created`` field -- the name collision is why the trace set
is nested, not flat.
"""

from __future__ import annotations

import asyncio
import enum
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.bus.bus import VolatileBus
from opencode.contracts import DurableEvent, EventDraft, canonical_json
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    PY_JOB_CREATED,
    PY_JOB_FINISHED,
    PY_JOB_GATED,
    PY_JOB_REDISPATCHED,
    PY_JOB_SPEC_BOUND,
    PY_JOB_STALLED,
    PY_JOB_STARTED,
    JobBudget,
    JobCreatedData,
    JobFinishedData,
    JobGatedData,
    JobLineage,
    JobRedispatchedData,
    JobSpecBoundData,
    JobStalledData,
    JobStartedData,
    JobTrace,
)
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.session.service import AscendingIds, Clock, IdFactory
from opencode.storage.jobs_store import JobRecord, JobsReadRepository, JobsStore, job_stream_key
from opencode.storage.repos import NotFoundError
from opencode.storage.store import EventBatch
from opencode.storage.txn import StoreValidationError, epoch_ms, from_epoch_ms

#: The §8 restart-recovery scan set (non-terminal statuses).
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"created", "running", "stalled", "gated"})

#: The terminal, absorbing statuses.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})


def compute_params_hash(params: JsonObject) -> str:
    """sha256 of the canonical params JSON (§6.2 pipeline; the S1 spec hash, see progress ①)."""
    return sha256(canonical_json(params).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(enum.Enum):
    """The §5.4 status closed set (the cards' ``status`` domain)."""

    CREATED = "created"
    RUNNING = "running"
    STALLED = "stalled"
    GATED = "gated"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


_OUTCOME_TO_STATUS: dict[str, JobStatus] = {
    "done": JobStatus.DONE,
    "failed": JobStatus.FAILED,
    "cancelled": JobStatus.CANCELLED,
}


class JobTransitionError(ValueError):
    """The event is illegal over the job's current folded state (the fold's verdict)."""

    def __init__(self, message: str, *, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.details = details if details is not None else {}


@dataclass(frozen=True, slots=True)
class JobState:
    """The pure fold state: the §5.4 job field set plus the projection watermark.

    Row-isomorphic with the ``jobs`` table (the projector maps both ways);
    JSON-typed fields carry the wire models' ``model_dump(mode="json")``.
    """

    job_id: str
    status: JobStatus
    pipeline_id: str = ""
    depth: int = 0
    lineage: JsonObject | None = None
    budget: JsonObject | None = None
    trace_id: str | None = None
    params: JsonObject | None = None
    params_hash: str | None = None
    session_anchor: str | None = None
    retry_count: int = 0
    attempt_id: str | None = None
    stalled_reason: str | None = None
    result: JsonValue | None = None
    error: JsonObject | None = None
    applied_seq: int = 0
    checkpoint_ref: str | None = None


def fold_job_event(state: JobState | None, event: DurableEvent[Any]) -> JobState:
    """Fold one ``py.job.*`` event onto the job state; illegal transitions raise.

    The single source of the transition table above: the commit-path
    projector translates a raise into ``StoreValidationError`` (reject +
    rollback), the rebuild into ``StorageCorruptError`` (stored-log damage).
    """
    data = event.data
    if isinstance(data, JobCreatedData):
        if state is not None:
            raise JobTransitionError(
                f"py.job.created over an existing job (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        return JobState(
            job_id=data.job_id,
            status=JobStatus.CREATED,
            pipeline_id=data.pipeline_id,
            depth=data.depth,
            lineage=data.lineage.model_dump(mode="json"),
            budget=data.budget.model_dump(mode="json"),
            trace_id=data.trace.trace_id,
            applied_seq=event.seq,
        )
    if state is None:
        raise JobTransitionError(
            f"{event.type} cannot be the first event of a job stream (py.job.created missing)",
            details={"event_type": event.type},
        )
    if isinstance(data, JobSpecBoundData):
        if state.status is not JobStatus.CREATED:
            raise JobTransitionError(
                f"py.job.spec_bound is legal only from created (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        if state.params is not None:
            raise JobTransitionError(
                "py.job.spec_bound after the spec is already bound",
                details={"job_id": data.job_id},
            )
        return replace(
            state,
            params=data.params,
            params_hash=data.params_hash,
            session_anchor=data.session_anchor,
            applied_seq=event.seq,
        )
    if isinstance(data, JobStartedData):
        if state.status is not JobStatus.CREATED:
            raise JobTransitionError(
                f"py.job.started is legal only from created (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        return replace(state, status=JobStatus.RUNNING, attempt_id=data.attempt_id, applied_seq=event.seq)
    if isinstance(data, JobStalledData):
        if state.status is not JobStatus.RUNNING:
            raise JobTransitionError(
                f"py.job.stalled is legal only from running (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        return replace(
            state,
            status=JobStatus.STALLED,
            stalled_reason=data.stalled_reason,
            attempt_id=data.attempt_id,
            applied_seq=event.seq,
        )
    if isinstance(data, JobRedispatchedData):
        if state.status is not JobStatus.STALLED:
            raise JobTransitionError(
                f"py.job.redispatched is legal only from stalled (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        if data.retry_count != 1 or state.retry_count != 0:
            raise JobTransitionError(
                "py.job.redispatched is legal exactly once (event retry_count must be 1 on a state retry_count of 0)",
                details={"job_id": data.job_id, "retry_count": data.retry_count},
            )
        return replace(
            state,
            status=JobStatus.RUNNING,
            attempt_id=data.attempt_id,
            retry_count=1,
            stalled_reason=None,
            applied_seq=event.seq,
        )
    if isinstance(data, JobGatedData):
        if state.status is not JobStatus.RUNNING:
            raise JobTransitionError(
                f"py.job.gated is legal only from running (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        return replace(state, status=JobStatus.GATED, applied_seq=event.seq)
    if isinstance(data, JobFinishedData):
        if state.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            raise JobTransitionError(
                f"py.job.finished over a terminal job (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        if data.outcome == "done" and state.status is not JobStatus.RUNNING:
            raise JobTransitionError(
                f"py.job.finished outcome=done is legal only from running (status {state.status.value})",
                details={"job_id": data.job_id, "status": state.status.value},
            )
        return replace(
            state,
            status=_OUTCOME_TO_STATUS[data.outcome],
            result=data.result,
            error=data.error.model_dump(mode="json") if data.error is not None else None,
            applied_seq=event.seq,
        )
    raise JobTransitionError(
        f"event type {event.type!r} is not part of the py.job.* fold",
        details={"event_type": event.type},
    )


# ---------------------------------------------------------------------------
# The jobs read-model projector (storage-injected seam; the fold's SQL face)
# ---------------------------------------------------------------------------

_SELECT_JOBS_STATE = sa.text(
    "SELECT job_id, stream_id, pipeline_id, params, params_hash, session_anchor, status, depth, "
    "budget, checkpoint_ref, retry_count, attempt_id, stalled_reason, lineage, trace_id, "
    "result, error, applied_seq, created_epoch_ms, updated_epoch_ms FROM jobs WHERE stream_id = :sk"
)
_INSERT_JOB_ROW = sa.text(
    "INSERT INTO jobs (job_id, stream_id, pipeline_id, params, params_hash, session_anchor, status, "
    "depth, budget, checkpoint_ref, retry_count, attempt_id, stalled_reason, lineage, trace_id, "
    "result, error, applied_seq, created_epoch_ms, updated_epoch_ms) VALUES "
    "(:job_id, :stream_id, :pipeline_id, :params, :params_hash, :session_anchor, :status, :depth, "
    ":budget, :checkpoint_ref, :retry_count, :attempt_id, :stalled_reason, :lineage, :trace_id, "
    ":result, :error, :applied_seq, :created_epoch_ms, :updated_epoch_ms)"
)
_UPDATE_JOB_ROW = sa.text(
    "UPDATE jobs SET pipeline_id = :pipeline_id, params = :params, params_hash = :params_hash, "
    "session_anchor = :session_anchor, status = :status, depth = :depth, budget = :budget, "
    "checkpoint_ref = :checkpoint_ref, retry_count = :retry_count, attempt_id = :attempt_id, "
    "stalled_reason = :stalled_reason, lineage = :lineage, trace_id = :trace_id, result = :result, "
    "error = :error, applied_seq = :applied_seq, updated_epoch_ms = :updated_epoch_ms "
    "WHERE stream_id = :stream_id"
)


def _json_text(value: JsonObject | None) -> str | None:
    return None if value is None else canonical_json(value)


def _result_text(value: JsonValue | None) -> str | None:
    return None if value is None else canonical_json(value)


def _loads_or_none(raw: Any) -> JsonObject | None:
    """Stored JSON column -> parsed object (text() rows return raw driver strings)."""
    if isinstance(raw, str | bytes | bytearray):
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    return raw if isinstance(raw, dict) else None


def _loads_value(raw: Any) -> JsonValue | None:
    """Stored JSON column -> any parsed JSON value (the result column's loader)."""
    if isinstance(raw, str | bytes | bytearray):
        return json.loads(raw)
    return raw


class JobsProjector:
    """The concrete ``JobsProjector``: fold + row write, per event, in-transaction.

    Reads the stream's current row (None = new job), folds the event (the
    single transition source), and writes the whole row back -- INSERT for
    the first event, UPDATE afterwards -- advancing ``applied_seq`` to the
    event's seq. A fold violation raises ``StoreValidationError`` (the
    commit path rolls back; the rebuild translates it into
    ``StorageCorruptError``). ``created_epoch_ms`` is set by the first write
    and preserved on updates (``updated_epoch_ms`` tracks the last).
    """

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        row = (await connection.execute(_SELECT_JOBS_STATE, {"sk": event.session_id})).first()
        state: JobState | None = None
        created_epoch_ms = 0
        if row is not None:
            created_epoch_ms = int(row.created_epoch_ms)
            error_json = row.error if row.error is None else _loads_or_none(row.error)
            state = JobState(
                job_id=str(row.job_id),
                status=JobStatus(str(row.status)),
                pipeline_id=str(row.pipeline_id),
                depth=int(row.depth),
                lineage=None if row.lineage is None else _loads_or_none(row.lineage),
                budget=None if row.budget is None else _loads_or_none(row.budget),
                trace_id=None if row.trace_id is None else str(row.trace_id),
                params=None if row.params is None else _loads_or_none(row.params),
                params_hash=None if row.params_hash is None else str(row.params_hash),
                session_anchor=None if row.session_anchor is None else str(row.session_anchor),
                retry_count=int(row.retry_count),
                attempt_id=None if row.attempt_id is None else str(row.attempt_id),
                stalled_reason=None if row.stalled_reason is None else str(row.stalled_reason),
                result=None if row.result is None else _loads_value(row.result),
                error=error_json,
                applied_seq=int(row.applied_seq),
                checkpoint_ref=None if row.checkpoint_ref is None else str(row.checkpoint_ref),
            )
        try:
            new_state = fold_job_event(state, event)
        except JobTransitionError as error:
            raise StoreValidationError(
                f"illegal job transition: {error}",
                details={
                    "reason": "job_illegal_transition",
                    "stream_id": event.session_id,
                    "event_id": event.id,
                    **error.details,
                },
            ) from error
        updated_epoch_ms = epoch_ms(event.created_at)
        if row is None:
            created_epoch_ms = updated_epoch_ms
        values: dict[str, Any] = {
            "job_id": new_state.job_id,
            "stream_id": event.session_id,
            "pipeline_id": new_state.pipeline_id,
            "params": _json_text(new_state.params),
            "params_hash": new_state.params_hash,
            "session_anchor": new_state.session_anchor,
            "status": new_state.status.value,
            "depth": new_state.depth,
            "budget": _json_text(new_state.budget),
            "checkpoint_ref": new_state.checkpoint_ref,
            "retry_count": new_state.retry_count,
            "attempt_id": new_state.attempt_id,
            "stalled_reason": new_state.stalled_reason,
            "lineage": _json_text(new_state.lineage),
            "trace_id": new_state.trace_id,
            "result": _result_text(new_state.result),
            "error": _json_text(new_state.error),
            "applied_seq": new_state.applied_seq,
            "created_epoch_ms": created_epoch_ms,
            "updated_epoch_ms": updated_epoch_ms,
        }
        if row is None:
            await connection.execute(_INSERT_JOB_ROW, values)
        else:
            await connection.execute(_UPDATE_JOB_ROW, values)


# ---------------------------------------------------------------------------
# The executor seam + the fake execution body (§7 A fork-3, S1)
# ---------------------------------------------------------------------------


class JobHandle:
    """The executor's face onto one submitted job: identity + the emit calls.

    The executor drives the lifecycle through ``started``/``finished``; the
    service folds each emit through the read model's current status first
    (``JobTransitionError`` on an illegal transition -- e.g. a cancel race
    already finished the job) and the store re-validates atomically inside
    the commit transaction.
    """

    def __init__(self, service: JobService, job_id: str) -> None:
        self.service = service
        self.job_id = job_id

    @property
    def stream_key(self) -> str:
        """The job's events stream key (``ses_job_{job_id}``)."""
        return job_stream_key(self.job_id)

    async def started(self, attempt_id: str | None = None) -> None:
        """Emit ``py.job.started`` (created -> running)."""
        await self.service.emit_started(self.job_id, attempt_id=attempt_id)

    async def finished(
        self,
        outcome: Literal["done", "failed", "cancelled"],
        *,
        result: JsonValue | None = None,
        error: CoreError | None = None,
    ) -> None:
        """Emit ``py.job.finished`` (terminal; done only from running)."""
        await self.service.emit_finished(self.job_id, outcome, result=result, error=error)


class JobExecutor(Protocol):
    """The execution seam (§7 A fork-3): drive one job to its terminal state.

    S1 ships :class:`FakeJobExecutor` only. ``resume=True`` is the §8
    recovery-resume shape: the job is already past its start event, so the
    executor emits only the terminal event.
    """

    async def drive(self, handle: JobHandle, *, resume: bool = False) -> None: ...


class FakeJobExecutor:
    """The S1 fake execution body: ``started -> finished(done)`` (no budget face).

    A ``JobTransitionError`` from any emit ends the drive silently -- the
    job is already terminal (a cancel race won); the fold is the authority,
    never the executor's local view.
    """

    async def drive(self, handle: JobHandle, *, resume: bool = False) -> None:
        if not resume:
            try:
                await handle.started()
            except JobTransitionError:
                return
        try:
            await handle.finished("done", result={"executor": "fake"})
        except JobTransitionError:
            return


# ---------------------------------------------------------------------------
# The §8 restart-recovery faces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobCheckpoint:
    """One job's executor checkpoint (§8): the restart-resume/write-back envelope.

    ``spec_hash`` is ``params_hash`` in S1 (the true pipeline spec hash lands
    with spec loading, S2; progress ①). A terminal ``outcome`` plus
    ``batch_id``/``event_id``/``created_at_ms`` is the branch-⑤ write-back
    envelope: the service re-commits the exact original ``py.job.finished``
    draft under the checkpoint's stable batch id, so a repeated recovery is
    the writer's idempotent whole-batch replay.
    """

    job_id: str
    attempt_id: str | None = None
    spec_hash: str | None = None
    batch_id: str | None = None
    outcome: Literal["done", "failed", "cancelled"] | None = None
    result: JsonValue | None = None
    error: JsonObject | None = None
    event_id: str | None = None
    created_at_ms: int | None = None


class JobCheckpointReader(Protocol):
    """The injectable checkpoint read face (§8): None in the App default (S1)."""

    async def read(self, job_id: str) -> JobCheckpoint | None: ...


@dataclass(frozen=True, slots=True)
class JobRecoveryAction:
    """One job's §8 recovery verdict."""

    job_id: str
    action: Literal["redispatched", "resumed", "written_back", "failed_unknown"]
    detail: JsonObject


# ---------------------------------------------------------------------------
# The service (§7 A fork-3 submit order + §8 recovery scan)
# ---------------------------------------------------------------------------


class JobService:
    """The jobs service: submit, run, cancel, status, and the §8 recovery scan.

    Commits run per job under a per-job asyncio lock: read H -> commit the
    batch with expected_seq=H (the writer's CAS) -> notify the bus after the
    commit. IDs are minted through the shared composition ``IdFactory``
    (event/batch ids share the process-wide ascending counter, so
    ``evt_``/``bat_`` ids never collide with the session domain); the
    ``job_``/``att_``/``trc_``/``spn_`` conventions are built on the same
    counter's bare hex (OP-3: new internal prefixes carry no §3.1 pattern).
    """

    def __init__(
        self,
        *,
        store: JobsStore,
        repos: JobsReadRepository,
        executor: JobExecutor,
        bus: VolatileBus | None = None,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._repos = repos
        self._executor = executor
        self._bus = bus
        self._ids = ids if ids is not None else AscendingIds(clock)
        self._clock = clock if clock is not None else _utc_now
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -- identity -----------------------------------------------------------

    def _lock(self, job_id: str) -> asyncio.Lock:
        """Get-or-create the per-job commit lock (sync, no await -> no race)."""
        lock = self._locks.get(job_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[job_id] = lock
        return lock

    def _mint(self, prefix: str) -> str:
        """One id under a jobs convention, built on the shared counter's hex."""
        return f"{prefix}_{self._ids.event_id()[4:]}"

    # -- the commit spine ----------------------------------------------------

    async def _commit(self, job_id: str, drafts: list[EventDraft[Any]]) -> list[DurableEvent[Any]]:
        """Commit one batch under the per-job lock, then notify the bus (post-commit)."""
        stream_key = job_stream_key(job_id)
        async with self._lock(job_id):
            high = await self._repos.log_high(stream_key)
            batch = EventBatch(id=self._ids.batch_id(), events=tuple(drafts))
            events = await self._store.commit(stream_key, batch, high)
        if self._bus is not None:
            self._bus.notify(stream_key, events)
        return events

    def _trace(self, record: JobRecord, *, parent_id: str | None = None) -> JobTrace:
        """The next span of the job's trace (trace_id persists; span_id is new)."""
        return JobTrace(
            trace_id=record.trace_id if record.trace_id is not None else self._mint("trc"),
            span_id=self._mint("spn"),
            parent_id=parent_id,
            depth=0,
        )

    def _draft(self, stream_key: str, event_type: str, data: Any, created_at: datetime) -> EventDraft[Any]:
        return EventDraft(
            id=self._ids.event_id(),
            type=event_type,
            version=1,
            session_id=stream_key,
            created_at=created_at,
            data=data,
            metadata={},
        )

    # -- §7 A fork-3: submit --------------------------------------------------

    async def submit(
        self,
        pipeline_id: str,
        params: JsonObject,
        session_anchor: str = "api",
        *,
        depth: int = 0,
        budget: JobBudget | None = None,
    ) -> JobHandle:
        """Submit one job: ``created`` + ``spec_bound`` atomically, before any execution.

        Structural validation only in S1 (non-empty pipeline_id, JSON-object
        params, anchor ``"api"`` or ``^ses_``); validating params against the
        pipeline's JSON Schema needs spec loading and lands in S2 (progress ①).
        """
        if not isinstance(pipeline_id, str) or not pipeline_id:
            raise StoreValidationError("pipeline_id must be a non-empty string")
        if not isinstance(params, dict):
            raise StoreValidationError("params must be a JSON object")
        if not isinstance(session_anchor, str) or (session_anchor != "api" and not session_anchor.startswith("ses_")):
            raise StoreValidationError("session_anchor must be 'api' or a ses_-prefixed session id")
        job_id = self._mint("job")
        stream_key = job_stream_key(job_id)
        trace_id = self._mint("trc")
        now = self._clock()
        origin: Literal["api", "session"] = "api" if session_anchor == "api" else "session"
        created = self._draft(
            stream_key,
            PY_JOB_CREATED,
            JobCreatedData(
                job_id=job_id,
                pipeline_id=pipeline_id,
                depth=depth,
                lineage=JobLineage(
                    parent_job_id=None,
                    session_id=None if session_anchor == "api" else session_anchor,
                    origin=origin,
                ),
                budget=budget if budget is not None else JobBudget(),
                trace=JobTrace(trace_id=trace_id, span_id=self._mint("spn"), parent_id=None, depth=0),
            ),
            now,
        )
        spec_bound = self._draft(
            stream_key,
            PY_JOB_SPEC_BOUND,
            JobSpecBoundData(
                job_id=job_id,
                params=params,
                params_hash=compute_params_hash(params),
                session_anchor=session_anchor,
                trace=JobTrace(trace_id=trace_id, span_id=self._mint("spn"), parent_id=None, depth=0),
            ),
            now,
        )
        await self._commit(job_id, [created, spec_bound])
        return JobHandle(service=self, job_id=job_id)

    # -- reads ----------------------------------------------------------------

    async def get_job(self, job_id: str) -> JobRecord:
        """The job's read-model row; ``NotFoundError`` (a StoreError) when absent."""
        record = await self._repos.job_by_id(job_id)
        if record is None:
            raise NotFoundError(f"job {job_id} does not exist", details={"job_id": job_id})
        return record

    # -- the lifecycle emits (the executor's commit faces) ---------------------

    async def emit_started(self, job_id: str, *, attempt_id: str | None = None) -> DurableEvent[Any]:
        """Emit ``py.job.started`` (created -> running)."""
        record = await self.get_job(job_id)
        if record.status != JobStatus.CREATED.value:
            raise JobTransitionError(
                f"py.job.started is legal only from created (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        stream_key = job_stream_key(job_id)
        draft = self._draft(
            stream_key,
            PY_JOB_STARTED,
            JobStartedData(
                job_id=job_id,
                attempt_id=attempt_id if attempt_id is not None else self._mint("att"),
                trace=self._trace(record),
            ),
            self._clock(),
        )
        events = await self._commit(job_id, [draft])
        return events[0]

    async def emit_stalled(
        self, job_id: str, stalled_reason: str, *, attempt_id: str | None = None
    ) -> DurableEvent[Any]:
        """Emit ``py.job.stalled`` (running -> stalled; the watchdog hook)."""
        record = await self.get_job(job_id)
        if record.status != JobStatus.RUNNING.value:
            raise JobTransitionError(
                f"py.job.stalled is legal only from running (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        stream_key = job_stream_key(job_id)
        draft = self._draft(
            stream_key,
            PY_JOB_STALLED,
            JobStalledData(
                job_id=job_id,
                stalled_reason=stalled_reason,
                attempt_id=attempt_id if attempt_id is not None else self._mint("att"),
                trace=self._trace(record),
            ),
            self._clock(),
        )
        events = await self._commit(job_id, [draft])
        return events[0]

    async def emit_redispatched(self, job_id: str, *, attempt_id: str | None = None) -> DurableEvent[Any]:
        """Emit ``py.job.redispatched`` (stalled -> running, exactly once)."""
        record = await self.get_job(job_id)
        if record.status != JobStatus.STALLED.value:
            raise JobTransitionError(
                f"py.job.redispatched is legal only from stalled (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        if record.retry_count != 0:
            raise JobTransitionError(
                "py.job.redispatched is legal exactly once",
                details={"job_id": job_id, "retry_count": record.retry_count},
            )
        stream_key = job_stream_key(job_id)
        draft = self._draft(
            stream_key,
            PY_JOB_REDISPATCHED,
            JobRedispatchedData(
                job_id=job_id,
                attempt_id=attempt_id if attempt_id is not None else self._mint("att"),
                retry_count=1,
                trace=self._trace(record),
            ),
            self._clock(),
        )
        events = await self._commit(job_id, [draft])
        return events[0]

    async def emit_gated(self, job_id: str, gate_id: str, expires_at: datetime) -> DurableEvent[Any]:
        """Emit ``py.job.gated`` (running -> gated; the human-gate interrupt)."""
        record = await self.get_job(job_id)
        if record.status != JobStatus.RUNNING.value:
            raise JobTransitionError(
                f"py.job.gated is legal only from running (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        stream_key = job_stream_key(job_id)
        draft = self._draft(
            stream_key,
            PY_JOB_GATED,
            JobGatedData(job_id=job_id, gate_id=gate_id, expires_at=expires_at, trace=self._trace(record)),
            self._clock(),
        )
        events = await self._commit(job_id, [draft])
        return events[0]

    async def emit_finished(
        self,
        job_id: str,
        outcome: Literal["done", "failed", "cancelled"],
        *,
        result: JsonValue | None = None,
        error: CoreError | None = None,
    ) -> DurableEvent[Any]:
        """Emit ``py.job.finished`` (terminal; done only from running)."""
        record = await self.get_job(job_id)
        if record.status in TERMINAL_STATUSES:
            raise JobTransitionError(
                f"py.job.finished over a terminal job (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        if outcome == "done" and record.status != JobStatus.RUNNING.value:
            raise JobTransitionError(
                f"py.job.finished outcome=done is legal only from running (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        stream_key = job_stream_key(job_id)
        draft = self._draft(
            stream_key,
            PY_JOB_FINISHED,
            JobFinishedData(job_id=job_id, outcome=outcome, result=result, error=error, trace=self._trace(record)),
            self._clock(),
        )
        events = await self._commit(job_id, [draft])
        return events[0]

    # -- run / cancel ----------------------------------------------------------

    async def run(self, job_id: str, *, resume: bool = False) -> None:
        """Drive the job through the executor (deterministic, inline)."""
        await self._executor.drive(JobHandle(service=self, job_id=job_id), resume=resume)

    def spawn(self, job_id: str, *, resume: bool = False) -> asyncio.Task[None]:
        """Schedule ``run`` as a tracked background task (the API submit shape)."""
        task = asyncio.create_task(self.run(job_id, resume=resume))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel(self, job_id: str) -> JobRecord:
        """Cancel the job; idempotent (a terminal job returns unchanged)."""
        record = await self.get_job(job_id)
        if record.status in TERMINAL_STATUSES:
            return record
        await self.emit_finished(job_id, "cancelled")
        return await self.get_job(job_id)

    # -- §8: the restart-recovery scan -----------------------------------------

    async def recover(self, reader: JobCheckpointReader | None = None) -> list[JobRecoveryAction]:
        """Resolve every non-terminal job per the §8 recovery matrix.

        Branches: ``created`` -> re-dispatch (the fake run); a checkpoint
        with a terminal outcome -> the branch-⑤ idempotent write-back under
        the checkpoint's stable batch id; a checkpoint without an outcome ->
        branch-② resume (``run(resume=True)``); no reader, no checkpoint, or
        a spec-hash mismatch -> branch-④ fail closed: ``finished(failed)``
        carrying ``ExecutionUnknown`` (side effects cannot be proven). A
        ``JobTransitionError`` during a fail-closed write means a concurrent
        resolution won the race and is swallowed.
        """
        actions: list[JobRecoveryAction] = []
        for record in await self._repos.non_terminal_jobs():
            actions.append(await self._recover_one(record, reader))
        return actions

    async def _recover_one(self, record: JobRecord, reader: JobCheckpointReader | None) -> JobRecoveryAction:
        job_id = record.job_id
        if record.status == JobStatus.CREATED.value:
            self.spawn(job_id)
            return JobRecoveryAction(job_id=job_id, action="redispatched", detail={"status": record.status})
        checkpoint = await reader.read(job_id) if reader is not None else None
        spec_matches = (
            checkpoint is None
            or checkpoint.spec_hash is None
            or record.params_hash is None
            or checkpoint.spec_hash == record.params_hash
        )
        if checkpoint is not None and checkpoint.outcome is not None and spec_matches:
            await self._write_back(record, checkpoint)
            return JobRecoveryAction(job_id=job_id, action="written_back", detail={"outcome": checkpoint.outcome})
        if checkpoint is not None and checkpoint.outcome is None and spec_matches:
            self.spawn(job_id, resume=True)
            return JobRecoveryAction(job_id=job_id, action="resumed", detail={"status": record.status})
        await self._fail_closed_unknown(job_id, spec_mismatch=not spec_matches)
        return JobRecoveryAction(
            job_id=job_id,
            action="failed_unknown",
            detail={"status": record.status, "spec_mismatch": not spec_matches},
        )

    async def _fail_closed_unknown(self, job_id: str, *, spec_mismatch: bool) -> None:
        """Branch ④: the job's execution outcome is unknown at restart."""
        try:
            await self.emit_finished(
                job_id,
                "failed",
                error=CoreError(
                    code=CoreErrorCode.EXECUTION_UNKNOWN,
                    message=(
                        "job execution outcome is unknown after restart"
                        + (" (checkpoint spec hash mismatch)" if spec_mismatch else " (no checkpoint reader wired)")
                    ),
                    retryable=False,
                    details={"job_id": job_id, "spec_mismatch": spec_mismatch},
                ),
            )
        except JobTransitionError:
            return  # a concurrent resolution won the race

    async def _write_back(self, record: JobRecord, checkpoint: JobCheckpoint) -> None:
        """Branch ⑤: re-commit the checkpointed ``py.job.finished`` under its stable batch id.

        The draft is rebuilt exactly (event id, created-at milliseconds,
        content), so a recovery that runs after the write-back already
        committed is the writer's idempotent whole-batch replay: the
        originals return, nothing advances, no duplicate event exists.
        """
        assert (
            checkpoint.batch_id is not None
            and checkpoint.event_id is not None
            and checkpoint.created_at_ms is not None
            and checkpoint.outcome is not None
        )
        stream_key = job_stream_key(record.job_id)
        error = CoreError.model_validate(checkpoint.error) if checkpoint.error is not None else None
        # The span is derived from the checkpoint's stable event id (never
        # minted fresh): the rebuilt draft is content-identical across
        # recoveries, so a repeat is the writer's idempotent whole-batch
        # replay -- the entire point of branch ⑤.
        trace = JobTrace(
            trace_id=record.trace_id if record.trace_id is not None else self._mint("trc"),
            span_id=f"spn_{checkpoint.event_id[4:]}",
            parent_id=None,
            depth=0,
        )
        draft = EventDraft(
            id=checkpoint.event_id,
            type=PY_JOB_FINISHED,
            version=1,
            session_id=stream_key,
            created_at=from_epoch_ms(checkpoint.created_at_ms),
            data=JobFinishedData(
                job_id=record.job_id,
                outcome=checkpoint.outcome,
                result=checkpoint.result,
                error=error,
                trace=trace,
            ),
            metadata={},
        )
        async with self._lock(record.job_id):
            high = await self._repos.log_high(stream_key)
            batch = EventBatch(id=checkpoint.batch_id, events=(draft,))
            events = await self._store.commit(stream_key, batch, high)
        if self._bus is not None:
            self._bus.notify(stream_key, events)

    # -- lifecycle --------------------------------------------------------------

    async def close(self) -> None:
        """Cancel and await every spawned run task (the App close order: jobs first)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
