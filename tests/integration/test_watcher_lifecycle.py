"""Watcher W1 lifecycle tests: spike criterion 2 — stop/cancel reclaims the watch tasks.

PHASE2-MODULES 7 discipline: a stopped/closed/cancelled watch leaves zero
background tasks. ``stop`` is idempotent (double stop, stop-before-start),
``start`` is not reentrant, the async context manager reaps on exit, and a
cancelled host coroutine still reclaims through ``__aexit__``.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from opencode.watcher import WatcherService

_ARM_S = 0.25


def _watcher_tasks() -> list[asyncio.Task[object]]:
    """Every still-pending task the watcher package created in this loop."""
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("watcher-")]


async def test_stop_reclaims_the_watch_tasks(tmp_path: Path) -> None:
    """After stop the pump+merger tasks are done and zero watcher tasks remain pending."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.1)
    assert _watcher_tasks() == []
    await service.start()
    assert service.is_running
    assert len(service.watch_tasks) == 2
    await asyncio.sleep(_ARM_S)
    (root / "activity.txt").write_text("x", encoding="utf-8")
    notification = await asyncio.wait_for(service.notifications.get(), 5.0)
    assert root / "activity.txt" in notification.paths, "the watch was demonstrably live before stop"
    await service.stop()
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _watcher_tasks() == []


async def test_stop_is_idempotent_and_stop_before_start_is_a_noop(tmp_path: Path) -> None:
    """Double stop and stop-before-start never raise and never leak tasks."""
    service = WatcherService((tmp_path,), debounce_ms=50, merge_window_s=0.1)
    await service.stop()
    assert not service.is_running
    await service.start()
    await service.stop()
    await service.stop()
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)


async def test_start_is_not_reentrant(tmp_path: Path) -> None:
    """A second start on the same instance is refused (one watch per lifetime)."""
    service = WatcherService((tmp_path,), debounce_ms=50, merge_window_s=0.1)
    await service.start()
    with pytest.raises(RuntimeError, match="not reentrant"):
        await service.start()
    await service.stop()


async def test_empty_roots_are_rejected(tmp_path: Path) -> None:
    """A watch over nothing is a host bug: fail fast at construction."""
    del tmp_path
    with pytest.raises(ValueError, match="at least one root"):
        WatcherService(())


async def test_context_manager_reaps_on_exit(tmp_path: Path) -> None:
    """async with: running inside, fully reaped after the block."""
    root = tmp_path / "watched"
    root.mkdir()
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.1) as service:
        assert service.is_running
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _watcher_tasks() == []


async def test_host_task_cancellation_still_reclaims(tmp_path: Path) -> None:
    """Cancellation face: a cancelled host coroutine unwinds through __aexit__ and reaps."""

    async def host() -> None:
        async with WatcherService((tmp_path,), debounce_ms=50, merge_window_s=0.1):
            await asyncio.sleep(30)

    task = asyncio.create_task(host())
    await asyncio.sleep(0.4)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert _watcher_tasks() == []
