"""Watcher W3 cancel/close full-path tests (p2-slices L165; CORE-CONTRACTS 6.4 L415, lifecycle face).

The cancel/close closeout segment, as pure characterization of the frozen
W1/W2 deliverables -- every path that can end a watch or a reload
lifetime:

- the direct task kills: a host loop teardown cancels the pump/merger
  tasks through the public ``watch_tasks`` face (zero leftovers either
  way; a cancelled pump ends the watch with a clean ``watch_error``
  because ``CancelledError`` is not an ``Exception``);
- ``stop`` under storm backpressure: events still flowing while the stop
  lands -> a clean closeout with zero leftovers;
- ``stop`` inside the merge window: the final-flush semantics (the
  service.py declaration face -- stop_event set and the raw queue empty
  -> the pending batch is EMITTED, then the merger returns; the flush has
  already happened when ``stop()`` returns);
- stop-before-start: never poisons a later watch lifetime (the same
  instance still starts and delivers);
- double ``stop`` / double ``__aexit__``: idempotent, never a raise;
- post-stop residual drain: the host still consumes whatever landed in
  the notification queue before the stop;
- the W2 orchestrator extension: a host cancel during an in-flight reload
  interrupts the three-phase closure -> the lock is released, the regime
  is unchanged and the orchestrator stays usable; and a ``close``
  coexists with a pending idle check plus an in-flight reload -- the gate
  refuses only NEW commands and never interrupts admitted work.

Every test asserts the 6.4 L415 red line: cancel/close reclaims the watch
tasks -- ``asyncio.all_tasks()`` shows zero pending watcher-*/reload-*
tasks (the W1 lifecycle / W2 reclaim assertion form, extended).

Platform face (the W3 honest disclosure): network filesystems (NFS/SMB/
network drives and similar unreliable filesystems) are NOT claimed
supported -- only local, already-tested filesystems are covered
(CORE-CONTRACTS 6.4 L415 / M1 L53 wording).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode.config.loader import ResolvedRuntime
from opencode.storage import ReadRepositories, open_database
from opencode.watcher import ChangeNotification, WatcherService
from opencode.watcher.reload import ReloadOrchestrator
from tests.integration.test_projector import _require_success, _upgrade_head
from tests.integration.test_watcher_reclaim import _composition_tasks, _FakeClock, _resolve_cli

_ARM_S = 0.25
_TARGET = "ses_0000000000000000000000000099"


async def test_stop_during_storm_backpressure_reclaims_cleanly(tmp_path: Path) -> None:
    """Stop lands mid-flow while the queue is saturated: a clean closeout, zero leftovers."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=30, merge_window_s=0.05, queue_size=2)
    await service.start()
    await asyncio.sleep(_ARM_S)
    for i in range(12):  # sustained storm; the slow consumer never drains
        (root / f"storm_{i:02d}.txt").write_text("x", encoding="utf-8")
        await asyncio.sleep(0.3)
    assert service.notifications.qsize() <= 2, "real backpressure: the queue is saturated"
    assert service.dropped_notifications >= 1, "the storm demonstrably overflowed the host queue"
    assert service.is_running

    (root / "during_stop.txt").write_text("x", encoding="utf-8")  # still flowing as stop lands
    await service.stop()
    residual = service.notifications.qsize()

    (root / "after_stop.txt").write_text("x", encoding="utf-8")  # races nothing: the watch is gone
    await asyncio.sleep(0.5)
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _composition_tasks() == []
    assert service.notifications.qsize() == residual, "a stopped watch never delivers again"


async def test_stop_inside_the_merge_window_flushes_the_pending_batch(tmp_path: Path) -> None:
    """Final-flush semantics: a stop inside the merge window emits the pending batch before returning."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=3.0)
    await service.start()
    await asyncio.sleep(_ARM_S)
    target = root / "pending.txt"
    target.write_text("x", encoding="utf-8")
    await asyncio.sleep(1.0)  # the batch reached the merger; it is parked inside its 3.0 s window
    await service.stop()

    # The merger emitted the pending batch before returning (stop_event set
    # and the raw queue empty -> emit, then return), and stop() awaited it:
    # the flushed notification is already in the queue, no waiting needed.
    notification = service.notifications.get_nowait()
    assert target in notification.paths
    assert notification.event_types
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _composition_tasks() == []


async def test_pump_task_direct_cancel_reclaims_cleanly(tmp_path: Path) -> None:
    """Direct kill of the pump task: the watch ends clean, stop() still reclaims the merger."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.1)
    await service.start()
    await asyncio.sleep(_ARM_S)
    target = root / "before_kill.txt"
    target.write_text("x", encoding="utf-8")
    notification = await asyncio.wait_for(service.notifications.get(), 10.0)
    assert target in notification.paths, "the watch was demonstrably live before the kill"

    pump = next(task for task in service.watch_tasks if task.get_name() == "watcher-pump")
    pump.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pump

    assert not service.is_running
    assert service.watch_error is None, "CancelledError is not an Exception: the terminal face stays clean"
    await service.stop()
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _composition_tasks() == []


