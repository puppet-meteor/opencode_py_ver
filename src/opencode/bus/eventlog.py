"""Durable event-log read/follow surface (R1-S6, CORE-CONTRACTS §4 L222-238).

This is the DB-coupled half of the §4 bus seam: :mod:`opencode.bus.bus`
(VolatileBus) is the live-only, zero-DB notification channel, while this
module is the durable feed over storage -- it imports sqlalchemy and
``opencode.storage`` (engine type, read-only repositories, the shared JSON
decode helpers) by design. The R1-S5 structural guard only pins
``bus/__init__`` and ``bus/bus`` as DB-free, which stays true; the volatile
package export surface is unchanged and this module is deliberately NOT
re-exported there -- users import ``from opencode.bus.eventlog import ...``.

TS mapping (``packages/core/src/bus.ts``, doc/parity.md §3):
- ``readAfter`` L786-828: ``gt(seq, after) + lte(through)``, asc, page limit
  -> Python :meth:`EventLog.read` (limit 1-1000, default 100, clamped).
  **T-A-002 final ruling (§3.2.1/§4)**: TS skips unknown durable types and
  advances the raw seq (L809-827) -- Python does NOT: every fetched row is
  decoded by original seq in order, unknown type/version or corrupted data
  fails the whole page closed as ``StorageCorrupt(retryable=false)`` with no
  half page and no next_cursor advance, so retrying the same cursor raises
  the same error again.
- ``log`` L850-893: ``target = latestSequence``, one ``Synced`` marker after
  the replay, then wake -> query -> yield -> :meth:`EventLog.follow` four
  steps (register wakeup BEFORE capturing H in a short read transaction;
  paginated catch-up after.seq < seq <= H; exactly one
  ``Synced(EventCursor(session_id, H))``; then always query seq > last_seq
  with the bus as a wake signal only).
- ``subscribeDurable`` L830-848 ``PubSub.sliding(1)`` -> Python one
  ``asyncio.Event`` per follow (single-slot: duplicate wakes merge) drained
  from an injected :class:`~opencode.bus.bus.VolatileBus` subscription, plus
  the mandatory <= 1 second poll fallback (§4: "重复信号合并、最长 1 秒轮询
  兜底"). The notification payload is never re-sent: H-before/H-after commits
  are separated by the query range, not by the wake content.

Bad-page semantics (T-A-002, contract L227/L236), all fail closed:
- read: whole-page ``StorageCorrupt(retryable=false)``; details carry the
  sanitized reason, session_id, the locatable bad seq/event_id and
  ``last_confirmed_cursor`` (== the input cursor, None -> 0) and never the
  raw row content; pages returned before the corruption was discovered stay
  valid.
- follow: already-yielded healthy events are kept, then StorageCorrupt
  terminates the iteration and the context exit unregisters the wakeup; no
  event, no later event and no Synced marker past the bad point is emitted
  (a Synced(H) already sent only ever confirmed the old window);
  ``last_confirmed_cursor`` is the last yielded event's cursor, never a seq
  decoded but undelivered inside the failing page.
- once a session is ``read_only_diagnostic``, typed read/follow raise
  StorageCorrupt for any cursor (no bypassing the bad point with a larger
  after or type filters) -- the status is read through the read-only repo
  (:class:`opencode.storage.repos.ReadRepositories`), keeping this module
  free of any write/isolation entry (§3.2.1: EventLog may only request
  isolation, never write tables).

The JSON decode helpers ``_json_column``/``from_epoch_ms`` and the reason
constant are imported from :mod:`opencode.storage.txn` (private cross-module
import -- a known tradeoff accepted by the task book; extracting a shared
codec module is deliberately NOT done here, see the progress file).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from opencode.bus.bus import Event, VolatileBus
from opencode.contracts import (
    DurableEvent,
    EventDecodeError,
    EventRegistry,
    JsonObject,
    default_event_registry,
)
from opencode.storage.repos import (
    InvalidCursorError,
    NotFoundError,
    ReadRepositories,
    clamp_page_limit,
)
from opencode.storage.txn import (
    REASON_STORED_DECODE_FAILED,
    StorageCorruptError,
    _json_column,
    from_epoch_ms,
)

#: §4: read limit bounds -- out-of-range values are clamped into the interval.
MIN_READ_LIMIT = 1
MAX_READ_LIMIT = 1000
DEFAULT_READ_LIMIT = 100

#: Internal catch-up/live page size (TS ``logReadPageSize = 512``, bus.ts L207).
DEFAULT_FOLLOW_PAGE_SIZE = 512

#: §4: the poll fallback waits at most 1 second (contract ceiling, never exceeded).
MAX_POLL_INTERVAL = 1.0

#: The §4 wire literal for a quarantined session (sessions.status column).
STATUS_READ_ONLY_DIAGNOSTIC = "read_only_diagnostic"

#: Sanitized reasons owned by this module (txn owns the decode-failure reason).
REASON_SESSION_READ_ONLY = "session_read_only_diagnostic"
REASON_NON_INCREASING_SEQ = "non_increasing_seq"

_SELECT_HIGH = sa.text("SELECT COALESCE(MAX(seq), 0) AS h FROM events WHERE session_id = :sid")
_SELECT_PAGE = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata "
    "FROM events WHERE session_id = :sid AND seq > :after ORDER BY seq LIMIT :limit"
)
_SELECT_PAGE_THROUGH = sa.text(
    "SELECT id, session_id, seq, type, version, created, data, metadata "
    "FROM events WHERE session_id = :sid AND seq > :after AND seq <= :through ORDER BY seq LIMIT :limit"
)

_MISSING = object()


@dataclass(frozen=True, slots=True)
class EventCursor:
    """§4 L222: ``{session_id: str, seq: int >= 0}``; 0 means before the log.

    Cursors are never valid across sessions. Construction stays permissive
    (the dataclass is a wire shape, not a validator): :meth:`EventLog.read`
    and :meth:`EventLog.follow` validate structurally at call time and raise
    InvalidCursor for a mismatched session, a negative/non-integer/boolean
    seq, or a seq beyond the current high watermark H.
    """

    session_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class EventPage:
    """§4 L222: one read page of durable events.

    ``next_cursor`` is the last item's seq; an empty page keeps the input
    cursor (None -> seq 0). ``has_more`` tells whether another page exists.
    """

    items: list[DurableEvent[Any]]
    next_cursor: EventCursor
    has_more: bool


@dataclass(frozen=True, slots=True)
class Synced:
    """The §4 catch-up marker (TS ``log.synced`` L879-884): caught up to H.

    Emitted exactly once per follow, after the catch-up window
    (after.seq < seq <= H) is exhausted -- never out of band and never past
    a bad point.
    """

    cursor: EventCursor


#: What a follow yields: durable events plus the one Synced marker.
type LogItem = DurableEvent[Any] | Synced


def _cursor_json(cursor: EventCursor) -> JsonObject:
    """Serialize a cursor into sanitized JSON details (§4 last_confirmed_cursor)."""
    return {"session_id": cursor.session_id, "seq": cursor.seq}


def _resolve_cursor(session_id: str, after: EventCursor | None) -> EventCursor:
    """Structural cursor validation (state-independent, before any DB access).

    ``None`` is the session's seq-0 cursor (before the log). A duck-typed
    cursor is tolerated field-wise: a missing/non-string session_id, or a
    boolean/non-integer/negative seq, is invalid encoding -> InvalidCursor
    (§4 L225: "session 不同、负数或编码非法为 InvalidCursor").
    """
    if after is None:
        return EventCursor(session_id=session_id, seq=0)
    cursor_session = getattr(after, "session_id", _MISSING)
    cursor_seq = getattr(after, "seq", _MISSING)
    if cursor_session is _MISSING or not isinstance(cursor_session, str) or not cursor_session:
        raise InvalidCursorError(
            "cursor.session_id must be a non-empty string",
            details={"session_id": session_id, "reason": "cursor_session_invalid"},
        )
    if cursor_session != session_id:
        raise InvalidCursorError(
            f"cursor belongs to session {cursor_session!r}, not {session_id!r}",
            details={
                "session_id": session_id,
                "cursor_session_id": cursor_session,
                "reason": "cursor_session_mismatch",
            },
        )
    if cursor_seq is _MISSING or isinstance(cursor_seq, bool) or not isinstance(cursor_seq, int):
        raise InvalidCursorError(
            "cursor.seq must be an integer (booleans are rejected)",
            details={"session_id": session_id, "reason": "cursor_seq_invalid"},
        )
    if cursor_seq < 0:
        raise InvalidCursorError(
            f"cursor.seq {cursor_seq} is negative",
            details={"session_id": session_id, "seq": cursor_seq, "reason": "cursor_seq_negative"},
        )
    return EventCursor(session_id=session_id, seq=cursor_seq)


def _require_session_id(session_id: str) -> None:
    """State-independent caller-input check (rejected before any DB access)."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")


