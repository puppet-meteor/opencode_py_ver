"""Builtin tool-output truncation tests (T-C3-001; upstream ``tool-output.ts`` @2.3.2).

The head-preserving truncation with full-output persistence, through the
public :func:`opencode.tools.tool_output.truncate_contents` face and the
invoker normalization seam:

* within limits -- the content passes through unchanged;
* over the line limit -- the head is kept, the FULL text lands under the
  target directory, and the marker names the range, the total and the saved
  file's path;
* over the byte limit -- the UTF-8 budget is respected without splitting a
  code point, and the CR of a CRLF pair is never orphaned;
* multi-part -- the cut is redistributed across the original text parts and
  file items pass through untouched;
* the write-failure face -- the marker keeps the failure reason (a cap is
  never silent);
* retention cleanup removes only expired ``tool_<hex>`` files;
* the invoker seam applies the same truncation on ``_normalize_result``;
* the upstream opt-out contract -- a result whose ``metadata["truncated"]``
  is defined (set by every adapter enforcing its own honest caps) skips
  the seam truncation entirely (upstream ``tool-output.ts`` L76).
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from pathlib import Path

from opencode.contracts.messages import ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools import invoker as invoker_module
from opencode.tools import tool_output
from opencode.tools.invoker import ToolLimits
from opencode.tools.tool_output import truncate_contents


def _texts(content: Sequence[object]) -> list[str]:
    return [item.text for item in content if isinstance(item, ToolTextContent)]


def test_within_limits_passes_through_unchanged(tmp_path: Path) -> None:
    original = [ToolTextContent(type="text", text="\n".join(f"line {i}" for i in range(10)))]
    result = truncate_contents(original, directory=tmp_path)
    assert _texts(result) == _texts(original)
    assert list(tmp_path.iterdir()) == []


def test_over_line_limit_keeps_head_and_saves_full_output(tmp_path: Path) -> None:
    text = "\n".join(f"line {i}" for i in range(10))
    result = truncate_contents([ToolTextContent(type="text", text=text)], directory=tmp_path, max_lines=3)
    texts = _texts(result)
    assert texts[0] == "line 0\nline 1\nline 2"
    marker = texts[1]
    assert marker.startswith(f"[showing lines 1-3 of 10; full output saved to {tmp_path}")
    assert "(last line cut)" not in marker
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == text
    assert tool_output._FILE_RE.match(saved[0].name)


def test_over_byte_limit_respects_code_points(tmp_path: Path) -> None:
    text = "漢" * 100  # one line, 300 UTF-8 bytes
    result = truncate_contents([ToolTextContent(type="text", text=text)], directory=tmp_path, max_bytes=30)
    texts = _texts(result)
    assert texts[0] == "漢" * 10  # exactly 30 bytes, no code point split
    assert "[showing lines 1-1 of 1 (last line cut); full output saved to" in texts[1]
    assert len(texts[0].encode("utf-8")) <= 30


def test_byte_boundary_never_splits_crlf(tmp_path: Path) -> None:
    text = "aaa\r\nbbb\r\nccc"
    result = truncate_contents([ToolTextContent(type="text", text=text)], directory=tmp_path, max_bytes=4)
    texts = _texts(result)
    # budget 4 would cut between \r and \n; the boundary backs off to "aaa"
    assert texts[0] == "aaa"
    # "aaa" is a complete line (its \r\n follows intact), so no last-line-cut
    assert texts[1].startswith("[showing lines 1-1 of 3; full output saved to")


def test_multi_part_redistribution_and_files_pass_through(tmp_path: Path) -> None:
    first = ToolTextContent(type="text", text="alpha\nbeta\n")
    artifact = ToolFileContent(type="file", uri="file:///tmp/report.html", mime="text/html", name="report.html")
    second = ToolTextContent(type="text", text="gamma\ndelta\nepsilon")
    result = truncate_contents([first, artifact, second], directory=tmp_path, max_lines=3)
    assert result[0] == first
    assert result[1] == artifact  # untouched, still in position
    # the head budget lands inside the second part: "alpha\nbeta\n" (11) of
    # the 16-char head, leaving "gamma" (5) of it
    assert isinstance(result[2], ToolTextContent) and result[2].text == "gamma"
    marker = result[3]
    assert isinstance(marker, ToolTextContent)
    # the cut landed on a line boundary (after "gamma\n"), so no last-line-cut
    assert marker.text.startswith("[showing lines 1-3 of 5; full output saved to")
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "alpha\nbeta\ngamma\ndelta\nepsilon"


def test_write_failure_stays_in_the_marker(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    text = "\n".join(f"line {i}" for i in range(10))
    result = truncate_contents([ToolTextContent(type="text", text=text)], directory=blocker, max_lines=3)
    marker = _texts(result)[-1]
    assert "full output could not be saved:" in marker
    assert "[showing lines 1-3 of 10" in marker


def test_second_pass_is_a_noop(tmp_path: Path) -> None:
    text = "\n".join(f"line {i}" for i in range(10))
    once = truncate_contents([ToolTextContent(type="text", text=text)], directory=tmp_path, max_lines=3)
    twice = truncate_contents(once, directory=tmp_path, max_lines=3)
    assert _texts(twice) == _texts(once)
    assert len(list(tmp_path.iterdir())) == 1  # no second file


def test_the_metadata_flag_opts_out_of_seam_truncation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Upstream tool-output.ts L76: a defined metadata.truncated skips the seam.

    bash/pty/read/git/glob/grep/ls enforce their own honest caps and set the
    flag on every content-bearing result, so the seam never re-truncates
    them (whatever the flag's value -- defined IS the opt-out).
    """
    monkeypatch.setattr(tool_output, "data_directory", lambda explicit=None: tmp_path)
    spec = ToolSpec(
        namespace="t",
        name="t",
        version=1,
        description="d",
        input_schema={},
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=False,
    )
    big = "\n".join(f"l{i}" for i in range(2500))
    for flag in (True, False):
        result = ToolResult(content=[ToolTextContent(type="text", text=big)], metadata={"truncated": flag})
        content, _structured = invoker_module._normalize_result(spec, result, ToolLimits())
        assert content == [ToolTextContent(type="text", text=big)]  # untouched, no marker
    assert list(tmp_path.iterdir()) == []  # no save happened


