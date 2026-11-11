"""Builtin ``pty__run`` tool (T-C1-072 PTY T1 Windows + T-C1-074 PTY T2 POSIX; CORE-CONTRACTS §5.3 L315, §5.2).

The single-call pseudo-terminal execution face: one command spawns under
a PTY, its stdout/stderr arrive MERGED through the terminal (a real
terminal's rendering, ANSI included -- exactly what a terminal user
sees), and the call returns when the run reaches its terminal. Two
platform legs carry the tool, gated independently (PHASE2-MODULES L68:
an unverified platform is not enabled): the win32 winpty-agent face
(T1, T-C1-072) and the POSIX stdlib face (T2, T-C1-074).

**S1 backend decision (T-B-086 ruling, option (a))**: the pywinpty
LEGACY WINPTY AGENT backend (``winpty.enums.Backend.WinPTY``) carries
this tool. The T0 evidence (doc/evidence/pty0/platform.json) recorded
the split: the vendored ConPTY host does not dispatch the console
control event for a PTY-written ``0x03`` (no observable interrupt), while
the legacy winpty agent delivers it (child KeyboardInterrupt marker +
exit code 130). All four primitives were then verified on the winpty
agent backend in ``tests/integration/test_pty_s1_winpty_backend.py``
(resize / encoding / process_tree_exit with the production Job Object /
ctrl_c re-assert) BEFORE this implementation (PHASE2-MODULES L68:
verification precedes wiring). p2-slices L169's "ConPTY" wording is
superseded by that empirical ruling -- the backend that actually carries
all four primitives is the one that carries the tool.

**T2 POSIX face (T-C1-074, stdlib-only)**: ``os.forkpty`` +
``os.execve`` of the explicit interpreter (never ``shell=True``; the
forkpty child is a session leader on a fresh pty, so pgid == pid --
the killpg coverage guarantee), the initial winsize lands on the master
before the interpreter can boot far enough to read geometry, master
reads go through an INCREMENTAL UTF-8 decoder (a multibyte sequence is
never split across chunk boundaries -- the encoding-leg correctness
form), ``OSError`` (EIO) is the EOF face once the last slave descriptor
closes, resizes are ``TIOCSWINSZ`` ioctls on the master, and the Ctrl-C
primitive is an observe-style ``\x03`` write to the master with a
draining grace window and NO marker matcher (the #198 four-round
ruling: the ECHOCTL ``^C`` echo is part of what a terminal user sees,
and the raw stream is returned verbatim -- the tool never
pattern-matches interrupt markers). All four primitives were verified
on the CI ubuntu carrier BEFORE this wiring (#197 T0 probe + #198
discriminator chain, run 43052915106 all-green).

Input face (§5.3 L315 discipline, the bash__run precedent): the command
STRING runs on the EXPLICIT interpreter resolved at startup
(:func:`opencode.process.interpreter.resolve_interpreter` -- PowerShell
on Windows, bash on POSIX; never ``create_subprocess_shell``'s platform
default, never ``shell=True``), spawned as a plain argv under the PTY. The strict input
validator's frozen schema subset carries no array type, so the
explicit-argv array form is not expressible on this wire; the
explicit-interpreter form is the §5.3 L315 named mechanism and is used
verbatim.

§5.3 L315 discipline on the PTY face:

- **Timeout**: strong monotonic deadline (default 120 s, ``0`` disables
  -- bash.py semantics), polled whether or not output arrives.
- **Truncation + drain**: the merged stream is capped at 64 KiB with an
  explicit marker and the original byte count; reading never stops at
  the cap (the PTY keeps draining to its terminal -- the
  cap-only-stops-appending rule).
- **Cancellation = the Ctrl-C primitive**: a mid-run cancel writes
  ``\x03`` to the PTY input (the winpty agent backend delivers it as a
  console control event -- PowerShell forwards it to the child, observed
  on this stack; on POSIX the master write is ISIG-delivered to the
  foreground process group), the grace window keeps draining the merged
  stream (the observation face: the child's own output IS the evidence,
  never "sent and assumed"; the POSIX leg carries no marker matcher --
  the ECHOCTL ``^C`` echo renders as any terminal would show it), then
  the tree is terminated and reclaimed. The outcome keeps the honest
  unknown effect (a killed run's effects are not provable).
- **Tree reclamation**: the PTY child is captured in the PRODUCTION
  :class:`opencode.process.winjob.JobObject` immediately after spawn
  (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``; membership is inherited at
  creation), any ``input`` is written only AFTER the capture (the T0
  assign-before-proceed-gate order, so gate-driven grandchildren always
  land inside the job), and ``TerminateJobObject`` sweeps the tree on
  EVERY exit path -- a normal exit still reaps orphaned descendants
  (Phase 1 has no background face). A capture/termination failure
  degrades to the pty's own child termination (runner.py disclosed
  fallback); a cleanup that cannot be confirmed surfaces as
  ``reaped=False`` → effect_status=unknown. On POSIX the sweep is
  ``os.killpg(pid, SIGKILL)`` on the forkpty session group on EVERY
  exit path (the group id IS the child's pid; a grandchild holding the
  slave open past the child's death is broken by the 2.0 s EOF-settle
  backstop, then the sweep covers the whole group; ProcessLookupError
  means the group is already gone).
- **Resize primitive**: the optional ``resize`` input
  (``after_seconds``/``rows``/``cols``) schedules one mid-run
  ``setwinsize`` -- the running child observes the geometry change
  (``os.get_terminal_size(1)``, the T0 assertion form).

Environment: the inherited environment minus ``COLUMNS``/``LINES``
(those would shadow the real PTY geometry -- the T0 ``_child_env``
discipline); no AGENT/OPENCODE markers (zero TS anchor for the PTY card
-- the bash.ts prepare face is deliberately not imported, git.py
reasoning).

Capability negotiation (r0b-2 semantics): registration is composed on
the VERIFIED platforms only (:func:`pty_adapters`, the PHASE2-MODULES
L68 "an unverified platform is not enabled" gate -- win32 since the T1
ticket, linux since this T2 ticket; darwin and every other unverified
platform composes nothing); on win32 a missing pywinpty extras install
returns ``UnsupportedCapability`` with the install pointer (never a
bare ``ImportError``, never a fake success; the linux face is pure
stdlib, zero extras); a directly instantiated adapter on an unverified
platform refuses the same way (defense in depth behind the composition
gate).

Deviations/disclosures (no TS anchor exists for this card -- no TS
fingerprint is claimed): the win32 surfaced exit code is the INTERPRETER
layer's own code (Windows PowerShell exits 1 after an interrupted
child; pwsh would propagate the child's 130) -- the direct-child
exit-130 form is the S1 backend-level assertion, not a tool-level one;
the POSIX surfaced exit code is the waitpid face (WIFEXITED ->
WEXITSTATUS, WIFSIGNALED -> -WTERMSIG -- a negative value is the honest
signal-terminated code, e.g. -9 after the killpg sweep; bash propagates
an interrupted child's 130); the merged stream is returned RAW (ANSI
rendering included, ``^C`` echoes included); stdout and stderr are not
separable on this face (pty-merged by construction);
``timeout=0`` disables the deadline (bash.py TS-exact semantics).
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import sys
import time
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.process.interpreter import Interpreter, resolve_interpreter

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

PTY_NAMESPACE = "pty"
PTY_NAME = "run"
PTY_VERSION = 1

#: §5.3 L315 default timeout (the bash.py value, an independent constant).
PTY_DEFAULT_TIMEOUT_S = 120.0
#: §5.3 L315 truncation: merged output capped at 64 KiB + marker + original size.
PTY_MAX_OUTPUT_BYTES = 64 * 1024
#: Spawn geometry defaults (the conventional terminal size).
PTY_DEFAULT_ROWS = 24
PTY_DEFAULT_COLS = 80

#: Read chunk for the merged stream (one select-gated read pass).
PTY_READ_CHUNK = 65536
#: Loop cadence: deadline/cancel/resize checks and the select wait bound.
PTY_POLL_INTERVAL_S = 0.05
#: After the child's death, how long EOF may lag before the drain ends.
PTY_EOF_SETTLE_S = 2.0
#: How long the child has to exit on its own after the \x03 write (the
#: Ctrl-C cancellation primitive's grace before the hard sweep).
PTY_CTRL_C_GRACE_S = 10.0
#: How long the hard sweep may take to confirm the child's death.
PTY_KILL_GRACE_S = 5.0

PTY_DESCRIPTION = (
    "Execute a command inside a pseudo-terminal (PTY) and return its merged output. "
    "The command runs on the explicit shell interpreter (PowerShell on Windows, bash on POSIX) "
    "with a default timeout of 120 seconds; pass a larger timeout for long-running commands or "
    "0 to disable the timeout. stdout and stderr arrive merged into one stream with real "
    "terminal rendering. Cancelling the call sends Ctrl-C to the running command and terminates "
    "its process tree. Output above the limit is truncated. On Windows requires the optional "
    "'pty' extras (pywinpty)."
)

PTY_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Run one command in a pseudo-terminal (PTY) and return its pty-merged output.",
    "properties": {
        "command": {
            "type": "string",
            "description": "Command string to run on the explicit shell interpreter "
            "(PowerShell on Windows, bash on POSIX) inside the pseudo-terminal",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory to execute the command in (defaults to the session working directory)",
        },
        "timeout": {
            "type": "integer",
            "minimum": 0,
            "description": "Timeout in seconds; 0 disables the timeout (default 120)",
        },
        "rows": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32767,
            "description": "Initial pseudo-terminal rows (default 24)",
        },
        "cols": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32767,
            "description": "Initial pseudo-terminal columns (default 80)",
        },
        "input": {
            "type": "string",
            "description": "Text written to the PTY input once after spawn; end lines with \\r (the console Enter)",
        },
        "resize": {
            "type": "object",
            "description": "Mid-run pseudo-terminal size change: after after_seconds of runtime, resize to rows x cols",
            "properties": {
                "after_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Seconds into the run before the resize is applied",
                },
                "rows": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32767,
                    "description": "New pseudo-terminal rows",
                },
                "cols": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32767,
                    "description": "New pseudo-terminal columns",
                },
            },
            "required": ["after_seconds", "rows", "cols"],
            "additionalProperties": False,
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

PTY_SPEC = ToolSpec(
    namespace=PTY_NAMESPACE,
    name=PTY_NAME,
    version=PTY_VERSION,
    description=PTY_DESCRIPTION,
    input_schema=PTY_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    idempotency_support=False,
)


# ---------------------------------------------------------------------------
# Capability negotiation (r0b-2: restricted is never a bare exception, never
# a fake success)
# ---------------------------------------------------------------------------


def _pty_problem() -> str | None:
    """The platform/extras gate: ``None`` runs; a message refuses."""
    if sys.platform == "win32":
        if find_spec("winpty") is None:
            return (
                "the pseudoconsole face requires the optional 'pty' extras "
                "(install with: pip install 'opencode-py[pty]'); the core library works without them"
            )
        return None
    if sys.platform == "linux":
        return None  # the T2 POSIX face: pure stdlib, zero extras
    return (
        "the pty__run tool is not verified on this platform and is not enabled "
        "(PHASE2-MODULES L68: an unverified platform is not enabled)"
    )


def pty_adapters() -> tuple[PtyRunAdapter, ...]:
    """The host composition gate: the adapter tuple splices into the builtin
    tools on the VERIFIED platforms only (PHASE2-MODULES L68: an unverified
    platform is not enabled) -- win32 (the T1 winpty-agent face, T-C1-072)
    and linux (the T2 stdlib face, T-C1-074); every other platform (darwin
    etc.) composes nothing. Both host faces consume this: the CLI ``_drive``
    list since T-C1-072 and the httpapi ``_builtin_adapters`` list since
    T-C1-073 (B-seat T-B-087 ruling 2; the serving census pin test carries
    the platform-conditional form -- unverified platforms twelve, win32 and
    linux thirteen appended).
    """
    if sys.platform not in ("win32", "linux"):
        return ()
    return (PtyRunAdapter(),)


# ---------------------------------------------------------------------------
# The merged-stream capped capture (the runner _CappedBuffer discipline,
# chunk-granular for the PTY stream: cap only stops appending, the byte
# count keeps tracking -- §5.3 L315 "输出截断后仍排空管道")
# ---------------------------------------------------------------------------


class _MergedCapture:
    """A byte-capped accumulator for the pty-merged stream."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._parts: list[str] = []
        self._bytes = 0
        self._truncated = False

    def append(self, chunk: str) -> None:
        encoded = chunk.encode("utf-8", "replace")
        size = len(encoded)
        self._bytes += size
        if self._truncated:
            return
        if self._bytes <= self._max_bytes:
            self._parts.append(chunk)
            return
        self._truncated = True
        # The byte-exact prefix of the overflowing chunk (a mid-character
        # cut is dropped by the "ignore" decode, runner.py form).
        remaining = self._max_bytes - (self._bytes - size)
        if remaining > 0:
            self._parts.append(encoded[:remaining].decode("utf-8", "ignore"))

    def snapshot(self) -> tuple[str, bool, int]:
        """Returns ``(text, truncated, original_bytes)``."""
        return "".join(self._parts), self._truncated, self._bytes


