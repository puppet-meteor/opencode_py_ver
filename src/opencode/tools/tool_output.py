"""Tool output truncation (T-C3-001; upstream ``packages/core/src/tool-output.ts`` @v2.3.2).

Head-preserving truncation with full-output persistence at the result
normalization seam: text over the line/byte limits keeps its beginning, the
full text is written under the data root's ``tool-output/`` directory
(7-day retention), and the preview ends with the upstream marker sentence
carrying the saved file's path. A write failure keeps the marker with the
failure reason -- a cap is never silent (the honest-cap discipline the
process runner already pins; this is that same discipline at the
normalization seam, the token-cost gate's direct lever per ``doc/GOAL.md``).

Recorded deviations from the TS reference:

- the py ``py.tool.finished`` wire has no metadata echo (§3.1 L131 pins
  the drop at the invoker seam), so upstream's ``metadata.truncated`` /
  ``outputPath`` ride as marker TEXT for seam-truncated results. The
  OPT-OUT half of the upstream contract ports cleanly, though: adapters
  that manage their own truncation (bash/pty/read/git/glob/grep/ls)
  set ``result.metadata["truncated"]`` and the seam skips them (upstream
  ``tool-output.ts`` L76, ``metadata?.truncated !== undefined``);
- upstream runs an hourly cleanup fiber; no scheduler exists yet
  (T-C3-003 automation), so cleanup is opportunistic and time-gated to
  at most once per hour on truncation calls;
- upstream backs the byte boundary off to a grapheme cluster; py strings
  are code points, so the boundary backs off to a code point (an emoji
  ZWJ sequence may split -- cosmetically different, never corrupt).

Idempotency: upstream guards on ``metadata.truncated``; this wire has no
metadata channel, so the guard recognizes our own marker sentence (a last
text part starting ``[showing `` and naming the saved/failed full output)
and passes already-truncated content through unchanged.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from opencode.contracts.messages import ToolContent, ToolTextContent

#: Upstream parity: the preview keeps at most 2000 lines ...
MAX_LINES = 2000
#: ... and at most 50 KiB of UTF-8 text, whichever cuts first.
MAX_BYTES = 50 * 1024
#: Saved full outputs are retained for 7 days (upstream ``RETENTION``).
RETENTION_DAYS = 7
#: The directory name under the app data root (upstream ``DIRECTORY``).
DIRECTORY = "tool-output"

_FILE_RE = re.compile(r"^tool_[0-9a-f]{12}$")
_CLEANUP_INTERVAL_S = 3600.0
_last_cleanup = 0.0


def data_directory(explicit: Path | None = None) -> Path:
    """``<data root>/opencode/tool-output`` (the ``default_db_path`` root anchor).

    Pure path math mirroring :func:`opencode.config.product.default_db_path`:
    ``$XDG_DATA_HOME`` else ``~/.local/share``, joined with the app name.
    """
    if explicit is not None:
        return Path(explicit) / DIRECTORY
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "opencode" / DIRECTORY


def truncate_contents(
    content: Sequence[ToolContent],
    *,
    directory: Path | None = None,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> list[ToolContent]:
    """Return the model-visible content, head-truncated with full-output persistence.

    Within limits: the content passes through unchanged. Over limits: the
    joined text is cut once at the line or byte limit (whichever comes
    first), the FULL text is written under ``directory`` (best effort -- a
    failure stays in the marker), and the upstream marker text is inserted
    after the cut, redistributed across the original text parts so file
    items pass through untouched.
    """
    if _already_truncated(content):
        return list(content)
    target = data_directory() if directory is None else directory
    joined = _join_text(content)
    if joined == "":
        return list(content)
    if _count_lines(joined) <= max_lines and len(joined.encode("utf-8")) <= max_bytes:
        return list(content)
    _maybe_cleanup(target)

    end = min(_after_lines(joined, max_lines), _byte_boundary(joined, max_bytes))
    shown = joined[:end]
    if shown.endswith("\r\n"):
        shown = shown[:-2]
    elif shown.endswith("\n"):
        shown = shown[:-1]

    file_path = target / f"tool_{uuid.uuid4().hex[:12]}"
    failure = _save_full_output(file_path, joined)

    shown_lines = _count_lines(joined[:end])
    last_line_cut = (
        shown_lines > 0
        and len(shown) < len(joined)
        and not joined.startswith("\n", len(shown))
        and not joined.startswith("\r\n", len(shown))
    )
    line_range = "0 lines" if shown_lines == 0 else f"lines 1-{shown_lines}"
    if failure is None:
        tail = f"full output saved to {file_path}"
    else:
        tail = f"full output could not be saved: {failure}"
    marker = f"[showing {line_range} of {_count_lines(joined)}{' (last line cut)' if last_line_cut else ''}; {tail}]"

    bounded: list[ToolContent] = []
    remaining = len(shown)
    separator = False
    marked = False
    for item in content:
        if not isinstance(item, ToolTextContent):
            bounded.append(item)
            continue
        # A line break joins text parts unless the text before already ends in one.
        if separator and remaining > 0:
            remaining -= 1
        separator = item.text != "" and not item.text.endswith("\n")
        if not marked and remaining >= len(item.text):
            bounded.append(item)
            remaining -= len(item.text)
            continue
        if remaining > 0:
            bounded.append(item.model_copy(update={"text": item.text[:remaining]}))
        if not marked:
            bounded.append(ToolTextContent(type="text", text=marker))
        remaining = 0
        marked = True
    if not marked:
        bounded.append(ToolTextContent(type="text", text=marker))
    return bounded


def cleanup(directory: Path, *, now: float | None = None) -> int:
    """Delete expired full-output files (7-day retention); returns the removed count."""
    moment = time.time() if now is None else now
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not _FILE_RE.match(entry.name):
            continue
        try:
            if moment - entry.stat().st_mtime > RETENTION_DAYS * 86400:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _already_truncated(content: Sequence[ToolContent]) -> bool:
    """The upstream ``metadata.truncated`` guard, on the marker text instead."""
    for item in reversed(content):
        if not isinstance(item, ToolTextContent):
            continue
        return item.text.startswith("[showing ") and (
            "full output saved to " in item.text or "full output could not be saved: " in item.text
        )
    return False


def _join_text(content: Sequence[ToolContent]) -> str:
    joined = ""
    for item in content:
        if not isinstance(item, ToolTextContent):
            continue
        if joined == "" or joined.endswith("\n"):
            joined = joined + item.text
        else:
            joined = f"{joined}\n{item.text}"
    return joined


def _save_full_output(file_path: Path, text: str) -> str | None:
    """Write the full text; ``None`` on success, else a one-line failure reason."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(text, encoding="utf-8")
    except OSError as error:
        reason = str(error).splitlines()[0] if str(error) else error.__class__.__name__
        return reason
    return None


