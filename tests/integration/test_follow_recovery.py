"""Integration tests: EventLog follow disconnection / slow-consumer recovery (R4-S5).

T-C1-038 (r4-slices §2.4): coverage closure over the EXISTING CORE-CONTRACTS
§4 L227-246 implementation -- ``src/opencode/bus/bus.py`` and
``src/opencode/bus/eventlog.py`` are product-frozen here (zero change unless
a defect surfaces); these tests only close the R4-S5 acceptance gaps:

- slow consumer (§4 L227-228 + failure-matrix L123 "慢消费者" + acceptance C
  "慢订阅"): the controlled ``VolatileBus(queue_limit=N)`` ctor injection
  (OP-R4-4 conservative default: a real small bound, no synthetic exception,
  no monkeypatch) disconnects an overflowing external subscription loudly --
  accepted prefix still delivered, the §8 SlowConsumer wire shape (what the
  cli.py L821-822 display face catches, read-only anchor) surfaces on the
  iterator, the subscription unregisters, and every committed durable event
  stays recoverable from the log (read backfill + a fresh subscription);
- the follow-internal wakeup subscription can overflow the same way: the
  drain task only loses wakeups (``_drain`` swallows the disconnect and sets
  one final wake) and the stream stays complete through the <= 1s poll
  fallback -- the bus is a wake signal, never a data path (§4 L241);
- follow reconnect is at-least-once (§4 L242④): a consumer reconnecting from
  a lagging checkpoint -- the reconnect cursor deliberately placed on an
  already-delivered seq, the deterministic duplicate window -- sees the
  boundary events redelivered and dedups by session+seq/id into exactly-once
  processing;
- durable vs ephemeral (§4 L246 + L123 "durable 从 DB 补读, ephemeral 可丢"):
  a parallel live Bus subscription (the real-time UI preview face) receives
  the ephemeral notifications while the follow stream never yields one, lost
  wakes degrade only to the poll cadence for durable events, and no read
  face can ever return an ephemeral event.

Existing coverage is deliberately NOT re-pinned (对拍, progress ①):
tests/integration/test_eventlog.py pins the §4 read/follow/bad-page/gate
faces and tests/contract/test_bus.py pins the pure-bus bounded-queue faces
(overflow wire shape incl. the ephemeral no-seq variant, accepted prefix,
drain isolation, context-exit release, fresh subscription). Real SQLite +
``alembic upgrade head`` + ``SessionStore.commit`` keep these tests
composition-true; the sqlite3 hand-corruption recipes are not needed here.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from opencode.bus import SlowConsumerError, VolatileBus
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
from opencode.storage import EventBatch, SessionStore, open_database

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


# -- gap 1: an overflowing external subscription disconnects loudly ---------------


async def test_slow_consumer_disconnect_keeps_every_durable_event_recoverable(tmp_path: Path) -> None:
    """§4 L227-228 end to end over the real composition seam: commit, then
    notify; a subscription that overflows its injected bound is disconnected
    loudly, while the log (not the notification) keeps every durable event."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus(queue_limit=2)  # OP-R4-4: a real controlled bound, not a fault injection

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=1) == 2
        log = EventLog(database.engine)

        async with bus.subscribe("ses_a") as events:
            # One synchronous notify of a real committed batch: the first two
            # events fill the queue, the third overflows it (notify never
            # awaits, so the overflow point is deterministic).
            batch = tuple(
                _prompt_accepted_draft(
                    "ses_a", f"evt_ses_a_slow{index}", text=f"slow-{index}", message_id=f"msg_slow{index}"
                )
                for index in range(3)
            )
            committed = await store.commit("ses_a", EventBatch("batch_slow", batch), 2)
            bus.notify("ses_a", committed)
            assert bus.subscription_count == 0  # disconnected at the overflow, already unregistered

            # The accepted prefix (the two already-enqueued events) is still
            # delivered first -- never a silent drop.
            assert (await asyncio.wait_for(anext(events), timeout=1.0)).id == "evt_ses_a_slow0"
            assert (await asyncio.wait_for(anext(events), timeout=1.0)).id == "evt_ses_a_slow1"

            # ... then the loud disconnect: the §8 wire shape the cli.py
            # L821-822 display face catches (read-only anchor, zero change).
            with pytest.raises(SlowConsumerError) as slow:
                await anext(events)
            assert slow.value.core.code == CoreErrorCode.SLOW_CONSUMER
            assert slow.value.core.retryable is False
            assert slow.value.core.details == {
                "session_id": "ses_a",
                "queue_limit": 2,
                "event_id": "evt_ses_a_slow2",
                "seq": 5,
            }

            # Disconnected: a later notify reaches nobody and the drained
            # subscription stays done.
            after = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_after",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_after", text="after", message_id="msg_after"),),
                ),
                5,
            )
            bus.notify("ses_a", after)
            assert bus.subscription_count == 0
            with pytest.raises(StopAsyncIteration):
                await anext(events)

        # §4 L228: durable events are never silently dropped -- the log, not
        # the notification, is the source of truth: read backfills everything.
        page = await log.read("ses_a")
        assert [event.seq for event in page.items] == [1, 2, 3, 4, 5, 6]
        assert page.items[-1] == after[0]
        assert page.has_more is False

        # Recovery is a fresh subscription plus the log: new notifications
        # are delivered live again (L228: 重订阅/EventLog.read 可全量补读).
        async with bus.subscribe("ses_a") as recovered:
            fresh = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_fresh",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_fresh", text="fresh", message_id="msg_fresh"),),
                ),
                6,
            )
            bus.notify("ses_a", fresh)
            assert await asyncio.wait_for(anext(recovered), timeout=1.0) == fresh[0]


