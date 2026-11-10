"""Shell command parsing prototype (P2-S14 M2 shell-parsing slices C1+C2, T-C1-064/065).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox."

Three red lines for this package (mirrored in the module docstrings, the
contract tests, and doc/orchestration/progress/T-C1-064.md + T-C1-065.md):

1. advisory only: the parse result feeds advisory hints. The bash prototype
   (C1) feeds the bash.py deny-hint enrichment (C2): quote-masked and
   line-continuation-masked dangerous forms now reach the advisory face,
   with any parse anomaly failing open to the raw regex floor. The
   PowerShell prototype (C2) is an unwired syntax prototype -- its parse
   products enter no tool face. A parse failure keeps the current ask/deny
   path unchanged.
2. not an OS sandbox: a structured decomposition proves nothing about safety
   (CORE-CONTRACTS §5.3: a dangerous-command regex cannot prove safety; the
   deny list is a hint, not a sandbox).
3. pure functions: no I/O, no environment reads, no subprocesses, no global
   mutable state.

The intermediate form is advisory material only; it never gates execution.
"""

from __future__ import annotations

from .bash import parse_bash
from .ir import (
    BashCommand,
    CommandSegment,
    LiteralPart,
    QuoteKind,
    ShellParseError,
    Statement,
    StatementSeparator,
    Token,
    TokenPart,
    VariablePart,
)
from .powershell import parse_powershell

__all__ = [
    "BashCommand",
    "CommandSegment",
    "LiteralPart",
    "QuoteKind",
    "ShellParseError",
    "Statement",
    "StatementSeparator",
    "Token",
    "TokenPart",
    "VariablePart",
    "parse_bash",
    "parse_powershell",
]
