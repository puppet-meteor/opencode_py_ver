"""PTY T0 spike probes (p2-slices L175 spike criteria; PHASE2-MODULES L68).

Standalone probes only -- no future modules: everything lives under
``tools/pty0/`` and imports nothing from ``src/opencode`` (the T1/T2
implementation slices own any PTY toolface wiring). The Windows leg drives
pywinpty's ConPTY (``pywinpty>=3.1,<4`` extras, win32 marker only -- when
absent every check records ``restricted`` with the install pointer, never
a pass); the POSIX leg uses the standard library only (``os.forkpty``,
``fcntl`` TIOCSWINSZ, ``os.killpg``).

Four primitives (PHASE2-MODULES L68: each verified separately, per
platform, before any library decision; an unverified platform is not
enabled):

- ``resize``: a running child observes the PTY size change (Windows:
  ConPTY ``setwinsize``; POSIX: TIOCSWINSZ on the master + observable
  SIGWINCH in the child);
- ``ctrl_c``: ``\\x03`` written to the PTY input interrupts the child with
  an observable marker and exit code 130 (never "sent and assumed");
- ``encoding``: a multi-script UTF-8 payload round-trips through the PTY
  in both directions with no mojibake (echo round trip);
- ``process_tree_exit``: a Windows Job Object (ctypes direct, r0 form) /
  POSIX process group kill covers the PTY child AND a grandchild the child
  spawned explicitly (PHASE1-SLIM L150 red line: the Job Object x
  ConPTY interaction is this spike's core unknown).

Child-process discipline (CORE-CONTRACTS 5.3 L315): every child is the
explicit interpreter (``sys.executable``, argv form; never ``shell=True``,
never the platform default shell); grandchildren are spawned explicitly by
the child through a PTY-input proceed gate so the kill verdict can never
race the assignment. Communication is PTY-only (no on-disk markers).

Evidence sanitization: the emitted JSON never contains host paths,
usernames, PIDs or secrets; workspace and temp locations appear only as
``<WORKSPACE>`` / ``<TEMP>`` placeholders and a leak self-check rejects the
serialized evidence if any host-specific value survives. Restricted and
unsupported faces are listed separately from verified ones (r0b-2
semantics: a restricted check never masquerades as verified). The POSIX
leg is carried by CI dual-OS (the dispatcher closes that record); a local
manual run never impersonates CI results and vice versa.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import select
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROBE_VERSION = "pty0-1"

#: Generous timing budgets only (CI slow-runner compatible, W1 spike
#: precedent): these are deadlines the probe waits up to, never tight
#: upper-bound assertions about how fast a platform primitive must be.
CHILD_BUDGET_SECONDS = 90  # children self-cap; the probe always reaps earlier
MARKER_TIMEOUT_SECONDS = 30.0
EXIT_TIMEOUT_SECONDS = 30.0
GRACE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05
#: Generous observation window for a NEGATIVE cross-backend result: an
#: interrupt that works arrives in milliseconds; if none is seen inside this
#: window the face is recorded as not delivering (never a tight assert).
CTRL_C_OBSERVATION_WINDOW_SECONDS = 8.0

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "doc" / "evidence" / "pty0" / "platform.json"

_ENCODING_PAYLOAD = "日本語 πλθ café naïve ♥"

_WIN_RESIZE_MECHANISM = (
    "pywinpty ConPTY PtyProcess.setwinsize (ResizePseudoConsole); child observes via "
    "os.get_terminal_size(fd=1) and prints a SIZE line on change"
)
_WIN_CTRL_C_MECHANISM = (
    "pywinpty write '\\x03' -> console control event -> child KeyboardInterrupt (observable marker + "
    "exit code 130); both pywinpty backends are probed and recorded, the delivering one carries the verdict"
)
_WIN_ENCODING_MECHANISM = "ConPTY UTF-8 input/output round trip via pywinpty PtyProcess write/read"
_WIN_TREE_MECHANISM = (
    "win32 Job Object (JOB_OBJECT_LIMIT_KILL_ON_CLOSE, ctypes direct -- r0 form) assigned to the "
    "ConPTY child BEFORE the PTY-input proceed gate releases the grandchild; TerminateJobObject "
    "must cover the pseudoconsole child AND the grandchild"
)
_POSIX_RESIZE_MECHANISM = "fcntl TIOCSWINSZ on the pty master; child SIGWINCH handler + os.get_terminal_size(fd=1)"
_POSIX_CTRL_C_MECHANISM = (
    "ISIG line discipline: '\\x03' on the pty master -> SIGINT to the foreground process group; "
    "the child installs its own SIGINT disposition and reports its tty (os.ttyname) so the probe "
    "can hold the slave open and the exit-time marker stays readable after the child exits; the "
    "marker match tolerates the ECHOCTL '^C' prefix the INTR echo can glue onto the marker line"
)
_POSIX_ENCODING_MECHANISM = "pty master UTF-8 echo round trip through the child's stdin/stdout"
_POSIX_TREE_MECHANISM = (
    "posix process group (os.forkpty session leader); os.killpg SIGKILL must cover the pty child AND the grandchild"
)

_WINPTY_MISSING_REASON = (
    "pywinpty extras not installed in this environment (pip install 'opencode-py[pty]'); "
    "recorded restricted, never a pass"
)
_POSIX_PTY_MISSING_REASON = "os.forkpty unavailable on this platform; recorded restricted, never a pass"


# ---------------------------------------------------------------------------
# Check result plumbing (r0 platform_probe form)
# ---------------------------------------------------------------------------

CheckStatus = str  # "verified" | "restricted" | "failed"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    details: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        out.update(self.details)
        if self.reason is not None:
            out["reason"] = self.reason
        return out


def _unsupported_entry(check: str, capability: str, reason: str) -> dict[str, str]:
    return {"check": check, "capability": capability, "platform": sys.platform, "reason": reason}


def _restricted(name: str, mechanism: str, reason: str) -> CheckResult:
    return CheckResult(name, "restricted", {"mechanism": mechanism}, reason=reason)


# ---------------------------------------------------------------------------
# Evidence assembly + sanitization (r0 form, plus the explicit
# <WORKSPACE>/<TEMP> placeholder pass)
# ---------------------------------------------------------------------------


def _sanitize(value: Any) -> Any:
    """Recursively replace workspace/temp path occurrences with placeholders.

    Everything the probe records is placeholder-free by construction (sizes,
    markers, booleans, elapsed milliseconds); this pass is the defense in
    depth that keeps any accidental path recording from reaching the
    evidence file. Home paths and usernames are NOT placeholders -- the
    leak self-check rejects those outright.
    """
    workspace = str(Path(__file__).resolve().parents[2])
    temp_root = tempfile.gettempdir()
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        text = value
        for raw, placeholder in ((workspace, "<WORKSPACE>"), (temp_root, "<TEMP>")):
            if raw:
                text = text.replace(raw.rstrip("\\/") + os.sep, placeholder + os.sep)
                text = text.replace(raw, placeholder)
        return text
    return value


def _leak_self_check(evidence: dict[str, Any]) -> None:
    serialized = json.dumps(evidence, ensure_ascii=False)
    forbidden: list[str] = []
    home = str(Path.home())
    if home:
        forbidden.append(home)
    forbidden.append(tempfile.gettempdir())
    try:
        import getpass

        user = getpass.getuser()
        if user:
            forbidden.append(user)
    except Exception:
        pass
    for raw in forbidden:
        if raw and raw in serialized:
            raise ValueError(f"evidence leak self-check failed (matched a sanitized-away value of length {len(raw)})")
    for key in ("pid", "ppid"):
        if f'"{key}' in serialized.lower():
            raise ValueError(f"evidence leak self-check failed (pid-like key '{key}')")


def build_evidence(checks: list[CheckResult], unsupported: list[dict[str, str]], generated_by: str) -> dict[str, Any]:
    # Only fully-verified checks may appear in ci_verified/manual_verified;
    # restricted checks are recorded separately and never masquerade as verified.
    verified_names = [check.name for check in checks if check.status == "verified"]
    restricted_names = [check.name for check in checks if check.status == "restricted"]
    evidence: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "sys_platform": sys.platform,
            "python": platform.python_version(),
        },
        "generated": {
            "by": generated_by,
            "probe": "tools/pty0/pty_probe.py",
        },
        "ci_verified": verified_names if generated_by == "ci" else [],
        "manual_verified": verified_names if generated_by == "manual" else [],
        "restricted": restricted_names,
        "unsupported": unsupported,
        "checks": {check.name: check.to_dict() for check in checks},
        "notes": [
            "sanitization: host paths <WORKSPACE>/<TEMP> placeholders only; "
            "no usernames, PIDs, prompts, secrets or endpoints recorded",
            "restricted = capability untestable in this environment with a recorded reason, not a pass; "
            "failed = primitive not validated on this platform (platform not enabled per PHASE2-MODULES L68)",
            "the POSIX leg is carried by CI dual-OS; the dispatcher closes that record "
            "(manual and ci_verified never impersonate each other)",
        ],
    }
    return _sanitize(evidence)


# ---------------------------------------------------------------------------
# Child scripts (explicit interpreter ``sys.executable`` everywhere; the
# placeholders are materialized with the same r0 template discipline)
# ---------------------------------------------------------------------------

_RESIZE_CHILD_TEMPLATE = """\
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
end = time.monotonic() + __PTY0_BUDGET__
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

