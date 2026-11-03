"""Integration tests for the S6 EventLog read/follow + read-only repos (R1-S6).

Real SQLite + ``alembic upgrade head`` + ``SessionStore.commit`` (with the real
S4 projector) populate the log; these tests pin CORE-CONTRACTS §4 L222-238 and
the r1-slices R1-S6 acceptance list over ``opencode.bus.eventlog.EventLog`` and
``opencode.storage.repos.ReadRepositories``:

- read: pagination boundaries (empty page keeps the input cursor, next_cursor
  = last item's seq, has_more), limit clamping 1..1000, NotFound for unbuilt
  sessions, the three InvalidCursor classes (+ invalid encodings), and
  H = MAX(events.seq) -- never sessions.applied_seq;
- bad pages (T-A-002 final ruling): whole-page StorageCorrupt(retryable=
  false) with the sanitized five-field details, no half page, no next_cursor
  advance (same-cursor retry -> same error), read-failure cursor = the input
  after (None -> 0), and unknown version/type FAIL CLOSED (the TS
  readAfter skip-and-advance behavior is deliberately not ported);
- read_only_diagnostic gate: typed read/follow fail closed on ANY cursor (no
  bypassing the bad point with a larger after) while the projected read model
  stays queryable (§3.2.1 "读取仍可用");
- follow: wakeup registered before the H capture, catch-up + exactly one
  Synced at H, strictly increasing seq, live wake via the bus (payload never
  replayed -- ephemeral notifications wake but do not yield), the <= 1s poll
  fallback after lost notifications (acceptance B, latter half: read still
  returns every committed event with the bus disconnected), cursor/session
  validation at enter, and the wakeup unregistered on plain exit, on failed
  enter and on bad-page termination;
- repos: messages paginate by projected seq order (not message-id
  lexicographic), after_message_id cursor + clamped limit, empty page keeps
  the cursor, unknown cursor -> InvalidCursor, unbuilt session -> NotFound,
  missing read model with a surviving log -> StorageCorrupt (never faked
  empty), undecodable message rows fail closed with the bad row located;
- queries and reads make no external calls (socket/subprocess banned under
  monkeypatch, the S4 assertion pattern);
- structure: ``opencode.storage.repos`` and the ``opencode.storage`` package
  import nothing bus-flavored -- the bus-freedom ``repos.py`` claims is
  pinned here, while ``opencode.bus.eventlog`` stays out of scope by design
  (it is the DB+bus-coupled durable read face).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from opencode.bus import VolatileBus
from opencode.bus.eventlog import EventCursor, EventLog, LogItem, Synced
from opencode.contracts import (
    ConfigSnapshot,
    CoreErrorCode,
    DurableEvent,
    EphemeralEvent,
    EventDraft,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.session import SessionProjector
from opencode.storage import (
    EventBatch,
    InvalidCursorError,
    MessagePage,
    NotFoundError,
    ReadRepositories,
    SessionRecord,
    SessionStore,
    StorageCorruptError,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_CREATED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


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


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _session_created_draft(session_id: str, event_id: str) -> EventDraft[SessionCreatedData]:
    snapshot = ConfigSnapshot.create()
    return EventDraft[SessionCreatedData](
        id=event_id,
        type="py.session.created",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ),
    )


def _prompt_accepted_draft(
    session_id: str,
    event_id: str,
    *,
    run_id: str = "run_1",
    text: str = "hello",
    message_id: str = "msg_0",
) -> EventDraft[PromptAcceptedData]:
    return EventDraft[PromptAcceptedData](
        id=event_id,
        type="py.prompt.accepted",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id="req_1",
            run_id=run_id,
            message=UserMessage(id=message_id, type="user", created_at=_CREATED_AT, metadata={}, text=text),
        ),
        metadata={"run_id": run_id},
    )


async def _populate(store: SessionStore, session_id: str, *, prompts: int, run_id: str = "run_1") -> int:
    """Create the session, then commit ``prompts`` prompt events; return H.

    Events: seq 1 = py.session.created (``evt_{sid}_c``), seq 2.. = prompts
    (``evt_{sid}_p{i}``, message ids ``msg_{i-1}`` starting at seq 2).
    """
    created = _session_created_draft(session_id, f"evt_{session_id}_c")
    await store.commit(session_id, EventBatch(f"batch_{session_id}_c", (created,)), 0)
    for index in range(prompts):
        draft = _prompt_accepted_draft(
            session_id,
            f"evt_{session_id}_p{index}",
            run_id=run_id,
            text=f"text-{index}",
            message_id=f"msg_{index}",
        )
        await store.commit(session_id, EventBatch(f"batch_{session_id}_p{index}", (draft,)), 1 + index)
    return 1 + prompts


def _user_texts(page: MessagePage) -> list[str]:
    """User-text projection over a page; every item must be a UserMessage
    (T-C1-023b #95: static narrowing over the union face -- runtime-true pin)."""
    texts: list[str] = []
    for message in page.items:
        assert isinstance(message, UserMessage)
        texts.append(message.text)
    return texts


class _TextDelta(BaseModel):
    """Test-local ephemeral payload (wake-signal test; contracts §3.1 pattern)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    delta: str = ""


def _ephemeral(session_id: str, event_id: str) -> EphemeralEvent[_TextDelta]:
    """A live ephemeral envelope (no seq, no version; never in the log)."""
    return EphemeralEvent[_TextDelta](
        id=event_id,
        type="py.text.delta",
        session_id=session_id,
        created_at=_CREATED_AT,
        data=_TextDelta(message_id="msg_0", part_id="part_0", delta="x"),
        metadata={"run_id": "run_1"},
    )


# -- read: pagination, limits, NotFound, InvalidCursor, H ----------------------


async def test_read_paginates_with_cursor_and_limit_boundaries(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine)

        first = await log.read("ses_a", limit=2)
        assert [event.seq for event in first.items] == [1, 2]
        assert first.next_cursor == EventCursor(session_id="ses_a", seq=2)
        assert first.has_more is True

        second = await log.read("ses_a", after=first.next_cursor, limit=2)
        assert [event.seq for event in second.items] == [3, 4]
        assert second.next_cursor == EventCursor(session_id="ses_a", seq=4)
        assert second.has_more is False

        # §4 L222: an empty page keeps the input cursor.
        empty = await log.read("ses_a", after=second.next_cursor, limit=2)
        assert empty.items == []
        assert empty.next_cursor == EventCursor(session_id="ses_a", seq=4)
        assert empty.has_more is False

        # None equals this session's seq-0 cursor; the default limit is 100.
        default = await log.read("ses_a")
        assert [event.seq for event in default.items] == [1, 2, 3, 4]
        assert default.next_cursor == EventCursor(session_id="ses_a", seq=4)
        assert default.has_more is False


async def test_read_clamps_limit_into_bounds(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine)

        for out_of_range in (0, -7):
            below = await log.read("ses_a", limit=out_of_range)
            assert [event.seq for event in below.items] == [1]
            assert below.has_more is True
        above = await log.read("ses_a", limit=10_000)
        assert [event.seq for event in above.items] == [1, 2, 3, 4]
        assert above.has_more is False

        for bad in (True, 2.5, "3"):
            with pytest.raises(ValueError, match="limit"):
                await log.read("ses_a", limit=bad)  # type: ignore[arg-type]


async def test_read_and_follow_report_not_found_for_unbuilt_session(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=0) == 1
        log = EventLog(database.engine, bus=bus)

        with pytest.raises(NotFoundError) as read_error:
            await log.read("ses_missing")
        assert read_error.value.core.code == CoreErrorCode.NOT_FOUND
        assert read_error.value.core.retryable is False

        with pytest.raises(NotFoundError):
            async with log.follow("ses_missing"):
                pytest.fail("the follow context must not enter an unbuilt session")
        # The failed enter unregistered its wakeup (no __aexit__ runs).
        assert bus.subscription_count == 0


async def test_read_invalid_cursor_rejections(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine)

        # ① A cursor of another session (§4: 游标不可跨 session 使用).
        with pytest.raises(InvalidCursorError) as mismatch:
            await log.read("ses_a", after=EventCursor(session_id="ses_b", seq=1))
        assert mismatch.value.core.code == CoreErrorCode.INVALID_CURSOR

        # ② A negative seq.
        with pytest.raises(InvalidCursorError) as negative:
            await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=-1))
        assert negative.value.core.code == CoreErrorCode.INVALID_CURSOR

        # ③ A seq beyond the current high watermark: explicit, never an empty wait.
        with pytest.raises(InvalidCursorError) as beyond:
            await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=5))
        assert beyond.value.core.code == CoreErrorCode.INVALID_CURSOR
        assert beyond.value.core.details["high_watermark"] == 4

        # seq == H is valid; the empty tail page keeps it.
        tail = await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=4))
        assert tail.items == []
        assert tail.next_cursor == EventCursor(session_id="ses_a", seq=4)

        # Invalid encodings: non-integer/boolean seq, missing fields.
        for fake in (
            SimpleNamespace(session_id="ses_a", seq="2"),
            SimpleNamespace(session_id="ses_a", seq=True),
            SimpleNamespace(session_id="ses_a"),
        ):
            with pytest.raises(InvalidCursorError):
                await log.read("ses_a", after=fake)  # type: ignore[arg-type]


async def test_read_high_watermark_is_log_max_not_applied_seq(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

    # Hand-inflate the projection watermark far beyond the log: H stays 4.
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE sessions SET applied_seq = 10 WHERE session_id = 'ses_a'")

    async with open_database(db_path, mode="create") as database:
        log = EventLog(database.engine)
        with pytest.raises(InvalidCursorError) as beyond:
            await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=5))
        assert beyond.value.core.details["high_watermark"] == 4
        tail = await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=4))
        assert tail.items == []
        assert tail.next_cursor == EventCursor(session_id="ses_a", seq=4)


# -- read: bad pages fail closed (T-A-002) -------------------------------------


async def test_read_bad_page_fails_closed_as_a_whole(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

    # seq 3's payload stops being valid JSON (the literal N-2① recipe).
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE events SET data = '{not json' WHERE session_id = 'ses_a' AND seq = 3")

    async with open_database(db_path, mode="create") as database:
        log = EventLog(database.engine)
        after = EventCursor(session_id="ses_a", seq=2)
        with pytest.raises(StorageCorruptError) as corrupt:
            await log.read("ses_a", after=after, limit=10)
        error = corrupt.value
        assert error.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.core.retryable is False
        # The five sanitized detail fields (§4 L227); no raw row content.
        assert error.core.details == {
            "reason": "stored_event_decode_failed",
            "session_id": "ses_a",
            "first_bad_seq": 3,
            "first_bad_event_id": "evt_ses_a_p1",
            "last_confirmed_cursor": {"session_id": "ses_a", "seq": 2},
        }
        assert "{not json" not in json.dumps(error.core.details)

        # Same cursor retry -> the same error: next_cursor never advanced.
        with pytest.raises(StorageCorruptError) as retry:
            await log.read("ses_a", after=after, limit=10)
        assert retry.value.core.details == error.core.details

        # A failing read from the beginning reports the seq-0 input cursor.
        with pytest.raises(StorageCorruptError) as from_start:
            await log.read("ses_a", limit=10)
        assert from_start.value.core.details["last_confirmed_cursor"] == {"session_id": "ses_a", "seq": 0}

        # While the session is not quarantined, the healthy suffix stays
        # readable (§4: pages already returned remain valid; the S8 read path
        # owns wiring read-path detections into isolation).
        suffix = await log.read("ses_a", after=EventCursor(session_id="ses_a", seq=3), limit=10)
        assert [event.seq for event in suffix.items] == [4]


async def test_read_unknown_version_or_type_fails_closed_instead_of_skipping(tmp_path: Path) -> None:
    """TS readAfter skips unknown types and advances the raw seq (L809-827);
    the T-A-002 final ruling fails the page closed instead."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        assert await _populate(store, "ses_b", prompts=3, run_id="run_ses_b") == 4

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE events SET version = 99 WHERE session_id = 'ses_a' AND seq = 2")
        connection.execute("UPDATE events SET type = 'py.future.event' WHERE session_id = 'ses_b' AND seq = 2")

    async with open_database(db_path, mode="create") as database:
        log = EventLog(database.engine)

        with pytest.raises(StorageCorruptError) as version_error:
            await log.read("ses_a", limit=10)
        assert version_error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert version_error.value.core.details["reason"] == "stored_event_decode_failed"
        assert version_error.value.core.details["first_bad_seq"] == 2

        with pytest.raises(StorageCorruptError) as type_error:
            await log.read("ses_b", limit=10)
        assert type_error.value.core.details["reason"] == "stored_event_decode_failed"
        assert type_error.value.core.details["first_bad_seq"] == 2


async def test_read_only_diagnostic_session_fails_closed_on_any_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

    # Simulate §3.2.1 isolation (status + sanitized diagnostic) plus the bad
    # row that would have triggered it: the larger-after bypass must fail.
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE events SET data = '{not json' WHERE session_id = 'ses_a' AND seq = 3")
        connection.execute(
            "UPDATE sessions SET status = 'read_only_diagnostic', diagnostic = ? WHERE session_id = 'ses_a'",
            ('{"reason": "stored_event_decode_failed", "applied_seq": 4}',),
        )

    async with open_database(db_path, mode="create") as database:
        repos = ReadRepositories(database.engine)
        log = EventLog(database.engine, repos=repos)

        for after in (None, EventCursor(session_id="ses_a", seq=2), EventCursor(session_id="ses_a", seq=3)):
            with pytest.raises(StorageCorruptError) as gate:
                await log.read("ses_a", after=after, limit=10)
            assert gate.value.core.code == CoreErrorCode.STORAGE_CORRUPT
            assert gate.value.core.retryable is False
            assert gate.value.core.details["reason"] == "session_read_only_diagnostic"
            expected_seq = 0 if after is None else after.seq
            assert gate.value.core.details["last_confirmed_cursor"] == {"session_id": "ses_a", "seq": expected_seq}

        with pytest.raises(StorageCorruptError) as follow_gate:
            async with log.follow("ses_a", after=EventCursor(session_id="ses_a", seq=3)):
                pytest.fail("the follow context must not enter a quarantined session")
        assert follow_gate.value.core.details["reason"] == "session_read_only_diagnostic"

        # §3.2.1 "读取仍可用": the projected read model stays queryable.
        record = await repos.session("ses_a")
        assert record == SessionRecord(
            session_id="ses_a",
            status="read_only_diagnostic",
            applied_seq=4,
            active_run_id="run_1",
            config_snapshot={"permission_rules": [], "schema_version": 1},
            snapshot_hash=compute_snapshot_hash(ConfigSnapshot.create()),
            diagnostic={"reason": "stored_event_decode_failed", "applied_seq": 4},
        )
        assert await repos.session("ses_missing") is None
        page = await repos.messages("ses_a", limit=10)
        assert [message.id for message in page.items] == ["msg_0", "msg_1", "msg_2"]


# -- follow ---------------------------------------------------------------------


async def test_follow_catches_up_emits_one_synced_and_streams_strictly_increasing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine, bus=bus, poll_interval=1.0)

        async with log.follow("ses_a") as items:
            received: list[LogItem] = []
            for _ in range(5):  # catch-up is ready without waiting: 4 events + the marker
                received.append(await anext(items))
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2, 3, 4]
            # Exactly one Synced marker, at the captured high watermark H.
            assert received[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=4))
            assert len([item for item in received if isinstance(item, Synced)]) == 1

            # Live: the commit wakes the stream (the bus is only a signal).
            committed = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_live",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_live", text="live", message_id="msg_live"),),
                ),
                4,
            )
            bus.notify("ses_a", committed)
            woken = await asyncio.wait_for(anext(items), timeout=0.5)
            assert woken == committed[0]

            # ④ Strictly increasing seq across the whole connection.
            seqs = [item.seq for item in received if isinstance(item, DurableEvent)] + [committed[0].seq]
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == len(seqs)

            # Nothing else without new commits (and never a second Synced).
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(items), timeout=0.15)

        # Leaving the context unregistered the wakeup.
        assert bus.subscription_count == 0


