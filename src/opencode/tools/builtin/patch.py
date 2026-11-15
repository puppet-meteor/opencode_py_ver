"""Builtin ``patch__apply`` tool (P6-S4; T-C1-097 toolkit A, ticket §3-2).

Applies one multi-file patch inside the working directory. The patch
language is the TS ``patch.txt`` spec ported semantics-for-semantics from
``packages/util/src/patch.ts`` (parse/derive) and
``packages/core/src/tool/plugin/patch.ts`` (the execute face): the
``*** Begin Patch`` / ``*** End Patch`` envelope, the three headers
(``*** Add File:`` body-all-``+``-lines, ``*** Delete File:`` no body,
``*** Update File:`` with the optional ``*** Move to:`` rename and the
``@@ <anchor>`` context-hunk bodies of ``+``/``-``/space lines), the
``*** End of File`` end anchor, the tiered context seek (exact, then
rstrip, then trim, then typographic-normalized), the in-patch state map
(a later hunk sees an earlier hunk's result: Add-then-Update, the
delete/rename chains), and the heredoc envelope strip.

Transaction semantics (the seat ruling the ticket §3-2 left open, TS
patch.ts execute shape): **prepare-everything-then-apply**. Every hunk is
resolved, existence-checked, read (capped), derived and anchor-matched
against the disk state BEFORE the first byte is written; only a fully
verified patch enters the apply phase. An apply-phase write/remove
failure stops at the failing operation and reports the already-applied
list verbatim (``. Completed before failure: ...``) — never a silent
half-applied patch.

TS mapping / deviations, quantified (float-reported, progress file):

- ``Add File`` on an existing target is rejected (ticket §4 matrix;
  the TS resolve face has no existence check, so a TS Add silently
  clobbers — the Python side keeps the write tool's create/overwrite split
  honest: creation goes through a non-existence proof).
- ``Delete File`` reads nothing (no file-diff preview face exists here —
  the TS ``before`` read feeds ``file-diff.ts``, which is not ported;
  progress file ②), so no size cap applies to deletes.
- BOM management is ported (the TS ``Bom.split``/``joinBom`` pair rides
  ``derive``: a leading ``\ufeff`` is split off before the hunk splice
  and re-joined onto the written text, including a BOM introduced by
  the replacement lines themselves — never doubled). Line endings stay
  raw: TS splits on ``\n`` and joins with ``\n``, so a CRLF file whose
  hunks replace lines ends up mixed — ported verbatim.
- Every trim face is the JS ``String.prototype.trim``/``trimEnd``
  semantics — Unicode White_Space plus ``\ufeff`` — not Python's
  ASCII-only ``str.strip``: the parser's boundary/header trimming and
  the laxer seek tiers all go through ``_js_trim``/``_js_trim_end``,
  so an NBSP-padded marker or a Unicode-whitespace-padded seek line
  behaves exactly as in TS.
- No Formatter hook (TS runs the project formatter over every written
  file after the apply loop; no Python formatter runtime exists —
  progress file ①).
- ``PATCH_MAX_SOURCE_BYTES`` = 1 MiB is the Python-side per-source cap
  (edit.py ``EDIT_MAX_SOURCE_BYTES`` precedent; patch.ts has none), so
  every file a patch can modify is fully readable back through
  ``fs__read``.
- The result face is the edit.py precedent: model-readable text only (the
  TS ``files``/FileDiff structured output rides the unported file-diff
  face). Failure surfaces as ``ToolResult.error`` fail-closed with the
  honest already-applied list (ticket §8).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _display_path, _effective_roots, _failure, _validate_roots
from opencode.tools.builtin.write import _atomic_replace, _reject, _resolve_write_target_roots

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (edit.py does the same).
    from opencode.tools.invoker import ToolContext

PATCH_NAMESPACE = "patch"
PATCH_NAME = "apply"
PATCH_VERSION = 1

#: Python-side per-source cap: every file a patch reads during
#: verification must fit in memory (edit.py ``EDIT_MAX_SOURCE_BYTES``
#: precedent, float-reported; patch.ts carries no size check).
PATCH_MAX_SOURCE_BYTES = 1024 * 1024

PATCH_DESCRIPTION = (
    "Use the `patch` tool to edit files. Your patch language is a stripped\u2011down, file\u2011oriented "
    "diff format designed to be easy to parse and safe to apply. You can think of it as a high\u2011level "
    "envelope:\n"
    "\n"
    "*** Begin Patch\n"
    "[ one or more file sections ]\n"
    "*** End Patch\n"
    "\n"
    "Within that envelope, you get a sequence of file operations.\n"
    "You MUST include a header to specify the action you are taking.\n"
    "Each operation starts with one of three headers:\n"
    "\n"
    "*** Add File: <path> - create a new file. Every following line is a + line (the initial contents).\n"
    "*** Delete File: <path> - remove an existing file. Nothing follows.\n"
    "*** Update File: <path> - patch an existing file in place (optionally with a rename).\n"
    "\n"
    "Example patch:\n"
    "\n"
    "```\n"
    "*** Begin Patch\n"
    "*** Add File: hello.txt\n"
    "+Hello world\n"
    "*** Update File: src/app.py\n"
    "*** Move to: src/main.py\n"
    "@@ def greet():\n"
    '-print("Hi")\n'
    '+print("Hello, world!")\n'
    "*** Delete File: obsolete.txt\n"
    "*** End Patch\n"
    "```\n"
    "\n"
    "It is important to remember:\n"
    "\n"
    "- You must include a header with your intended action (Add/Delete/Update)\n"
    "- You must prefix new lines with `+` even when creating a new file"
)

PATCH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Apply a multi-file patch (Add/Delete/Update sections with @@ anchor hunks) inside the working directory."
    ),
    "properties": {
        "patchText": {
            "type": "string",
            "description": "The full patch text describing add, update, and delete operations",
        },
    },
    "required": ["patchText"],
    "additionalProperties": False,
}

PATCH_SPEC = ToolSpec(
    namespace=PATCH_NAMESPACE,
    name=PATCH_NAME,
    version=PATCH_VERSION,
    description=PATCH_DESCRIPTION,
    input_schema=PATCH_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    # A retried identical patch fails (the anchors are gone after the
    # first application), so the adapter honestly declares no idempotency
    # (the fs__edit precedent).
    idempotency_support=False,
)


# ---------------------------------------------------------------------------
# The util/patch.ts parser port (parse/parseAdd/parseUpdate/isBoundary).
# ---------------------------------------------------------------------------


class _PatchParseError(ValueError):
    """One parse/derive failure (the util/patch.ts ``ParseError`` family + derive throws)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(slots=True)
