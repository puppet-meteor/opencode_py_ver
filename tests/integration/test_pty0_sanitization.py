"""PTY T0 spike mechanism tests: sanitization, leak guard, classification.

Mirrors the r0 platform-probe test form: the evidence generator keeps
``restricted``/``unsupported`` strictly separate from verified (r0b-2
semantics), the sanitization pass replaces workspace/temp paths with
``<WORKSPACE>``/``<TEMP>`` placeholders, the leak self-check rejects any
surviving host path, username or PID-like key, and the committed evidence
artifact stays sanitized. Capability-restricted faces are the probe's
``restricted`` record with an actionable reason -- never an unconditional
naked skip.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from tools.pty0.pty_probe import (
    PROBE_VERSION,
    CheckResult,
    _leak_self_check,
    _sanitize,
    build_evidence,
    probe_resize,
)

EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "doc" / "evidence" / "pty0" / "platform.json"


def test_evidence_generation_modes_and_restricted_never_verified() -> None:
    checks = [CheckResult("resize", "verified", {"k": 1})]

    ci = build_evidence(checks, [], "ci")
    assert ci["ci_verified"] == ["resize"]
    assert ci["manual_verified"] == []
    assert ci["restricted"] == []
    assert ci["generated"]["by"] == "ci"

    manual = build_evidence(checks, [], "manual")
    assert manual["manual_verified"] == ["resize"]
    assert manual["ci_verified"] == []
    assert manual["generated"]["by"] == "manual"

    # Regression: a restricted check must never masquerade as verified in
    # ci_verified/manual_verified; it is recorded in its own `restricted` list.
    mixed = [
        CheckResult("resize", "verified", {}),
        CheckResult("ctrl_c", "restricted", {}, reason="capability unavailable in this environment"),
    ]
    unsupported_entry = {
        "check": "ctrl_c",
        "capability": "pty input delivery",
        "platform": sys.platform,
        "reason": "capability unavailable in this environment",
    }
    evidence = build_evidence(mixed, [unsupported_entry], "manual")
    assert evidence["manual_verified"] == ["resize"]
    assert evidence["ci_verified"] == []
    assert "ctrl_c" not in evidence["manual_verified"]
    assert evidence["restricted"] == ["ctrl_c"]
    assert evidence["unsupported"] == [unsupported_entry]
    assert evidence["probe_version"] == PROBE_VERSION


def test_sanitize_replaces_workspace_and_temp_paths() -> None:
    workspace = str(Path(__file__).resolve().parents[2])
    scratch = str(Path(tempfile.gettempdir()) / "pty0-scratch")
    leaky = f"probe ran under {workspace}{os.sep}tools and wrote {scratch}"

    out = _sanitize(leaky)
    assert workspace not in out
    assert tempfile.gettempdir() not in out
    assert "<WORKSPACE>" in out
    assert "<TEMP>" in out

    nested = _sanitize({"dir": workspace, "list": [scratch], "int": 5, "none": None})
    assert workspace not in json.dumps(nested)
    assert tempfile.gettempdir() not in json.dumps(nested)
    assert "<WORKSPACE>" in json.dumps(nested)
    assert "<TEMP>" in json.dumps(nested)
    assert nested["int"] == 5 and nested["none"] is None


def test_leak_self_check_rejects_home_paths_and_pid_keys() -> None:
    leaky_home = build_evidence([CheckResult("resize", "verified", {"detail": str(Path.home())})], [], "manual")
    with pytest.raises(ValueError, match="leak"):
        _leak_self_check(leaky_home)

    user = getpass.getuser()
    if user:
        with pytest.raises(ValueError, match="leak"):
            _leak_self_check({"checks": {"resize": {"detail": f"hello {user}"}}})

    with pytest.raises(ValueError, match="leak"):
        _leak_self_check({"checks": {"resize": {"pid": 123}}})
    with pytest.raises(ValueError, match="leak"):
        _leak_self_check({"checks": {"resize": {"ppid": 122}}})

    clean = build_evidence([CheckResult("resize", "verified", {"elapsed_ms": 42})], [], "manual")
    _leak_self_check(clean)  # no raise


def test_committed_windows_evidence_is_sanitized() -> None:
    """The committed local-run artifact (doc/evidence/pty0/) stays leak-free.

    The committed file is the local Windows manual leg; the POSIX leg is
    carried by CI dual-OS and closed by the dispatcher -- manual and
    ci_verified never impersonate each other.
    """
    assert EVIDENCE_PATH.exists(), "committed probe evidence doc/evidence/pty0/platform.json is missing"
    serialized = EVIDENCE_PATH.read_text(encoding="utf-8")
    evidence = json.loads(serialized)

    assert evidence["probe_version"] == PROBE_VERSION
    assert evidence["generated"]["by"] == "manual"
    assert evidence["platform"]["sys_platform"] == "win32"
    assert set(evidence["checks"]) == {"resize", "ctrl_c", "encoding", "process_tree_exit"}
    for name in evidence["restricted"]:
        assert name not in evidence["manual_verified"]
        assert name not in evidence["ci_verified"]
    for check in evidence["checks"].values():
        assert check["status"] in {"verified", "restricted"}, check

    for forbidden in (str(Path.home()), tempfile.gettempdir()):
        assert forbidden not in serialized
    user = getpass.getuser()
    if user:
        assert user not in serialized
    lowered = serialized.lower()
    assert '"pid' not in lowered
    assert '"ppid' not in lowered


def test_capability_restricted_face_records_reason_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """r0b-2: the skip face is the probe's restricted record with a reason.

    The primitive tests skip ON this record (reason + evidence pointer); this
    pins that the probe side actually produces the record -- capability
    restrictions are never silent, never a pass, never a naked skip.
    """
    from tools.pty0 import pty_probe

    if sys.platform == "win32":
        monkeypatch.setattr(pty_probe, "_winpty_available", lambda: False)
    else:
        monkeypatch.setattr(pty_probe, "_posix_pty_available", lambda: False)

    result = probe_resize()
    assert result.status == "restricted"
    assert result.reason
    assert result.details["mechanism"]

    evidence = build_evidence([result], [], "manual")
    assert evidence["restricted"] == ["resize"]
    assert "resize" not in evidence["manual_verified"]
    assert evidence["manual_verified"] == []
