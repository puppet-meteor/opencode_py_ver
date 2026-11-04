"""Builtin ``fs__ls`` tool (R3-S3; plan §2.3, CORE-CONTRACTS §5.2 L266, PHASE1-SLIM §4 L91).

Lists one directory inside the working directory with a capped number of
entries and an explicit truncation marker (acceptance A: "glob/grep/ls
限制数量" -- PHASE1-SLIM L91).

``ls`` has no TS counterpart (r3-slices §4 difference table 2: TS glob
covers the list-directory semantics), so the reference chain is the one
sanctioned there: ``glob.ts`` listing semantics plus the ``read`` adapter
precedent. Concretely:

- Entry rendering follows ``filesystem.ts`` ``list`` (L168-199): only
  file/directory entries are shown (L186), directories get a trailing
  separator (L191, normalized to "/" for model-facing paths), and entries
  sort directories first then by name (L196; ``localeCompare`` becomes a
  plain string comparison -- Python avoids locale-dependent ordering so
  the output is deterministic across machines, float-reported).
- The ``path``/``hidden``/``limit`` input face follows ``glob.ts``
  (L15-27): the listed directory defaults to ".", hidden dot-entries are
  excluded unless ``hidden`` is true, and ``limit`` caps the result count.
- Path containment reuses the R0-B verified scheme from
  ``tools/builtin/read.py`` (``_resolve_within_root``: NUL check → lexical
  resolve → containment → strict realpath → containment of the realpathed
  root) -- the same single-sourced face ``fs__read`` runs on; §5.2 L266
  lists root-internal read/glob/grep/ls as allow.

Deviations, quantified (float-reported): the default limit is 100
(``filesystem.ts`` L66 ``DEFAULT_SEARCH_LIMIT``); the hard cap is 1000
entries (Python-side safety face; no TS number exists for a listing cap)
and a larger requested ``limit`` is clamped to it (the ``fs__read``
limit-clamp precedent); the truncation marker reuses the ``glob.ts`` L39
wording with "or pattern" dropped (a directory listing has no pattern).
Listing stays synchronous like ``fs__read``: Phase-1 tool calls execute
serially under the registry lock (PHASE1-SLIM L85) and one directory read
is bounded by the directory itself.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import (
    _display_path,
    _effective_roots,
    _failure,
    _resolve_within_roots,
    _validate_roots,
)

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

LS_NAMESPACE = "fs"
LS_NAME = "ls"
LS_VERSION = 1

#: filesystem.ts L66 DEFAULT_SEARCH_LIMIT.
LS_DEFAULT_LIMIT = 100
#: Python-side hard cap (read.py limit-clamp precedent; float-reported).
LS_MAX_LIMIT = 1_000
#: glob.ts L39 marker wording, "or pattern" dropped (no pattern on this face).
LS_TRUNCATION_SUFFIX = " Consider using a more specific path.)"

LS_DESCRIPTION = (
    "List the entries of a directory inside the working directory. Directories are "
    "sorted before files and marked with a trailing '/'; hidden dot-entries are "
    "excluded unless hidden is true. Use limit to cap the number of entries returned."
)

LS_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "List a directory resolved inside the working directory.",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to list, relative to the working directory (defaults to it)",
        },
        "hidden": {
            "type": "boolean",
            "description": "Include hidden dot-entries (default: false).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": f"Maximum number of entries to return (default: {LS_DEFAULT_LIMIT})",
        },
    },
    "required": [],
    "additionalProperties": False,
}

LS_SPEC = ToolSpec(
    namespace=LS_NAMESPACE,
    name=LS_NAME,
    version=LS_VERSION,
    description=LS_DESCRIPTION,
    input_schema=LS_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


def _reject(path: str, reason: str) -> ToolResult:
    """Map a containment rejection to its §8 error code (read.py ``_rejection`` family)."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"Path not found: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


def _display_entry(working_dir: str, real_dir: str, name: str, is_dir: bool) -> str:
    """Entry path relative to the working directory, '/' separators, '/' suffix for dirs.

    filesystem.ts L191 renders directories with a trailing path separator;
    model-facing paths are normalized to '/' so the output is identical on
    Windows and Linux (float-reported). R3-S6: entries under a secondary
    root render as absolute paths (read.py ``_display_path``).
    """
    suffix = "/" if is_dir else ""
    return _display_path(working_dir, os.path.join(real_dir, name)) + suffix


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str] = ()) -> ToolResult:
    # The Invoker strictly validated this input against LS_INPUT_SCHEMA before
    # execute (adapter contract, §5.1 L257); the casts below are static narrows.
    path = cast(str, input.get("path", "."))
    hidden = cast(bool, input.get("hidden", False))
    limit = min(cast(int, input.get("limit", LS_DEFAULT_LIMIT)), LS_MAX_LIMIT)

    real, reason = _resolve_within_roots(_effective_roots(roots, context.working_dir), path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _reject(path, reason)
    if not os.path.isdir(real):
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, f"Path is not a directory: {path}")

    rows: list[tuple[int, str, bool]] = []
    try:
        with os.scandir(real) as scan:
            for entry in scan:
                try:
                    is_dir = entry.is_dir()
                    is_file = entry.is_file()
                except OSError:
                    continue  # dangling symlink/stat race: skip the entry
                # filesystem.ts L186: only file/directory entries are listed.
                if not is_dir and not is_file:
                    continue
                if not hidden and entry.name.startswith("."):
                    continue
                rows.append((0 if is_dir else 1, entry.name, is_dir))
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot list directory {path}: {error}")

    # filesystem.ts L196: directories first, then by name.
    rows.sort(key=lambda row: (row[0], row[1]))
    truncated = len(rows) > limit
    shown = rows[:limit]

    if not shown:
        text = "No entries found"
    else:
        lines = [_display_entry(context.working_dir, real, name, is_dir) for _, name, is_dir in shown]
        if truncated:
            # glob.ts L39 marker wording, "or pattern" dropped.
            lines.append("")
            lines.append(f"(Results are truncated: showing first {len(shown)} results.{LS_TRUNCATION_SUFFIX}")
        text = "\n".join(lines)
    return ToolResult(content=[ToolTextContent(type="text", text=text)])


class FsLsAdapter:
    """The ``fs__ls`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the listing is synchronous by
    design (module docstring: serial Phase-1 execution bounds the cost).
    ``roots`` is the optional R3-S6 config-declared root extension (empty
    default = the R2 single-root behavior, ⑤).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return LS_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input, self._roots)
