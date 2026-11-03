"""Contract tests for the volatile bus (R1-S5, CORE-CONTRACTS §4 L219-220).

Pure asyncio unit tests, no database (the S4 storage composition is covered by
tests/integration/test_bus_commit.py). Covers the r1-slices §2 R1-S5 test plan:
- subscription timing: notifications published before the context is entered
  (or between ``subscribe`` and ``__aenter__``) are invisible;
- bounded queue: an exact fill passes and the queue recovers; overflow
  disconnects the subscription, still delivers the accepted prefix, then
  raises SlowConsumer (§8 wire shape, sanitized details locating the first
  undeliverable event) -- durable events are never silently dropped -- and a
  slow consumer does not affect a subscriber that keeps draining;
- context exit: the subscription is unregistered (publishing afterwards
  neither raises nor enqueues) and its queue is released (the iterator is
  done even with unread events); a fresh subscription sees only new events;
- type filter: ``types=...`` narrows delivery, an empty filter matches
  nothing, ``None`` receives everything;
- delivery: committed durable + ephemeral arrive live in publish order,
  duplicate ids within one notify call are delivered once, and notifications
  are isolated per session;
- validation: foreign-session events are rejected, registry validation fails
  closed before any delivery (and is skipped when no registry is injected);
- structural: the bus module imports no database machinery (zero DB
  dependencies, acceptance A) and exposes none of the TS surfaces R1
  deliberately drops (listen/project, location routing, durable cursor,
  EventLog read/follow/replay).
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from inspect import getsource
from types import ModuleType

import pytest
from pydantic import BaseModel, ConfigDict, Field

from opencode.bus import Event, SlowConsumerError, VolatileBus
from opencode.contracts import (
    R1_EVENT_DEFINITIONS,
    CoreErrorCode,
    DurabilityMismatchError,
    DurableEvent,
    EphemeralEvent,
    EventDefinition,
    EventRegistry,
    PromptAcceptedData,
    UnknownEventTypeError,
    UserMessage,
)

_CREATED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

_TEXT_DELTA = "py.text.delta"  # test-local ephemeral definition (contracts §3.1)


class TextDeltaData(BaseModel):
    """Test-local ephemeral payload (contracts keep only the mechanism, §3.1)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    part_id: str = Field(min_length=1)
    delta: str = ""


def _registry() -> EventRegistry:
    """R1 durable definitions plus one test-local ephemeral definition."""
    return EventRegistry(
        (
            *R1_EVENT_DEFINITIONS,
            EventDefinition(event_type=_TEXT_DELTA, durability="ephemeral", data_model=TextDeltaData),
        )
    )


def _durable(session_id: str, event_id: str, seq: int) -> DurableEvent[PromptAcceptedData]:
    """A committed ``py.prompt.accepted`` envelope at ``seq``."""
    return DurableEvent[PromptAcceptedData](
        id=event_id,
        type="py.prompt.accepted",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id="req_1",
            run_id="run_1",
            message=UserMessage(id="msg_1", type="user", created_at=_CREATED_AT, metadata={}, text="hello"),
        ),
        metadata={"run_id": "run_1"},
        seq=seq,
    )


def _ephemeral(session_id: str, event_id: str, delta: str = "x") -> EphemeralEvent[TextDeltaData]:
    """A live ephemeral ``py.text.delta`` envelope (no seq, no version)."""
    return EphemeralEvent[TextDeltaData](
        id=event_id,
        type=_TEXT_DELTA,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=TextDeltaData(message_id="msg_1", part_id="part_1", delta=delta),
        metadata={"run_id": "run_1"},
    )


def _seq(event: Event) -> int:
    """Narrow a delivered event to its durable envelope (seq assertions)."""
    assert isinstance(event, DurableEvent)
    return event.seq


