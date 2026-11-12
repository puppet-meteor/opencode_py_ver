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

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.contracts import DurableEvent, EventDraft
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
from opencode.storage.jobs_store import (
    REASON_JOBS_DUPLICATE_SEQ,
    REASON_JOBS_ILLEGAL_SEQ,
    REASON_JOBS_PROJECTION_INVARIANT,
    REASON_JOBS_REPLAY_ILLEGAL_TRANSITION,
    REASON_JOBS_SEQ_GAP,
    JobsReadRepository,
    JobsStore,
    job_stream_key,
)
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


class TestDeepDefenseBranches:
    """The S1-leftover fail-closed branches, directly driven (T-B-091 S-2; T-C1-078 ⑧).

    The S1 suite covered the read-model-mismatch / missing-row / gap shapes at
    commit time; these drive the remaining stored-log and rebuild defenses:
    an illegal seq below 1 and a duplicate seq (a constraint-less damaged
    schema -- unreachable under the live UNIQUE(session_id, seq)) at commit
    time, the A=H+n projection invariant (a projector that folds nothing
    must fail the commit and roll it back), and the rebuild-time trio -- a
    replayed illegal transition, an undecodable log row, and a seq-run
    violation.
    """

    async def test_stored_seq_below_one_fails_closed_at_commit(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_p1")
        await store.commit(job_stream_key("job_p1"), EventBatch(id="bat_p1", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE events SET seq = 0 WHERE seq = 1 AND session_id = ?", (job_stream_key("job_p1"),)
            )
        started = _draft(
            job_stream_key("job_p1"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_p1", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StorageCorruptError, match=REASON_JOBS_ILLEGAL_SEQ):
            await store.commit(job_stream_key("job_p1"), EventBatch(id="bat_p1b", events=(started,)), 2)

    async def test_stored_duplicate_seq_fails_closed_at_commit(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        """The duplicate-seq classification: unreachable in a healthy schema
        (UNIQUE(session_id, seq)), so the probe rebuilds the events table
        without constraints -- the damaged-schema shape the defense exists for."""
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_p2")
        await store.commit(job_stream_key("job_p2"), EventBatch(id="bat_p2", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute("ALTER TABLE events RENAME TO events_orig")
            connection.execute("CREATE TABLE events AS SELECT * FROM events_orig")
            connection.execute("DROP TABLE events_orig")
            connection.execute(
                "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
                "batch_id, batch_ordinal) "
                "VALUES ('evt_dup_seq', ?, 1, 'py.job.started', 1, 1770000000000, '{}', '{}', 'bat_p2x', 0)",
                (job_stream_key("job_p2"),),
            )
        started = _draft(
            job_stream_key("job_p2"),
            PY_JOB_STARTED,
            JobStartedData(job_id="job_p2", attempt_id="att_1", trace=JobTrace(trace_id="t", span_id="s", depth=0)),
        )
        with pytest.raises(StorageCorruptError, match=REASON_JOBS_DUPLICATE_SEQ) as raised:
            await store.commit(job_stream_key("job_p2"), EventBatch(id="bat_p2b", events=(started,)), 2)
        assert raised.value.core.details["reason"] == REASON_JOBS_DUPLICATE_SEQ

    async def test_noop_projection_fails_the_commit_and_rolls_it_back(self, tmp_path: Path) -> None:
        """The A=H+n invariant: a projector that folds nothing leaves the read
        model behind the log, so the commit fails and nothing half-applies."""

        class _NoopProjector:
            """A projector that applies nothing (the invariant probe)."""

            async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
                return None

        db_path = tmp_path / "noop.db"
        _require_success(_upgrade_head(db_path))
        database = open_database(db_path, mode="create")
        try:
            store = JobsStore(database.engine, projector=_NoopProjector())
            created, spec_bound = _submit_drafts("job_p3")
            with pytest.raises(StorageCorruptError, match="projection did not advance") as raised:
                await store.commit(job_stream_key("job_p3"), EventBatch(id="bat_p3", events=(created, spec_bound)), 0)
            assert raised.value.core.details["reason"] == REASON_JOBS_PROJECTION_INVARIANT
            with sqlite3.connect(db_path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE session_id = ?", (job_stream_key("job_p3"),)
                ).fetchone()[0]
            assert count == 0
        finally:
            await database.close()

    async def test_rebuild_replay_of_an_illegal_transition_fails_closed(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_p4")
        await store.commit(job_stream_key("job_p4"), EventBatch(id="bat_p4", events=(created, spec_bound)), 0)
        # Surgery: append two extra started rows with valid payloads, so the
        # replay folds created -> spec_bound -> started -> started.
        started_data = json.dumps(
            JobStartedData(
                job_id="job_p4", attempt_id="att_x", trace=JobTrace(trace_id="trc_x", span_id="spn_x", depth=0)
            ).model_dump(mode="json")
        )
        with sqlite3.connect(db_path) as connection:
            connection.executemany(
                "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
                "batch_id, batch_ordinal) VALUES (?, ?, ?, ?, 1, 1770000000000, ?, '{}', ?, 0)",
                [
                    ("evt_twice_a", job_stream_key("job_p4"), 3, PY_JOB_STARTED, started_data, "bat_p4x"),
                    ("evt_twice_b", job_stream_key("job_p4"), 4, PY_JOB_STARTED, started_data, "bat_p4y"),
                ],
            )
        with pytest.raises(StorageCorruptError, match=REASON_JOBS_REPLAY_ILLEGAL_TRANSITION) as raised:
            await store.rebuild(job_stream_key("job_p4"))
        assert raised.value.core.details["reason"] == REASON_JOBS_REPLAY_ILLEGAL_TRANSITION

    async def test_rebuild_of_an_undecodable_log_row_fails_closed(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_p5")
        await store.commit(job_stream_key("job_p5"), EventBatch(id="bat_p5", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE events SET type = 'py.nope.nope' WHERE seq = 1 AND session_id = ?",
                (job_stream_key("job_p5"),),
            )
        with pytest.raises(StorageCorruptError, match="jobs log row failed to decode"):
            await store.rebuild(job_stream_key("job_p5"))

    async def test_rebuild_of_a_gapped_seq_run_fails_closed(
        self, jobs_stack: tuple[JobsStore, JobsReadRepository], tmp_path: Path
    ) -> None:
        store, _repos = jobs_stack
        db_path = tmp_path / "jobs.db"
        created, spec_bound = _submit_drafts("job_p6")
        await store.commit(job_stream_key("job_p6"), EventBatch(id="bat_p6", events=(created, spec_bound)), 0)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM events WHERE seq = 1 AND session_id = ?", (job_stream_key("job_p6"),))
        with pytest.raises(StorageCorruptError, match="jobs log seq run violation") as raised:
            await store.rebuild(job_stream_key("job_p6"))
        assert raised.value.core.details["reason"] == REASON_JOBS_SEQ_GAP


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
