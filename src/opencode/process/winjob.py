"""Windows Job Object wrapper (R3-S5; CORE-CONTRACTS §5.3, plan §2.5).

The contract's first-named Windows process-tree reaping mechanism: every
command's process is assigned to a job object at spawn, the job is created
with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (a backstop: if this process
dies without terminating the job, the kernel kills the job members), and
``TerminateJobObject`` on timeout/cancel kills the whole tree atomically
-- any process spawned by a job member is itself in the job, so there is
no enumeration race (the ``taskkill /T /F`` alternative builds a PID
snapshot and can miss late spawns; job membership is inherited at
creation instead of enumerated).

Selected over ``taskkill /T /F`` (OP-R3-9): atomic termination, no
external process dependency, covers descendants spawned after the kill
starts, and it is the mechanism CORE-CONTRACTS §5.3 names first
("Windows Job Object、Unix process group 或已验证等价方案" -- the tree-reap
test in ``tests/integration/test_tool_bash.py`` is the verification).

ctypes is the only Windows API access available (the project is
stdlib-only); all functions live in kernel32. This module is imported
only on ``win32`` (``runner.py`` guards the import).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - platform guard for mypy/Unix imports
    raise ImportError("opencode.process.winjob is Windows-only")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

#: JobObjectExtendedLimitInformation (class index for SetInformationJobObject).
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
#: Kill every job member when the last job handle closes (kernel backstop).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
#: OpenProcess rights needed to assign a process to a job.
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


#: ``ctypes.wintypes`` has no ``ULONGLONG`` member; this is the same C type.
_ULONGLONG = ctypes.c_ulonglong


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", _ULONGLONG),
        ("WriteOperationCount", _ULONGLONG),
        ("OtherOperationCount", _ULONGLONG),
        ("ReadTransferCount", _ULONGLONG),
        ("WriteTransferCount", _ULONGLONG),
        ("OtherTransferCount", _ULONGLONG),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
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


_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class JobObject:
    """One job object: create → capture(pid) → terminate() → close().

    ``capture`` opens the process by pid (``PROCESS_TERMINATE |
    PROCESS_SET_QUOTA``) and assigns it to the job; descendants spawned
    afterwards inherit job membership (unless they create with
    ``CREATE_BREAKAWAY_FROM_JOB`` and the job allows breakaway, which it
    does not -- ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` is deliberately unset).
    ``terminate`` kills every member atomically with the given exit code.
    """

    def __init__(self) -> None:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle: int | None = handle
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def capture(self, pid: int) -> None:
        """Assign the process ``pid`` (and, by inheritance, its future tree) to this job."""
        process = _kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")
        try:
            ok = _kernel32.AssignProcessToJobObject(self._require_handle(), process)
        finally:
            _kernel32.CloseHandle(process)
        if not ok:
            raise OSError(ctypes.get_last_error(), f"AssignProcessToJobObject({pid}) failed")

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job atomically (timeout/cancel reaping)."""
        if not _kernel32.TerminateJobObject(self._require_handle(), exit_code):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        """Release the job handle; with KILL_ON_JOB_CLOSE this is a last-resort sweep."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _kernel32.CloseHandle(handle)

    def __enter__(self) -> JobObject:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require_handle(self) -> int:
        if self._handle is None:
            raise OSError("the job object handle is already closed")
        return self._handle
