"""M6-compaction P2-S8 slice-one contract tests (CORE-CONTRACTS §2.4 + §3.1 L176).

T-C1-060 acceptance faces, each test mapped to its contract sentence:

* 阈值 (§2.4 L128) -- compaction triggers on an explicit threshold: 39 settled
  messages stay below ``COMPACT_TRIGGER_MESSAGE_COUNT`` (no summary call, no
  commit); 40 cross it.
* 执行序 (§2.4 L126) -- the sentence end to end: the capture happens under the
  injected lock, the summary runs OUTSIDE it, and the commit re-enters the
  short lock with the captured ``expected_seq`` (CAS basis); on success exactly
  one durable ``py.context.compacted`` event with the seven §2.4 L116-124
  fields is appended through the REAL ``SessionStore``.
* 边界变化丢弃 (§2.4 L126/L132) -- history advances between capture and commit:
  the stale ``expected_seq`` fails the store's CAS (ConflictError), the summary
  is discarded (zero commits, zero replacement, zero partial writes) and the
  extra model call's usage still rides the outcome.
* retained 边界 (§2.4 L118-124) -- covered_seq/retained_from/message_count are
  computed from the captured settled history (covered = settled minus the
  ``COMPACT_RETAINED_TAIL_MESSAGES`` tail).
* 原历史零修改 (§2.4 L126/L133) -- the engine appends exactly one event and the
  original rows are byte-identical before/after.
* 负例 (§3.1 L176) -- the wire face: out-of-range ints, empty strings, missing
  fields, extra fields and bool masquerades fail registry decode; durability
  routing and version probes fail closed.
* 失败保守接缝 (§2.4 L132) -- a raising summary generator propagates: nothing
  swallowed, nothing committed (the full failure/cancel/crash matrix is the
  slice-two face; this pins the no-half-commit seam).

Real-store tests reuse the test_skill_activation/test_store_commit harness
(alembic-migrated SQLite + a minimal §3.2 projector fake); the fast faces use
contract-faithful fakes over the same Protocol seams.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from opencode.contracts.events import (
    PY_CONTEXT_COMPACTED,
    PY_SKILL_ACTIVATED,
    ContextCompactedData,
    DurabilityMismatchError,
    DurableEvent,
    EventDraft,
    SkillActivatedData,
    UnknownEventVersionError,
    canonical_json,
    default_event_registry,
)
from opencode.contracts.llm import ModelRef, Usage
from opencode.contracts.messages import AssistantMessage, AssistantTextPart, SessionMessage, UserMessage
from opencode.session.compaction import (
    COMPACT_RETAINED_TAIL_MESSAGES,
    COMPACT_TRIGGER_MESSAGE_COUNT,
    CompactionEngine,
    SettledHistoryView,
    SettledMessage,
    SummaryResult,
)
from opencode.storage import ConflictError, EventBatch, SessionStore, open_database

REPO_ROOT = Path(__file__).resolve().parents[2]

SESSION_ID = "ses_00000000000000000000000000c1"
_T0 = datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC)
_MODEL = ModelRef(provider="maas", model="glm-4.6")
_USAGE = Usage(input=2048, output=256)
_SUMMARY_TEXT = "The conversation covered project setup, a notes.md read, and two tool-call turns."
_SUMMARY_DIGEST = hashlib.sha256(_SUMMARY_TEXT.encode("utf-8")).hexdigest()

_VALID_DATA: dict[str, Any] = {
    "covered_seq": 63,
    "retained_from": 64,
    "summary_text": "Covered: project setup and two tool turns.",
    "summary_hash": "a" * 64,
    "model": {"provider": "maas", "model": "glm-4.6"},
    "usage": {"input": 4096, "output": 512},
    "message_count": 32,
}


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


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout: {result.stdout}\nstderr: {result.stderr}"


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


class _GateProjector:
    """Injected §3.2 projector fake: minimal sessions row + applied_seq advance
    (the RecordingProjector pattern; the real compaction projection is slice two)."""

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        await connection.execute(
            sa_text(
                "INSERT INTO sessions (session_id, status, applied_seq) VALUES (:sid, 'active', :applied) "
                "ON CONFLICT(session_id) DO UPDATE SET applied_seq = excluded.applied_seq"
            ),
            {"sid": event.session_id, "applied": event.seq},
        )


def _seed_draft(n: int) -> EventDraft[SkillActivatedData]:
    """One cheap legal durable event used only to advance the real log high.

    The engine reads history through the injected source, not the database;
    the seeded rows exist so the captured ``expected_seq`` matches the real
    watermark and the zero-modification assertion has real bytes to compare.
    """
    return EventDraft[SkillActivatedData](
        id=f"evt_seed_{n:04d}",
        type=PY_SKILL_ACTIVATED,
        version=1,
        session_id=SESSION_ID,
        created_at=_T0,
        data=SkillActivatedData(
            skill_id=f"seed-{n}",
            source="test",
            path=f"seed-{n}/SKILL.md",
            hash="0" * 64,
            size=1,
            body="x",
        ),
    )


class _RecordingLock(asyncio.Lock):
    """Lock that records the current hold depth so the fakes can assert the
    engine's lock discipline (capture in-lock, summary out-of-lock, commit in-lock)."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0

    async def __aenter__(self) -> None:
        await super().__aenter__()
        self.depth += 1

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.depth -= 1
        await super().__aexit__(exc_type, exc, tb)


