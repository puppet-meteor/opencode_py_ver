"""Automation scheduler: the leader loop (T-C3-003).

Upstream 2.3.2 ``automation.ts`` run-loop slice, simplified for the
first period (no event watches to reconcile): claim the lease, tick,
sleep until the next wake (doorbell or the earliest due time, capped at
``max_sleep_s``). A follower -- another process holding the lease --
sleeps a full ``max_sleep_s`` and retries; ownership decays with the TTL,
so a crashed leader never wedges the table.

All cadence parameters are constructor-injectable (repo discipline).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode.automation.service import AutomationService


class AutomationScheduler:
    """Owns timer ticks until stopped; one lease-elected scheduler per database."""

    def __init__(
        self,
        *,
        service: AutomationService,
        max_sleep_s: float = 30.0,
        leader_ttl_s: float | None = None,
    ) -> None:
        self._service = service
        self._max_sleep_s = max_sleep_s
        self._leader_ttl_ms = None if leader_ttl_s is None else int(leader_ttl_s * 1000)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> AutomationScheduler:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.stop()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

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
