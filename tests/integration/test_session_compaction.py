"""M6-compaction slice-two service wiring tests (CORE-CONTRACTS §2.4; T-C1-062).

The full wiring matrix over the REAL stack: alembic-migrated SQLite, the real
``SessionStore`` + ``SessionProjector``, the real ``ReadRepositories``, the
real ``VolatileBus``, and a real ``SessionService`` whose only injected faces
are the fake runner (a RunScheduler that settles its run immediately) and the
summary seam (the model call the service does not own). Everything else the
engine touches is the production path: the real settled read face (the
EventLog fold + ``repos.log_high``), the service's own per-session commit
lock, the shared id factory/clock, and the store's §3.2 CAS.

Per-ticket discipline (six wiring faces, each at least one test):

* 触发 + 39/40 边界 (§2.4 L128): 38 settled messages at the post-settlement
  trigger -> below_threshold (no event, no notify); the 40-message pass
  compacts exactly once with the engine's 32/8 boundary math.
* ContextBuilder 接线 (§2.4 L126): the folded effective view (summary +
  retained tail) flows through ``ContextBuilder.build`` unchanged -- the
  request the next run would send.
* bus notify (§3.2 L159 step 5 shape): only a committed ``py.context.compacted``
  notifies; below_threshold and the discarded pass are no-ops.
* 失败/取消/崩溃矩阵 (§2.4 L136/L132): 取消/崩溃双负例 -- a runner cancelled
  or crashed mid-flight settles nothing, so the post-settlement trigger
  never fires (cancel is not an error; the crash stays in the runner's own
  ``_schedule_errors`` channel); ① a raising summary seam -> zero
  replacement, zero events, the error recorded per session, the settled run
  untouched;
  ② a competing commit landing while the summary generates outside the lock
  -> the store's CAS discards the summary (no replacement, no notify, and
  the discard is an outcome, not a recorded failure);
  ③ crash faces: a mid-summary abort leaves the committed log byte-identical
  to the fold captured at summarize time (no partial state), and a
  post-commit replay through a FRESH fold (the recovery.py mirror)
  reproduces the effective view deterministically -- the mid-commit
  atomicity is the store's §3.2 all-or-nothing transaction (pinned by the
  store tests; the engine adds no second write).
* settled 读面: ``service.settled_history`` is the real fold read -- the
  materialized summary never counts (8 settled after a pass, not 9), so the
  next pass re-triggers on real growth.
* 存量兼容: a service without the summarizer seam never triggers.

Test-only private access note: ``service._compaction_errors`` /
``service._schedule_errors`` / ``service._tasks`` are read to pin the
failure-recording face and to await the scheduled task deterministically
(the task reference is read synchronously before the loop can start it);
``service._compaction_summarizer`` / ``service._compaction_engine`` are
rebound before the first trigger to late-bind seams that need the stack's
own store faces (the engine is lazily rebuilt over the new seam).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.bus import VolatileBus
from opencode.bus.eventlog import EventCursor, EventLog
from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.events import (
    PY_CONTEXT_COMPACTED,
    PY_RUN_FINISHED,
    EventDraft,
    RunFinishedData,
)
from opencode.contracts.llm import LLMTextPart, ModelRef, Usage
from opencode.session import SessionProjector
from opencode.session.compaction import (
    COMPACT_RETAINED_TAIL_MESSAGES,
    COMPACT_TRIGGER_MESSAGE_COUNT,
    SettledMessage,
    SummaryGenerator,
    SummaryResult,
)
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.service import AscendingIds, RunRef, SessionService
from opencode.session.state import SessionState, apply
from opencode.storage import Database, EventBatch, SessionStore, open_database
from opencode.storage.repos import NotFoundError, ReadRepositories

REPO_ROOT = Path(__file__).resolve().parents[2]

_SESSION_ID = "ses_00000000000000000000000000w1"
_T0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
_SNAPSHOT = ConfigSnapshot.create()
_SUMMARY_TEXT = "The session covered seeded setup rounds and two tool turns."
_TRIGGER = COMPACT_TRIGGER_MESSAGE_COUNT
_TAIL = COMPACT_RETAINED_TAIL_MESSAGES


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


def _migrated(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"
    return db_path


class _SeedIds:
    """Counter ids for store-side seeding (distinct from the service's factory)."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self) -> str:
        self._n += 1
        return f"{self._n:05d}"


def _finished_draft(session_id: str, run_id: str, event_id: str, at: datetime) -> EventDraft[RunFinishedData]:
    return EventDraft[RunFinishedData](
        id=event_id,
        type=PY_RUN_FINISHED,
        version=1,
        session_id=session_id,
        created_at=at,
        data=RunFinishedData(run_id=run_id, outcome="interrupted", error=None),
        metadata={"run_id": run_id},
    )


async def _seed_round(
    store: SessionStore, repos: ReadRepositories, ids: _SeedIds, prefix: str, *, text: str | None = None
) -> None:
    """One settled round through the REAL store: prompt.accepted + run.finished.

    The multi-accepted R1-baseline log shape (§0.2): a user message settles at
    ``py.prompt.accepted``, the idle row settles at ``py.run.finished``, and
    the active-run slot is released -- so two settled messages land per round
    and the service's own prompt gate stays open afterwards.
    """
    high = await repos.log_high(_SESSION_ID)
    ordinal = ids._n + 1
    run_id = f"run_{prefix}_{ids._next()}"
    user = UserMessage(
        id=f"msg_{prefix}_{ids._next()}",
        type="user",
        created_at=_T0,
        metadata={},
        text=text if text is not None else f"seed turn {ordinal}",
    )
    prompt = EventDraft[PromptAcceptedData](
        id=f"evt_{prefix}p{ids._next()}",
        type=PY_PROMPT_ACCEPTED,
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=PromptAcceptedData(request_id=f"req_{prefix}_{ids._next()}", run_id=run_id, message=user),
        metadata={"run_id": run_id},
    )
    finished = _finished_draft(_SESSION_ID, run_id, f"evt_{prefix}f{ids._next()}", _T0)
    await store.commit(_SESSION_ID, EventBatch(f"batch_{prefix}_{ids._next()}", (prompt, finished)), high)


def _created_draft() -> EventDraft[SessionCreatedData]:
    return EventDraft[SessionCreatedData](
        id="evt_created_00000",
        type=PY_SESSION_CREATED,
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=_SNAPSHOT,
            snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
        ),
    )