def test_cleanup_removes_only_expired_outputs(tmp_path: Path) -> None:
    stale = tmp_path / "tool_0000000000aa"
    stale.write_text("old", encoding="utf-8")
    fresh = tmp_path / "tool_0000000000bb"
    fresh.write_text("new", encoding="utf-8")
    other = tmp_path / "notes.txt"
    other.write_text("keep", encoding="utf-8")
    old = time.time() - 8 * 86400
    os.utime(stale, (old, old))
    removed = tool_output.cleanup(tmp_path)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_the_invoker_seam_applies_truncation(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(tool_output, "data_directory", lambda explicit=None: tmp_path)
    spec = ToolSpec(
        namespace="t",
        name="t",
        version=1,
        description="d",
        input_schema={},
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=False,
    )
    big = "\n".join(f"l{i}" for i in range(2500))
    result = ToolResult(content=[ToolTextContent(type="text", text=big)])
    content, _structured = invoker_module._normalize_result(spec, result, ToolLimits())
    joined = "".join(item.text for item in content if isinstance(item, ToolTextContent))
    assert "[showing lines 1-2000 of 2500" in joined
    saved = [p for p in tmp_path.iterdir() if p.name.startswith("tool_")]
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == big


def test_the_invoker_seam_leaves_small_results_alone(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(tool_output, "data_directory", lambda explicit=None: tmp_path)
    spec = ToolSpec(
        namespace="t",
        name="t",
        version=1,
        description="d",
        input_schema={},
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=False,
    )
    result = ToolResult(content=[ToolTextContent(type="text", text="small")])
    content, _structured = invoker_module._normalize_result(spec, result, ToolLimits())
    assert _texts(content) == ["small"]
    assert list(tmp_path.iterdir()) == []
