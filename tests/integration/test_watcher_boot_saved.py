"""Watcher W2 boot_id/saved-decision tests (CORE-CONTRACTS 6.4 ruling 4; T-C1-069).

The cross-snapshot linkage of boot_id and saved decisions:

- reload is not a boot: the boot_id is unchanged across an applied reload
  and every regime entry carries the same boot_id;
- a saved decision recorded under the old snapshot regime is retained for
  audit (its durable row stays byte-identical) and does NOT auto-activate
  under the new snapshot regime; the host's new always-reply under the new
  regime does activate; a decision of another boot never activates (the
  5.2 axis); the carrier is the evaluation-time regime gate, because the
  reload itself writes no permission events at all;
- zero permission-event writes: across an applied reload the durable log
  and the permissions projection are byte-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from opencode.config import CliOverrides, ConfigLoader
from opencode.config.loader import ResolvedRuntime
from opencode.session import SessionProjector
from opencode.storage import EventBatch, ReadRepositories, SessionStore, open_database
from opencode.watcher.reload import ReloadOrchestrator
from tests.integration.test_permission_projector import _permission_draft, _permission_rows
from tests.integration.test_projector import (
    _R2_SESSION,
    _fetch_all,
    _require_success,
    _seed_created_and_prompt,
    _upgrade_head,
)

_TARGET = "ses_0000000000000000000000000099"
_BOOT = "boot_0000000000000000000000000001"
_OTHER_BOOT = "boot_0000000000000000000000000002"
_SAVED_RECORDED_AT = datetime(2026, 11, 3, 8, 0, 23, tzinfo=UTC)  # the fixture event's created_at


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


async def test_saved_decisions_cross_the_snapshot_regime_boundary(tmp_path: Path) -> None:
    """Ruling 4: audit-retained across the boundary, never auto-activated under the new regime."""
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 3, 9, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)
        saved = EventBatch("w2_saved", (_permission_draft("saved"),))
        await store.commit(_R2_SESSION, saved, high)  # durable saved decision of _BOOT
        events_before = _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq")

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            boot_id=_BOOT,
            clock=clock,
        )
        assert orchestrator.saved_decision_is_active(boot_id=_BOOT, recorded_at=_SAVED_RECORDED_AT)

        clock.advance(10.0)
        result = await orchestrator.reload()

    assert result.applied
    assert orchestrator.boot_id == _BOOT, "reload is not a boot: no new boot_id (ruling 4)"
    assert all(regime.boot_id == _BOOT for regime in orchestrator.regimes)
    assert orchestrator.active_regime.since is not None

    # The old-regime decision: audit-retained, never auto-activated under the new regime.
    assert not orchestrator.saved_decision_is_active(boot_id=_BOOT, recorded_at=_SAVED_RECORDED_AT)
    # The host's new always-reply under the new regime does activate.
    assert orchestrator.active_regime.since is not None
    new_reply_at = orchestrator.active_regime.since + timedelta(hours=1)
    assert orchestrator.saved_decision_is_active(boot_id=_BOOT, recorded_at=new_reply_at)
    # Another boot's decisions never activate (the 5.2 axis).
    assert not orchestrator.saved_decision_is_active(boot_id=_OTHER_BOOT, recorded_at=new_reply_at)

    # The durable audit trail is byte-identical: the reload wrote no permission event.
    assert _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq") == events_before
    assert _fetch_all(db_path, "SELECT type FROM events WHERE type LIKE 'py.permission.%'") == [
        ("py.permission.saved",)
    ]


async def test_reload_writes_no_permission_events_and_preserves_the_decision_rows(tmp_path: Path) -> None:
    """Zero-touch red line: the permissions projection is byte-identical across a reload."""
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 3, 9, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)
        saved = EventBatch("w2_saved", (_permission_draft("saved"),))
        await store.commit(_R2_SESSION, saved, high)
        permissions_before = _permission_rows(db_path)
        assert len(permissions_before) == 1
        sessions_before = _fetch_all(
            db_path,
            "SELECT session_id, status, active_run_id, applied_seq FROM sessions ORDER BY session_id",
        )

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            boot_id=_BOOT,
            clock=clock,
        )
        clock.advance(10.0)
        result = await orchestrator.reload()
        assert result.applied

        assert _permission_rows(db_path) == permissions_before
        assert (
            _fetch_all(
                db_path,
                "SELECT session_id, status, active_run_id, applied_seq FROM sessions ORDER BY session_id",
            )
            == sessions_before
        )
        assert _fetch_all(db_path, "SELECT type FROM events WHERE type LIKE 'py.permission.%'") == [
            ("py.permission.saved",)
        ]