async def test_follow_wakeup_overflow_only_costs_wakeups_poll_stays_complete(tmp_path: Path) -> None:
    """The follow-internal wakeup subscription overflows the same controlled
    way (queue_limit=1); the drain task only loses wakeups and the stream
    stays complete through the poll fallback (§4 L241: the bus is a signal)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus(queue_limit=1)  # the wakeup subscription's own bound

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=1) == 2
        log = EventLog(database.engine, bus=bus, poll_interval=0.05)

        async with log.follow("ses_a") as items:
            catchup = [await anext(items) for _ in range(3)]
            assert [item.seq for item in catchup if isinstance(item, DurableEvent)] == [1, 2]
            assert catchup[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=2))

            # One synchronous notify of a 2-event batch: the first event fills
            # the wakeup queue, the second overflows the follow-internal
            # subscription itself (deterministic: notify never awaits, so the
            # drain task cannot consume in between).
            batch = tuple(
                _prompt_accepted_draft(
                    "ses_a", f"evt_ses_a_drop{index}", text=f"drop-{index}", message_id=f"msg_drop{index}"
                )
                for index in range(2)
            )
            committed = await store.commit("ses_a", EventBatch("batch_drop", batch), 2)
            bus.notify("ses_a", committed)
            assert bus.subscription_count == 0  # the wakeup subscription was disconnected

            # The disconnect only cost wakeups: _drain swallowed it, set one
            # final wake, and the stream still delivers BOTH events from the
            # log (the query range, not the notification, is the data path).
            woken = [await asyncio.wait_for(anext(items), timeout=1.0) for _ in range(2)]
            assert [event.seq for event in woken if isinstance(event, DurableEvent)] == [3, 4]

            # With the wake path dead, a commit nobody notifies about still
            # arrives through the <= 1s poll fallback (§4 L241③).
            silent = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_silent",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_silent", text="silent", message_id="msg_silent"),),
                ),
                4,
            )
            polled = await asyncio.wait_for(anext(items), timeout=1.0)
            assert polled == silent[0]

        assert bus.subscription_count == 0


# -- gap 2: follow reconnect is at-least-once; the consumer dedups ---------------


async def test_follow_reconnect_is_at_least_once_and_the_consumer_dedups(tmp_path: Path) -> None:
    """§4 L242④: reconnecting with a cursor on an already-delivered seq (a
    checkpoint lagging the last processed seq -- the at-least-once duplicate
    window, constructed deterministically) redelivers the boundary events;
    dedup by session+seq/id turns that into exactly-once processing."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=5) == 6
        log = EventLog(database.engine, bus=bus, poll_interval=0.05)

        processed: dict[int, str] = {}  # seq -> event id: a deduplicating consumer

        def process(item: LogItem) -> None:
            if isinstance(item, DurableEvent):
                if item.seq in processed:
                    assert processed[item.seq] == item.id  # (session, seq) and id keys agree on redelivery
                else:
                    processed[item.seq] = item.id

        # Connection 1: the full catch-up 1..6 + Synced(6), then a plain
        # consumer-side exit (the disconnect form -- deterministic, no fault
        # injection; bad-page-termination reconnect is already pinned by
        # test_eventlog.py, the external slow disconnect by the tests above).
        async with log.follow("ses_a") as items:
            first = [await anext(items) for _ in range(7)]
            assert [item.seq for item in first if isinstance(item, DurableEvent)] == [1, 2, 3, 4, 5, 6]
            assert first[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=6))
            for item in first:
                process(item)
        assert bus.subscription_count == 0
        assert sorted(processed) == [1, 2, 3, 4, 5, 6]

        # Connection 2: the reconnect cursor sits ON an already-delivered seq
        # (4): a checkpoint lagging the last processed seq (6) is exactly the
        # at-least-once duplicate window -- the boundary events 5 and 6 are
        # redelivered, by design rather than by timing luck.
        async with log.follow("ses_a", after=EventCursor(session_id="ses_a", seq=4)) as items:
            redelivered = [await anext(items) for _ in range(3)]
            assert [item.seq for item in redelivered if isinstance(item, DurableEvent)] == [5, 6]
            assert redelivered[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=6))

            committed = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_reconnect",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_r7", text="r7", message_id="msg_r7"),),
                ),
                6,
            )
            bus.notify("ses_a", committed)
            second = [*redelivered, await asyncio.wait_for(anext(items), timeout=1.0)]

        assert bus.subscription_count == 0
        for item in second:
            process(item)

        # Strictly increasing seq within the reconnected stream (§4 L242④),
        # with the redelivery window really delivered twice.
        seqs = [item.seq for item in second if isinstance(item, DurableEvent)]
        assert seqs == [5, 6, 7]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        # At-least-once redelivery (5, 6 delivered on both connections) plus
        # consumer dedup -> every event processed exactly once.
        assert sorted(processed) == [1, 2, 3, 4, 5, 6, 7]
        assert len(processed) == 7