def _maybe_cleanup(directory: Path) -> None:
    global _last_cleanup
    moment = time.time()
    if moment - _last_cleanup < _CLEANUP_INTERVAL_S:
        return
    _last_cleanup = moment
    cleanup(directory)


def _count_lines(text: str) -> int:
    """Line count, not counting an empty one after a final line break."""
    if text == "":
        return 0
    count = 1
    index = text.find("\n")
    while index != -1 and index < len(text) - 1:
        count += 1
        index = text.find("\n", index + 1)
    return count


def _after_lines(text: str, count: int) -> int:
    """Where the line after the first ``count`` lines starts, or the end."""
    index = 0
    for _ in range(count):
        nxt = text.find("\n", index)
        if nxt == -1:
            return len(text)
        index = nxt + 1
    return index


def _byte_boundary(text: str, budget: int) -> int:
    """The longest prefix fitting ``budget`` UTF-8 bytes without splitting a code point."""
    if budget <= 0:
        return 0
    if len(text.encode("utf-8")) <= budget:
        return len(text)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= budget:
            lo = mid
        else:
            hi = mid - 1
    # Never cut between the CR and LF of a CRLF pair.
    if lo > 0 and text[lo - 1] == "\r" and text[lo : lo + 1] == "\n":
        return lo - 1
    return lo