def _truncation_marker(shown: str, original_bytes: int) -> str:
    """§5.3 L315: mark the truncation and the original size."""
    shown_bytes = len(shown.encode("utf-8", "replace"))
    return (
        f"[pty output truncated: showing {shown_bytes} of {original_bytes} bytes (limit {PTY_MAX_OUTPUT_BYTES} bytes)]"
    )


def _pty_env() -> dict[str, str]:
    """Inherited environment minus COLUMNS/LINES (the T0 discipline: those
    would shadow the real PTY geometry). Zero TS anchor -> no AGENT/OPENCODE
    markers (git.py reasoning)."""
    env = dict(os.environ)
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    return env


def _failure(code: CoreErrorCode, message: str) -> ToolResult:
    """A graceful adapter failure: model-readable text + §8 error (bash.py form)."""
    return ToolResult(
        content=[ToolTextContent(type="text", text=message)],
        error=CoreError(code=code, message=message, retryable=False),
    )


# ---------------------------------------------------------------------------
# The blocking run loop (off-loaded to a worker thread by execute)
# ---------------------------------------------------------------------------


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against PTY_INPUT_SCHEMA
    # before execute (adapter contract, §5.1); the casts below are static
    # narrows, not conversions (bash.py form).
    command = cast(str, input["command"])
    if not command:
        return _failure(CoreErrorCode.VALIDATION_ERROR, "Command must not be empty")
    raw_cwd = input.get("cwd")
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else context.working_dir
    if not os.path.isdir(cwd):
        return _failure(CoreErrorCode.TOOL_FAILED, f"Working directory does not exist: {cwd}")
    timeout_s = cast("int | float", input.get("timeout", PTY_DEFAULT_TIMEOUT_S))
    rows = cast("int", input.get("rows", PTY_DEFAULT_ROWS))
    cols = cast("int", input.get("cols", PTY_DEFAULT_COLS))
    raw_input_text = input.get("input")
    input_text = raw_input_text if isinstance(raw_input_text, str) else None
    raw_resize = input.get("resize")
    resize = cast("dict[str, Any]", raw_resize) if isinstance(raw_resize, dict) else None

    problem = _pty_problem()
    if problem is not None:
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, problem)

    interpreter, iproblem = resolve_interpreter()
    if interpreter is None:
        assert iproblem is not None  # the discovery always states a problem on failure
        return _failure(
            CoreErrorCode.TOOL_FAILED,
            f"pty__run requires an explicit shell interpreter, but {iproblem}. "
            "Install the platform shell (PowerShell on Windows, bash on POSIX) and retry; "
            "no silent fallback interpreter is used (CORE-CONTRACTS §5.3 L315).",
        )

    # The two verified platform legs (the gate above refused everything
    # else): win32 = the T1 winpty-agent face, linux = the T2 stdlib face.
    # The winpty import lives INSIDE the win32 leg -- pywinpty carries a
    # win32 extras marker, so a bare import would die at runtime on linux.
    if sys.platform == "win32":
        return _win32_execute(context, command, cwd, timeout_s, rows, cols, input_text, resize, interpreter)
    return _posix_execute(context, command, cwd, timeout_s, rows, cols, input_text, resize, interpreter)


