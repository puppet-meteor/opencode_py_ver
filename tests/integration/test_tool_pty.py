"""T-C1-072 integration tests: the ``pty__run`` builtin (PTY T1 Windows).

The single-call pseudoconsole execution tool: one explicit-interpreter
command under a winpty-agent pseudoconsole (the S1 backend decision,
T-B-086 ruling option (a) -- all four primitives verified on that
backend in ``test_pty_s1_winpty_backend.py``), with the §5.3 L315
discipline carried over the PTY face: 120 s default timeout, output
capped at 64 KiB with an explicit truncation marker + original size
while the PTY keeps draining past the cap, cancel = the Ctrl-C
primitive (``\x03`` written to the PTY input, the interrupt observed in
the merged stream) followed by the Job Object tree reclamation, and a
cleanup that cannot be confirmed surfacing as an unknown effect --
never a fake success.

Test plan (one red/green per primitive, plus the registration faces):

1. pty-merged capture: a plain command's output arrives from the
   pseudoconsole renderer (the raw stream carries the ANSI face a real
   terminal would render; stdout/stderr are merged by construction).
2. exit codes are data (bash.py semantics: any completed exit code
   succeeds as a tool run with a notice).
3. Ctrl-C cancellation primitive: a mid-run cancel writes ``\x03``, the
   child's interrupt marker is OBSERVED in the merged output (T0 form,
   never "sent and assumed"), the interrupted tree is terminated and
   reaped, and the outcome keeps the honest unknown effect terminal.
4. resize primitive: a scheduled mid-run size change is observed by the
   running child (``SIZE <cols> <rows>`` lines from
   ``os.get_terminal_size(1)``, the T0 assertion shape).
5. encoding primitive: a multi-script UTF-8 payload round-trips both
   directions through the PTY with no mojibake.
6. process_tree_exit: a timeout kills the Job-Object-captured tree --
   the PTY child AND a grandchild released by a post-capture PTY-input
   proceed gate (the T0 assign-before-gate discipline preserved by the
   tool's own spawn → capture → input ordering), both generations
   verified gone through process-level probes.
7. output flood: capped at the limit with marker + original size, and
   the original byte count keeps growing past the cap (the PTY drain
   never stops on truncation, L315).
8. registration: the §5.2 default table carries ``pty__run`` = ask
   (git-trio closure shape), the platform gate composes the adapter on
   win32 only (PHASE2-MODULES L68: an unverified platform is not
   enabled; the POSIX face is the independent T2 ticket, zero POSIX
   implementation here), and both capability-refusal faces (missing
   extras, non-win32 direct call) return UnsupportedCapability (r0b-2:
   restricted is never a bare exception and never a fake success).

④ disclosures: the exit code surfaced by the tool is the INTERPRETER
layer's own code (PowerShell exits 1 on this host after an interrupted
child; pwsh would propagate 130) -- the direct-child exit-130 form is
the S1 backend-level assertion, not a tool-level one; the tool returns
the RAW merged stream (ANSI rendering included, exactly what a terminal
user sees); timing assertions use generous windows only.
"""

from __future__ import annotations

import asyncio
import ctypes
import re
import sys
import time
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.process.interpreter import resolve_interpreter
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import PTY_DEFAULT_TIMEOUT_S, PtyRunAdapter, pty_adapters
from opencode.tools.builtin.bash import BASH_DEFAULT_TIMEOUT_S
from opencode.tools.policy import _DEFAULT_EFFECTS

if _PLATFORM == "win32":
    from ctypes import wintypes

_SESSION_ID = "ses_itpty"
_RUN_ID = "run_itpty"
_MESSAGE_ID = "msg_itpty"
_TOOL_CALL_ID = "call_itpty"
_EXEC_ID = "exec_itpty"
_FIXED_TIME = datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC)
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ENCODING_PAYLOAD = "日本語 πλθ café naïve ♥"


def _normalize(text: str) -> str:
    """Strip the ANSI rendering and unify line endings before assertions."""
    return _CSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _winpty_or_skip() -> None:
    if sys.platform != "win32":
        pytest.skip(
            "T1 platform gate: the Windows PTY face is verified on win32 only; "
            "the POSIX leg is the independent T2 ticket (p2-slices L170)"
        )
    if find_spec("winpty") is None:
        pytest.skip("restricted: pywinpty extras not installed (pip install 'opencode-py[pty]')")


def _interpreter_or_skip() -> None:
    interpreter, _problem = resolve_interpreter()
    if interpreter is None:
        pytest.skip("no explicit shell interpreter (PowerShell/bash) available on this platform")


# ---------------------------------------------------------------------------
# process-level probes (test_tool_bash.py form, OP-R3-9 evidence)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    assert not remaining, f"processes survived the tree kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# direct-registry harness (test_tool_bash.py form; the App tests own approvals)
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
# child helpers (explicit-interpreter argv through the resolved shell)
# ---------------------------------------------------------------------------


