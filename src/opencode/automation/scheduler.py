"""Automation scheduler: the leader loop (T-C3-003).

Upstream 2.3.2 ``automation.ts`` run-loop slice, simplified for the
first period (timer ticks; T-C3-004 adds the event-watch reconcile):
claim the lease, tick, reconcile event watches, sleep until the next
wake (doorbell or the earliest due time, capped at ``max_sleep_s``). A follower -- another process holding the lease --
sleeps a full ``max_sleep_s`` and retries; ownership decays with the TTL,
so a crashed leader never wedges the table.

All cadence parameters are constructor-injectable (repo discipline).

Ownership (T-C3-016): ``stop()`` cancels the task and ``__aexit__`` then
waits for it, bounded by ``stop_timeout_s``, so its DB work -- and
SQLAlchemy's graceful close of a connection cancelled mid-call -- finishes
while the loop and the database are still alive. A scheduler task that
outlives its owner is what the ``asyncio.run()`` shutdown wave turned into
the aiosqlite #403 orphan (see ``opencode.storage.db._make_stop_idempotent``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode.automation.service import AutomationService


class AutomationScheduler:
    """Owns timer ticks until stopped; one lease-elected scheduler per database.

    ``stop_timeout_s`` bounds the exit wait: twice the storage busy timeout
    (5 s), i.e. the longest an in-flight SQLite call can hold the cancelled
    task before its own teardown. A task still running after that keeps its
    cancel and finishes on its own.
    """

    def __init__(
        self,
        *,
        service: AutomationService,
        max_sleep_s: float = 30.0,
        leader_ttl_s: float | None = None,
        stop_timeout_s: float = 10.0,
    ) -> None:
        self._service = service
        self._max_sleep_s = max_sleep_s
        self._leader_ttl_ms = None if leader_ttl_s is None else int(leader_ttl_s * 1000)
        self._stop_timeout_s = stop_timeout_s
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> AutomationScheduler:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        # The handle is taken BEFORE stop() clears it; ``start()`` called while
        # we wait installs a new task that is not ours to await.
        task = self._task
        self.stop()
        if task is None:
            return
        owner = asyncio.current_task()
        cancelling = owner.cancelling() if owner is not None else 0
        try:
            await asyncio.wait_for(asyncio.shield(task), self._stop_timeout_s)
        except TimeoutError:
            pass  # bounded: the task keeps its cancel and finishes on its own
        except asyncio.CancelledError:
            # The awaited task ends cancelled on every stop; that one is
            # expected. A cancel that reached the OWNING task is not ours to
            # swallow -- a stale count from before the wait does not count.
            if owner is None or owner.cancelling() != cancelling:
                raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="automation-scheduler")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def wake(self) -> None:
        self._service.wake()

    async def _run(self) -> None:
        while True:
            now = self._service._now()
            claimed = await self._service.claim_leader(now, ttl_ms=self._leader_ttl_ms)
            if claimed:
                try:
                    await self._service.tick()
                    # T-C3-004: the leader also reconciles event watches --
                    # active event rows gain a watcher, strays are cancelled.
                    await self._service.reconcile_event_watches()
                except Exception:
                    pass
                now = self._service._now()
                next_due = await self._service.next_wake_after(now)
                timeout = self._max_sleep_s
                if next_due is not None:
                    timeout = min(self._max_sleep_s, max(0.0, (next_due - now) / 1000))
            else:
                timeout = self._max_sleep_s
            await self._service.doorbell_wait(timeout)