async def test_merger_task_direct_cancel_reclaims_cleanly(tmp_path: Path) -> None:
    """Direct kill of the merger task: the pump survives, further changes are honestly not delivered."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.1)
    await service.start()
    await asyncio.sleep(_ARM_S)
    target = root / "before_kill.txt"
    target.write_text("x", encoding="utf-8")
    notification = await asyncio.wait_for(service.notifications.get(), 10.0)
    assert target in notification.paths, "the watch was demonstrably live before the kill"

    merger = next(task for task in service.watch_tasks if task.get_name() == "watcher-merge")
    merger.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await merger
    assert service.is_running, "the pump face survives the merge consumer's death"

    (root / "orphaned.txt").write_text("x", encoding="utf-8")  # honest loss: no consumer, no delivery
    await asyncio.sleep(0.3)
    assert service.notifications.qsize() == 0

    await service.stop()
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _composition_tasks() == []


async def test_stop_before_start_never_poisons_a_later_watch_lifetime(tmp_path: Path) -> None:
    """The service.py declaration face: a stop before any start leaves the lifetime usable."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.1)
    await service.stop()  # before any start: a no-op that must not poison
    await service.start()
    await asyncio.sleep(_ARM_S)
    target = root / "later.txt"
    target.write_text("x", encoding="utf-8")
    notification = await asyncio.wait_for(service.notifications.get(), 10.0)
    assert target in notification.paths, "the later watch lifetime delivers normally"
    await service.stop()
    assert not service.is_running
    assert _composition_tasks() == []


async def test_double_stop_and_double_aexit_are_idempotent(tmp_path: Path) -> None:
    """Idempotency: double stop, double __aexit__, and a context exit after a manual stop never raise."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.1)
    await service.start()
    await asyncio.sleep(_ARM_S)
    await service.stop()
    await service.stop()
    await service.__aexit__(None, None, None)
    await service.__aexit__(None, None, None)
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _composition_tasks() == []

    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.1) as inner:
        await inner.stop()  # the manual stop inside the block: the exit re-stop is a no-op
    assert not inner.is_running
    assert _composition_tasks() == []


async def test_residual_notifications_stay_drainable_after_stop(tmp_path: Path) -> None:
    """Post-stop drain: what landed before the stop stays consumable by the host afterwards."""
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.05)
    await service.start()
    await asyncio.sleep(_ARM_S)
    (root / "residual.txt").write_text("x", encoding="utf-8")
    await asyncio.sleep(1.0)  # the notification landed; the host never consumed it
    assert service.notifications.qsize() == 1
    await service.stop()

    drained: list[ChangeNotification] = []
    while service.notifications.qsize():
        drained.append(service.notifications.get_nowait())
    assert len(drained) == 1
    assert (root / "residual.txt") in drained[0].paths
    assert _composition_tasks() == []


async def test_host_cancel_during_inflight_reload_releases_the_lock_and_keeps_the_regime(
    tmp_path: Path,
) -> None:
    """The interrupted three-phase closure: cancel releases the lock, keeps the regime, stays usable."""
    working = tmp_path / "watched"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    release = asyncio.Event()

    async def parked_resolve() -> ResolvedRuntime:
        await release.wait()
        return _resolve_cli(working, model="m-b")

    async with open_database(db_path, mode="create") as database:
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=parked_resolve,
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        task = asyncio.create_task(orchestrator.reload())
        await asyncio.sleep(0.2)  # the reload is parked inside the resolve closure
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # A cancel is not a close: the lock was released (check_idle acquires
        # it), the regime is untouched and the face stays open and usable.
        assert not orchestrator.is_closed
        assert orchestrator.current_runtime is initial
        assert len(orchestrator.regimes) == 1
        assert (await orchestrator.check_idle()).idle

        release.set()
        result = await orchestrator.reload()
        assert result.applied
        assert result.runtime is not None
        assert result.runtime.snapshot.model.model == "m-b"
        assert result.boot_id == orchestrator.boot_id
        assert len(orchestrator.regimes) == 2
    assert _composition_tasks() == []


async def test_close_coexists_with_a_pending_idle_check_and_an_inflight_reload(tmp_path: Path) -> None:
    """Close is a gate, not an interrupt: admitted work closes out; only NEW commands are refused."""
    working = tmp_path / "watched"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    release = asyncio.Event()

    async def parked_resolve() -> ResolvedRuntime:
        await release.wait()
        return _resolve_cli(working, model="m-b")

    async with open_database(db_path, mode="create") as database:
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=parked_resolve,
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        reload_task = asyncio.create_task(orchestrator.reload())  # holds the lock, parked in resolve
        await asyncio.sleep(0.2)
        idle_task = asyncio.create_task(orchestrator.check_idle())  # admitted, waiting on the lock
        await asyncio.sleep(0.2)

        await orchestrator.close()  # the gate lands while both are pending
        assert orchestrator.is_closed

        release.set()
        result = await reload_task  # the in-flight reload closes out, never interrupted
        observation = await idle_task  # the pending idle check completes, never refused
        assert result.applied
        assert result.runtime is not None
        assert result.runtime.snapshot.model.model == "m-b"
        assert observation.idle

        # The closed gate refuses only NEW commands; a double close is a no-op.
        with pytest.raises(RuntimeError, match="closed"):
            await orchestrator.reload()
        with pytest.raises(RuntimeError, match="closed"):
            await orchestrator.check_idle()
        await orchestrator.close()
        assert orchestrator.is_closed
        assert len(orchestrator.regimes) == 2, "the admitted reload applied before the gate took effect"
        assert result.boot_id == orchestrator.boot_id
    assert _composition_tasks() == []
