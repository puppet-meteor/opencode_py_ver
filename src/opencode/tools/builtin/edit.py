"""Builtin ``fs__edit`` tool (R3-S4; CORE-CONTRACTS §5.3 L272/L274-275, plan §2.4, PHASE1-SLIM §4 L91).

Exact find-and-replace on a UTF-8 text file inside the working directory,
with the §5.3 L274 conflict-detection + atomic-replace discipline: the
occurrence analysis runs against the version read from disk, a zero or
(non-``replaceAll``) ambiguous match is a clean failure that never touches
the file, and an accepted edit is swapped in through the write.py
``_atomic_replace`` (same-directory temp file + ``os.replace``).

TS mapping (``packages/core/src/tool/plugin/edit.ts``, 332L) — the
conflict-detection core is a direct port: ``normalizeForMatch`` (typographic
quote/dash/space variants), ``findOccurrences`` (exact spans),
``findLineOccurrences`` (trailing-whitespace-tolerant whole-line spans with
the overlap dedup), ``lineEnding`` (dominant ending), ``withRegionEndings``
+ ``alignLines`` (LCS-aligned reuse of the matched region's line endings),
``matchView`` + ``countBefore`` (match on the LF view Read shows the model,
map offsets back over the removed CRLF ``\\r`` positions), and the
``endingsOnly`` mode (old/new differing only in ``\\r`` apply to the exact
bytes). Failure wording follows edit.ts: identical old/new, empty
oldString, "Could not find oldString", "Found N matches ... expected
exactly one", the unchanged-report family, "File not found" and
"Path is a directory, not a file".

Deviations, quantified (float-reported, progress file): no BOM management
(``Bom.join`` dropped — the Python ``fs__read`` face does not strip BOMs,
so ``\\ufeff`` is ordinary text here); no formatter hook; no file-diff
preview/structured output (file-diff.ts not ported — progress file ①);
input validation failures surface as VALIDATION_ERROR ToolResults at
adapter time (TS raises ToolFailure before its permission.assert; the
Python §5.1 L259 gate runs before the adapter, so the model sees the
failure only after the call was approved and started);
``EDIT_MAX_SOURCE_BYTES`` = 1 MiB is a Python-side cap (edit.ts has none;
sized at the §5.3 L272 read window — the model can only have read what
Read shows, larger files get the explicit oversized diagnostic
PHASE1-SLIM L91 asks for).
"""

from __future__ import annotations

import os
import re
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import (
    _effective_roots,
    _failure,
    _resolve_within_roots,
    _validate_roots,
)
from opencode.tools.builtin.write import _atomic_replace, _reject

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

EDIT_NAMESPACE = "fs"
EDIT_NAME = "edit"
EDIT_VERSION = 1

#: Python-side source cap: the file being edited must fit in memory. edit.ts
#: carries no size check (no TS number exists to align with); sized at the
#: §5.3 L272 1 MiB read window — the model can only have seen that much of
#: the file through ``fs__read``, and larger files get the explicit
#: oversized diagnostic (PHASE1-SLIM L91; float-reported).
EDIT_MAX_SOURCE_BYTES = 1024 * 1024

EDIT_DESCRIPTION = (
    "Edit the contents of a file by finding and replacing exact text. When editing text from Read "
    "output, preserve the exact indentation (tabs or spaces) and omit the line-number prefix, such as "
    "`1: `. Never include the prefix in oldString or newString. Write line breaks as \\n; the file's "
    "existing CRLF or LF line endings are matched and preserved. To change line endings themselves, "
    "make oldString and newString differ only in \\r; that edit applies to the exact bytes. If there is "
    "no exact match, the edit retries while ignoring typographic quote, dash, and space variants, then "
    "trailing whitespace on each line. The edit fails if oldString is not found. By default, oldString "
    "must identify a UNIQUE location. Multiple matches FAIL unless replaceAll is true. Add more "
    "surrounding context to disambiguate, or set replaceAll to true to replace every occurrence. Use "
    "replaceAll when the change should apply to every occurrence, such as renaming a variable."
)