class _SettlingRunner:
    """RunScheduler fake: settle the run immediately, releasing the slot.

    ``run_finished(interrupted)`` is the legal minimal terminal from ACCEPTED
    (§2.1 L59): the fold accepts it with no step choreography, the idle
    message settles, and the next prompt is not SessionBusy. The trigger
    treats the runner's normal return as the settlement signal regardless of
    the terminal outcome (the T-C1-062 trigger-point ruling). The two fault
    modes pin the ruling's negative legs: ``cancel`` raises CancelledError
    before any commit (a close-time-style cancel mid-flight) and ``failure``
    raises before any commit (a crashed runner) -- neither settles, so
    neither may trigger.
    """

    def __init__(self, store: SessionStore, repos: ReadRepositories) -> None:
        self._store = store
        self._repos = repos
        self.failure: Exception | None = None
        self.cancel = False
        self.settled_runs: list[str] = []

    async def run(self, session_id: str, run_id: str) -> object:
        if self.cancel:
            raise asyncio.CancelledError
        if self.failure is not None:
            raise self.failure
        high = await self._repos.log_high(session_id)
        draft = _finished_draft(session_id, run_id, f"evt_runner_fin_{run_id}", _T0)
        await self._store.commit(session_id, EventBatch(f"batch_fin_{run_id}", (draft,)), high)
        self.settled_runs.append(run_id)
        return None

    def cancel_event(self, run_id: str) -> asyncio.Event:
        return asyncio.Event()