@dataclass
class _SeqIds:
    """Deterministic id face for the engine (the AscendingIds structural shape)."""

    counter: int = 0

    def event_id(self) -> str:
        self.counter += 1
        return f"evt_engine_{self.counter:04d}"

    def batch_id(self) -> str:
        self.counter += 1
        return f"batch_engine_{self.counter:04d}"


def _settled_history(count: int, *, first_seq: int = 2) -> tuple[SettledMessage, ...]:
    """``count`` settled messages (user/assistant pairs), settling seqs ``first_seq``..."""
    messages: list[SettledMessage] = []
    for index in range(count):
        ordinal = index + 1
        message: SessionMessage
        if ordinal % 2:
            message = UserMessage(
                id=f"msg_{ordinal:04d}", type="user", created_at=_T0, metadata={}, text=f"turn {ordinal}"
            )
        else:
            message = AssistantMessage(
                id=f"msg_{ordinal:04d}",
                type="assistant",
                created_at=_T0,
                metadata={},
                model=_MODEL,
                parts=[AssistantTextPart(type="text", part_id=f"part_{ordinal:04d}", text=f"reply {ordinal}")],
                status="completed",
            )
        messages.append(SettledMessage(message=message, seq=first_seq + index))
    return tuple(messages)


class _FakeHistory:
    """SettledHistorySource fake: serves the prepared view, records the lock depth."""

    def __init__(self, view: SettledHistoryView, lock: _RecordingLock) -> None:
        self._view = view
        self._lock = lock
        self.calls: list[str] = []
        self.depths: list[int] = []

    async def settled_history(self, session_id: str) -> SettledHistoryView:
        self.calls.append(session_id)
        self.depths.append(self._lock.depth)
        return self._view


class _FakeSummarizer:
    """SummaryGenerator fake: returns the configured result (or raises), records the lock depth."""

    def __init__(self, lock: _RecordingLock, *, error: Exception | None = None) -> None:
        self._lock = lock
        self._error = error
        self.calls: list[tuple[SettledMessage, ...]] = []
        self.depths: list[int] = []

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        self.calls.append(tuple(covered))
        self.depths.append(self._lock.depth)
        if self._error is not None:
            raise self._error
        return SummaryResult(text=_SUMMARY_TEXT, model=_MODEL, usage=_USAGE)


class _RacingSummarizer:
    """Simulates the concurrent writer of §2.4 L126: while the engine is outside
    the lock generating the summary, another commit advances the log high, so the
    engine's stale ``expected_seq`` must fail the store's CAS."""

    def __init__(self, store: SessionStore, lock: _RecordingLock) -> None:
        self._store = store
        self._lock = lock

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        assert self._lock.depth == 0, "the race happens strictly outside the engine locks"
        await self._store.commit(SESSION_ID, EventBatch("batch_racer", (_seed_draft(99),)), 41)
        return SummaryResult(text=_SUMMARY_TEXT, model=_MODEL, usage=_USAGE)


