"""Bash command-line prototype parser: ``parse_bash`` (T-C1-064, P2-S14 M2 slice C1).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox" -- this
module is the bash side.

Three red lines (mirrored in the package docstring, the tests, and the
progress doc):

1. advisory only: the result feeds advisory hints; wiring into bash.py's
   ``_DENY_HINT_PATTERNS`` face is a later slice (C2). A parse failure keeps
   the current ask/deny path unchanged.
2. not an OS sandbox: a structured decomposition proves nothing about safety
   (CORE-CONTRACTS §5.3: a dangerous-command regex cannot prove safety).
3. pure function: no I/O, no environment reads, no subprocesses, no global
   mutable state (pinned by tests/contract/test_shellparse_bash.py).

Fail-closed scope: malformed input raises ShellParseError with a stable code
plus offset/line/column -- never a guessed repair, never a partial result.
Error codes: ``unclosed-single-quote``, ``unclosed-double-quote``,
``unclosed-brace``, ``empty-variable-name``, ``dangling-escape``,
``empty-command``, ``unsupported-construct``.

Disclosed prototype approximations (advisory-grade, not exec-grade):

- redirection operators become standalone unquoted tokens (``>``, ``>>``,
  ``>&``, and ``2>``-style fd folds when the digit word is adjacent);
- unsupported constructs raise instead of being approximated: command
  substitution (``$(...)``/backticks), ``$'..'``/``$".."`` quoting, subshell
  and group parentheses, the ``&`` background operator, ``;;``, ``|&``,
  ``<>``, ``>|``, heredocs, and any parameter expansion beyond the plain
  ``${NAME}`` form;
- variables are recognized, never evaluated; a lone ``$`` stays literal
  (bash keeps it verbatim too).
"""

from __future__ import annotations

from typing import NoReturn

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

#: Inline whitespace: separates tokens, never terminates a statement.
_INLINE_BREAK = frozenset(" \t\r\x0b\x0c")
#: Word-breaking chars in unquoted context (newline included: it ends a statement).
_WORD_BREAK = frozenset(" \t\r\n\x0b\x0c")
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_DIGITS = frozenset("0123456789")
_SPECIAL_VAR_NAMES = frozenset("@*#?$!")
#: Chars backslash-escapes inside double quotes (bash rule); other backslashes stay literal.
_DOUBLE_QUOTE_ESCAPES = frozenset('$`"\\')


def parse_bash(command: str) -> BashCommand:
    """Parse a bash command string into the structured intermediate form (pure).

    Fail-closed: any malformed construct raises ShellParseError carrying a
    stable code plus offset/line/column. The result is advisory material only.
    """
    return _BashParser(command).parse()