# ---------------------------------------------------------------------------
# The win32 execution face (T1, T-C1-072: the winpty agent + Job Object)
# ---------------------------------------------------------------------------


def _win32_execute(
    context: ToolContext,
    command: str,
    cwd: str,
    timeout_s: float,
    rows: int,
    cols: int,
    input_text: str | None,
    resize: dict[str, Any] | None,
    interpreter: Interpreter,
) -> ToolResult:
    """The T1 Windows run loop (winpty agent + Job Object, T-C1-072)."""
    # Bare ignores: on win32 winpty is installed but untyped ([import-untyped]);
    # the posix mypy leg has it absent entirely ([import-not-found]).
    from winpty.enums import Backend  # type: ignore
    from winpty.ptyprocess import PtyProcess  # type: ignore

    sink = _MergedCapture(PTY_MAX_OUTPUT_BYTES)

    def drain(readable: bool) -> bool:
        """One read pass; returns True on EOF. EOFError is the pty terminal."""
        if not readable:
            return False
        try:
            chunk = cast("str", PtyProcess.read(proc, PTY_READ_CHUNK))
        except EOFError:
            return True
        if chunk:
            sink.append(chunk)
        return False

    proc: object | None = None
    job: object | None = None
    timed_out = False
    cancelled = False
    reaped = True
    exit_code: int | None = None
    try:
        proc = PtyProcess.spawn(
            interpreter.argv(command),
            cwd=cwd,
            env=_pty_env(),
            dimensions=(rows, cols),
            backend=Backend.WinPTY,
        )

        # Tree capture BEFORE any input: gate-driven grandchildren always
        # land inside the job (T0 assign-before-proceed-gate). A failed
        # capture degrades to the pty's own child termination (runner.py
        # disclosed fallback -- the tree guarantee degrades, the spawn lives).
        from opencode.process.winjob import JobObject

        try:
            job = JobObject()
            cast("Any", job).capture(cast("Any", proc).pid)
        except OSError:
            if job is not None:
                try:
                    cast("Any", job).close()
                except OSError:
                    pass
            job = None

        if input_text is not None:
            try:
                cast("Any", proc).write(input_text)
            except EOFError:
                pass  # the child died before the write; the loop's terminal handles it

        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        resize_at = time.monotonic() + float(cast("float", resize["after_seconds"])) if resize is not None else None
        resize_done = resize_at is None
        dead_since: float | None = None

        while True:
            if context.cancel.is_set():
                cancelled = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            if resize is not None and not resize_done and time.monotonic() >= cast("float", resize_at):
                cast("Any", proc).setwinsize(int(cast("int", resize["rows"])), int(cast("int", resize["cols"])))
                resize_done = True
            readable, _, _ = select.select([cast("Any", proc).fileobj], [], [], PTY_POLL_INTERVAL_S)
            if drain(bool(readable)):
                break  # EOF: the merged stream reached its terminal
            if not cast("Any", proc).isalive():
                dead_since = dead_since if dead_since is not None else time.monotonic()
                if time.monotonic() - dead_since >= PTY_EOF_SETTLE_S:
                    break  # dead and settled: EOF lagged past the backstop

        if cancelled:
            # The Ctrl-C cancellation primitive: write \x03, then give the
            # child a bounded grace to exit on its own (the interrupt is
            # OBSERVED in the merged stream this grace keeps draining --
            # never "sent and assumed"); the hard sweep below follows
            # regardless, so the tree never outlives the call.
            try:
                cast("Any", proc).write("\x03")
            except (EOFError, OSError):
                pass  # already gone; the sweep below handles the tree
            grace_deadline = time.monotonic() + PTY_CTRL_C_GRACE_S
            while cast("Any", proc).isalive() and time.monotonic() < grace_deadline:
                readable, _, _ = select.select([cast("Any", proc).fileobj], [], [], PTY_POLL_INTERVAL_S)
                if drain(bool(readable)):
                    break

        # The single-call sweep: on EVERY exit path the tree is terminated
        # (a normal exit still reaps orphaned descendants -- no background
        # face). The job is the atomic tree kill; the degraded face falls
        # back to the pty's own child termination.
        if job is not None:
            try:
                cast("Any", job).terminate()
            except OSError:
                reaped = False  # §5.3: cleanup could not be confirmed
        else:
            try:
                cast("Any", proc).close(force=True)
            except Exception:
                reaped = False
        if reaped:
            death_deadline = time.monotonic() + PTY_KILL_GRACE_S
            while cast("Any", proc).isalive() and time.monotonic() < death_deadline:
                time.sleep(PTY_POLL_INTERVAL_S)
            if cast("Any", proc).isalive():
                reaped = False
            else:
                exit_code = cast("int | None", cast("Any", proc).exitstatus)
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to execute command: {command}: {error}")
    finally:
        if job is not None:
            try:
                cast("Any", job).close()
            except OSError:
                pass
        if proc is not None:
            try:
                cast("Any", proc).close(force=True)
            except Exception:
                pass

    merged, truncated, original_bytes = sink.snapshot()

    # bash.py render faces on the merged stream.
    texts: list[str] = []
    if merged:
        texts.append(merged)
    if truncated:
        texts.append(_truncation_marker(merged, original_bytes))
    if timed_out:
        texts.append("Timed out before completion")
    elif exit_code is not None and exit_code != 0:
        texts.append(f"Exited with code {exit_code}")
    if not reaped:
        texts.append("Process cleanup could not be confirmed")
    content: list[ToolTextContent | ToolFileContent] = [ToolTextContent(type="text", text=text) for text in texts]

    structured: dict[str, JsonValue] = {
        "exit": exit_code,
        "truncated": truncated,
        "timeout": timed_out,
    }

    if timed_out:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.TOOL_TIMEOUT,
                message=(
                    f"Command exceeded timeout of {timeout_s:.0f} seconds. Retry with a larger "
                    "timeout if the command is expected to take longer."
                ),
                retryable=True,
            ),
        )
    if cancelled:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message=(
                    "Command cancelled while running: the process tree was terminated "
                    "and the final state could not be confirmed"
                ),
                retryable=True,
            ),
        )
    if not reaped:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message="Command finished but its process cleanup could not be confirmed",
                retryable=False,
            ),
        )
    # A completed command (any exit code) succeeded as a tool run: the exit
    # code is data (notice + structured_output), not a tool failure.
    return ToolResult(content=content, structured_output=structured)