class _Chunk:
    """One ``@@`` hunk of an Update section (util/patch.ts ``UpdateFileChunk``)."""

    old_lines: list[str]
    new_lines: list[str]
    change_context: str | None = None
    end_of_file: bool = False


@dataclass(frozen=True, slots=True)
class _Hunk:
    """One parsed file operation (util/patch.ts ``Hunk``)."""

    kind: Literal["add", "delete", "update"]
    path: str
    contents: str = ""
    move_path: str | None = None
    chunks: tuple[_Chunk, ...] = ()


#: util/patch.ts stripHeredoc: a ``cat <<'EOF' ... EOF`` envelope around the
#: patch text is unwrapped to the payload before parsing. The word class is
#: the JS ``\w`` (ASCII): a non-ASCII heredoc tag is no tag in TS.
_HEREDOC_RE = re.compile(r"^(?:cat\s+)?<<(['\"]?)([A-Za-z0-9_]+)\1\s*\n([\s\S]*?)\n\2\s*$")

_BEGIN_PATCH = "*** Begin Patch"
_END_PATCH = "*** End Patch"
_ADD_FILE = "*** Add File: "
_DELETE_FILE = "*** Delete File: "
_UPDATE_FILE = "*** Update File: "
_MOVE_TO = "*** Move to: "
#: The rtrimmed bare form of the Move-to header: TS checks
#: ``move === "*** Move to:"`` OR ``move.startsWith("*** Move to: ")`` —
#: the prefixed form's trailing space is gone after trimEnd, so the bare
#: equality is a separate arm (the empty-destination test pins it).
_MOVE_TO_BARE = "*** Move to:"
_ENVIRONMENT_ID = "*** Environment ID:"

