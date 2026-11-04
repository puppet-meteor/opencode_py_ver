"""S6 regression batch (scheduler row #32, minor-1): multi-root smoke for the
glob/ls/grep adapters plus the ``_display_path`` cross-drive fallback.

R3-S6 split the fs boundary in two: the Invoker/policy single-root frozen
face (containment against the working directory) and the adapter multi-root
face (declared roots extend the boundary after the working directory —
test_config_loader.py pins read/write there). This file pins the remaining
three static read tools: a secondary-root path is *allowed* through its
absolute form, its entries *render* absolute (the only shape that
round-trips through the multi-root resolver), and the default adapter
(roots=()) keeps the exact R2 single-root rejection.

The ``_display_path`` cross-drive test is pure string math, so no
skip-if-single-drive is needed: on Windows the ``ValueError`` branch fires
for any cross-drive pair (drive letters decide, not drive existence); on
POSIX the same inputs take the lexical ``..`` guard branch with the
identical absolute-form outcome (④ disclosure — both branches reach the
same contract, only one of them executes per platform).
"""

from __future__ import annotations

import glob as globlib
import os
import shutil
from pathlib import Path

import pytest

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import ToolTextContent
from opencode.contracts.tools import ToolResult
from opencode.tools import ToolContext, ToolLimits
from opencode.tools.builtin import FsGlobAdapter, FsGrepAdapter, FsLsAdapter
from opencode.tools.builtin import grep as grep_module
from opencode.tools.builtin.read import _display_path

_SESSION_ID = "ses_multiroot0000001"
_RUN_ID = "run_multiroot0000001"
_MESSAGE_ID = "msg_multiroot00000001"
_TOOL_CALL_ID = "tcl_multiroot00000001"


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


def _text(result: ToolResult) -> str:
    parts = [part for part in result.content if isinstance(part, ToolTextContent)]
    assert len(parts) == 1
    return parts[0].text


def _real_rg_or_skip() -> str:
    """A real ripgrep binary or an honest skip (test_tool_static_trio.py idiom)."""
    found = shutil.which("rg")
    if found is None:
        bundled = sorted(globlib.glob(os.path.expanduser(r"~/.vscode/extensions/*/bin/windows-x86_64/rg.exe")))
        if bundled:
            found = bundled[-1]
    if found is None:
        pytest.skip("no ripgrep binary found on PATH or in known bundled locations")
    return found


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """working/ + secondary/ with one file each (and a dir in the secondary root)."""
    working = tmp_path / "wd"
    secondary = tmp_path / "sec"
    working.mkdir()
    secondary.mkdir()
    (working / "inside.txt").write_bytes(b"inside\n")
    (secondary / "sub").mkdir()
    (secondary / "b.txt").write_bytes(b"needle in the secondary root\n")
    return working, secondary


async def test_glob_secondary_root_allows_and_renders_absolute(tmp_path: Path) -> None:
    working, secondary = _layout(tmp_path)
    adapter = FsGlobAdapter(roots=(str(secondary),))

    result = await adapter.execute(_context(working), {"pattern": "*.txt", "path": str(secondary)})

    assert result.error is None
    assert _text(result) == (secondary.resolve() / "b.txt").as_posix()


async def test_ls_secondary_root_allows_and_renders_absolute(tmp_path: Path) -> None:
    working, secondary = _layout(tmp_path)
    adapter = FsLsAdapter(roots=(str(secondary),))

    result = await adapter.execute(_context(working), {"path": str(secondary)})

    assert result.error is None
    # directories first with the trailing '/', then files — all absolute (multi-root render).
    assert _text(result) == "\n".join(
        [(secondary.resolve() / "sub").as_posix() + "/", (secondary.resolve() / "b.txt").as_posix()]
    )


async def test_grep_secondary_root_allows_and_renders_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rg = _real_rg_or_skip()
    monkeypatch.setattr(grep_module, "_RG_CACHE", {})
    monkeypatch.setattr(grep_module, "_which_rg", lambda: rg)
    working, secondary = _layout(tmp_path)
    adapter = FsGrepAdapter(roots=(str(secondary),))

    result = await adapter.execute(_context(working), {"pattern": "needle", "path": str(secondary)})

    assert result.error is None
    # rg runs with file_arg "." and reports "./b.txt"; the *absolute-form*
    # render keeps that segment (the relative branch would collapse it via
    # relpath). Pinned as the current face — a cosmetic wart disclosed ④
    # (semantically the same path; the regression batch changes no tools).
    sec = secondary.resolve().as_posix()
    assert _text(result) == f"Found 1 matches\n{sec}/./b.txt:\n  Line 1: needle in the secondary root"


async def test_glob_default_adapter_keeps_the_secondary_root_out(tmp_path: Path) -> None:
    """roots=() is the exact R2 single-root face: the secondary directory is
    not part of the boundary and is rejected as an escape."""
    working, secondary = _layout(tmp_path)
    adapter = FsGlobAdapter()  # no declared roots

    result = await adapter.execute(_context(working), {"pattern": "*.txt", "path": str(secondary)})

    assert result.error is not None and result.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(result)


def test_display_path_cross_drive_and_dotdot_fallbacks() -> None:
    """nit-2(a): outside the working directory the absolute form is returned —
    cross-drive pairs through the ``ValueError`` branch on Windows, the lexical
    ``..`` guard elsewhere; inside stays relative with '/' separators."""
    assert _display_path("C:/wd", "D:/sec/a.txt") == "D:/sec/a.txt"
    assert _display_path("C:/wd", "C:/elsewhere/x.txt") == "C:/elsewhere/x.txt"
    assert _display_path("C:/wd", "C:/wd/sub/a.txt") == "sub/a.txt"
    assert _display_path("C:/wd", "C:/wd/a.txt") == "a.txt"
