"""Builtin ``fs__grep`` tool (R3-S3; plan §2.3, CORE-CONTRACTS §5.2 L266, PHASE1-SLIM §4 L91).

Content search over the working directory through an **external ripgrep
dependency**, with a capped number of matches and an explicit truncation
marker (acceptance A: "glob/grep/ls 限制数量 … 明确 rg 为外部依赖并检查可用性"
-- PHASE1-SLIM L91). §5.2 L266 lists root-internal read/glob/grep/ls as
allow; path containment reuses the R0-B verified ``read.py``
``_resolve_within_root`` scheme (NUL check → lexical resolve →
containment → strict realpath → containment of the realpathed root).

TS mapping (``packages/core/src/tool/plugin/grep.ts``): ``name`` L15,
the non-empty pattern rule (L19), the ``toModelContent`` rendering --
``Found N matches`` / per-file ``path:`` headers / ``  Line N: text``
previews (L44-55) -- the truncation marker (L58), ``Search path does not
exist`` (L106), the ``limit + 1`` fetch then ``slice(0, limit)`` with
``truncated = matches.length > limit`` (L119/L145), and the
``Invalid regex pattern`` / ``Unable to grep`` error wording (L162-163).
The default limit (100) and search timeout (30 s) come from
``filesystem.ts`` L66-67.

ripgrep dependency (plan §4 difference table 8, OP-R3-1 conservative
reading): TS ``ripgrep/binary.ts`` discovers a system rg on PATH (L69,
L121) and only then falls back to a vendored download pinned by a SHA-256
``verify`` (L155-158). Python declares rg as an external dependency and
**checks availability explicitly instead of ever degrading silently**:
discovery runs ``shutil.which`` plus an ``rg --version`` identification
check, is cached for the process lifetime, and an unavailable or broken
rg fails the call with a TOOL_FAILED diagnostic that names the
dependency and the fix -- there is no pure-Python fallback search. No
vendored download and no dependency declaration are added (OP-R3-1:
``pyproject``/``uv.lock`` changes are the scheduler's call).

Deviations from TS (quantified, float-reported): matches are sorted by
(path, line) after collection so the model-facing output is deterministic
(rg's parallel traversal order is not); matched-line previews are clamped
at the ``fs__read`` per-line limit (2000 chars, ``read.py``
``_clamp_line``); the hard result cap is 1000 (Python-side safety face;
TS has none) and a larger requested ``limit`` is clamped to it; a
single-file target is pre-checked for binary content (NUL in the first
8 KiB → explicit ``Cannot grep binary file`` diagnostic, the ``fs__read``
binary-face precedent) while directory searches rely on rg's own built-in
binary detection; rg's default ignore/hidden behavior is kept (hidden
files are not searched; ignore files are respected).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.process import run_command
from opencode.tools.builtin.read import (
    _clamp_line,
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

GREP_NAMESPACE = "fs"
GREP_NAME = "grep"
GREP_VERSION = 1

#: filesystem.ts L66 DEFAULT_SEARCH_LIMIT / L67 DEFAULT_SEARCH_TIMEOUT_MS.
GREP_DEFAULT_LIMIT = 100
GREP_TIMEOUT_S = 30.0
#: Python-side hard cap (read.py limit-clamp precedent; float-reported).
GREP_MAX_LIMIT = 1_000
#: binary.ts L155 verify analog: how long the ``rg --version`` probe may take.
RG_VERSION_TIMEOUT_S = 5.0
#: Single-file binary pre-check window (fs__read binary-face precedent).
_BINARY_SNIFF_BYTES = 8_192

GREP_DESCRIPTION = (
    "Search file contents using ripgrep's regular expression syntax or literal text matching "
    "inside the working directory. Returns matching file paths, line numbers, and line previews; "
    "narrow searches with path or include. Requires the ripgrep (rg) binary to be installed."
)

GREP_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Search file contents inside the working directory via the external ripgrep binary.",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regular expression or literal text to match in file contents.",
        },
        "path": {
            "type": "string",
            "description": "File or directory to search. Defaults to the current working directory.",
        },
        "include": {
            "type": "string",
            "description": 'Glob pattern to filter files (for example, "*.js" or "*.{ts,tsx}")',
        },
        "literal": {
            "type": "boolean",
            "description": "Treat `pattern` as exact text instead of a regular expression (default: false).",
        },
        "caseSensitive": {
            "type": "boolean",
            "description": "Use case-sensitive matching (default: true).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": f"Maximum number of matching lines to return (default: {GREP_DEFAULT_LIMIT})",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

GREP_SPEC = ToolSpec(
    namespace=GREP_NAMESPACE,
    name=GREP_NAME,
    version=GREP_VERSION,
    description=GREP_DESCRIPTION,
    input_schema=GREP_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


# ---------------------------------------------------------------------------
# ripgrep discovery: PATH lookup + explicit availability check (OP-R3-1).
# ---------------------------------------------------------------------------

#: Process-lifetime cache: "rg" -> (executable, problem).
_RG_CACHE: dict[str, tuple[str | None, str | None]] = {}


def _which_rg() -> str | None:
    """binary.ts L69/L121: the system ripgrep is discovered on PATH."""
    return shutil.which("rg")


def _rg_version_ok(executable: str) -> bool:
    """The availability check: a runnable rg identifies itself as ripgrep.

    binary.ts pins a vendored download by SHA-256 (L155-158); with a
    PATH-provided binary there is nothing to hash-verify, so the check is
    that ``rg --version`` runs and prints a ``ripgrep <version>`` banner
    (this also surfaces a broken executor shim, e.g. a Windows app
    execution alias stub).
    """
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=RG_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    banner = completed.stdout.strip().splitlines()
    return bool(banner) and banner[0].startswith("ripgrep ")


def _discover_rg() -> tuple[str | None, str | None]:
    """Returns ``(executable, None)`` or ``(None, problem)`` -- never a silent fallback."""
    executable = _which_rg()
    if executable is None:
        return None, "it was not found on PATH"
    if not _rg_version_ok(executable):
        return None, f"the executable found at {executable} did not identify as ripgrep (`rg --version` failed)"
    return executable, None


def _resolve_rg() -> tuple[str | None, str | None]:
    """Cached first-use resolution (the availability check runs once per process)."""
    if "rg" not in _RG_CACHE:
        _RG_CACHE["rg"] = _discover_rg()
    return _RG_CACHE["rg"]


# ---------------------------------------------------------------------------
# The rg subprocess: --json stream with an early stop at limit + 1.
# ---------------------------------------------------------------------------


def _parse_match(line: str) -> tuple[str, int, str] | None:
    """One ``--json`` line to ``(path, line_number, text)``; None for non-match records.

    Non-UTF-8 paths arrive base64-encoded in a ``bytes`` field and are
    skipped (float-reported); non-UTF-8 line content is decoded from the
    ``bytes`` field with replacement characters.
    """
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None  # defensive: skip an unparseable stream line
    if not isinstance(record, dict) or record.get("type") != "match":
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    path_field = data.get("path")
    if not isinstance(path_field, dict) or not isinstance(path_field.get("text"), str):
        return None  # binary (base64) path: not representable, skip
    line_number = data.get("line_number")
    if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
        return None
    lines_field = data.get("lines")
    text = ""
    if isinstance(lines_field, dict) and isinstance(lines_field.get("text"), str):
        text = cast(str, lines_field["text"])
    elif isinstance(lines_field, dict) and isinstance(lines_field.get("bytes"), str):
        try:
            text = base64.b64decode(cast(str, lines_field["bytes"])).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            text = ""
    return cast(str, path_field["text"]), line_number, text.rstrip("\r\n")


def _run_rg(
    executable: str,
    args: list[str],
    cwd: str,
    *,
    limit: int,
    timeout_s: float,
) -> tuple[list[tuple[str, int, str]], bool, int, str]:
    """Run rg and collect at most ``limit + 1`` matches (grep.ts L119 semantics).

    R3-S5 consolidation (plan §3 item 25): the subprocess runs through the
    unified controlled runner (:mod:`opencode.process.runner`), so the
    deadline is a strong monotonic-clock timeout that fires even when rg
    produces no output at all -- the previous read-loop deadline was only
    checked when a stdout line arrived, so a silently hung rg would park
    the serial Invoker forever. The limit+1 early stop is a per-line
    callback, and the process (tree) is killed and reaped on every exit
    path (Windows Job Object / Unix process group). Matches are collected
    per line in the callback, so the runner's 64 KiB buffered-stdout cap
    does not affect them (quantified delta, progress file); an early stop
    kills rg, hence a nonzero exit code there is expected.
    """
    matches: list[tuple[str, int, str]] = []

    def _collect(line: str) -> bool:
        match = _parse_match(line)
        if match is not None:
            matches.append(match)
        return len(matches) > limit

    result = run_command(args, cwd=cwd, timeout_s=timeout_s, on_stdout_line=_collect)
    returncode = result.returncode if result.returncode is not None else -1
    return matches, result.timed_out, returncode, result.stderr


def _render(matches: list[tuple[str, int, str]], truncated: bool) -> str:
    """grep.ts ``toModelContent`` L44-61: match count, per-file groups, marker."""
    if not matches:
        return "No matches found"
    lines = [f"Found {len(matches)} matches"]
    current: str | None = None
    for path, line_number, text in matches:
        if current != path:
            if current is not None:
                lines.append("")
            current = path
            lines.append(f"{path}:")
        lines.append(f"  Line {line_number}: {_clamp_line(text)}")
    if truncated:
        lines.append("")
        lines.append(
            f"(Results are truncated: showing first {len(matches)} results. "
            "Consider using a more specific path or pattern.)"
        )
    return "\n".join(lines)


def _reject(path: str, reason: str) -> ToolResult:
    """Map a containment rejection to its §8 error code (grep.ts L106 wording)."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"Search path does not exist: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str] = ()) -> ToolResult:
    # The Invoker strictly validated this input against GREP_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts are narrows.
    pattern = cast(str, input["pattern"])
    if not pattern:
        # grep.ts L19: the pattern must not be empty (the strict schema
        # subset has no minLength, so the rule lives here).
        return _failure(CoreErrorCode.VALIDATION_ERROR, "Pattern must not be empty")
    path = cast(str, input.get("path", "."))
    include = cast("str | None", input.get("include"))
    literal = cast(bool, input.get("literal", False))
    case_sensitive = cast(bool, input.get("caseSensitive", True))
    limit = min(cast(int, input.get("limit", GREP_DEFAULT_LIMIT)), GREP_MAX_LIMIT)

    real, reason = _resolve_within_roots(_effective_roots(roots, context.working_dir), path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _reject(path, reason)
    if os.path.isdir(real):
        cwd, file_arg = real, "."
    elif os.path.isfile(real):
        # Single-file target: the binary-file diagnostic face (fs__read
        # precedent) before handing the file to rg.
        with open(real, "rb") as handle:
            head = handle.read(_BINARY_SNIFF_BYTES)
        if b"\x00" in head:
            return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot grep binary file: {path}")
        cwd, file_arg = os.path.dirname(real), os.path.basename(real)
    else:
        return _failure(CoreErrorCode.NOT_FOUND, f"Search path does not exist: {path}")

    executable, problem = _resolve_rg()
    if executable is None:
        assert problem is not None  # the discovery always states a problem on failure
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            "ripgrep (rg) is an external dependency of the fs__grep tool, but "
            f"{problem}. Install a working ripgrep (https://github.com/BurntSushi/ripgrep) "
            "and retry; no silent fallback search is performed.",
        )

    args: list[str] = [executable, "--json"]
    if include is not None:
        args += ["-g", include]
    if literal:
        args += ["-F"]
    if not case_sensitive:
        args += ["-i"]
    args += ["-e", pattern, "--", file_arg]

    try:
        matches, timed_out, returncode, stderr = _run_rg(executable, args, cwd, limit=limit, timeout_s=GREP_TIMEOUT_S)
    except OSError as error:
        # e.g. the binary vanished between discovery and spawn.
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to grep for {pattern}: {error}")

    if timed_out:
        # grep.ts timeout wording, filesystem.ts L67 number.
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            f"Search timed out after {GREP_TIMEOUT_S:.0f} seconds. Consider using a more specific path or pattern.",
        )
    # An early stop at limit + 1 kills rg, so a nonzero exit there is
    # expected; otherwise rg exits 0 (matches) or 1 (no matches), and any
    # other code is an error to diagnose.
    early_stop = len(matches) > limit
    if not early_stop and returncode not in (0, 1):
        detail = " ".join(stderr.split())[:400] or f"rg exited with code {returncode}"
        if "regex parse error" in stderr.lower():
            # grep.ts L162 wording.
            return _failure(CoreErrorCode.TOOL_FAILED, f"Invalid regex pattern: {detail}")
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to grep for {pattern}: {detail}")

    # Deterministic output: sort by (path, line), then the grep.ts grouping.
    display = [
        (
            _display_path(context.working_dir, os.path.join(cwd, raw_path)),
            raw_line,
            raw_text,
        )
        for raw_path, raw_line, raw_text in matches
    ]
    display.sort(key=lambda match: (match[0], match[1]))
    truncated = early_stop
    text = _render(display[:limit], truncated)
    return ToolResult(content=[ToolTextContent(type="text", text=text)])


class FsGrepAdapter:
    """The ``fs__grep`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the rg subprocess runs
    synchronously by design (serial Phase-1 execution, PHASE1-SLIM L85,
    and the match cap bounds the collected set). ``roots`` is the optional
    R3-S6 config-declared root extension (empty default = the R2
    single-root behavior, ⑤).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return GREP_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input, self._roots)
