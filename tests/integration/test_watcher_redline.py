"""Watcher W1 redline tests (CORE-CONTRACTS 6.4 W1 shares; PHASE2-MODULES L65/L70).

Three behavioral redlines over the real faces plus two structural ones:

- during an event storm the running config snapshot stays byte-identical:
  canonical JSON + sha256 over the ConfigLoader-resolved runtime snapshot
  (the test_config_loader.py anchor form) are unchanged while the watcher
  demonstrably delivers storm notifications;
- during an event storm the approval face and the durable log show zero
  change: a real SQLite store seeded to a pending ``py.permission.requested``
  approval (the test_permission_projector.py anchor form) keeps its
  permissions rows, its sessions projection and its durable events table
  byte-identical, and no new ``py.permission.*`` event is ever produced;
- the service is structurally state-change-free: ``service.py`` imports no
  opencode module at all, the package ``__init__`` imports only the guard
  faces (contracts.errors code registry + storage StoreError base), and no
  module outside ``src/opencode/watcher/`` imports the watcher package.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from opencode.config import CliOverrides, ConfigLoader
from opencode.contracts import canonical_json, compute_snapshot_hash
from opencode.session import SessionProjector
from opencode.storage import EventBatch, SessionStore, open_database
from opencode.watcher import WatcherService
from tests.integration.test_permission_projector import _permission_draft, _permission_rows, _seed_pending_tool
from tests.integration.test_projector import _R2_SESSION, _fetch_all, _require_success, _upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHER_SRC = REPO_ROOT / "src" / "opencode" / "watcher"
_ARM_S = 0.25
_STORM_FILES = 120


def _storm(root: Path) -> None:
    for i in range(_STORM_FILES):
        (root / f"storm_{i:03d}.txt").write_text("x", encoding="utf-8")


def _module_imports(tree: ast.Module) -> list[str]:
    """Every module-level name an import statement binds or imports from."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


async def test_storm_leaves_the_running_snapshot_byte_identical(tmp_path: Path) -> None:
    """PHASE2-MODULES L65/L70: file events never modify the running config snapshot."""
    working = tmp_path / "project"
    working.mkdir()
    cli = CliOverrides(model="m", context_limit=100000)
    endpoint_url = "https://api.example.test/v1"
    snapshot = ConfigLoader(working_dir=working).resolve_runtime(cli=cli, endpoint_url=endpoint_url).snapshot
    before_text = canonical_json(snapshot.model_dump(mode="json"))
    before_hash = compute_snapshot_hash(snapshot)

    async with WatcherService((working,), debounce_ms=50, merge_window_s=0.15) as service:
        await asyncio.sleep(_ARM_S)
        _storm(working)
        notification = await asyncio.wait_for(service.notifications.get(), 5.0)
    assert notification.paths, "the watcher was demonstrably live during the storm"

    assert canonical_json(snapshot.model_dump(mode="json")) == before_text
    assert compute_snapshot_hash(snapshot) == before_hash
    re_resolved = ConfigLoader(working_dir=working).resolve_runtime(cli=cli, endpoint_url=endpoint_url).snapshot
    assert canonical_json(re_resolved.model_dump(mode="json")) == before_text


async def test_storm_leaves_the_approval_face_and_durable_log_unchanged(tmp_path: Path) -> None:
    """Zero state change: permissions rows, projection and durable events are byte-identical."""
    db_path = tmp_path / "app.db"
    watched = tmp_path / "watched"
    watched.mkdir()
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)
        requested = EventBatch("w_requested", (_permission_draft("requested"),))
        high = (await store.commit(_R2_SESSION, requested, high))[-1].seq

        permission_before = _permission_rows(db_path)
        events_before = _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq")
        assert permission_before and permission_before[-1][-1] == "pending"
        assert sum(1 for row in events_before if str(row[1]).startswith("py.permission.")) == 1

        async with WatcherService((watched,), debounce_ms=50, merge_window_s=0.15) as service:
            await asyncio.sleep(_ARM_S)
            _storm(watched)
            notification = await asyncio.wait_for(service.notifications.get(), 5.0)
        assert notification.paths, "the watcher was demonstrably live during the storm"

        assert _permission_rows(db_path) == permission_before
        assert _fetch_all(db_path, "SELECT id, type, seq, data FROM events ORDER BY seq") == events_before
        assert _fetch_all(db_path, "SELECT type FROM events WHERE type LIKE 'py.permission.%'") == [
            ("py.permission.requested",)
        ]
        assert _fetch_all(db_path, f"SELECT applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(high,)]


def test_the_service_module_imports_no_opencode_module() -> None:
    """Structural zero-state-change face: the runtime service is opencode-free."""
    tree = ast.parse((WATCHER_SRC / "service.py").read_text(encoding="utf-8"))
    imports = _module_imports(tree)
    assert imports, "the service module must exist and import its dependencies"
    assert not [name for name in imports if name == "opencode" or name.startswith("opencode.")]


def test_the_package_imports_only_the_guard_faces() -> None:
    """The __init__ guard mirrors httpapi: code registry + StoreError base + the service."""
    tree = ast.parse((WATCHER_SRC / "__init__.py").read_text(encoding="utf-8"))
    opencode_imports = [name for name in _module_imports(tree) if name == "opencode" or name.startswith("opencode.")]
    assert set(opencode_imports) == {"opencode.contracts.errors", "opencode.storage", "opencode.watcher.service"}


def test_core_never_imports_the_watcher_package() -> None:
    """PHASE2-MODULES 2 L41 deferred-optional: nothing outside watcher/ imports it."""
    src = REPO_ROOT / "src" / "opencode"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if "watcher" in path.relative_to(src).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _module_imports(tree):
            if name == "opencode.watcher" or name.startswith("opencode.watcher."):
                offenders.append(str(path.relative_to(src)))
    assert offenders == []
