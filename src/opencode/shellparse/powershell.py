"""PowerShell command-line prototype parser: ``parse_powershell`` (T-C1-065, P2-S14 M2 slice C2).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox" -- this
module is the PowerShell side. It reuses the C1 intermediate form
(:mod:`opencode.shellparse.ir`) unchanged: ``QuoteKind.SINGLE`` carries PS
single-quote literalness, ``QuoteKind.DOUBLE`` carries the $-expansion
semantics, and the escape-character difference (backtick vs backslash) is
fully absorbed at decode time (escapes land in ``LiteralPart.text`` before
the form exists). No IR shape was added or extended.

Prototype only: the parse result feeds nothing -- PS advisory wiring is not
part of this slice. Three red lines (mirrored in the package docstring, the
tests, and doc/orchestration/progress/T-C1-065.md):

1. advisory only: the result is advisory material; a parse failure keeps the
   current ask/deny path unchanged (the prototype is unwired by design).
2. not an OS sandbox: a structured decomposition proves nothing about safety
   (CORE-CONTRACTS §5.3: a dangerous-command regex cannot prove safety).
3. pure function: no I/O, no environment reads, no subprocesses, no global
   mutable state (pinned by tests/contract/test_shellparse_powershell.py).

Fail-closed scope: malformed input raises ShellParseError with a stable code
plus offset/line/column -- never a guessed repair, never a partial result.
Error codes: the C1 seven reused verbatim (``unclosed-single-quote``,
``unclosed-double-quote``, ``unclosed-brace``, ``empty-variable-name``,
``dangling-escape``, ``empty-command``, ``unsupported-construct``); no
PS-specific code is needed (every PS-only rejection maps to
``unsupported-construct``).

Explicitly rejected PS constructs (fail-closed over approximation, the
drafted unsupported list): the subexpression ``$(...)``, the array expression
``@(...)``, the hashtable literal ``@{...}``, splatting (a leading ``@name``
word), script blocks ``{...}``, type literals and indexing ``[...]``, comma
array literals ``,``, the reserved ``<`` operator, the call operator ``&``,
the pipeline-chain operators ``&&``/``||``, and any variable form beyond the
plain ``$NAME``/``${NAME}``/``$?`` family.

Disclosed prototype approximations (advisory-grade, not exec-grade):

- redirection becomes standalone unquoted tokens (``>``, ``>>``, ``>&`` and
  the ``2>``-style digit folds), mirroring the bash C1 approximation;
- the backtick escapes decode per about_Special_Characters: `` `n `t `r `b
  `f `v `a `0 `` to their control characters, `` `e `` to ESC (PS 6+
  semantics; PS 5.1 would keep ``e`` literal); every other escaped char
  lands literally (the PS rule) and `` ` `` + newline is a removed line
  continuation;
- single-quote ``''`` doubling decodes to one literal ``'`` (the PS rule --
  bash would yield adjacent parts instead);
- variables are recognized, never evaluated; ``$?`` is the only automatic
  variable modeled; a ``$`` before a digit or other non-name char stays
  literal (PS has no positional parameters -- a bash difference), and a lone
  ``$`` stays literal;
- ``@`` is special only at word start (mid-word ``user@host`` stays one
  literal token); ``#`` starts a comment only at word start (``a#b`` stays
  literal);
- a ``|`` at end of line continues the pipeline on the next line (the PS
  implicit line continuation);
- expression mode (assignments, member access, ranges, casts) is not
  modeled -- words are argument-mode tokens only.
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
    Token,
    TokenPart,
    VariablePart,
)

#: The C1 intermediate form reused unchanged; the alias names the PS face.
PowerShellCommand = BashCommand

#: Inline whitespace: separates tokens, never terminates a statement.
_INLINE_BREAK = frozenset(" \t\r\x0b\x0c")
#: Word-breaking chars in unquoted context (newline ends a statement).
_WORD_BREAK = frozenset(" \t\r\n\x0b\x0c;|&(){}[],<>")
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_DIGITS = frozenset("0123456789")
#: PS automatic variables modeled beyond plain names (``$_`` is covered by _NAME_START).
_PS_SPECIAL_VAR_NAMES = frozenset("?")
#: Statement-start constructs that fail closed immediately.
_STATEMENT_REJECT = frozenset("&@([{<,])}")
#: Constructs rejected after a '|' before the next stage is parsed.
_AFTER_PIPE_REJECT = frozenset("({[<,@)}]}")
#: The PS backtick escape table (about_Special_Characters; `` `e `` is PS 6+).
_BACKTICK_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def parse_powershell(command: str) -> PowerShellCommand:
    """Parse a PowerShell command string into the structured intermediate form (pure).

    Fail-closed: any malformed construct raises ShellParseError carrying a
    stable code plus offset/line/column. The result is advisory material only
    and feeds nothing (a syntax prototype).
    """
    return _PowerShellParser(command).parse()


class _PowerShellParser:
    """Single-pass recursive-descent parser over one PowerShell command string."""

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
    def parse(self) -> PowerShellCommand:
        statements: list[Statement] = []
        while True:
            self._skip_blank()
            if self._index >= self._length:
                break
            ch = self._source[self._index]
            if ch in ";|":
                self._error("empty-command", f"no command before {ch!r}", self._index)
            if ch in _STATEMENT_REJECT:
                self._error("unsupported-construct", f"the {ch!r} construct is unsupported", self._index)
            statement = self._parse_statement()
            statements.append(statement)
            if statement.separator is None:
                break
        return PowerShellCommand(tuple(statements))

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
                return Statement(tuple(segments), ";")
            if ch == "&":
                if self._current_at(self._index + 1) == "&":
                    self._error("unsupported-construct", "the '&&' pipeline-chain operator is unsupported", self._index)
                self._error("unsupported-construct", "the '&' call operator is unsupported", self._index)
            if ch == "|":
                if self._current_at(self._index + 1) == "|":
                    self._error("unsupported-construct", "the '||' pipeline-chain operator is unsupported", self._index)
                self._index += 1
                self._skip_blank()
                if self._index >= self._length or self._source[self._index] in ";|&":
                    self._error("empty-command", "no command after '|'", self._index)
                if self._source[self._index] in _AFTER_PIPE_REJECT:
                    self._error("unsupported-construct", "an unsupported construct follows '|'", self._index)
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
            if ch == "@":
                self._error("unsupported-construct", "the '@' expression/splatting family is unsupported", self._index)
            if ch in ")}]":
                self._error("unsupported-construct", f"the unbalanced {ch!r} construct is unsupported", self._index)
            if ch in "({[<,":
                self._error("unsupported-construct", f"the {ch!r} construct is unsupported", self._index)
            if ch == ">":
                tokens.append(self._parse_redirect(tokens))
                continue
            tokens.append(self._parse_word())

    def _parse_redirect(self, tokens: list[Token]) -> Token:
        """A redirection operator token; folds an adjacent all-digit word (``2>`` forms)."""
        digits = ""
        if tokens and self._last_token_end == self._index:
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
        if self._current() == ">":
            self._index += 1
            op = ">>"
        else:
            op = ">"
        if self._current() == "&":
            self._index += 1
            op += "&"
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
                if ch in _WORD_BREAK:
                    break
                if ch == "`":
                    decoded = self._decode_backtick(open_double=-1)
                    if decoded is not None:
                        literal.append(decoded)
                    continue
                if ch == "'":
                    flush(QuoteKind.UNQUOTED)
                    parts.append(self._parse_single_quoted())
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
                # A dangling backtick here means the quote never closed.
                decoded = self._decode_backtick(open_double=open_double)
                if decoded is not None:
                    literal.append(decoded)
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

    def _decode_backtick(self, *, open_double: int) -> str | None:
        """Consume one backtick escape; the decoded char, or None for a line continuation.

        A trailing backtick with nothing after is dangling: unquoted it is a
        ``dangling-escape``; inside a still-open double quote it means the
        quote never closed (``unclosed-double-quote``).
        """
        self._index += 1
        if self._index >= self._length:
            if open_double >= 0:
                self._error("unclosed-double-quote", "unclosed double quote", open_double)
            self._error("dangling-escape", "a trailing backtick has nothing to escape", self._index - 1)
        nxt = self._source[self._index]
        self._index += 1
        if nxt == "\n":
            return None  # a removed line continuation
        return _BACKTICK_ESCAPES.get(nxt, nxt)

    def _parse_single_quoted(self) -> LiteralPart:
        """A single-quoted literal: ``''`` doubling decodes to one ``'`` (the PS rule)."""
        open_quote = self._index
        self._index += 1
        text: list[str] = []
        while True:
            if self._index >= self._length:
                self._error("unclosed-single-quote", "unclosed single quote", open_quote)
            ch = self._source[self._index]
            if ch == "'":
                if self._current_at(self._index + 1) == "'":
                    text.append("'")
                    self._index += 2
                    continue
                self._index += 1
                return LiteralPart("".join(text), QuoteKind.SINGLE)
            text.append(ch)
            self._index += 1

    def _parse_dollar(self, quote: QuoteKind) -> VariablePart | str:
        """Consume a ``$`` construct at ``self._index``; return the part or a literal fallback."""
        start = self._index
        self._index += 1
        nxt = self._current()
        if nxt == "":
            return "$"
        if nxt == "(":
            self._error("unsupported-construct", "the subexpression '$(...)' is unsupported", start)
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
                if name[0] not in _NAME_START:
                    self._error("unsupported-construct", "only the plain '${NAME}' form is supported", start)
                self._index += 1
                return VariablePart(name=name, braced=True, quote=quote)
            self._error("unsupported-construct", "only the plain '${NAME}' form is supported", start)
        if nxt in _PS_SPECIAL_VAR_NAMES:
            self._index += 1
            return VariablePart(name=nxt, braced=False, quote=quote)
        if nxt in _NAME_START:
            self._index += 1
            while self._current() in _NAME_CHARS:
                self._index += 1
            return VariablePart(name=self._source[start + 1 : self._index], braced=False, quote=quote)
        return "$"
