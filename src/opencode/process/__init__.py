"""The controlled subprocess environment (R3-S5; CORE-CONTRACTS §5.3 L276-277, plan §2.5).

Every subprocess the core spawns goes through this package so the §5.3
L276-277 discipline lives in one place:

- :mod:`opencode.process.interpreter` -- explicit interpreter selection
  (Unix bash / Windows PowerShell, availability checked at first use and
  cached; never ``create_subprocess_shell``'s platform default);
- :mod:`opencode.process.runner` -- the controlled run loop: strong
  timeout (default 120 s, 0 disables), cooperative cancellation, process
  -tree termination and reaping (Windows Job Object / Unix process
  group), and continuously drained pipes under an output cap so floods
  cannot dead-lock the run;
- :mod:`opencode.process.winjob` -- the Windows Job Object ctypes
  wrapper (imported only on ``win32``).

Consumers: the ``bash__run`` builtin tool (R3-S5) and ``fs__grep``'s rg
subprocess (the plan §3 item 25 read-loop-timeout consolidation).
"""

from opencode.process.interpreter import (
    DEFAULT_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    Interpreter,
    resolve_interpreter,
)
from opencode.process.runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    KILL_GRACE_S,
    POLL_INTERVAL_S,
    RunResult,
    run_command,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "KILL_GRACE_S",
    "POLL_INTERVAL_S",
    "PROBE_TIMEOUT_S",
    "Interpreter",
    "RunResult",
    "resolve_interpreter",
    "run_command",
]