# ---------------------------------------------------------------------------
# The POSIX execution face (T2, T-C1-074: forkpty + execve of the explicit
# interpreter, stdlib-only; the probe per-function win32-early-guard form)
# ---------------------------------------------------------------------------


def _posix_spawn(interpreter: Interpreter, command: str, cwd: str, rows: int, cols: int) -> tuple[int, int]:
    """``os.forkpty`` + ``os.execve`` of the explicit interpreter (the T0
    probe ``_posix_spawn`` form: never ``shell=True``).

    Returns ``(child_pid, master_fd)``. The forkpty child is a session
    leader on a fresh pty, so its process group id equals its pid -- the
    killpg coverage guarantee. The initial winsize is set on the master
    before the interpreter can boot far enough to read geometry. The
    win32 guard marks the POSIX-only body unreachable for mypy on
    Windows; this helper never runs there (the ``_execute`` dispatch
    selects the win32 leg first).
    """
    if sys.platform == "win32":
        raise OSError("os.forkpty is a POSIX primitive")
    pid, master_fd = os.forkpty()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execve(interpreter.executable, interpreter.argv(command), _pty_env())
        finally:
            os._exit(127)
    _posix_set_winsize(master_fd, rows, cols)
    return pid, master_fd


def _posix_set_winsize(master_fd: int, rows: int, cols: int) -> None:
    """``TIOCSWINSZ`` on the pty master (drives the child's SIGWINCH).

    The win32 early-return plus function-level ``fcntl``/``termios``
    imports are the probe mypy pattern: those modules do not exist on
    Windows, so the POSIX-only body stays unreachable for mypy there
    (the dual-platform green form, probe 167-0 win32 / 163-0 linux).
    """
    if sys.platform == "win32":
        return
    import fcntl
    import struct
    import termios

    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _posix_exit_code(pid: int) -> int | None:
    """``waitpid(WNOHANG)``: the exit code once the child is reaped
    (negative when a signal terminated it), ``None`` while it runs."""
    if sys.platform == "win32":
        return None
    try:
        waited, status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return None
    if waited != pid:
        return None
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return None


