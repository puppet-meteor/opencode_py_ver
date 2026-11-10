"""Shell command parsing prototype (P2-S14 M2 shell-parsing slice C1, T-C1-064).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox."

Three red lines for this package (mirrored in the module docstrings, the
contract tests, and doc/orchestration/progress/T-C1-064.md):

1. advisory only: the parse result feeds advisory hints; the wiring into
   bash.py's deny-hint face is a later slice (C2). A parse failure keeps the
   current ask/deny path unchanged.
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
]
