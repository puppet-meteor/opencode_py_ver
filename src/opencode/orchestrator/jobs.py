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
py.job.started         created/gated          attempt identity; -> running (the gated
                                              leg is the S5a gate-reply resume)
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

``gated -> running`` is produced by the S5a gate reply chain (T-C1-080):
``reply_gate`` emits ``py.job.started`` on approve/revise (then re-drives
the job from the advisory cursor through the injected broker), and a reject
resolves terminally through ``py.job.finished(failed)`` with
``PermissionDenied`` naming the gate. The reply is validated against the
active gate (id match, 300 s expiry per the m-3 precedent); every
rejection is a structured conflict carrying the job id and gate id, and an
expired or dangling gate never re-enters the graph.

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
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
from opencode.contracts.pipelines import MAX_SUBWORKFLOW_DEPTH, PipelineSpec
from opencode.orchestrator.gates import GATE_EXPIRY_S, GateBroker, GateResolution
from opencode.session.service import AscendingIds, Clock, IdFactory
from opencode.storage.jobs_store import JobRecord, JobsReadRepository, JobsStore, job_stream_key
from opencode.storage.repos import NotFoundError
from opencode.storage.store import EventBatch
from opencode.storage.txn import StoreValidationError, epoch_ms, from_epoch_ms
from opencode.tools.invoker import validate_input_strict, validate_schema_supported

#: The §8 restart-recovery scan set (non-terminal statuses).
NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"created", "running", "stalled", "gated"})

#: The terminal, absorbing statuses.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

#: The dispatch self-check horizon (PHASE3 §6 discipline 5, the S3 leftover
#: landed with S5a): an advisory probe that runs this many seconds after a
#: spawn and confirms the job is actually being driven (drive task alive /
#: event heartbeat / row readable). Results are in-memory and advisory only
#: -- no consumer gates on them in v1 (disclosed, CORE-CONTRACTS §5.4 S5a).
SELFCHECK_DELAY_S = 30.0


