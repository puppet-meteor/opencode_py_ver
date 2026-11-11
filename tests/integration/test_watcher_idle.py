"""Watcher W2 idle-segment tests (CORE-CONTRACTS 6.4 ruling 3; T-C1-069).

The idle gate is the three-condition conjunction of the reload threshold:

- each condition's counter-example rejects (defers) the reload with a
  structured reason the host can retry: an in-flight accepted/running/
  cancelling run of the target session, a pending ``py.permission.requested``
  approval anywhere in the process, and the quiet window not yet elapsed
  (with file-change and command activity resetting it);
- the observation itself is read-only: over a demonstrably busy state it
  reports idle=False while every database row stays byte-identical --
  nothing is cancelled, closed or interrupted;
- at the reload application point the precondition is atomically
  re-verified per the 3.2 commit-lock discipline: state that changed
  between the pre-check and the apply point (a run that starts during
  resolve) is caught and the reload is rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from opencode.config import CliOverrides, ConfigLoader
from opencode.config.loader import ResolvedRuntime
from opencode.contracts import compute_snapshot_hash
from opencode.session import SessionProjector
from opencode.storage import EventBatch, ReadRepositories, SessionStore, open_database
from opencode.watcher.reload import ReloadOrchestrator
from tests.integration.test_permission_projector import _permission_draft, _seed_pending_tool
from tests.integration.test_projector import (
    _R2_RUN,
    _R2_SESSION,
    _fetch_all,
    _require_success,
    _seed_created_and_prompt,
    _upgrade_head,
)

_TARGET = "ses_0000000000000000000000000099"
_PERMISSION_ID = "per_0000000000000000000000000001"


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


async def test_reload_rejected_while_the_target_run_is_in_flight(tmp_path: Path) -> None:
    """Condition 1 counter-example: an accepted run of the target session defers the reload."""
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await _seed_created_and_prompt(store)  # the R2 run is accepted: in flight
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_R2_SESSION,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        result = await orchestrator.reload()

    assert not result.applied
    assert result.runtime is None
    assert result.regime is None
    assert not result.observation.idle
    assert not result.observation.no_in_flight_runs
    assert result.observation.in_flight_run_ids == (_R2_RUN,)
    assert result.observation.no_pending_approvals
    assert result.observation.quiet_window_elapsed
    assert _R2_RUN in (result.rejection_reason or "")
    assert len(orchestrator.regimes) == 1, "a rejected reload never swaps the regime"
    assert orchestrator.current_runtime is initial
    assert orchestrator.active_regime.snapshot_hash == compute_snapshot_hash(initial.snapshot)


async def test_pending_approval_blocks_reload_even_for_an_idle_target_session(tmp_path: Path) -> None:
    """Condition 2 counter-example: any pending approval defers the reload.

    The pending-approval condition is the process-wide ``pending_permissions``
    face (ruling 3 names it without a target-session qualifier): a pending
    ``py.permission.requested`` in another session still defers this
    session's reload -- the host retries after resolving it.
    """
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)  # the R2 run is running with a pending tool
        requested = EventBatch("w2_requested", (_permission_draft("requested"),))
        await store.commit(_R2_SESSION, requested, high)

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_TARGET,  # idle: no run of THIS session anywhere
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        result = await orchestrator.reload()

    assert not result.applied
    assert result.observation.no_in_flight_runs, "the target session itself has no in-flight run"
    assert not result.observation.no_pending_approvals
    assert result.observation.pending_permission_ids == (_PERMISSION_ID,)
    assert not result.observation.idle
    assert len(orchestrator.regimes) == 1
    assert orchestrator.current_runtime is initial


async def test_reload_deferred_within_the_quiet_window_and_activity_resets_it(tmp_path: Path) -> None:
    """Condition 3: the quiet window defers; file and command activity restart it."""
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_TARGET,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )

        within = await orchestrator.reload()  # at construction: the full window remains
        assert not within.applied
        assert within.observation.no_in_flight_runs
        assert within.observation.no_pending_approvals
        assert not within.observation.quiet_window_elapsed
        assert within.observation.quiet_seconds_remaining == 10.0

        clock.advance(10.0)
        first = await orchestrator.reload()
        assert first.applied, "all three conditions hold: the reload passes"
        assert len(orchestrator.regimes) == 2

        orchestrator.note_file_activity()  # the host consumed a file change
        after_file = await orchestrator.reload()
        assert not after_file.applied
        assert not after_file.observation.quiet_window_elapsed
        assert after_file.observation.quiet_seconds_remaining == 10.0

        clock.advance(5.0)
        orchestrator.note_command_activity()  # a host command happened mid-window
        after_command = await orchestrator.reload()
        assert not after_command.applied
        assert after_command.observation.quiet_seconds_remaining == 10.0

        clock.advance(10.0)
        second = await orchestrator.reload()
        assert second.applied
        assert len(orchestrator.regimes) == 3


async def test_idle_observation_is_read_only_over_busy_state(tmp_path: Path) -> None:
    """The idle judgment is a read-only observation: nothing is cancelled or interrupted."""
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)
        requested = EventBatch("w2_requested", (_permission_draft("requested"),))
        await store.commit(_R2_SESSION, requested, high)
        events_before = _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq")
        runs_before = _fetch_all(db_path, "SELECT run_id, session_id, status FROM runs ORDER BY run_id")
        sessions_before = _fetch_all(
            db_path,
            "SELECT session_id, status, active_run_id, applied_seq FROM sessions ORDER BY session_id",
        )
        permissions_before = _fetch_all(
            db_path,
            "SELECT session_id, permission_id, execution_id, state FROM permissions ORDER BY permission_id",
        )

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_R2_SESSION,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        observation = await orchestrator.check_idle()
        assert not observation.idle
        assert not observation.no_in_flight_runs
        assert not observation.no_pending_approvals
        assert observation.quiet_window_elapsed

        again = await orchestrator.check_idle()
        assert not again.idle

        assert _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq") == events_before
        assert _fetch_all(db_path, "SELECT run_id, session_id, status FROM runs ORDER BY run_id") == runs_before
        assert (
            _fetch_all(
                db_path,
                "SELECT session_id, status, active_run_id, applied_seq FROM sessions ORDER BY session_id",
            )
            == sessions_before
        )
        assert (
            _fetch_all(
                db_path,
                "SELECT session_id, permission_id, execution_id, state FROM permissions ORDER BY permission_id",
            )
            == permissions_before
        )


async def test_apply_point_recheck_rejects_a_run_that_started_during_resolve(tmp_path: Path) -> None:
    """3.2 discipline at the application point: state change mid-reload is rejected.

    The pre-check passes (no runs anywhere), the resolve closure -- as its
    side effect -- starts a run, and the apply-point re-verification
    catches it: the reload is rejected and the old regime stands.
    """
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())

        async def resolve_with_a_race() -> ResolvedRuntime:
            # The race under test: between the pre-check and the apply point
            # a run of the target session starts (created + prompt accepted).
            await _seed_created_and_prompt(store)
            return _resolve_cli(working, model="m-b")

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=resolve_with_a_race,
            read=ReadRepositories(database.engine),
            session_id=_R2_SESSION,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        pre = await orchestrator.check_idle()
        assert pre.idle, "the pre-check passes before the race fires"
        result = await orchestrator.reload()

    assert not result.applied
    assert result.runtime is None
    assert not result.observation.idle
    assert not result.observation.no_in_flight_runs
    assert result.observation.in_flight_run_ids == (_R2_RUN,)
    assert _R2_RUN in (result.rejection_reason or "")
    assert len(orchestrator.regimes) == 1
    assert orchestrator.current_runtime is initial
