"""T-C3-016 contract set: ``AutomationScheduler`` owns its task to the end.

``stop()`` cancels the loop task; ``__aexit__`` then waits for it, bounded by
``stop_timeout_s``. Before T-C3-016 ``__aexit__`` looked the task up AFTER
``stop()`` had cleared the handle, so the await was dead code and every
scheduler task outlived its owner -- through the engine dispose, into the
``asyncio.run()`` shutdown wave. A task cancelled mid-DBAPI-call is exactly
the one that wave turns into the aiosqlite #403 orphan
(tests/integration/test_storage_stop_once.py pins the storage side).

The contract, one test per clause:

- exit waits for the cancelled task to finish its teardown (ownership);
- exit gives up after ``stop_timeout_s`` when the task absorbs its cancel
  (bounded: never a dead await);
- a real exception from the task surfaces at exit instead of dying with an
  unretrieved "Task exception was never retrieved";
- a cancel that reaches the OWNING task is never swallowed.

``claim_leader`` is the loop's first await, so the task is always parked
there when a test stops the scheduler. No wall-clock assertion: the budgets
below only turn a regression into a named failure.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opencode.automation.scheduler import AutomationScheduler

_NAME = "automation-scheduler"


class _Service:
    """The faces ``AutomationScheduler._run`` touches, with a scripted claim."""

    def __init__(self, *, teardown_s: float = 0.0) -> None:
        self.entered = asyncio.Event()
        self.teardown_s = teardown_s
        self.cleanup_done = asyncio.Event()
        self.cancels_seen = 0

    def _now(self) -> int:
        return 0

    async def claim_leader(self, now_ms: int | None = None, ttl_ms: int | None = None) -> bool:
        self.entered.set()
        try:
            await asyncio.sleep(3600)  # parked until the scheduler is stopped
        except asyncio.CancelledError:
            self.cancels_seen += 1
            await asyncio.sleep(self.teardown_s)  # the stand-in for SQLAlchemy's graceful close
            self.cleanup_done.set()
            raise
        return False

    async def tick(self) -> int:
        return 0

    async def reconcile_event_watches(self) -> None:
        return None

    async def next_wake_after(self, now_ms: int) -> int | None:
        return None

    async def doorbell_wait(self, timeout_s: float) -> None:
        await asyncio.sleep(3600)


class _AbsorbingService(_Service):
    """The pathological tenant: ``claim_leader`` absorbs every cancel."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def claim_leader(self, now_ms: int | None = None, ttl_ms: int | None = None) -> bool:
        self.entered.set()
        while not self.release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancels_seen += 1
        raise asyncio.CancelledError


class _CrashingService(_Service):
    async def claim_leader(self, now_ms: int | None = None, ttl_ms: int | None = None) -> bool:
        self.entered.set()
        raise RuntimeError("lease table is gone")


def _scheduler(service: Any, **options: Any) -> AutomationScheduler:
    return AutomationScheduler(service=service, **options)


async def _reap_scheduler_tasks() -> None:
    """Red-state safety net: no scheduler task is left for the loop teardown."""
    tasks = [task for task in asyncio.all_tasks() if task.get_name() == _NAME]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_exit_waits_for_the_cancelled_task_to_finish_its_teardown() -> None:
    service = _Service(teardown_s=0.05)
    try:
        async with _scheduler(service):
            await service.entered.wait()
        assert service.cleanup_done.is_set(), "the scheduler task outlived the async-with block"
    finally:
        await _reap_scheduler_tasks()


async def test_exit_gives_up_after_stop_timeout_when_the_task_absorbs_its_cancel() -> None:
    service = _AbsorbingService()

    async def scenario() -> None:
        async with _scheduler(service, stop_timeout_s=0.5):
            await service.entered.wait()

    try:
        # A regression to "wait forever" fails here instead of hanging CI.
        await asyncio.wait_for(scenario(), timeout=10.0)
        assert service.cancels_seen >= 1, "exit did not give the task time to take its cancel"
    finally:
        service.release.set()
        await _reap_scheduler_tasks()


async def test_exit_surfaces_a_crashed_task() -> None:
    service = _CrashingService()
    try:
        with pytest.raises(RuntimeError, match="lease table is gone"):
            async with _scheduler(service):
                await service.entered.wait()
    finally:
        await _reap_scheduler_tasks()


async def test_exit_never_swallows_a_cancel_that_reaches_the_owner() -> None:
    service = _Service(teardown_s=0.3)

    async def owner() -> None:
        async with _scheduler(service):
            await asyncio.sleep(3600)  # the body; cancelled from outside

    owner_task = asyncio.create_task(owner(), name="owner")
    try:
        await service.entered.wait()
        owner_task.cancel()  # the body's cancel: exit starts waiting on the task's teardown
        await asyncio.sleep(0)
        assert owner_task.cancel(), "the owner had already left __aexit__: it never waited for the task"
        with pytest.raises(asyncio.CancelledError):
            await owner_task
        # The shield kept the task's own teardown out of the owner's cancel.
        await asyncio.wait_for(service.cleanup_done.wait(), timeout=10.0)
    finally:
        await _reap_scheduler_tasks()
