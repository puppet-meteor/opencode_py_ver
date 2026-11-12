"""Integration tests for the jobs-domain writer + read repository (T-C1-076, P3-S1).

Covers the PHASE3 M-7 "two writer domains" commit path over the same events
table on ``ses_job_*`` stream keys: the minimal §3.2 sequence (H/A
invariants, idempotent whole-batch replay, expected_seq CAS, consecutive seq,
insert + same-transaction projection, A=H+n verify), the fold guards as
commit-time ``StoreValidationError`` rejections, stored-row corruption as
``StorageCorruptError`` (no isolation face -- the disclosed deviation), the
§3.3-style rebuild, and the 0002 -> 0003 migration upgrade path with old
data preserved.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts import EventDraft
from opencode.contracts.events import (
    PY_JOB_CREATED,
    PY_JOB_FINISHED,
    PY_JOB_GATED,
    PY_JOB_STARTED,
    JobBudget,
    JobCreatedData,
    JobFinishedData,
    JobGatedData,
    JobLineage,
    JobSpecBoundData,
    JobStalledData,
    JobStartedData,
    JobTrace,
    RunCancelRequestedData,
)
from opencode.contracts.messages import JsonObject
from opencode.orchestrator.jobs import JobsProjector, compute_params_hash
from opencode.storage import open_database
from opencode.storage.jobs_store import JobsReadRepository, JobsStore, job_stream_key
from opencode.storage.store import EventBatch
from opencode.storage.txn import (
    ConflictError,
    IdempotencyConflictError,
    StorageCorruptError,
    StoreValidationError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

JOB_COLUMNS = (
    "job_id",
    "stream_id",
    "pipeline_id",
    "params",
    "params_hash",
    "session_anchor",
    "status",
    "depth",
    "budget",
    "checkpoint_ref",
    "retry_count",
    "attempt_id",
    "stalled_reason",
    "lineage",
    "trace_id",
    "result",
    "error",
    "applied_seq",
    "created_epoch_ms",
    "updated_epoch_ms",
)

_NOW = datetime(2026, 11, 12, 8, 0, 0, tzinfo=UTC)
_COUNTER = iter(range(1, 10_000))


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


def _upgrade(db_path: Path, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", revision],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout:{result.stdout}\nstderr:{result.stderr}"


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


@pytest.fixture()
async def jobs_stack(tmp_path: Path) -> Any:
    """One migrated database + the jobs writer/read/projector stack (closed on exit)."""
    db_path = tmp_path / "jobs.db"
    _require_success(_upgrade_head(db_path))
    database = open_database(db_path, mode="create")
    store = JobsStore(database.engine, projector=JobsProjector())
    repos = JobsReadRepository(database.engine)
    try:
        yield store, repos
    finally:
        await database.close()


def _draft(stream_key: str, event_type: str, data: Any) -> EventDraft[Any]:
    return EventDraft(
        id=f"evt_{next(_COUNTER):026d}",
        type=event_type,
        version=1,
        session_id=stream_key,
        created_at=_NOW,
        data=data,
        metadata={},
    )


def _submit_drafts(job_id: str) -> tuple[EventDraft[Any], EventDraft[Any]]:
    stream_key = job_stream_key(job_id)
    created = _draft(
        stream_key,
        PY_JOB_CREATED,
        JobCreatedData(
            job_id=job_id,
            pipeline_id="research",
            depth=0,
            lineage=JobLineage(origin="api"),
            budget=JobBudget(max_tool_calls=5),
            trace=JobTrace(trace_id=f"trc_{job_id}", span_id="spn_1", parent_id=None, depth=0),
        ),
    )
    params: JsonObject = {"query": "hi"}
    spec_bound = _draft(
        stream_key,
        "py.job.spec_bound",
        JobSpecBoundData(
            job_id=job_id,
            params=params,
            params_hash=compute_params_hash(params),
            session_anchor="api",
            trace=JobTrace(trace_id=f"trc_{job_id}", span_id="spn_2", parent_id=None, depth=0),
        ),
    )
    return created, spec_bound


class TestCommitSequence:
    async def test_submit_order_batch_projects_the_created_row(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, repos = jobs_stack
        created, spec_bound = _submit_drafts("job_a")
        batch = EventBatch(id="bat_a", events=(created, spec_bound))
        events = await store.commit(job_stream_key("job_a"), batch, 0)
        assert [event.seq for event in events] == [1, 2]
        assert [event.type for event in events] == [PY_JOB_CREATED, "py.job.spec_bound"]

        record = await repos.job_by_id("job_a")
        assert record is not None
        assert record.status == "created"
        assert record.pipeline_id == "research"
        assert record.params == {"query": "hi"}
        assert record.params_hash == compute_params_hash({"query": "hi"})
        assert record.session_anchor == "api"
        assert record.applied_seq == 2
        assert await repos.log_high(job_stream_key("job_a")) == 2

    async def test_expected_seq_cas_rejects_a_stale_watermark(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, _repos = jobs_stack
        created, spec_bound = _submit_drafts("job_b")
        batch = EventBatch(id="bat_b", events=(created, spec_bound))
        await store.commit(job_stream_key("job_b"), batch, 0)
        started = _draft(
            job_stream_key("job_b"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_b", attempt_id="att_1", trace=JobTrace(trace_id="trc_b", span_id="s", depth=0)),
        )
        stale = EventBatch(id="bat_b2", events=(started,))
        with pytest.raises(ConflictError):
            await store.commit(job_stream_key("job_b"), stale, 0)  # H is 2 now, not 0

    async def test_whole_batch_retry_returns_the_originals_without_advancing(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, repos = jobs_stack
        created, spec_bound = _submit_drafts("job_c")
        batch = EventBatch(id="bat_c", events=(created, spec_bound))
        first = await store.commit(job_stream_key("job_c"), batch, 0)
        # Same batch, stale expected_seq: the idempotent replay path answers.
        second = await store.commit(job_stream_key("job_c"), batch, 99)
        assert [event.id for event in second] == [event.id for event in first]
        assert [event.seq for event in second] == [1, 2]
        assert await repos.log_high(job_stream_key("job_c")) == 2

    async def test_same_batch_id_with_diverged_content_is_idempotency_conflict(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, _repos = jobs_stack
        created, spec_bound = _submit_drafts("job_d")
        await store.commit(job_stream_key("job_d"), EventBatch(id="bat_d", events=(created, spec_bound)), 0)
        other = _draft(
            job_stream_key("job_d"),
            "py.job.spec_bound",
            JobSpecBoundData(
                job_id="job_d",
                params={"query": "DIFFERENT"},
                params_hash=compute_params_hash({"query": "DIFFERENT"}),
                session_anchor="api",
                trace=JobTrace(trace_id="trc_d", span_id="s2", depth=0),
            ),
        )
        with pytest.raises(IdempotencyConflictError):
            await store.commit(job_stream_key("job_d"), EventBatch(id="bat_d", events=(created, other)), 2)

    async def test_event_id_committed_in_another_stream_is_idempotency_conflict(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, _repos = jobs_stack
        created, spec_bound = _submit_drafts("job_e")
        await store.commit(job_stream_key("job_e"), EventBatch(id="bat_e", events=(created, spec_bound)), 0)
        # Reuse the created draft's id under a different stream key.
        stream_two = job_stream_key("job_e2")
        replay_created = created.model_copy(update={"session_id": stream_two})
        batch = EventBatch(id="bat_e2", events=(replay_created,))
        with pytest.raises(IdempotencyConflictError):
            await store.commit(stream_two, batch, 0)


class TestFoldGuards:
    """The fold is the single transition source: violations reject and roll back."""

    async def _submitted(self, store: JobsStore, job_id: str) -> None:
        created, spec_bound = _submit_drafts(job_id)
        await store.commit(job_stream_key(job_id), EventBatch(id=f"bat_{job_id}", events=(created, spec_bound)), 0)

    async def test_finished_done_rejects_from_created(self, jobs_stack: tuple[JobsStore, JobsReadRepository]) -> None:
        store, _repos = jobs_stack
        await self._submitted(store, "job_f")
        finished = _draft(
            job_stream_key("job_f"),
            PY_JOB_FINISHED,
            JobFinishedData(job_id="job_f", outcome="done", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StoreValidationError, match="illegal job transition"):
            await store.commit(job_stream_key("job_f"), EventBatch(id="bat_f1", events=(finished,)), 2)

    async def test_stalled_rejects_from_created(self, jobs_stack: tuple[JobsStore, JobsReadRepository]) -> None:
        store, _repos = jobs_stack
        await self._submitted(store, "job_g")
        stalled = _draft(
            job_stream_key("job_g"),
            "py.job.stalled",
            JobStalledData(
                job_id="job_g",
                stalled_reason="watchdog",
                attempt_id="att_1",
                trace=JobTrace(trace_id="t", span_id="s", depth=0),
            ),
        )
        with pytest.raises(StoreValidationError):
            await store.commit(job_stream_key("job_g"), EventBatch(id="bat_g", events=(stalled,)), 2)

    async def test_a_non_job_event_type_rejects_on_a_job_stream(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        """The fold owns the ses_job_* domain: a non-job event type is not a job event."""
        store, _repos = jobs_stack
        await self._submitted(store, "job_h")
        run_cancel = EventDraft(
            id="evt_run_leak",
            type="py.run.cancel_requested",
            version=1,
            session_id=job_stream_key("job_h"),
            created_at=_NOW,
            data=RunCancelRequestedData(run_id="run_1", reason="user"),
            metadata={"run_id": "run_1"},
        )
        with pytest.raises(StoreValidationError):
            await store.commit(job_stream_key("job_h"), EventBatch(id="bat_h", events=(run_cancel,)), 2)

    async def test_terminal_absorbing_rejects_any_later_event(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository]
    ) -> None:
        store, _repos = jobs_stack
        await self._submitted(store, "job_i")
        started = _draft(
            job_stream_key("job_i"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_i", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        await store.commit(job_stream_key("job_i"), EventBatch(id="bat_i1", events=(started,)), 2)
        finished = _draft(
            job_stream_key("job_i"),
            PY_JOB_FINISHED,
            JobFinishedData(job_id="job_i", outcome="done", trace=JobTrace(trace_id="t", span_id="s2", depth=0)),
        )
        await store.commit(job_stream_key("job_i"), EventBatch(id="bat_i2", events=(finished,)), 3)
        gated = _draft(
            job_stream_key("job_i"),
            PY_JOB_GATED,
            JobGatedData(
                job_id="job_i",
                gate_id="gate_1",
                expires_at=_NOW + timedelta(seconds=300),
                trace=JobTrace(trace_id="t", span_id="s3", depth=0),
            ),
        )
        with pytest.raises(StoreValidationError):
            await store.commit(job_stream_key("job_i"), EventBatch(id="bat_i3", events=(gated,)), 4)


class TestCorruptionDetections:
    """Stored-row/log invariant violations fail closed (no isolation face: the disclosed deviation)."""

    async def test_applied_seq_mismatch_fails_closed(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_j")
        await store.commit(job_stream_key("job_j"), EventBatch(id="bat_j", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE jobs SET applied_seq = 1 WHERE job_id = 'job_j'")
        started = _draft(
            job_stream_key("job_j"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_j", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StorageCorruptError):
            await store.commit(job_stream_key("job_j"), EventBatch(id="bat_j2", events=(started,)), 2)

    async def test_missing_jobs_row_with_log_fails_closed(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_k")
        await store.commit(job_stream_key("job_k"), EventBatch(id="bat_k", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM jobs WHERE job_id = 'job_k'")
        started = _draft(
            job_stream_key("job_k"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_k", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StorageCorruptError):
            await store.commit(job_stream_key("job_k"), EventBatch(id="bat_k2", events=(started,)), 2)

    async def test_seq_gap_fails_closed(self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_l")
        await store.commit(job_stream_key("job_l"), EventBatch(id="bat_l", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM events WHERE seq = 2 AND session_id = ?", (job_stream_key("job_l"),))
        started = _draft(
            job_stream_key("job_l"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_l", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StorageCorruptError):
            await store.commit(job_stream_key("job_l"), EventBatch(id="bat_l2", events=(started,)), 1)


class TestRebuild:
    async def test_rebuild_restores_a_tampered_row_from_the_log(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_m")
        await store.commit(job_stream_key("job_m"), EventBatch(id="bat_m", events=(created, spec_bound)), 0)
        started = _draft(
            job_stream_key("job_m"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_m", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        await store.commit(job_stream_key("job_m"), EventBatch(id="bat_m2", events=(started,)), 2)
        # Tamper the read model: rebuild must restore it from the log.
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE jobs SET status = 'done', params = NULL WHERE job_id = 'job_m'")
        await store.rebuild(job_stream_key("job_m"))
        record = await repos.job_by_id("job_m")
        assert record is not None
        assert record.status == "running"
        assert record.params == {"query": "hi"}
        assert record.applied_seq == 3

    async def test_rebuild_is_idempotent(self, jobs_stack: tuple[JobsStore, JobsReadRepository]) -> None:
        store, repos = jobs_stack
        created, spec_bound = _submit_drafts("job_n")
        await store.commit(job_stream_key("job_n"), EventBatch(id="bat_n", events=(created, spec_bound)), 0)
        await store.rebuild(job_stream_key("job_n"))
        await store.rebuild(job_stream_key("job_n"))
        record = await repos.job_by_id("job_n")
        assert record is not None
        assert record.applied_seq == 2


class TestMigrationUpgradePath:
    def test_upgrade_from_0002_preserves_old_data_and_adds_the_jobs_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "upgrade.db"
        _require_success(_upgrade(db_path, "0002"))
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
                "batch_id, batch_ordinal) "
                "VALUES ('evt_old1', 'ses_old', 1, 'py.session.created', 1, 1770000000000, '{}', '{}', 'bat_old', 0)"
            )
            connection.execute(
                "INSERT INTO runs (run_id, session_id, request_id, status) "
                "VALUES ('run_1', 'ses_old', 'req_1', 'accepted')"
            )
        _require_success(_upgrade(db_path, "head"))
        tables = {row[0] for row in _fetch_all(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "jobs" in tables
        columns = {row[1] for row in _fetch_all(db_path, "PRAGMA table_info(jobs)")}
        assert columns == set(JOB_COLUMNS)
        assert _fetch_all(db_path, "SELECT COUNT(*) FROM events WHERE id = 'evt_old1'") == [(1,)]
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE run_id = 'run_1'") == [("accepted",)]

    def test_downgrade_refuses_to_drop_the_jobs_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "down.db"
        _require_success(_upgrade_head(db_path))
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "downgrade", "0002"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode != 0
        assert "restore-from-verified-backup" in result.stderr
        tables = {row[0] for row in _fetch_all(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "jobs" in tables