class _FakeStore:
    """Contract-faithful CompactionStore fake: records commits and models the
    §3.2 expected_seq equality face (mismatch -> ConflictError); the real-store
    tests below cover the actual TransactionWriter CAS."""

    def __init__(self, lock: _RecordingLock, *, log_high: int) -> None:
        self._lock = lock
        self.log_high = log_high
        self.calls: list[tuple[str, str, tuple[EventDraft[Any], ...], int]] = []
        self.depths: list[int] = []

    async def commit(self, session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]:
        self.calls.append((session_id, batch.id, tuple(batch.events), expected_seq))
        self.depths.append(self._lock.depth)
        if expected_seq != self.log_high:
            raise ConflictError(
                f"expected_seq {expected_seq} does not match the current high watermark {self.log_high}",
                details={"expected_seq": expected_seq, "high_watermark": self.log_high},
            )
        committed: list[DurableEvent[Any]] = []
        for draft in batch.events:
            self.log_high += 1
            envelope: Any = DurableEvent
            committed.append(envelope[type(draft.data)](**draft.model_dump(), seq=self.log_high))
        return committed


def _view(count: int) -> SettledHistoryView:
    """``count`` settled messages with settling seqs 2..count+1 plus the created event -> high."""
    return SettledHistoryView(messages=_settled_history(count), log_high=count + 1)


def _fast_engine(
    store: _FakeStore, history: _FakeHistory, summarizer: _FakeSummarizer | _RacingSummarizer, lock: _RecordingLock
) -> CompactionEngine:
    return CompactionEngine(
        store=store,
        history=history,
        summarizer=summarizer,
        ids=_SeqIds(),
        clock=lambda: _T0,
        lock=lambda session_id: lock,
    )


@asynccontextmanager
async def _migrated_store(db_path: Path) -> AsyncIterator[tuple[SessionStore, AsyncEngine]]:
    """One alembic-migrated database + real SessionStore over the projector fake."""
    async with open_database(db_path, mode="create") as database:
        yield SessionStore(database.engine, projector=_GateProjector()), database.engine


# -- 阈值: §2.4 L128 --------------------------------------------------------


class TestThreshold:
    """compaction 由明确阈值触发 (L128); 钉值=本票承载 (40 settled 消息)。"""

    def test_pinned_constants_carry_the_ticket_ruling(self) -> None:
        assert COMPACT_TRIGGER_MESSAGE_COUNT == 40
        assert COMPACT_RETAINED_TAIL_MESSAGES == 8

    async def test_thirty_nine_settled_messages_stay_below_the_trigger(self) -> None:
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=40)
        history = _FakeHistory(_view(39), lock)
        summarizer = _FakeSummarizer(lock)
        engine = _fast_engine(store, history, summarizer, lock)

        outcome = await engine.compact(SESSION_ID)

        assert outcome.status == "below_threshold"
        assert outcome.capture is None
        assert outcome.event is None
        assert outcome.usage is None
        assert summarizer.calls == []
        assert store.calls == []
        assert lock.depth == 0

    async def test_forty_settled_messages_cross_the_trigger(self) -> None:
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=41)
        history = _FakeHistory(_view(40), lock)
        summarizer = _FakeSummarizer(lock)
        engine = _fast_engine(store, history, summarizer, lock)

        outcome = await engine.compact(SESSION_ID)

        assert outcome.status == "compacted"
        assert outcome.event is not None and outcome.event.seq == 42
        # exactly one commit, carrying the captured watermark as its CAS basis
        assert len(store.calls) == 1
        session_id, batch_id, drafts, expected_seq = store.calls[0]
        assert (session_id, expected_seq) == (SESSION_ID, 41)
        assert batch_id and drafts[0].type == PY_CONTEXT_COMPACTED
        # the summary covered exactly the first 32 messages (seqs 2..33)
        assert len(summarizer.calls) == 1
        assert [item.seq for item in summarizer.calls[0]] == list(range(2, 34))


# -- retained 边界: §2.4 L118-124 --------------------------------------------