_CTRL_C_CHILD_SCRIPT = """\
import os, signal, sys, time
# The SIGINT disposition is installed explicitly, never inherited: the CI
# process tree (runner -> bash -> pytest) can carry SIGINT blocked or ignored,
# and both states survive fork+exec -- an inherited mask keeps the signal
# pending forever, an inherited ignore discards it. The SIGWINCH child installs
# its handler explicitly for the same reason: when the primitive under test is
# the delivery itself, the disposition must be controlled, not inherited.
if hasattr(signal, "pthread_sigmask"):
    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGINT])
signal.signal(signal.SIGINT, signal.default_int_handler)
# Report the child's own terminal so the probe parent can hold the slave side
# open: the exit-time marker stays readable only while a slave descriptor
# exists (the kernel flushes the master read queue once the last slave
# descriptor closes). os.ttyname is the portable form -- os.ptsname does not
# exist on Linux CPython. Platforms without os.ttyname print a bare READY and
# the parent simply cannot hold the slave.
_tty = ""
if hasattr(os, "ttyname"):
    try:
        _tty = os.ttyname(0)
    except OSError:
        pass
if _tty:
    print("READY " + _tty, flush=True)
else:
    print("READY", flush=True)
try:
    time.sleep(__PTY0_BUDGET__)
except KeyboardInterrupt:
    print("PYPROBE_INTERRUPTED", flush=True)
    sys.exit(130)
"""

_ENCODING_CHILD_TEMPLATE = """\
import sys
print("PYPROBE_ENC:" + "__PTY0_PAYLOAD__", flush=True)
line = sys.stdin.readline()
sys.stdout.write("ECHO:" + line)
sys.stdout.flush()
"""

_GRANDCHILD_TEMPLATE = """\
import time
print("GRANDCHILD_ALIVE", flush=True)
time.sleep(__PTY0_BUDGET__)
"""

