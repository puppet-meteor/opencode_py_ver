"""R3-S3 integration tests: the static read trio on a real filesystem.

The real ``FsLsAdapter``/``FsGlobAdapter``/``FsGrepAdapter`` through a real
``ToolRegistry`` assembled with the host's default quartet (the cli.py L276
face), each over a real temporary workspace (``tmp_path`` is redirected to
the approved temp root by ``tests/integration/conftest.py``). ``fs__grep``
exercises the real external ripgrep binary wherever one can be located
(PATH first, then known bundled locations) and pins the rg-unavailable /
broken-rg diagnostic faces by injection. Contract anchors: plan §2 R3-S3,
CORE-CONTRACTS §5.1 L253/L255/L257, §5.2 L266, §5.3 L272-276,
PHASE1-SLIM §4 L91/L94 (acceptance A front half + verification line).
"""

from __future__ import annotations

import glob
import os
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

import pytest
from pydantic import BaseModel

import opencode.tools.builtin.grep as grep_module
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import (
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
)
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolRef
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import FsGlobAdapter, FsGrepAdapter, FsLsAdapter, FsReadAdapter

_FIXED_TIME = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_ittrio"
_RUN_ID = "run_ittrio"
_MESSAGE_ID = "msg_ittrio"
_TOOL_CALL_ID = "call_ittrio"


def _clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    """EventSink stand-in: records every committed batch verbatim."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []
        self.drafts: list[EventDraft[BaseModel]] = []

    async def __call__(self, drafts: Sequence[EventDraft[BaseModel]]) -> None:
        self.batches.append(tuple(drafts))
        self.drafts.extend(drafts)


def _registry_with_quartet(sink: _RecordingSink) -> ToolRegistry:
    """The host's default assembly (cli.py L276): all four static read tools."""
    registry = ToolRegistry(sink=sink, clock=_clock, ids=cast("Any", _FixedIds()))
    for adapter in (FsReadAdapter(), FsLsAdapter(), FsGlobAdapter(), FsGrepAdapter()):
        registry.register(adapter)
    registry.snapshot(_SESSION_ID)
    return registry


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


async def _run(working_dir: Path, wire_name: str, input: JsonObject) -> ToolOutcome:
    """One invoke through the quartet registry; outcome only."""
    registry = _registry_with_quartet(_RecordingSink())
    return await registry.invoke(_context(working_dir), wire_name, input, "exec_it")


def _text(outcome: ToolOutcome) -> str:
    """The single text content of a tool outcome."""
    parts = [part for part in outcome.content if isinstance(part, ToolTextContent)]
    assert len(parts) == 1
    return parts[0].text


def _symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink, skipping when the OS privilege is unavailable (R0-B)."""
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("symlink creation privilege not held (winerror 1314)")
        raise


# ---------------------------------------------------------------------------
# ripgrep location: PATH first, then known bundled copies (a real binary,
# never a fake) -- the rg-present faces skip honestly where none exists.
# ---------------------------------------------------------------------------


def _real_rg_or_skip() -> str:
    found = shutil.which("rg")
    if found is None:
        bundled = sorted(glob.glob(os.path.expanduser(r"~/.vscode/extensions/*/bin/windows-x86_64/rg.exe")))
        if bundled:
            found = bundled[-1]
    if found is None:
        pytest.skip("no ripgrep binary found on PATH or in known bundled locations")
    return found


def _patch_rg_path(monkeypatch: pytest.MonkeyPatch, executable: str | None) -> None:
    """Point grep's rg discovery at ``executable`` (None = absent) for one test."""
    monkeypatch.setattr(grep_module, "_RG_CACHE", {})
    monkeypatch.setattr(grep_module, "_which_rg", lambda: executable)


# ---------------------------------------------------------------------------
# fs__ls
# ---------------------------------------------------------------------------


async def test_ls_lists_entries_directories_first_sorted(tmp_path: Path) -> None:
    (tmp_path / "beta.txt").write_bytes(b"b\n")
    (tmp_path / "alpha.txt").write_bytes(b"a\n")
    (tmp_path / "zeta").mkdir()
    (tmp_path / "gamma").mkdir()

    outcome = await _run(tmp_path, "fs__ls", {})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.requested_name == "fs__ls"
    # filesystem.ts L196: directories first, then by name; L191: trailing '/'.
    assert _text(outcome) == "gamma/\nzeta/\nalpha.txt\nbeta.txt"


