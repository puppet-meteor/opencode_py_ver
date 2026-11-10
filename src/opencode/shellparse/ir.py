"""Structured intermediate form for the shell-parse prototype (T-C1-064).

The shape is advisory material only. Three red lines (mirrored in the package
docstring, the tests, and doc/orchestration/progress/T-C1-064.md):

1. advisory only -- a parse result feeds hints; a parse failure keeps the
   current ask/deny path unchanged;
2. a structured decomposition is not an OS sandbox and proves nothing about
   safety;
3. the form is produced by pure functions: no I/O, no environment reads, no
   subprocesses, no global mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class QuoteKind(Enum):
    """The quoting context a token contribution appeared in."""

    UNQUOTED = "unquoted"
    SINGLE = "single"
    DOUBLE = "double"


#: How one statement was terminated; ``None`` only on the final statement.
StatementSeparator = Literal[";", "&&", "||", "newline"]


@dataclass(frozen=True, slots=True)
class LiteralPart:
    """A decoded literal contribution to a word (escapes already applied)."""

    text: str
    quote: QuoteKind


@dataclass(frozen=True, slots=True)
class VariablePart:
    """A recognized-but-never-evaluated variable reference (``$VAR``/``${VAR}``)."""

    name: str
    braced: bool
    quote: QuoteKind


TokenPart = LiteralPart | VariablePart


@dataclass(frozen=True, slots=True)
class Token:
    """One argv-shaped word: a sequence of parts with quoting metadata."""

    parts: tuple[TokenPart, ...]

    @property
    def text(self) -> str:
        """The decoded word with variables rendered back in source form."""
        chunks: list[str] = []
        for part in self.parts:
            if isinstance(part, VariablePart):
                chunks.append(f"${{{part.name}}}" if part.braced else f"${part.name}")
            else:
                chunks.append(part.text)
        return "".join(chunks)


@dataclass(frozen=True, slots=True)
class CommandSegment:
    """One pipeline stage: the tokens between two ``|`` boundaries."""

    tokens: tuple[Token, ...]

    @property
    def argv(self) -> tuple[str, ...]:
        """The argv-shaped view (variables rendered in source form, not evaluated)."""
        return tuple(token.text for token in self.tokens)


@dataclass(frozen=True, slots=True)
class Statement:
    """One command (possibly a pipeline) plus how it was terminated."""

    segments: tuple[CommandSegment, ...]
    separator: StatementSeparator | None


@dataclass(frozen=True, slots=True)
class BashCommand:
    """The top-level parse result: statements in source order."""

    statements: tuple[Statement, ...]


class ShellParseError(ValueError):
    """Explicit fail-closed parse error: never a guess, never a partial result."""

    def __init__(self, code: str, message: str, offset: int, line: int, column: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset
        self.line = line
        self.column = column

    def __str__(self) -> str:
        return f"{self.message} ({self.code} at line {self.line}, column {self.column}, offset {self.offset})"
