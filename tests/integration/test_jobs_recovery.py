"""Integration tests for the pure fold guards + the §8 restart-recovery matrix (T-C1-076, P3-S1).

Two layers over the jobs domain:

- the pure fold (:func:`fold_job_event` over synthetic ``DurableEvent``
  envelopes, no database): the legal full lifecycle including ``stalled ->
  redispatched -> finished(done)``, and every rejected shape -- finished(done)
  off running, the terminal absorbing rule, the exactly-once redispatch guard,
  gated off running, created over an existing job, a second spec binding, and
  a non-job event type;
- the §8 five-branch recovery matrix over a real migrated database and the
  standalone JobsStore/JobsReadRepository/JobService stack: created ->
  redispatch; no reader -> fail closed (``ExecutionUnknown``); a checkpoint
  without an outcome -> resume; a terminal checkpoint -> the idempotent
  branch-5 write-back (repeating the recovery over a tampered row replays the
  exact batch -- no new events, H unchanged; the rebuild heals the row);
  a spec-hash mismatch -> fail closed; gated without a reader -> fail closed.
  Plus the App wiring readability (``app.jobs`` is the JobService over the
  shared engine/bus/ids) and the close semantics (spawned run tasks are
  cancelled; close is safe on an idle service).
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.contracts import DurableEvent
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
    RunCancelRequestedData,
)
from opencode.contracts.messages import JsonObject
from opencode.orchestrator.jobs import (
    FakeJobExecutor,
    JobCheckpoint,
    JobHandle,
    JobService,
    JobsProjector,
    JobState,
    JobStatus,
    JobTransitionError,
    compute_params_hash,
    fold_job_event,
)
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobRecord, JobsReadRepository, JobsStore, job_stream_key

REPO_ROOT = Path(__file__).resolve().parents[2]

_T0 = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)
_PARAMS: JsonObject = {"query": "hi"}
_PARAMS_HASH = compute_params_hash(_PARAMS)


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# The pure fold: helpers over synthetic envelopes (no database behind them)
# ---------------------------------------------------------------------------

_FOLD_SEQ = iter(range(1, 10_000))


def _trace() -> JobTrace:
    return JobTrace(trace_id="trc_fold", span_id="spn_fold", parent_id=None, depth=0)


def _fold_event(
    event_type: str, data: Any, job_id: str = "job_fold", *, metadata: JsonObject | None = None
) -> DurableEvent[Any]:
    """One synthetic committed envelope; the counter keeps ids and seqs apart."""
    n = next(_FOLD_SEQ)
    return DurableEvent(
        id=f"evt_{n:026d}",
        type=event_type,
        version=1,
        session_id=job_stream_key(job_id),
        created_at=_T0,
        data=data,
        metadata=metadata if metadata is not None else {},
        seq=n,
    )


def _created_data(job_id: str = "job_fold") -> JobCreatedData:
    return JobCreatedData(
        job_id=job_id,
        pipeline_id="research",
        depth=0,
        lineage=JobLineage(origin="api"),
        budget=JobBudget(),
        trace=_trace(),
    )


def _spec_bound_data(job_id: str = "job_fold") -> JobSpecBoundData:
    return JobSpecBoundData(
        job_id=job_id,
        params=_PARAMS,
        params_hash=_PARAMS_HASH,
        session_anchor="api",
        trace=_trace(),
    )


def _bound_state(job_id: str = "job_fold") -> JobState:
    state = fold_job_event(None, _fold_event(PY_JOB_CREATED, _created_data(job_id)))
    return fold_job_event(state, _fold_event(PY_JOB_SPEC_BOUND, _spec_bound_data(job_id)))


def _running_state(job_id: str = "job_fold") -> JobState:
    return fold_job_event(
        _bound_state(job_id),
        _fold_event(PY_JOB_STARTED, JobStartedData(job_id=job_id, attempt_id="att_1", trace=_trace())),
    )


def _stalled_state(job_id: str = "job_fold") -> JobState:
    return fold_job_event(
        _running_state(job_id),
        _fold_event(
            PY_JOB_STALLED,
            JobStalledData(job_id=job_id, stalled_reason="watchdog", attempt_id="att_1", trace=_trace()),
        ),
    )


def _gated_state(job_id: str = "job_fold") -> JobState:
    return fold_job_event(
        _running_state(job_id),
        _fold_event(
            PY_JOB_GATED,
            JobGatedData(job_id=job_id, gate_id="gate_1", expires_at=_T0 + timedelta(seconds=300), trace=_trace()),
        ),
    )


class TestFoldLifecycle:
    def test_the_full_legal_lifecycle_folds_to_done(self) -> None:
        created = _fold_event(PY_JOB_CREATED, _created_data("job_full"))
        state = fold_job_event(None, created)
        assert state.status is JobStatus.CREATED
        assert state.pipeline_id == "research"
        assert state.applied_seq == created.seq

        spec_bound = _fold_event(PY_JOB_SPEC_BOUND, _spec_bound_data("job_full"))
        state = fold_job_event(state, spec_bound)
        assert state.status is JobStatus.CREATED
        assert state.params == _PARAMS
        assert state.params_hash == _PARAMS_HASH
        assert state.session_anchor == "api"
        assert state.applied_seq == spec_bound.seq

        started = _fold_event(PY_JOB_STARTED, JobStartedData(job_id="job_full", attempt_id="att_1", trace=_trace()))
        state = fold_job_event(state, started)
        assert state.status is JobStatus.RUNNING
        assert state.attempt_id == "att_1"

        stalled = _fold_event(
            PY_JOB_STALLED,
            JobStalledData(job_id="job_full", stalled_reason="watchdog", attempt_id="att_1", trace=_trace()),
        )
        state = fold_job_event(state, stalled)
        assert state.status is JobStatus.STALLED
        assert state.stalled_reason == "watchdog"

        redispatched = _fold_event(
            PY_JOB_REDISPATCHED,
            JobRedispatchedData(job_id="job_full", attempt_id="att_2", retry_count=1, trace=_trace()),
        )
        state = fold_job_event(state, redispatched)
        assert state.status is JobStatus.RUNNING
        assert state.retry_count == 1
        assert state.attempt_id == "att_2"
        assert state.stalled_reason is None  # the stall is cleared on redispatch

        finished = _fold_event(
            PY_JOB_FINISHED,
            JobFinishedData(job_id="job_full", outcome="done", result={"answer": 1}, trace=_trace()),
        )
        state = fold_job_event(state, finished)
        assert state.status is JobStatus.DONE
        assert state.result == {"answer": 1}
        assert state.applied_seq == finished.seq

    def test_failed_and_cancelled_may_land_from_any_non_terminal_state(self) -> None:
        for state in (_bound_state(), _running_state(), _stalled_state(), _gated_state()):
            failed = fold_job_event(
                state,
                _fold_event(
                    PY_JOB_FINISHED,
                    JobFinishedData(
                        job_id="job_fold",
                        outcome="failed",
                        error=CoreError(code=CoreErrorCode.EXECUTION_UNKNOWN, message="boom", retryable=False),
                        trace=_trace(),
                    ),
                ),
            )
            assert failed.status is JobStatus.FAILED
            assert failed.error is not None
            assert failed.error["code"] == "ExecutionUnknown"


class TestFoldGuards:
    def test_created_rejects_over_an_existing_job(self) -> None:
        with pytest.raises(JobTransitionError, match="over an existing job"):
            fold_job_event(_running_state(), _fold_event(PY_JOB_CREATED, _created_data()))

    def test_no_job_event_can_precede_created(self) -> None:
        with pytest.raises(JobTransitionError, match="cannot be the first event"):
            fold_job_event(
                None,
                _fold_event(PY_JOB_STARTED, JobStartedData(job_id="job_fold", attempt_id="att_1", trace=_trace())),
            )

    def test_spec_bound_rejects_from_running(self) -> None:
        with pytest.raises(JobTransitionError, match="legal only from created"):
            fold_job_event(_running_state(), _fold_event(PY_JOB_SPEC_BOUND, _spec_bound_data()))

    def test_spec_bound_rejects_a_second_binding(self) -> None:
        with pytest.raises(JobTransitionError, match="already bound"):
            fold_job_event(_bound_state(), _fold_event(PY_JOB_SPEC_BOUND, _spec_bound_data()))

    def test_started_rejects_from_running(self) -> None:
        with pytest.raises(JobTransitionError, match="legal only from created"):
            fold_job_event(
                _running_state(),
                _fold_event(PY_JOB_STARTED, JobStartedData(job_id="job_fold", attempt_id="att_2", trace=_trace())),
            )

    def test_stalled_rejects_from_created(self) -> None:
        with pytest.raises(JobTransitionError, match="legal only from running"):
            fold_job_event(
                _bound_state(),
                _fold_event(
                    PY_JOB_STALLED,
                    JobStalledData(job_id="job_fold", stalled_reason="watchdog", attempt_id="att_1", trace=_trace()),
                ),
            )

    def test_redispatch_rejects_off_stalled(self) -> None:
        with pytest.raises(JobTransitionError, match="legal only from stalled"):
            fold_job_event(
                _running_state(),
                _fold_event(
                    PY_JOB_REDISPATCHED,
                    JobRedispatchedData(job_id="job_fold", attempt_id="att_2", retry_count=1, trace=_trace()),
                ),
            )

    def test_redispatch_is_legal_exactly_once(self) -> None:
        # the event itself must carry retry_count == 1
        with pytest.raises(JobTransitionError, match="exactly once"):
            fold_job_event(
                _stalled_state(),
                _fold_event(
                    PY_JOB_REDISPATCHED,
                    JobRedispatchedData(job_id="job_fold", attempt_id="att_2", retry_count=2, trace=_trace()),
                ),
            )
        # the one legal redispatch advances retry_count to 1 ...
        running_again = fold_job_event(
            _stalled_state(),
            _fold_event(
                PY_JOB_REDISPATCHED,
                JobRedispatchedData(job_id="job_fold", attempt_id="att_2", retry_count=1, trace=_trace()),
            ),
        )
        assert running_again.retry_count == 1
        # ... so a later stall + second redispatch hits the exactly-once guard
        stalled_again = fold_job_event(
            running_again,
            _fold_event(
                PY_JOB_STALLED,
                JobStalledData(job_id="job_fold", stalled_reason="watchdog", attempt_id="att_2", trace=_trace()),
            ),
        )
        with pytest.raises(JobTransitionError, match="exactly once"):
            fold_job_event(
                stalled_again,
                _fold_event(
                    PY_JOB_REDISPATCHED,
                    JobRedispatchedData(job_id="job_fold", attempt_id="att_3", retry_count=1, trace=_trace()),
                ),
            )

    def test_gated_rejects_from_stalled(self) -> None:
        with pytest.raises(JobTransitionError, match="legal only from running"):
            fold_job_event(
                _stalled_state(),
                _fold_event(
                    PY_JOB_GATED,
                    JobGatedData(
                        job_id="job_fold", gate_id="gate_1", expires_at=_T0 + timedelta(seconds=300), trace=_trace()
                    ),
                ),
            )

    def test_finished_done_rejects_off_running(self) -> None:
        for state in (_bound_state(), _stalled_state(), _gated_state()):
            with pytest.raises(JobTransitionError, match="outcome=done is legal only from running"):
                fold_job_event(
                    state,
                    _fold_event(PY_JOB_FINISHED, JobFinishedData(job_id="job_fold", outcome="done", trace=_trace())),
                )

    def test_terminal_statuses_absorb_every_later_event(self) -> None:
        later = (
            (
                PY_JOB_STARTED,
                lambda: JobStartedData(job_id="job_fold", attempt_id="att_9", trace=_trace()),
            ),
            (
                PY_JOB_STALLED,
                lambda: JobStalledData(
                    job_id="job_fold", stalled_reason="watchdog", attempt_id="att_9", trace=_trace()
                ),
            ),
            (
                PY_JOB_GATED,
                lambda: JobGatedData(
                    job_id="job_fold", gate_id="gate_9", expires_at=_T0 + timedelta(seconds=300), trace=_trace()
                ),
            ),
            (PY_JOB_FINISHED, lambda: JobFinishedData(job_id="job_fold", outcome="failed", trace=_trace())),
        )
        for terminal_state in (
            fold_job_event(
                _running_state(),
                _fold_event(PY_JOB_FINISHED, JobFinishedData(job_id="job_fold", outcome="done", trace=_trace())),
            ),
            fold_job_event(
                _bound_state(),
                _fold_event(PY_JOB_FINISHED, JobFinishedData(job_id="job_fold", outcome="failed", trace=_trace())),
            ),
            fold_job_event(
                _bound_state(),
                _fold_event(PY_JOB_FINISHED, JobFinishedData(job_id="job_fold", outcome="cancelled", trace=_trace())),
            ),
        ):
            assert terminal_state.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
            for event_type, data in later:
                with pytest.raises(JobTransitionError, match=r"status (done|failed|cancelled)"):
                    fold_job_event(terminal_state, _fold_event(event_type, data()))

    def test_a_non_job_event_type_is_not_part_of_the_fold(self) -> None:
        with pytest.raises(JobTransitionError, match=r"not part of the py\.job"):
            fold_job_event(
                _running_state(),
                _fold_event(
                    "py.run.cancel_requested",
                    RunCancelRequestedData(run_id="run_1", reason="user"),
                    metadata={"run_id": "run_1"},
                ),
            )


# ---------------------------------------------------------------------------
# The §8 recovery matrix: the standalone stack over a real migrated database
# ---------------------------------------------------------------------------


class _StaticReader:
    """The injectable checkpoint reader face: canned checkpoints, no I/O."""

    def __init__(self, checkpoints: dict[str, JobCheckpoint]) -> None:
        self._checkpoints = checkpoints

    async def read(self, job_id: str) -> JobCheckpoint | None:
        return self._checkpoints.get(job_id)


@pytest.fixture()
async def service_stack(tmp_path: Path) -> Any:
    """One migrated database + the jobs writer/read/service stack (closed on exit)."""
    db_path = tmp_path / "recovery.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\n{result.stderr}"
    database = open_database(db_path, mode="create")
    store = JobsStore(database.engine, projector=JobsProjector())
    repos = JobsReadRepository(database.engine)
    service = JobService(
        store=store,
        repos=repos,
        executor=FakeJobExecutor(),
        ids=AscendingIds(lambda: _T0),
        clock=lambda: _T0,
    )
    try:
        yield service, store, repos, db_path
    finally:
        await service.close()
        await database.close()


async def _running_record(service: JobService) -> JobRecord:
    """One submitted + started job (the classic crash mid-execution shape)."""
    handle = await service.submit("research", _PARAMS, "api")
    await service.emit_started(handle.job_id)
    return await service.get_job(handle.job_id)


async def _await_status(service: JobService, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> JobRecord:
    """Poll the read model until the spawned run settles the job."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await service.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


