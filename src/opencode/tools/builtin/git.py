"""Builtin ``git__status`` / ``git__diff`` tools (P2-S6 M2-git; T-C1-053).

PHASE2-MODULES L67 is the authority sentence (translated): "status/diff
first, then approved worktree operations; record cwd/roots and conflicts;
never silently overwrite uncommitted files" -- this module is exactly the
read-only first half (``status`` + ``diff``); the approved worktree
operations and the conflict face belong to the later slices (red line: no
add/commit/push/worktree subcommand can appear here). The structured
output records ``cwd``/``roots`` (the L67 "record cwd/roots" first half);
the "record...conflicts" half is worktree-domain.

Zero TS anchor: the recorded TS fingerprint baseline (doc/source-baseline
.json, 59 files) has no git tool carrier, so this is a Python-side new
design -- no TS fingerprint is claimed. The faces that do have precedents
follow them exactly:

- subprocess face: the bash.py template (:func:`opencode.process.run_command`
  through the controlled runner -- strong 120 s timeout, 64 KiB per-stream
  cap with explicit truncation marker + original size, process tree killed
  and reaped on timeout/cancel);
- exit codes are data (bash.py "any exit code...data, not a tool failure"):
  a non-repository directory (git exits 128/129 with a fatal/warning on
  stderr) is carried in content + structured_output, never a tool error;
  TOOL_FAILED is reserved for the process surface (git missing / OSError),
  timeout maps to TOOL_TIMEOUT (retryable) and cancellation to
  EXECUTION_UNKNOWN (retryable), both bash.py-shaped;
- path resolution reuses the read.py R0-B helper family
  (``_effective_roots`` + ``_resolve_within_roots``: NUL check -> lexical
  resolve -> containment -> strict realpath -> containment of the realpathed
  root); out-of-root/traversal/symlink escapes are rejected with the same
  clear-error-no-guess-fix semantics; the ``roots`` constructor kwarg is the
  fs-family host-config shape;
- git discovery is PATH ``shutil.which`` cached per process plus a
  ``git --version`` identification check (the grep.py ``_RG_CACHE`` /
  ``_rg_version_ok`` precedent -- this also surfaces a Windows app
  execution alias stub); a missing or unidentifiable git fails with a
  TOOL_FAILED diagnostic that names the dependency and the fix.

Scheduler-pinned argv (T-C1-053 rulings a/b), with one quantified mechanical
correction (float-reported in the progress file): ``--no-optional-locks``
is a git *global* option, so its only valid position is before the
subcommand -- ``git status --no-optional-locks`` is a usage error
(exit 129, probed on git 2.53.0.windows.2, T-C1-053 probe P1). The pinned
flag set is kept verbatim, one position moved:

- git__status: ``git --no-optional-locks status --porcelain=v1 --branch
  -- <pathspec>`` (read-only guarantee + machine-readable v1 + branch
  header; empty pathspec = the whole working directory);
- git__diff: ``git diff [--staged] -- <pathspec>`` (``staged`` truthy
  inserts ``--staged``; working tree vs index by default, index vs HEAD
  when staged).

Environment: inherited plus ``GIT_TERMINAL_PROMPT=0`` (a credential prompt
can never hang the run); no AGENT/OPENCODE marker face (zero TS anchor --
the bash.ts prepare face is deliberately not imported). The timeout is a
fixed 120 s (BASH_DEFAULT_TIMEOUT_S same value, an independent constant;
no input override parameter), the output cap 64 KiB (same value,
independent constant). ``execute`` off-loads the blocking subprocess loop
to a worker thread (bash.py form) and polls ``context.cancel`` every
runner tick.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    RunResult,
    run_command,
)
from opencode.tools.builtin.read import (
    _effective_roots,
    _failure,
    _resolve_within_roots,
    _validate_roots,
)

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

GIT_NAMESPACE = "git"
GIT_STATUS_NAME = "status"
GIT_DIFF_NAME = "diff"
GIT_VERSION = 1

#: Ruling f: fixed 120 s (the bash.py default value, an independent constant;
#: no input override parameter on the git face).
GIT_DEFAULT_TIMEOUT_S = DEFAULT_TIMEOUT_S
#: Ruling f: per-stream 64 KiB cap with explicit truncation marker + original size.
GIT_MAX_OUTPUT_BYTES = DEFAULT_MAX_OUTPUT_BYTES
#: How long the ``git --version`` probe may take (grep.py RG_VERSION_TIMEOUT_S form).
GIT_VERSION_TIMEOUT_S = 5.0

GIT_STATUS_DESCRIPTION = (
    "Show the git working tree status inside the working directory in machine-readable "
    "porcelain format: one branch header line plus one line per change (staged, unstaged "
    "or untracked). Pass path to narrow the report to one file or directory. Requires the "
    "git binary to be installed."
)

GIT_DIFF_DESCRIPTION = (
    "Show git's unified diff of changes inside the working directory. By default compares "
    "the working tree against the index (unstaged changes); pass staged to compare the "
    "index against HEAD instead. Pass path to narrow the diff to one file or directory. "
    "Requires the git binary to be installed."
)

GIT_STATUS_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Show the git working tree status (porcelain v1 with a branch header).",
    "properties": {
        "path": {
            "type": "string",
            "description": "File or directory to report on (defaults to the whole working directory)",
        },
    },
    "additionalProperties": False,
}

GIT_DIFF_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Show git's unified diff for the working directory.",
    "properties": {
        "path": {
            "type": "string",
            "description": "File or directory to diff (defaults to the whole working directory)",
        },
        "staged": {
            "type": "boolean",
            "description": "Diff the index against HEAD instead of the working tree against the index (default: false)",
        },
    },
    "additionalProperties": False,
}

GIT_STATUS_SPEC = ToolSpec(
    namespace=GIT_NAMESPACE,
    name=GIT_STATUS_NAME,
    version=GIT_VERSION,
    description=GIT_STATUS_DESCRIPTION,
    input_schema=GIT_STATUS_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)

GIT_DIFF_SPEC = ToolSpec(
    namespace=GIT_NAMESPACE,
    name=GIT_DIFF_NAME,
    version=GIT_VERSION,
    description=GIT_DIFF_DESCRIPTION,
    input_schema=GIT_DIFF_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


# ---------------------------------------------------------------------------
# git discovery: PATH lookup + explicit availability check (grep.py form).
# ---------------------------------------------------------------------------

#: Process-lifetime cache: "git" -> (executable, problem).
_GIT_CACHE: dict[str, tuple[str | None, str | None]] = {}


def _which_git() -> str | None:
    """The system git is discovered on PATH (grep.py ``_which_rg`` form)."""
    return shutil.which("git")


def _git_version_ok(executable: str) -> bool:
    """The availability check: a runnable git identifies itself.

    ``git --version`` must run and print a ``git version <n>`` banner -- this
    also surfaces a broken executor shim (e.g. a Windows app execution alias
    stub that exits without running git, the ``_rg_version_ok`` precedent).
    """
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=GIT_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    banner = completed.stdout.strip().splitlines()
    return bool(banner) and banner[0].startswith("git version ")


def _discover_git() -> tuple[str | None, str | None]:
    """Returns ``(executable, None)`` or ``(None, problem)`` -- never a silent fallback."""
    executable = _which_git()
    if executable is None:
        return None, "it was not found on PATH"
    if not _git_version_ok(executable):
        return None, f"the executable found at {executable} did not identify as git (`git --version` failed)"
    return executable, None


def _resolve_git() -> tuple[str | None, str | None]:
    """Cached first-use resolution (the availability check runs once per process)."""
    if "git" not in _GIT_CACHE:
        _GIT_CACHE["git"] = _discover_git()
    return _GIT_CACHE["git"]


# ---------------------------------------------------------------------------
# Rendering: the bash.py faces (markers, notice) with cwd/roots recorded.
# ---------------------------------------------------------------------------


def _truncation_marker(stream: str, shown: str, original_bytes: int) -> str:
    """The §5.3 truncation marker: shown size, original size, the limit."""
    shown_bytes = len(shown.encode("utf-8", "replace"))
    return f"[{stream} truncated: showing {shown_bytes} of {original_bytes} bytes (limit {GIT_MAX_OUTPUT_BYTES} bytes)]"


def _notice(result: RunResult) -> str | None:
    """The terminal one-line status (bash.py ``_notice`` form)."""
    if result.timed_out:
        return "Timed out before completion"
    if result.returncode is not None and result.returncode != 0:
        return f"Exited with code {result.returncode}"
    return None


def _render(result: RunResult) -> list[ToolTextContent | ToolFileContent]:
    """The model-facing text items: capped streams, markers, notice."""
    texts: list[str] = []
    if result.stdout:
        texts.append(result.stdout)
    if result.stdout_truncated:
        texts.append(_truncation_marker("stdout", result.stdout, result.stdout_original_bytes))
    if result.stderr:
        texts.append(f"Stderr:\n{result.stderr}")
    if result.stderr_truncated:
        texts.append(_truncation_marker("stderr", result.stderr, result.stderr_original_bytes))
    notice = _notice(result)
    if notice is not None:
        texts.append(notice)
    if not result.reaped:
        texts.append("Process cleanup could not be confirmed")
    content: list[ToolTextContent | ToolFileContent] = [ToolTextContent(type="text", text=text) for text in texts]
    return content


def _structured(result: RunResult, context: ToolContext, roots: Sequence[str]) -> dict[str, JsonValue]:
    """exit/truncated/timeout (bash.py form) plus the L67 ``cwd``/``roots`` record."""
    structured: dict[str, JsonValue] = {
        "exit": result.returncode,
        "truncated": result.stdout_truncated or result.stderr_truncated,
        "timeout": result.timed_out,
        "cwd": context.working_dir,
        "roots": cast("JsonValue", list(_effective_roots(roots, context.working_dir))),
    }
    if result.returncode is not None and result.returncode < 0:
        # POSIX convention: a negative exit code encodes the killing signal.
        structured["signal"] = f"SIG{-result.returncode}"
    return structured


# ---------------------------------------------------------------------------
# The shared execute body (bash.py error mapping; rulings a/b/c/e/f/g).
# ---------------------------------------------------------------------------


def _reject(path: str, reason: str) -> ToolResult:
    """Map an R0-B containment rejection to its §8 error code (grep.py wording face)."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"Path does not exist: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


