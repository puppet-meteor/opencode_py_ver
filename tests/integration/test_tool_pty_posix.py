"""T-C1-074 integration tests: the ``pty__run`` POSIX execution face (PTY T2).

The stdlib forkpty face of the ``pty__run`` tool: one explicit-interpreter
command (bash) under an ``os.forkpty`` pseudo-terminal, carrying the §5.3
L315 discipline over the same shared loop shape as the T1 Windows face --
120 s default timeout, output capped at 64 KiB with an explicit truncation
marker + original size while the PTY keeps draining past the cap, cancel =
the Ctrl-C primitive (an observe-style ``\\x03`` write to the master with a
draining grace window and NO marker matcher -- the #198 four-round ruling:
the ECHOCTL ``^C`` echo is part of what a terminal user sees, and the raw
stream is returned verbatim), and a ``killpg(pid, SIGKILL)`` sweep on every
exit path (the forkpty child is the session and group leader, so the whole
group -- gate-driven grandchildren included -- is covered; a cleanup that
cannot be confirmed surfaces as an unknown effect, never a fake success).

Platform honesty (r0b-2 / PHASE2-MODULES L68): the POSIX face is verified
on linux only -- CI ubuntu is the authoritative POSIX verification face
(the #197/#198 environment fact; a Windows seat never impersonates a POSIX
result). The whole module skips off linux, so the runtime correctness here
is written-to-green and merge-gated on the CI leg (the disclosed TDD form,
the #197 T0 probe-test precedent). The Windows face is the independent T1
ticket (p2-slices L170: T1/T2 platform gates are independent).

Exit-code face: the surfaced code is the waitpid face (WIFEXITED ->
WEXITSTATUS, WIFSIGNALED -> -WTERMSIG); bash propagates an interrupted
child's 130, and a sweep-terminated run surfaces the honest negative
signal code (e.g. -9). Timing assertions use generous windows only.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.process.interpreter import resolve_interpreter
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import PtyRunAdapter

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "T2 platform gate: the POSIX PTY face is verified on linux only (CI ubuntu is the "
        "authoritative POSIX verification face, #197/#198); the Windows leg is the independent "
        "T1 ticket (p2-slices L170: T1/T2 platform gates are independent)"
    ),
)

_SESSION_ID = "ses_itptx"
_RUN_ID = "run_itptx"
_MESSAGE_ID = "msg_itptx"
_TOOL_CALL_ID = "call_itptx"
_EXEC_ID = "exec_itptx"
_FIXED_TIME = datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC)

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ENCODING_PAYLOAD = "日本語 πλθ café naïve ♥"


def _normalize(text: str) -> str:
    """Strip the ANSI rendering and unify line endings before assertions."""
    return _CSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _interpreter_or_skip() -> None:
    interpreter, _problem = resolve_interpreter()
    if interpreter is None:
        pytest.skip("no explicit shell interpreter (bash) available on this platform")


# ---------------------------------------------------------------------------
# process-level probes (the /proc form: a zombie holds no resources -- the
# killpg coverage claim is about LIVE processes)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _wait_pid_file(path: Path, *, timeout_s: float = 20.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"pid file {path} never appeared (timeout {timeout_s}s)")


def _assert_pids_gone(pids: tuple[int, ...], *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            time.sleep(0.05)
    assert not remaining, f"processes survived the group kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# direct-registry harness (test_tool_pty.py form; the App tests own approvals)
# ---------------------------------------------------------------------------


def _clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []

    async def __call__(self, drafts: tuple[EventDraft[BaseModel], ...]) -> None:
        self.batches.append(drafts)


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast("Any", _FixedIds()))
    registry.register(PtyRunAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


async def _run(tmp_path: Path, input: JsonObject) -> ToolOutcome:
    return await _registry().invoke(_context(tmp_path), "pty__run", input, _EXEC_ID)


def _texts(outcome: ToolOutcome) -> list[str]:
    return [part.text for part in outcome.content if isinstance(part, ToolTextContent)]


def _structured(outcome: ToolOutcome) -> dict[str, Any]:
    assert outcome.structured_output is not None
    return cast("dict[str, Any]", outcome.structured_output)


# ---------------------------------------------------------------------------
# child helpers (explicit-interpreter argv through the resolved bash)
# ---------------------------------------------------------------------------


def _bash_command(script: Path, *args: Path | str) -> str:
    """The bash command string running one explicit-interpreter child."""
    quoted = " ".join(f"'{arg}'" for arg in args)
    suffix = f" {quoted}" if quoted else ""
    return f"'{sys.executable}' -u '{script}'{suffix}"


def _write_echo_child(directory: Path) -> Path:
    script = directory / "echo_child.py"
    script.write_text(
        "import sys\n"
        f"print('PYPROBE_ENC:{_ENCODING_PAYLOAD}', flush=True)\n"
        "print(f'ISTTY:{sys.stdout.isatty()}', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "sys.stdout.write('ECHO:' + line)\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return script


def _write_ctrl_c_child(directory: Path, pid_file: Path) -> Path:
    script = directory / "ctrl_c_child.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "if hasattr(signal, 'pthread_sigmask'):\n"
        "    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGINT])\n"
        "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
        f"with open(r'{pid_file}', 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "print('READY', flush=True)\n"
        "try:\n"
        "    time.sleep(120)\n"
        "except KeyboardInterrupt:\n"
        "    print('PYPROBE_INTERRUPTED', flush=True)\n"
        "    sys.exit(130)\n",
        encoding="utf-8",
    )
    return script


def _write_tree_child(directory: Path) -> Path:
    """Parent mode: write pid, wait for the GO gate, spawn the grandchild, sleep.

    Child mode: write pid, announce, sleep. The gate makes grandchild creation
    strictly observable before the timeout sweep (the T0 proceed-gate form).
    """
    script = directory / "tree_child.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "mode = sys.argv[2]\n"
        "if mode == 'parent':\n"
        "    with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "        handle.write(str(os.getpid()))\n"
        "    print('READY', flush=True)\n"
        "    gate = sys.stdin.readline()\n"
        "    if gate.strip() != 'GO':\n"
        "        sys.exit(2)\n"
        "    subprocess.Popen([sys.executable, '-u', sys.argv[0], sys.argv[3], 'child'])\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "        handle.write(str(os.getpid()))\n"
        "    print('GRANDCHILD_UP', flush=True)\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


def _write_resize_child(directory: Path) -> Path:
    """The child observes the live geometry and exits when the target lands.

    Every pass checks the CURRENT size (not only changes): if the tool's
    resize lands before the interpreter chain finishes booting, the child
    still observes the target geometry as its first reading (no
    tight-timing assumption, the CI-slow-runner compatibility form).
    """
    script = directory / "resize_child.py"
    script.write_text(
        "import os, sys, time\n"
        "last = os.get_terminal_size(1)\n"
        "print(f'SIZE {last.columns} {last.lines}', flush=True)\n"
        "end = time.monotonic() + 60\n"
        "while time.monotonic() < end:\n"
        "    cur = os.get_terminal_size(1)\n"
        "    if cur != last:\n"
        "        last = cur\n"
        "        print(f'SIZE {cur.columns} {cur.lines}', flush=True)\n"
        "    if (cur.columns, cur.lines) == (120, 50):\n"
        "        print('RESIZED_OBSERVED', flush=True)\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.05)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    return script


# ---------------------------------------------------------------------------
# 1. spawn + merged capture + encoding: a pty round trip through forkpty
# ---------------------------------------------------------------------------


async def test_pty_posix_merged_capture_utf8_roundtrip(tmp_path: Path) -> None:
    _interpreter_or_skip()
    script = _write_echo_child(tmp_path)
    outcome = await _run(
        tmp_path,
        {"command": _bash_command(script), "timeout": 30, "input": _ENCODING_PAYLOAD + "\r"},
    )

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    normalized = _normalize("\n".join(_texts(outcome)))
    # The child ran ON a pseudo-terminal: its stdout was a tty (the forkpty
    # face, not a pipe), and both sides of the UTF-8 round trip arrived
    # intact (banner through the incremental decoder, echo through the
    # master input write) -- no mojibake anywhere.
    assert "ISTTY:True" in normalized
    assert f"PYPROBE_ENC:{_ENCODING_PAYLOAD}" in normalized
    assert f"ECHO:{_ENCODING_PAYLOAD}" in normalized
    assert _structured(outcome)["exit"] == 0


# ---------------------------------------------------------------------------
# 2. resize primitive: a scheduled mid-run TIOCSWINSZ, observed by the child
# ---------------------------------------------------------------------------


async def test_pty_posix_midrun_resize_observable(tmp_path: Path) -> None:
    _interpreter_or_skip()
    script = _write_resize_child(tmp_path)
    outcome = await _run(
        tmp_path,
        {
            "command": _bash_command(script),
            "timeout": 30,
            "resize": {"after_seconds": 2.5, "rows": 50, "cols": 120},
        },
    )

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    normalized = _normalize("\n".join(_texts(outcome)))
    # The spawn geometry was 24x80; a 120x50 observation proves the tool-side
    # TIOCSWINSZ resize reached the running child's terminal.
    assert "120 50" in normalized
    assert "RESIZED_OBSERVED" in normalized
    assert _structured(outcome)["exit"] == 0


# ---------------------------------------------------------------------------
# 3. Ctrl-C cancellation primitive: observable interrupt + group reclamation
# ---------------------------------------------------------------------------


async def test_pty_posix_cancel_ctrl_c_observable_and_group_reaped(tmp_path: Path) -> None:
    _interpreter_or_skip()
    pid_file = tmp_path / "gen1.pid"
    script = _write_ctrl_c_child(tmp_path, pid_file)
    context = _context(tmp_path)
    registry = _registry()
    task = asyncio.create_task(
        registry.invoke(context, "pty__run", {"command": _bash_command(script), "timeout": 0}, _EXEC_ID)
    )
    gen1 = await asyncio.to_thread(_wait_pid_file, pid_file)

    context.cancel.set()  # the cooperative signal, mid-execution
    outcome = await task

    # The honest unknown terminal: the group was terminated, the final
    # state is unknown (a killed run's effects are not provable).
    assert (outcome.state, outcome.effect_status) == ("failed", "unknown")
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.EXECUTION_UNKNOWN
    joined = "\n".join(_texts(outcome))
    # The observation face: the child's own interrupt marker reached the
    # merged stream (never "sent and assumed"), and the ECHOCTL "^C" echo
    # of the written INTR byte is in the RAW stream verbatim (the #198
    # fact -- part of what a terminal user sees; the tool matches nothing).
    assert "PYPROBE_INTERRUPTED" in _normalize(joined)
    assert "^C" in joined
    # bash propagates the interrupted child's 130; the tool surfaces the
    # waitpid exit-code face verbatim.
    assert _structured(outcome)["exit"] == 130
    _assert_pids_gone((gen1,))


# ---------------------------------------------------------------------------
# 4. process_tree_exit: a timeout killpg-sweeps the group, two generations
# ---------------------------------------------------------------------------


async def test_pty_posix_timeout_kills_group_with_grandchild(tmp_path: Path) -> None:
    _interpreter_or_skip()
    script = _write_tree_child(tmp_path)
    gen1_pid_file = tmp_path / "gen1.pid"
    gen2_pid_file = tmp_path / "gen2.pid"

    outcome = await _run(
        tmp_path,
        {
            "command": _bash_command(script, gen1_pid_file, "parent", gen2_pid_file),
            "input": "GO\r",  # the proceed gate: the grandchild exists before the sweep
            "timeout": 8,
        },
    )

    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_TIMEOUT
    assert "Timed out before completion" in _normalize("\n".join(_texts(outcome)))
    # The sweep-terminated interpreter surfaces the honest negative signal
    # code (SIGKILL = 9 -> -9, the WIFSIGNALED face).
    assert _structured(outcome)["exit"] == -9

    # Process-level group reclamation evidence: both generations gone (the
    # forkpty session group covered the child AND the grandchild).
    gen1 = _wait_pid_file(gen1_pid_file)
    gen2 = _wait_pid_file(gen2_pid_file)
    assert gen1 != gen2
    _assert_pids_gone((gen1, gen2))
