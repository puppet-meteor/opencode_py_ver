"""Builtin ``fs__glob`` tool (R3-S3; plan §2.3, CORE-CONTRACTS §5.2 L266, PHASE1-SLIM §4 L91).

Matches file paths against a glob pattern inside the working directory
with a capped number of results and an explicit truncation marker
(acceptance A: "glob/grep/ls 限制数量" -- PHASE1-SLIM L91).

TS mapping (``packages/core/src/tool/plugin/glob.ts``): ``name`` L15, the
``pattern``/``path``/``hidden``/``limit`` input face (L16-27), the
``toModelContent`` rendering with ``No files found`` and the truncation
marker (L34-39), ``Search path does not exist`` (L84), ``Search path is
not a directory`` (L89), the ``limit + 1`` fetch then ``slice(0, limit)``
with ``truncated = entries.length > limit`` (L98/L119), and the
default/cap constants come from ``filesystem.ts`` L66
(``DEFAULT_SEARCH_LIMIT = 100``).

Deviations from TS (plan §4 difference table 8 / OP-R3-1 conservative
reading, quantified and float-reported):

- Matching runs through the stdlib ``glob`` module, not a ripgrep
  subprocess: the external ripgrep dependency is declared on ``fs__grep``
  only (the one tool whose semantics need it), so ``fs__glob`` works on a
  machine without rg. Stdlib glob has no ignore-file awareness (TS routes
  through ripgrep, which respects ``.gitignore`` by default) and is
  memory-bounded by the ``limit + 1`` early stop, not by a search timeout
  (TS's 30 s timeout guards a subprocess; there is no subprocess here).
- Results are sorted by path for deterministic output, and rendered
  relative to the working directory with '/' separators (Windows/Linux
  identical model-facing output).
- An absolute pattern or one containing a ``..`` component is rejected
  with VALIDATION_ERROR: stdlib glob resolves the pattern itself (rg only
  filters files found under its cwd), so the containment guard has to live
  on the pattern, not just the search path. Path containment otherwise
  reuses the R0-B verified ``read.py`` ``_resolve_within_root`` scheme;
  §5.2 L266 lists root-internal read/glob/grep/ls as allow.
- The hard cap is 1000 results (Python-side safety face; TS has no cap
  here) and a larger requested ``limit`` is clamped to it (the
  ``fs__read`` limit-clamp precedent).
"""

from __future__ import annotations

import glob as _globlib
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

GLOB_NAMESPACE = "fs"
GLOB_NAME = "glob"
GLOB_VERSION = 1

#: filesystem.ts L66 DEFAULT_SEARCH_LIMIT.
GLOB_DEFAULT_LIMIT = 100
#: Python-side hard cap (read.py limit-clamp precedent; float-reported).
GLOB_MAX_LIMIT = 1_000

GLOB_DESCRIPTION = (
    'Search file paths using a glob pattern (examples: "**/*.ts", "src/**/*.tsx") '
    "inside the working directory. Directories are not returned; hidden dot-entries are "
    "excluded unless hidden is true. Use limit to cap the number of results."
)

GLOB_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Match file paths against a glob pattern inside the working directory.",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern to match files against (relative to the search path)",
        },
        "path": {
            "type": "string",
            "description": "Directory to search. Defaults to the current working directory.",
        },
        "hidden": {
            "type": "boolean",
            "description": "Include hidden files and directories (default: false).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": f"Maximum number of matching files to return (default: {GLOB_DEFAULT_LIMIT})",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

GLOB_SPEC = ToolSpec(
    namespace=GLOB_NAMESPACE,
    name=GLOB_NAME,
    version=GLOB_VERSION,
    description=GLOB_DESCRIPTION,
    input_schema=GLOB_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


def _reject(path: str, reason: str) -> ToolResult:
    """Map a containment rejection to its §8 error code (glob.ts L84 wording)."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"Search path does not exist: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


def _pattern_stays_inside(pattern: str) -> bool:
    """Containment guard on the pattern itself (stdlib glob resolves it as a path)."""
    if not pattern or os.path.isabs(pattern):
        return False
    return all(part != ".." for part in pattern.replace("\\", "/").split("/"))


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str] = ()) -> ToolResult:
    # The Invoker strictly validated this input against GLOB_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts are narrows.
    pattern = cast(str, input["pattern"])
    path = cast(str, input.get("path", "."))
    hidden = cast(bool, input.get("hidden", False))
    limit = min(cast(int, input.get("limit", GLOB_DEFAULT_LIMIT)), GLOB_MAX_LIMIT)

    if not _pattern_stays_inside(pattern):
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Pattern must be relative and stay inside the working directory: {pattern}",
        )

    real, reason = _resolve_within_roots(_effective_roots(roots, context.working_dir), path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _reject(path, reason)
    if not os.path.isdir(real):
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, f"Search path is not a directory: {path}")

    # glob.ts L98: fetch limit + 1, then slice -- the +1 makes "more than
    # the limit" a local decision and bounds the collected set.
    collected: list[str] = []
    for candidate in _globlib.iglob(pattern, root_dir=real, recursive=True, include_hidden=hidden):
        if os.path.isfile(os.path.join(real, candidate)):
            collected.append(candidate)
            if len(collected) > limit:
                break
    collected.sort()
    truncated = len(collected) > limit
    shown = collected[:limit]

    if not shown:
        text = "No files found"
    else:
        lines = [_display_path(context.working_dir, os.path.join(real, candidate)) for candidate in shown]
        if truncated:
            lines.append("")
            lines.append(
                f"(Results are truncated: showing first {len(shown)} results. "
                "Consider using a more specific path or pattern.)"
            )
        text = "\n".join(lines)
    return ToolResult(content=[ToolTextContent(type="text", text=text)])


class FsGlobAdapter:
    """The ``fs__glob`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the walk is synchronous by
    design (see module docstring: serial Phase-1 execution bounds the cost).
    ``roots`` is the optional R3-S6 config-declared root extension (empty
    default = the R2 single-root behavior, ⑤).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return GLOB_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input, self._roots)