class TestRetainedBoundary:
    """covered_seq/retained_from/message_count come from the captured settled history."""

    async def test_boundaries_split_at_the_retained_tail(self) -> None:
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=41)
        history = _FakeHistory(_view(40), lock)
        summarizer = _FakeSummarizer(lock)
        engine = _fast_engine(store, history, summarizer, lock)

        outcome = await engine.compact(SESSION_ID)

        assert outcome.event is not None
        data = outcome.event.data
        assert isinstance(data, ContextCompactedData)
        assert (data.covered_seq, data.retained_from, data.message_count) == (33, 34, 32)
        assert data.summary_text == _SUMMARY_TEXT
        assert data.summary_hash == _SUMMARY_DIGEST
        assert data.model == _MODEL
        assert data.usage == _USAGE
        capture = outcome.capture
        assert capture is not None
        assert capture.covered_seq == 33
        assert capture.retained_from == 34
        assert capture.message_count == 32
        assert capture.expected_seq == 41
        assert len(capture.history_hash) == 64

    async def test_invalid_history_view_fails_closed_before_any_commit(self) -> None:
        """Provider-seam defense: non-ascending settling seqs break the boundary
        math, so the engine rejects the view before touching the store."""
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=41)
        messages = list(_settled_history(40))
        messages[10] = SettledMessage(message=messages[10].message, seq=messages[9].seq)
        history = _FakeHistory(SettledHistoryView(messages=tuple(messages), log_high=41), lock)
        engine = _fast_engine(store, history, _FakeSummarizer(lock), lock)

        with pytest.raises(ValueError, match="strictly ascending"):
            await engine.compact(SESSION_ID)
        assert store.calls == []

    async def test_session_id_argument_is_validated_before_any_lock(self) -> None:
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=0)
        engine = _fast_engine(store, _FakeHistory(_view(0), lock), _FakeSummarizer(lock), lock)

        with pytest.raises(ValueError, match="session_id"):
            await engine.compact("not_a_session_id")
        assert store.calls == []


# -- 执行序 + 锁纪律 + 原历史零修改: §2.4 L126 ---------------------------------


class TestExecutionOrder:
    """The L126 sentence over the real SessionStore: capture in-lock -> summary
    out-of-lock -> short commit lock with the captured expected_seq."""

    async def test_capture_and_commit_hold_the_lock_summary_runs_outside(self) -> None:
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=41)
        history = _FakeHistory(_view(40), lock)
        summarizer = _FakeSummarizer(lock)
        engine = _fast_engine(store, history, summarizer, lock)

        await engine.compact(SESSION_ID)

        assert history.depths == [1]
        assert summarizer.depths == [0]
        assert store.depths == [1]
        assert lock.depth == 0

    async def test_summary_failure_propagates_without_commit(self) -> None:
        """§2.4 L132 conservative seam: the exception is never swallowed and
        nothing is committed (the full failure/cancel/crash matrix is slice two)."""
        lock = _RecordingLock()
        store = _FakeStore(lock, log_high=41)
        history = _FakeHistory(_view(40), lock)
        summarizer = _FakeSummarizer(lock, error=RuntimeError("model endpoint exploded"))
        engine = _fast_engine(store, history, summarizer, lock)

        with pytest.raises(RuntimeError, match="model endpoint exploded"):
            await engine.compact(SESSION_ID)
        assert store.calls == []
        assert lock.depth == 0

    async def test_success_commits_the_seven_field_event_over_the_real_store(self, tmp_path: Path) -> None:
        db_path = tmp_path / "compaction.db"
        _require_success(_upgrade_head(db_path))
        async with _migrated_store(db_path) as (store, _engine_unused):
            seed = EventBatch("batch_seed", tuple(_seed_draft(n) for n in range(1, 42)))
            await store.commit(SESSION_ID, seed, 0)
            rows_before = _fetch_all(
                db_path,
                "SELECT id, seq, type, data FROM events WHERE session_id = ? ORDER BY seq",
                (SESSION_ID,),
            )
            assert len(rows_before) == 41

            lock = _RecordingLock()
            engine = CompactionEngine(
                store=store,
                history=_FakeHistory(_view(40), lock),
                summarizer=_FakeSummarizer(lock),
                ids=_SeqIds(),
                clock=lambda: _T0,
                lock=lambda session_id: lock,
            )

            outcome = await engine.compact(SESSION_ID)

            assert outcome.status == "compacted"
            event = outcome.event
            assert event is not None
            assert event.type == PY_CONTEXT_COMPACTED
            assert event.seq == 42
            assert event.session_id == SESSION_ID
            assert event.metadata == {}
            data = event.data
            assert isinstance(data, ContextCompactedData)
            assert data.model_dump() == {
                "covered_seq": 33,
                "retained_from": 34,
                "summary_text": _SUMMARY_TEXT,
                "summary_hash": _SUMMARY_DIGEST,
                "model": {"provider": "maas", "model": "glm-4.6"},
                "usage": {"input": 2048, "output": 256, "reasoning": None, "cache_read": None, "cache_write": None},
                "message_count": 32,
            }
            assert outcome.usage == _USAGE

            # 原历史零修改 (§2.4 L126/L133): the original rows are byte-identical;
            # the engine's only write is the single appended compaction event.
            rows_after = _fetch_all(
                db_path,
                "SELECT id, seq, type, data FROM events WHERE session_id = ? ORDER BY seq",
                (SESSION_ID,),
            )
            assert rows_after[:41] == rows_before
            assert len(rows_after) == 42
            assert rows_after[41] == (
                event.id,
                42,
                "py.context.compacted",
                canonical_json(data.model_dump(mode="json")),
            )

    async def test_boundary_moved_after_capture_discards_the_summary(self, tmp_path: Path) -> None:
        """§2.4 L126: history advanced after the capture -> the stale expected_seq
        fails the store CAS -> the summary is discarded: zero py.context.compacted
        rows, zero replacement, and the extra call's usage still rides the outcome."""
        db_path = tmp_path / "compaction.db"
        _require_success(_upgrade_head(db_path))
        async with _migrated_store(db_path) as (store, _engine_unused):
            seed = EventBatch("batch_seed", tuple(_seed_draft(n) for n in range(1, 42)))
            await store.commit(SESSION_ID, seed, 0)

            lock = _RecordingLock()
            engine = CompactionEngine(
                store=store,
                history=_FakeHistory(_view(40), lock),
                summarizer=_RacingSummarizer(store, lock),
                ids=_SeqIds(),
                clock=lambda: _T0,
                lock=lambda session_id: lock,
            )

            outcome = await engine.compact(SESSION_ID)

            assert outcome.status == "discarded"
            assert outcome.event is None
            assert outcome.usage == _USAGE  # 额外模型调用也记录 usage (§2.4 L132)
            capture = outcome.capture
            assert capture is not None
            assert (capture.covered_seq, capture.retained_from, capture.message_count) == (33, 34, 32)
            assert capture.expected_seq == 41

            rows = _fetch_all(
                db_path,
                "SELECT id, seq, type FROM events WHERE session_id = ? ORDER BY seq",
                (SESSION_ID,),
            )
            assert len(rows) == 42  # 41 seed + the racing commit; the summary left no trace
            assert all(row[2] != "py.context.compacted" for row in rows)
            assert rows[41] == ("evt_seed_0099", 42, "py.skill.activated")


