"""R0-B platform probe tests (STEP-01-R0.md sections 5-6).

Verifies the four probes as contracted: explicit interpreter selection
(never the platform default shell, never ``shell=True``), process-tree
reaping down to the grandchild (Windows Job Object / POSIX process
group) for both cancel and timeout, workspace path containment in the
filesystem.ts order (resolve -> containment -> realpath -> containment),
and the OS data-directory writer lock held before SQLite is opened
(WAL, foreign_keys, busy_timeout, second writer conflicts without
writing). The end-to-end CLI test pins the sanitized evidence shape.

Capability restrictions (e.g. Windows symlink privilege, winerror 1314)
skip the affected unit assertion here; the probe itself records them as
``restricted``/``unsupported`` in the evidence instead of failing.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from tools.r0.platform_probe import (
    PROBE_VERSION,
    CheckResult,
    build_evidence,
    check_interpreter,
    check_process_tree,
    check_sqlite_writer_lock,
    resolve_user_path,
)

PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "r0" / "platform_probe.py"


def test_probe_interpreter_is_explicit_and_versioned() -> None:
    result = check_interpreter()
    assert result.status == "verified", result.reason
    if sys.platform == "win32":
        assert result.details["interpreter"] in {"powershell.exe", "pwsh.exe"}
        assert result.details["required_by_contract"] == "powershell"
    else:
        assert result.details["interpreter"] == "bash"
        assert result.details["required_by_contract"] == "bash"
    assert result.details["version"]
    assert result.details["shell_true_used"] is False
    assert result.details["default_shell_used"] is False


def test_path_resolution_follows_filesystem_ts_order() -> None:
    """Lexical resolve -> containment -> realpath -> containment, per filesystem.ts."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        workspace = root / "workspace"
        (workspace / "sub").mkdir(parents=True)
        (workspace / "inside.txt").write_text("inside", encoding="utf-8")
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        base = str(workspace)

        assert resolve_user_path(base, "inside.txt") == (True, "inside_workspace")
        # `..` that stays inside is collapsed by the lexical resolve and allowed.
        assert resolve_user_path(base, "sub/../inside.txt") == (True, "inside_workspace")
        # `..` traversal and absolute outside paths are caught BEFORE realpath.
        assert resolve_user_path(base, "sub/../../outside/secret.txt") == (False, "escapes_workspace_before_realpath")
        assert resolve_user_path(base, str(outside / "secret.txt")) == (False, "escapes_workspace_before_realpath")
        # Missing targets fail the strict realpath.
        assert resolve_user_path(base, "missing.txt") == (False, "realpath_failed_or_missing")
        # NUL bytes never reach the filesystem.
        assert resolve_user_path(base, "bad\x00name") == (False, "nul_byte_in_path")


def _symlink_status(target: Path, link: Path) -> str:
    try:
        os.symlink(target, link)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            return "unsupported_privilege"
        return f"setup_failed:{type(error).__name__}"
    return "created"


def test_symlink_escape_rejected_after_realpath() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        (workspace / "inside.txt").write_text("inside", encoding="utf-8")

        status = _symlink_status(outside / "secret.txt", workspace / "escape-link.txt")
        if status != "created":
            pytest.skip(f"symlink capability unavailable: {status}")
        assert resolve_user_path(str(workspace), "escape-link.txt") == (False, "escapes_workspace_after_realpath")

        # A symlink that stays inside the workspace remains allowed.
        assert _symlink_status(workspace / "inside.txt", workspace / "good-link.txt") == "created"
        assert resolve_user_path(str(workspace), "good-link.txt") == (True, "inside_workspace")


def test_junction_escape_rejected_after_realpath() -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are windows-only")
    import _winapi

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        link = workspace / "escape-junction"
        try:
            _winapi.CreateJunction(str(outside), str(link))
        except OSError as error:
            pytest.skip(f"junction capability unavailable: {type(error).__name__}")
        try:
            assert resolve_user_path(str(workspace), "escape-junction/secret.txt") == (
                False,
                "escapes_workspace_after_realpath",
            )
        finally:
            # Remove the reparse point itself; never traverse into the target.
            os.rmdir(link)