def _event_count(db_path: Path, stream: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (stream,)).fetchone()[0])


class TestRecoveryBranches:
    async def test_created_redispatches_and_settles(self, service_stack: Any) -> None:
        service, _store, _repos, _db_path = service_stack
        handle = await service.submit("research", _PARAMS, "api")  # created, never run
        actions = await service.recover()
        assert [action.action for action in actions] == ["redispatched"]
        assert actions[0].job_id == handle.job_id
        record = await _await_status(service, handle.job_id, {"done"})
        assert record.result == {"executor": "fake"}

    async def test_running_without_a_reader_fails_closed(self, service_stack: Any) -> None:
        service, _store, _repos, _db_path = service_stack
        record = await _running_record(service)
        actions = await service.recover()  # reader None: the App default
        assert [action.action for action in actions] == ["failed_unknown"]
        assert actions[0].detail == {"status": "running", "spec_mismatch": False}
        failed = await service.get_job(record.job_id)
        assert failed.status == "failed"
        assert failed.error is not None
        assert failed.error["code"] == "ExecutionUnknown"
        assert "no checkpoint reader wired" in failed.error["message"]

    async def test_a_checkpoint_without_an_outcome_resumes(self, service_stack: Any) -> None:
        service, _store, _repos, _db_path = service_stack
        record = await _running_record(service)
        reader = _StaticReader(
            {record.job_id: JobCheckpoint(job_id=record.job_id, attempt_id="att_1", spec_hash=record.params_hash)}
        )
        actions = await service.recover(reader)
        assert [action.action for action in actions] == ["resumed"]
        assert actions[0].detail == {"status": "running"}
        done = await _await_status(service, record.job_id, {"done"})
        assert done.result == {"executor": "fake"}

    async def test_a_terminal_checkpoint_is_written_back_idempotently(self, service_stack: Any) -> None:
        service, store, repos, db_path = service_stack
        record = await _running_record(service)
        stream = job_stream_key(record.job_id)
        checkpoint = JobCheckpoint(
            job_id=record.job_id,
            attempt_id="att_1",
            spec_hash=record.params_hash,
            batch_id="bat_wb",
            outcome="done",
            result={"answer": 42},
            event_id="evt_wb1",
            created_at_ms=1_770_000_000_000,
        )
        reader = _StaticReader({record.job_id: checkpoint})
        actions = await service.recover(reader)
        assert [action.action for action in actions] == ["written_back"]
        assert actions[0].detail == {"outcome": "done"}
        done = await service.get_job(record.job_id)
        assert done.status == "done"
        assert done.result == {"answer": 42}
        assert _event_count(db_path, stream) == 4  # created, spec_bound, started, written-back finished
        assert await repos.log_high(stream) == 4
        # A repeated recovery over a tampered row replays the exact batch: no
        # new events, H unchanged (the write-back is the idempotent replay path).
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (record.job_id,))
        actions = await service.recover(reader)
        assert [action.action for action in actions] == ["written_back"]
        assert _event_count(db_path, stream) == 4
        assert await repos.log_high(stream) == 4
        # Replay never re-projects; the rebuild heals the tampered row from the log.
        await store.rebuild(stream)
        healed = await service.get_job(record.job_id)
        assert healed.status == "done"
        assert healed.result == {"answer": 42}

    async def test_a_spec_hash_mismatch_fails_closed(self, service_stack: Any) -> None:
        service, _store, _repos, _db_path = service_stack
        record = await _running_record(service)
        reader = _StaticReader(
            {
                record.job_id: JobCheckpoint(
                    job_id=record.job_id,
                    spec_hash="deadbeef",
                    outcome="done",
                    batch_id="bat_x",
                    event_id="evt_x",
                    created_at_ms=1,
                )
            }
        )
        actions = await service.recover(reader)
        assert [action.action for action in actions] == ["failed_unknown"]
        assert actions[0].detail == {"status": "running", "spec_mismatch": True}
        failed = await service.get_job(record.job_id)
        assert failed.status == "failed"
        assert failed.error is not None
        assert failed.error["code"] == "ExecutionUnknown"
        assert "spec hash mismatch" in failed.error["message"]

    async def test_gated_without_a_reader_fails_closed(self, service_stack: Any) -> None:
        service, _store, _repos, _db_path = service_stack
        handle = await service.submit("research", _PARAMS, "api")
        await service.emit_started(handle.job_id)
        await service.emit_gated(handle.job_id, "gate_1", _T0 + timedelta(seconds=300))
        actions = await service.recover()
        assert [action.action for action in actions] == ["failed_unknown"]
        assert actions[0].detail == {"status": "gated", "spec_mismatch": False}
        failed = await service.get_job(handle.job_id)
        assert failed.status == "failed"
        assert failed.error is not None
        assert failed.error["code"] == "ExecutionUnknown"


