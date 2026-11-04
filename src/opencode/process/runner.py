"""The controlled subprocess runner (R3-S5; CORE-CONTRACTS §5.3 L277, plan §2.5).

One place owns the §5.3 L277 execution discipline for every spawned
subprocess (the bash tool's commands **and** fs__grep's rg, whose old
weak read-loop deadline is folded into this runner -- plan §3 item 25):

- **Strong timeout**: the deadline is enforced by a monotonic-clock poll
  loop that fires whether or not any output arrives (the old rg loop
  only checked the clock when a stdout line arrived, so a silent hang
  never timed out). ``timeout_s=0`` disables the deadline (TS
  ``shell.ts`` L55 semantics).
- **Cooperative cancellation**: a ``should_cancel`` callable is polled
  on the same tick; a cancelled run is killed exactly like a timeout.
- **Process-tree reaping**: Windows assigns the process to a Job Object
  at spawn and terminates the job (descendants inherit membership, so
  the kill is atomic and enumeration-race-free); Unix starts the
  process in a new session (``start_new_session``) and kills the
  process group with SIGKILL. The main process is then reaped with a
  bounded grace; an unreapable process is reported (``reaped=False``)
  so the caller can surface ``effect_status=unknown`` (§5.3 L277: no
  pretending cleanup finished). Descendants are swept even after a
  normal main exit -- Phase 1 has no background-command face, so the
  tree must not outlive the call (TS persistent-pty not ported, plan
  §4 difference table 6).
- **No pipe deadlock under output floods**: stdout and stderr are each
  drained by a dedicated reader thread that keeps reading to EOF even
  after the output cap is reached -- the cap only stops *appending*
  (§5.3 L277 "输出截断后仍排空管道"), the byte count keeps tracking the
  original size, and the main loop never depends on reading a pipe.

The spawn itself is ``create_subprocess_exec``-shaped (a plain argv, no
``shell=True``) and fully synchronous; the bash tool wraps it in
``asyncio.to_thread`` so a long command never blocks the event loop.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import IO

if sys.platform == "win32":
    from opencode.process.winjob import JobObject

#: §5.3 L274 last-sentence default: tool output is capped at 64 KiB with an
#: explicit truncation marker and the original size.
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
#: Main-loop poll cadence: deadline/cancel/exit detection granularity.
POLL_INTERVAL_S = 0.02
#: How long a killed (or exited) process may take to confirm reaping.
KILL_GRACE_S = 5.0
#: How long the reader threads may lag behind process death before the
#: (daemon) threads are abandoned with the buffers read as-is.
_JOIN_GRACE_S = 2.0


class _CappedBuffer:
    """A thread-safe line buffer that keeps draining past its cap.

    Once ``max_bytes`` have been appended, further lines are only counted
    (``original_bytes`` keeps growing) -- truncation is line-granular, and
    the reader thread keeps consuming so the pipe never fills up.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._bytes = 0
        self._truncated = False

    def append_line(self, line: str) -> None:
        encoded = line.encode("utf-8", "replace")
        size = len(encoded)
        with self._lock:
            self._bytes += size
            if self._truncated:
                return
            if self._bytes <= self._max_bytes:
                self._parts.append(line)
                return
            self._truncated = True
            # Show the budget-fitting byte-exact prefix of the overflowing
            # line (a mid-character cut is dropped by the "ignore" decode).
            remaining = self._max_bytes - (self._bytes - size)
            if remaining > 0:
                self._parts.append(encoded[:remaining].decode("utf-8", "ignore"))

    def snapshot(self) -> tuple[str, bool, int]:
        """Returns ``(text, truncated, original_bytes)``."""
        with self._lock:
            return "".join(self._parts), self._truncated, self._bytes