def _ps_command(script: Path, *args: Path | str) -> str:
    """The PowerShell command string running one explicit-interpreter child."""
    quoted = " ".join(f"'{arg}'" for arg in args)
    suffix = f" {quoted}" if quoted else ""
    return f"& '{sys.executable}' -u '{script}'{suffix}"


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
    strictly follow the tool's job capture (T0 assign-before-proceed-gate).
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
    still observes the target geometry as its first reading -- the
    observation is robust to boot timing either way (no tight-timing
    assumption, the CI-slow-runner compatibility form).
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


def _write_encoding_child(directory: Path) -> Path:
    script = directory / "encoding_child.py"
    script.write_text(
        "import sys\n"
        f"print('PYPROBE_ENC:{_ENCODING_PAYLOAD}', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "sys.stdout.write('ECHO:' + line)\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return script


def _write_flood_child(directory: Path) -> Path:
    script = directory / "flood_child.py"
    script.write_text(
        "for _ in range(1000):\n    print('y' * 100, flush=True)\n",
        encoding="utf-8",
    )
    return script


# ---------------------------------------------------------------------------
# 1. pty-merged capture through the pseudoconsole renderer
# ---------------------------------------------------------------------------


async def test_pty_runs_command_with_pty_merged_capture(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": "Write-Output 'hello from pty'"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    joined = "\n".join(_texts(outcome))
    assert "hello from pty" in _normalize(joined)
    # The stream came through the pseudoconsole renderer: a real terminal's
    # ANSI face is present in the raw merged capture.
    assert "\x1b[" in joined
    assert _structured(outcome)["exit"] == 0


# ---------------------------------------------------------------------------
# 2. exit codes are data (bash.py semantics)
# ---------------------------------------------------------------------------


async def test_pty_nonzero_exit_code_is_data_not_tool_failure(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": "exit 7"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    joined = "\n".join(_texts(outcome))
    assert "Exited with code 7" in _normalize(joined)
    assert _structured(outcome)["exit"] == 7


# ---------------------------------------------------------------------------
# 3. Ctrl-C cancellation primitive: observable interrupt + tree reclamation
# ---------------------------------------------------------------------------


async def test_pty_cancel_ctrl_c_observable_interrupt_and_tree_reaped(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    pid_file = tmp_path / "gen1.pid"
    script = _write_ctrl_c_child(tmp_path, pid_file)
    context = _context(tmp_path)
    registry = _registry()
    task = asyncio.create_task(
        registry.invoke(context, "pty__run", {"command": _ps_command(script), "timeout": 0}, _EXEC_ID)
    )
    gen1 = await asyncio.to_thread(_wait_pid_file, pid_file)

    context.cancel.set()  # the cooperative signal, mid-execution
    outcome = await task

    # The honest unknown terminal (bash.py §2.1 L35 form): the tree was
    # terminated, the final state is unknown.
    assert (outcome.state, outcome.effect_status) == ("failed", "unknown")
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.EXECUTION_UNKNOWN
    # The Ctrl-C primitive is OBSERVED, never "sent and assumed": the child's
    # KeyboardInterrupt marker reached the merged stream.
    assert "PYPROBE_INTERRUPTED" in _normalize("\n".join(_texts(outcome)))
    # The interpreter layer's own exit code is surfaced (not the direct
    # child's 130 -- that form is the S1 backend-level assertion).
    assert _structured(outcome)["exit"] is not None
    _assert_pids_gone((gen1,))


# ---------------------------------------------------------------------------
# 4. resize primitive: a scheduled mid-run size change, observed by the child
# ---------------------------------------------------------------------------


async def test_pty_midrun_resize_observable(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    script = _write_resize_child(tmp_path)
    outcome = await _run(
        tmp_path,
        {
            "command": _ps_command(script),
            "timeout": 30,
            "resize": {"after_seconds": 2.5, "rows": 50, "cols": 120},
        },
    )

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    normalized = _normalize("\n".join(_texts(outcome)))
    # The child observed the tool's mid-run size change: the target geometry
    # and the explicit observation marker both reached the merged stream.
    # (The spawn geometry was 24x80, so a 120x50 observation proves the
    # tool-side resize; the child tolerates the resize landing before or
    # after its boot -- no tight-timing assumption.)
    assert "120 50" in normalized
    assert "RESIZED_OBSERVED" in normalized
    assert _structured(outcome)["exit"] == 0


# ---------------------------------------------------------------------------
# 5. encoding primitive: multi-script UTF-8 round trip, no mojibake
# ---------------------------------------------------------------------------


async def test_pty_utf8_roundtrip_no_mojibake(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    script = _write_encoding_child(tmp_path)
    outcome = await _run(
        tmp_path,
        {"command": _ps_command(script), "timeout": 30, "input": _ENCODING_PAYLOAD + "\r"},
    )

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    normalized = _normalize("\n".join(_texts(outcome)))
    assert f"PYPROBE_ENC:{_ENCODING_PAYLOAD}" in normalized
    assert f"ECHO:{_ENCODING_PAYLOAD}" in normalized


# ---------------------------------------------------------------------------
# 6. process_tree_exit: timeout kills the Job-Object tree, two generations
# ---------------------------------------------------------------------------


async def test_pty_timeout_kills_tree_with_grandchild(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    script = _write_tree_child(tmp_path)
    gen1_pid_file = tmp_path / "gen1.pid"
    gen2_pid_file = tmp_path / "gen2.pid"

    outcome = await _run(
        tmp_path,
        {
            "command": _ps_command(script, gen1_pid_file, "parent", gen2_pid_file),
            "input": "GO\r",  # written after the job capture: the tool's gate order
            "timeout": 8,
        },
    )

    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_TIMEOUT
    joined = "\n".join(_texts(outcome))
    assert "Timed out before completion" in _normalize(joined)

    # Process-level tree reaping evidence: both generations gone.
    gen1 = _wait_pid_file(gen1_pid_file)
    gen2 = _wait_pid_file(gen2_pid_file)
    assert gen1 != gen2
    _assert_pids_gone((gen1, gen2))


# ---------------------------------------------------------------------------
# 7. output flood: capped, marked, and the drain continues past the cap
# ---------------------------------------------------------------------------


async def test_pty_output_flood_truncates_with_original_size(tmp_path: Path) -> None:
    _winpty_or_skip()
    _interpreter_or_skip()
    script = _write_flood_child(tmp_path)
    outcome = await _run(tmp_path, {"command": _ps_command(script), "timeout": 60})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    texts = _texts(outcome)
    joined = "\n".join(texts)
    assert "[pty output truncated: showing 65536 of " in joined
    assert " bytes (limit 65536 bytes)]" in joined
    # The shown merged output is byte-exactly at the cap; the original byte
    # count kept tracking past the cap (the drain never stops, L315).
    assert len(texts[0].encode("utf-8")) == 65536
    assert _structured(outcome)["truncated"] is True
    assert _structured(outcome)["exit"] == 0


# ---------------------------------------------------------------------------
# 8. registration faces: default table ask, platform gate, capability refusals
# ---------------------------------------------------------------------------


def test_pty_default_table_is_ask() -> None:
    # §5.2 (CORE-CONTRACTS L304): a new tool name enters the default table
    # in ask form (git-trio closure shape; side_effects=True asks via the
    # residual rule anyway -- the explicit key documents it).
    assert _DEFAULT_EFFECTS["pty__run"] == "ask"


def test_pty_platform_gate_composes_on_win32_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert pty_adapters() == ()


@pytest.mark.skipif(sys.platform != "win32", reason="the win32 composition face is observable on win32")
def test_pty_platform_gate_composes_one_adapter_on_win32() -> None:
    adapters = pty_adapters()
    assert len(adapters) == 1
    assert isinstance(adapters[0], PtyRunAdapter)


async def test_pty_extras_missing_is_unsupported_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import opencode.tools.builtin.pty as pty_module

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(pty_module, "find_spec", lambda name: None)
    outcome = await _run(tmp_path, {"command": "Write-Output 'x'"})

    # r0b-2: a restricted capability is never a bare exception and never a
    # fake success -- it surfaces as UnsupportedCapability.
    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
    texts = "\n".join(_texts(outcome))
    assert "opencode-py[pty]" in texts


async def test_pty_direct_posix_call_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Defense in depth: the composition gate keeps the tool out of the POSIX
    # catalog entirely, but a directly instantiated adapter also refuses.
    import opencode.tools.builtin.pty as pty_module

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pty_module, "find_spec", lambda name: None)
    outcome = await _run(tmp_path, {"command": "echo hi"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY


# ---------------------------------------------------------------------------
# validation faces (platform-free, bash.py form)
# ---------------------------------------------------------------------------


async def test_pty_empty_command_is_rejected(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, {"command": ""})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.VALIDATION_ERROR


async def test_pty_missing_cwd_fails_with_diagnostic(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    outcome = await _run(tmp_path, {"command": "Write-Output 'x'", "cwd": str(missing)})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_FAILED
    assert "Working directory does not exist" in "\n".join(_texts(outcome))


def test_pty_default_timeout_is_120_seconds() -> None:
    assert PTY_DEFAULT_TIMEOUT_S == BASH_DEFAULT_TIMEOUT_S == 120.0