async def test_ls_hidden_entries_excluded_unless_requested(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_bytes(b"v\n")
    (tmp_path / ".hidden.txt").write_bytes(b"h\n")
    (tmp_path / ".cfg").mkdir()

    default = await _run(tmp_path, "fs__ls", {})
    assert _text(default) == "visible.txt"

    with_hidden = await _run(tmp_path, "fs__ls", {"hidden": True})
    assert _text(with_hidden) == ".cfg/\n.hidden.txt\nvisible.txt"


async def test_ls_nested_path_renders_relative_to_working_dir(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_bytes(b"i\n")
    (sub / "nested-dir").mkdir()

    outcome = await _run(tmp_path, "fs__ls", {"path": "sub"})

    assert _text(outcome) == "sub/nested-dir/\nsub/inner.txt"


async def test_ls_limit_boundary_marks_truncation(tmp_path: Path) -> None:
    for n in range(5):
        (tmp_path / f"f{n}.txt").write_bytes(b"x\n")

    exact = await _run(tmp_path, "fs__ls", {"limit": 5})
    # Exactly at the limit: every entry shown, no truncation marker.
    assert _text(exact) == "\n".join(f"f{n}.txt" for n in range(5))

    over = await _run(tmp_path, "fs__ls", {"limit": 4})
    text = _text(over)
    assert text.startswith("\n".join(f"f{n}.txt" for n in range(4)))
    assert text.endswith("\n\n(Results are truncated: showing first 4 results. Consider using a more specific path.)")


async def test_ls_empty_directory_message(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__ls", {})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "No entries found"


async def test_ls_missing_path_is_not_found(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__ls", {"path": "nope"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert "Path not found: nope" in _text(outcome)


async def test_ls_file_path_is_unsupported(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_bytes(b"f\n")

    outcome = await _run(tmp_path, "fs__ls", {"path": "file.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "Path is not a directory: file.txt" in _text(outcome)


async def test_ls_nul_in_path_is_validation_error(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__ls", {"path": "a\x00b"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Path contains NUL bytes" in _text(outcome)


async def test_ls_traversal_escape_is_denied(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")

    outcome = await _run(tmp_path, "fs__ls", {"path": "../outside/whatever"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(outcome)


async def test_ls_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").mkdir()
    _symlink_or_skip(outside / "secret", tmp_path / "escape-dir")

    outcome = await _run(tmp_path, "fs__ls", {"path": "escape-dir"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


@pytest.mark.skipif(_PLATFORM != "win32", reason="junctions are Windows-only")
async def test_ls_junction_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    # mypy narrowing: typeshed types _winapi as win32-only; the skipif above
    # already keeps the runtime off this body on non-Windows hosts.
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(tmp_path / "escape-junction"))
    except OSError as error:
        pytest.skip(f"junction creation unavailable: {type(error).__name__}")

    outcome = await _run(tmp_path, "fs__ls", {"path": "escape-junction"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


async def test_ls_event_sequence_allow_root_internal(tmp_path: Path) -> None:
    """§5.1 L257 requested→started→finished through the quartet registry;
    §5.2 L266: root-internal ls runs allow (R2 allow face, no approval)."""
    (tmp_path / "notes.txt").write_bytes(b"n\n")
    sink = _RecordingSink()
    registry = _registry_with_quartet(sink)
    context = _context(tmp_path)

    outcome = await registry.invoke(context, "fs__ls", {}, "exec_001")

    assert outcome.state == "succeeded"
    assert outcome.tool_ref == ToolRef(namespace="fs", name="ls", version=1)
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED, PY_TOOL_FINISHED]
    requested = sink.drafts[0].data
    assert isinstance(requested, ToolRequestedData)
    assert requested.requested_name == "fs__ls"
    assert requested.tool_ref == ToolRef(namespace="fs", name="ls", version=1)
    assert requested.input == {}
    started = sink.drafts[1].data
    assert isinstance(started, ToolStartedData)
    assert started.permission_decision == "allow"
    finished = sink.drafts[2].data
    assert isinstance(finished, ToolFinishedData)
    assert (finished.state, finished.effect_status) == ("succeeded", "completed")


# ---------------------------------------------------------------------------
# fs__glob
# ---------------------------------------------------------------------------


async def test_glob_matches_pattern_sorted_files_only(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_bytes(b"b\n")
    (tmp_path / "a.txt").write_bytes(b"a\n")
    (tmp_path / "skip.md").write_bytes(b"s\n")
    (tmp_path / "only-dir.txt").mkdir()  # directories are never glob results

    outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.txt"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.requested_name == "fs__glob"
    assert _text(outcome) == "a.txt\nb.txt"


async def test_glob_recursive_double_star_matches_nested(tmp_path: Path) -> None:
    (tmp_path / "root.txt").write_bytes(b"r\n")
    deep = tmp_path / "src" / "lib"
    deep.mkdir(parents=True)
    (deep / "deep.ts").write_bytes(b"d\n")
    (tmp_path / "src" / "top.ts").write_bytes(b"t\n")

    outcome = await _run(tmp_path, "fs__glob", {"pattern": "**/*.ts"})

    assert _text(outcome) == "src/lib/deep.ts\nsrc/top.ts"


async def test_glob_hidden_excluded_by_default(tmp_path: Path) -> None:
    (tmp_path / "open.txt").write_bytes(b"o\n")
    (tmp_path / ".secret.txt").write_bytes(b"s\n")

    default = await _run(tmp_path, "fs__glob", {"pattern": "*.txt"})
    assert _text(default) == "open.txt"

    with_hidden = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "hidden": True})
    assert _text(with_hidden) == ".secret.txt\nopen.txt"


async def test_glob_no_matches_message(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a\n")

    outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.zzz"})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "No files found"


async def test_glob_limit_boundary_marks_truncation(tmp_path: Path) -> None:
    for n in range(5):
        (tmp_path / f"g{n}.txt").write_bytes(b"x\n")

    exact = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "limit": 5})
    assert _text(exact) == "\n".join(f"g{n}.txt" for n in range(5))

    over = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "limit": 4})
    text = _text(over)
    assert text.startswith("\n".join(f"g{n}.txt" for n in range(4)))
    assert text.endswith(
        "\n\n(Results are truncated: showing first 4 results. Consider using a more specific path or pattern.)"
    )


async def test_glob_search_path_missing_is_not_found(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "path": "nope"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert "Search path does not exist: nope" in _text(outcome)


async def test_glob_search_path_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "file.md").write_bytes(b"f\n")

    outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "path": "file.md"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "Search path is not a directory: file.md" in _text(outcome)


async def test_glob_escaping_pattern_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a\n")

    absolute = await _run(tmp_path, "fs__glob", {"pattern": str(tmp_path / "*.txt")})
    assert absolute.state == "failed"
    assert absolute.error is not None
    assert absolute.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Pattern must be relative" in _text(absolute)

    parent = await _run(tmp_path, "fs__glob", {"pattern": "../outside/*.txt"})
    assert parent.state == "failed"
    assert parent.error is not None
    assert parent.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Pattern must be relative" in _text(parent)


async def test_glob_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"s\n")
    _symlink_or_skip(outside, tmp_path / "escape-dir")

    outcome = await _run(tmp_path, "fs__glob", {"pattern": "*.txt", "path": "escape-dir"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# fs__grep (external ripgrep dependency)
# ---------------------------------------------------------------------------


async def test_grep_finds_matches_grouped_by_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "a.txt").write_bytes(b"needle here\nplain\nneedle again\n")
    (tmp_path / "b.txt").write_bytes(b"start\nneedle\n")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.requested_name == "fs__grep"
    # grep.ts toModelContent L44-55: count, per-file headers, line previews.
    assert _text(outcome) == (
        "Found 3 matches\na.txt:\n  Line 1: needle here\n  Line 3: needle again\n\nb.txt:\n  Line 2: needle"
    )


async def test_grep_case_sensitive_by_default_with_insensitive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "case.txt").write_bytes(b"Alpha\nalpha\n")

    default = await _run(tmp_path, "fs__grep", {"pattern": "Alpha"})
    assert "Found 1 matches" in _text(default)

    insensitive = await _run(tmp_path, "fs__grep", {"pattern": "Alpha", "caseSensitive": False})
    assert "Found 2 matches" in _text(insensitive)


async def test_grep_literal_mode_escapes_regex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "dots.txt").write_bytes(b"a.b\naxb\n")

    literal = await _run(tmp_path, "fs__grep", {"pattern": "a.b", "literal": True})
    text = _text(literal)
    assert "Found 1 matches" in text
    assert "  Line 1: a.b" in text

    regex = await _run(tmp_path, "fs__grep", {"pattern": "a.b"})
    assert "Found 2 matches" in _text(regex)


async def test_grep_include_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "keep.py").write_bytes(b"needle\n")
    (tmp_path / "drop.txt").write_bytes(b"needle\n")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle", "include": "*.py"})

    assert _text(outcome) == "Found 1 matches\nkeep.py:\n  Line 1: needle"


async def test_grep_single_file_path_targets_that_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "one.txt").write_bytes(b"needle\n")
    (tmp_path / "two.txt").write_bytes(b"needle\n")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle", "path": "one.txt"})

    assert _text(outcome) == "Found 1 matches\none.txt:\n  Line 1: needle"


async def test_grep_no_matches_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "a.txt").write_bytes(b"plain\n")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})

    assert outcome.state == "succeeded"  # rg exits 1: "no matches" is a clean result
    assert _text(outcome) == "No matches found"


async def test_grep_limit_boundary_marks_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "many.txt").write_bytes(b"".join(f"needle {n}\n".encode() for n in range(5)))

    exact = await _run(tmp_path, "fs__grep", {"pattern": "needle", "limit": 5})
    text = _text(exact)
    assert "Found 5 matches" in text
    assert "truncated" not in text

    over = await _run(tmp_path, "fs__grep", {"pattern": "needle", "limit": 4})
    text = _text(over)
    assert "Found 4 matches" in text
    assert text.endswith(
        "\n\n(Results are truncated: showing first 4 results. Consider using a more specific path or pattern.)"
    )


async def test_grep_empty_pattern_is_validation_error(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__grep", {"pattern": ""})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Pattern must not be empty" in _text(outcome)


async def test_grep_invalid_regex_is_tool_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "a.txt").write_bytes(b"plain\n")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "(["})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "Invalid regex pattern" in _text(outcome)