class EventLog:
    """The §4 durable feed over one async storage engine: read + follow.

    ``read`` is a stateless page read; ``follow`` is the four-step streaming
    surface (register wakeup -> capture H -> catch-up + one Synced -> live
    with the bus as a wake signal and a <= 1s poll fallback). Both fail
    closed on corrupted stored rows and on quarantined sessions. H is always
    the committed log watermark ``COALESCE(MAX(events.seq), 0)`` -- never the
    projection watermark ``sessions.applied_seq`` (§4 L225).
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        bus: VolatileBus | None = None,
        registry: EventRegistry | None = None,
        repos: ReadRepositories | None = None,
        poll_interval: float = 1.0,
        page_size: int = DEFAULT_FOLLOW_PAGE_SIZE,
    ) -> None:
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, int | float) or poll_interval <= 0:
            raise ValueError("poll_interval must be a positive number of seconds (bools are rejected)")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not MIN_READ_LIMIT <= page_size <= MAX_READ_LIMIT
        ):
            raise ValueError(f"page_size must be an integer in {MIN_READ_LIMIT}..{MAX_READ_LIMIT}")
        self._engine = engine
        self._bus = bus
        self._registry = registry if registry is not None else default_event_registry()
        self._repos = repos if repos is not None else ReadRepositories(engine)
        #: clamped to the §4 ceiling: the poll fallback never waits > 1s.
        self._poll_interval = min(float(poll_interval), MAX_POLL_INTERVAL)
        self._page_size = page_size

    # -- §4 L224-227: read -------------------------------------------------

    async def read(
        self,
        session_id: str,
        after: EventCursor | None = None,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> EventPage:
        """Read one page of durable events with seq > after.seq.

        ``None`` equals this session's seq-0 cursor; ``limit`` is clamped
        into 1..1000. Raises NotFound for a never-created session,
        InvalidCursor for a cursor of another session / negative or invalid
        encoding / a seq beyond the current high watermark H (explicit, never
        an empty wait), and a whole-page ``StorageCorrupt(retryable=false)``
        when any fetched row (the page or its has-more probe) fails to decode
        -- the failing page's ``last_confirmed_cursor`` equals the input
        cursor (None -> 0).
        """
        _require_session_id(session_id)
        cursor = _resolve_cursor(session_id, after)
        read_limit = clamp_page_limit(limit)

        record = await self._repos.session(session_id)
        async with self._engine.connect() as connection:
            high = await self._log_high(connection, session_id)
            if record is None and high == 0:
                raise NotFoundError(f"session {session_id} does not exist", details={"session_id": session_id})
            if record is not None and record.status == STATUS_READ_ONLY_DIAGNOSTIC:
                raise self._gate_error(session_id, cursor)
            if cursor.seq > high:
                raise InvalidCursorError(
                    f"cursor seq {cursor.seq} is beyond the current high watermark {high}",
                    details={
                        "session_id": session_id,
                        "seq": cursor.seq,
                        "high_watermark": high,
                        "reason": "cursor_seq_beyond_high_watermark",
                    },
                )
            items, has_more = await self._fetch_page(
                connection, session_id, cursor.seq, read_limit, through=None, confirmed=cursor
            )
        next_cursor = EventCursor(session_id=session_id, seq=items[-1].seq) if items else cursor
        return EventPage(items=items, next_cursor=next_cursor, has_more=has_more)

    # -- §4 L229-236: follow -----------------------------------------------

    def follow(
        self,
        session_id: str,
        after: EventCursor | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[LogItem]]:
        """Stream durable events plus exactly one ``Synced`` marker (§4 four steps).

        Entering the context registers the wakeup FIRST and only then
        captures H in a short read transaction (that read is the
        linearization point for the initial cursor validation and the
        catch-up window); the iterator then paginates ``after.seq < seq <=
        H``, yields one ``Synced(EventCursor(session_id, H))`` and continues
        strictly seq-increasing from the log, waking on bus notifications and
        polling at most every ``poll_interval`` (<= 1s) seconds. No session
        commit lock is ever held while catching up, yielding or waiting.
        """
        _require_session_id(session_id)
        cursor = _resolve_cursor(session_id, after)
        return _Follow(self, session_id, cursor)

    # -- internals ----------------------------------------------------------

    async def _log_high(self, connection: AsyncConnection, session_id: str) -> int:
        """H = COALESCE(MAX(events.seq), 0) over the session's committed rows."""
        row = (await connection.execute(_SELECT_HIGH, {"sid": session_id})).first()
        return int(row.h) if row is not None else 0

    async def _fetch_page(
        self,
        connection: AsyncConnection,
        session_id: str,
        after_seq: int,
        limit: int,
        *,
        through: int | None,
        confirmed: EventCursor,
    ) -> tuple[list[DurableEvent[Any]], bool]:
        """Fetch + decode one page in one short read transaction (fail closed).

        One extra row is fetched as the has_more probe. Every fetched row --
        the probe included -- is decoded by original seq in order; the FIRST
        row that fails to decode fails the whole page as StorageCorrupt with
        the bad row located and ``confirmed`` as last_confirmed_cursor, so no
        half page is returned and no cursor advances (§4 L227).
        """
        if through is None:
            rows = (
                await connection.execute(_SELECT_PAGE, {"sid": session_id, "after": after_seq, "limit": limit + 1})
            ).fetchall()
        else:
            rows = (
                await connection.execute(
                    _SELECT_PAGE_THROUGH,
                    {"sid": session_id, "after": after_seq, "through": through, "limit": limit + 1},
                )
            ).fetchall()
        decoded: list[DurableEvent[Any]] = []
        for row in rows:
            try:
                decoded.append(self._decode_row(row))
            except (EventDecodeError, ValueError, TypeError) as error:
                raise self._page_corrupt(session_id, row, confirmed) from error
        has_more = len(decoded) > limit
        if has_more:
            decoded = decoded[:limit]
        return decoded, has_more

    def _decode_row(self, row: Any) -> DurableEvent[Any]:
        """Decode one stored events row into a typed DurableEvent (fail closed).

        Mirrors the store writer's private ``TransactionWriter._decode_stored``
        over the same ``_json_column``/``from_epoch_ms`` helpers so both decode
        paths agree byte for byte; unknown type/version reaches here as
        EventDecodeError from the registry (never skipped, T-A-002).
        """
        envelope: dict[str, Any] = {
            "id": row.id,
            "type": row.type,
            "version": int(row.version),
            "session_id": row.session_id,
            "created_at": from_epoch_ms(int(row.created)),
            "data": _json_column(row.data),
            "metadata": _json_column(row.metadata),
            "seq": int(row.seq),
        }
        return self._registry.decode_durable(envelope)

    def _page_corrupt(self, session_id: str, row: Any, confirmed: EventCursor) -> StorageCorruptError:
        """Whole-page StorageCorrupt over already-stored rows (§4 L227, sanitized)."""
        return StorageCorruptError(
            f"stored event row failed to decode for session {session_id!r}",
            isolate=True,
            details={
                "reason": REASON_STORED_DECODE_FAILED,
                "session_id": session_id,
                "first_bad_seq": int(row.seq),
                "first_bad_event_id": str(row.id),
                "last_confirmed_cursor": _cursor_json(confirmed),
            },
        )

    def _gate_error(self, session_id: str, confirmed: EventCursor) -> StorageCorruptError:
        """read/follow on a read_only_diagnostic session (§4 L227, no bypass)."""
        return StorageCorruptError(
            f"session {session_id} is read_only_diagnostic; typed log reads fail closed",
            isolate=True,
            details={
                "reason": REASON_SESSION_READ_ONLY,
                "session_id": session_id,
                "status": STATUS_READ_ONLY_DIAGNOSTIC,
                "last_confirmed_cursor": _cursor_json(confirmed),
            },
        )

    def _non_increasing_error(
        self, session_id: str, event: DurableEvent[Any], confirmed: EventCursor
    ) -> StorageCorruptError:
        """Guarantee ④ (strictly increasing seq per follow connection), fail closed."""
        return StorageCorruptError(
            f"event seq {event.seq} does not increase past {confirmed.seq} on this follow connection",
            isolate=True,
            details={
                "reason": REASON_NON_INCREASING_SEQ,
                "session_id": session_id,
                "first_bad_seq": event.seq,
                "first_bad_event_id": event.id,
                "last_confirmed_cursor": _cursor_json(confirmed),
            },
        )


