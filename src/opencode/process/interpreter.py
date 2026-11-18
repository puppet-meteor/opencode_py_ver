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
``args()`` L164-169 pins the argv shapes (PowerShell
``-NoLogo -NoProfile -NonInteractive -Command``, bash ``-c``);
``win()``/``unix()`` resolution order; ``executable()``'s which+stat
verification. Deviations (quantified, float-reported in the progress
file): no gitbash/COMSPEC/zsh/dash fallbacks (contract names bash and
PowerShell only); no ``/etc/shells`` listing; the probe is a short
``--version`` / ``-Command exit 0`` spawn (select.ts verifies the file
via stat + which without executing it -- a stub alias that stat-successs
but cannot run would pass TS and fail here, which is the point).

T-C1-PT-002 addendum (parity D5): the explicit selection face ports
select.ts ``resolve()``'s config priority (L181 -- the configured shell
resolves first, fail-closed) and ``args()``'s ``cmd`` arm (L166
``["/c", command]``), carried by :data:`SHELL_ENV`. The default chains
stay contract-narrowed as above: the knob is the opt-in path for hosts
that need a different family (e.g. gitbash on Windows for sh-like
semantics), never a silent reordering of the defaults.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

#: TS ``tool/plugin/shell.ts`` L24 ``DEFAULT_TIMEOUT_MS = 2 * 60 * 1_000`` --
#: exactly 120 seconds (CORE-CONTRACTS §5.3 default; zero deviation).
DEFAULT_TIMEOUT_S = 120.0
#: How long the availability probe may take (rg probe precedent: 5 s).
PROBE_TIMEOUT_S = 5.0
#: Conventional absolute fallback when bash is not on PATH (Unix).
_FALLBACK_BASH = "/bin/bash"

#: T-C1-PT-002 (parity D5): the explicit shell-selection face, porting TS
#: ``shell/select.ts`` ``resolve()``'s config priority (L181 --
#: ``if (configShell) return select(configShell, ...)``). A name on PATH or
#: an absolute path; the family is inferred from the executable name (TS
#: ``meta()``/``name()`` precedent), the availability probe applies unchanged,
#: and an unusable value is an explicit diagnostic -- never a silent fall
#: back to the platform default.
SHELL_ENV = "OPENCODE_PY_SHELL"

#: Process-lifetime cache (the rg ``_RG_CACHE`` precedent): one probe per
#: resolution key (the configured value, or the platform default).
_INTERPRETER_CACHE: dict[str, tuple[Interpreter | None, str | None]] = {}


@dataclass(frozen=True)
class Interpreter:
    """One resolved interpreter: the executable and its command shape."""

    executable: str
    family: Literal["bash", "powershell", "cmd"]

    def argv(self, command: str) -> list[str]:
        """The explicit argv for ``command`` (select.ts ``args()`` L164-169).

        T-C1-PT-002 ports the ``cmd`` arm (L166 ``["/c", command]``) next to
        the PowerShell and bash shapes; the family comes from resolution, so
        every arm here mirrors TS exactly.
        """
        if self.family == "powershell":
            return [self.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        if self.family == "cmd":
            return [self.executable, "/c", command]
        return [self.executable, "-c", command]

    @property
    def display_name(self) -> str:
        if self.family == "powershell":
            return "PowerShell"
        if self.family == "cmd":
            return "cmd"
        return "bash"


def _infer_family(executable: str) -> Literal["bash", "powershell", "cmd"]:
    """The family from the executable name (TS ``meta()``/``name()`` order).

    ``pwsh``/``powershell`` are the PowerShell family (``META``'s ``ps``
    entries), ``cmd`` is its own family (``args()``'s first arm), and every
    other name (bash, sh, zsh, dash, ...) is the sh-like ``-c`` family --
    TS ``args()``'s final arm, which covers them all identically.

    The basename is separator-agnostic (both ``/`` and ``\\`` strip the
    directory) -- a deliberate robustness superset over TS ``name()``'s
    platform branch (win32 windowsPath vs posix basename): a Windows-style
    configured path infers the same family on any host, so the inference is
    platform-independent by construction (CI-linux follow-up to T-C1-PT-002:
    ``PurePath`` on POSIX does not treat ``\\`` as a separator).
    """
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    if name in ("pwsh", "powershell"):
        return "powershell"
    if name == "cmd":
        return "cmd"
    return "bash"


def _candidates() -> list[tuple[str, Literal["bash", "powershell"]]]:
    """Resolution order per platform (select.ts win()/unix(), contract-narrowed)."""
    if sys.platform == "win32":
        return [("pwsh", "powershell"), ("powershell", "powershell")]
    return [("bash", "bash")]


def _probe_ok(interpreter: Interpreter) -> bool:
    """A real spawn probe: the resolved executable must run and exit cleanly."""
    if interpreter.family == "powershell":
        probe = [interpreter.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
    elif interpreter.family == "cmd":
        probe = [interpreter.executable, "/c", "exit 0"]
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


def _resolve_configured(value: str) -> tuple[Interpreter | None, str | None]:
    """The explicit selection face (select.ts ``resolve()`` config priority).

    ``shutil.which`` accepts both a bare name (PATH + PATHEXT resolution)
    and an absolute/relative path (file check); the family is inferred from
    the resolved name, the probe applies, and failure is an explicit
    diagnostic naming the configured value -- the default chain is never a
    silent fallback (the §5.3 L276 discipline extended to the opt-in face).
    """
    resolved = shutil.which(value)
    if resolved is None:
        return None, (
            f"the configured {SHELL_ENV} value {value!r} did not resolve to an executable "
            "(PATH lookup and the direct file check both failed)"
        )
    candidate = Interpreter(executable=resolved, family=_infer_family(resolved))
    if not _probe_ok(candidate):
        return None, (
            f"the configured {SHELL_ENV} value {value!r} resolved to {resolved!r} but "
            "did not pass the availability probe"
        )
    return candidate, None


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


def resolve_interpreter(configured: str | None = None) -> tuple[Interpreter | None, str | None]:
    """The cached explicit interpreter (first use = the startup availability check).

    T-C1-PT-002: a configured value (the :data:`SHELL_ENV` face) resolves
    first and fail-closed, exactly TS ``select()``'s config priority; the
    platform default chain runs only without one. The cache key carries the
    configured value so a mid-process change re-probes rather than serving
    a stale executable.
    """
    key = f"configured:{configured}" if configured else ("win32" if sys.platform == "win32" else "unix")
    if key not in _INTERPRETER_CACHE:
        _INTERPRETER_CACHE[key] = _resolve_configured(configured) if configured else _discover()
    return _INTERPRETER_CACHE[key]