def _resolve_pathspec(
    input: JsonObject, roots: Sequence[str], working_dir: str
) -> tuple[list[str] | None, ToolResult | None]:
    """The pathspec for this call: ``([], None)`` whole-tree, ``([abs], None)`` resolved,
    or ``(None, rejection)`` when the path is out of root / traversing / missing.

    Ruling c: read.py helper family; the resolved absolute path is handed to git
    with ``/`` separators (native on POSIX, equally accepted on Windows). An
    empty string is treated as "omitted" (whole working directory) -- git
    2.53 itself tolerates an empty pathspec element, so the two faces agree.
    """
    raw = input.get("path")
    if not (isinstance(raw, str) and raw):
        return [], None
    real, reason = _resolve_within_roots(_effective_roots(roots, working_dir), raw)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return None, _reject(raw, reason)
    return [real.replace(os.sep, "/")], None


def _execute(context: ToolContext, input: JsonObject, roots: Sequence[str], *, name: str) -> ToolResult:
    """The shared body: resolve -> discover -> spawn -> map (one of status/diff)."""
    pathspec, rejection = _resolve_pathspec(input, roots, context.working_dir)
    if rejection is not None:
        return rejection
    assert pathspec is not None  # a rejection result implies the pathspec half is None

    executable, problem = _resolve_git()
    if executable is None:
        assert problem is not None  # the discovery always states a problem on failure
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            f"git is an external dependency of the git__{name} tool, but {problem}. "
            "Install a working git (https://git-scm.com/downloads) and retry; no silent "
            "fallback is performed.",
        )

    if name == GIT_STATUS_NAME:
        # Ruling a (with the one mechanical correction in the module docstring):
        # read-only guarantee + porcelain v1 + branch header + separator.
        argv = [executable, "--no-optional-locks", "status", "--porcelain=v1", "--branch", "--", *pathspec]
    else:
        # Ruling b: staged truthy inserts --staged before the separator.
        staged = cast(bool, input.get("staged", False))
        argv = [executable, "diff", *(["--staged"] if staged else []), "--", *pathspec]

    # Ruling g: inherited environment plus the no-prompt guard; no AGENT/OPENCODE
    # markers (zero TS anchor -- the bash.ts prepare face is not imported).
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        # Runs in the adapter's worker thread (execute wraps _execute): a slow
        # git never blocks the event loop; context.cancel is polled on every
        # runner tick.
        result = run_command(
            argv,
            cwd=context.working_dir,
            env=env,
            timeout_s=GIT_DEFAULT_TIMEOUT_S,
            max_output_bytes=GIT_MAX_OUTPUT_BYTES,
            should_cancel=context.cancel.is_set,
        )
    except OSError as error:
        # e.g. the binary vanished between discovery and spawn.
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to run git {name}: {error}")

    content = _render(result)
    structured = _structured(result, context, roots)
    if result.timed_out:
        # The timeout is fixed (no input override), so the actionable hint is
        # a narrower path, not a larger timeout (grep.py wording precedent).
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.TOOL_TIMEOUT,
                message=(
                    f"git {name} timed out after {GIT_DEFAULT_TIMEOUT_S:.0f} seconds. Consider narrowing the path."
                ),
                retryable=True,
            ),
        )
    if result.cancelled:
        # bash.py same form: a killed run keeps its honest unknown terminal.
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message=(
                    f"git {name} cancelled while running: the process tree was terminated "
                    "and the final state could not be confirmed"
                ),
                retryable=True,
            ),
        )
    if not result.reaped:
        # §5.3: cleanup could not be confirmed -> unknown effect.
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message="git finished but its process cleanup could not be confirmed",
                retryable=False,
            ),
        )
    # A completed command (any exit code, including the 128/129 non-repository
    # face) succeeded as a tool run: the exit code is data (notice +
    # structured_output), not a tool failure (ruling e, bash.py semantics).
    return ToolResult(content=content, structured_output=structured)


class GitStatusAdapter:
    """The ``git__status`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` off-loads the blocking subprocess loop to a worker thread
    (bash.py form). ``roots`` is the optional R3-S6 config-declared root
    extension (empty default = single-root behavior; the working_dir always
    stays a root).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return GIT_STATUS_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await asyncio.to_thread(_execute, context, input, self._roots, name=GIT_STATUS_NAME)


class GitDiffAdapter:
    """The ``git__diff`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    Same shape as :class:`GitStatusAdapter`; the ``staged`` input flag is the
    only behavioral difference (ruling b).
    """

    def __init__(self, *, roots: Sequence[str] = ()) -> None:
        self._roots = _validate_roots(roots)

    @property
    def spec(self) -> ToolSpec:
        return GIT_DIFF_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await asyncio.to_thread(_execute, context, input, self._roots, name=GIT_DIFF_NAME)
