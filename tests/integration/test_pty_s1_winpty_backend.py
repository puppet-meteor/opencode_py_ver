"""S1 backend-completeness verification: all four primitives on the winpty agent backend (T-C1-072).

The S1 hard gate (T-B-086 ruling, ticket T-C1-072 section 1): no single
pywinpty backend carried all four primitives through T0 -- ctrl_c was
verified only on the legacy winpty agent backend (doc/evidence/pty0/
platform.json: marker + exit code 130; the vendored ConPTY host does not
dispatch the PTY-written 0x03 console control event), while resize /
encoding / process_tree_exit were verified on the ConPTY default backend.
The tool backend decision (option (a): the winpty agent backend carries
the T1 Windows tool) is only closed when the remaining three primitives
are supplementary-verified ON that backend with real test assertions,
BEFORE any tool implementation (PHASE2-MODULES L68: verification
precedes wiring; an unverified platform/backend face is not enabled).

The four legs here exercise the winpty agent backend directly
(``winpty.enums.Backend.WinPTY``) through the same ``PtyProcess`` API
surface the tool will use (spawn with explicit argv / dimensions,
select-gated reads, ``setwinsize``, ``write``), and the process-tree leg
pairs it with the PRODUCTION ``opencode.process.winjob.JobObject`` (the
T1 wiring target, r0 form's ctypes-direct job is the T0 standalone; the
assign-before-proceed-gate discipline from T-C1-071 is preserved: the
child's grandchild spawn is gated on PTY input that is only written
after the job capture).

Child-process discipline (CORE-CONTRACTS 搂5.3 L315): every child is the
explicit interpreter (``sys.executable -u -c``, argv form; never
``shell=True``), grandchildren are spawned explicitly by the child, the
SIGINT disposition is installed explicitly in the ctrl_c child (an
inherited blocked/ignored mask survives exec -- the T0 CI forensics
lesson), and no on-disk marker files are used for the primitive
observations (PTY output only).

Platform/extras honesty (r0b-2 semantics): the POSIX face skips with the
T2 ticket pointer (p2-slices L170: T1/T2 platform gates are independent
-- a Windows seat never impersonates a POSIX result), and a missing
pywinpty extras install skips as ``restricted`` with the install pointer
(a restricted check never masquerades as verified). Timing assertions
use generous observation windows (never tight upper bounds -- the
CI-slow-runner compatibility form from T-C1-071).
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

_IS_WIN32 = sys.platform == "win32"
_WINPTY_AVAILABLE = find_spec("winpty") is not None

pytestmark = [
    pytest.mark.skipif(
        not _IS_WIN32,
        reason=(
            "T1 platform gate: the Windows PTY face is verified on win32 only; the POSIX leg is "
            "the independent T2 ticket (p2-slices L170: T1/T2 platform gates are independent)"
        ),
    ),
    pytest.mark.skipif(
        not _WINPTY_AVAILABLE,
        reason=(
            "restricted: pywinpty extras not installed in this environment "
            "(pip install 'opencode-py[pty]') -- a restricted check is never a pass (r0b-2)"
        ),
    ),
]

_CHILD_BUDGET_S = 90  # children self-cap; every test reaps earlier in its finally
_MARKER_WINDOW_S = 30.0  # generous observation window, never a tight upper bound
_EXIT_WINDOW_S = 30.0
_POLL_INTERVAL_S = 0.05
_ENCODING_PAYLOAD = "日本語 πλθ café naïve ♥"

if _IS_WIN32:
    import ctypes
    from ctypes import wintypes

_WIN_SYNCHRONIZE = 0x00100000
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_WAIT_TIMEOUT = 258


# ---------------------------------------------------------------------------
# PTY plumbing (T0 probe forms: normalized line predicates, stale-sink-tolerant
# accumulate loop, select-gated reads)
# ---------------------------------------------------------------------------

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _normalize(text: str) -> str:
    return _CSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _has_line(text: str, pattern: str) -> bool:
    """True when ``pattern`` (regex) matches a whole normalized output line."""
    return re.search(rf"(?:^|\n){pattern}(?:\n|$)", _normalize(text)) is not None


def _read_until(
    read_chunk: Callable[[], str], predicate: Callable[[str], bool], deadline: float, sink: list[str]
) -> bool:
    """Accumulate PTY output until ``predicate`` accepts it or the deadline.

    The predicate is evaluated against the accumulated output at the top of
    every pass: multiple markers can surface in one read burst, so a later
    wait must accept a marker an earlier chunk already delivered (the T0
    stale-sink lesson -- never gate on new chunks alone).
    """
    while True:
        if sink and predicate("".join(sink)):
            return True
        if deadline - time.monotonic() <= 0.0:
            return False
        chunk = read_chunk()
        if chunk:
            sink.append(chunk)
            if predicate("".join(sink)):
                return True
        time.sleep(_POLL_INTERVAL_S)


def _child_env() -> dict[str, str]:
    """Explicit env copy: COLUMNS/LINES never shadow the real PTY geometry."""
    return {key: value for key, value in os.environ.items() if key not in ("COLUMNS", "LINES")}


def _spawn(script: str, dimensions: tuple[int, int], cwd: str) -> Any:
    """Spawn the explicit interpreter under a WinPTY-backend pty (T0 form)."""
    # Bare ignores: on win32 winpty is installed but untyped ([import-untyped]);
    # the posix mypy leg has it absent entirely ([import-not-found]).
    from winpty.enums import Backend  # type: ignore
    from winpty.ptyprocess import PtyProcess  # type: ignore

    return PtyProcess.spawn(
        [sys.executable, "-u", "-c", script],
        cwd=cwd,
        env=_child_env(),
        dimensions=dimensions,
        backend=Backend.WinPTY,
    )


def _reader(proc: Any) -> Callable[[], str]:
    def read_chunk() -> str:
        readable, _, _ = select.select([proc.fileobj], [], [], _POLL_INTERVAL_S)
        if not readable:
            return ""
        try:
            return proc.read(65536)
        except EOFError:
            return ""

    return read_chunk


def _reap(proc: Any) -> None:
    try:
        proc.close(force=True)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """Process-level probe (WaitForSingleObject form, T0 _proc_alive)."""
    if sys.platform != "win32":
        # posix-mypy narrowing guard (the cli.py _enable_windows_ctrl_c
        # early-guard form): everything below is win32-only ctypes and is
        # statically unreachable under a posix check; at runtime these
        # helpers are called only from the win32-gated S1 tests above.
        return False
    lib = ctypes.windll.kernel32
    handle = lib.OpenProcess(_WIN_SYNCHRONIZE | _WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return lib.WaitForSingleObject(handle, 0) == _WIN_WAIT_TIMEOUT
    finally:
        lib.CloseHandle(handle)


def _wait_pids_gone(pids: tuple[int, ...], *, window_s: float = 10.0) -> None:
    deadline = time.monotonic() + window_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            time.sleep(_POLL_INTERVAL_S)
    assert not remaining, f"processes survived the job termination: {sorted(remaining)}"


def _exit_code(pid: int, *, window_s: float) -> int:
    """Wait for the process to terminate, then read its exit code."""
    if sys.platform != "win32":
        # posix-mypy narrowing guard (the _pid_alive form): this probe is
        # win32-only ctypes, statically unreachable under a posix check and
        # never called at runtime off the win32-gated S1 tests.
        raise AssertionError("win32-only probe helper")
    lib = ctypes.windll.kernel32
    handle = lib.OpenProcess(_WIN_SYNCHRONIZE | _WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    assert handle, f"OpenProcess({pid}) failed"
    try:
        waited = lib.WaitForSingleObject(handle, int(window_s * 1000))
        assert waited == 0, f"process {pid} did not exit within {window_s}s"
        code = wintypes.DWORD()
        assert lib.GetExitCodeProcess(handle, ctypes.byref(code)), "GetExitCodeProcess failed"
        return int(code.value)
    finally:
        lib.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Child scripts (T0 templates: the proven assertion shapes, verbatim bodies)
# ---------------------------------------------------------------------------

_RESIZE_CHILD = """\
import os, signal, time
sig_winch = [0]
try:
    signal.signal(signal.SIGWINCH, lambda *_: sig_winch.__setitem__(0, 1))