EDIT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Edit a UTF-8 text file resolved inside the working directory by exact find-and-replace.",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to edit, relative to the working directory (an absolute path must resolve inside it)",
        },
        "oldString": {
            "type": "string",
            "description": "Exact text to find and replace",
        },
        "newString": {
            "type": "string",
            "description": "Text to replace oldString with (must differ from oldString)",
        },
        "replaceAll": {
            "type": "boolean",
            "description": "Whether to replace every occurrence of oldString. When false, oldString must match "
            "exactly once. Defaults to false.",
        },
    },
    "required": ["path", "oldString", "newString"],
    "additionalProperties": False,
}

EDIT_SPEC = ToolSpec(
    namespace=EDIT_NAMESPACE,
    name=EDIT_NAME,
    version=EDIT_VERSION,
    description=EDIT_DESCRIPTION,
    input_schema=EDIT_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    # A retried identical edit fails (oldString is gone after the first
    # application), so the adapter honestly declares no idempotency.
    idempotency_support=False,
)


# ---------------------------------------------------------------------------
# The edit.ts conflict-detection core (332L port).
# ---------------------------------------------------------------------------

#: edit.ts findLineOccurrences line splitter (L66) and the CRLF-aware
#: region splitter/ending collector (withRegionEndings L102-103).
_LINE_RE = re.compile(r"[^\n]*(?:\n|$)")
_NEWLINE_RE = re.compile(r"\r?\n")

#: edit.ts normalizeForMatch (L49-54) as a 1:1 character translation: the
#: mappings are strictly single-character, so ``str.translate`` preserves
#: offsets exactly (the unicode tier indexes the normalized view).
_NORMALIZE_TABLE = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u202f": " ",
        "\u205f": " ",
        "\u3000": " ",
    }
)