#: The ECMAScript ``String.prototype.trim`` character class: White_Space
#: (\t \n \v \f \r space NBSP + the Zs category) + the LineTerminators
#: LS/PS + ZWNBSP (\ufeff). Python's ``str.strip`` is ASCII-whitespace-only,
#: so every TS ``.trim()``/``.trimEnd()`` face goes through these instead
#: (the seek tiers and the parser boundary faces).
_JS_TRIM_CLASS = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_TRIM_RE = re.compile(f"^[{_JS_TRIM_CLASS}]+|[{_JS_TRIM_CLASS}]+$")
_JS_TRIM_END_RE = re.compile(f"[{_JS_TRIM_CLASS}]+$")


def _js_trim(value: str) -> str:
    """The JS ``String.prototype.trim`` face (both ends, Unicode)."""
    return _JS_TRIM_RE.sub("", value)


def _js_trim_end(value: str) -> str:
    """The JS ``String.prototype.trimEnd`` face (trailing end, Unicode)."""
    return _JS_TRIM_END_RE.sub("", value)


def _strip_heredoc(text: str) -> str:
    """util/patch.ts stripHeredoc: the heredoc payload, or the input unchanged."""
    match = _HEREDOC_RE.match(text)
    return match.group(3) if match is not None else text


def _is_boundary(line: str) -> bool:
    """util/patch.ts isBoundary: a line that ends the current section."""
    return line == _END_PATCH or line.startswith((_ADD_FILE, _DELETE_FILE, _UPDATE_FILE))


def _invalid_hunk(line: str, line_number: int, reason: str | None = None) -> _PatchParseError:
    """util/patch.ts InvalidHunkError: the default header message, or the reason."""
    if reason is not None:
        return _PatchParseError(f"Invalid hunk at line {line_number}: {reason}")
    return _PatchParseError(
        f"Invalid hunk at line {line_number}: '{line}' is not a valid hunk header. "
        f"Valid hunk headers: '{_ADD_FILE}{{path}}', '{_DELETE_FILE}{{path}}', '{_UPDATE_FILE}{{path}}'"
    )


def _parse_add(lines: list[str], start: int, end: int, path: str) -> tuple[str, int]:
    """util/patch.ts parseAdd: every body line is a ``+`` line until the next boundary."""
    content: list[str] = []
    index = start
    while index < end and not _is_boundary(_js_trim(lines[index])):
        if not lines[index].startswith("+"):
            line = _js_trim(lines[index])
            raise _invalid_hunk(
                line,
                index + 1,
                f"Invalid Add File line for '{path}': expected a line starting with '+', got '{line}'",
            )
        content.append(lines[index][1:])
        index += 1
    return "\n".join(content), index