def compute_params_hash(params: JsonObject) -> str:
    """sha256 of the canonical params JSON (§6.2 pipeline; the S1 spec hash, see progress ①)."""
    return sha256(canonical_json(params).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- P3-S4 (T-C1-079): the submit-face params gate ----------------------------

#: The strict default params schema when a pipeline declares none: the empty
#: object only. Fail-closed reading of "unknown fields are rejected"
#: (PHASE3 §1 condition 4): an undeclared params face accepts no fields at
#: all, so a params-less pipeline still validates zero-model and strictly.
_EMPTY_PARAMS_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


class ParamsValidator(Protocol):
    """The gateway params gate every wired submit passes through (T-C1-079 ⑥).

    Zero-model strict JSON-Schema validation of ``params`` against the
    pipeline's declared schema: a missing field, a type error or an unknown
    field is a structured 4xx rejection (``StoreValidationError``), an
    unregistered pipeline a ``NotFoundError``. One shared instance serves the
    brain submit (``workflow__run``) and the future S5b router; it is injected
    into :class:`JobService` as a seam so S1/S2 direct-submit callers (no
    validator wired) keep their structural-only face unchanged.
    """

    def __call__(self, pipeline_id: str, params: JsonObject) -> None: ...


class SpecParamsValidator:
    """The concrete gate over a registered-pipeline lookup seam.

    ``lookup`` resolves a pipeline id to its registered :class:`PipelineSpec`
    (None = never registered -> NotFound). The declared ``params_schema`` must
    stay inside the strict validator's supported subset
    (:func:`opencode.tools.invoker.validate_schema_supported` -- the same
    subset the tool registry enforces at registration): a spec outside the
    subset is surfaced here fail-closed as a ValidationError rather than an
    invoke-time surprise. A spec without ``params_schema`` validates against
    the empty-object default above (only ``{}`` is legal).
    """

    def __init__(self, lookup: Callable[[str], PipelineSpec | None]) -> None:
        self._lookup = lookup

    def __call__(self, pipeline_id: str, params: JsonObject) -> None:
        spec = self._lookup(pipeline_id)
        if spec is None:
            raise NotFoundError(
                f"pipeline {pipeline_id!r} is not registered",
                details={"pipeline_id": pipeline_id},
            )
        schema = spec.params_schema if spec.params_schema is not None else _EMPTY_PARAMS_SCHEMA
        try:
            validate_schema_supported(schema, root=True)
        except ValueError as error:
            raise StoreValidationError(
                f"pipeline {pipeline_id!r} declares a params schema outside the strict subset: {error}",
                details={"pipeline_id": pipeline_id},
            ) from error
        issues = validate_input_strict(schema, params)
        if issues:
            raise StoreValidationError(
                f"params for pipeline {pipeline_id!r} failed strict validation: {'; '.join(issues)}",
                details={"pipeline_id": pipeline_id, "issues": list(issues)},
            )


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
        if state.status not in (JobStatus.CREATED, JobStatus.GATED):
            raise JobTransitionError(
                f"py.job.started is legal only from created or gated (status {state.status.value})",
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


class RunCanceller(Protocol):
    """The cancel hook (§7; S3 T-C1-078): stop the live execution of one job.

    :meth:`JobService.cancel` calls it *before* committing the terminal
    ``py.job.finished(cancelled)`` event, so the in-flight execution (an
    ``opencode_agent`` worker subprocess tree) is signalled while the job is
    still non-terminal and the fold stays the single status authority. The
    face is sync and best-effort by contract: it must never await, and a
    signal for an already-finished job is the idempotent no-op the service's
    terminal guard already provides.
    """

    def cancel(self, job_id: str) -> None: ...


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

    ``spec_hash`` carries the job's ``params_hash`` binding (the S2 engine
    writes the pipeline spec hash as its own ``pipeline_spec_hash`` graph
    channel and compares it on resume; the recovery scan keeps comparing the
    params binding, T-C1-077). The S3 worker face leaves this envelope
    unchanged (T-C1-078). A terminal ``outcome`` plus
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


@dataclass(frozen=True, slots=True)
class _ActiveGate:
    """The in-memory active-gate entry (S5a): the gate id plus its expiry horizon."""

    gate_id: str
    expires_at: datetime


class JobService:
    """The jobs service: submit, run, cancel, status, and the §8 recovery scan.

    Commits run per job under a per-job asyncio lock: read H -> commit the
    batch with expected_seq=H (the writer's CAS) -> notify the bus after the
    commit. IDs are minted through the shared composition ``IdFactory``
    (event/batch ids share the process-wide ascending counter, so
    ``evt_``/``bat_`` ids never collide with the session domain); the
    ``job_``/``att_``/``trc_``/``spn_``/``gat_`` conventions are built on the
    same counter's bare hex (OP-3: new internal prefixes carry no §3.1
    pattern).

    S5a (T-C1-080) adds three in-memory registries alongside the durable
    ``py.job.*`` spine: the active-gate registry (one live gate per job,
    populated by ``emit_gated`` and consumed/cleared by the reply face and
    ``emit_finished``), the per-job spawn-task registry (the cancel face
    interrupts the live drive tasks), and the advisory self-check reports.
    All three are process-local by design -- a restart rebuilds none of
    them, and recovery answers through the durable fold (an active gate
    lost to a restart is re-minted fresh by the re-driven ``human_gate``
    node; CORE-CONTRACTS §5.4 S5a).
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
        params_validator: ParamsValidator | None = None,
        canceller: RunCanceller | None = None,
        gate_broker: GateBroker | None = None,
        selfcheck_delay_s: float = SELFCHECK_DELAY_S,
    ) -> None:
        self._store = store
        self._repos = repos
        self._executor = executor
        self._bus = bus
        self._ids = ids if ids is not None else AscendingIds(clock)
        self._clock = clock if clock is not None else _utc_now
        self._params_validator = params_validator
        self._canceller = canceller
        self._gate_broker = gate_broker
        self._selfcheck_delay_s = selfcheck_delay_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._gates: dict[str, _ActiveGate] = {}
        self._job_tasks: dict[str, set[asyncio.Task[None]]] = {}
        # The S5a deliberate-interrupt marker: the cancel face marks the job
        # BEFORE cancelling its live drives, so jobs.run can tell its quiet
        # exit (the fold owns the terminal) from a genuine task cancellation
        # (close-time teardown keeps the task cancelled).
        self._interrupted: set[str] = set()
        self._selfchecks: dict[str, JsonObject] = {}

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

    def _draft(
        self,
        stream_key: str,
        event_type: str,
        data: Any,
        created_at: datetime,
        metadata: JsonObject | None = None,
    ) -> EventDraft[Any]:
        return EventDraft(
            id=self._ids.event_id(),
            type=event_type,
            version=1,
            session_id=stream_key,
            created_at=created_at,
            data=data,
            metadata=metadata if metadata is not None else {},
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
        lineage: JobLineage | None = None,
    ) -> JobHandle:
        """Submit one job: ``created`` + ``spec_bound`` atomically, before any execution.

        Structural validation only in S1 (non-empty pipeline_id, JSON-object
        params, anchor ``"api"`` or ``^ses_``); validating params against the
        pipeline's JSON Schema needs spec loading and lands in S2 (progress ①).
        P3-S4 (T-C1-079) adds the two gateway faces: the injected params
        validator (zero-model strict JSON-Schema validation over the
        registered spec, PHASE3 §1 condition 4 -- shared by the brain submit
        and the future S5b router) and the M-9 lineage/depth recursion guard
        (``lineage`` declares the submission's parent/origin explicitly; None
        keeps the S1 derived face).
        """
        if not isinstance(pipeline_id, str) or not pipeline_id:
            raise StoreValidationError("pipeline_id must be a non-empty string")
        if not isinstance(params, dict):
            raise StoreValidationError("params must be a JSON object")
        if not isinstance(session_anchor, str) or (session_anchor != "api" and not session_anchor.startswith("ses_")):
            raise StoreValidationError("session_anchor must be 'api' or a ses_-prefixed session id")
        if self._params_validator is not None:
            self._params_validator(pipeline_id, params)
        if lineage is None:
            resolved: JobLineage = JobLineage(
                parent_job_id=None,
                session_id=None if session_anchor == "api" else session_anchor,
                origin="api" if session_anchor == "api" else "session",
            )
        else:
            await self._check_lineage(lineage, depth, session_anchor)
            resolved = lineage
        job_id = self._mint("job")
        stream_key = job_stream_key(job_id)
        trace_id = self._mint("trc")
        now = self._clock()
        created = self._draft(
            stream_key,
            PY_JOB_CREATED,
            JobCreatedData(
                job_id=job_id,
                pipeline_id=pipeline_id,
                depth=depth,
                lineage=resolved,
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

    # -- P3-S4 (T-C1-079): the M-9 lineage/depth recursion guard ----------------

    async def _check_lineage(self, lineage: JobLineage, depth: int, session_anchor: str) -> None:
        """Reject recursive/illegal submissions before any event is drafted (M-9).

        The gateway's first line (PHASE3 §5.2): a worker identity never
        submits (``origin == "worker"`` is the workers' reserved channel), the
        declared lineage must agree with the anchor, the ancestor chain must
        exist and stay within the cap, and ``depth <= 3`` is the second line
        behind the spec-side hard cap. Every rejection is a structured 4xx
        (``StoreValidationError``; a missing ancestor surfaces the read
        repository's ``NotFoundError``).
        """
        if lineage.origin == "worker":
            raise StoreValidationError(
                "worker-identity job submission is rejected (M-9 recursion guard)",
                details={"origin": "worker"},
            )
        if lineage.origin == "api" and session_anchor != "api":
            raise StoreValidationError(
                "lineage origin 'api' requires session_anchor 'api'",
                details={"origin": "api", "session_anchor": session_anchor},
            )
        if lineage.origin == "session" and (
            session_anchor == "api" or (lineage.session_id is not None and lineage.session_id != session_anchor)
        ):
            raise StoreValidationError(
                "lineage origin 'session' requires a matching ses_-prefixed session_anchor",
                details={"origin": "session", "session_anchor": session_anchor},
            )
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise StoreValidationError("depth must be a non-negative integer")
        if depth > MAX_SUBWORKFLOW_DEPTH:
            raise StoreValidationError(
                f"workflow nesting depth {depth} exceeds the hard cap {MAX_SUBWORKFLOW_DEPTH} (M-9 second line)",
                details={"depth": depth, "max_depth": MAX_SUBWORKFLOW_DEPTH},
            )
        if lineage.parent_job_id is None:
            if depth != 0:
                raise StoreValidationError(
                    "a root submission (no parent job) must carry depth 0",
                    details={"depth": depth},
                )
            return
        # The ancestor walk: every ancestor must exist (NotFoundError on a
        # dangling parent_job_id) and the chain must stay within the cap.
        chain: list[str] = []
        parent_id: str | None = lineage.parent_job_id
        parent_depth: int | None = None
        while parent_id is not None:
            record = await self.get_job(parent_id)
            chain.append(record.job_id)
            if parent_depth is None:
                parent_depth = record.depth
            raw_parent = (record.lineage or {}).get("parent_job_id")
            parent_id = raw_parent if isinstance(raw_parent, str) else None
        if len(chain) > MAX_SUBWORKFLOW_DEPTH:
            raise StoreValidationError(
                f"the ancestor chain length {len(chain)} exceeds the cap {MAX_SUBWORKFLOW_DEPTH} (M-9)",
                details={"chain_length": len(chain), "max_depth": MAX_SUBWORKFLOW_DEPTH},
            )
        if parent_depth is None or depth != parent_depth + 1:
            raise StoreValidationError(
                "lineage depth must equal the parent job's depth + 1",
                details={"depth": depth, "parent_depth": parent_depth},
            )

    # -- reads ----------------------------------------------------------------

    async def get_job(self, job_id: str) -> JobRecord:
        """The job's read-model row; ``NotFoundError`` (a StoreError) when absent."""
        record = await self._repos.job_by_id(job_id)
        if record is None:
            raise NotFoundError(f"job {job_id} does not exist", details={"job_id": job_id})
        return record

    # -- the lifecycle emits (the executor's commit faces) ---------------------

    async def emit_started(self, job_id: str, *, attempt_id: str | None = None) -> DurableEvent[Any]:
        """Emit ``py.job.started`` (created -> running; gated -> running is the S5a resume).

        The gated leg is the gate-reply chain: ``reply_gate`` starts a gated
        job (approve/revise) before re-driving it from the advisory cursor,
        and the engine's gate body does the same on a reply-less resume.
        """
        record = await self.get_job(job_id)
        if record.status not in (JobStatus.CREATED.value, JobStatus.GATED.value):
            raise JobTransitionError(
                f"py.job.started is legal only from created or gated (status {record.status})",
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

    async def emit_gated(
        self,
        job_id: str,
        gate_id: str | None = None,
        expires_at: datetime | None = None,
        *,
        conversation_ref: str | None = None,
    ) -> DurableEvent[Any]:
        """Emit ``py.job.gated`` (running -> gated; the human-gate interrupt, S5a).

        With no explicit gate id/expiry (the engine's gate body), the gate
        is minted here (``gat_`` prefix, unique within the job) and the
        expiry defaults to the 300 s m-3 precedent. The event itself is the
        gate card over the job's control channel: ``conversation_ref``
        (the engine passes the job's session anchor) rides the envelope
        metadata so the frontend routes the card to its conversation without
        the brain ever seeing it. The active-gate registry records the mint
        (in-memory; ``reply_gate`` validates against it and
        ``emit_finished`` clears it).
        """
        record = await self.get_job(job_id)
        if record.status != JobStatus.RUNNING.value:
            raise JobTransitionError(
                f"py.job.gated is legal only from running (status {record.status})",
                details={"job_id": job_id, "status": record.status},
            )
        resolved_gate = gate_id if gate_id is not None else self._mint("gat")
        resolved_expiry = expires_at if expires_at is not None else self._clock() + timedelta(seconds=GATE_EXPIRY_S)
        stream_key = job_stream_key(job_id)
        metadata: JsonObject | None = {"conversation_ref": conversation_ref} if conversation_ref is not None else None
        draft = self._draft(
            stream_key,
            PY_JOB_GATED,
            JobGatedData(job_id=job_id, gate_id=resolved_gate, expires_at=resolved_expiry, trace=self._trace(record)),
            self._clock(),
            metadata,
        )
        events = await self._commit(job_id, [draft])
        self._gates[job_id] = _ActiveGate(gate_id=resolved_gate, expires_at=resolved_expiry)
        return events[0]

    async def emit_finished(
        self,
        job_id: str,
        outcome: Literal["done", "failed", "cancelled"],
        *,
        result: JsonValue | None = None,
        error: CoreError | None = None,
    ) -> DurableEvent[Any]:
        """Emit ``py.job.finished`` (terminal; done only from running).

        A terminal emit also clears the job's active-gate registry entry
        (S5a): a cancelled or failed gated job takes its gate with it --
        the card is invalidated by the same event that resolves the job.
        """
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
        self._gates.pop(job_id, None)
        return events[0]

    # -- the S5a gate-reply chain (T-C1-080) --------------------------------------

    def active_gate(self, job_id: str) -> str | None:
        """The job's one active gate id (None = no live gate; the pure read face)."""
        gate = self._gates.get(job_id)
        return gate.gate_id if gate is not None else None

    async def reply_gate(
        self,
        job_id: str,
        gate_id: str,
        reply: Literal["approve", "reject", "revise"],
        *,
        params: JsonObject | None = None,
    ) -> JobRecord:
        """Resolve one human-gate reply (approve/reject/revise; PHASE3 §4).

        Validation order -- every rejection is a structured conflict carrying
        the job id and gate id, so the transport answers 409 with the same
        body shape the S1 shell pinned: the chain must be wired (a broker
        exists); the job must be ``gated``; the gate must be the job's one
        active gate; the expiry must not have passed. An approve/revise then
        starts the job (``gated -> running``), stores the resolution on the
        broker and spawns the resume drive: the engine's gate node takes the
        reply and the graph continues from the advisory cursor (a revise
        carries the revised params into the node result for downstream
        nodes; the job's ``params_hash`` binding is never re-bound). A
        reject resolves the job terminally -- ``finished(failed)`` with
        ``PermissionDenied`` naming the gate, the card invalidated by the
        same event. A concurrent terminal resolution (a cancel race)
        surfaces as the fold's ``JobTransitionError``, unchanged for the
        caller.
        """
        details: JsonObject = {"job_id": job_id, "gate_id": gate_id}
        if self._gate_broker is None:
            raise JobTransitionError(
                "the human-gate reply chain is not wired in this composition (no gate broker)",
                details=details,
            )
        record = await self.get_job(job_id)
        if record.status != JobStatus.GATED.value:
            raise JobTransitionError(
                f"job {job_id} is not gated (status {record.status}); no reply is applied",
                details=details,
            )
        gate = self._gates.get(job_id)
        if gate is None or gate.gate_id != gate_id:
            raise JobTransitionError(
                f"gate {gate_id!r} is not the active gate of job {job_id}"
                + ("" if gate is None else f" (active gate {gate.gate_id!r})"),
                details=details,
            )
        if self._clock() >= gate.expires_at:
            self._gates.pop(job_id, None)
            raise JobTransitionError(
                f"gate {gate_id!r} of job {job_id} has expired; no reply is applied",
                details=details,
            )
        self._gates.pop(job_id, None)
        if reply == "reject":
            await self.emit_finished(
                job_id,
                "failed",
                error=CoreError(
                    code=CoreErrorCode.PERMISSION_DENIED,
                    message=f"the human gate {gate_id!r} rejected the job",
                    retryable=False,
                    details=details,
                ),
            )
            return await self.get_job(job_id)
        await self.emit_started(job_id)
        self._gate_broker.resolve(
            GateResolution(
                job_id=job_id,
                gate_id=gate_id,
                reply=reply,
                params=params if reply == "revise" else None,
            )
        )
        self.spawn(job_id, resume=True)
        return await self.get_job(job_id)

    # -- run / cancel --------------------------------------------------------------

    async def run(self, job_id: str, *, resume: bool = False) -> None:
        """Drive the job through the executor (deterministic, inline).

        The S5a cancel face interrupts a live drive with ``task.cancel()``
        (CORE-CONTRACTS §5.4 S5a) -- and only that deliberate interrupt is
        absorbed here, at the spawn wrapper (the cancel face marks the job
        first): the interrupted drive exits quietly, never touches job
        state on the way out, and the fold stays the sole terminal
        authority, the same discipline as the S3 ``WorkerSuspended``
        swallow one frame down. Any other cancellation (close-time
        teardown) is a genuine task cancellation and re-raises untouched.
        """
        try:
            await self._executor.drive(JobHandle(service=self, job_id=job_id), resume=resume)
        except asyncio.CancelledError:
            if job_id not in self._interrupted:
                raise
            return

    def spawn(self, job_id: str, *, resume: bool = False) -> asyncio.Task[None]:
        """Schedule ``run`` as a tracked background task (the API submit shape).

        The task also registers under the job's id -- the S5a cancel face
        interrupts exactly the drives of the cancelled job -- and an
        advisory self-check probe is scheduled alongside (§6 discipline 5:
        ``selfcheck_delay_s`` after dispatch, swallowing everything; the
        report is in-memory and no consumer gates on it in v1 -- disclosed,
        CORE-CONTRACTS §5.4 S5a).
        """
        task = asyncio.create_task(self.run(job_id, resume=resume))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._job_tasks.setdefault(job_id, set()).add(task)

        def _release(finished: asyncio.Task[None]) -> None:
            tasks = self._job_tasks.get(job_id)
            if tasks is not None:
                tasks.discard(finished)
                if not tasks:
                    self._job_tasks.pop(job_id, None)

        task.add_done_callback(_release)
        if self._selfcheck_delay_s >= 0:
            self._tasks.add(asyncio.create_task(self._advisory_selfcheck(job_id)))
        return task

    async def _advisory_selfcheck(self, job_id: str) -> None:
        """The deferred probe body: sleep the horizon, then record the report (never raise)."""
        await asyncio.sleep(self._selfcheck_delay_s)
        try:
            await self.selfcheck(job_id)
        except Exception:
            return  # advisory only: a failing probe never surfaces anywhere

    async def selfcheck(self, job_id: str) -> JsonObject:
        """The §6 discipline-5 three-check probe: is the dispatch actually being driven?

        Pure reads, honestly answered: (1) ``row`` -- the job's read-model
        row is readable; (2) ``heartbeat`` -- the committed event high
        watermark advanced past the submit batch (the A fork-3 pair);
        (3) ``drive_task`` -- the job has a live drive task, or is
        legitimately self-settled (terminal: nothing left to drive; gated:
        parked on a human reply). ``ok`` is the conjunction. The report is
        recorded for the ``last_selfcheck`` read face (in-memory).
        """
        try:
            record = await self.get_job(job_id)
        except NotFoundError:
            self._selfchecks[job_id] = {
                "job_id": job_id,
                "row": False,
                "heartbeat": False,
                "drive_task": False,
                "ok": False,
            }
            return self._selfchecks[job_id]
        high = await self._repos.log_high(job_stream_key(job_id))
        heartbeat = high >= 2  # py.job.created + py.job.spec_bound committed
        drive_task = record.status in TERMINAL_STATUSES or record.status == "gated" or bool(self._job_tasks.get(job_id))
        report: JsonObject = {
            "job_id": job_id,
            "row": True,
            "heartbeat": heartbeat,
            "drive_task": drive_task,
            "ok": heartbeat and drive_task,
        }
        self._selfchecks[job_id] = report
        return report

    def last_selfcheck(self, job_id: str) -> JsonObject | None:
        """The most recent advisory self-check report (None = not yet run)."""
        return self._selfchecks.get(job_id)

    async def cancel(self, job_id: str) -> JobRecord:
        """Cancel the job -- and its non-terminal descendants -- idempotently.

        The S5a full chain (PHASE3 §7): descendants first (leaves before the
        parent, each through this same face -- a subworkflow's nested job is
        an independent job record whose lineage parent is the cancelled
        job), then the job itself: the canceller hook (S3) kills a live
        worker subprocess tree *before* the terminal emit, the job's live
        drive tasks are interrupted (``task.cancel`` -- the interrupted
        node's side effects are bounded by the checkpointer's last
        committed node boundary, the same frontier a restart would see),
        and ``py.job.finished(cancelled)`` commits the outcome with the
        fold as the single status authority. A concurrent resolution that
        already made the job terminal is swallowed (idempotent over the
        closed terminal set: the pinned second cancel is the no-op). A
        gated job cancelled here takes its active gate with it (the
        registry clear inside ``emit_finished``): the card is invalidated
        by the same event.
        """
        record = await self.get_job(job_id)
        if record.status in TERMINAL_STATUSES:
            return record
        for child_id in await self._non_terminal_children(job_id):
            await self.cancel(child_id)
        if self._canceller is not None:
            self._canceller.cancel(job_id)
        # Mark before the interrupt so the drive can never observe its own
        # cancellation without the marker (the quiet-exit contract, jobs.run).
        self._interrupted.add(job_id)
        for task in list(self._job_tasks.get(job_id, ())):
            task.cancel()
        try:
            await self.emit_finished(job_id, "cancelled")
        except JobTransitionError:
            pass  # a concurrent resolution won the race; the fold is the authority
        return await self.get_job(job_id)

    async def _non_terminal_children(self, job_id: str) -> list[str]:
        """The job's non-terminal children by lineage (the subworkflow descent walk)."""
        children: list[str] = []
        for record in await self._repos.non_terminal_jobs():
            lineage = record.lineage if record.lineage is not None else {}
            if lineage.get("parent_job_id") == job_id:
                children.append(record.job_id)
        return children

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
        self._job_tasks.clear()
