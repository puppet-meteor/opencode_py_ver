"""The notify-only watcher service over watchfiles ``awatch`` (P2-S20 M2 watcher W1).

CORE-CONTRACTS 6.4 ruling 1 (T-A-016, merged #193) makes this whole module a
host-face notification signal: it never imports the event bus, the durable
log, config snapshots, the approval face or any other opencode module, so it
is structurally incapable of producing a state change. A lost notification
is harmless — the next change or an explicit reload re-supplies it.

Dedup/merge is two-layered:

- library level: the watchfiles ``debounce`` window groups a burst of raw
  filesystem events into one batch (N raw events -> one yielded set);
- Python level: every batch arriving inside one quiet window of
  ``merge_window_s`` after the first pending batch collapses into a single
  :class:`ChangeNotification` for the host (N batches -> one queue entry).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from watchfiles import Change, awatch

__all__ = ["ChangeNotification", "WatcherService"]

#: Library-level debounce window in milliseconds: how long watchfiles may
#: group a burst of raw filesystem events over before yielding one batch.
DEFAULT_DEBOUNCE_MS = 250

#: Watchfiles sub-window in milliseconds: yield a group early once no new
#: raw events arrived for this long (bounds single-change latency).
_LIB_STEP_MS = 50

#: Python-side merge window in seconds: every batch arriving inside one
#: quiet window of this length after the first pending batch collapses into
#: a single host notification (N changes -> 1 signal).
DEFAULT_MERGE_WINDOW_S = 0.5

#: Bound of the host-facing notification queue: a slow host gets a bounded
#: backlog — an overflowing notification is dropped (harmless per 6.4) and
#: counted in :attr:`WatcherService.dropped_notifications`.
DEFAULT_QUEUE_SIZE = 64

#: Bound of the internal raw-batch queue between the pump and the merger.
_MAX_RAW_BATCHES = 256

#: How long ``stop`` waits for the graceful stop_event path before falling
#: back to cancelling the tasks (zero leftovers either way).
_STOP_GRACE_S = 5.0

#: Idle poll of the merger in seconds: the only cost of the graceful,
#: sentinel-free shutdown path.
_IDLE_POLL_S = 0.05


@dataclass(frozen=True, slots=True)
class ChangeNotification:
    """One merged, deduplicated change record — pure data, host-only signal.

    ``paths``/``event_types`` are the union over every raw batch merged into
    this notification. The payload is deliberately plain data: CORE-CONTRACTS
    6.4 ruling 1 keeps it out of the py.* event registry, the durable log and
    every projection path — the host embedder is the sole consumer.
    """

    paths: frozenset[Path]
    event_types: frozenset[str]


class WatcherService:
    """Notify-only file watcher over watchfiles ``awatch`` (host face, W1).

    Lifecycle: async context manager — ``start`` spawns the pump+merger
    tasks, ``stop``/``__aexit__`` reap them (idempotent, zero leftover
    tasks). The host consumes :attr:`notifications`, a bounded
    ``asyncio.Queue[ChangeNotification]``; overflow drops (harmless per 6.4).
    Watch failures never raise into the host loop — the watch ends and
    :attr:`watch_error` carries the cause.
    """

    def __init__(
        self,
        roots: Sequence[Path],
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        merge_window_s: float = DEFAULT_MERGE_WINDOW_S,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        roots_tuple = tuple(Path(root) for root in roots)
        if not roots_tuple:
            raise ValueError("at least one root path is required")
        if debounce_ms < 0:
            raise ValueError("debounce_ms must be >= 0")
        if merge_window_s <= 0:
            raise ValueError("merge_window_s must be > 0")
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self._roots = roots_tuple
        self._debounce_ms = debounce_ms
        self._merge_window_s = merge_window_s
        self._notifications: asyncio.Queue[ChangeNotification] = asyncio.Queue(maxsize=queue_size)
        self._raw_batches: asyncio.Queue[set[tuple[Change, str]]] = asyncio.Queue(maxsize=_MAX_RAW_BATCHES)
        self._stop_event = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._merger_task: asyncio.Task[None] | None = None
        self._dropped_notifications = 0
        self._dropped_batches = 0
        self._started = False
        self._watch_error: BaseException | None = None

    # -- public face ---------------------------------------------------------

    @property
    def roots(self) -> tuple[Path, ...]:
        """The watched roots (host-injected session roots / config sources)."""
        return self._roots

    @property
    def notifications(self) -> asyncio.Queue[ChangeNotification]:
        """The bounded host-facing queue of merged change notifications."""
        return self._notifications

    @property
    def dropped_notifications(self) -> int:
        """Notifications dropped because the host queue was full (harmless)."""
        return self._dropped_notifications

    @property
    def dropped_batches(self) -> int:
        """Raw batches dropped because the internal queue was full."""
        return self._dropped_batches

    @property
    def watch_error(self) -> BaseException | None:
        """The terminal watch failure if the pump died, else ``None``."""
        return self._watch_error

    @property
    def is_running(self) -> bool:
        """True between a successful ``start`` and the completion of the pump task."""
        return self._pump_task is not None and not self._pump_task.done()

    @property
    def watch_tasks(self) -> tuple[asyncio.Task[None], ...]:
        """The pump+merger tasks (diagnostics / reclamation assertions)."""
        return tuple(task for task in (self._pump_task, self._merger_task) if task is not None)

    # -- lifecycle -----------------------------------------------------------

    async def __aenter__(self) -> WatcherService:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        """Spawn the watch (pump) and merge tasks over the roots."""
        if self._started:
            raise RuntimeError("WatcherService.start() is not reentrant; use one instance per watch lifetime")
        self._started = True
        self._pump_task = asyncio.create_task(self._pump(), name="watcher-pump")
        self._merger_task = asyncio.create_task(self._merge(), name="watcher-merge")

    async def stop(self) -> None:
        """Stop watching and reap both tasks — idempotent, zero leftovers.

        The graceful path is the watchfiles ``stop_event`` (the rust watch
        honors it promptly); if any task outlives the grace window it is
        cancelled and awaited, so no background task ever survives a stop.
        Idempotency is derived from live task state, so a stop before any
        start never poisons a later watch lifetime.
        """
        tasks = [task for task in (self._pump_task, self._merger_task) if task is not None and not task.done()]
        if not tasks:
            return  # never started, or already fully stopped
        self._stop_event.set()
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), _STOP_GRACE_S)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals -----------------------------------------------------------

    async def _pump(self) -> None:
        """Feed watchfiles batches into the bounded raw queue; never raise into the host loop."""
        try:
            async for changes in awatch(
                *self._roots,
                debounce=self._debounce_ms,
                step=_LIB_STEP_MS,
                stop_event=self._stop_event,
            ):
                if not changes:
                    continue
                try:
                    self._raw_batches.put_nowait(changes)
                except asyncio.QueueFull:
                    self._dropped_batches += 1  # raw overflow is harmless: the next batch re-supplies
        except Exception as error:
            self._watch_error = error

    async def _merge(self) -> None:
        """Collapse every batch inside one quiet window into a single notification."""
        pending_paths: set[Path] = set()
        pending_types: set[str] = set()
        while True:
            if self._stop_event.is_set() and self._raw_batches.empty():
                self._emit(pending_paths, pending_types)
                return
            has_pending = bool(pending_paths)
            timeout = self._merge_window_s if has_pending else _IDLE_POLL_S
            try:
                batch = await asyncio.wait_for(self._raw_batches.get(), timeout)
            except TimeoutError:
                if has_pending:
                    self._emit(pending_paths, pending_types)
                    pending_paths = set()
                    pending_types = set()
                continue
            for change, raw_path in batch:
                pending_paths.add(Path(raw_path))
                pending_types.add(change.name.lower())

    def _emit(self, pending_paths: Iterable[Path], pending_types: Iterable[str]) -> None:
        if not pending_paths:
            return
        notification = ChangeNotification(paths=frozenset(pending_paths), event_types=frozenset(pending_types))
        try:
            self._notifications.put_nowait(notification)
        except asyncio.QueueFull:
            self._dropped_notifications += 1  # host loss is harmless per 6.4: the next change re-supplies
