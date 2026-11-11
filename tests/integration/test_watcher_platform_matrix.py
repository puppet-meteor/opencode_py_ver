"""Watcher W3 platform-matrix tests (p2-slices L165; CORE-CONTRACTS 6.4 L415, platform face).

The dual-OS matrix segment of the W3 closeout, as pure characterization of
the frozen W1/W2 deliverables: every assertion is platform-AGNOSTIC and
every timing bound is tolerant (the W1 spike precedent -- no tight
upper-bound timing asserts, CI slow-runner compatible). Local Windows is
proven on this machine; the CI Linux leg carries the same suite (the
dispatcher CI-WATCH closes the dual-OS record -- a local manual run and a
CI ci_verified run never impersonate each other).

Honest platform disclosure (CORE-CONTRACTS 6.4 L415, the M1 L53 wording):
network filesystems (NFS/SMB/network drives and similar unreliable
filesystems) are NOT claimed supported -- the watcher face covers only the
local, already-tested filesystems of this machine and the CI runners.
Nothing here is asserted over a network mount and no behavior over one is
implied.

Faces recorded honestly (event delivery / loss windows):

- event delivery: write/modify/delete each reach the host within a
  tolerant window; every watched root delivers its own notifications; a
  non-ASCII path survives the delivery roundtrip (the Windows UTF-8 face);
- loss windows: a slow consumer with a saturated host queue gets bounded
  drops counted in ``dropped_notifications`` (never backlog growth, never
  a hang), and a lost notification is HARMLESS -- the queue is not an
  acknowledgement protocol; the NEXT change re-supplies a fresh signal
  (6.4 ruling 1: honestly recording the loss introduces no retry,
  compensation or acknowledgement mechanism). Raw-batch overflow is
  counted in ``dropped_batches`` -- the overflow precondition is opened
  by removing the merge consumer and seeding the internal raw queue to
  its bound through the frozen seam, because the platform's batch pacing
  makes the bound unreachable by any bounded storm (probe-recorded; see
  the W3 progress doc); the pump's overflow path itself is exercised
  live;
- merge/debounce recheck: N rapid writes collapse into a few merged
  notifications -- the dual-OS tolerant recheck of the W1 spike's
  exactly-one form, with the assertion form deliberately lenient.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from watchfiles import Change

from opencode.watcher import ChangeNotification, WatcherService

_ARM_S = 0.25  #: settle window for the rust watcher to arm after start()
_EVENT_VOCABULARY = {"added", "modified", "deleted"}


def _watcher_tasks() -> list[asyncio.Task[object]]:
    """Every still-pending task the watcher package created in this loop."""
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("watcher-")]


async def test_write_modify_delete_each_deliver_within_tolerant_windows(tmp_path: Path) -> None:
    """Event delivery: all three change kinds reach the host, each within a tolerant window."""
    root = tmp_path / "watched"
    root.mkdir()
    target = root / "delivery.txt"
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.15) as service:
        await asyncio.sleep(_ARM_S)

        target.write_text("v1", encoding="utf-8")
        added = await asyncio.wait_for(service.notifications.get(), 10.0)

        target.write_text("v2", encoding="utf-8")
        modified = await asyncio.wait_for(service.notifications.get(), 10.0)

        target.unlink()
        deleted = await asyncio.wait_for(service.notifications.get(), 10.0)

    assert target in added.paths
    assert "added" in added.event_types
    assert target in modified.paths
    assert "modified" in modified.event_types
    assert target in deleted.paths
    assert "deleted" in deleted.event_types
    for notification in (added, modified, deleted):
        assert notification.event_types <= _EVENT_VOCABULARY


async def test_each_watched_root_delivers_its_own_notifications(tmp_path: Path) -> None:
    """Event delivery: three roots, each write delivered as its own notification."""
    roots = [tmp_path / name for name in ("alpha", "beta", "gamma")]
    for root in roots:
        root.mkdir()
    async with WatcherService(tuple(roots), debounce_ms=50, merge_window_s=0.15) as service:
        await asyncio.sleep(_ARM_S)
        deliveries: list[ChangeNotification] = []
        for root in roots:
            (root / "solo.txt").write_text("x", encoding="utf-8")
            deliveries.append(await asyncio.wait_for(service.notifications.get(), 10.0))
    for root, notification in zip(roots, deliveries, strict=True):
        assert (root / "solo.txt") in notification.paths, f"the write under {root.name} was delivered"


async def test_non_ascii_path_survives_the_delivery_roundtrip(tmp_path: Path) -> None:
    """Encoding face: a non-ASCII path and payload round-trip through the notification."""
    root = tmp_path / "watched"
    root.mkdir()
    # CJK + Latin-1 + Cyrillic code points, assembled via chr() to keep this
    # source file pure ASCII -- the payload is what matters, not the literal.
    name = "cfg_" + chr(0x914D) + "_" + chr(0x30D5) + "_" + chr(0x0442) + ".txt"
    target = root / name
    payload = "payload " + chr(0x4F60) + chr(0x597D) + " " + chr(0x00E9) + chr(0x00FC)
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.15) as service:
        await asyncio.sleep(_ARM_S)
        target.write_text(payload, encoding="utf-8")
        notification = await asyncio.wait_for(service.notifications.get(), 10.0)
    assert target in notification.paths
    assert target.name == name
    assert target.read_text(encoding="utf-8") == payload


async def test_slow_consumer_loss_window_is_bounded_and_loss_is_harmless(tmp_path: Path) -> None:
    """Loss window: a saturated queue drops (counted, bounded, no hang); the next change re-supplies."""
    root = tmp_path / "watched"
    root.mkdir()
    sustained = 12
    async with WatcherService((root,), debounce_ms=30, merge_window_s=0.05, queue_size=2) as service:
        await asyncio.sleep(_ARM_S)
        for i in range(sustained):  # the slow consumer never drains: the loss window opens
            (root / f"slow_{i:02d}.txt").write_text("x", encoding="utf-8")
            await asyncio.sleep(0.3)
        await asyncio.sleep(1.0)  # let the tail notification land or drop

        assert service.notifications.qsize() <= 2, "the host queue never grows past its cap"
        assert service.dropped_notifications >= 1, "the loss window is honestly recorded, not hidden"
        assert service.is_running, "drops never kill the watch"

        drained: list[ChangeNotification] = []
        while service.notifications.qsize():
            drained.append(service.notifications.get_nowait())
        assert len(drained) <= 2, "only the bounded backlog survived the loss window"

        # Loss is harmless (6.4 ruling 1): the queue is not an acknowledgement
        # protocol -- the NEXT change re-supplies a fresh notification.
        (root / "resupply.txt").write_text("x", encoding="utf-8")
        resupply = await asyncio.wait_for(service.notifications.get(), 10.0)
        assert (root / "resupply.txt") in resupply.paths
        assert service.dropped_notifications >= 1


async def test_raw_batch_overflow_is_counted_and_never_fatal(tmp_path: Path) -> None:
    """Loss window (raw face): the internal batch overflow is counted, bounded and non-fatal.

    Platform-pacing honesty: on this Windows machine the pump yields raw
    batches at roughly one per 100 ms and coalesces a pure-sync burst of
    thousands of writes into one or two batches (probe-recorded; numbers in
    the W3 progress doc T-C1-070), so the bounded internal raw window
    cannot be opened by any bounded, CI-compatible storm -- it would need
    roughly 26 s of sustained paced writes. The overflow PRECONDITION is
    therefore seeded through the internal queue seam (a face frozen and
    pinned by the W1 redline suite), and the pump path itself
    (``put_nowait`` -> ``QueueFull`` -> the counted drop) is then exercised
    for real by live writes.
    """
    root = tmp_path / "watched"
    root.mkdir()
    service = WatcherService((root,), debounce_ms=50, merge_window_s=0.05)
    await service.start()
    await asyncio.sleep(_ARM_S)
    merger = next(task for task in service.watch_tasks if task.get_name() == "watcher-merge")
    merger.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await merger
    assert service.is_running, "the pump face survives the merge consumer's death"

    # Seed the internal raw queue to its exact bound (fill until IT says
    # full -- no magic size), so the pump's next real batch hits QueueFull.
    seeded = 0
    dummy: set[tuple[Change, str]] = set()
    while True:
        try:
            service._raw_batches.put_nowait(dummy)
            seeded += 1
        except asyncio.QueueFull:
            break
    assert seeded >= 1

    (root / "overflow_a.txt").write_text("x", encoding="utf-8")
    (root / "overflow_b.txt").write_text("x", encoding="utf-8")
    await asyncio.sleep(1.0)  # the pump delivers within its pacing; no timing assert
    assert service.dropped_batches >= 1, "the raw-overflow loss window is honestly recorded"
    assert service.is_running, "the overflow is counted, never fatal"
    assert service.watch_error is None

    await service.stop()
    assert not service.is_running
    assert all(task.done() for task in service.watch_tasks)
    assert _watcher_tasks() == []


async def test_rapid_writes_collapse_into_a_few_merged_notifications(tmp_path: Path) -> None:
    """Merge/debounce recheck: a burst of writes collapses into a few notifications (lenient form)."""
    root = tmp_path / "watched"
    root.mkdir()
    count = 24
    async with WatcherService((root,), debounce_ms=50, merge_window_s=0.2) as service:
        await asyncio.sleep(_ARM_S)
        for i in range(count):
            (root / f"burst_{i:02d}.txt").write_text("x", encoding="utf-8")
            await asyncio.sleep(0.004)

        notifications: list[ChangeNotification] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                notifications.append(await asyncio.wait_for(service.notifications.get(), 1.0))
            except TimeoutError:
                break
        await asyncio.sleep(0.8)  # past any residual merge window: no stragglers may land
        assert service.notifications.qsize() == 0

    assert notifications, "the burst demonstrably flowed through"
    assert len(notifications) <= 8, "the merge face collapses the burst (dual-OS tolerant recheck)"
    merged_paths = set().union(*(item.paths for item in notifications))
    expected = {root / f"burst_{i:02d}.txt" for i in range(count)}
    assert merged_paths == expected
