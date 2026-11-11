"""Watcher W1 spike-segment integration tests: watchfiles criteria 1/3 (local face).

The p2-slices L174 spike criteria, carried into the test suite through the
real service (the pre-implementation library validation ran as a throwaway
scratch script; these pin the same criteria against the shipped module):

- criterion 1: a real event round trip (a tmp-dir write reaches the host
  notification within a reasonable window) and merge/dedup (N rapid writes
  inside one quiet window collapse into exactly one notification);
- criterion 3: a 300-file storm stays bounded (notification count bounded,
  the bounded queue never grows past its cap, nothing hangs) and a full
  queue drops instead of growing (a lost notification is harmless per
  CORE-CONTRACTS 6.4 ruling 1).

Platform face: local Windows is proven here; CI Linux/macOS carry the same
suite (dispatcher CI-WATCH closes the dual-OS record). Network filesystems
are out of scope (M1 L53: not claimed supported).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from opencode.watcher import ChangeNotification, WatcherService

_ARM_S = 0.25  #: settle window for the rust watcher to arm after start()


async def test_round_trip_single_write_notifies(tmp_path: Path) -> None:
    """Criterion 1: one real write reaches the host as one notification."""
    root = tmp_path / "watched"
    root.mkdir()
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.15) as service:
        await asyncio.sleep(_ARM_S)
        target = root / "roundtrip.txt"
        target.write_text("hello", encoding="utf-8")
        notification = await asyncio.wait_for(service.notifications.get(), 5.0)
    assert target in notification.paths
    assert notification.event_types
    assert notification.event_types <= {"added", "modified", "deleted"}


async def test_rapid_writes_merge_into_exactly_one_notification(tmp_path: Path) -> None:
    """Criterion 1: 20 rapid writes inside one quiet window -> one merged notification."""
    root = tmp_path / "watched"
    root.mkdir()
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.2) as service:
        await asyncio.sleep(_ARM_S)
        for i in range(20):
            (root / f"merge_{i:02d}.txt").write_text("x", encoding="utf-8")
            await asyncio.sleep(0.004)
        notification = await asyncio.wait_for(service.notifications.get(), 5.0)
        await asyncio.sleep(0.7)  # 3x the merge window: no further notification may arrive
        assert service.notifications.qsize() == 0
    expected = {root / f"merge_{i:02d}.txt" for i in range(20)}
    assert notification.paths == expected
    assert notification.event_types


async def test_storm_stays_bounded_without_hanging(tmp_path: Path) -> None:
    """Criterion 3: 300 rapid writes -> bounded notifications, bounded queue, no hang."""
    root = tmp_path / "watched"
    root.mkdir()
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.1, queue_size=8) as service:
        await asyncio.sleep(_ARM_S)
        for i in range(300):
            (root / f"storm_{i:03d}.txt").write_text("x", encoding="utf-8")
        notifications: list[ChangeNotification] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                notifications.append(await asyncio.wait_for(service.notifications.get(), 1.0))
            except TimeoutError:
                break
        assert notifications, "the storm produced at least one notification"
        assert len(notifications) <= 16, "notification count stays bounded (the merge window coalesces the storm)"
        assert service.notifications.qsize() <= 8, "the queue never grows past its cap"
    merged_paths: set[Path] = set().union(*(item.paths for item in notifications))
    assert len(merged_paths) >= 100, "the storm demonstrably flowed through"


async def test_full_queue_drops_instead_of_growing(tmp_path: Path) -> None:
    """Criterion 3: an idle host with a full queue gets drops, never backlog growth."""
    root = tmp_path / "watched"
    root.mkdir()
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.1, queue_size=1) as service:
        await asyncio.sleep(_ARM_S)
        (root / "first.txt").write_text("x", encoding="utf-8")
        await asyncio.sleep(1.0)  # notification 1 lands; the host never consumes it
        assert service.notifications.qsize() == 1
        (root / "second.txt").write_text("x", encoding="utf-8")
        await asyncio.sleep(1.0)  # notification 2 attempt -> QueueFull -> dropped
        assert service.notifications.qsize() == 1, "the queue never grows past its cap"
        assert service.dropped_notifications == 1
        first = await asyncio.wait_for(service.notifications.get(), 1.0)
        assert root / "first.txt" in first.paths


async def test_multiple_roots_are_watched_together(tmp_path: Path) -> None:
    """The Sequence[Path] roots face: one write per root merges into one notification."""
    root_a = tmp_path / "alpha"
    root_b = tmp_path / "beta"
    root_a.mkdir()
    root_b.mkdir()
    async with WatcherService((root_a, root_b), debounce_ms=50, merge_window_s=0.2) as service:
        await asyncio.sleep(_ARM_S)
        (root_a / "a.txt").write_text("x", encoding="utf-8")
        (root_b / "b.txt").write_text("x", encoding="utf-8")
        notification = await asyncio.wait_for(service.notifications.get(), 5.0)
        await asyncio.sleep(0.6)
        assert service.notifications.qsize() == 0
    assert root_a / "a.txt" in notification.paths
    assert root_b / "b.txt" in notification.paths