def test_probe_process_tree_reaps_all_descendants() -> None:
    result = check_process_tree()
    assert result.status == "verified", result.reason
    scenarios = result.details["scenarios"]
    assert [s["scenario"] for s in scenarios] == ["cancel", "timeout"]
    for scenario in scenarios:
        assert scenario["all_exited_within_grace"] is True, scenario
        assert scenario["descendants"] == {"child_shell": True, "grandchild": True}
        assert scenario["reason"] is None
    assert result.details["temp_cleanup_verified"] is True


def test_probe_sqlite_writer_lock_conflict_without_writes() -> None:
    result = check_sqlite_writer_lock()
    assert result.status == "verified", result.reason
    details = result.details
    # Second writer fails the OS data-directory lock before any write transaction.
    assert details["writer2"] == {"result": "conflict", "database_opened": False}
    # No-write proof across the writer-2 window and the writer-1 hold.
    assert details["no_write_evidence"]["snapshots_identical"] is True
    assert details["no_write_evidence"]["writer1_rows_stable_across_hold"] is True
    # OS releases the lock when the holder dies.
    assert details["crash_release"]["lock_released_by_os_on_death"] is True
    assert details["crash_release"]["crashed_exit_code"] == 3
    # SQLite pragmas are actually in effect, and FKs are enforced.
    sqlite = details["sqlite"]
    assert str(sqlite["journal_mode"]).lower() == "wal"
    assert sqlite["foreign_keys"] == 1
    assert sqlite["busy_timeout_ms"] == 5000
    assert sqlite["fk_violation_rejected"] is True
    assert details["cleanup"]["temp_dir_removed"] is True


def test_cli_writes_sanitized_evidence() -> None:
    with tempfile.TemporaryDirectory() as raw:
        out_path = Path(raw) / "platform.json"
        env = {key: value for key, value in os.environ.items()}
        env.pop("GITHUB_ACTIONS", None)  # force manual mode for a deterministic assertion
        proc = subprocess.run(
            [sys.executable, str(PROBE_PATH), "--output", str(out_path)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        serialized = out_path.read_text(encoding="utf-8")
    evidence = json.loads(serialized)

    for key in ("platform", "probe_version", "ci_verified", "manual_verified", "unsupported", "checks", "notes"):
        assert key in evidence, key
    assert evidence["probe_version"] == PROBE_VERSION
    assert set(evidence["checks"]) == {"interpreter", "process_tree", "path_containment", "sqlite_writer_lock"}
    for name, check in evidence["checks"].items():
        assert check["status"] in {"verified", "restricted"}, (name, check)
    if any(check["status"] == "restricted" for check in evidence["checks"].values()):
        assert evidence["unsupported"]
    assert evidence["ci_verified"] == []
    assert evidence["manual_verified"]

    # Sanitization: no host paths, no username, no PID-like keys.
    for forbidden in (str(Path.home()), tempfile.gettempdir()):
        assert forbidden not in serialized
    user = getpass.getuser()
    if user:
        assert user not in serialized
    lowered = serialized.lower()
    assert '"pid' not in lowered
    assert '"ppid' not in lowered


def test_evidence_generation_modes_and_leak_guard() -> None:
    checks = [CheckResult("interpreter", "verified", {"version": "1"})]

    ci = build_evidence(checks, [], "ci")
    assert ci["ci_verified"] == ["interpreter"]
    assert ci["manual_verified"] == []
    assert ci["generated"]["by"] == "ci"

    manual = build_evidence(checks, [], "manual")
    assert manual["manual_verified"] == ["interpreter"]
    assert manual["ci_verified"] == []

    from tools.r0.platform_probe import _leak_self_check

    leaky = build_evidence([CheckResult("path_containment", "verified", {"detail": str(Path.home())})], [], "manual")
    with pytest.raises(ValueError, match="leak"):
        _leak_self_check(leaky)
