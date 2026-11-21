"""T-C3-012 evaluation set: tool/search bounded faces (glob + grep).

The bounded core (limit+1 fetch, slice, truncation marker, the grep timeout)
already ships; this set measures the FOUR delta faces the recon banked
(doc/plans/port-232-increments.md T-C3-012):

* D1 -- the metadata counters: glob ``{"count": n, "truncated": bool}`` and
  grep ``{"matches": n, "truncated": bool}`` (upstream glob.ts L114 /
  grep.ts L145);
* D2 -- the glob ``"undefined"``/``"null"`` literal path coercion to the
  working directory (glob.ts L74, the model-quirk guard; grep has no such
  face upstream);
* D3 -- the glob 30 s search deadline (``GLOB_TIMEOUT_S == 30.0``, the
  in-process os.walk counterpart of ``ToolSearch.bounded``'s
  ``timeoutOrElse``, same failure wording as grep's);
* D4 -- the malformed-glob error face: glob pattern and grep include report
  ``Invalid glob in pattern '<p>': unclosed character class; missing ']'``
  (the upstream test family's exact wording) instead of an empty result.

BEFORE the deltas land every scenario reads absent (the keys/faces are
missing); each must reach its goal status AFTER -- anything else is
drifting: fix the alignment, never the reading. The rg-dependent metrics
degrade honestly when no ripgrep binary exists (the metric stays false and
the report says so) instead of skipping the whole scenario.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from opencode.tools.builtin import glob as glob_module
from tests.integration.test_tool_static_trio import _run, _text


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _rg_available() -> str | None:
    found = shutil.which("rg")
    if found is None:
        bundled = sorted(Path.home().glob(r".vscode/extensions/*/bin/windows-x86_64/rg.exe"))
        if bundled:
            found = str(bundled[-1])
    return found


def _failure_text(outcome: Any) -> str:
    """The message text of a failed outcome ('' when it completed)."""
    if getattr(outcome, "state", "") != "failed":
        return ""
    try:
        return _text(outcome)
    except AssertionError:
        return ""


def _metadata(outcome: Any) -> dict[str, Any]:
    value = getattr(outcome, "metadata", None)
    return value if isinstance(value, dict) else {}


def _seed_files(root: Path, count: int, text: str = "needle\n") -> None:
    for index in range(count):
        (root / f"g{index}.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# P0 -- the capability surface
# ---------------------------------------------------------------------------


async def test_p0_capability(tmp_path: Path) -> None:
    rg = _rg_available()
    _seed_files(tmp_path, 3)
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "d.ts").write_text("needle\n", encoding="utf-8")

    metrics: dict[str, Any] = {
        "glob_metadata_count": False,
        "glob_undefined_null_coerced": False,
        "glob_invalid_pattern_face": False,
        "glob_timeout_constant": False,
        "grep_metadata_matches": False,
        "grep_invalid_include_face": False,
        "rg_absent": rg is None,
    }
    glob_out = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "limit": 2})
    metrics["glob_metadata_count"] = _metadata(glob_out).get("count") == 2
    coerced = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "path": "undefined"})
    metrics["glob_undefined_null_coerced"] = getattr(coerced, "state", "") == "completed" and "g0.txt" in _text(coerced)
    bad = await _run(tmp_path, "fs__glob", {"pattern": "["})
    metrics["glob_invalid_pattern_face"] = _failure_text(bad).startswith("Invalid glob in pattern")
    metrics["glob_timeout_constant"] = getattr(glob_module, "GLOB_TIMEOUT_S", None) == 30.0
    if rg is not None:
        grep_out = await _run(tmp_path, "fs__grep", {"pattern": "needle", "limit": 2})
        metrics["grep_metadata_matches"] = _metadata(grep_out).get("matches") == 2
        bad_include = await _run(tmp_path, "fs__grep", {"pattern": "needle", "include": "["})
        metrics["grep_invalid_include_face"] = _failure_text(bad_include).startswith("Invalid glob in include")
    required = [
        key for key in metrics if key not in ("rg_absent", "grep_metadata_matches", "grep_invalid_include_face")
    ]
    status = "probed" if all(metrics[key] for key in required) else "absent-partial"
    _report({"scenario": "P0-capability", "status": status, **metrics})
    assert status in ("absent-partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- the bounded parity anchor (green before AND after: proves the
# harness sees the already-shipped core, so the new faces are deltas, not
# the whole story)
# ---------------------------------------------------------------------------


async def test_m1_bounded_parity_anchor(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        _seed_files(tmp_path, 5)
        over = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "limit": 4})
        text = _text(over)
        metrics["four_names_shown"] = all(f"g{index}.txt" in text for index in range(4))
        metrics["truncation_marker"] = text.endswith(
            "\n\n(Results are truncated: showing first 4 results. Consider using a more specific path or pattern.)"
        )
        status = "bounded" if all(metrics.values()) else "drifting"
    except Exception:
        status = "drifting"
    _report({"scenario": "M1-bounded-anchor", "status": status, **metrics})
    assert status in ("capability-absent", "bounded", "drifting")


# ---------------------------------------------------------------------------
# M2 -- the glob deadline behavior (D3)
# ---------------------------------------------------------------------------


async def test_m2_glob_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    original = getattr(glob_module, "GLOB_TIMEOUT_S", None)
    try:
        if original is None:
            raise KeyError("GLOB_TIMEOUT_S missing")
        _seed_files(tmp_path, 5)
        monkeypatch.setattr(glob_module, "GLOB_TIMEOUT_S", 0.0)
        outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.txt"})
        message = _failure_text(outcome)
        metrics["zero_deadline_fails"] = getattr(outcome, "state", "") == "failed"
        metrics["upstream_wording"] = "Search timed out after 0 seconds." in message
        status = "enforced" if all(metrics.values()) else "drifting"
    except KeyError:
        pass
    finally:
        if original is not None:
            monkeypatch.setattr(glob_module, "GLOB_TIMEOUT_S", original)
    _report({"scenario": "M2-glob-deadline", "status": status, **metrics})
    assert status in ("capability-absent", "enforced", "drifting")


# ---------------------------------------------------------------------------
# M3 -- the metadata counters, exact (D1)
# ---------------------------------------------------------------------------


async def test_m3_metadata_counters_exact(tmp_path: Path) -> None:
    rg = _rg_available()
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        _seed_files(tmp_path, 5)
        glob_out = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "limit": 4})
        if "count" not in _metadata(glob_out):
            raise KeyError("count key missing")
        metrics["glob_exact"] = _metadata(glob_out) == {"count": 4, "truncated": True}
        if rg is not None:
            grep_out = await _run(tmp_path, "fs__grep", {"pattern": "needle", "limit": 4})
            if "matches" not in _metadata(grep_out):
                raise KeyError("matches key missing")
            metrics["grep_exact"] = _metadata(grep_out) == {"matches": 4, "truncated": True}
        status = "counted" if all(metrics.values()) else "drifting"
    except KeyError:
        pass
    _report({"scenario": "M3-metadata", "status": status, **metrics})
    assert status in ("capability-absent", "counted", "drifting")


# ---------------------------------------------------------------------------
# M4 -- the malformed-glob wording, exact (D4)
# ---------------------------------------------------------------------------


async def test_m4_invalid_glob_wording(tmp_path: Path) -> None:
    rg = _rg_available()
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        (tmp_path / "visible.ts").write_text("needle\n", encoding="utf-8")
        glob_out = await _run(tmp_path, "fs__glob", {"pattern": "["})
        if not _failure_text(glob_out).startswith("Invalid glob in pattern"):
            raise KeyError("glob error face missing")
        metrics["glob_wording"] = _failure_text(glob_out).startswith(
            "Invalid glob in pattern '[': unclosed character class; missing ']'"
        )
        if rg is not None:
            grep_out = await _run(tmp_path, "fs__grep", {"pattern": "needle", "include": "["})
            metrics["include_wording"] = _failure_text(grep_out).startswith("Invalid glob in include")
        status = "worded" if all(metrics.values()) else "drifting"
    except KeyError:
        pass
    _report({"scenario": "M4-invalid-glob", "status": status, **metrics})
    assert status in ("capability-absent", "worded", "drifting")
