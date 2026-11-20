"""SharedCache: the upstream ``core/shared-cache.ts`` single-flight cache.

A generic async cache for values expensive to load (upstream consumers:
provider model-list refreshes; the py consumers arrive on later tickets --
the module lands now as the ready base). Semantics, verbatim:

- fresh hit (age < ``interval_s``, not ``force``) returns the stored
  result directly -- a stored FAILURE is re-raised on fresh hits;
- otherwise every concurrent ``get`` for one key joins the single running
  load (per-key single flight; the load retries TRANSIENT failures with
  the injectable ``retry_delays_s`` -- attempts = 1 + ``len(retry_delays_s)``,
  matching Effect ``times: 3`` with the exponential (1, 2, 4) default);
- an exhausted or non-transient failure keeps the LAST GOOD value and
  serves it stale (refreshing its clock for one interval -- no retry
  storm), or without one raises to the callers;
- a cancelled load settles nothing, and a load whose every joiner
  cancelled is itself cancelled (no orphan loads pinning the slot -- the
  recorded py deviation: upstream loads run detached in a managed fiber
  set interrupted at scope close; py owns the load task here);
- each ``get`` sweeps entries unread for longer than
  ``max(24h, 2 * interval)`` -- a caller refreshing less often than daily
  never finds its entry dropped between two refreshes.

``fingerprint`` is the short one-way stand-in for secrets in cache keys;
``transient`` classifies retryable failures (timeouts, transport errors,
HTTP 408/429/500/502/503/504 -- the Effect ``retryTransient`` reading).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar, cast

import httpx

#: How long a result stays fresh unless a caller passes its own.
INTERVAL_S = 600.0

#: Entries nobody asked for in 24 hours are dropped.
UNREAD_MS = 24 * 3600 * 1000

_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0)

_T = TypeVar("_T")


def fingerprint(secret: str) -> str:
    """A short, one-way stand-in for a secret in a cache key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def transient(error: BaseException | httpx.Response) -> bool:
    """Timeouts, transport errors and the retryable HTTP status codes."""
    if isinstance(error, httpx.Response):
        return error.status_code in _TRANSIENT_STATUS
    return isinstance(error, httpx.TransportError)


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


class _Entry:
    __slots__ = ("last_error", "last_time", "last_value", "loading", "read", "unread", "waiters")

    def __init__(self, *, read: float, unread: float) -> None:
        self.last_value: object = _MISSING
        self.last_error: BaseException | None = None
        self.last_time: float | None = None
        self.loading: asyncio.Task[Any] | None = None
        self.waiters = 0
        self.read = read
        self.unread = unread


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


class SharedCache:
    """Per-key single-flight async cache; one instance per process."""

    def __init__(
        self,
        *,
        interval_s: float = INTERVAL_S,
        retry_delays_s: Sequence[float] = _RETRY_DELAYS_S,
        now_ms: Callable[[], float] | None = None,
    ) -> None:
        self._interval_ms = interval_s * 1000.0
        self._retry_delays_s = tuple(float(delay) for delay in retry_delays_s)
        self._now_ms = now_ms or _monotonic_ms
        self._entries: dict[str, _Entry] = {}

    async def get(
        self,
        key: str,
        load: Callable[[], Awaitable[_T]],
        *,
        interval_s: float | None = None,
        force: bool = False,
    ) -> _T:
        """The last result for ``key`` while fresh, else one shared load.

        ``load`` is a zero-argument awaitable factory; ``interval_s``
        overrides the constructor freshness for this key; ``force`` skips
        the age check but still joins a running load.
        """
        now = self._now_ms()
        age_ms = self._interval_ms if interval_s is None else interval_s * 1000.0
        for stale_key in [
            name for name, entry in self._entries.items() if entry.loading is None and now - entry.read > entry.unread
        ]:
            del self._entries[stale_key]
        entry = self._entries.setdefault(key, _Entry(read=now, unread=float(UNREAD_MS)))
        entry.read = now
        entry.unread = max(entry.unread, 2.0 * age_ms)
        last_time = entry.last_time
        if not force and last_time is not None and now - last_time < age_ms:
            if entry.last_error is not None:
                raise entry.last_error
            return cast(_T, entry.last_value)
        if entry.loading is None:
            entry.loading = asyncio.create_task(self._run_load(entry, load, started_ms=now))
        task = entry.loading
        entry.waiters += 1
        try:
            return await asyncio.shield(task)
        finally:
            entry.waiters -= 1
            if entry.waiters <= 0 and entry.loading is task and not task.done():
                # every joiner left: no orphan load pins the slot
                entry.loading = None
                task.cancel()

    async def _run_load(self, entry: _Entry, load: Callable[[], Awaitable[_T]], *, started_ms: float) -> _T:
        current = asyncio.current_task()
        delays = self._retry_delays_s
        try:
            for attempt in range(len(delays) + 1):
                try:
                    value = await load()
                except asyncio.CancelledError:
                    raise  # an interrupted load settles nothing
                except BaseException as error:  # narrowed below: re-raised or kept as last error
                    if attempt < len(delays) and transient(error):
                        await asyncio.sleep(delays[attempt])
                        continue
                    if entry.last_value is not _MISSING:
                        # keep the last good value, refreshing its clock
                        entry.last_error = None
                        entry.last_time = started_ms
                        return cast(_T, entry.last_value)
                    entry.last_value = _MISSING
                    entry.last_error = error
                    entry.last_time = started_ms
                    raise
                entry.last_value = value
                entry.last_error = None
                entry.last_time = started_ms
                return value
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            if entry.loading is current:
                entry.loading = None