async def test_follow_poll_fallback_backfills_lost_notifications(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        # No bus injected at all: the <= 1s poll fallback is the only wake.
        log = EventLog(database.engine, poll_interval=0.05)

        async with log.follow("ses_a") as items:
            for _ in range(5):
                await anext(items)  # catch-up + Synced
            # A commit nobody notifies about still arrives via the poll.
            committed = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_lost",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_lost", text="lost", message_id="msg_lost"),),
                ),
                4,
            )
            woken = await asyncio.wait_for(anext(items), timeout=1.0)
            assert woken == committed[0]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(items), timeout=0.2)


async def test_read_backfills_everything_after_the_bus_disconnects(tmp_path: Path) -> None:
    """Acceptance B, latter half: with the bus subscriber gone, read still
    returns every committed event (the log, not the notification, is the
    source of truth)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

        committed = await store.commit(
            "ses_a",
            EventBatch(
                "batch_quiet",
                (_prompt_accepted_draft("ses_a", "evt_ses_a_quiet", text="quiet", message_id="msg_quiet"),),
            ),
            4,
        )
        bus.notify("ses_a", committed)  # no subscriber: the wake is lost, harmlessly

        log = EventLog(database.engine, bus=bus)
        page = await log.read("ses_a")
        assert [event.seq for event in page.items] == [1, 2, 3, 4, 5]
        assert page.items[-1] == committed[0]
        assert page.has_more is False


async def test_follow_enter_validates_cursor_and_session(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine, bus=bus)

        cases: list[Any] = [
            EventCursor(session_id="ses_b", seq=1),  # another session's cursor
            EventCursor(session_id="ses_a", seq=-1),  # negative seq
            EventCursor(session_id="ses_a", seq=5),  # beyond the captured H = 4
            SimpleNamespace(session_id="ses_a", seq="2"),  # invalid encoding
        ]
        for cursor in cases:
            with pytest.raises(InvalidCursorError):
                async with log.follow("ses_a", after=cursor):
                    pytest.fail("the follow context must not enter")
            assert bus.subscription_count == 0  # every failed enter released its wakeup


async def test_follow_bad_page_in_catchup_terminates_before_any_synced(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE events SET data = '{not json' WHERE session_id = 'ses_a' AND seq = 2")

    async with open_database(db_path, mode="create") as database:
        log = EventLog(database.engine, bus=bus)
        received: list[LogItem] = []
        with pytest.raises(StorageCorruptError) as corrupt:
            async with log.follow("ses_a") as items:
                received.append(await anext(items))  # the whole catch-up page fails
                pytest.fail("the first catch-up page must fail closed")
        error = corrupt.value
        assert error.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.core.retryable is False
        assert error.core.details["reason"] == "stored_event_decode_failed"
        assert error.core.details["first_bad_seq"] == 2
        # No partial yield (no half page) and the input cursor (None -> 0).
        assert received == []
        assert error.core.details["last_confirmed_cursor"] == {"session_id": "ses_a", "seq": 0}
        # The exit unregistered the wakeup; no Synced ever crossed the bad point.
        assert bus.subscription_count == 0


async def test_follow_bad_page_after_synced_keeps_healthy_then_terminates(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine, bus=bus, poll_interval=1.0)

        async with log.follow("ses_a") as items:
            received: list[LogItem] = []
            for _ in range(5):
                received.append(await anext(items))
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2, 3, 4]
            assert received[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=4))

            # Commits land in the live phase; the consumer drains the whole
            # live page (5-8) so the next live page is fetched fresh from
            # the log -- a page is a decode-time snapshot, so a row corrupted
            # after its page was fetched can only fail a later fetch.
            live_batch = tuple(
                _prompt_accepted_draft("ses_a", f"evt_ses_a_p{index}", text=f"live-{index}", message_id=f"msg_{index}")
                for index in range(3, 7)
            )
            more = await store.commit("ses_a", EventBatch("batch_live4", live_batch), 4)
            bus.notify("ses_a", more)
            for _ in range(len(more)):
                received.append(await asyncio.wait_for(anext(items), timeout=0.5))
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2, 3, 4, 5, 6, 7, 8]

            # A bad row lands in the NEXT live page, beyond the initial H=4
            # (the emitted Synced(4) only ever confirmed the old window).
            last = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_after_bad",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_late", text="late", message_id="msg_late"),),
                ),
                8,
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE events SET data = '{not json' WHERE session_id = 'ses_a' AND seq = 9")
            bus.notify("ses_a", last)

            with pytest.raises(StorageCorruptError) as corrupt:
                await anext(items)
            error = corrupt.value
            assert error.core.code == CoreErrorCode.STORAGE_CORRUPT
            assert error.core.retryable is False
            assert error.core.details["reason"] == "stored_event_decode_failed"
            assert error.core.details["first_bad_seq"] == 9
            assert error.core.details["first_bad_event_id"] == "evt_ses_a_late"
            # The last CONFIRMED cursor is the last yielded event's cursor
            # (seq 8): the bad page returned nothing and advanced nothing.
            assert error.core.details["last_confirmed_cursor"] == {"session_id": "ses_a", "seq": 8}
            # Healthy events yielded before the failure are kept (no loss),
            # and no second Synced was fabricated past the bad point.
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2, 3, 4, 5, 6, 7, 8]
            assert len([item for item in received if isinstance(item, Synced)]) == 1

        assert bus.subscription_count == 0

        # A fresh follow from the last confirmed cursor hits the same bad
        # page again: same cursor -> same error, no marker, no bypass.
        async with log.follow("ses_a", after=EventCursor(session_id="ses_a", seq=8)) as retry_items:
            with pytest.raises(StorageCorruptError) as retry:
                await anext(retry_items)
            assert retry.value.core.details == error.core.details


async def test_follow_uses_the_bus_as_a_wake_signal_only(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        log = EventLog(database.engine, bus=bus, poll_interval=1.0)

        async with log.follow("ses_a") as items:
            for _ in range(5):
                await anext(items)  # catch-up + Synced

            # An ephemeral notification wakes the stream but is never replayed
            # through it: follow guarantees durable only (§4 L238). The wake
            # arrived first, so a bus-as-data-path bug would deliver the
            # ephemeral event as the next item instead of the durable commit.
            bus.notify("ses_a", [_ephemeral("ses_a", "evt_eph")])
            committed = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_wake",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_wake", text="wake", message_id="msg_wake"),),
                ),
                4,
            )
            bus.notify("ses_a", committed)

            # The next item is the durable commit replayed from the log, in
            # seq order -- never the ephemeral event that woke the stream.
            assert await asyncio.wait_for(anext(items), timeout=0.5) == committed[0]

            # The ephemeral event was not merely deferred either: with no
            # further durable commits the stream stays quiet. (This closing
            # timeout only ends this test's pull; the context exit releases
            # the wakeup either way.)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(items), timeout=0.2)

        assert bus.subscription_count == 0


# -- repos: messages pagination + session record --------------------------------


async def test_repos_messages_paginate_by_seq_not_id_order(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await store.commit("ses_a", EventBatch("batch_c", (_session_created_draft("ses_a", "evt_c"),)), 0)
        # Message ids deliberately sort differently from their event seqs.
        await store.commit(
            "ses_a",
            EventBatch("batch_p1", (_prompt_accepted_draft("ses_a", "evt_p1", text="one", message_id="msg_z"),)),
            1,
        )
        await store.commit(
            "ses_a",
            EventBatch("batch_p2", (_prompt_accepted_draft("ses_a", "evt_p2", text="two", message_id="msg_a"),)),
            2,
        )
        await store.commit(
            "ses_a",
            EventBatch("batch_p3", (_prompt_accepted_draft("ses_a", "evt_p3", text="three", message_id="msg_m"),)),
            3,
        )

        repos = ReadRepositories(database.engine)
        first = await repos.messages("ses_a", limit=2)
        assert [message.id for message in first.items] == ["msg_z", "msg_a"]  # seq order, not id order
        assert _user_texts(first) == ["one", "two"]
        assert first.has_more is True
        assert first.next_after_message_id == "msg_a"

        second = await repos.messages("ses_a", after_message_id="msg_a", limit=2)
        assert [message.id for message in second.items] == ["msg_m"]
        assert second.has_more is False
        assert second.next_after_message_id == "msg_m"

        # An empty page keeps the input cursor.
        tail = await repos.messages("ses_a", after_message_id="msg_m")
        assert tail.items == []
        assert tail.next_after_message_id == "msg_m"
        assert tail.has_more is False

        # An unknown cursor message id is InvalidCursor.
        with pytest.raises(InvalidCursorError) as cursor_error:
            await repos.messages("ses_a", after_message_id="msg_missing")
        assert cursor_error.value.core.code == CoreErrorCode.INVALID_CURSOR

        # An unbuilt session is NotFound.
        with pytest.raises(NotFoundError) as not_found:
            await repos.messages("ses_missing")
        assert not_found.value.core.code == CoreErrorCode.NOT_FOUND


async def test_repos_missing_projection_and_undecodable_rows_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        assert await _populate(store, "ses_b", prompts=3, run_id="run_ses_b") == 4

    # ses_a loses its read model while the log survives; ses_b keeps the row
    # but one message payload stops being decodable.
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM sessions WHERE session_id = 'ses_a'")
        connection.execute("UPDATE messages SET data = '{not json' WHERE session_id = 'ses_b' AND message_id = 'msg_1'")

    async with open_database(db_path, mode="create") as database:
        repos = ReadRepositories(database.engine)

        # §3.2.1: a missing projection is never faked as an empty history.
        with pytest.raises(StorageCorruptError) as missing:
            await repos.messages("ses_a")
        assert missing.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert missing.value.core.details["reason"] == "sessions_row_missing_with_log"
        assert await repos.session("ses_a") is None

        with pytest.raises(StorageCorruptError) as bad_row:
            await repos.messages("ses_b")
        assert bad_row.value.core.details["reason"] == "message_row_decode_failed"
        assert bad_row.value.core.details["message_id"] == "msg_1"
        assert bad_row.value.core.details["seq"] == 3
        assert "{not json" not in json.dumps(bad_row.value.core.details)


async def test_repos_session_record_reads_the_s4_read_model(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4
        repos = ReadRepositories(database.engine)

        record = await repos.session("ses_a")
        assert record == SessionRecord(
            session_id="ses_a",
            status="active",
            applied_seq=4,
            active_run_id="run_1",
            config_snapshot={"permission_rules": [], "schema_version": 1},
            snapshot_hash=compute_snapshot_hash(ConfigSnapshot.create()),
            diagnostic=None,
        )
        assert await repos.session("ses_missing") is None


# -- purity: no external calls ----------------------------------------------------


async def test_reads_and_queries_make_no_external_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The S4 assertion pattern: commits, projections, EventLog reads and
    repo queries are local-only -- the whole pipeline passes with outbound
    sockets and subprocesses banned under monkeypatch (the socket class
    itself stays intact: the Windows proactor loop's wakeup pipe needs it)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    def banned(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("external call attempted under the ban")

    monkeypatch.setattr(socket, "create_connection", banned)
    monkeypatch.setattr(socket, "getaddrinfo", banned)
    monkeypatch.setattr(socket.socket, "connect", banned)
    monkeypatch.setattr(subprocess, "Popen", banned)
    monkeypatch.setattr(subprocess, "run", banned)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", banned)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", banned)

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=3) == 4

        repos = ReadRepositories(database.engine)
        log = EventLog(database.engine, bus=VolatileBus(), poll_interval=0.05)

        page = await log.read("ses_a")
        assert [event.seq for event in page.items] == [1, 2, 3, 4]
        assert (await repos.session("ses_a")) is not None
        messages = await repos.messages("ses_a")
        assert [message.id for message in messages.items] == ["msg_0", "msg_1", "msg_2"]

        async with log.follow("ses_a") as items:
            received = [await anext(items) for _ in range(5)]
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2, 3, 4]
            assert isinstance(received[-1], Synced)


# -- structure: storage stays bus-free ---------------------------------------------


def _imported_module_names(module: ModuleType) -> list[str]:
    """All module names imported by ``module`` (imports only, not docstrings)."""
    tree = ast.parse(inspect.getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_repos_and_storage_package_are_structurally_bus_free() -> None:
    """The ``repos.py`` docstring claims storage's bus-freedom is "asserted
    structurally by tests/integration/test_eventlog.py"; this is that
    assertion (the S3 store/txn AST walk, helper copied locally): neither
    ``opencode.storage.repos`` nor the ``opencode.storage`` package may
    import any bus-flavored name. ``opencode.bus.eventlog`` is deliberately
    out of scope -- it is the DB+bus-coupled durable read face by design
    (the S5 structural test pins the bus-side modules instead)."""
    import opencode.storage as storage_package
    import opencode.storage.repos as repos_module

    for module in (repos_module, storage_package):
        for name in _imported_module_names(module):
            assert "bus" not in name.lower(), f"{module.__name__} must not import {name}"
