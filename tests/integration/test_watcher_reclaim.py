"""Watcher W2 reclamation tests (CORE-CONTRACTS 6.4 red line 3 / 7; T-C1-069).

The coexistence lifecycle of the watcher service and the reload
orchestrator (the notification face + the reload threshold face):

- closing the composition reclaims everything: the watch tasks are done,
  ``asyncio.all_tasks()`` shows zero pending watcher/reload tasks, the
  closed reload face refuses further commands (reload and the idle
  observation alike), and a double close is a no-op;
- a cancelled host coroutine still reaps the composition through the
  async-context-manager unwinding (the W1 lifecycle extension form).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencode.config import CliOverrides, ConfigLoader
from opencode.config.loader import ResolvedRuntime
from opencode.storage import ReadRepositories, open_database
from opencode.watcher import WatcherService
from opencode.watcher.reload import ReloadOrchestrator
from tests.integration.test_projector import _require_success, _upgrade_head

_ARM_S = 0.25
_TARGET = "ses_0000000000000000000000000099"


class _FakeClock:
    """A controllable UTC clock: the quiet-window fixture face."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _resolve_cli(working: Path, model: str = "m") -> ResolvedRuntime:
    """The real ConfigLoader public face over CLI overrides (no config file needed)."""
    cli = CliOverrides(model=model, context_limit=100000)
    return ConfigLoader(working_dir=working).resolve_runtime(cli=cli, endpoint_url="https://api.example.test/v1")


def _composition_tasks() -> list[asyncio.Task[object]]:
    """Every still-pending task of the composition (watcher-* and reload-* names)."""
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("watcher-") or task.get_name().startswith("reload-")
    ]


async def test_close_reclaims_the_composition_and_gates_the_face(tmp_path: Path) -> None:
    """close = the notification face AND the reload threshold face fully reclaimed."""
    working = tmp_path / "watched"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        service = WatcherService((working,), debounce_ms=50, merge_window_s=0.1)
        await service.start()
        assert service.is_running
        assert len(service.watch_tasks) == 2

        async with ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        ) as orchestrator:
            assert not orchestrator.is_closed
            clock.advance(10.0)
            assert (await orchestrator.check_idle()).idle
            result = await orchestrator.reload()
            assert result.applied

            # Both faces are demonstrably live together: a file change is
            # delivered as a host notification, the host notes it, and the
            # reload threshold re-arms (the quiet window restarts).
            (working / "activity.txt").write_text("x", encoding="utf-8")
            notification = await asyncio.wait_for(service.notifications.get(), 5.0)
            assert (working / "activity.txt") in notification.paths
            orchestrator.note_file_activity()
            observation = await orchestrator.check_idle()
            assert not observation.quiet_window_elapsed

        await service.stop()
        assert not service.is_running
        assert all(task.done() for task in service.watch_tasks)
        assert _composition_tasks() == []

        # The closed reload face refuses further commands and close is idempotent.
        assert orchestrator.is_closed
        with pytest.raises(RuntimeError, match="closed"):
            await orchestrator.reload()
        with pytest.raises(RuntimeError, match="closed"):
            await orchestrator.check_idle()
        await orchestrator.close()
        assert _composition_tasks() == []


async def test_cancelled_host_coroutine_reaps_the_composition(tmp_path: Path) -> None:
    """Cancellation face: the unwinding reaps both faces; zero leftover tasks (W1 extension)."""

    async def host() -> None:
        working = tmp_path / "watched"
        working.mkdir()
        db_path = tmp_path / "app.db"
        _require_success(_upgrade_head(db_path))
        clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
        clock.advance(10.0)
        async with open_database(db_path, mode="create") as database:
            service = WatcherService((working,), debounce_ms=50, merge_window_s=0.1)
            orchestrator = ReloadOrchestrator(
                runtime=_resolve_cli(working),
                resolve=lambda: _resolve_cli(working),
                read=ReadRepositories(database.engine),
                session_id=_TARGET,
                working_dir=str(working),
                quiet_window_s=10.0,
                clock=clock,
            )
            async with service, orchestrator:
                await asyncio.sleep(30)

    task = asyncio.create_task(host())
    await asyncio.sleep(_ARM_S)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert _composition_tasks() == []