@dataclass(frozen=True, slots=True)
class _Match:
    """One located replacement span (edit.ts ``Match``)."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _LineSpan:
    """One line of the view plus its span metadata (edit.ts findLineOccurrences)."""

    start: int
    end: int
    text: str
    content_end: int
    newline: bool


def _normalize_for_match(value: str) -> str:
    """edit.ts normalizeForMatch: typographic quote/dash/space variants to ASCII."""
    return value.translate(_NORMALIZE_TABLE)


def _find_occurrences(content: str, search: str) -> list[_Match]:
    """edit.ts findOccurrences: every exact substring span, stepping past each hit."""
    result: list[_Match] = []
    offset = content.find(search)
    while offset != -1:
        result.append(_Match(offset, offset + len(search)))
        offset = content.find(search, offset + len(search))
    return result


def _find_line_occurrences(content: str, search: str) -> list[_Match]:
    """edit.ts findLineOccurrences: trailing-whitespace-tolerant whole-line spans.

    Lines compare after ``normalizeForMatch`` + trailing-whitespace trim;
    a search ending in ``\\n`` must land on a newline-terminated line; and
    overlapping candidates (adjacent windows over the same lines) are
    deduplicated to the first — edit.ts L84-89.
    """
    trailing_newline = search.endswith("\n")
    expected = search.split("\n")
    if trailing_newline:
        expected.pop()  # the final empty piece is the newline itself
    lines: list[_LineSpan] = []
    for match in _LINE_RE.finditer(content):
        raw = match.group(0)
        if raw == "":
            continue  # the regex's empty end-of-input match
        newline = raw.endswith("\n")
        text = raw[:-1] if newline else raw
        lines.append(
            _LineSpan(
                start=match.start(),
                end=match.end(),
                text=text,
                content_end=match.start() + len(text) - (1 if text.endswith("\r") else 0),
                newline=newline,
            )
        )
    candidates: list[_Match] = []
    for index, line in enumerate(lines):
        actual = lines[index : index + len(expected)]
        if len(actual) != len(expected):
            continue
        if any(
            _normalize_for_match(item.text.rstrip()) != _normalize_for_match(want.rstrip())
            for item, want in zip(actual, expected, strict=True)
        ):
            continue
        last = actual[-1]
        if trailing_newline and not last.newline:
            continue
        candidates.append(_Match(line.start, last.end if trailing_newline else last.content_end))
    result: list[_Match] = []
    for candidate in candidates:
        if any(found.end > candidate.start and found.start < candidate.end for found in result):
            continue  # overlaps an already-kept candidate
        result.append(candidate)
    return result


def _line_ending(text: str, fallback: str) -> str:
    """edit.ts lineEnding: the dominant line ending, or the fallback on a tie/none."""
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    if crlf_count == lf_count:
        return fallback
    return "\r\n" if crlf_count > lf_count else "\n"


def _region_key(line: str) -> str:
    """The comparison key of withRegionEndings: normalize + trailing-whitespace trim."""
    return _normalize_for_match(line.rstrip())


def _align_lines(before: list[str], after: list[str]) -> list[int | None]:
    """edit.ts alignLines: the LCS alignment of two line-key lists.

    For each ``after`` line, the index of the ``before`` line it keeps
    (equal keys), or — between kept lines — the line it takes the place of;
    ``None`` when the after side has no counterpart (edit.ts undefined).
    """
    width = len(after) + 1
    table = [0] * ((len(before) + 1) * width)
    for i in range(len(before) - 1, -1, -1):
        for j in range(len(after) - 1, -1, -1):
            if before[i] == after[j]:
                table[i * width + j] = table[(i + 1) * width + j + 1] + 1
            else:
                table[i * width + j] = max(table[(i + 1) * width + j], table[i * width + j + 1])
    result: list[int | None] = [None] * len(after)
    gap_before: list[int] = []
    gap_after: list[int] = []

    def flush() -> None:
        for k, j in enumerate(gap_after):
            if k < len(gap_before):
                result[j] = gap_before[k]
        gap_before.clear()
        gap_after.clear()

    i = j = 0
    while i < len(before) and j < len(after):
        if before[i] == after[j]:
            flush()
            result[j] = i
            i += 1
            j += 1
            continue
        if table[(i + 1) * width + j] >= table[i * width + j + 1]:
            gap_before.append(i)
            i += 1
        else:
            gap_after.append(j)
            j += 1
    while i < len(before):
        gap_before.append(i)
        i += 1
    while j < len(after):
        gap_after.append(j)
        j += 1
    flush()
    return result


def _with_region_endings(region: str, replacement: str, fallback: str) -> str:
    """edit.ts withRegionEndings: splice the replacement onto the region's endings.

    A replacement line that keeps a region line (LCS over the comparison
    keys) reuses that line's ending; a line changed in place reuses the
    ending of the line it replaces; only lines beyond those take the
    fallback. Unchanged context inside a match keeps its bytes even in a
    mixed-ending file.
    """
    before = _NEWLINE_RE.split(region)
    endings = _NEWLINE_RE.findall(region)
    after = replacement.split("\n")
    source = _align_lines([_region_key(line) for line in before], [_region_key(line) for line in after])
    parts: list[str] = []
    for index, line in enumerate(after):
        if index == len(after) - 1:
            parts.append(line)  # the last line never carries an ending
            continue
        origin = source[index]
        ending = endings[origin] if origin is not None and 0 <= origin < len(endings) else fallback
        parts.append(line + ending)
    return "".join(parts)


def _match_view(source: str, old_string: str, new_string: str) -> tuple[list[_Match], Callable[[_Match], str]]:
    """edit.ts matchView: locate on the LF view, map offsets back to the source.

    The three tiers are tried in order and are mutually exclusive: exact
    spans on the view, then the normalizeForMatch tier, then the
    line-occurrence tier (edit.ts L141-146). The view is the source with
    CRLF collapsed to LF (what Read shows the model); ``removed`` holds the
    view-relative offsets of every dropped ``\\r`` so ``countBefore``
    (bisect on the sorted list) maps a view offset back to the source
    offset. The one-to-one normalize translation preserves offsets, so the
    unicode tier's spans index the view directly.
    """

    def replace(match: _Match) -> str:
        region = source[match.start : match.end]
        return _with_region_endings(region, replacement, _line_ending(region, file_ending))

    view = source.replace("\r\n", "\n")
    removed = [match.start() - index for index, match in enumerate(re.finditer("\r\n", source))]
    file_ending = _line_ending(source, "\n")
    search = old_string.replace("\r\n", "\n")
    replacement = new_string.replace("\r\n", "\n")
    exact = _find_occurrences(view, search)
    unicode_matches = [] if exact else _find_occurrences(_normalize_for_match(view), _normalize_for_match(search))
    trailing = [] if exact or unicode_matches else _find_line_occurrences(view, search)
    matches = [
        _Match(match.start + bisect_left(removed, match.start), match.end + bisect_left(removed, match.end))
        for match in exact or unicode_matches or trailing
    ]
    return matches, replace


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str] = ()) -> ToolResult:
    # The Invoker strictly validated this input against EDIT_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts below are
    # static narrows, not conversions.
    path = cast(str, input["path"])
    old_string = cast(str, input["oldString"])
    new_string = cast(str, input["newString"])
    replace_all = cast(bool, input.get("replaceAll", False))

    if old_string == new_string:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            "No changes to apply: oldString and newString are identical.",
        )
    if old_string == "":
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            "oldString must not be empty. Use write to create or overwrite a file.",
        )

    real, reason = _resolve_within_roots(_effective_roots(roots, context.working_dir), path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _reject(path, reason)
    if os.path.isdir(real):
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, f"Path is a directory, not a file: {path}")
    if not os.path.isfile(real):
        return _failure(CoreErrorCode.NOT_FOUND, f"File not found: {path}")

    try:
        with open(real, "rb") as handle:
            source_bytes = handle.read()
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot edit {path}: {error}")
    if len(source_bytes) > EDIT_MAX_SOURCE_BYTES:
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            f"Cannot edit {path}: the file is {len(source_bytes)} bytes, "
            f"over the {EDIT_MAX_SOURCE_BYTES} byte edit limit",
        )
    try:
        source = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot edit {path}: file is not valid UTF-8 text")

    # Strings differing only in \r change the line endings themselves, so
    # they match the exact bytes; every other edit matches the LF view Read
    # shows the model and keeps the file's endings (edit.ts L211-214).
    endings_only = old_string.replace("\r", "") == new_string.replace("\r", "")
    if endings_only:

        def replace_exact(match: _Match) -> str:
            return new_string

        matches = _find_occurrences(source, old_string)
        replace: Callable[[_Match], str] = replace_exact
    else:
        matches, replace = _match_view(source, old_string, new_string)

    replacements = len(matches)
    if replacements == 0:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Could not find oldString in {path}. It must match exactly, including whitespace and indentation.",
        )
    if replacements > 1 and not replace_all:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Found {replacements} matches for oldString, but expected exactly one. Add more surrounding "
            "context to make oldString unique, or set replaceAll to true to replace every occurrence.",
        )

    selected = matches if replace_all else matches[:1]
    replaced = source
    for match in reversed(selected):
        replaced = replaced[: match.start] + replace(match) + replaced[match.end :]
    if replaced == source:
        # The endings-only variant is unreachable (an identical-outcome case
        # is either pre-checked as identical strings or yields zero
        # matches); kept for edit.ts parity (④ disclosure).
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"No changes to apply: {path} already has those line endings."
            if endings_only
            else f"No changes to apply: the edit leaves {path} unchanged. Line endings follow the file; "
            "to change them, write \\r explicitly in oldString or newString.",
        )

    try:
        data = replaced.encode("utf-8")
    except UnicodeEncodeError:
        # Defensive: unreachable through the Invoker (see write.py).
        return _failure(
            CoreErrorCode.VALIDATION_ERROR, f"Cannot edit {path}: the edited text is not encodable as UTF-8"
        )
    try:
        _atomic_replace(real, data)
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot edit {path}: {error}")

    plural = "" if replacements == 1 else "s"
    return ToolResult(
        content=[ToolTextContent(type="text", text=f"Edited {path} ({replacements} replacement{plural})")]
    )


class FsEditAdapter:
    """The ``fs__edit`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the edit itself is synchronous
    by design (write.py module docstring: serial Phase-1 execution + the
    source cap bound each edit's cost). ``roots`` is the optional R3-S6
    config-declared root extension (empty default = the R2 single-root
    behavior, ⑤).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return EDIT_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input, self._roots)
