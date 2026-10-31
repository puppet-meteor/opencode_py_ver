"""R0-B platform and local-storage probes (STEP-01-R0.md sections 5-6).

Standalone probes only -- no future modules: everything lives under
``tools/r0/`` and uses nothing beyond the standard library (``fcntl`` on
POSIX, ``msvcrt`` + ``ctypes`` on Windows for the Job Object, ``sqlite3``
for the WAL / OS writer-lock probe). No SessionService/SessionStore, no
runner, no schema, no migrations, no provider changes.

Behavior anchors:
- CORE-CONTRACTS L233-234: explicit interpreter (Unix bash, Windows
  PowerShell; never the platform default shell, never ``shell=True``);
  process trees must be reaped via a Windows Job Object or a Unix process
  group.
- CORE-CONTRACTS L165: one writer process per data directory holding an OS
  file lock; SQLite runs with WAL, ``foreign_keys`` and ``busy_timeout``.
- filesystem.ts:133-146: path resolution is lexical resolve -> containment
  -> realpath -> containment against the realpathed workspace root.
- sqlite.bun.ts:93 (``PRAGMA journal_mode = WAL``) and event/sql.ts:4-25
  (parent/child foreign-key shape) are the storage anchors.

Evidence sanitization: the emitted JSON never contains host paths,
usernames, PIDs or secrets; workspace and temp locations appear only as
``<WORKSPACE>`` / ``<TEMP>`` placeholders. Raw child-process debug output
stays in the system temp directory and is removed with it.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROBE_VERSION = "r0b-1"
GRACE_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
MARKER_TIMEOUT_SECONDS = 20.0
HOLD_TIMEOUT_SECONDS = 30.0
WORKER_TIMEOUT_SECONDS = 40.0
TIMEOUT_SCENARIO_BUDGET_SECONDS = 3.0
BUSY_TIMEOUT_MS = 5000

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "doc" / "evidence" / "r0" / "platform.json"


# ---------------------------------------------------------------------------
# Check result plumbing
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


# ---------------------------------------------------------------------------
# Windows kernel32 plumbing (Job Object + process liveness). All bodies are
# behind `sys.platform` guards: unreachable code is never executed on POSIX
# and mypy suppresses it there, while it is fully checked on Windows.
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
        lib.TerminateProcess.restype = wintypes.BOOL
        lib.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        _KERNEL32_READY = True
    return lib


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


def _win_terminate_pid(pid: int) -> None:
    lib = _kernel32()
    handle = lib.OpenProcess(_WIN_PROCESS_TERMINATE, False, pid)
    if handle:
        try:
            lib.TerminateProcess(handle, 1)
        finally:
            lib.CloseHandle(handle)


def _win_close(handle: int) -> None:
    lib = _kernel32()
    lib.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Check 1: interpreter selection (CORE-CONTRACTS L233)
# ---------------------------------------------------------------------------


def check_interpreter() -> CheckResult:
    """Explicit bash (POSIX) / PowerShell (Windows); never the default shell."""
    if sys.platform == "win32":
        required = "powershell"
        names = ("powershell.exe", "pwsh.exe")
    else:
        required = "bash"
        names = ("bash",)
    searched = list(names)
    exe = next((found for found in (shutil.which(name) for name in names) if found), None)
    if exe is None:
        return CheckResult(
            "interpreter",
            "failed",
            {"required_by_contract": required, "searched": searched},
            reason="required interpreter not found on PATH",
        )
    if sys.platform == "win32":
        version_argv: list[str] = [exe, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]
    else:
        version_argv = [exe, "-c", 'printf %s "$BASH_VERSION"']
    proc = subprocess.run(version_argv, capture_output=True, text=True, timeout=30, check=False)
    version = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    if proc.returncode != 0 or not version:
        return CheckResult(
            "interpreter",
            "failed",
            {"required_by_contract": required, "interpreter": Path(exe).name},
            reason="interpreter version probe failed",
        )
    details = {
        "required_by_contract": required,
        "interpreter": Path(exe).name,
        "version": version,
        "selection": "explicit executable resolved on PATH; argv spawn; platform default shell never used",
        "shell_true_used": False,
        "default_shell_used": False,
    }
    return CheckResult("interpreter", "verified", details)


# ---------------------------------------------------------------------------
# Check 2: process-tree reaping (CORE-CONTRACTS L234)
# ---------------------------------------------------------------------------

_BASH_TREE_SCRIPT = 'printf \'child=%s\\n\' "$$" > "$1"\nsleep 300 &\nprintf \'grandchild=%s\\n\' "$!" > "$2"\nwait\n'

_PS_TREE_SCRIPT = (
    "param($ChildMarker, $GrandchildMarker, $ProceedMarker)\n"
    "$ErrorActionPreference = 'Stop'\n"
    'Set-Content -LiteralPath $ChildMarker -Value ("child=" + $PID)\n'
    "$deadline = (Get-Date).AddSeconds(30)\n"
    "while (-not (Test-Path -LiteralPath $ProceedMarker) -and (Get-Date) -lt $deadline) {\n"
    "  Start-Sleep -Milliseconds 50\n"
    "}\n"
    "if (Test-Path -LiteralPath $ProceedMarker) {\n"
    "  $gc = Start-Process -FilePath 'powershell.exe' "
    "-ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 300' -PassThru -WindowStyle Hidden\n"
    '  Set-Content -LiteralPath $GrandchildMarker -Value ("grandchild=" + $gc.Id)\n'
    "}\n"
    "Start-Sleep -Seconds 300\n"
)


def _wait_for_tree_markers(child_marker: Path, grandchild_marker: Path, timeout: float) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids: dict[str, int] = {}
        if child_marker.exists() and grandchild_marker.exists():
            for marker in (child_marker, grandchild_marker):
                for line in marker.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "=" in line:
                        role, value = line.split("=", 1)
                        pids[role] = int(value)
            if "child" in pids and "grandchild" in pids:
                return pids
        time.sleep(0.05)
    raise TimeoutError("child/grandchild markers not observed in time")


def _await_descendants(
    pids: dict[str, int], grace: float, child: subprocess.Popen[bytes] | None = None
) -> tuple[dict[str, bool], bool, int]:
    """Poll every descendant repeatedly until all exit or the grace budget ends.

    The child shell is checked via its Popen handle (poll reaps the POSIX
    zombie our own subprocess table would otherwise keep "alive" for kill(0)).
    """
    start = time.monotonic()
    while True:
        exited: dict[str, bool] = {}
        for role, pid in pids.items():
            if role == "child_shell" and child is not None:
                exited[role] = child.poll() is not None
            else:
                exited[role] = not _proc_alive(pid)
        if all(exited.values()):
            return exited, True, int((time.monotonic() - start) * 1000)
        if time.monotonic() - start >= grace:
            return exited, False, int((time.monotonic() - start) * 1000)
        time.sleep(POLL_INTERVAL_SECONDS)


def _run_tree_scenario(scenario_dir: Path, scenario: str, interpreter_exe: str) -> dict[str, Any]:
    """Spawn interpreter -> grandchild, kill (cancel or timeout), verify reaping."""
    scenario_dir.mkdir(parents=True, exist_ok=True)
    child_marker = scenario_dir / "child.marker"
    grandchild_marker = scenario_dir / "grandchild.marker"
    proceed_marker = scenario_dir / "proceed.marker"
    outcome: dict[str, Any] = {
        "scenario": scenario,
        "descendants": {"child_shell": False, "grandchild": False},
        "all_exited_within_grace": False,
        "elapsed_ms": None,
        "escalation_used": False,
        "grace_seconds": GRACE_SECONDS,
        "reason": None,
    }
    job = 0
    child: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    log = (scenario_dir / "child.log").open("wb")
    try:
        if sys.platform == "win32":
            job = _win_create_kill_on_close_job()
            script_path = scenario_dir / "tree.ps1"
            script_path.write_text(_PS_TREE_SCRIPT, encoding="utf-8")
            child = subprocess.Popen(
                [
                    interpreter_exe,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    str(child_marker),
                    str(grandchild_marker),
                    str(proceed_marker),
                ],
                cwd=scenario_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            # Assign the child to the job BEFORE releasing the gate that lets
            # it spawn the grandchild: grandchild creation then strictly
            # follows job assignment, so the whole tree is job-scoped.
            _win_assign_process(job, child.pid)
            proceed_marker.write_text("go", encoding="utf-8")
        else:
            child = subprocess.Popen(
                [interpreter_exe, "-c", _BASH_TREE_SCRIPT, "r0b-tree", str(child_marker), str(grandchild_marker)],
                cwd=scenario_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            pids = _wait_for_tree_markers(child_marker, grandchild_marker, MARKER_TIMEOUT_SECONDS)
        except TimeoutError:
            outcome["reason"] = "child/grandchild markers not observed in time"
            return outcome
        if scenario == "timeout":
            remaining = TIMEOUT_SCENARIO_BUDGET_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
        if sys.platform == "win32":
            _win_terminate_job(job)
        else:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        exited, all_exited, elapsed = _await_descendants(
            {"child_shell": pids["child"], "grandchild": pids["grandchild"]}, GRACE_SECONDS, child
        )
        outcome["descendants"] = exited
        outcome["elapsed_ms"] = elapsed
        outcome["all_exited_within_grace"] = all_exited
        if not all_exited:
            # Escalate: SIGKILL the whole group (POSIX) or force-terminate the
            # job and any surviving pid (Windows), then wait one more grace.
            outcome["escalation_used"] = True
            if sys.platform != "win32":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                _win_terminate_job(job)
                for pid in pids.values():
                    _win_terminate_pid(pid)
            exited, all_exited, elapsed = _await_descendants(
                {"child_shell": pids["child"], "grandchild": pids["grandchild"]}, GRACE_SECONDS, child
            )
            outcome["descendants"] = exited
            outcome["elapsed_ms"] = elapsed
            outcome["all_exited_within_grace"] = all_exited
        return outcome
    finally:
        # Best-effort cleanup even on failure paths: no descendant survives.
        if child is not None and child.poll() is None:
            if sys.platform == "win32":
                if job:
                    _win_terminate_job(job)
                _win_terminate_pid(child.pid)
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if child is not None:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if job:
            _win_close(job)
        log.close()


def check_process_tree() -> CheckResult:
    mechanism = (
        "win32 Job Object (JOB_OBJECT_LIMIT_KILL_ON_CLOSE, AssignProcessToJobObject before grandchild "
        "spawn via proceed-gate, TerminateJobObject)"
        if sys.platform == "win32"
        else "posix process group (start_new_session; killpg SIGTERM then SIGKILL)"
    )
    if sys.platform == "win32":
        interpreter_exe = shutil.which("powershell") or shutil.which("pwsh")
    else:
        interpreter_exe = shutil.which("bash")
    if interpreter_exe is None:
        return CheckResult("process_tree", "failed", {"mechanism": mechanism}, reason="interpreter unavailable")
    scenarios: list[dict[str, Any]] = []
    temp_root = Path(tempfile.mkdtemp(prefix="r0b-tree-"))
    try:
        for scenario in ("cancel", "timeout"):
            scenarios.append(_run_tree_scenario(temp_root / scenario, scenario, interpreter_exe))
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    temp_cleanup_verified = not temp_root.exists()
    details: dict[str, Any] = {
        "mechanism": mechanism,
        "tree_shape": "probe -> child interpreter -> grandchild (explicit interpreters, shell never used)",
        "scenarios": scenarios,
        "temp_cleanup_verified": temp_cleanup_verified,
    }
    failed = [s for s in scenarios if not s["all_exited_within_grace"] or s["reason"] is not None]
    if failed or not temp_cleanup_verified:
        reasons = "; ".join(f"{s['scenario']}: {s.get('reason') or 'descendant survived grace period'}" for s in failed)
        return CheckResult("process_tree", "failed", details, reason=reasons or "temp cleanup failed")
    return CheckResult("process_tree", "verified", details)


# ---------------------------------------------------------------------------
# Check 3: workspace path containment (filesystem.ts:133-146)
# ---------------------------------------------------------------------------


def _contains(base: str, target: str) -> bool:
    base_norm = os.path.normcase(os.path.normpath(base))
    target_norm = os.path.normcase(os.path.normpath(target))
    base_prefix = base_norm if base_norm.endswith(os.sep) else base_norm + os.sep
    return target_norm == base_norm or target_norm.startswith(base_prefix)


def resolve_user_path(workspace: str, user_path: str) -> tuple[bool, str]:
    """filesystem.ts order: lexical resolve -> containment -> realpath -> containment(root).

    Rejects NUL bytes, pre-realpath escapes (`..` traversal, absolute paths
    outside), missing/unrealpathable targets, and symlink/junction escapes
    caught by the second containment check against the realpathed root.
    """
    if "\x00" in user_path:
        return False, "nul_byte_in_path"
    absolute = os.path.normpath(os.path.join(workspace, user_path))
    if not _contains(workspace, absolute):
        return False, "escapes_workspace_before_realpath"
    try:
        real = os.path.realpath(absolute, strict=True)
    except OSError:
        return False, "realpath_failed_or_missing"
    real_root = os.path.realpath(workspace, strict=True)
    if not _contains(real_root, real):
        return False, "escapes_workspace_after_realpath"
    return True, "inside_workspace"


def _setup_link(kind: str, target: Path, link: Path) -> str:
    """Create a symlink/junction; classify privilege denial as 'unsupported'."""
    try:
        if kind == "junction":
            if sys.platform != "win32":
                return "not_applicable"
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link)
        return "created"
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            return "unsupported_privilege"
        return f"setup_failed:{type(error).__name__}"


def check_path_containment() -> CheckResult:
    scenarios: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    temp_root = Path(tempfile.mkdtemp(prefix="r0b-path-"))
    try:
        workspace = temp_root / "workspace"
        workspace.mkdir()
        (workspace / "inside.txt").write_text("inside", encoding="utf-8")
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested", encoding="utf-8")
        outside_dir = temp_root / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("outside", encoding="utf-8")

        def run(case: str, user_path: str, expected_allow: bool) -> dict[str, Any]:
            allowed, reason = resolve_user_path(str(workspace), user_path)
            entry: dict[str, Any] = {
                "case": case,
                "expected": "allow" if expected_allow else "deny",
                "actual": "allow" if allowed else "deny",
                "result": "verified" if allowed == expected_allow else "failed",
            }
            if not allowed:
                entry["rejection_reason"] = reason
            scenarios.append(entry)
            return entry

        run("inside_file", "inside.txt", True)
        run("nested_file", "sub/nested.txt", True)
        run("dotdot_traversal", "sub/../../outside/secret.txt", False)
        run("absolute_outside", str(outside_dir / "secret.txt"), False)
        run("missing_target", "missing.txt", False)
        run("nul_byte", "bad\x00name", False)

        # Symlink to an inside file: allowed after realpath.
        good_link = workspace / "good-link.txt"
        state = _setup_link("symlink", workspace / "inside.txt", good_link)
        if state == "created":
            run("symlink_inside_workspace", "good-link.txt", True)
        elif state == "unsupported_privilege":
            scenarios.append(
                {
                    "case": "symlink_inside_workspace",
                    "result": "unsupported",
                    "reason": "symlink creation: SeCreateSymbolicLinkPrivilege not held (winerror 1314)",
                }
            )
            unsupported.append(
                _unsupported_entry(
                    "path_containment", "symlink creation", "SeCreateSymbolicLinkPrivilege not held (winerror 1314)"
                )
            )
        else:
            scenarios.append({"case": "symlink_inside_workspace", "result": "unsupported", "reason": state})
            unsupported.append(_unsupported_entry("path_containment", "symlink creation", state))

        # Symlink escaping the workspace: must be denied by the post-realpath check.
        bad_link = workspace / "escape-link.txt"
        state = _setup_link("symlink", outside_dir / "secret.txt", bad_link)
        if state == "created":
            run("symlink_escape", "escape-link.txt", False)
        elif state == "unsupported_privilege":
            scenarios.append(
                {
                    "case": "symlink_escape",
                    "result": "unsupported",
                    "reason": "symlink creation: SeCreateSymbolicLinkPrivilege not held (winerror 1314)",
                }
            )
            unsupported.append(
                _unsupported_entry(
                    "path_containment",
                    "symlink escape rejection",
                    "cannot create symlink to test rejection (winerror 1314)",
                )
            )
        else:
            scenarios.append({"case": "symlink_escape", "result": "unsupported", "reason": state})
            unsupported.append(_unsupported_entry("path_containment", "symlink escape rejection", state))

        # Junction escaping the workspace (Windows-only capability).
        junction_link = workspace / "escape-junction"
        state = _setup_link("junction", outside_dir, junction_link)
        if state == "created":
            allowed, reason = resolve_user_path(str(workspace), "escape-junction/secret.txt")
            entry = {
                "case": "junction_escape",
                "expected": "deny",
                "actual": "allow" if allowed else "deny",
                "result": "verified" if not allowed else "failed",
            }
            if not allowed:
                entry["rejection_reason"] = reason
            scenarios.append(entry)
        elif state == "not_applicable":
            scenarios.append(
                {"case": "junction_escape", "result": "not_applicable", "reason": "junctions are windows-only"}
            )
        else:
            scenarios.append({"case": "junction_escape", "result": "unsupported", "reason": state})
            unsupported.append(_unsupported_entry("path_containment", "junction escape rejection", state))
    finally:
        # Remove links first so cleanup never touches link targets.
        for name, remover in (
            ("good-link.txt", os.unlink),
            ("escape-link.txt", os.unlink),
            ("escape-junction", os.rmdir),
        ):
            path = workspace / name if (workspace := temp_root / "workspace").exists() else None
            if path is not None and path.exists():
                try:
                    remover(path)
                except OSError:
                    pass
        shutil.rmtree(temp_root, ignore_errors=True)
    temp_cleanup_verified = not temp_root.exists()

    failed = [s for s in scenarios if s.get("result") == "failed"]
    details: dict[str, Any] = {
        "order": "lexical resolve -> containment -> realpath(strict) -> containment(realpathed root)",
        "comparison": "os.path.normcase-aware prefix containment (case-insensitive on Windows)",
        "scenarios": scenarios,
        "temp_cleanup_verified": temp_cleanup_verified,
    }
    if failed:
        reasons = ", ".join(f"{s['case']}: expected {s['expected']} got {s['actual']}" for s in failed)
        return CheckResult("path_containment", "failed", details, reason=reasons)
    status: CheckStatus = "restricted" if unsupported else "verified"
    details["unsupported_capabilities"] = unsupported
    return CheckResult("path_containment", status, details)


# ---------------------------------------------------------------------------
# Check 4: SQLite WAL + OS data-directory writer lock (CORE-CONTRACTS L165)
# ---------------------------------------------------------------------------


def _lock_acquire(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _lock_release(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # best-effort; process exit always releases the OS lock
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _lock_mechanism() -> str:
    if sys.platform == "win32":
        return "msvcrt.locking(LK_NBLCK) on the data-directory lock file (native LockFile region lock)"
    return "fcntl.flock(LOCK_EX | LOCK_NB) on the data-directory lock file (native POSIX advisory lock)"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_snapshot(root: Path) -> dict[str, list[Any]]:
    """Comparable before/after view of a data dir. Sizes/mtimes always; hashes only
    when the OS sharing mode permits a read (live WAL/SHM handles may not)."""
    snapshot: dict[str, list[Any]] = {}
    for path in sorted(root.iterdir()):
        if path.is_file():
            stat = path.stat()
            try:
                digest: str = _sha256(path)
            except OSError:
                digest = "hash_unreadable"
            snapshot[path.name] = [stat.st_size, stat.st_mtime_ns, digest]
    return snapshot


def _writer_main(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    lock_path = data_dir / "writer.lock"
    db_path = data_dir / "app.db"
    out: dict[str, Any]

    if args.writer_mode == "try-acquire":
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            acquired = _lock_acquire(fd)
            if acquired:
                _lock_release(fd)
        finally:
            os.close(fd)
        out = {
            "result": "acquired" if acquired else "conflict",
            "database_opened": False,
            "lock": "granted" if acquired else "denied",
        }
        print(json.dumps(out))
        return 0

    if args.writer_mode == "acquire-die":
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _lock_acquire(fd):
            print(json.dumps({"result": "conflict", "database_opened": False}))
            return 0
        # Simulate a crash while holding the OS lock: no unlock, no close.
        Path(args.held_file).write_text("held", encoding="utf-8")
        time.sleep(0.2)
        os._exit(3)

    # acquire-hold: lock first; only then open the database and write.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock_acquire(fd):
            print(json.dumps({"result": "conflict", "database_opened": False, "lock": "denied", "stage": "lock"}))
            return 0
        import sqlite3

        held_path = Path(args.held_file)
        release_path = Path(args.release_file)
        report: dict[str, Any] = {"result": "ok", "database_opened": True, "lock": "acquired-before-db-open"}
        try:
            connection = sqlite3.connect(db_path, timeout=5.0)
            cursor = connection.cursor()
            report["journal_mode"] = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            cursor.execute("PRAGMA foreign_keys=ON")
            report["foreign_keys"] = cursor.execute("PRAGMA foreign_keys").fetchone()[0]
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            report["busy_timeout_ms"] = cursor.execute("PRAGMA busy_timeout").fetchone()[0]
            if str(report["journal_mode"]).lower() != "wal":
                report["result"] = "error"
                report["stage"] = f"journal_mode={report['journal_mode']}"
                print(json.dumps(report))
                return 1
            # Parent/child shape mirrors event/sql.ts (event_sequence -> event FK).
            cursor.execute("CREATE TABLE event_sequence (aggregate_id TEXT PRIMARY KEY, seq INTEGER NOT NULL)")
            cursor.execute(
                "CREATE TABLE event (id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL "
                "REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE, seq INTEGER NOT NULL, "
                "created INTEGER NOT NULL DEFAULT 0, type TEXT NOT NULL, data TEXT NOT NULL)"
            )
            cursor.execute("INSERT INTO event_sequence VALUES ('agg-1', 1)")
            cursor.executemany(
                "INSERT INTO event VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("ev-1", "agg-1", 1, 0, "py.text.finalized", '{"text_hash":"<sanitized>"}'),
                    ("ev-2", "agg-1", 2, 0, "py.step.finished", '{"ok":true}'),
                ],
            )
            connection.commit()
            report["rows_written"] = cursor.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            # foreign_keys must actually be enforced (not just set).
            try:
                cursor.execute("INSERT INTO event VALUES ('ev-bad', 'missing-agg', 3, 0, 'py.bogus', '{}')")
                connection.commit()
                report["fk_violation_rejected"] = False
            except Exception:
                connection.rollback()
                report["fk_violation_rejected"] = True
            report["rows_before_hold"] = cursor.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            held_path.write_text("held", encoding="utf-8")
            deadline = time.monotonic() + HOLD_TIMEOUT_SECONDS
            while not release_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            report["hold_timed_out"] = not release_path.exists()
            report["rows_after_hold"] = cursor.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            report["wal_file_present"] = (data_dir / "app.db-wal").exists()
            report["db_sha256"] = _sha256(db_path)
            connection.close()
        finally:
            _lock_release(fd)
    finally:
        os.close(fd)
    print(json.dumps(report))
    return 0 if report.get("result") == "ok" else 1


def _run_worker(argv: list[str]) -> dict[str, Any]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=WORKER_TIMEOUT_SECONDS, check=False)
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"result": "worker-error", "returncode": proc.returncode, "stderr_tail": proc.stderr.strip()[-200:]}


def check_sqlite_writer_lock() -> CheckResult:
    probe_path = str(Path(__file__).resolve())
    temp_root = Path(tempfile.mkdtemp(prefix="r0b-db-"))
    data_dir = temp_root / "data"
    details: dict[str, Any] = {
        "lock_mechanism": _lock_mechanism(),
        "third_party_dependency": "none (stdlib fcntl/msvcrt/sqlite3)",
    }

    def worker(mode: str, held: Path | None = None, release: Path | None = None) -> list[str]:
        argv = [sys.executable, probe_path, "--writer-mode", mode, "--data-dir", str(data_dir)]
        if held is not None:
            argv += ["--held-file", str(held)]
        if release is not None:
            argv += ["--release-file", str(release)]
        return argv

    async_worker: subprocess.Popen[bytes] | None = None
    try:
        data_dir.mkdir()
        (data_dir / "writer.lock").write_bytes(b"L")

        # Scenario 1: a writer crashes while holding the OS lock; the OS must
        # release it at process death so the next writer can acquire it.
        died_marker = temp_root / "died.marker"
        died = subprocess.Popen(
            worker("acquire-die", held=died_marker), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        died.wait(timeout=WORKER_TIMEOUT_SECONDS)
        crash_exit_code = died.returncode
        retry = _run_worker(worker("try-acquire"))
        details["crash_release"] = {
            "crashed_exit_code": crash_exit_code,
            "lock_released_by_os_on_death": retry.get("result") == "acquired",
        }

        # Scenario 2: writer 1 holds the lock and the open database; writer 2
        # must fail to acquire the data-directory lock before any write.
        held_marker = temp_root / "held.marker"
        release_marker = temp_root / "release.marker"
        worker1_report = temp_root / "worker1.json"
        with worker1_report.open("wb") as log:
            async_worker = subprocess.Popen(
                worker("acquire-hold", held=held_marker, release=release_marker), stdout=log, stderr=subprocess.STDOUT
            )
        deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
        while not held_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not held_marker.exists():
            # Console-only diagnostics (temp paths stay out of evidence).
            log_tail = worker1_report.read_text(encoding="utf-8", errors="replace") if worker1_report.exists() else ""
            print(f"[sqlite-probe] writer1 poll={async_worker.poll()} log_tail={log_tail[-400:]!r}", file=sys.stderr)
            details["reason"] = "writer 1 never reported holding the lock"
            return CheckResult("sqlite_writer_lock", "failed", details, reason="writer 1 never held the lock")
        before = _dir_snapshot(data_dir)
        writer2 = _run_worker(worker("try-acquire"))
        after = _dir_snapshot(data_dir)
        details["writer2"] = {"result": writer2.get("result"), "database_opened": writer2.get("database_opened")}
        details["no_write_evidence"] = {
            "compared_fields": ["file_name", "size_bytes", "mtime_ns", "sha256_or_unreadable"],
            "dir_snapshot_before_writer2": before,
            "dir_snapshot_after_writer2": after,
            "files_hash_unreadable": sorted(name for name, value in before.items() if value[2] == "hash_unreadable"),
            "snapshots_identical": before == after,
        }
        release_marker.write_text("go", encoding="utf-8")
        async_worker.wait(timeout=WORKER_TIMEOUT_SECONDS)
        report = json.loads(worker1_report.read_text(encoding="utf-8"))
        details["writer1"] = report
        details["no_write_evidence"]["writer1_rows_stable_across_hold"] = report.get("rows_before_hold") == report.get(
            "rows_after_hold"
        )
        details["sqlite"] = {
            "journal_mode": report.get("journal_mode"),
            "foreign_keys": report.get("foreign_keys"),
            "busy_timeout_ms": report.get("busy_timeout_ms"),
            "fk_violation_rejected": report.get("fk_violation_rejected"),
            "schema_shape": "event_sequence -> event parent/child FK (event/sql.ts analog)",
        }
        details["cleanup"] = {"files_left_after_close": sorted(p.name for p in data_dir.iterdir())}
    finally:
        if async_worker is not None:
            if async_worker.poll() is None:
                async_worker.kill()
            async_worker.wait(timeout=10)
        shutil.rmtree(temp_root, ignore_errors=True)
    details["cleanup"]["temp_dir_removed"] = not temp_root.exists()

    ok = (
        details["crash_release"]["lock_released_by_os_on_death"]
        and details["writer2"].get("result") == "conflict"
        and details["writer2"].get("database_opened") is False
        and details["no_write_evidence"]["snapshots_identical"]
        and details["no_write_evidence"]["writer1_rows_stable_across_hold"]
        and report.get("result") == "ok"
        and str(details["sqlite"]["journal_mode"]).lower() == "wal"
        and details["sqlite"]["foreign_keys"] == 1
        and details["sqlite"]["busy_timeout_ms"] == BUSY_TIMEOUT_MS
        and details["sqlite"]["fk_violation_rejected"] is True
        and details["cleanup"]["temp_dir_removed"]
    )
    if not ok:
        return CheckResult(
            "sqlite_writer_lock", "failed", details, reason="one or more writer-lock assertions failed (see details)"
        )
    return CheckResult("sqlite_writer_lock", "verified", details)


# ---------------------------------------------------------------------------
# Evidence assembly
# ---------------------------------------------------------------------------


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
    verified_names = [check.name for check in checks if check.status in ("verified", "restricted")]
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
            "probe": "tools/r0/platform_probe.py",
        },
        "ci_verified": verified_names if generated_by == "ci" else [],
        "manual_verified": verified_names if generated_by == "manual" else [],
        "unsupported": unsupported,
        "checks": {check.name: check.to_dict() for check in checks},
        "notes": [
            "sanitization: host paths <WORKSPACE>/<TEMP> placeholders only; "
            "no usernames, PIDs, prompts, secrets or endpoints recorded",
            "restricted status = capability untestable in this environment with a recorded reason, not a pass",
            "raw child-process debug output stays in the system temp dir and is removed with it",
        ],
    }
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform_probe", description="R0-B platform and local-storage probes")
    parser.add_argument("--output", default=None, help="sanitized evidence JSON path")
    parser.add_argument("--writer-mode", choices=("try-acquire", "acquire-hold", "acquire-die"), default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--held-file", default=None)
    parser.add_argument("--release-file", default=None)
    args = parser.parse_args(argv)

    if args.writer_mode is not None:
        if not args.data_dir:
            print("writer-mode requires --data-dir", file=sys.stderr)
            return 2
        if args.writer_mode in ("acquire-hold", "acquire-die") and not args.held_file:
            print("writer-mode requires --held-file", file=sys.stderr)
            return 2
        if args.writer_mode == "acquire-hold" and not args.release_file:
            print("acquire-hold requires --release-file", file=sys.stderr)
            return 2
        return _writer_main(args)

    checks = [check_interpreter(), check_process_tree(), check_path_containment(), check_sqlite_writer_lock()]
    unsupported: list[dict[str, str]] = []
    for check in checks:
        caps = check.details.get("unsupported_capabilities")
        if isinstance(caps, list):
            unsupported.extend(caps)
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