except (AttributeError, ValueError, OSError):
    pass


def emit(tag):
    size = os.get_terminal_size(1)
    print(tag, size.columns, size.lines, flush=True)


emit("SIZE")
last = os.get_terminal_size(1)
end = time.monotonic() + __S1_BUDGET__
while time.monotonic() < end:
    if sig_winch[0]:
        sig_winch[0] = 0
        emit("WINCH")
    cur = os.get_terminal_size(1)
    if cur != last:
        last = cur
        emit("SIZE")
    time.sleep(0.05)
"""

_CTRL_C_CHILD = """\
import os, signal, sys, time
# The SIGINT disposition is installed explicitly, never inherited: an
# inherited blocked mask keeps the signal pending forever, an inherited
# ignore discards it (the T0 CI forensics lesson).
if hasattr(signal, "pthread_sigmask"):
    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGINT])
signal.signal(signal.SIGINT, signal.default_int_handler)
print("READY", flush=True)
try:
    time.sleep(__S1_BUDGET__)
except KeyboardInterrupt:
    print("PYPROBE_INTERRUPTED", flush=True)
    sys.exit(130)
"""

_ENCODING_CHILD = """\
import sys
print("PYPROBE_ENC:" + "__S1_PAYLOAD__", flush=True)
line = sys.stdin.readline()
sys.stdout.write("ECHO:" + line)
sys.stdout.flush()
"""

_TREE_GRANDCHILD = """\
import time
print("GRANDCHILD_ALIVE", flush=True)
time.sleep(__S1_BUDGET__)
"""

_TREE_CHILD = """\
import subprocess, sys, time
print("READY", flush=True)
gate = sys.stdin.readline()
if gate.strip() != "GO":
    sys.exit(2)
