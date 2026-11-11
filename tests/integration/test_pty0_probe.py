"""PTY T0 spike integration tests: four primitives x dual platform (p2-slices L175).

Runs the CURRENT platform's leg of each primitive probe (platform-agnostic
test form with current-platform branching; CI dual-OS carries the POSIX leg
-- the dispatcher closes the dual-platform record; this suite never
impersonates a POSIX result on Windows or vice versa).

- resize: a running child observes the PTY size change (pywinpty ConPTY
  ``setwinsize`` on Windows; ``fcntl`` TIOCSWINSZ + observable SIGWINCH on
  POSIX);
- Ctrl-C: ``\\x03`` written to the PTY input interrupts the child with an
  observable marker + exit code 130 (never "sent and assumed");
- encoding: a multi-script UTF-8 payload round-trips through the PTY in
  both directions with no mojibake (echo round trip);
- process-tree exit: a Job Object (Windows, ctypes direct -- r0 form) /
  process group (POSIX, ``os.killpg``) kill covers the PTY child AND a
  grandchild the child spawned explicitly (PHASE1-SLIM L150 red line).

Timing bounds are generous deadlines (CI slow-runner compatible, W1 spike
precedent); no tight upper-bound asserts. Capability-restricted faces skip
with the probe's recorded restriction reason + evidence pointer (r0b-2
semantics: restricted never masquerades as verified, never a naked skip).
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
from tools.pty0.pty_probe import (
    PROBE_VERSION,
    CheckResult,
    probe_ctrl_c,
    probe_encoding,
    probe_process_tree_exit,
    probe_resize,
)

PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "pty0" / "pty_probe.py"

_EVIDENCE_NOTE = "probe evidence: doc/evidence/pty0/platform.json"
_FOUR_PRIMITIVES = {"resize", "ctrl_c", "encoding", "process_tree_exit"}


def _require_verified(result: CheckResult) -> None:
    """r0b-2 form: a restricted face skips WITH its recorded reason + evidence pointer."""
    if result.status == "restricted":
        pytest.skip(f"{result.name} restricted on this platform: {result.reason} ({_EVIDENCE_NOTE})")
    assert result.status == "verified", (result.name, result.reason, result.details)


def test_resize_observed_by_running_child() -> None:
    result = probe_resize()
    _require_verified(result)
    details = result.details
    assert details["set_columns"] == 120
    assert details["set_rows"] == 50
    assert details["child_observed_new_size"] is True
    assert details["observed_before"] != details["observed_after"]
    if sys.platform != "win32":
        assert details["sigwinch_observed"] is True  # POSIX criterion: SIGWINCH observable


def test_ctrl_c_interrupts_child_with_observable_exit() -> None:
    result = probe_ctrl_c()
    _require_verified(result)
    details = result.details
    assert details["marker_observed"] is True
    assert details["child_exit_code"] == 130
    assert details["child_alive_after_interrupt"] is False


def test_utf8_round_trip_has_no_mojibake() -> None:
    result = probe_encoding()
    _require_verified(result)
    details = result.details
    payload = details["payload"]
    assert any(ord(char) > 127 for char in payload)  # really non-ASCII, multi-script
    assert details["banner_observed"] is True
    assert details["echo_observed"] is True
    assert details["exact_match"] is True


def test_process_tree_exit_covers_child_and_grandchild() -> None:
    result = probe_process_tree_exit()
    _require_verified(result)
    details = result.details
    assert details["grandchild_alive_before_kill"] is True
    assert details["child_terminated"] is True
    assert details["grandchild_terminated"] is True
    assert details["all_exited_within_grace"] is True
    assert details["elapsed_ms"] is not None
    if sys.platform == "win32":
        assert "Job Object" in details["mechanism"]
    else:
        assert "process group" in details["mechanism"]


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

    assert evidence["probe_version"] == PROBE_VERSION == "pty0-1"
    for key in ("platform", "ci_verified", "manual_verified", "restricted", "unsupported", "checks", "notes"):
        assert key in evidence, key
    assert set(evidence["checks"]) == _FOUR_PRIMITIVES
    for name, check in evidence["checks"].items():
        assert check["status"] in {"verified", "restricted"}, (name, check)
    assert evidence["ci_verified"] == []  # manual mode
    if any(check["status"] == "restricted" for check in evidence["checks"].values()):
        assert evidence["restricted"]
        # Restricted checks are never listed as verified.
        for name in evidence["restricted"]:
            assert name not in evidence["manual_verified"]
            assert name not in evidence["ci_verified"]
    else:
        assert evidence["restricted"] == []
        assert sorted(evidence["manual_verified"]) == sorted(_FOUR_PRIMITIVES)

    # Leak guard (r0 precedent): no host paths, no username, no PID-like keys.
    for forbidden in (str(Path.home()), tempfile.gettempdir()):
        assert forbidden not in serialized
    user = getpass.getuser()
    if user:
        assert user not in serialized
    lowered = serialized.lower()
    assert '"pid' not in lowered
    assert '"ppid' not in lowered