def _parse_update(lines: list[str], start: int, end: int, path: str, hunk: int) -> tuple[list[_Chunk], int]:
    """util/patch.ts parseUpdate: ``@@`` chunks over ``+``/``-``/context lines."""
    chunks: list[_Chunk] = []
    index = start
    after_end_of_file = False
    while index < end:
        line = lines[index]
        update_line = _js_trim_end(line)
        if after_end_of_file:
            if update_line == "":
                index += 1
                continue
            if update_line == "@@" or update_line.startswith("@@ "):
                after_end_of_file = False  # falls through: this line opens the next chunk
            elif _is_boundary(update_line):
                break
            else:
                raise _invalid_hunk(
                    line,
                    index + 1,
                    f"Expected update hunk to start with a @@ context marker, got: '{line}'",
                )
        if update_line == "*** End of File":
            if chunks:
                last = chunks[-1]
                if not last.old_lines and not last.new_lines:
                    raise _invalid_hunk(update_line, index + 1, "Update hunk does not contain any lines")
                last.end_of_file = True
                after_end_of_file = True
            index += 1
            continue
        if _is_boundary(update_line):
            break
        if update_line == "@@" or update_line.startswith("@@ "):
            if chunks:
                previous = chunks[-1]
                if not previous.old_lines and not previous.new_lines:
                    raise _invalid_hunk(
                        line,
                        index + 1,
                        f"Unexpected line found in update hunk: '{line}'. Every line should start with "
                        "' ' (context line), '+' (added line), or '-' (removed line)",
                    )
            chunks.append(
                _Chunk(
                    old_lines=[],
                    new_lines=[],
                    change_context=None if update_line == "@@" else update_line[len("@@ ") :],
                )
            )
            index += 1
            continue
        if not chunks:
            chunks.append(_Chunk(old_lines=[], new_lines=[]))
        chunk = chunks[-1]
        if line == "":
            chunk.old_lines.append("")
            chunk.new_lines.append("")
            index += 1
            continue
        if line.startswith(" "):
            chunk.old_lines.append(line[1:])
            chunk.new_lines.append(line[1:])
            index += 1
            continue
        if line.startswith("-"):
            chunk.old_lines.append(line[1:])
            index += 1
            continue
        if line.startswith("+"):
            chunk.new_lines.append(line[1:])
            index += 1
            continue
        populated = bool(chunk.old_lines) or bool(chunk.new_lines)
        raise _invalid_hunk(
            line,
            index + 1,
            f"Expected update hunk to start with a @@ context marker, got: '{line}'"
            if populated
            else f"Unexpected line found in update hunk: '{line}'. Every line should start with "
            "' ' (context line), '+' (added line), or '-' (removed line)",
        )
    if not chunks:
        raise _invalid_hunk(_js_trim(lines[hunk]), hunk + 1, f"Update file hunk for path '{path}' is empty")
    last = chunks[-1]
    if not last.old_lines and not last.new_lines:
        line = _js_trim(lines[index])
        raise _invalid_hunk(
            line,
            index + 1,
            "Update hunk does not contain any lines"
            if line == _END_PATCH
            else f"Unexpected line found in update hunk: '{line}'. Every line should start with "
            "' ' (context line), '+' (added line), or '-' (removed line)",
        )
    return chunks, index


def parse_patch(patch_text: str) -> list[_Hunk]:
    """Parse one patch envelope into its file operations (util/patch.ts ``parse``).

    Raises :class:`_PatchParseError` with the TS message wording on every
    rejection: a missing/mismatched boundary, a malformed header, an Add
    body line without ``+``, a body line after Delete File, an empty
    Update hunk, or a bad hunk line.
    """
    lines = [line[:-1] if line.endswith("\r") else line for line in _strip_heredoc(_js_trim(patch_text)).split("\n")]
    begin = 0 if lines and _js_trim(lines[0]) == _BEGIN_PATCH else -1
    end = len(lines) - 1 if lines and _js_trim(lines[-1]) == _END_PATCH else -1
    if begin == -1:
        raise _PatchParseError(f"The first line of the patch must be '{_BEGIN_PATCH}'")
    if end == -1 or begin >= end:
        raise _PatchParseError(f"The last line of the patch must be '{_END_PATCH}'")

    hunks: list[_Hunk] = []
    index = begin + 1
    while index < end:
        line = lines[index]
        header = _js_trim(line)
        if index == begin + 1 and header.startswith(_ENVIRONMENT_ID) and _js_trim(header[len(_ENVIRONMENT_ID) :]):
            index += 1
            continue
        if header.startswith(_ADD_FILE):
            path = _js_trim(header[len(_ADD_FILE) :])
            contents, next_index = _parse_add(lines, index + 1, end, path)
            hunks.append(_Hunk(kind="add", path=path, contents=contents))
            index = next_index
            continue
        if header.startswith(_DELETE_FILE):
            path = _js_trim(header[len(_DELETE_FILE) :])
            next_line = _js_trim(lines[index + 1]) if index + 1 < len(lines) else None
            if index + 1 < end and next_line is not None and not _is_boundary(next_line):
                if next_line.startswith("*** "):
                    raise _invalid_hunk(next_line, index + 2)
                raise _invalid_hunk(
                    next_line,
                    index + 2,
                    f"Unexpected line after Delete File '{path}': '{next_line}'. "
                    "Delete hunks do not contain body lines",
                )
            hunks.append(_Hunk(kind="delete", path=path))
            index += 1
            continue
        if header.startswith(_UPDATE_FILE):
            path = _js_trim(header[len(_UPDATE_FILE) :])
            next_index = index + 1
            while next_index < len(lines) and _js_trim_end(lines[next_index]) == "*** End of File":
                next_index += 1
            move_line = _js_trim_end(lines[next_index]) if next_index < len(lines) else None
            move_path: str | None = None
            if move_line == _MOVE_TO_BARE or (move_line is not None and move_line.startswith(_MOVE_TO)):
                move_path = _js_trim(move_line[len(_MOVE_TO) :])
                if not move_path:
                    raise _invalid_hunk(
                        _js_trim(lines[next_index]),
                        next_index + 1,
                        f"Move destination for '{path}' must not be empty",
                    )
                next_index += 1
            chunks, parsed_next = _parse_update(lines, next_index, end, path, index)
            hunks.append(_Hunk(kind="update", path=path, move_path=move_path, chunks=tuple(chunks)))
            index = parsed_next
            continue
        raise _invalid_hunk(header, index + 1)
    return hunks