def _posix_kill_process_group(pid: int) -> None:
    """SIGKILL the whole process group (the forkpty child IS the group
    leader, so pgid == pid -- the T0-verified tree reclamation form)."""
    if sys.platform == "win32":
        return
    os.killpg(pid, signal.SIGKILL)


def _posix_execute(
    context: ToolContext,
    command: str,
    cwd: str,
    timeout_s: float,
    rows: int,
    cols: int,
    input_text: str | None,
    resize: dict[str, Any] | None,
    interpreter: Interpreter,
) -> ToolResult:
    """The T2 POSIX run loop: the T1 shared shape on the forkpty primitives.

    One select-gated read pass per loop cadence through the incremental
    UTF-8 decoder; ``OSError`` (EIO) from the master read is the EOF face
    (the T1 ``EOFError`` counterpart). The Ctrl-C primitive is
    observe-style: the ``\\x03`` write is followed by a draining grace
    window with NO marker matcher (the #198 four-round ruling -- the
    ECHOCTL ``^C`` echo is part of what a terminal user sees; the raw
    stream is returned verbatim). The killpg sweep runs on EVERY exit
    path, and the surfaced exit code is the waitpid face (negative when
    signal-terminated).
    """
    import codecs

    sink = _MergedCapture(PTY_MAX_OUTPUT_BYTES)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    pid = -1
    master_fd = -1

    def drain(readable: bool) -> bool:
        """One read pass; True on EOF (EIO after the last slave closes)."""
        if not readable:
            return False
        try:
            data = os.read(master_fd, PTY_READ_CHUNK)
        except OSError:
            return True  # EIO: the pty terminal, the T1 EOFError counterpart
        if data:
            sink.append(decoder.decode(data))
        return False

    timed_out = False
    cancelled = False
    reaped = True
    exit_code: int | None = None
    try:
        pid, master_fd = _posix_spawn(interpreter, command, cwd, rows, cols)

        if input_text is not None:
            try:
                os.write(master_fd, input_text.encode("utf-8"))
            except OSError:
                pass  # the child died before the write; the loop's terminal handles it

        deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
        resize_at = time.monotonic() + float(cast("float", resize["after_seconds"])) if resize is not None else None
        resize_done = resize_at is None
        dead_since: float | None = None

        while True:
            if context.cancel.is_set():
                cancelled = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            if resize is not None and not resize_done and time.monotonic() >= cast("float", resize_at):
                _posix_set_winsize(master_fd, int(cast("int", resize["rows"])), int(cast("int", resize["cols"])))
                resize_done = True
            readable, _, _ = select.select([master_fd], [], [], PTY_POLL_INTERVAL_S)
            if drain(bool(readable)):
                break  # EOF: the merged stream reached its terminal
            if exit_code is None:
                exit_code = _posix_exit_code(pid)
            if exit_code is not None:
                dead_since = dead_since if dead_since is not None else time.monotonic()
                if time.monotonic() - dead_since >= PTY_EOF_SETTLE_S:
                    # Dead and settled: EOF lagged past the backstop (a live
                    # descendant still holds the slave -- the sweep covers it).
                    break

        if cancelled:
            # The Ctrl-C cancellation primitive, observe-style: write \x03,
            # then give the child a bounded grace to exit on its own while
            # this window keeps draining the merged stream (the raw flow IS
            # the observation -- the tool never pattern-matches interrupt
            # markers); the hard sweep below follows regardless, so the
            # group never outlives the call.
            try:
                os.write(master_fd, b"\x03")
            except OSError:
                pass  # already gone; the sweep below handles the group
            grace_deadline = time.monotonic() + PTY_CTRL_C_GRACE_S
            while exit_code is None and time.monotonic() < grace_deadline:
                readable, _, _ = select.select([master_fd], [], [], PTY_POLL_INTERVAL_S)
                if drain(bool(readable)):
                    break
                exit_code = _posix_exit_code(pid)

        # The single-call sweep: on EVERY exit path the process group is
        # SIGKILLed (a normal exit still reaps orphaned descendants that
        # hold the slave open -- no background face). The forkpty child is
        # the group leader, so pgid == pid; an empty group
        # (ProcessLookupError) is already reaped, anything else unconfirmed
        # degrades to reaped=False (effect_status=unknown).
        try:
            _posix_kill_process_group(pid)
        except ProcessLookupError:
            pass  # the group is already gone
        except OSError:
            reaped = False  # §5.3: cleanup could not be confirmed
        if reaped and exit_code is None:
            # Confirm the SIGKILLed child's death within the kill grace and
            # surface its waitpid code (negative when signal-terminated).
            death_deadline = time.monotonic() + PTY_KILL_GRACE_S
            while time.monotonic() < death_deadline:
                code = _posix_exit_code(pid)
                if code is not None:
                    exit_code = code
                    break
                time.sleep(PTY_POLL_INTERVAL_S)
            if exit_code is None:
                reaped = False
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to execute command: {command}: {error}")
    finally:
        if master_fd >= 0:
            try:
                os.close(master_fd)  # SIGHUP to anything the sweep missed
            except OSError:
                pass

    merged, truncated, original_bytes = sink.snapshot()

    # bash.py render faces on the merged stream (the T1 shared shape).
    texts: list[str] = []
    if merged:
        texts.append(merged)
    if truncated:
        texts.append(_truncation_marker(merged, original_bytes))
    if timed_out:
        texts.append("Timed out before completion")
    elif exit_code is not None and exit_code != 0:
        texts.append(f"Exited with code {exit_code}")
    if not reaped:
        texts.append("Process cleanup could not be confirmed")
    content: list[ToolTextContent | ToolFileContent] = [ToolTextContent(type="text", text=text) for text in texts]

    structured: dict[str, JsonValue] = {
        "exit": exit_code,
        "truncated": truncated,
        "timeout": timed_out,
    }

    if timed_out:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.TOOL_TIMEOUT,
                message=(
                    f"Command exceeded timeout of {timeout_s:.0f} seconds. Retry with a larger "
                    "timeout if the command is expected to take longer."
                ),
                retryable=True,
            ),
        )
    if cancelled:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message=(
                    "Command cancelled while running: the process tree was terminated "
                    "and the final state could not be confirmed"
                ),
                retryable=True,
            ),
        )
    if not reaped:
        return ToolResult(
            content=content,
            structured_output=structured,
            error=CoreError(
                code=CoreErrorCode.EXECUTION_UNKNOWN,
                message="Command finished but its process cleanup could not be confirmed",
                retryable=False,
            ),
        )
    # A completed command (any exit code, including the negative
    # signal-terminated form, e.g. -9 after the killpg sweep) succeeded as
    # a tool run: the exit code is data (notice + structured_output), not
    # a tool failure.
    return ToolResult(content=content, structured_output=structured)


class PtyRunAdapter:
    """The ``pty__run`` builtin (§5.1 face: ``spec`` + ``execute``).

    ``execute`` off-loads the blocking PTY loop to a worker thread so a
    long command never blocks the event loop (bash.py form); the
    cooperative cancel signal is polled from that thread on every loop
    pass and delivers the Ctrl-C primitive.
    """

    @property
    def spec(self) -> ToolSpec:
        return PTY_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await asyncio.to_thread(_execute, context, input)