# ---------------------------------------------------------------------------
# Close semantics + the App wiring readability
# ---------------------------------------------------------------------------


class _ParkedExecutor:
    """An executor that never returns: close() must cancel its run task."""

    async def drive(self, handle: JobHandle, *, resume: bool = False) -> None:
        await asyncio.Event().wait()


class TestServiceClose:
    async def test_close_cancels_spawned_run_tasks_and_is_safe_when_idle(self, tmp_path: Path) -> None:
        db_path = tmp_path / "parked.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        database = open_database(db_path, mode="create")
        store = JobsStore(database.engine, projector=JobsProjector())
        repos = JobsReadRepository(database.engine)
        service = JobService(
            store=store,
            repos=repos,
            executor=_ParkedExecutor(),
            ids=AscendingIds(lambda: _T0),
            clock=lambda: _T0,
        )
        try:
            handle = await service.submit("research", _PARAMS, "api")
            task = service.spawn(handle.job_id)
            await asyncio.sleep(0)  # let the task start parking
            assert not task.done()
            await service.close()
            assert task.cancelled()
        finally:
            await service.close()  # idempotent on an idle service
            await database.close()


class TestAppWiring:
    async def test_app_jobs_is_the_shared_composition(self, tmp_path: Path) -> None:
        db_path = tmp_path / "app.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        app = App(db_path, working_dir="C:/work")
        try:
            assert isinstance(app.jobs, JobService)
            handle = await app.jobs.submit("research", _PARAMS, "api")
            await app.jobs.run(handle.job_id)
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "done"
            assert record.result == {"executor": "fake"}
        finally:
            await app.close()