# ---------------------------------------------------------------------------
# The util/patch.ts derive port (seek tiers + computeReplacements).
# ---------------------------------------------------------------------------

#: util/patch.ts normalize: the typographic quote/dash/space families to
#: their ASCII forms (the edit.py translation-table shape).
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


def _normalized(left: str, right: str) -> bool:
    """util/patch.ts normalized compare: JS-trimmed + typographic-normalized."""
    return _js_trim(left).translate(_NORMALIZE_TABLE) == _js_trim(right).translate(_NORMALIZE_TABLE)


#: The four seek tiers in their fixed order (util/patch.ts seek): an exact
#: match anywhere beats a laxer match earlier — the whole file is scanned
#: per tier before the next tier starts.
_COMPARES = (
    lambda left, right: left == right,
    lambda left, right: _js_trim_end(left) == _js_trim_end(right),
    lambda left, right: _js_trim(left) == _js_trim(right),
    _normalized,
)


def _seek(lines: list[str], pattern: list[str], start: int, eof: bool = False) -> int:
    """util/patch.ts seek: the first offset where the pattern matches."""
    if not pattern:
        return -1
    if eof:
        offset = len(lines) - len(pattern)
        if offset < start:
            return -1
        for compare in _COMPARES:
            if all(compare(lines[offset + i], line) for i, line in enumerate(pattern)):
                return offset
        return -1
    for compare in _COMPARES:
        for offset in range(start, len(lines) - len(pattern) + 1):
            if all(compare(lines[offset + i], line) for i, line in enumerate(pattern)):
                return offset
    return -1


def _compute_replacements(lines: list[str], path: str, chunks: Sequence[_Chunk]) -> list[tuple[int, int, list[str]]]:
    """util/patch.ts computeReplacements: the (start, remove, insert) splices."""
    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            context = _seek(lines, [chunk.change_context], line_index)
            if context == -1:
                raise _PatchParseError(f"Failed to find context '{chunk.change_context}' in {path}")
            line_index = context + 1
        if not chunk.old_lines:
            replacements.append((len(lines), 0, list(chunk.new_lines)))
            continue
        old_lines = list(chunk.old_lines)
        new_lines = list(chunk.new_lines)
        found = _seek(lines, old_lines, line_index, chunk.end_of_file)
        if found == -1 and old_lines[-1] == "":
            old_lines = old_lines[:-1]
            if new_lines and new_lines[-1] == "":
                new_lines = new_lines[:-1]
            found = _seek(lines, old_lines, line_index, chunk.end_of_file)
        if found == -1 and all(line == "" for line in chunk.old_lines):
            expected = (
                "an expected blank line"
                if len(chunk.old_lines) == 1
                else f"{len(chunk.old_lines)} consecutive blank lines"
            )
            raise _PatchParseError(f"Failed to find {expected} in {path}")
        if found == -1:
            raise _PatchParseError(f"Failed to find expected lines in {path}:\n" + "\n".join(chunk.old_lines))
        replacements.append((found, len(old_lines), new_lines))
        line_index = found + len(old_lines)
    # util/patch.ts `.sort(...).reverse()`: ascending by start, then the
    # in-place reverse — so equal-start entries (the end-of-file appends)
    # apply last-pushed-first and land in chunk order.
    ordered = sorted(replacements, key=lambda item: item[0])
    return ordered[::-1]