grandchild = subprocess.Popen([sys.executable, "-u", "-c", __S1_GRANDCHILD__])
print("GRANDCHILD " + str(grandchild.pid), flush=True)
time.sleep(__S1_BUDGET__)
"""


def _fill(template: str, **values: object) -> str:
    out = template
    for key, value in values.items():
        out = out.replace(f"__S1_{key.upper()}__", value if isinstance(value, str) else str(value))
    if "__S1_" in out:
        raise AssertionError("unmaterialized placeholder in child script")
    return out


# ---------------------------------------------------------------------------
# Leg 1: resize -- a running WinPTY-backend child observes the size change
# ---------------------------------------------------------------------------


def test_s1_winpty_resize_observable(tmp_path: Path) -> None:
    proc = _spawn(_fill(_RESIZE_CHILD, budget=_CHILD_BUDGET_S), (24, 80), str(tmp_path))
    reader = _reader(proc)
    sink: list[str] = []
    try:
        deadline = time.monotonic() + _MARKER_WINDOW_S
        assert _read_until(reader, lambda text: _has_line(text, r"SIZE 80 24"), deadline, sink), (
            "child never reported the initial 80x24 geometry"
        )
        proc.setwinsize(50, 120)
        assert _read_until(
            reader, lambda text: _has_line(text, r"SIZE 120 50"), time.monotonic() + _MARKER_WINDOW_S, sink
        ), "child never observed the new 120x50 geometry on the winpty agent backend"
        assert tuple(proc.getwinsize()) == (50, 120)
    finally:
        _reap(proc)


# ---------------------------------------------------------------------------
# Leg 2: encoding -- multi-script UTF-8 round-trips both directions, no mojibake
# ---------------------------------------------------------------------------


def test_s1_winpty_encoding_roundtrip(tmp_path: Path) -> None:
    payload_re = re.escape(_ENCODING_PAYLOAD)
    proc = _spawn(_fill(_ENCODING_CHILD, payload=_ENCODING_PAYLOAD), (24, 80), str(tmp_path))
    reader = _reader(proc)
    sink: list[str] = []
    try:
        deadline = time.monotonic() + _MARKER_WINDOW_S
        assert _read_until(reader, lambda text: _has_line(text, rf"PYPROBE_ENC:{payload_re}"), deadline, sink), (
            "banner side of the UTF-8 round trip never arrived"
        )
        # Windows console line input terminates on CR (Enter), not LF: a
        # PTY-written "\n" is a literal Ctrl-J that stays queued (T0 anchor).
        proc.write(_ENCODING_PAYLOAD + "\r")
        assert _read_until(reader, lambda text: _has_line(text, rf"ECHO:{payload_re}"), deadline, sink), (
            "echo side of the UTF-8 round trip never arrived"
        )
        # Exact payload match on both sides: no mojibake anywhere.
        normalized = _normalize("".join(sink))
        assert f"PYPROBE_ENC:{_ENCODING_PAYLOAD}" in normalized
        assert f"ECHO:{_ENCODING_PAYLOAD}" in normalized
    finally:
        _reap(proc)


# ---------------------------------------------------------------------------
# Leg 3: process_tree_exit -- the PRODUCTION JobObject covers the WinPTY child
# AND a grandchild released by a post-capture proceed gate
# ---------------------------------------------------------------------------


def test_s1_winpty_process_tree_exit_with_production_jobobject(tmp_path: Path) -> None:
    from opencode.process.winjob import JobObject

    grandchild_script = _fill(_TREE_GRANDCHILD, budget=_CHILD_BUDGET_S)
    proc = _spawn(
        _fill(_TREE_CHILD, budget=_CHILD_BUDGET_S, grandchild=repr(grandchild_script)),
        (24, 80),
        str(tmp_path),
    )
    reader = _reader(proc)
    sink: list[str] = []
    job = None
    child_pid = 0
    grandchild_pid = 0
    try:
        deadline = time.monotonic() + _MARKER_WINDOW_S
        assert _read_until(reader, lambda text: _has_line(text, r"READY"), deadline, sink), (
            "PTY child never printed READY"
        )
        # Assign the WinPTY child to the production Job Object BEFORE the
        # proceed gate: grandchild creation then strictly follows the capture
        # (T0 assign-before-gate discipline; the tool preserves this order).
        job = JobObject()
        job.capture(proc.pid)
        proc.write("GO\r")  # CR terminates the Windows console line input
        assert _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD \d+"), deadline, sink), (
            "child never announced a grandchild"
        )
        assert _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD_ALIVE"), deadline, sink), (
            "grandchild never reported alive"
        )
        match = re.search(r"(?:^|\n)GRANDCHILD (\d+)(?:\n|$)", _normalize("".join(sink)))
        assert match is not None, "grandchild line unparseable"
        child_pid = proc.pid
        grandchild_pid = int(match.group(1))
        assert grandchild_pid != child_pid
        assert _pid_alive(grandchild_pid), "grandchild was not alive before the kill"
        assert _pid_alive(child_pid), "child was not alive before the kill"

        job.terminate()

        _wait_pids_gone((child_pid, grandchild_pid))
    finally:
        if job is not None:
            try:
                job.terminate()
            except OSError:
                pass
            job.close()
        _reap(proc)


# ---------------------------------------------------------------------------
# Leg 4: ctrl_c -- the T0 positive re-asserted on the chosen backend, the
# observable-interrupt form (marker + exit 130, never "sent and assumed")
# ---------------------------------------------------------------------------


def test_s1_winpty_ctrl_c_observable_interrupt(tmp_path: Path) -> None:
    proc = _spawn(_fill(_CTRL_C_CHILD, budget=_CHILD_BUDGET_S), (24, 80), str(tmp_path))
    reader = _reader(proc)
    sink: list[str] = []
    try:
        deadline = time.monotonic() + _MARKER_WINDOW_S
        assert _read_until(reader, lambda text: _has_line(text, r"READY"), deadline, sink), "child never printed READY"
        proc.write("\x03")
        # The ECHOCTL echo of the written \x03 can glue a "^C" prefix onto the
        # marker line (T0 hotfix 4): the predicate tolerates the glued prefix.
        assert _read_until(
            reader,
            lambda text: _has_line(text, r"(?:\^C)?PYPROBE_INTERRUPTED"),
            time.monotonic() + _MARKER_WINDOW_S,
            sink,
        ), "no observable interrupt marker on the winpty agent backend"
        exit_code = _exit_code(proc.pid, window_s=_EXIT_WINDOW_S)
        assert exit_code == 130, f"expected the KeyboardInterrupt exit code 130, got {exit_code}"
    finally:
        _reap(proc)