@pytest.mark.skipif(_PLATFORM != "win32", reason="the byte-range lock face is Windows-only (msvcrt)")
async def test_grep_locked_sibling_file_is_tolerated_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-C1-033 task-B empirical pin (Windows leg, measured not assumed): a
    sibling file locked by another handle -- the exact writer.lock shape
    (db.py: ``msvcrt.locking`` over 1 byte at offset 0) -- makes rg exit 2
    mid-scan. The TS three-diagnosis (ripgrep.ts L144-152) tolerates a
    code-2 non-pattern error: the already-collected rows are the answer,
    never a TOOL_FAILED wedge. A-PLATFORM: the POSIX leg (e.g. an
    unreadable chmod-000 file) is not woven here -- no Linux evidence is
    fabricated; it is disclosed in the progress file for CI to measure."""
    # mypy narrowing: typeshed types msvcrt as win32-only; the skipif above
    # keeps the runtime off this body on non-Windows hosts.
    if sys.platform != "win32":
        pytest.skip("the byte-range lock face is Windows-only")
    import msvcrt

    rg = _real_rg_or_skip()
    _patch_rg_path(monkeypatch, rg)
    (tmp_path / "readable.txt").write_bytes(b"needle here\n")
    locked_path = tmp_path / "locked.txt"
    locked_path.write_bytes(b"needle there\n")

    fd = os.open(locked_path, os.O_RDWR)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    try:
        outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})
    finally:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert _text(outcome) == "Found 1 matches\nreadable.txt:\n  Line 1: needle here"


async def test_grep_missing_search_path_is_not_found(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle", "path": "nope"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert "Search path does not exist: nope" in _text(outcome)


async def test_grep_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").mkdir()
    (outside / "secret" / "s.txt").write_bytes(b"needle\n")
    _symlink_or_skip(outside / "secret", tmp_path / "escape-dir")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle", "path": "escape-dir"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


async def test_grep_binary_file_is_tool_failed(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"ok\x00needle")

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle", "path": "blob.bin"})

    assert (outcome.state, outcome.effect_status) == ("failed", "unknown")  # §2.1 L50 (T-C1-033)
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "Cannot grep binary file: blob.bin" in _text(outcome)


async def test_grep_rg_unavailable_is_explicit_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rg-missing face (risk table S3): explicit diagnostic, no silent fallback."""
    (tmp_path / "a.txt").write_bytes(b"needle\n")
    _patch_rg_path(monkeypatch, None)

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})

    assert (outcome.state, outcome.effect_status) == ("failed", "unknown")  # §2.1 L50 (T-C1-033)
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    text = _text(outcome)
    assert "ripgrep (rg) is an external dependency" in text
    assert "not found on PATH" in text
    assert "no silent fallback search is performed" in text


async def test_grep_broken_rg_is_explicit_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH hit that is not a working ripgrep is diagnosed, not silently used."""
    (tmp_path / "a.txt").write_bytes(b"needle\n")
    _patch_rg_path(monkeypatch, sys.executable)  # a runnable binary that is not rg

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "did not identify as ripgrep" in _text(outcome)


@pytest.mark.skipif(
    shutil.which("rg") is not None, reason="rg is on PATH: the absent face is pinned by the patched test"
)
async def test_grep_real_path_absence_is_the_diagnostic_face(tmp_path: Path) -> None:
    """Un-patched discovery in an environment where rg is genuinely absent from
    PATH (this machine): the diagnostic is the observed real behavior."""
    (tmp_path / "a.txt").write_bytes(b"needle\n")
    grep_module._RG_CACHE.clear()

    outcome = await _run(tmp_path, "fs__grep", {"pattern": "needle"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "not found on PATH" in _text(outcome)