_BOM = "\ufeff"


def _derive(path: str, chunks: Sequence[_Chunk], original: str) -> str:
    """Apply the update chunks to ``original`` (util/patch.ts ``derive``).

    The TS ``Bom.split``/``joinBom`` pair rides here: a leading ``\ufeff``
    is split off before the splice and re-joined onto the result — the
    source BOM survives, a BOM introduced by the replacement lines is
    kept too, and neither is ever doubled.
    """
    bom = original.startswith(_BOM)
    text = original[1:] if bom else original
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for start, remove, insert in _compute_replacements(lines, path, chunks):
        lines[start : start + remove] = insert
    if not lines or lines[-1] != "":
        lines.append("")
    joined = "\n".join(lines)
    if bom or joined.startswith(_BOM):
        return f"{_BOM}{joined.removeprefix(_BOM)}"
    return joined


# ---------------------------------------------------------------------------
# The execute face (plugin/patch.ts prepare/apply loop, PY shapes).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prepared:
    """One verified operation awaiting the apply phase (patch.ts ``Prepared``)."""

    kind: Literal["add", "delete", "update"]
    display: str
    real: str
    content: str = ""
    move_display: str = ""
    move_real: str | None = None


_MISSING = object()  # sentinel: the path has no in-patch state yet


def _verify(context: ToolContext, hunks: list[_Hunk], roots: Sequence[str]) -> list[_Prepared] | ToolResult:
    """The prepare phase: resolve, existence-check, read, cap and derive every hunk.

    Returns the prepared list, or the failure ``ToolResult`` (never a
    partially prepared list — a rejected patch touches nothing).
    """
    effective = _effective_roots(roots, context.working_dir)
    prepared: list[_Prepared] = []
    # The in-patch state map (patch.ts ``updates``): absolute path ->
    # current content (None = deleted by an earlier hunk of this patch).
    updates: dict[str, str | None] = {}
    for hunk in hunks:
        real, reason = _resolve_write_target_roots(effective, hunk.path)
        if real is None:
            assert reason is not None  # the resolver always returns a reason on rejection
            return _reject(hunk.path, reason)
        display = _display_path(context.working_dir, real)
        previous = cast("str | None", updates.get(real, _MISSING))
        if hunk.kind == "add":
            if os.path.lexists(real) or (previous is not _MISSING and previous is not None):
                return _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    f"Failed to add {display}: file already exists",
                )
            content = hunk.contents if hunk.contents.endswith("\n") or hunk.contents == "" else f"{hunk.contents}\n"
            prepared.append(_Prepared(kind="add", display=display, real=real, content=content))
            updates[real] = content
            continue
        if hunk.kind == "delete":
            if previous is None or (previous is _MISSING and not os.path.lexists(real)):
                return _failure(
                    CoreErrorCode.VALIDATION_ERROR,
                    f"Failed to delete {display}: file does not exist",
                )
            prepared.append(_Prepared(kind="delete", display=display, real=real))
            updates[real] = None
            continue
        # update
        if previous is None or (previous is _MISSING and not os.path.lexists(real)):
            return _failure(
                CoreErrorCode.VALIDATION_ERROR,
                f"Failed to read file to update {display}: file does not exist",
            )
        if previous is not _MISSING:
            original = previous
        else:
            try:
                with open(real, "rb") as handle:
                    source_bytes = handle.read()
            except OSError as error:
                return _failure(CoreErrorCode.TOOL_FAILED, f"Failed to read file to update {display}: {error}")
            if len(source_bytes) > PATCH_MAX_SOURCE_BYTES:
                return _failure(
                    CoreErrorCode.TOOL_FAILED,
                    f"Cannot patch {display}: the file is {len(source_bytes)} bytes, "
                    f"over the {PATCH_MAX_SOURCE_BYTES} byte patch limit",
                )
            try:
                original = source_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot patch {display}: file is not valid UTF-8 text")
        try:
            content = _derive(display, hunk.chunks, original)
        except _PatchParseError as error:
            return _failure(CoreErrorCode.VALIDATION_ERROR, error.message)
        move_display = ""
        move_real: str | None = None
        if hunk.move_path is not None:
            move_target, move_reason = _resolve_write_target_roots(effective, hunk.move_path)
            if move_target is None:
                assert move_reason is not None
                return _reject(hunk.move_path, move_reason)
            if move_target != real:
                move_display = _display_path(context.working_dir, move_target)
                move_real = move_target
        prepared.append(
            _Prepared(
                kind="update",
                display=display,
                real=real,
                content=content,
                move_display=move_display,
                move_real=move_real,
            )
        )
        if move_real is not None:
            updates[real] = None
            updates[move_real] = content
        else:
            updates[real] = content
    return prepared