# -- 负例: §3.1 L176 行 ------------------------------------------------------


class TestWireNegatives:
    """The py.context.compacted wire face fails closed at registry decode."""

    @staticmethod
    def _envelope(
        *, data_override: dict[str, Any] | None = None, data_remove: str | None = None, version: int = 1
    ) -> dict[str, Any]:
        data = dict(_VALID_DATA)
        if data_override is not None:
            data.update(data_override)
        if data_remove is not None:
            data.pop(data_remove)
        return {
            "id": "evt_0000000000000000000000000060",
            "type": PY_CONTEXT_COMPACTED,
            "version": version,
            "session_id": SESSION_ID,
            "created_at": "2026-11-10T08:00:00Z",
            "data": data,
            "metadata": {},
            "seq": 72,
        }

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("covered_seq", -1),
            ("retained_from", -2),
            ("message_count", -3),
            ("covered_seq", True),
            ("retained_from", True),
            ("message_count", True),
            ("summary_text", ""),
            ("summary_hash", ""),
            ("model", None),
            ("usage", []),
        ],
    )
    def test_out_of_range_and_empty_values_rejected(self, field_name: str, value: Any) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(self._envelope(data_override={field_name: value}))

    @pytest.mark.parametrize(
        "field_name",
        ["covered_seq", "retained_from", "summary_text", "summary_hash", "model", "usage", "message_count"],
    )
    def test_missing_fields_rejected(self, field_name: str) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(self._envelope(data_remove=field_name))

    def test_unknown_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(self._envelope(data_override={"unexpected_extension_key": True}))

    def test_registry_resolves_the_durable_definition(self) -> None:
        definition = default_event_registry().resolve_durable(PY_CONTEXT_COMPACTED, 1)
        assert definition.data_model is ContextCompactedData
        assert definition.durability == "durable"

    def test_unknown_version_rejected(self) -> None:
        with pytest.raises(UnknownEventVersionError):
            default_event_registry().decode_durable(self._envelope(version=2))

    def test_durability_mismatch_rejected(self) -> None:
        with pytest.raises(DurabilityMismatchError):
            default_event_registry().decode_ephemeral(self._envelope())