class RunResult:
    """Everything a caller needs from one controlled run (§5.3 L277 faces)."""

    __slots__ = (
        "cancelled",
        "early_stopped",
        "reaped",
        "returncode",
        "stderr",
        "stderr_original_bytes",
        "stderr_truncated",
        "stdout",
        "stdout_original_bytes",
        "stdout_truncated",
        "timed_out",
    )

    def __init__(
        self,
        *,
        stdout: str,
        stderr: str,
        stdout_truncated: bool,
        stderr_truncated: bool,
        stdout_original_bytes: int,
        stderr_original_bytes: int,
        returncode: int | None,
        timed_out: bool,
        cancelled: bool,
        early_stopped: bool,
        reaped: bool,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated
        self.stdout_original_bytes = stdout_original_bytes
        self.stderr_original_bytes = stderr_original_bytes
        self.returncode = returncode
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.early_stopped = early_stopped
        self.reaped = reaped


def _start_reader(
    pipe: IO[str],
    buffer: _CappedBuffer,
    on_line: Callable[[str], bool] | None,
    early_stop: threading.Event,
) -> threading.Thread:
    """Drain ``pipe`` forever (blocking line reads until EOF)."""

    def drain() -> None:
        for line in pipe:
            buffer.append_line(line)
            if on_line is not None and on_line(line):
                early_stop.set()

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    return thread


def _kill_tree(process: subprocess.Popen[str], job: JobObject | None) -> None:
    """Terminate the whole process tree (§5.3 L277: Windows Job Object / Unix process group)."""
    if sys.platform == "win32":
        if job is not None:
            try:
                job.terminate(1)
                return
            except OSError:
                pass  # defensive: fall through to the main-process kill
        process.kill()
    else:
        import os
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)  # pgid == pid (start_new_session)
        except ProcessLookupError:
            pass  # the group is already gone
        except OSError:
            process.kill()  # defensive: fall back to the main process only


def run_command(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 120.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    should_cancel: Callable[[], bool] | None = None,
    on_stdout_line: Callable[[str], bool] | None = None,
) -> RunResult:
    """Run one subprocess to its §5.3 L277-controlled terminal.

    Spawns ``args`` (an explicit argv -- never ``shell=True``), drains
    both pipes continuously, enforces the strong deadline
    (``timeout_s=0`` disables it) and the cooperative cancel signal,
    kills and reaps the process tree on every exit path, and returns the
    capped output with explicit truncation flags and original sizes.
    ``on_stdout_line`` may request an early stop (rg's limit+1 fetch) by
    returning True.
    """
    spawn_env = dict(env) if env is not None else None
    if sys.platform == "win32":
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=spawn_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    else:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=spawn_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    assert process.stdout is not None and process.stderr is not None  # PIPE + text mode

    job: JobObject | None = None
    if sys.platform == "win32":
        job = _capture_in_job(process)

    stdout_buffer = _CappedBuffer(max_output_bytes)
    stderr_buffer = _CappedBuffer(max_output_bytes)
    early_stop = threading.Event()
    readers = (
        _start_reader(process.stdout, stdout_buffer, on_stdout_line, early_stop),
        _start_reader(process.stderr, stderr_buffer, None, early_stop),
    )

    deadline = time.monotonic() + timeout_s if timeout_s > 0 else None
    timed_out = False
    cancelled = False
    early_stopped = False
    while process.poll() is None:
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        if early_stop.is_set():
            early_stopped = True
            break
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(POLL_INTERVAL_S)

    # Every exit path sweeps the tree: a timeout/cancel kill, and a normal
    # exit still reaps orphaned descendants (no background face in Phase 1).
    _kill_tree(process, job)
    reaped = True
    if process.returncode is None:
        try:
            process.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            reaped = False
    returncode = process.returncode
    for reader in readers:
        reader.join(timeout=_JOIN_GRACE_S)
    if job is not None:
        job.close()

    stdout, stdout_truncated, stdout_original = stdout_buffer.snapshot()
    stderr, stderr_truncated, stderr_original = stderr_buffer.snapshot()
    return RunResult(
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        stdout_original_bytes=stdout_original,
        stderr_original_bytes=stderr_original,
        returncode=returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        early_stopped=early_stopped,
        reaped=reaped,
    )


def _capture_in_job(process: subprocess.Popen[str]) -> JobObject | None:
    """Assign the fresh process to a Job Object; None = defensive fallback.

    A process that exited between spawn and capture is not an error (the
    sweep then has nothing to do); any other failure falls back to the
    main-process-only kill (disclosed in the progress file -- the tree
    guarantee degrades instead of failing the spawn).
    """
    try:
        job = JobObject()
    except OSError:
        return None
    try:
        job.capture(process.pid)
    except OSError:
        if process.poll() is not None:
            return job  # already exited: nothing left to capture
        job.close()
        return None
    return job