def _apply_failed(operation: str, error: OSError, applied: Sequence[str]) -> ToolResult:
    """patch.ts ``fail``: the operation error plus the honest already-applied list."""
    completed = ", ".join(applied)
    message = f"{operation}: {error}"
    if completed:
        message = f"{message}. Completed before failure: {completed}"
    return _failure(CoreErrorCode.TOOL_FAILED, message)


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str] = ()) -> ToolResult:
    # The Invoker strictly validated this input against PATCH_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts below are
    # static narrows, not conversions.
    patch_text = cast(str, input["patchText"])
    if not patch_text:
        return _failure(CoreErrorCode.VALIDATION_ERROR, "patchText is required")
    try:
        hunks = parse_patch(patch_text)
    except _PatchParseError as error:
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"patch verification failed: {error.message}")
    if not hunks:
        return _failure(CoreErrorCode.VALIDATION_ERROR, "patch rejected: empty patch")

    prepared = _verify(context, hunks, roots)
    if isinstance(prepared, ToolResult):
        return prepared  # a rejected patch touches nothing (fail closed)

    # The apply phase (patch.ts Effect.forEach over prepared): in patch
    # order, one operation at a time; a write/remove failure stops at the
    # failing operation with the already-applied list reported verbatim.
    applied: list[str] = []
    for item in prepared:
        if item.kind == "delete":
            try:
                os.remove(item.real)
            except OSError as error:
                return _apply_failed(f"Failed to delete {item.display}", error, applied)
            applied.append(item.display)
            continue
        target_display = item.move_display or item.display
        target_real = item.move_real if item.move_real is not None else item.real
        try:
            data = item.content.encode("utf-8")
        except UnicodeEncodeError:
            # Defensive: unreachable through the Invoker's canonical-JSON
            # input hash (write.py face); kept fail-closed for direct callers.
            return _failure(
                CoreErrorCode.VALIDATION_ERROR, f"Cannot write {target_display}: content is not encodable as UTF-8"
            )
        try:
            os.makedirs(os.path.dirname(target_real), exist_ok=True)
            _atomic_replace(target_real, data)
        except OSError as error:
            return _apply_failed(f"Failed to write {target_display}", error, applied)
        if item.move_real is not None:
            try:
                os.remove(item.real)
            except OSError as error:
                return _apply_failed(f"Wrote {target_display} but failed to remove {item.display}", error, applied)
        applied.append(target_display)

    # patch.ts toModelContent: the per-file A/M/D report, one line each.
    lines = ["Success. Updated the following files:"]
    for item in prepared:
        letter = "A" if item.kind == "add" else "D" if item.kind == "delete" else "M"
        resource = item.move_display or item.display
        lines.append(f"{letter} {resource}")
    return ToolResult(content=[ToolTextContent(type="text", text="\n".join(lines))])


class PatchApplyAdapter:
    """The ``patch__apply`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the parse/verify/apply pipeline
    is synchronous by design (edit.py/write.py: serial Phase-1 execution +
    the source cap bound each patch's cost). ``roots`` is the optional
    config-declared root extension (empty default = the R2 single-root
    behavior, ⑤).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return PATCH_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input, self._roots)
