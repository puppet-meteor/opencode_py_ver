"""Volatile live-only bus (R1-S5, CORE-CONTRACTS §4 L219-220): in-process subscriptions.

Contract: ``Bus.subscribe(session_id, types?) -> AsyncContextManager[AsyncIterator[Event]]``
sees only live notifications published after the subscription was entered
(ephemeral + committed durable); every subscription has a bounded queue;
overflow disconnects the subscription and reports ``SlowConsumer`` instead of
silently dropping durable events; leaving the context unregisters the
subscription and releases its queue. The bus persists nothing -- no durable
cursor, no replay: §3.2 L159 step 5 lets the composition layer compensate
missed notifications by re-reading the S6 EventLog, which keeps this module
free of any storage import (asserted structurally by tests/contract/test_bus.py).

TS mapping (doc/parity.md §3, ``packages/core/src/bus.ts``):
- ``subscribe`` L751-782: one typed PubSub per subscription + acquireRelease
  unregistering -> Python one bounded queue per ``async with`` subscription.
- ``notify`` L496-509 (listeners -> live PubSub -> typed): Python keeps only
  the live/typed surfaces -- per-subscription type filters -- and delivers
  each event to a matching subscription exactly once per notify call
  (duplicate event ids inside one call are collapsed). The deprecated
  ``listen`` listeners and ``project`` registration are deliberately absent.
- ``PubSub.unbounded`` L199 -> Python bounded queue + ``SlowConsumer``: the
  explicit §4 contract difference -- a slow consumer is disconnected loudly,
  never silently starved of durable events.
- L729-749 ``local()``/location routing: not ported (R1 is single-process).

Composition seam (§3.2 L159 step 5, S3-established): storage stays bus-free;
the caller commits first (``SessionStore.commit``) and then notifies the bus
with the returned committed durable events in seq order. The bus therefore
never sees uncommitted events (asserted by tests/integration/test_bus_commit.py)
and holds zero DB dependencies (acceptance A, latter half).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from opencode.contracts import (
    CoreError,
    CoreErrorCode,
    DurableEvent,
    EphemeralEvent,
    EventRegistry,
    JsonObject,
)

#: What a live subscription can yield (§4): committed durable + ephemeral.
#: CORE-CONTRACTS defines no unified ``Event`` alias; the bus owns this one.
type Event = DurableEvent[Any] | EphemeralEvent[Any]

#: Default bounded-queue size per subscription (§4 "有队列上限").
DEFAULT_QUEUE_LIMIT = 1000


class SlowConsumerError(Exception):
    """A subscription overflowed its bounded queue and was disconnected (§4/§8).

    The accepted prefix (events already enqueued) is still delivered; the event
    that no longer fit -- and everything published after it -- is NOT delivered,
    loudly instead of silently (§4: overflow disconnects and reports
    SlowConsumer, never silently dropping durable events). The ``core``
    attribute carries the §8 wire shape with code ``SlowConsumer``;
    ``retryable=False`` because the subscription is terminally disconnected:
    recovery is a fresh subscription plus an EventLog backfill (S6), not a retry
    of the same one.
    """

    def __init__(self, message: str, *, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.core = CoreError(
            code=CoreErrorCode.SLOW_CONSUMER,
            message=message,
            retryable=False,
            details=details if details is not None else {},
        )


@dataclass(frozen=True, slots=True)
class _Terminated:
    """Queue sentinel: the subscription was disconnected after an overflow."""

    error: SlowConsumerError


class _Subscription(AbstractAsyncContextManager[AsyncIterator[Event]]):
    """One bounded live subscription: async context manager + async iterator.

    Entering registers the subscription with the bus (notifications published
    BEFORE entering are invisible, §4); leaving unregisters it and releases
    the queue (dropping unread events); an overflow disconnects immediately,
    delivers the already-accepted prefix, and then raises
    :class:`SlowConsumerError` from the iterator.
    """

    def __init__(self, bus: VolatileBus, session_id: str, types: frozenset[str] | None, queue_limit: int) -> None:
        self._bus = bus
        self._session_id = session_id
        self._types = types
        self._queue_limit = queue_limit
        self._queue: asyncio.Queue[Event | _Terminated] = asyncio.Queue()
        self._queued_events = 0
        self._entered = False
        self._closed = False
        self._terminated = False

    async def __aenter__(self) -> AsyncIterator[Event]:
        if self._entered:
            raise RuntimeError("a bus subscription context cannot be entered twice")
        self._entered = True
        self._bus._register(self)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        self._release()

    def __aiter__(self) -> AsyncIterator[Event]:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if isinstance(item, _Terminated):
            self._closed = True
            self._release()
            raise item.error
        self._queued_events -= 1
        return item

    # -- bus-internal delivery surface (no awaits: atomic within the event loop) --

    def _accepts(self, event_type: str) -> bool:
        """Type filter: ``None`` = all types; an explicit empty set matches nothing."""
        return self._types is None or event_type in self._types

    def _offer(self, event: Event) -> bool:
        """Enqueue one live event; False = this subscription just overflowed.

        Called only by :meth:`VolatileBus.notify` while the subscription is
        still registered. The terminating sentinel always has queue room
        because the queue is unbounded and the event limit is enforced here,
        before the enqueue.
        """
        if self._closed or self._terminated:
            return True  # consumer left or was disconnected: nothing more to deliver
        if self._queued_events >= self._queue_limit:
            self._terminated = True
            self._queue.put_nowait(_Terminated(self._slow_consumer_error(event)))
            return False
        self._queue.put_nowait(event)
        self._queued_events += 1
        return True

    def _slow_consumer_error(self, event: Event) -> SlowConsumerError:
        """Sanitized §8 wire error locating the first undeliverable event."""
        details: JsonObject = {
            "session_id": self._session_id,
            "queue_limit": self._queue_limit,
            "event_id": event.id,
        }
        if isinstance(event, DurableEvent):
            details["seq"] = event.seq
        return SlowConsumerError(
            f"subscription for session {self._session_id!r} overflowed its queue "
            f"({self._queue_limit} events) at event {event.id!r} and was disconnected",
            details=details,
        )

    def _release(self) -> None:
        """Unregister from the bus and drop every queued item (§4: release the queue)."""
        self._bus._unregister(self)
        self._queued_events = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


class VolatileBus:
    """Live-only, in-process subscription channel (§4); persists nothing.

    ``subscribe`` returns an async context manager over the live event stream
    of one session; ``notify`` is the publisher side, called by the composition
    layer after a successful commit (§3.2 L159 step 5). The bus keeps no
    durable cursor and replays nothing (the S6 EventLog owns backfill), so a
    session without live subscribers is a plain no-op. Not thread-safe:
    single asyncio event loop, like every R1 runtime component.
    """

    def __init__(self, *, queue_limit: int = DEFAULT_QUEUE_LIMIT, registry: EventRegistry | None = None) -> None:
        """``queue_limit`` is the per-subscription bounded queue size (>= 1).

        ``registry``, when provided, fail-closes ``notify``: every event's type
        must be registered on its own bus (durable types by version, ephemeral
        types by name). ``None`` trusts the composition layer (the store
        already validates durable events at commit time).
        """
        if isinstance(queue_limit, bool) or not isinstance(queue_limit, int) or queue_limit < 1:
            raise ValueError("queue_limit must be an integer >= 1 (bools are rejected)")
        self._queue_limit = queue_limit
        self._registry = registry
        self._subscriptions: dict[str, list[_Subscription]] = {}

    @property
    def subscription_count(self) -> int:
        """Live subscriptions currently registered (across all sessions)."""
        return sum(len(subscriptions) for subscriptions in self._subscriptions.values())

    def subscribe(
        self,
        session_id: str,
        types: Iterable[str] | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[Event]]:
        """§4 L219: open a live subscription for one session.

        Only notifications published after the context is entered are visible.
        ``types=None`` receives every event type; an explicit collection
        receives only those types (an empty iterable matches nothing).
        Leaving the context unregisters the subscription and releases its
        queue.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if isinstance(types, str):
            raise ValueError("types must be an iterable of event type names, not a single string")
        normalized: frozenset[str] | None
        if types is None:
            normalized = None
        else:
            names = frozenset(types)
            for name in names:
                if not isinstance(name, str) or not name:
                    raise ValueError("every entry of types must be a non-empty event type name")
            normalized = names
        return _Subscription(self, session_id, normalized, self._queue_limit)

    def notify(self, session_id: str, events: Sequence[Event]) -> None:
        """Publish live notifications for one session (composition layer).

        The caller owns ordering (§3.2 L159 step 5: pass committed durable
        events in seq order -- the ``SessionStore.commit`` return value already
        is); the bus delivers in that order, to every matching subscription
        exactly once per call (duplicate event ids within one call are
        collapsed). Events are validated up front when a registry was
        provided, so a bad batch notifies nobody. A session without
        subscribers is a no-op. Overflow never raises here: the slow
        subscription is disconnected and the error surfaces on its own
        iterator.
        """
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        registry = self._registry
        prepared: list[Event] = []
        seen: set[str] = set()
        for event in events:
            if event.session_id != session_id:
                raise ValueError(f"event {event.id} belongs to session {event.session_id!r}, not {session_id!r}")
            if registry is not None:
                if isinstance(event, DurableEvent):
                    registry.resolve_durable(event.type, event.version)
                else:
                    registry.resolve_ephemeral(event.type)
            if event.id not in seen:
                seen.add(event.id)
                prepared.append(event)
        subscribers = self._subscriptions.get(session_id)
        if not subscribers:
            return
        for event in prepared:
            for subscription in list(subscribers):
                if subscription._accepts(event.type) and not subscription._offer(event):
                    self._unregister(subscription)

    # -- registration (no awaits: atomic within the event loop) --

    def _register(self, subscription: _Subscription) -> None:
        self._subscriptions.setdefault(subscription._session_id, []).append(subscription)

    def _unregister(self, subscription: _Subscription) -> None:
        subscribers = self._subscriptions.get(subscription._session_id)
        if subscribers is None:
            return
        try:
            subscribers.remove(subscription)  # identity: _Subscription defines no __eq__
        except ValueError:
            return
        if not subscribers:
            del self._subscriptions[subscription._session_id]
