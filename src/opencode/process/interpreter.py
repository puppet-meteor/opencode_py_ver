"""Explicit interpreter selection (R3-S5; CORE-CONTRACTS §5.3 L276, plan §2.5).

CORE-CONTRACTS §5.3 L276 requires the shell to select the interpreter
explicitly -- bash on Unix, PowerShell on Windows -- and to check
availability at startup, never relying on the platform-default
interpreter that ``create_subprocess_shell`` would use.
Every command is constructed as ``[executable, *interpreter_args, command]``
for :func:`subprocess.Popen` **without** ``shell=True`` -- the platform
default interpreter of ``create_subprocess_shell`` is never used.

Windows resolves ``pwsh`` then ``powershell`` (TS ``shell/select.ts``
``win()`` order's first two entries; the gitbash/COMSPEC fallbacks are
not ported -- the contract names PowerShell as the Windows interpreter).
Unix resolves ``bash`` on PATH, then the conventional ``/bin/bash``
(select.ts probes the environment shell first; the contract pins bash,
so SHELL/zsh/dash probing is not ported). An unavailable interpreter is
an explicit diagnostic, never a silent fallback to another shell.

The availability check follows the fs__grep ripgrep precedent (plan §2.3,
OP-R3-1 conservative reading): PATH discovery plus a real spawn probe of
the resolved executable, cached for the process lifetime, first use =
the "startup" check for a lazily assembled host.

TS mapping (recorded fingerprint #102): ``shell/select.ts`` (227L) --
``args()`` L206-209 pins the argv shapes (PowerShell
``-NoLogo -NoProfile -NonInteractive -Command``, bash ``-c``);
``win()``/``unix()`` resolution order; ``executable()``'s which+stat
verification. Deviations (quantified, float-reported in the progress
file): no gitbash/COMSPEC/zsh/dash fallbacks (contract names bash and
PowerShell only); no ``/etc/shells`` listing; the probe is a short
``--version`` / ``-Command exit 0`` spawn (select.ts verifies the file
via stat + which without executing it -- a stub alias that stat-successs
but cannot run would pass TS and fail here, which is the point).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

#: TS ``tool/plugin/shell.ts`` L24 ``DEFAULT_TIMEOUT_MS = 2 * 60 * 1_000`` --
#: exactly 120 seconds (CORE-CONTRACTS §5.3 L277 default; zero deviation).
DEFAULT_TIMEOUT_S = 120.0
#: How long the availability probe may take (rg probe precedent: 5 s).
PROBE_TIMEOUT_S = 5.0
#: Conventional absolute fallback when bash is not on PATH (Unix).
_FALLBACK_BASH = "/bin/bash"

#: Process-lifetime cache (the rg ``_RG_CACHE`` precedent): one probe per process.
_INTERPRETER_CACHE: dict[str, tuple[Interpreter | None, str | None]] = {}


@dataclass(frozen=True)
class Interpreter:
    """One resolved interpreter: the executable and its command shape."""

    executable: str
    family: Literal["bash", "powershell"]

    def argv(self, command: str) -> list[str]:
        """The explicit argv for ``command`` (select.ts ``args()`` L206-209)."""
        if self.family == "powershell":
            return [self.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        return [self.executable, "-c", command]

    @property
    def display_name(self) -> str:
        return "PowerShell" if self.family == "powershell" else "bash"


def _candidates() -> list[tuple[str, Literal["bash", "powershell"]]]:
    """Resolution order per platform (select.ts win()/unix(), contract-narrowed)."""
    if sys.platform == "win32":
        return [("pwsh", "powershell"), ("powershell", "powershell")]
    return [("bash", "bash")]


def _probe_ok(interpreter: Interpreter) -> bool:
    """A real spawn probe: the resolved executable must run and exit cleanly."""
    if interpreter.family == "powershell":
        probe = [interpreter.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
    else:
        probe = [interpreter.executable, "--version"]
    try:
        completed = subprocess.run(probe, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _resolve_one(name: str, family: Literal["bash", "powershell"]) -> Interpreter | None:
    executable = shutil.which(name)
    if executable is not None:
        candidate = Interpreter(executable=executable, family=family)
        if _probe_ok(candidate):
            return candidate
        return None
    if family == "bash":
        # select.ts stat-verifies /bin/bash without PATH; probe it the same way.
        candidate = Interpreter(executable=_FALLBACK_BASH, family="bash")
        if _probe_ok(candidate):
            return candidate
    return None


def _discover() -> tuple[Interpreter | None, str | None]:
    """Returns ``(interpreter, None)`` or ``(None, problem)`` -- never a silent fallback."""
    for name, family in _candidates():
        resolved = _resolve_one(name, family)
        if resolved is not None:
            return resolved, None
    if sys.platform == "win32":
        problem = (
            "no PowerShell interpreter was found (neither pwsh nor powershell resolved on PATH "
            "and passed the availability probe)"
        )
    else:
        problem = "bash was not found on PATH or at /bin/bash and did not pass the availability probe"
    return None, problem


def resolve_interpreter() -> tuple[Interpreter | None, str | None]:
    """The cached explicit interpreter (first use = the startup availability check)."""
    key = "win32" if sys.platform == "win32" else "unix"
    if key not in _INTERPRETER_CACHE:
        _INTERPRETER_CACHE[key] = _discover()
    return _INTERPRETER_CACHE[key]