class _BashParser:
    """Single-pass recursive-descent parser over one command string."""

    __slots__ = ("_index", "_last_token_end", "_length", "_source")

    def __init__(self, source: str) -> None:
        self._source = source
        self._index = 0
        self._length = len(source)
        self._last_token_end = -1

    # -- plumbing ---------------------------------------------------------
    def _current(self) -> str:
        if self._index < self._length:
            return self._source[self._index]
        return ""

    def _current_at(self, index: int) -> str:
        if index < self._length:
            return self._source[index]
        return ""

    def _error(self, code: str, message: str, offset: int) -> NoReturn:
        line = self._source.count("\n", 0, offset) + 1
        column = offset - self._source.rfind("\n", 0, offset)
        raise ShellParseError(code=code, message=message, offset=offset, line=line, column=column)

    def _skip_inline(self) -> None:
        """Skip spaces/tabs/CR -- never a newline (a newline ends a statement)."""
        while self._current() in _INLINE_BREAK:
            self._index += 1

    def _skip_blank(self) -> None:
        """Skip all whitespace and comments (between statements / after separators/pipes)."""
        while True:
            ch = self._current()
            if ch in _INLINE_BREAK or ch == "\n":
                self._index += 1
            elif ch == "#":
                while self._current() not in ("", "\n"):
                    self._index += 1
            else:
                return

    # -- top level ---------------------------------------------------------
    def parse(self) -> BashCommand:
        statements: list[Statement] = []
        last_sep: StatementSeparator | None = None
        while True:
            self._skip_blank()
            if self._index >= self._length:
                if last_sep in ("&&", "||"):
                    self._error(
                        "empty-command",
                        "a command is required after '&&'/'||' but the input ends",
                        self._length,
                    )
                break
            ch = self._source[self._index]
            if ch in ";|&":
                self._error("empty-command", f"no command before {ch!r}", self._index)
            if ch in ("(", ")", "`"):
                self._error("unsupported-construct", f"the {ch!r} construct is unsupported", self._index)
            statement = self._parse_statement()
            statements.append(statement)
            last_sep = statement.separator
            if last_sep is None:
                break
        return BashCommand(tuple(statements))

    def _parse_statement(self) -> Statement:
        segments: list[CommandSegment] = [self._parse_segment()]
        while True:
            self._skip_inline()
            ch = self._current()
            if ch == "":
                return Statement(tuple(segments), None)
            if ch == "\n":
                self._index += 1
                return Statement(tuple(segments), "newline")
            if ch == ";":
                self._index += 1
                if self._current() == ";":
                    self._error("unsupported-construct", "the ';;' case terminator is unsupported", self._index)
                return Statement(tuple(segments), ";")
            if ch == "&":
                if self._current_at(self._index + 1) == "&":
                    self._index += 2
                    return Statement(tuple(segments), "&&")
                self._error("unsupported-construct", "the '&' background operator is unsupported", self._index)
            if ch == "|":
                self._index += 1
                nxt = self._current()
                if nxt == "&":
                    self._error("unsupported-construct", "the '|&' combined pipe is unsupported", self._index)
                if nxt == "|":
                    self._index += 1
                    return Statement(tuple(segments), "||")
                self._skip_blank()
                if self._index >= self._length or self._source[self._index] in ";|&":
                    self._error("empty-command", "no command after '|'", self._index)
                if self._source[self._index] in ("(", ")", "`"):
                    self._error("unsupported-construct", "unsupported shell construct after '|'", self._index)
                segments.append(self._parse_segment())
                continue
            raise AssertionError(f"unexpected character {ch!r} at statement boundary (internal invariant)")

    def _parse_segment(self) -> CommandSegment:
        tokens: list[Token] = []
        while True:
            self._skip_inline()
            ch = self._current()
            if ch == "" or ch in ";\n&|":
                return CommandSegment(tuple(tokens))
            if ch == "#":
                while self._current() not in ("", "\n"):
                    self._index += 1
                continue
            if ch in ("`", "(", ")"):
                self._error("unsupported-construct", f"the {ch!r} construct is unsupported", self._index)
            if ch in "<>":
                tokens.append(self._parse_redirect(tokens))
                continue
            tokens.append(self._parse_word())

    def _parse_redirect(self, tokens: list[Token]) -> Token:
        """A redirection operator token; folds an adjacent all-digit word (``2>`` forms)."""
        start = self._index
        ch = self._source[self._index]
        digits = ""
        if tokens and self._last_token_end == start:
            last = tokens[-1]
            if len(last.parts) == 1 and isinstance(last.parts[0], LiteralPart):
                candidate = last.parts[0]
                if (
                    candidate.quote is QuoteKind.UNQUOTED
                    and candidate.text
                    and all(c in _DIGITS for c in candidate.text)
                ):
                    digits = candidate.text
                    tokens.pop()
        self._index += 1
        if ch == "<":
            nxt = self._current()
            if nxt == "(":
                self._error("unsupported-construct", "process substitution is unsupported", start)
            if nxt == "<":
                self._error("unsupported-construct", "heredocs are unsupported", start)
            if nxt == ">":
                self._error("unsupported-construct", "the '<>' operator is unsupported", start)
            op = "<"
        else:
            if self._current() == "(":
                self._error("unsupported-construct", "process substitution is unsupported", start)
            if self._current() == ">":
                self._index += 1
                op = ">>"
            else:
                op = ">"
            if self._current() == "&":
                self._index += 1
                op += "&"
            if self._current() == "|":
                self._error("unsupported-construct", "the '>|' clobber operator is unsupported", start)
        return Token((LiteralPart(digits + op, QuoteKind.UNQUOTED),))

    def _parse_word(self) -> Token:
        parts: list[TokenPart] = []
        literal: list[str] = []
        quote: QuoteKind | None = None  # the context ``literal`` accumulates in
        open_double = -1  # offset of a still-open double quote (error anchor)

        def flush(active: QuoteKind) -> None:
            if literal:
                parts.append(LiteralPart("".join(literal), active))
                literal.clear()

        while True:
            if self._index >= self._length:
                if quote is QuoteKind.DOUBLE:
                    self._error("unclosed-double-quote", "unclosed double quote", open_double)
                break
            ch = self._source[self._index]
            if quote is None:
                if ch in _WORD_BREAK or ch in ";|&<>":
                    break
                if ch == "\\":
                    self._index += 1
                    if self._index >= self._length:
                        self._error("dangling-escape", "a trailing backslash has nothing to escape", self._index - 1)
                    nxt = self._source[self._index]
                    self._index += 1
                    if nxt != "\n":
                        literal.append(nxt)
                    continue
                if ch == "'":
                    flush(QuoteKind.UNQUOTED)
                    start = self._index
                    self._index += 1
                    close = self._source.find("'", self._index)
                    if close == -1:
                        self._error("unclosed-single-quote", "unclosed single quote", start)
                    parts.append(LiteralPart(self._source[self._index : close], QuoteKind.SINGLE))
                    self._index = close + 1
                    continue
                if ch == '"':
                    flush(QuoteKind.UNQUOTED)
                    open_double = self._index
                    self._index += 1
                    quote = QuoteKind.DOUBLE
                    continue
                if ch == "$":
                    outcome = self._parse_dollar(QuoteKind.UNQUOTED)
                    if isinstance(outcome, str):
                        literal.append(outcome)
                    else:
                        flush(QuoteKind.UNQUOTED)
                        parts.append(outcome)
                    continue
                if ch == "`":
                    self._error("unsupported-construct", "backtick command substitution is unsupported", self._index)
                if ch in ("(", ")"):
                    self._error("unsupported-construct", "subshell/group parentheses are unsupported", self._index)
                literal.append(ch)
                self._index += 1
                continue
            # inside double quotes
            if ch == '"':
                flush(QuoteKind.DOUBLE)
                self._index += 1
                quote = None
                continue
            if ch == "`":
                self._error("unsupported-construct", "backtick command substitution is unsupported", self._index)
            if ch == "\\":
                nxt = self._current_at(self._index + 1)
                if nxt in _DOUBLE_QUOTE_ESCAPES:
                    self._index += 2
                    literal.append(nxt)
                    continue
                if nxt == "\n":
                    self._index += 2
                    continue
                literal.append(ch)  # bash keeps a backslash literal before other chars
                self._index += 1
                continue
            if ch == "$":
                outcome = self._parse_dollar(QuoteKind.DOUBLE)
                if isinstance(outcome, str):
                    literal.append(outcome)
                else:
                    flush(QuoteKind.DOUBLE)
                    parts.append(outcome)
                continue
            literal.append(ch)
            self._index += 1
        flush(quote if quote is not None else QuoteKind.UNQUOTED)
        self._last_token_end = self._index
        return Token(tuple(parts))

    def _parse_dollar(self, quote: QuoteKind) -> VariablePart | str:
        """Consume a ``$`` construct at ``self._index``; return the part or a literal fallback."""
        start = self._index
        self._index += 1
        nxt = self._current()
        if nxt == "":
            return "$"
        if nxt == "(":
            self._error("unsupported-construct", "command substitution '$(...)' is unsupported", start)
        if nxt == "'":
            self._error("unsupported-construct", "ANSI-C quoting $'...' is unsupported", start)
        if nxt == '"':
            self._error("unsupported-construct", 'locale quoting $"..." is unsupported', start)
        if nxt == "{":
            self._index += 1
            name_start = self._index
            while self._current() in _NAME_CHARS:
                self._index += 1
            name = self._source[name_start : self._index]
            closer = self._current()
            if closer == "":
                self._error("unclosed-brace", "unclosed '${'", start)
            if closer == "}":
                if not name:
                    self._error("empty-variable-name", "'${}' has an empty variable name", start)
                if name[0] not in _NAME_START and not all(c in _DIGITS for c in name):
                    self._error("unsupported-construct", "only the plain '${NAME}' form is supported", start)
                self._index += 1
                return VariablePart(name=name, braced=True, quote=quote)
            self._error("unsupported-construct", "only the plain '${NAME}' form is supported", start)
        if nxt in _SPECIAL_VAR_NAMES or nxt in _DIGITS:
            self._index += 1
            return VariablePart(name=nxt, braced=False, quote=quote)
        if nxt in _NAME_START:
            self._index += 1
            while self._current() in _NAME_CHARS:
                self._index += 1
            return VariablePart(name=self._source[start + 1 : self._index], braced=False, quote=quote)
        return "$"
