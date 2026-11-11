"""Watcher W2 reload-segment tests (CORE-CONTRACTS 6.4 ruling 2; T-C1-069).

The host explicit reload command is the ONLY path to a new snapshot:

- the reload face rides the same extras gate as W1: importing it without
  the watcher extras fails with the section-8 ``UnsupportedCapability``
  error (the httpapi precedent);
- an explicit reload re-resolves through the real ``ConfigLoader`` public
  face and lands a new §6.1-version-path snapshot whose §6.2 canonical
  hash is recomputed from the new bytes (changed content -> new hash !=
  old hash; the digest is never carried over);
- file events alone NEVER rewrite the snapshot: a demonstrably delivered
  notification -- even one carrying a real config-file change -- leaves
  the current runtime, its hash and the regime timeline untouched until
  the host issues the explicit command;
- the stored ``py.session.created`` rows stay byte-identical across an
  applied reload (the old snapshot's meaning is never silently rewritten);
- the §6.2.3 re-verification roundtrip: the fresh hash validates back
  through the contracts created-data model and a stale hash fails as
  ``ValidationError``;
- a broken config source stops the reload with the loader's named
  ``ConfigError`` and the old regime intact (6.4/L340: stop, never
  silently swap a different set of permissions).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from opencode.config import CliOverrides, ConfigError, ConfigLoader
from opencode.config.loader import ResolvedRuntime
from opencode.contracts import compute_snapshot_hash
from opencode.contracts.errors import CoreErrorCode
from opencode.session import SessionProjector
from opencode.storage import EventBatch, ReadRepositories, SessionStore, StoreError, open_database
from opencode.watcher import WatcherService
from opencode.watcher.reload import ReloadOrchestrator, verify_snapshot_hash
from tests.integration.test_projector import (
    _draft_from_raw,
    _fetch_all,
    _fixture_raw,
    _require_success,
    _upgrade_head,
)

_ARM_S = 0.25
_V2_SESSION = "ses_0000000000000000000000000021"


class _FakeClock:
    """A controllable UTC clock: the quiet-window fixture face."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _write_config(working: Path, model: str) -> None:
    (working / "opencode.json").write_text(json.dumps({"model": model, "context_limit": 100000}), encoding="utf-8")


def _resolve_via_config_file(working: Path) -> ResolvedRuntime:
    """The real ConfigLoader public face over the working directory's config file."""
    return ConfigLoader(working_dir=working).resolve_runtime(endpoint_url="https://api.example.test/v1")


def _resolve_via_cli(working: Path, model: str = "m") -> ResolvedRuntime:
    """The real ConfigLoader public face over CLI overrides (no config file needed)."""
    cli = CliOverrides(model=model, context_limit=100000)
    return ConfigLoader(working_dir=working).resolve_runtime(cli=cli, endpoint_url="https://api.example.test/v1")