# -- gap 3: durable is backfilled from the DB; ephemeral is droppable ------------


async def test_follow_never_yields_ephemeral_while_the_live_preview_sees_it(tmp_path: Path) -> None:
    """§4 L246 + L123: a parallel live Bus subscription (the real-time UI
    preview face) sees the ephemeral notifications; the follow stream never
    yields one, lost wakes only degrade durable delivery to the poll cadence,
    and no read face can ever return an ephemeral event."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        assert await _populate(store, "ses_a", prompts=1) == 2
        log = EventLog(database.engine, bus=bus, poll_interval=0.05)

        received: list[LogItem] = []
        async with bus.subscribe("ses_a") as preview, log.follow("ses_a") as items:
            for _ in range(3):
                received.append(await anext(items))
            assert [item.seq for item in received if isinstance(item, DurableEvent)] == [1, 2]
            assert received[-1] == Synced(cursor=EventCursor(session_id="ses_a", seq=2))

            # An ephemeral notification reaches the parallel preview (the §4
            # L246 real-time UI face) but never enters the follow stream: the
            # next item follow yields is the later durable commit, and the
            # closing shape pin proves the ephemeral was not deferred either.
            delta_one = _ephemeral("ses_a", "evt_eph_one")
            bus.notify("ses_a", [delta_one])
            assert await asyncio.wait_for(anext(preview), timeout=1.0) is delta_one

            # A durable commit nobody notifies about: the lost wake degrades
            # only to the poll cadence -- the durable value still arrives in
            # full -- while the preview (never notified) misses it outright.
            quiet_one = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_quiet_one",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_quiet1", text="quiet-1", message_id="msg_quiet1"),),
                ),
                2,
            )
            durable = await asyncio.wait_for(anext(items), timeout=1.0)
            received.append(durable)
            assert durable == quiet_one[0]
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(preview), timeout=0.2)

            # A second ephemeral after the durable commit: same contrast, and
            # the pull below proves it never rides along either.
            delta_two = _ephemeral("ses_a", "evt_eph_two")
            bus.notify("ses_a", [delta_two])
            assert await asyncio.wait_for(anext(preview), timeout=1.0) is delta_two

            quiet_two = await store.commit(
                "ses_a",
                EventBatch(
                    "batch_quiet_two",
                    (_prompt_accepted_draft("ses_a", "evt_ses_a_quiet2", text="quiet-2", message_id="msg_quiet2"),),
                ),
                3,
            )
            durable_two = await asyncio.wait_for(anext(items), timeout=1.0)
            received.append(durable_two)
            assert durable_two == quiet_two[0]

        assert bus.subscription_count == 0

        # The follow stream was durable-only end to end: every yielded item is
        # a DurableEvent or the one Synced marker -- never an ephemeral token.
        assert all(isinstance(item, (DurableEvent, Synced)) for item in received)
        assert not any(isinstance(item, EphemeralEvent) for item in received)

        # Durable is fully recoverable from the DB; ephemeral exists in no
        # read face (L123: durable 从 DB 补读, ephemeral 可丢).
        page = await log.read("ses_a")
        assert [event.seq for event in page.items] == [1, 2, 3, 4]
        assert {event.id for event in page.items}.isdisjoint({"evt_eph_one", "evt_eph_two"})