_TREE_CHILD_TEMPLATE = """\
import subprocess, sys, time
print("READY", flush=True)
gate = sys.stdin.readline()
if gate.strip() != "GO":
    sys.exit(2)
grandchild = subprocess.Popen([sys.executable, "-u", "-c", __PTY0_GRANDCHILD__])
print("GRANDCHILD " + str(grandchild.pid), flush=True)
time.sleep(__PTY0_BUDGET__)
"""


def _fill(template: str, **values: Any) -> str:
    """Materialize a child script template (r0 placeholder discipline)."""
    out = template
    for key, value in values.items():
        out = out.replace(f"__PTY0_{key.upper()}__", value if isinstance(value, str) else str(value))
    if "__PTY0_" in out:
        raise ValueError("unmaterialized probe placeholder in child script")
    return out


def _child_env() -> dict[str, str]:
    """Explicit env copy: COLUMNS/LINES never shadow the real PTY geometry."""
    return {key: value for key, value in os.environ.items() if key not in ("COLUMNS", "LINES")}


# ---------------------------------------------------------------------------
# Output normalization: strip ConPTY ANSI control sequences and unify line
# endings before marker matching (a rendered pseudoconsole screen wraps and
# escapes; the markers themselves are plain ASCII lines).
# ---------------------------------------------------------------------------

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _normalize_output(text: str) -> str:
    return _CSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def _has_line(text: str, pattern: str) -> bool:
    """True when ``pattern`` (regex) matches a whole normalized output line."""
    return re.search(rf"(?:^|\n){pattern}(?:\n|$)", _normalize_output(text)) is not None


def _ready_tty(sink: list[str]) -> str | None:
    """The tty path reported on the child's READY line (None = bare READY)."""
    m = re.search(r"(?:^|\n)READY (\S+)(?:\n|$)", _normalize_output("".join(sink)))
    return m.group(1) if m else None