async def _assert_silent(events: AsyncIterator[Event], window: float = 0.05) -> None:
    """Assert that nothing arrives on the subscription within ``window`` seconds."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(events), timeout=window)


class TestSubscriptionTiming:
    async def test_notifications_before_entering_are_invisible(self) -> None:
        bus = VolatileBus(registry=_registry())
        bus.notify("ses_a", [_durable("ses_a", "evt_1", 1)])  # committed before subscribing
        async with bus.subscribe("ses_a") as events:
            await _assert_silent(events)
            live = _ephemeral("ses_a", "evt_2")
            bus.notify("ses_a", [live])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is live
            await _assert_silent(events)

    async def test_notifications_between_subscribe_and_enter_are_invisible(self) -> None:
        bus = VolatileBus(registry=_registry())
        subscription = bus.subscribe("ses_a")
        bus.notify("ses_a", [_ephemeral("ses_a", "evt_1")])  # after subscribe(), before __aenter__
        async with subscription as events:
            await _assert_silent(events)


class TestBoundedQueue:
    async def test_exact_fill_passes_and_the_queue_recovers(self) -> None:
        bus = VolatileBus(queue_limit=3, registry=_registry())
        async with bus.subscribe("ses_a") as events:
            bus.notify("ses_a", [_durable("ses_a", f"evt_{seq}", seq) for seq in (1, 2, 3)])
            received = [await asyncio.wait_for(anext(events), timeout=1.0) for _ in range(3)]
            assert [_seq(event) for event in received] == [1, 2, 3]
            # the drained queue accepts more events: no lingering limit state
            bus.notify("ses_a", [_durable("ses_a", "evt_4", 4)])
            assert _seq(await asyncio.wait_for(anext(events), timeout=1.0)) == 4
            await _assert_silent(events)

    async def test_overflow_disconnects_and_reports_slow_consumer(self) -> None:
        bus = VolatileBus(queue_limit=2, registry=_registry())
        async with bus.subscribe("ses_a") as events:
            bus.notify("ses_a", [_durable("ses_a", "evt_1", 1), _durable("ses_a", "evt_2", 2)])
            bus.notify("ses_a", [_durable("ses_a", "evt_3", 3)])  # one event too many
            # the accepted prefix is still delivered first (no silent drop) ...
            assert (await asyncio.wait_for(anext(events), timeout=1.0)).id == "evt_1"
            assert (await asyncio.wait_for(anext(events), timeout=1.0)).id == "evt_2"
            # ... then the loud disconnect: SlowConsumer with the §8 wire shape
            with pytest.raises(SlowConsumerError) as error:
                await anext(events)
            assert error.value.core.code == CoreErrorCode.SLOW_CONSUMER
            assert error.value.core.retryable is False
            assert error.value.core.details == {
                "session_id": "ses_a",
                "queue_limit": 2,
                "event_id": "evt_3",
                "seq": 3,
            }
            # disconnected: the subscription left the bus, later publishes
            # neither enqueue nor raise, and the iterator is done.
            bus.notify("ses_a", [_durable("ses_a", "evt_4", 4)])
            assert bus.subscription_count == 0
            with pytest.raises(StopAsyncIteration):
                await anext(events)

    async def test_overflow_details_on_ephemeral_have_no_seq(self) -> None:
        bus = VolatileBus(queue_limit=1)
        async with bus.subscribe("ses_a") as events:
            bus.notify("ses_a", [_ephemeral("ses_a", "evt_1")])
            bus.notify("ses_a", [_ephemeral("ses_a", "evt_2")])
            assert (await asyncio.wait_for(anext(events), timeout=1.0)).id == "evt_1"
            with pytest.raises(SlowConsumerError) as error:
                await anext(events)
            assert error.value.core.details == {"session_id": "ses_a", "queue_limit": 1, "event_id": "evt_2"}

    async def test_overflowing_subscriber_does_not_affect_the_draining_one(self) -> None:
        bus = VolatileBus(queue_limit=2, registry=_registry())
        async with bus.subscribe("ses_a") as slow, bus.subscribe("ses_a") as fast:
            received: list[Event] = []
            for seq in (1, 2, 3, 4):
                bus.notify("ses_a", [_durable("ses_a", f"evt_{seq}", seq)])
                received.append(await asyncio.wait_for(anext(fast), timeout=1.0))
            assert [_seq(event) for event in received] == [1, 2, 3, 4]
            # the never-drained subscription kept its two accepted events and
            # was disconnected loudly at the third
            assert _seq(await asyncio.wait_for(anext(slow), timeout=1.0)) == 1
            assert _seq(await asyncio.wait_for(anext(slow), timeout=1.0)) == 2
            with pytest.raises(SlowConsumerError) as error:
                await anext(slow)
            assert error.value.core.details["event_id"] == "evt_3"
            assert bus.subscription_count == 1  # only the draining one remains
            await _assert_silent(fast)


class TestContextExit:
    async def test_exit_unregisters_and_releases_the_queue(self) -> None:
        bus = VolatileBus(registry=_registry())
        subscription = bus.subscribe("ses_a")
        async with subscription as events:
            bus.notify("ses_a", [_durable("ses_a", "evt_1", 1), _durable("ses_a", "evt_2", 2)])
            assert bus.subscription_count == 1
        # leaving unregistered the subscription: publishing afterwards neither
        # raises nor enqueues anywhere
        bus.notify("ses_a", [_durable("ses_a", "evt_3", 3)])
        assert bus.subscription_count == 0
        # the queue was released: the iterator is done even though two events
        # were never read
        with pytest.raises(StopAsyncIteration):
            await anext(events)

    async def test_fresh_subscription_after_exit_sees_only_new_events(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a"):
            bus.notify("ses_a", [_durable("ses_a", "evt_1", 1)])  # never read
        second = _durable("ses_a", "evt_2", 2)
        async with bus.subscribe("ses_a") as events:
            bus.notify("ses_a", [second])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is second
            await _assert_silent(events)
            # live-only: no replay of evt_1 (backfill is S6 EventLog's job)

    async def test_subscription_context_cannot_be_entered_twice(self) -> None:
        bus = VolatileBus()
        subscription = bus.subscribe("ses_a")
        async with subscription:
            pass
        with pytest.raises(RuntimeError, match="cannot be entered twice"):
            async with subscription:
                pass


class TestTypeFilter:
    async def test_type_filter_delivers_only_matching_types(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with (
            bus.subscribe("ses_a", types=(_TEXT_DELTA,)) as filtered,
            bus.subscribe("ses_a") as everything,
        ):
            durable = _durable("ses_a", "evt_1", 1)
            delta = _ephemeral("ses_a", "evt_2", delta="a")
            bus.notify("ses_a", [durable, delta])
            assert await asyncio.wait_for(anext(filtered), timeout=1.0) is delta
            await _assert_silent(filtered)
            # the untyped subscription receives both, in publish order
            assert await asyncio.wait_for(anext(everything), timeout=1.0) is durable
            assert await asyncio.wait_for(anext(everything), timeout=1.0) is delta

    async def test_empty_type_filter_matches_nothing(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a", types=()) as events:
            bus.notify("ses_a", [_durable("ses_a", "evt_1", 1), _ephemeral("ses_a", "evt_2")])
            await _assert_silent(events)

    def test_single_string_type_filter_is_rejected(self) -> None:
        bus = VolatileBus()
        with pytest.raises(ValueError, match="not a single string"):
            bus.subscribe("ses_a", types=_TEXT_DELTA)  # a str would filter by characters


class TestDelivery:
    async def test_committed_durable_and_ephemeral_arrive_in_publish_order(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            durable = _durable("ses_a", "evt_1", 1)
            ephemeral = _ephemeral("ses_a", "evt_2")
            bus.notify("ses_a", [durable, ephemeral])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is durable
            assert await asyncio.wait_for(anext(events), timeout=1.0) is ephemeral
            await _assert_silent(events)

    async def test_duplicate_ids_within_one_call_are_delivered_once(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            event = _durable("ses_a", "evt_1", 1)
            bus.notify("ses_a", [event, event.model_copy()])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is event
            await _assert_silent(events)

    async def test_notifications_are_isolated_per_session(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            bus.notify("ses_b", [_durable("ses_b", "evt_1", 1), _ephemeral("ses_b", "evt_2")])
            await _assert_silent(events)
            mine = _durable("ses_a", "evt_3", 3)
            bus.notify("ses_a", [mine])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is mine


class TestValidation:
    async def test_notify_rejects_events_of_another_session(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            with pytest.raises(ValueError, match="belongs to session"):
                bus.notify("ses_a", [_durable("ses_b", "evt_1", 1)])
            await _assert_silent(events)

    async def test_registry_validation_fails_closed(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            with pytest.raises(UnknownEventTypeError):
                bus.notify("ses_a", [_durable("ses_a", "evt_1", 1).model_copy(update={"type": "py.unknown.type"})])
            with pytest.raises(UnknownEventTypeError):
                bus.notify("ses_a", [_ephemeral("ses_a", "evt_2").model_copy(update={"type": "py.unknown.delta"})])
            with pytest.raises(DurabilityMismatchError):
                bus.notify("ses_a", [_ephemeral("ses_a", "evt_3").model_copy(update={"type": "py.prompt.accepted"})])
            await _assert_silent(events)

    async def test_validation_happens_before_any_delivery(self) -> None:
        bus = VolatileBus(registry=_registry())
        async with bus.subscribe("ses_a") as events:
            good = _durable("ses_a", "evt_1", 1)
            bad = _durable("ses_a", "evt_2", 2).model_copy(update={"type": "py.unknown.type"})
            with pytest.raises(UnknownEventTypeError):
                bus.notify("ses_a", [good, bad])
            await _assert_silent(events)  # the good prefix was not delivered either

    async def test_without_registry_the_bus_does_not_validate_types(self) -> None:
        bus = VolatileBus()  # no registry: the composition layer owns validation
        async with bus.subscribe("ses_a") as events:
            event = _ephemeral("ses_a", "evt_1")
            bus.notify("ses_a", [event])
            assert await asyncio.wait_for(anext(events), timeout=1.0) is event

    def test_queue_limit_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, True):
            with pytest.raises(ValueError, match="queue_limit"):
                VolatileBus(queue_limit=bad)

    def test_subscribe_rejects_empty_session_id(self) -> None:
        bus = VolatileBus()
        with pytest.raises(ValueError, match="session_id"):
            bus.subscribe("")


def _imported_module_names(module: ModuleType) -> list[str]:
    """All module names imported by ``module`` (imports only, not docstrings)."""
    tree = ast.parse(getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


class TestStructuralGuards:
    def test_bus_modules_are_structurally_db_free(self) -> None:
        """Acceptance A (latter half): the bus holds zero DB dependencies."""
        import opencode.bus as bus_package
        import opencode.bus.bus as bus_module

        for module in (bus_package, bus_module):
            for name in _imported_module_names(module):
                root = name.split(".")[0]
                assert root not in {"aiosqlite", "alembic", "sqlite3", "sqlalchemy"}, (
                    f"{module.__name__} must not import {name}"
                )
                assert not name.startswith("opencode.storage"), f"{module.__name__} must not import {name}"

    def test_volatile_bus_has_no_dropped_ts_surfaces(self) -> None:
        """No overreach: no deprecated listen/project, no location routing,
        no durable cursor, and no S6 EventLog surface on the R1-S5 bus."""
        for name in ("listen", "project", "local", "routes", "read", "follow", "replay", "cursor"):
            assert not hasattr(VolatileBus, name), f"VolatileBus must not expose {name}()"

    def test_event_union_covers_both_envelopes(self) -> None:
        """The bus-owned ``Event`` alias is exactly durable | ephemeral."""
        assert isinstance(_durable("ses_a", "evt_1", 1), DurableEvent)
        assert isinstance(_ephemeral("ses_a", "evt_1"), EphemeralEvent)