class _FakeSummarizer:
    """SummaryGenerator fake: the configured result, or a raise (matrix ①)."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[SettledMessage, ...]] = []

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        self.calls.append(tuple(covered))
        if self._error is not None:
            raise self._error
        return SummaryResult(
            text=_SUMMARY_TEXT,
            model=ModelRef(provider="maas", model="glm-4.6"),
            usage=Usage(input=2048, output=256),
        )


class _RacingSummarizer(_FakeSummarizer):
    """Matrix ②: a competing writer lands between capture and commit.

    The engine generates the summary OUTSIDE the lock (§2.4 L126); this fake
    commits one more settled round through the REAL store while the engine's
    captured expected_seq is already stale, so the engine's commit must fail
    the store's CAS and discard the summary (§3.2 L203: the caller cannot
    skip events with a newer value).
    """

    def __init__(self, store: SessionStore, repos: ReadRepositories) -> None:
        super().__init__()
        self._store = store
        self._repos = repos
        self._ids = _SeedIds()

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        await _seed_round(self._store, self._repos, self._ids, "race")
        return await super().summarize(covered)


class _CrashCaptureSummarizer(_FakeSummarizer):
    """Matrix ③(a): fold the committed log at summarize time, then abort.

    The summary call runs outside the session lock (§2.4 L126), so the fold
    captured here IS the pre-commit truth; comparing a later fresh fold
    against it proves the mid-summary crash wrote nothing partial."""

    def __init__(self, engine: AsyncEngine, repos: ReadRepositories) -> None:
        super().__init__(error=RuntimeError("simulated crash mid-summary"))
        self._engine = engine
        self._repos = repos
        self.state_at_crash: SessionState | None = None

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        self.state_at_crash = await _fold(self._engine, self._repos)
        return await super().summarize(covered)


class _Stack:
    """The real wiring over one migrated engine (the composition-root shape)."""

    def __init__(self, database: Database, *, summarizer: SummaryGenerator | None = None) -> None:
        engine: AsyncEngine = database.engine
        self.store = SessionStore(engine, projector=SessionProjector())
        self.repos = ReadRepositories(engine)
        self.bus = VolatileBus()
        self.runner = _SettlingRunner(self.store, self.repos)
        self.summarizer: SummaryGenerator | None = summarizer
        self.service = SessionService(
            store=self.store,
            repos=self.repos,
            bus=self.bus,
            working_dir="C:/work",
            clock=lambda: _T0,
            ids=AscendingIds(lambda: _T0),
            engine=engine,
            runner=self.runner,
            compaction_summarizer=summarizer,
        )

    async def seed(self, rounds: int) -> None:
        ids = _SeedIds()
        await self.store.commit(_SESSION_ID, EventBatch("batch_seed_00000", (_created_draft(),)), 0)
        for ordinal in range(1, rounds + 1):
            await _seed_round(self.store, self.repos, ids, "seed", text=f"seed turn {ordinal}")

    async def prompted(self, *, text: str, request_id: str) -> tuple[RunRef, asyncio.Task[None]]:
        """prompt + await the scheduled task through the compaction trigger."""
        ref = await self.service.prompt(_SESSION_ID, text, request_id)
        task = self.service._tasks[ref.run_id][1]
        await task
        return ref, task


def _builder() -> ContextBuilder:
    return ContextBuilder(
        RuntimeSettings(
            model=ModelRef(provider="maas", model="glm-4.6"),
            context_limit=100_000,
            output_reserve=4096,
            timeout_s=30.0,
        )
    )


async def _fold(engine: AsyncEngine, repos: ReadRepositories) -> SessionState:
    """Fold the whole committed log (the recovery.py full-fold mirror)."""
    log = EventLog(engine, repos=repos)
    state = SessionState()
    after: EventCursor | None = None
    while True:
        page = await log.read(_SESSION_ID, after=after)
        for event in page.items:
            state = apply(state, event)
        if not page.has_more:
            return state
        after = page.next_cursor


async def _compacted_event(engine: AsyncEngine) -> tuple[str, dict[str, object]] | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa_text(
                    "SELECT id, data FROM events WHERE session_id = :sid AND type = 'py.context.compacted' ORDER BY seq"
                ),
                {"sid": _SESSION_ID},
            )
        ).first()
    if row is None:
        return None
    data = row.data
    return row.id, json.loads(data) if isinstance(data, str) else data


async def _applied_seq(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    sa_text("SELECT applied_seq FROM sessions WHERE session_id = :sid"), {"sid": _SESSION_ID}
                )
            ).scalar_one()
        )


async def _log_high(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    sa_text("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = :sid"), {"sid": _SESSION_ID}
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# 触发 + 39/40 边界 + notify + the effective view (the green matrix)
# ---------------------------------------------------------------------------


async def test_post_settlement_trigger_compacts_at_threshold_and_notifies(tmp_path: Path) -> None:
    """19 seeded rounds (38 settled) + the run's own user+idle (40 total):
    the trigger fires, the engine's 32/8 boundary math lands in one event,
    the bus carries it, and the ContextBuilder face assembles summary+tail."""
    db_path = _migrated(tmp_path, "compaction_trigger.db")
    summarizer = _FakeSummarizer()
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=summarizer)
        await stack.seed(19)

        async with stack.bus.subscribe(_SESSION_ID, {PY_CONTEXT_COMPACTED}) as events:
            _ref, _ = await stack.prompted(text="the final prompt", request_id="req_final")
            notified = await asyncio.wait_for(anext(events), timeout=1.0)

        assert notified.type == PY_CONTEXT_COMPACTED
        compacted = await _compacted_event(database.engine)
        assert compacted is not None
        payload = compacted[1]
        assert payload["covered_seq"] == 33
        assert payload["retained_from"] == 34
        assert payload["message_count"] == _TRIGGER - _TAIL
        assert payload["summary_text"] == _SUMMARY_TEXT
        assert len(summarizer.calls) == 1
        assert len(summarizer.calls[0]) == _TRIGGER - _TAIL

        # A=H survived the trigger's own commit; the read model kept every
        # original row (§2.4 L133/L134: never rewritten).
        assert await _applied_seq(database.engine) == await _log_high(database.engine)
        page = await stack.service.messages(_SESSION_ID, limit=200)
        assert len(page.items) == _TRIGGER

        # The effective view: exactly the summary + the 8-message tail.
        state = await _fold(database.engine, stack.repos)
        assert len(state.messages) == 1 + _TAIL
        assert state.messages[0].type == "user"
        assert state.messages[0].text == _SUMMARY_TEXT
        assert state.message_seqs == (None, *range(34, 34 + _TAIL))
        assert state.compaction is not None
        assert state.compaction.retained_from == 34

        # The next request the runner would build: the summary first, then the
        # retained tail verbatim. The assembled request carries the summary
        # plus the four retained USER messages -- the tail's idle rows
        # contribute nothing (§2.2 L243, the builder's standing rule over the
        # effective view).
        request = _builder().build(request_id="req_after_compact", history=state.messages)
        assert [message.role for message in request.messages] == ["user"] * 5
        first_part = request.messages[0].content[0]
        assert isinstance(first_part, LLMTextPart)
        texts = [part.text for message in request.messages for part in message.content if isinstance(part, LLMTextPart)]
        assert texts == [_SUMMARY_TEXT, "seed turn 17", "seed turn 18", "seed turn 19", "the final prompt"]

        # The real settled read face: the summary never counts as history.
        view = await stack.service.settled_history(_SESSION_ID)
        assert len(view.messages) == _TAIL
        assert [item.seq for item in view.messages] == list(range(34, 34 + _TAIL))
        assert view.log_high == await _log_high(database.engine)


async def test_below_threshold_does_not_compact_then_the_boundary_cross_fires(tmp_path: Path) -> None:
    """18 seeded rounds (36) + one prompted run (38) -> no pass, no notify;
    the second prompted run (40) crosses the threshold exactly (39/40 边界)."""
    db_path = _migrated(tmp_path, "compaction_boundary.db")
    summarizer = _FakeSummarizer()
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=summarizer)
        await stack.seed(18)

        async with stack.bus.subscribe(_SESSION_ID, {PY_CONTEXT_COMPACTED}) as events:
            await stack.prompted(text="turn a", request_id="req_a")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)
        assert await _compacted_event(database.engine) is None
        state = await _fold(database.engine, stack.repos)
        assert len(state.messages) == 38
        assert state.compaction is None
        assert not summarizer.calls

        async with stack.bus.subscribe(_SESSION_ID, {PY_CONTEXT_COMPACTED}) as events:
            await stack.prompted(text="turn b", request_id="req_b")
            notified = await asyncio.wait_for(anext(events), timeout=1.0)
        assert notified.type == PY_CONTEXT_COMPACTED
        assert len(summarizer.calls) == 1


async def test_service_without_the_summarizer_never_triggers(tmp_path: Path) -> None:
    """存量兼容: the pre-M6 composition (no seam injected) is a strict no-op."""
    db_path = _migrated(tmp_path, "compaction_dormant.db")
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database)  # no summarizer
        await stack.seed(19)
        await stack.prompted(text="the final prompt", request_id="req_final")
        assert await _compacted_event(database.engine) is None
        assert stack.service._compaction_errors == {}


# ---------------------------------------------------------------------------
# 失败/取消/崩溃矩阵 (§2.4 L136/L132)
# ---------------------------------------------------------------------------


async def test_cancelled_runner_never_triggers(tmp_path: Path) -> None:
    """Trigger negative leg (取消): a runner cancelled mid-flight settles
    nothing, so the post-settlement trigger never fires -- no event, no
    summary call, and no error recorded anywhere (cancel is a signal, not a
    failure; §2.4 L136: 失败/取消/崩溃不替换原上下文)."""
    db_path = _migrated(tmp_path, "compaction_cancel.db")
    summarizer = _FakeSummarizer()
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=summarizer)
        await stack.seed(19)
        stack.runner.cancel = True

        ref = await stack.service.prompt(_SESSION_ID, "the final prompt", "req_final")
        task = stack.service._tasks[ref.run_id][1]
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _compacted_event(database.engine) is None
        assert not summarizer.calls
        assert stack.service._compaction_errors == {}
        assert stack.service._schedule_errors == {}
        state = await _fold(database.engine, stack.repos)
        assert state.compaction is None


async def test_crashed_runner_never_triggers_and_keeps_the_error_channel(tmp_path: Path) -> None:
    """Trigger negative leg (崩溃): a crashed runner settles nothing -> no
    trigger, no summary call, and the crash stays in the runner's own
    ``_schedule_errors`` channel (never the compaction error face -- the run
    never reached settlement)."""
    db_path = _migrated(tmp_path, "compaction_crashed_runner.db")
    summarizer = _FakeSummarizer()
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=summarizer)
        await stack.seed(19)
        stack.runner.failure = RuntimeError("the runner exploded mid-flight")

        ref = await stack.service.prompt(_SESSION_ID, "the final prompt", "req_final")
        task = stack.service._tasks[ref.run_id][1]
        await task  # the crash is swallowed and recorded by _drive_run

        assert await _compacted_event(database.engine) is None
        assert not summarizer.calls
        assert stack.service._compaction_errors == {}
        recorded = stack.service._schedule_errors
        assert isinstance(recorded.get(ref.run_id), RuntimeError)
        state = await _fold(database.engine, stack.repos)
        assert state.compaction is None


async def test_summary_failure_replaces_nothing_and_keeps_the_run_settled(tmp_path: Path) -> None:
    """Matrix ①: the injected seam raises -> the engine propagates, the
    trigger records the failure per session, and the original context is
    untouched (no event, no notify, full history, settled run intact)."""
    db_path = _migrated(tmp_path, "compaction_summary_failure.db")
    summarizer = _FakeSummarizer(error=RuntimeError("the model call exploded"))
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=summarizer)
        await stack.seed(19)

        async with stack.bus.subscribe(_SESSION_ID, {PY_CONTEXT_COMPACTED}) as events:
            ref, _ = await stack.prompted(text="the final prompt", request_id="req_final")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)

        assert await _compacted_event(database.engine) is None
        recorded = stack.service._compaction_errors
        assert isinstance(recorded.get(_SESSION_ID), RuntimeError)
        assert stack.service._schedule_errors == {}
        state = await _fold(database.engine, stack.repos)
        assert len(state.messages) == 40  # zero replacement: the full history
        assert state.compaction is None
        assert stack.runner.settled_runs == [ref.run_id]  # the run settled anyway


async def test_competing_commit_discards_the_summary_via_the_real_cas(tmp_path: Path) -> None:
    """Matrix ② (边界变化丢弃, wiring level): a round commits while the
    summary generates outside the lock -> the stale expected_seq fails the
    store's CAS -> discarded: no event, no notify, full history stands, and
    the discard is an outcome, not a recorded failure."""
    db_path = _migrated(tmp_path, "compaction_discard.db")
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database)  # dormant first; the racing seam late-binds
        racing = _RacingSummarizer(stack.store, stack.repos)
        stack.service._compaction_summarizer = racing
        stack.service._compaction_engine = None  # rebuilt lazily over the seam
        await stack.seed(19)

        async with stack.bus.subscribe(_SESSION_ID, {PY_CONTEXT_COMPACTED}) as events:
            await stack.prompted(text="the final prompt", request_id="req_final")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)

        assert await _compacted_event(database.engine) is None
        state = await _fold(database.engine, stack.repos)
        assert len(state.messages) == 40 + 2  # the racing round stands, the summary does not
        assert state.compaction is None
        assert len(racing.calls) == 1  # the model call happened; its usage rode the discarded outcome
        assert stack.service._compaction_errors == {}


async def test_crash_faces_leave_no_partial_replacement(tmp_path: Path) -> None:
    """Matrix ③: (a) aborting before the commit -- a fresh full fold (the
    recovery.py replay shape) equals the fold captured at summarize time (no
    partial state, no half-written summary); (b) after a successful commit, a
    FRESH fold reproduces the effective view deterministically -- crash
    consistency rides the store's §3.2 all-or-nothing transaction (the
    engine's single commit adds no window)."""
    db_path = _migrated(tmp_path, "compaction_crash.db")
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database)  # dormant first; the crashing seam late-binds
        crashing = _CrashCaptureSummarizer(database.engine, stack.repos)
        stack.service._compaction_summarizer = crashing
        stack.service._compaction_engine = None
        await stack.seed(19)

        await stack.prompted(text="the final prompt", request_id="req_final")  # crashes mid-summary

        assert crashing.state_at_crash is not None
        replayed = await _fold(database.engine, stack.repos)
        assert replayed == crashing.state_at_crash  # the committed log is byte-identical
        assert await _compacted_event(database.engine) is None

        # (b) the committed face: a fresh fold replays the replacement.
        stack.service._compaction_summarizer = _FakeSummarizer()
        stack.service._compaction_engine = None  # rebuilt lazily over the new seam
        await stack.prompted(text="the next prompt", request_id="req_next")
        first = await _fold(database.engine, stack.repos)
        assert first.compaction is not None
        again = await _fold(database.engine, stack.repos)
        assert again == first  # the replay is the crash-recovery truth


# ---------------------------------------------------------------------------
# negative: the read face over a session without a log
# ---------------------------------------------------------------------------


async def test_settled_history_on_a_missing_session_keeps_the_notfound_face(tmp_path: Path) -> None:
    """The read face over an uncreated session raises NotFoundError -- the
    same fail-visible face as the service's other read methods (the engine
    never sees it through the trigger: a settled run implies the session
    row exists)."""
    db_path = _migrated(tmp_path, "compaction_empty_read.db")
    async with open_database(db_path, mode="create") as database:
        stack = _Stack(database, summarizer=_FakeSummarizer())
        with pytest.raises(NotFoundError):
            await stack.service.settled_history("ses_00000000000000000000000000n0")
