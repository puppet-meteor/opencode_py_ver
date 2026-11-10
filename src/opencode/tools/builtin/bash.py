"""Builtin ``bash__run`` tool (R3-S5; CORE-CONTRACTS §5.3 L276-277, plan §2.5, PHASE1-SLIM §4 L93).

Runs one shell command through the controlled subprocess environment
(:mod:`opencode.process`): the interpreter is selected explicitly (Unix
bash / Windows PowerShell, availability checked and cached per process --
never ``create_subprocess_shell``'s platform default; §5.3 L276), the
command string is passed as a single argv element with no JSON repair
(§5.3 L276), and the run carries the §5.3 discipline: 120 s default
timeout (TS ``shell.ts`` L24 ``DEFAULT_TIMEOUT_MS = 2 * 60 * 1_000`` --
exactly 120 seconds), output capped at 64 KiB with an explicit truncation
marker and original size (§5.3 L274 last-sentence precedent) while both
pipes keep draining (no dead-lock under floods), and the process tree
terminated and reaped on timeout/cancel (Windows Job Object / Unix
process group). A kill that cannot be confirmed surfaces as an unknown
effect through the §8 error face (the Invoker maps adapter errors of
side-effecting tools to ``failed``/``unknown``, §2.1 L35).

Approval (§5.2 L266/L268): the ``bash`` namespace is already in the
frozen ``_ASK_NAMESPACES`` default table (``tools/policy.py``), so every
``bash__run`` call asks by default; the policy binds the full
command/cwd pair via ``extract_resources`` (the S2-fixed input-key
convention -- this schema uses exactly ``command``/``cwd``).

Deny list (OP-R3-2, plan §3 item 25 / §4 difference table 5, L96 hard
constraint): a minimal advisory regex set -- a hit prepends a warning to
the result but **never blocks**; the TS ``shell/scan.ts`` 2409-line
lexer is deliberately not ported. §5.3 L276 last sentence, verbatim
truth: a dangerous-command regex cannot prove safety. The C2 enrichment
(T-C1-065) widens that scan with the structured decode from
:mod:`opencode.shellparse` (quote-masked and line-continuation-masked
forms now reach the hint); the raw regex floor, the label priority order
and the frozen advisory text are unchanged, and a parse failure
degrades to the raw floor (advisory material only -- the parse face
never touches the execution path).

TS mapping (recorded fingerprint #102): ``tool/plugin/shell.ts`` (293L)
-- ``DEFAULT_TIMEOUT_MS`` L24, the timeout=0-disables semantics (L55
description), the input face (command/workdir/timeout), the timeout
message ("Command exceeded timeout of N ms. Retry with a larger
timeout..."), ``StructuredOutput`` {exit, signal, truncated, timeout};
``shell/result.ts`` (81L) -- the notice family ("Timed out before
completion" / "Exited with code N"). Deviations (quantified,
float-reported in the progress file): no background/persistent-pty face
(difference table 6 -- the tree is swept even on normal exit);
``workdir`` renamed ``cwd`` (the S2 ``extract_resources`` key pins the
approval binding); timeout unit is seconds (the whole Python codebase's
unit; the 120 value is TS-exact); streams stay separate instead of a
pty-merged capture (deterministic rendering); interpreter availability
is a per-process cached probe rather than TS's config-time shell
selection.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    RunResult,
    resolve_interpreter,
    run_command,
)
from opencode.shellparse import parse_bash

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

BASH_NAMESPACE = "bash"
BASH_NAME = "run"
BASH_VERSION = 1

#: §5.3 default, TS shell.ts L24 (2 * 60 * 1_000 ms == 120 s, zero deviation).
BASH_DEFAULT_TIMEOUT_S = DEFAULT_TIMEOUT_S
#: §5.3 L274 last sentence: output default 64 KiB, marked truncation + original size.
BASH_MAX_OUTPUT_BYTES = DEFAULT_MAX_OUTPUT_BYTES

BASH_DESCRIPTION = (
    "Execute a shell command and return its output. Commands run on the explicit "
    "interpreter of the host platform (bash on Unix, PowerShell on Windows). "
    "Quote file paths containing spaces or special characters. The command runs "
    "with a default timeout of 120 seconds; pass a larger timeout for long-running "
    "commands or 0 to disable the timeout. Output above the limit is truncated. "
    "Prefer dedicated file tools over shell commands when possible."
)

BASH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Run one shell command through the explicitly selected interpreter.",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command string to execute",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory to execute the command in (defaults to the session working directory)",
        },
        "timeout": {
            "type": "integer",
            "minimum": 0,
            "description": f"Timeout in seconds; 0 disables the timeout (default {BASH_DEFAULT_TIMEOUT_S:.0f})",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

BASH_SPEC = ToolSpec(
    namespace=BASH_NAMESPACE,
    name=BASH_NAME,
    version=BASH_VERSION,
    description=BASH_DESCRIPTION,
    input_schema=BASH_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    idempotency_support=False,
)

#: OP-R3-2: the minimal advisory deny set. Advisory only -- a hit is a
#: warning in the result, never a block (L96: the deny list is a hint, not
#: a sandbox; §5.3 L276 last sentence: a regex cannot prove safety).
_DENY_HINT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive root/home deletion", re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)+(?:/|~)(?=\s|\*|$)")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{[^}]*\}\s*;\s*:")),
    ("filesystem format", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    ("Windows disk format", re.compile(r"\bformat\s+[c-z]:", re.IGNORECASE)),
    ("raw device overwrite", re.compile(r"\bdd\b[^;|&]*\bof=/dev/")),
)

_ADVISORY_TEXT = (
    "[advisory] the command matches a known dangerous pattern ({label}); the deny list is "
    "an advisory hint, not a sandbox -- the command was not blocked"
)


def _decoded_scan_surfaces(command: str) -> tuple[str, ...]:
    """The C2 enrichment faces (T-C1-065): each pipeline stage's joined decoded
    argv plus each decoded argv item, from the :mod:`opencode.shellparse` prototype.

    Advisory material only. Fail-open: a parse anomaly (ShellParseError or any
    other exception from the parse face) yields no faces -- the raw regex floor
    alone decides, exactly as before the enrichment.
    """
    try:
        parsed = parse_bash(command)
    except Exception:
        return ()
    surfaces: list[str] = []
    for statement in parsed.statements:
        for segment in statement.segments:
            argv = segment.argv
            surfaces.append(" ".join(argv))
            surfaces.extend(argv)
    return tuple(surfaces)


def _deny_label(command: str) -> str | None:
    """The first deny-list label matching ``command``, or None (advisory only).

    C2 enrichment (T-C1-065): the scan surface is the raw command string (the
    historical floor, preserved verbatim) plus the structured decode of the
    same string. Quote masking (``rm -rf "/"``) and line-continuation masking
    (``rm \\<newline>-rf /``) now reach the advisory face; a parse failure
    degrades to the raw floor unchanged. The label priority stays the tuple
    order of ``_DENY_HINT_PATTERNS``: the first pattern hitting any surface
    wins. A hit only prepends the advisory text -- never a block.
    """
    surfaces = (command, *_decoded_scan_surfaces(command))
    for label, pattern in _DENY_HINT_PATTERNS:
        if any(pattern.search(surface) for surface in surfaces):
            return label
    return None


def _truncation_marker(stream: str, shown: str, original_bytes: int) -> str:
    """§5.3 L274 last sentence: mark the truncation and the original size."""
    shown_bytes = len(shown.encode("utf-8", "replace"))
    return (
        f"[{stream} truncated: showing {shown_bytes} of {original_bytes} bytes (limit {BASH_MAX_OUTPUT_BYTES} bytes)]"
    )


def _notice(result: RunResult) -> str | None:
    """TS shell/result.ts ``notice``: the terminal one-line status."""
    if result.timed_out:
        return "Timed out before completion"
    if result.returncode is not None and result.returncode != 0:
        return f"Exited with code {result.returncode}"
    return None


def _render(result: RunResult, *, deny_label: str | None) -> list[ToolTextContent | ToolFileContent]:
    """The model-facing text items: advisory, capped streams, markers, notice."""
    texts: list[str] = []
    if deny_label is not None:
        texts.append(_ADVISORY_TEXT.format(label=deny_label))
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


def _structured(result: RunResult) -> dict[str, JsonValue]:
    """TS shell.ts ``StructuredOutput``: exit/signal/truncated/timeout."""
    structured: dict[str, JsonValue] = {
        "exit": result.returncode,
        "truncated": result.stdout_truncated or result.stderr_truncated,
        "timeout": result.timed_out,
    }
    if result.returncode is not None and result.returncode < 0:
        # POSIX convention: a negative exit code encodes the killing signal.
        structured["signal"] = f"SIG{-result.returncode}"
    return structured


def _failure(code: CoreErrorCode, message: str) -> ToolResult:
    """A graceful adapter failure: model-readable text + §8 error (§7 L341)."""
    return ToolResult(
        content=[ToolTextContent(type="text", text=message)],
        error=CoreError(code=code, message=message, retryable=False),
    )


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against BASH_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts below are
    # static narrows, not conversions. A caller bypassing the Invoker gets
    # a TypeError that the Invoker's exception path turns into a
    # model-readable failed result -- fail closed either way.
    command = cast(str, input["command"])
    if not command:
        # grep.ts L19's pattern rule is the precedent for this Python-side rule.
        return _failure(CoreErrorCode.VALIDATION_ERROR, "Command must not be empty")
    raw_cwd = input.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else context.working_dir
    if not os.path.isdir(cwd):
        return _failure(CoreErrorCode.TOOL_FAILED, f"Working directory does not exist: {cwd}")
    timeout_s = cast("int | float", input.get("timeout", BASH_DEFAULT_TIMEOUT_S))
    deny_label = _deny_label(command)

    interpreter, problem = resolve_interpreter()
    if interpreter is None:
        assert problem is not None  # the discovery always states a problem on failure
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            f"bash__run requires an explicit shell interpreter, but {problem}. "
            "Install bash (Unix) or PowerShell (Windows) and retry; no silent fallback "
            "interpreter is used (CORE-CONTRACTS §5.3 L276).",
        )

    # §5.3 "子进程用受控环境": the inherited environment plus the TS
    # shell.ts prepare anchors (AGENT/OPENCODE/AI_AGENT/OPENCODE_SESSION_ID).
    env = dict(os.environ)
    env["AGENT"] = "1"
    env["OPENCODE"] = "1"
    env["AI_AGENT"] = "opencode"
    env["OPENCODE_SESSION_ID"] = context.session_id

    try:
        # Runs in the adapter's worker thread (execute wraps _execute): a long
        # command never blocks the event loop; the cooperative cancel signal
        # (context.cancel) is polled from this thread on every runner tick.
        result = run_command(
            interpreter.argv(command),
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            max_output_bytes=BASH_MAX_OUTPUT_BYTES,
            should_cancel=context.cancel.is_set,
        )
    except OSError as error:
        # e.g. the interpreter vanished between resolution and spawn.
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to execute command: {command}: {error}")

    content = _render(result, deny_label=deny_label)
    if result.timed_out:
        # shell.ts timeout message family; the Invoker maps this to the
        # §2.1 L35 failed/unknown terminal (a killed command's effects are
        # not provable), never a fabricated success.
        message = (
            f"Command exceeded timeout of {timeout_s:.0f} seconds. Retry with a larger "
            "timeout if the command is expected to take longer."
        )
        return ToolResult(
            content=content,
            structured_output=_structured(result),
            error=CoreError(code=CoreErrorCode.TOOL_TIMEOUT, message=message, retryable=True),
        )
    if result.cancelled:
        # §7 L337/L335: a started command cancelled mid-run keeps its honest
        # terminal -- the tree was killed, the effects are unknown.
        return ToolResult(
            content=content,
            structured_output=_structured(result),
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message=(
                    "Command cancelled while running: the process tree was terminated "
                    "and the final state could not be confirmed"
                ),
                retryable=True,
            ),
        )
    if not result.reaped:
        # §5.3: cleanup could not be confirmed -> unknown effect.
        return ToolResult(
            content=content,
            structured_output=_structured(result),
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message="Command finished but its process cleanup could not be confirmed",
                retryable=False,
            ),
        )
    # A completed command (any exit code) succeeded as a tool run: the exit
    # code is data (notice + structured_output), not a tool failure.
    return ToolResult(content=content, structured_output=_structured(result))


class BashAdapter:
    """The ``bash__run`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` off-loads the blocking subprocess loop to a worker thread
    so a long command never blocks the event loop (run_status, approvals
    and close stay responsive, §7 L316); the cooperative cancel signal is
    polled from that thread on every runner tick.
    """

    @property
    def spec(self) -> ToolSpec:
        return BASH_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await asyncio.to_thread(_execute, context, input)