def _read_until(
    read_chunk: Callable[[], str], predicate: Callable[[str], bool], deadline: float, sink: list[str]
) -> bool:
    """Accumulate PTY output until ``predicate`` accepts it or the deadline.

    ``read_chunk`` returns the next available chunk ('' when nothing is
    readable right now). The predicate is evaluated against the accumulated
    output at the top of every pass: multiple markers can surface in one
    read burst, so a later wait must accept a marker that an earlier chunk
    already delivered. The deadline is a generous wait budget, not a tight
    upper bound on the primitive.
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
        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Process liveness (r0 platform_probe form; no PIDs are ever recorded)
# ---------------------------------------------------------------------------


def _proc_alive(pid: int) -> bool:
    """Is the process still running? No PIDs are ever recorded in evidence."""
    if sys.platform == "win32":
        lib = _kernel32()
        handle = lib.OpenProcess(_WIN_SYNCHRONIZE | _WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            return lib.WaitForSingleObject(handle, 0) == _WIN_WAIT_TIMEOUT
        finally:
            lib.CloseHandle(handle)
    # POSIX: a zombie (exited, not yet reaped by its parent) still answers
    # kill(pid, 0) but is dead for reaping purposes -- read its state when
    # /proc is available, e.g. Linux. macOS falls back to kill(pid, 0).
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        data = None
    if data is not None:
        # state is the field right after "(comm)"; comm may contain parens.
        rparen = data.rfind(b")")
        state = data[rparen + 2 : rparen + 3]
        return state not in (b"Z", b"X")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Windows kernel32 plumbing (Job Object + process liveness/exit codes). All
# bodies are behind `sys.platform` guards: unreachable code is never executed
# on POSIX and mypy suppresses it there, while it is fully checked on Windows.
# ---------------------------------------------------------------------------

_WIN_PROCESS_TERMINATE = 0x0001
_WIN_PROCESS_SET_QUOTA = 0x0100
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_SYNCHRONIZE = 0x00100000
_WIN_WAIT_TIMEOUT = 258
_WIN_JOB_KILL_ON_CLOSE = 0x2000
_WIN_JOB_EXTENDED_LIMIT_INFO = 9

_KERNEL32_READY = False


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_ulonglong),
        ("PerJobUserTimeLimit", ctypes.c_ulonglong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise RuntimeError("kernel32 helpers are windows-only")
    global _KERNEL32_READY
    lib = ctypes.windll.kernel32
    if not _KERNEL32_READY:
        lib.OpenProcess.restype = ctypes.c_void_p
        lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        lib.WaitForSingleObject.restype = wintypes.DWORD
        lib.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        lib.CloseHandle.restype = wintypes.BOOL
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
        lib.CreateJobObjectW.restype = ctypes.c_void_p
        lib.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        lib.SetInformationJobObject.restype = wintypes.BOOL
        lib.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        lib.AssignProcessToJobObject.restype = wintypes.BOOL
        lib.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.TerminateJobObject.restype = wintypes.BOOL
        lib.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        lib.GetExitCodeProcess.restype = wintypes.BOOL
        lib.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        _KERNEL32_READY = True
    return lib


def _win_create_kill_on_close_job() -> int:
    lib = _kernel32()
    job = lib.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _WIN_JOB_KILL_ON_CLOSE
    if not lib.SetInformationJobObject(job, _WIN_JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        lib.CloseHandle(job)
        raise OSError("SetInformationJobObject failed")
    return int(job)


def _win_assign_process(job: int, pid: int) -> None:
    lib = _kernel32()
    proc = lib.OpenProcess(_WIN_PROCESS_SET_QUOTA | _WIN_PROCESS_TERMINATE, False, pid)
    if not proc:
        raise OSError("OpenProcess failed for child")
    try:
        if not lib.AssignProcessToJobObject(job, proc):
            raise OSError("AssignProcessToJobObject failed for child")
    finally:
        lib.CloseHandle(proc)


def _win_terminate_job(job: int) -> None:
    lib = _kernel32()
    lib.TerminateJobObject(job, 1)


def _win_open_process_sync(pid: int) -> int | None:
    """A SYNCHRONIZE|QUERY handle for wait/exit-code observation."""
    lib = _kernel32()
    handle = lib.OpenProcess(_WIN_SYNCHRONIZE | _WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return int(handle) if handle else None


def _win_wait_terminated(handle: int, timeout_ms: int) -> bool:
    lib = _kernel32()
    return lib.WaitForSingleObject(handle, timeout_ms) == 0  # WAIT_OBJECT_0


def _win_exit_code(handle: int) -> int:
    lib = _kernel32()
    code = wintypes.DWORD()
    if not lib.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise OSError("GetExitCodeProcess failed")
    return int(code.value)


def _win_close(handle: int) -> None:
    lib = _kernel32()
    lib.CloseHandle(handle)


def _winpty_available() -> bool:
    """pywinpty extras gate: ``restricted`` (never a pass) when missing."""
    from importlib.util import find_spec

    return find_spec("winpty") is not None


# ---------------------------------------------------------------------------
# Windows leg: pywinpty ConPTY helpers
# ---------------------------------------------------------------------------


def _win_spawn(script: str, dimensions: tuple[int, int], backend: int | None = None) -> tuple[Any, str]:
    """Spawn the explicit interpreter inside a pywinpty PTY; returns (PtyProcess, cwd).

    ``backend=None`` means pywinpty's own default (the ConPTY face); the
    ctrl-c primitive probes BOTH pywinpty backends because this stack's
    vendored ConPTY host and its legacy winpty agent differ in control-event
    delivery. ``PtyProcess.read`` blocks, so reads always happen through the
    socket the reader thread feeds, gated by ``select`` inside the deadline
    loop.
    """
    # Bare ignores: on win32 winpty is installed but untyped ([import-untyped]);
    # the posix CI mypy leg has it absent entirely ([import-not-found]).
    from winpty.enums import Backend  # type: ignore
    from winpty.ptyprocess import PtyProcess  # type: ignore

    cwd = tempfile.mkdtemp(prefix="pty0-cwd-")
    proc = PtyProcess.spawn(
        [sys.executable, "-u", "-c", script],
        cwd=cwd,
        env=_child_env(),
        dimensions=dimensions,
        backend=backend if backend is not None else Backend.ConPTY,
    )
    return proc, cwd


def _win_reader(proc: Any) -> Callable[[], str]:
    def read_chunk() -> str:
        readable, _, _ = select.select([proc.fileobj], [], [], POLL_INTERVAL_SECONDS)
        if not readable:
            return ""
        try:
            return proc.read(65536)
        except EOFError:
            return ""

    return read_chunk


def _win_reap(proc: Any, cwd: str) -> None:
    try:
        proc.close(force=True)
    except Exception:
        pass
    shutil.rmtree(cwd, ignore_errors=True)


def _posix_pty_available() -> bool:
    """POSIX gate: stdlib forkpty; ``restricted`` when the platform lacks it."""
    return hasattr(os, "forkpty")


# ---------------------------------------------------------------------------
# Primitive probes: four per PHASE2-MODULES L68, each platform-branching
# ---------------------------------------------------------------------------


def probe_resize() -> CheckResult:
    """Primitive 1: a running child observes the PTY size change."""
    if sys.platform == "win32":
        return _win_probe_resize()
    return _posix_probe_resize()


def probe_ctrl_c() -> CheckResult:
    """Primitive 2: '\\x03' on the PTY input interrupts the child (observable)."""
    if sys.platform == "win32":
        return _win_probe_ctrl_c()
    return _posix_probe_ctrl_c()


def probe_encoding() -> CheckResult:
    """Primitive 3: multi-script UTF-8 round trip through the PTY, no mojibake."""
    if sys.platform == "win32":
        return _win_probe_encoding()
    return _posix_probe_encoding()


def probe_process_tree_exit() -> CheckResult:
    """Primitive 4: tree kill covers the PTY child AND an explicit grandchild."""
    if sys.platform == "win32":
        return _win_probe_process_tree_exit()
    return _posix_probe_process_tree_exit()


# ---------------------------------------------------------------------------
# Windows primitive legs
# ---------------------------------------------------------------------------


def _first_geometry(sink: list[str]) -> str:
    """``cols x rows`` string from the first SIZE/WINCH line in the sink."""
    for line in _normalize_output("".join(sink)).splitlines():
        if re.fullmatch(r"(?:SIZE|WINCH) \d+ \d+", line):
            parts = line.split()
            return f"{parts[1]}x{parts[2]}"
    return "unknown"


def _win_probe_resize() -> CheckResult:
    if not _winpty_available():
        return _restricted("resize", _WIN_RESIZE_MECHANISM, _WINPTY_MISSING_REASON)
    script = _fill(_RESIZE_CHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS)
    proc, cwd = _win_spawn(script, (24, 80))
    reader = _win_reader(proc)
    sink: list[str] = []
    details: dict[str, Any] = {"mechanism": _WIN_RESIZE_MECHANISM, "set_rows": 50, "set_columns": 120}
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"SIZE \d+ \d+"), deadline, sink):
            return CheckResult("resize", "failed", details, reason="child never reported an initial SIZE line")
        details["observed_before"] = _first_geometry(sink)
        proc.setwinsize(50, 120)
        if not _read_until(reader, lambda text: _has_line(text, r"SIZE 120 50"), deadline, sink):
            details["child_observed_new_size"] = False
            return CheckResult("resize", "failed", details, reason="child never observed the new 120x50 geometry")
        details["child_observed_new_size"] = True
        details["observed_after"] = "120x50"
        return CheckResult("resize", "verified", details)
    finally:
        _win_reap(proc, cwd)


def _win_ctrl_c_observation(backend: int, marker_window: float) -> tuple[bool, int | None, str]:
    """One '\\x03' delivery attempt on one pywinpty backend.

    Returns (marker_observed, exit_code, note). ``\\r`` never plays a role
    here: the child never reads stdin, so the only input event is the 0x03
    byte itself.
    """
    script = _fill(_CTRL_C_CHILD_SCRIPT, budget=CHILD_BUDGET_SECONDS)
    proc, cwd = _win_spawn(script, (24, 80), backend)
    reader = _win_reader(proc)
    sink: list[str] = []
    handle = _win_open_process_sync(proc.pid)
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"READY"), deadline, sink):
            return False, None, "child never printed READY"
        proc.write("\x03")
        marker = _read_until(
            reader, lambda text: _has_line(text, r"PYPROBE_INTERRUPTED"), time.monotonic() + marker_window, sink
        )
        exit_code: int | None = None
        if marker:
            wait_deadline = time.monotonic() + EXIT_TIMEOUT_SECONDS
            while time.monotonic() < wait_deadline:
                if not proc.isalive() and not _proc_alive(proc.pid):
                    break
                time.sleep(POLL_INTERVAL_SECONDS)
            if handle is not None:
                exit_code = _win_exit_code(handle)
            return True, exit_code, ""
        return False, None, "no interrupt marker within the observation window"
    finally:
        if handle is not None:
            _win_close(handle)
        _win_reap(proc, cwd)


def _win_probe_ctrl_c() -> CheckResult:
    if not _winpty_available():
        return _restricted("ctrl_c", _WIN_CTRL_C_MECHANISM, _WINPTY_MISSING_REASON)
    from winpty.enums import Backend

    details: dict[str, Any] = {"mechanism": _WIN_CTRL_C_MECHANISM}
    # Core T0 finding carried per-run: on this stack the vendored ConPTY
    # host does not dispatch the console control event for PTY-written
    # 0x03 (the pending cooked read is flushed, nothing is signalled),
    # while the legacy winpty agent backend delivers it. Both faces are
    # observed and recorded; the delivering one carries the verdict.
    legs: list[tuple[str, bool, int | None, str]] = []
    for label, backend, window in (
        ("winpty (legacy agent)", Backend.WinPTY, MARKER_TIMEOUT_SECONDS),
        ("conpty (vendored pseudoconsole host)", Backend.ConPTY, CTRL_C_OBSERVATION_WINDOW_SECONDS),
    ):
        interrupted, exit_code, note = _win_ctrl_c_observation(backend, window)
        legs.append((label, interrupted, exit_code, note))
    details["backend_observations"] = [
        {
            "backend": label,
            "interrupt_delivered": interrupted,
            "child_exit_code": code,
            "note": note or "interrupt marker observed",
        }
        for label, interrupted, code, note in legs
    ]
    delivering = next(((label, code) for label, ok, code, _ in legs if ok and code == 130), None)
    if delivering is None:
        details["marker_observed"] = False
        details["child_exit_code"] = None
        details["child_alive_after_interrupt"] = True
        return CheckResult(
            "ctrl_c", "failed", details, reason="x03 produced no observable interrupt semantics on any pywinpty backend"
        )
    details["backend_used"] = delivering[0]
    details["marker_observed"] = True
    details["child_exit_code"] = delivering[1]
    details["child_alive_after_interrupt"] = False
    return CheckResult("ctrl_c", "verified", details)


def _win_probe_encoding() -> CheckResult:
    if not _winpty_available():
        return _restricted("encoding", _WIN_ENCODING_MECHANISM, _WINPTY_MISSING_REASON)
    script = _fill(_ENCODING_CHILD_TEMPLATE, payload=_ENCODING_PAYLOAD)
    proc, cwd = _win_spawn(script, (24, 80))
    reader = _win_reader(proc)
    sink: list[str] = []
    payload_re = re.escape(_ENCODING_PAYLOAD)
    details: dict[str, Any] = {"mechanism": _WIN_ENCODING_MECHANISM, "payload": _ENCODING_PAYLOAD}
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        banner = _read_until(reader, lambda text: _has_line(text, rf"PYPROBE_ENC:{payload_re}"), deadline, sink)
        # Windows console line input terminates on CR (Enter), not LF: a
        # PTY-written "\n" is a literal Ctrl-J that stays queued in the line.
        proc.write(_ENCODING_PAYLOAD + "\r")
        echo = _read_until(reader, lambda text: _has_line(text, rf"ECHO:{payload_re}"), deadline, sink)
        details["banner_observed"] = banner
        details["echo_observed"] = echo
        details["exact_match"] = bool(banner and echo)
        if not (banner and echo):
            side = "banner" if not banner else "echo"
            return CheckResult(
                "encoding",
                "failed",
                details,
                reason=f"UTF-8 payload did not round-trip ({side} side mojibake or missing)",
            )
        return CheckResult("encoding", "verified", details)
    finally:
        _win_reap(proc, cwd)


def _win_probe_process_tree_exit() -> CheckResult:
    if not _winpty_available():
        return _restricted("process_tree_exit", _WIN_TREE_MECHANISM, _WINPTY_MISSING_REASON)
    grandchild_script = _fill(_GRANDCHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS)
    script = _fill(_TREE_CHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS, grandchild=repr(grandchild_script))
    proc, cwd = _win_spawn(script, (24, 80))
    reader = _win_reader(proc)
    sink: list[str] = []
    job = 0
    child_handle: int | None = None
    grandchild_handle: int | None = None
    details: dict[str, Any] = {
        "mechanism": _WIN_TREE_MECHANISM,
        "tree_shape": (
            "probe -> PTY child (explicit interpreter) -> grandchild (explicit interpreter, PTY-input proceed gate)"
        ),
    }
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"READY"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="PTY child never printed READY")
        # Assign the ConPTY child to the Job Object BEFORE the proceed gate:
        # grandchild creation then strictly follows job assignment, so the
        # whole tree is job-scoped (r0 proceed-gate discipline).
        job = _win_create_kill_on_close_job()
        _win_assign_process(job, proc.pid)
        proc.write("GO\r")  # CR terminates the Windows console line input (LF is a literal Ctrl-J)
        if not _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD \d+"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="child never announced a grandchild")
        if not _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD_ALIVE"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="grandchild never reported alive")
        match = re.search(r"(?:^|\n)GRANDCHILD (\d+)(?:\n|$)", _normalize_output("".join(sink)))
        if match is None:
            return CheckResult("process_tree_exit", "failed", details, reason="grandchild line unparseable")
        grandchild_id = int(match.group(1))
        alive_deadline = time.monotonic() + EXIT_TIMEOUT_SECONDS
        grandchild_alive = False
        while time.monotonic() < alive_deadline:
            if _proc_alive(grandchild_id):
                grandchild_alive = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        details["grandchild_alive_before_kill"] = grandchild_alive
        if not grandchild_alive:
            return CheckResult(
                "process_tree_exit", "failed", details, reason="grandchild was not alive before the kill"
            )
        child_handle = _win_open_process_sync(proc.pid)
        grandchild_handle = _win_open_process_sync(grandchild_id)
        started = time.monotonic()
        _win_terminate_job(job)
        wait_ms = int(GRACE_SECONDS * 1000)
        child_terminated = child_handle is not None and _win_wait_terminated(child_handle, wait_ms)
        grandchild_terminated = grandchild_handle is not None and _win_wait_terminated(grandchild_handle, wait_ms)
        details["child_terminated"] = child_terminated
        details["grandchild_terminated"] = grandchild_terminated
        details["all_exited_within_grace"] = bool(child_terminated and grandchild_terminated)
        details["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        if not (child_terminated and grandchild_terminated):
            return CheckResult(
                "process_tree_exit",
                "failed",
                details,
                reason="Job Object termination did not cover the ConPTY child and the grandchild within grace",
            )
        return CheckResult("process_tree_exit", "verified", details)
    finally:
        if job:
            try:
                _win_terminate_job(job)
            finally:
                _win_close(job)
        if child_handle is not None:
            _win_close(child_handle)
        if grandchild_handle is not None:
            _win_close(grandchild_handle)
        _win_reap(proc, cwd)


# ---------------------------------------------------------------------------
# POSIX primitive legs
# ---------------------------------------------------------------------------


def _posix_spawn(script: str, dimensions: tuple[int, int]) -> tuple[int, int]:
    """``os.forkpty`` + ``os.execve`` of the explicit interpreter (never ``shell=True``).

    Returns (child_pid, master_fd). The forkpty child is a session leader on a
    fresh pty, so its process group id equals its pid (the killpg coverage
    form). The initial winsize is set on the master before the interpreter
    can boot far enough to read geometry. The win32 guard below mirrors the
    Windows-section discipline: it marks the POSIX-only body unreachable for
    mypy on Windows; this helper never runs there.
    """
    if sys.platform == "win32":
        raise OSError("os.forkpty is a POSIX primitive")
    pid, master_fd = os.forkpty()
    if pid == 0:
        try:
            os.execve(sys.executable, [sys.executable, "-u", "-c", script], _child_env())
        finally:
            os._exit(127)
    _posix_set_winsize(master_fd, *dimensions)
    return pid, master_fd


def _posix_set_winsize(master_fd: int, rows: int, cols: int) -> None:
    """``TIOCSWINSZ`` on the pty master (drives the child's SIGWINCH)."""
    if sys.platform == "win32":
        return
    import fcntl
    import struct
    import termios

    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _posix_reader(master_fd: int) -> Callable[[], str]:
    """Non-blocking master reads; the incremental UTF-8 decoder never splits a
    multibyte sequence across chunk boundaries (encoding leg correctness)."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_chunk() -> str:
        readable, _, _ = select.select([master_fd], [], [], POLL_INTERVAL_SECONDS)
        if not readable:
            return ""
        try:
            data = os.read(master_fd, 65536)
        except OSError:  # EIO after the last slave closes
            return ""
        if not data:
            return ""
        return decoder.decode(data)

    return read_chunk


def _posix_wait_exit(pid: int, timeout: float) -> int | None:
    """``waitpid`` exit code (negative when signal-terminated); None = still running."""
    if sys.platform == "win32":
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return None
        if waited == pid:
            if os.WIFEXITED(status):
                return os.WEXITSTATUS(status)
            if os.WIFSIGNALED(status):
                return -os.WTERMSIG(status)
            return None
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def _posix_kill_process_group(pgid: int) -> None:
    """SIGKILL the whole process group (the forkpty child IS the group leader)."""
    if sys.platform == "win32":
        return
    os.killpg(pgid, signal.SIGKILL)


def _posix_hold_slave(slave_path: str) -> int:
    """Open the pty slave by path and hold the descriptor open (O_NOCTTY).

    The ctrl_c child prints its interrupt marker and exits within
    microseconds, and once the LAST slave descriptor closes the kernel
    flushes the master read queue (reads then return EIO) -- the
    exit-time tail stays observable only while a slave descriptor is held.
    The path comes from the child's own READY line (its os.ttyname(0)
    report, the portable form: os.ptsname does not exist on Linux CPython).
    The win32 guard mirrors _posix_spawn: POSIX-only body, unreachable on
    Windows (mypy platform narrowing).
    """
    if sys.platform == "win32":
        raise OSError("opening a pty slave by path is a POSIX primitive")
    return os.open(slave_path, os.O_RDWR | os.O_NOCTTY)


def _posix_reap(pid: int, master_fd: int) -> None:
    """Close the master (SIGHUP to the session), then reap the child."""
    try:
        os.close(master_fd)
    except OSError:
        pass
    if sys.platform == "win32":
        return
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return
        if waited == pid:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


def _posix_probe_resize() -> CheckResult:
    if not _posix_pty_available():
        return _restricted("resize", _POSIX_RESIZE_MECHANISM, _POSIX_PTY_MISSING_REASON)
    script = _fill(_RESIZE_CHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS)
    pid, master_fd = _posix_spawn(script, (24, 80))
    reader = _posix_reader(master_fd)
    sink: list[str] = []
    details: dict[str, Any] = {"mechanism": _POSIX_RESIZE_MECHANISM, "set_rows": 50, "set_columns": 120}
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"SIZE \d+ \d+"), deadline, sink):
            return CheckResult("resize", "failed", details, reason="child never reported an initial SIZE line")
        details["observed_before"] = _first_geometry(sink)
        _posix_set_winsize(master_fd, 50, 120)
        if not _read_until(reader, lambda text: _has_line(text, r"SIZE 120 50"), deadline, sink):
            details["child_observed_new_size"] = False
            return CheckResult("resize", "failed", details, reason="child never observed the new 120x50 geometry")
        details["child_observed_new_size"] = True
        details["observed_after"] = "120x50"
        # The child emits its WINCH line before the SIZE re-print in the same
        # loop pass; the generous settle window keeps the SIGWINCH observation
        # tolerant of an async-signal delivery reorder.
        winch_deadline = time.monotonic() + CTRL_C_OBSERVATION_WINDOW_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"WINCH \d+ \d+"), winch_deadline, sink):
            details["sigwinch_observed"] = False
            return CheckResult("resize", "failed", details, reason="child never observed a SIGWINCH")
        details["sigwinch_observed"] = True
        return CheckResult("resize", "verified", details)
    finally:
        _posix_reap(pid, master_fd)


def _posix_probe_ctrl_c() -> CheckResult:
    if not _posix_pty_available():
        return _restricted("ctrl_c", _POSIX_CTRL_C_MECHANISM, _POSIX_PTY_MISSING_REASON)
    script = _fill(_CTRL_C_CHILD_SCRIPT, budget=CHILD_BUDGET_SECONDS)
    pid, master_fd = _posix_spawn(script, (24, 80))
    reader = _posix_reader(master_fd)
    sink: list[str] = []
    details: dict[str, Any] = {"mechanism": _POSIX_CTRL_C_MECHANISM}
    # The slave descriptor is held from the moment the child is observably
    # alive (its READY line carries the tty path) until the reap: while it is
    # held the pty cannot hang up, so the exit-time marker stays readable
    # instead of racing the poll interval against a microsecond exit (see
    # _posix_hold_slave).
    slave_fd: int | None = None
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"READY(?: \S+)?"), deadline, sink):
            return CheckResult("ctrl_c", "failed", details, reason="child never printed READY")
        tty_path = _ready_tty(sink)
        details["child_tty"] = tty_path or "unreported"
        if tty_path:
            try:
                slave_fd = _posix_hold_slave(tty_path)
            except OSError as exc:
                # An unholdable slave degrades to the unhedged read race; the
                # negative record below will still report the observed state.
                details["slave_hold_error"] = str(exc)
        os.write(master_fd, b"\x03")
        marker_deadline = time.monotonic() + CTRL_C_OBSERVATION_WINDOW_SECONDS
        # The ECHOCTL echo of the \x03 just written can arrive WITHOUT a
        # trailing newline: an empty pending input line (the probe writes only
        # the INTR byte) echoes just "^C", so the child's marker line reaches
        # the master glued to it -- "^CPYPROBE_INTERRUPTED". The marker match
        # therefore tolerates that glued prefix.
        marker = _read_until(
            reader, lambda text: _has_line(text, r"(?:\^C)?PYPROBE_INTERRUPTED"), marker_deadline, sink
        )
        details["marker_observed"] = marker
        if not marker:
            # A negative record still reports the OBSERVED child state, never
            # assumed liveness (a silently dead child must not read as alive),
            # plus the raw read tail and the hold state so the next record is
            # self-diagnosing (echo glue vs lost bytes are distinguishable).
            exit_code = _posix_wait_exit(pid, POLL_INTERVAL_SECONDS)
            details["child_exit_code"] = exit_code
            details["child_alive_after_interrupt"] = exit_code is None
            details["slave_held"] = slave_fd is not None
            details["sink_tail"] = repr(_normalize_output("".join(sink))[-160:])
            if exit_code == 130:
                reason = (
                    "interrupt delivered (exit 130) but the PYPROBE_INTERRUPTED line was not observed at the master"
                )
            else:
                reason = "x03 produced no observable interrupt within the observation window"
            return CheckResult("ctrl_c", "failed", details, reason=reason)
        exit_code = _posix_wait_exit(pid, EXIT_TIMEOUT_SECONDS)
        details["child_exit_code"] = exit_code
        details["child_alive_after_interrupt"] = exit_code is None
        if exit_code != 130:
            return CheckResult(
                "ctrl_c", "failed", details, reason=f"child exit code {exit_code} is not the 130 interrupt convention"
            )
        return CheckResult("ctrl_c", "verified", details)
    finally:
        # Close the held slave first: the pty hangs up only when the last
        # slave descriptor closes, then the reap's master close completes the
        # documented teardown order.
        if slave_fd is not None:
            os.close(slave_fd)
        _posix_reap(pid, master_fd)


def _posix_probe_encoding() -> CheckResult:
    if not _posix_pty_available():
        return _restricted("encoding", _POSIX_ENCODING_MECHANISM, _POSIX_PTY_MISSING_REASON)
    script = _fill(_ENCODING_CHILD_TEMPLATE, payload=_ENCODING_PAYLOAD)
    pid, master_fd = _posix_spawn(script, (24, 80))
    reader = _posix_reader(master_fd)
    sink: list[str] = []
    payload_re = re.escape(_ENCODING_PAYLOAD)
    details: dict[str, Any] = {"mechanism": _POSIX_ENCODING_MECHANISM, "payload": _ENCODING_PAYLOAD}
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        banner = _read_until(reader, lambda text: _has_line(text, rf"PYPROBE_ENC:{payload_re}"), deadline, sink)
        # POSIX termios line input terminates on LF (the Windows ConPTY face
        # needs CR instead; the two legs never share line-ending forms).
        os.write(master_fd, (_ENCODING_PAYLOAD + "\n").encode("utf-8"))
        echo = _read_until(reader, lambda text: _has_line(text, rf"ECHO:{payload_re}"), deadline, sink)
        details["banner_observed"] = banner
        details["echo_observed"] = echo
        details["exact_match"] = bool(banner and echo)
        if not (banner and echo):
            side = "banner" if not banner else "echo"
            return CheckResult(
                "encoding",
                "failed",
                details,
                reason=f"UTF-8 payload did not round-trip ({side} side mojibake or missing)",
            )
        return CheckResult("encoding", "verified", details)
    finally:
        _posix_reap(pid, master_fd)


def _posix_probe_process_tree_exit() -> CheckResult:
    if not _posix_pty_available():
        return _restricted("process_tree_exit", _POSIX_TREE_MECHANISM, _POSIX_PTY_MISSING_REASON)
    grandchild_script = _fill(_GRANDCHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS)
    script = _fill(_TREE_CHILD_TEMPLATE, budget=CHILD_BUDGET_SECONDS, grandchild=repr(grandchild_script))
    pid, master_fd = _posix_spawn(script, (24, 80))
    reader = _posix_reader(master_fd)
    sink: list[str] = []
    details: dict[str, Any] = {
        "mechanism": _POSIX_TREE_MECHANISM,
        "tree_shape": (
            "probe -> PTY child (explicit interpreter, session leader) -> "
            "grandchild (explicit interpreter, same process group)"
        ),
    }
    try:
        deadline = time.monotonic() + MARKER_TIMEOUT_SECONDS
        if not _read_until(reader, lambda text: _has_line(text, r"READY"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="PTY child never printed READY")
        os.write(master_fd, b"GO\n")  # LF terminates POSIX line input (CR is the ConPTY form)
        if not _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD \d+"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="child never announced a grandchild")
        if not _read_until(reader, lambda text: _has_line(text, r"GRANDCHILD_ALIVE"), deadline, sink):
            return CheckResult("process_tree_exit", "failed", details, reason="grandchild never reported alive")
        match = re.search(r"(?:^|\n)GRANDCHILD (\d+)(?:\n|$)", _normalize_output("".join(sink)))
        if match is None:
            return CheckResult("process_tree_exit", "failed", details, reason="grandchild line unparseable")
        grandchild_id = int(match.group(1))
        alive_deadline = time.monotonic() + EXIT_TIMEOUT_SECONDS
        grandchild_alive = False
        while time.monotonic() < alive_deadline:
            if _proc_alive(grandchild_id):
                grandchild_alive = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        details["grandchild_alive_before_kill"] = grandchild_alive
        if not grandchild_alive:
            return CheckResult(
                "process_tree_exit", "failed", details, reason="grandchild was not alive before the kill"
            )
        started = time.monotonic()
        _posix_kill_process_group(pid)  # the forkpty child is the session leader: pgid == pid
        grace_deadline = time.monotonic() + GRACE_SECONDS
        child_terminated = False
        grandchild_terminated = False
        while time.monotonic() < grace_deadline and not (child_terminated and grandchild_terminated):
            if not child_terminated and not _proc_alive(pid):
                child_terminated = True
            if not grandchild_terminated and not _proc_alive(grandchild_id):
                grandchild_terminated = True
            time.sleep(POLL_INTERVAL_SECONDS)
        details["child_terminated"] = child_terminated
        details["grandchild_terminated"] = grandchild_terminated
        details["all_exited_within_grace"] = bool(child_terminated and grandchild_terminated)
        details["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        if not (child_terminated and grandchild_terminated):
            return CheckResult(
                "process_tree_exit",
                "failed",
                details,
                reason="process group kill did not cover the PTY child and the grandchild within grace",
            )
        return CheckResult("process_tree_exit", "verified", details)
    finally:
        _posix_reap(pid, master_fd)


# ---------------------------------------------------------------------------
# CLI (r0 platform_probe --output form)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pty_probe", description="PTY T0 spike probes (four primitives x dual platform)"
    )
    parser.add_argument("--output", default=None, help="sanitized evidence JSON path")
    args = parser.parse_args(argv)

    checks = [probe_resize(), probe_ctrl_c(), probe_encoding(), probe_process_tree_exit()]
    unsupported: list[dict[str, str]] = []
    generated_by = "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "manual"
    evidence = build_evidence(checks, unsupported, generated_by)
    _leak_self_check(evidence)

    output = Path(args.output) if args.output else DEFAULT_OUTPUT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"probe version {PROBE_VERSION} ({generated_by}) -> {output}")
    for check in checks:
        print(f"  {check.name}: {check.status}" + (f" ({check.reason})" if check.reason else ""))
    failed = [check for check in checks if check.status == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