def test_reload_face_requires_the_watcher_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reload capability is extras-gated at package import (same gate as W1)."""
    monkeypatch.setitem(sys.modules, "watchfiles", None)
    for module in ("opencode.watcher", "opencode.watcher.service", "opencode.watcher.reload"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(StoreError) as excinfo:
        importlib.import_module("opencode.watcher.reload")
    error = excinfo.value
    assert type(error).__name__ == "WatcherExtrasMissingError"
    assert error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "opencode-py[watcher]" in str(error)


async def test_explicit_reload_lands_the_new_snapshot_with_a_fresh_canonical_hash(tmp_path: Path) -> None:
    """Ruling 2 positive face: the command re-resolves and lands a new regime.

    The §6.2 digest is recomputed from the NEW snapshot's canonical bytes
    (changed content -> a different hash); the regime timeline keeps both
    hashes and the current runtime swap is the only injection path.
    """
    working = tmp_path / "project"
    working.mkdir()
    _write_config(working, "m-a")
    initial = _resolve_via_config_file(working)
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_via_config_file(working),
            read=ReadRepositories(database.engine),
            session_id="ses_0000000000000000000000000099",
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        assert orchestrator.current_runtime is initial
        old_hash = orchestrator.active_regime.snapshot_hash
        assert old_hash == compute_snapshot_hash(initial.snapshot)

        _write_config(working, "m-b")
        clock.advance(10.0)
        result = await orchestrator.reload()

    assert result.applied
    assert result.rejection_reason is None
    assert result.runtime is not None
    assert result.runtime.snapshot.model.model == "m-b"
    new_hash = compute_snapshot_hash(result.runtime.snapshot)
    assert new_hash != old_hash, "changed snapshot content must hash differently (6.2: no carried-over digest)"
    assert result.regime is not None
    assert result.regime.snapshot_hash == new_hash
    assert result.previous_snapshot_hash == old_hash
    assert orchestrator.current_runtime is result.runtime
    assert orchestrator.active_regime is result.regime
    assert [regime.snapshot_hash for regime in orchestrator.regimes] == [old_hash, new_hash]


async def test_file_events_never_rewrite_the_runtime_snapshot(tmp_path: Path) -> None:
    """Ruling 2 negative face: file events alone never trigger a snapshot change.

    A live watcher demonstrably delivers a notification that carries a real
    config-file change (m-a -> m-b on disk); without the explicit command
    the current runtime, its hash and the regime timeline are untouched,
    even though a fresh resolve would now see the new content.
    """
    working = tmp_path / "watched"
    working.mkdir()
    _write_config(working, "m-a")
    initial = _resolve_via_config_file(working)
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        async with WatcherService((working,), debounce_ms=50, merge_window_s=0.15) as service:
            orchestrator = ReloadOrchestrator(
                runtime=initial,
                resolve=lambda: _resolve_via_config_file(working),
                read=ReadRepositories(database.engine),
                session_id="ses_0000000000000000000000000099",
                working_dir=str(working),
                quiet_window_s=10.0,
                clock=clock,
            )
            clock.advance(10.0)
            await asyncio.sleep(_ARM_S)
            _write_config(working, "m-b")
            notification = await asyncio.wait_for(service.notifications.get(), 5.0)
            assert (working / "opencode.json") in notification.paths, "the change was demonstrably delivered"

            observation = await orchestrator.check_idle()
            assert observation.idle, "the idle gate observes, it never applies anything"
            assert orchestrator.current_runtime is initial
            assert orchestrator.active_regime.snapshot_hash == compute_snapshot_hash(initial.snapshot)
            assert len(orchestrator.regimes) == 1
            assert orchestrator.current_runtime.snapshot.model.model == "m-a"

    drifted = _resolve_via_config_file(working)
    assert compute_snapshot_hash(drifted.snapshot) != compute_snapshot_hash(initial.snapshot), "drift is real"


async def test_reload_preserves_the_stored_session_created_rows(tmp_path: Path) -> None:
    """Ruling 2: the old snapshot's meaning is never silently rewritten.

    A session created with a v2 snapshot keeps its ``py.session.created``
    event row and its ``sessions`` projection byte-identical across an
    applied reload (§3.2: the log prefix is never rewritten).
    """
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_via_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        draft = _draft_from_raw(_fixture_raw("events_session_created_v2_valid.json"))
        await store.commit(_V2_SESSION, EventBatch("w2_created_v2", (draft,)), 0)
        events_before = _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq")
        sessions_before = _fetch_all(
            db_path,
            "SELECT session_id, status, active_run_id, config_snapshot, snapshot_hash, applied_seq "
            f"FROM sessions WHERE session_id = '{_V2_SESSION}'",
        )
        assert len(events_before) == 1

        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_via_cli(working, model="m-b"),
            read=ReadRepositories(database.engine),
            session_id=_V2_SESSION,
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        result = await orchestrator.reload()
        assert result.applied
        assert result.runtime is not None
        assert compute_snapshot_hash(result.runtime.snapshot) != sessions_before[0][4]

        assert _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq") == events_before
        assert (
            _fetch_all(
                db_path,
                "SELECT session_id, status, active_run_id, config_snapshot, snapshot_hash, applied_seq "
                f"FROM sessions WHERE session_id = '{_V2_SESSION}'",
            )
            == sessions_before
        )


def test_verify_snapshot_hash_roundtrip_and_stale_hash_rejection(tmp_path: Path) -> None:
    """The 6.2.3 re-verification face: fresh hash validates, stale hash is ValidationError."""
    working = tmp_path / "project"
    working.mkdir()
    snapshot_a = _resolve_via_cli(working, model="m-a").snapshot
    snapshot_b = _resolve_via_cli(working, model="m-b").snapshot
    fresh_hash = compute_snapshot_hash(snapshot_a)
    stale_hash = compute_snapshot_hash(snapshot_b)

    data = verify_snapshot_hash(snapshot_a, working_dir=str(working), snapshot_hash=fresh_hash)
    assert data.snapshot_hash == fresh_hash
    assert compute_snapshot_hash(data.config_snapshot) == fresh_hash  # the decode-time recheck

    with pytest.raises(ValidationError, match="snapshot_hash does not match"):
        verify_snapshot_hash(snapshot_a, working_dir=str(working), snapshot_hash=stale_hash)


async def test_reload_stops_on_config_error_and_keeps_the_old_regime(tmp_path: Path) -> None:
    """6.4/L340: a broken config source stops the reload; the old regime stands.

    The explicit stop surfaces the loader's named ``ConfigError``; the
    regime timeline and the current runtime are unchanged (never a silent
    swap of a different permission set).
    """
    working = tmp_path / "project"
    working.mkdir()
    initial = _resolve_via_cli(working, model="m-a")
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    clock = _FakeClock(datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC))
    async with open_database(db_path, mode="create") as database:
        orchestrator = ReloadOrchestrator(
            runtime=initial,
            resolve=lambda: _resolve_via_config_file(working),
            read=ReadRepositories(database.engine),
            session_id="ses_0000000000000000000000000099",
            working_dir=str(working),
            quiet_window_s=10.0,
            clock=clock,
        )
        clock.advance(10.0)
        (working / "opencode.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            await orchestrator.reload()

        assert len(orchestrator.regimes) == 1
        assert orchestrator.current_runtime is initial
        assert orchestrator.active_regime.snapshot_hash == compute_snapshot_hash(initial.snapshot)
