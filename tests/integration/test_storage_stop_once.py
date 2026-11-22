"""T-C3-016 contract set: the aiosqlite #403 ``stop()`` guard on every pooled connection.

Upstream omnilib/aiosqlite#403 (still open at 0.22.1): ``Connection.stop()``
queues ONE stop item and returns a fresh future per call, while the worker
thread exits on the FIRST item -- the future of every later call is resolved
by nobody. SQLAlchemy 2.1.6 reaches exactly that second call when a task is
cancelled while a DBAPI call is parked on the worker thread and a second
cancel wave follows: ``terminate()`` (sqlalchemy/connectors/asyncio.py) falls
back to ``_terminate_force_close()`` = ``stop()`` #1, and the cancelled
``Connection.close()`` runs its ``finally`` = ``stop()`` #2 and awaits that
dead future forever. That orphan is the task ``asyncio.run()``'s shutdown
``_cancel_all_tasks`` gather never sees finish: the test_automation_event CI
hang.

The fix is the workaround recorded on the upstream issue: ``open_database``
installs a ``connect`` listener that makes ``stop()`` idempotent per
connection. Both live tests go through ``open_database``; the wave test is
the deterministic form of the hang (the T-C3-016 repro script, five modes:
wave order [task, close] hangs without the wrapper, every order is clean
once ``stop()`` is idempotent).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from opencode.storage import open_database

#: Generous: every wait below completes in milliseconds; the deadline only turns a
#: surprise into a named failure.
_DEADLINE_S = 5.0


def _is_graceful_close(task: asyncio.Task[Any]) -> bool:
    """The ``_terminate_graceful_close()`` task SQLAlchemy spawns on a cancelled
    DBAPI call (private coroutine name: the selector IS the scenario)."""
    return getattr(task.get_coro(), "__qualname__", "").endswith("_terminate_graceful_close")


class _Park:
    """A SQLite function that parks the aiosqlite worker thread until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()

    def __call__(self) -> int:
        self.entered.set()
        self.gate.wait()
        return 1


class _Driver:
    """``aiosqlite.Connection.stop()`` as 0.22.1 behaves: a fresh result per call
    (``None`` = the calling thread has no event loop)."""

    def __init__(self, *results: Any) -> None:
        self._results = list(results)
        self.calls = 0

    def stop(self) -> Any:
        self.calls += 1
        return self._results.pop(0) if self._results else "unpaced-stop"


async def _until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


async def _graceful_close_task() -> asyncio.Task[Any]:
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        found = [task for task in asyncio.all_tasks() if _is_graceful_close(task)]
        if found:
            return found[0]
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for SQLAlchemy's graceful-close task")


async def _reap(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_make_stop_idempotent_hands_back_the_first_stop_result() -> None:
    from opencode.storage.db import _make_stop_idempotent

    driver = _Driver("future-1", "future-2")
    _make_stop_idempotent(driver)
    assert driver.stop() == "future-1"
    assert driver.stop() == "future-1", "a second stop() minted a future the worker thread never resolves"
    assert driver.calls == 1

    # No event loop in the calling thread: the stop item is queued all the same,
    # so nothing queued later could ever resolve either -- the None is final too.
    without_loop = _Driver(None, "future-2")
    _make_stop_idempotent(without_loop)
    assert without_loop.stop() is None
    assert without_loop.stop() is None
    assert without_loop.calls == 1

    # aiosqlite < 0.22.1 has no stop(): nothing to wrap, nothing raised.
    plain = SimpleNamespace()
    _make_stop_idempotent(plain)
    assert not hasattr(plain, "stop")


async def test_stop_is_idempotent_on_every_pooled_connection(tmp_path: Path) -> None:
    async with open_database(tmp_path / "stop-once.db") as database:
        one = await database.engine.connect()
        two = await database.engine.connect()  # checked out together: two physical connections
        try:
            for connection in (one, two):
                driver: Any = (await connection.get_raw_connection()).driver_connection
                stop_future = driver.stop()
                await stop_future  # the worker thread closes its handle and exits
                assert stop_future.done()
                assert driver.stop() is stop_future, "a later stop() must not queue a second stop item"
        finally:
            for connection in (one, two):
                await connection.invalidate()  # stopped connections never go back to the pool
                await connection.close()


async def test_second_cancel_wave_on_a_terminated_connection_leaves_no_pending_task(tmp_path: Path) -> None:
    park = _Park()

    def register_park(sync_connection: Any) -> None:
        # the DBAPI shim's sync face hops to the worker thread for us
        sync_connection.connection.dbapi_connection.create_function("hold", 0, park)

    async with open_database(tmp_path / "wave.db") as database:
        async with database.engine.connect() as connection:
            await connection.run_sync(register_park)  # checked in below: the loop borrows this very connection

        async def loop() -> None:
            """The scheduler analog: one DBAPI call after another on that connection."""
            while True:
                async with database.engine.connect() as borrowed:
                    await borrowed.execute(text("select hold()"))
                await asyncio.sleep(0.005)

        task = asyncio.create_task(loop(), name="scheduler")
        close_task: asyncio.Task[Any] | None = None
        try:
            await _until(park.entered.is_set, "the statement to park on the worker thread")
            # 1. AutomationScheduler.stop(): the cancel lands mid-DBAPI-call, so
            #    SQLAlchemy invalidates the connection and spawns the shielded
            #    graceful close.
            task.cancel()
            close_task = await _graceful_close_task()
            await asyncio.sleep(0)  # let it park on the worker thread as well
            # 2. the asyncio.run() shutdown wave, in the order that hangs without
            #    the wrapper: the task force-closes (stop #1), the cancelled close
            #    runs its finally (stop #2).
            task.cancel()
            close_task.cancel()
            await asyncio.sleep(0)  # both cancellations delivered
            park.gate.set()  # the parked statement returns; the worker thread drains its queue and exits
            _, pending = await asyncio.wait({task, close_task}, timeout=_DEADLINE_S)
            assert not pending, "the graceful-close task outlived the worker thread (aiosqlite #403)"
        finally:
            # Red-state safety net: without the wrapper the close task waits on a
            # future the exited worker thread never resolves; a THIRD cancel lands
            # on that await and ends the task, so a regression fails the assertion
            # above instead of hanging the loop teardown.
            park.gate.set()
            await _reap(*[item for item in (task, close_task) if item is not None])