class _Follow(AbstractAsyncContextManager[AsyncIterator[LogItem]]):
    """One follow connection: async context manager + async iterator (§4).

    Entering runs the four-step preamble (wakeup registration first, then
    the short read transaction capturing H and validating the initial
    cursor); the iterator first yields the catch-up window plus one
    ``Synced``, then streams strictly seq-increasing events woken by the bus
    (single-slot ``asyncio.Event``: duplicate wakes merge) with a
    ``poll_interval``-bounded poll fallback. Leaving the context -- normally,
    on error, or on cancellation -- closes the stream and unregisters the
    wakeup. The bus is only ever a wake signal: the notification payload is
    never re-sent and correctness never depends on it (lost wakes degrade to
    the poll cadence, §4 L233 + r1-slices "丢通知补读").
    """

    def __init__(
        self,
        log: EventLog,
        session_id: str,
        cursor: EventCursor,
    ) -> None:
        self._log = log
        self._session_id = session_id
        self._cursor = cursor
        self._wake = asyncio.Event()
        self._entered = False
        self._stream: AsyncGenerator[LogItem, None] | None = None
        self._subscription: AbstractAsyncContextManager[AsyncIterator[Event]] | None = None
        self._events: AsyncIterator[Event] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._high = 0
        self._last_seq = cursor.seq

    async def __aenter__(self) -> AsyncIterator[LogItem]:
        if self._entered:
            raise RuntimeError("a follow context cannot be entered twice")
        self._entered = True
        # ① register the wakeup BEFORE capturing H: any commit that becomes
        # visible after the H read either is inside the catch-up window or
        # has set the wake -- the query range separates the two (§4 L231).
        await self._register_wakeup()
        try:
            record = await self._log._repos.session(self._session_id)
            async with self._log._engine.connect() as connection:
                high = await self._log._log_high(connection, self._session_id)
            if record is None and high == 0:
                raise NotFoundError(
                    f"session {self._session_id} does not exist", details={"session_id": self._session_id}
                )
            if record is not None and record.status == STATUS_READ_ONLY_DIAGNOSTIC:
                raise self._log._gate_error(self._session_id, self._cursor)
            if self._cursor.seq > high:
                raise InvalidCursorError(
                    f"cursor seq {self._cursor.seq} is beyond the current high watermark {high}",
                    details={
                        "session_id": self._session_id,
                        "seq": self._cursor.seq,
                        "high_watermark": high,
                        "reason": "cursor_seq_beyond_high_watermark",
                    },
                )
        except BaseException:
            # __aenter__ failures never reach __aexit__: undo the wakeup here.
            await self._release_wakeup()
            raise
        self._high = high
        self._last_seq = self._cursor.seq
        self._stream = self._run()
        return self

    def __aiter__(self) -> AsyncIterator[LogItem]:
        """The entered context object is itself the async iterator."""
        return self

    async def __anext__(self) -> LogItem:
        stream = self._stream
        if stream is None:
            if not self._entered:
                raise RuntimeError("the follow context must be entered before iteration")
            raise StopAsyncIteration
        return await stream.__anext__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            # Suspended-at-yield generators run their cleanup here; already
            # finished/never-started generators make aclose a no-op.
            with suppress(Exception):
                await stream.aclose()
        await self._release_wakeup()

    # -- wakeup plumbing ----------------------------------------------------

    async def _register_wakeup(self) -> None:
        """Subscribe on the injected bus and drain it into the single-slot wake."""
        bus = self._log._bus
        if bus is None:
            return  # no bus injected: the poll fallback is the only wake
        self._subscription = bus.subscribe(self._session_id)
        self._events = await self._subscription.__aenter__()
        self._drain_task = asyncio.create_task(self._drain(self._events))

    async def _release_wakeup(self) -> None:
        """Unregister the wakeup: cancel the drain, then leave the subscription."""
        task, self._drain_task = self._drain_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        subscription, self._subscription = self._subscription, None
        self._events = None
        if subscription is not None:
            with suppress(Exception):
                await subscription.__aexit__(None, None, None)

    async def _drain(self, events: AsyncIterator[Event]) -> None:
        """Best-effort wake pump: every bus event sets the single-slot wake.

        A drain failure (e.g. SlowConsumer disconnect) only costs wakeups,
        never correctness: the task sets one final wake and stops while the
        poll fallback keeps the stream complete -- the bus is a signal, not
        a data path (§4 L233).
        """
        try:
            async for _ in events:
                self._wake.set()
        except Exception:
            self._wake.set()

    # -- the stream (②③④) ---------------------------------------------------

    async def _run(self) -> AsyncGenerator[LogItem, None]:
        session_id = self._session_id
        log = self._log
        confirmed = EventCursor(session_id=session_id, seq=self._cursor.seq)

        # ② paginated catch-up: after.seq < seq <= H (clamped to the captured
        # window so the coming Synced(H) confirms exactly that window).
        while True:
            async with log._engine.connect() as connection:
                items, has_more = await log._fetch_page(
                    connection, session_id, self._last_seq, log._page_size, through=self._high, confirmed=confirmed
                )
            for event in items:
                if event.seq <= self._last_seq:
                    raise log._non_increasing_error(session_id, event, confirmed)
                self._last_seq = event.seq
                confirmed = EventCursor(session_id=session_id, seq=event.seq)
                yield event
            if not has_more:
                break
        yield Synced(cursor=EventCursor(session_id=session_id, seq=self._high))

        # ③ live phase: always query seq > last_seq; the bus is only a wake
        # signal (merged, single-slot) and the poll is the fallback bound.
        wake = self._wake
        while True:
            try:
                await asyncio.wait_for(wake.wait(), timeout=log._poll_interval)
            except TimeoutError:
                pass  # poll fallback: query the log even without any wake
            wake.clear()
            while True:
                async with log._engine.connect() as connection:
                    items, has_more = await log._fetch_page(
                        connection, session_id, self._last_seq, log._page_size, through=None, confirmed=confirmed
                    )
                for event in items:
                    if event.seq <= self._last_seq:
                        raise log._non_increasing_error(session_id, event, confirmed)
                    self._last_seq = event.seq
                    confirmed = EventCursor(session_id=session_id, seq=event.seq)
                    yield event
                if not has_more:
                    break
