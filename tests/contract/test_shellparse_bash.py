"""P2-S14 M2 shell-parsing slice C1 contract tests: the bash prototype (T-C1-064).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox." This
file pins the bash side through the public face ``opencode.shellparse.parse_bash``
against the golden corpus
``tests/contract/shellparse_fixtures/shellparse_bash_golden.json`` (its own
fixture directory, not the event-envelope corpus under ``fixtures/`` that
``test_schema_lock.py`` classifies -- the ``skills_fixtures/`` precedent).
The corpus is the
self-anchored comparison material for the TS shell faces already recorded in
doc/source-baseline.json (shell/scan.ts, shell/select.ts, shell/result.ts,
tool/plugin/shell.ts) -- zero new TS file:line references; those four files
are quoted, not re-cited.

Face map (per the ticket's test discipline):

- normal: golden positives -- statement separators (``;``/``&&``/``||``/
  newline), pipelines, quote metadata per token part, escapes, recognized-
  never-evaluated variables, the redirect-operator approximation, comments,
  blank/empty input;
- failure: golden negatives -- every malformed family raises ShellParseError
  with a stable code and offset/line/column (fail-closed: no guessed repair,
  no partial result);
- purity: the package imports nothing outside the allowlist (AST audit),
  opens no file while parsing (runtime trap over the whole corpus), and the
  intermediate form is frozen (immutability);
- red lines: parse failure or a deny-hint hit leaves the policy default ask
  unchanged (advisory never blocks) and the bash.py advisory face untouched;
  the package docstring must carry the three red lines.

(4) disclosures: the ``2>&1``-style fd fold and redirect-operator tokens are
advisory-grade approximations; unsupported constructs (command substitution,
ANSI-C/locale quoting, heredocs, subshell parentheses, ``&``, ``;;``,
``|&``, non-plain parameter expansion) fail closed by design instead of
being approximated.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import json
from pathlib import Path
from typing import Any, cast

import pytest

import opencode.shellparse
from opencode.shellparse import (
    BashCommand,
    CommandSegment,
    LiteralPart,
    QuoteKind,
    ShellParseError,
    Statement,
    Token,
    VariablePart,
    parse_bash,
)
from opencode.tools.builtin.bash import _deny_label
from opencode.tools.policy import PermissionPolicy

_FIXTURE_PATH = Path(__file__).parent / "shellparse_fixtures" / "shellparse_bash_golden.json"
_FIXTURES: dict[str, list[dict[str, Any]]] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
_POSITIVE = _FIXTURES["positive"]
_NEGATIVE = _FIXTURES["negative"]
_ALL_COMMANDS = [case["command"] for case in _POSITIVE] + [case["command"] for case in _NEGATIVE]


def _quote(value: dict[str, Any]) -> QuoteKind:
    return QuoteKind(value.get("quote", "unquoted"))


def _part(value: dict[str, Any]) -> LiteralPart | VariablePart:
    if "literal" in value:
        return LiteralPart(text=value["literal"], quote=_quote(value))
    return VariablePart(name=value["variable"], braced=value["braced"], quote=_quote(value))


def _expected(value: dict[str, Any]) -> BashCommand:
    statements = tuple(
        Statement(
            segments=tuple(
                CommandSegment(tokens=tuple(Token(parts=tuple(_part(p) for p in t["parts"])) for t in seg["tokens"]))
                for seg in st["segments"]
            ),
            separator=st["separator"],
        )
        for st in value["statements"]
    )
    return BashCommand(statements=statements)


# ---------------------------------------------------------------------------
# normal: the golden positives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _POSITIVE, ids=[str(case["name"]) for case in _POSITIVE])
def test_golden_positives_decompose_as_expected(case: dict[str, Any]) -> None:
    """Legal commands parse into the pinned structured intermediate form."""
    assert parse_bash(case["command"]) == _expected(case)


# ---------------------------------------------------------------------------
# failure: the golden negatives (fail-closed with position info)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _NEGATIVE, ids=[str(case["name"]) for case in _NEGATIVE])
def test_golden_negatives_fail_closed_with_position(case: dict[str, Any]) -> None:
    """Malformed input raises the explicit error type -- never a guess, never a partial result."""
    with pytest.raises(ShellParseError) as info:
        parse_bash(case["command"])
    error = info.value
    assert type(error) is ShellParseError, "the caller sees the exact parse error, not a generic wrapper"
    assert error.code == case["code"]
    assert (error.offset, error.line, error.column) == (case["offset"], case["line"], case["column"])
    assert error.message, "a human-readable diagnosis always accompanies the code"


def test_negative_family_codes_are_the_declared_set() -> None:
    """The malformed-family taxonomy is closed and documented (the ticket's fail-closed scope)."""
    assert {case["code"] for case in _NEGATIVE} == {
        "unclosed-single-quote",
        "unclosed-double-quote",
        "unclosed-brace",
        "empty-variable-name",
        "dangling-escape",
        "empty-command",
        "unsupported-construct",
    }


# ---------------------------------------------------------------------------
# purity: no I/O, no environment reads, no subprocesses, no global state
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(opencode.shellparse.__file__).parent
_ALLOWED_MODULES = {"__future__", "dataclasses", "enum", "typing"}
_FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}


def test_purity_import_allowlist_and_no_io_calls() -> None:
    """AST audit: only stdlib typing/dataclass/enum imports; no io calls; no global state."""
    sources = sorted(_PACKAGE_DIR.glob("*.py"))
    assert {p.name for p in sources} >= {"__init__.py", "bash.py", "ir.py"}
    for source_path in sources:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name in _ALLOWED_MODULES for alias in node.names), (source_path, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert node.level > 0 or module in _ALLOWED_MODULES, (source_path, node.lineno)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                raise AssertionError(f"global-state declaration at {source_path}:{node.lineno}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _FORBIDDEN_CALLS, (source_path, node.lineno)


def test_purity_parser_never_touches_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime trap: parsing the whole golden corpus never opens a file."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the parser opened a file mid-parse")

    monkeypatch.setattr(builtins, "open", _boom)
    for command in _ALL_COMMANDS:
        try:
            parse_bash(command)
        except ShellParseError:
            continue


def test_intermediate_form_is_frozen() -> None:
    """No global mutable state starts at the value level: the form is immutable."""
    first = parse_bash("echo 'a b'").statements[0].segments[0].tokens[1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, first).parts = ()


# ---------------------------------------------------------------------------
# red lines: advisory only; parse failure keeps the ask/deny path
# ---------------------------------------------------------------------------


def test_parse_outcome_never_changes_the_ask_face_or_the_advisory_hints(tmp_path: Path) -> None:
    """L66: a parse failure keeps the current ask/deny path; an advisory hint never blocks.

    Three representative commands: one parses clean with no deny hint, one
    parses clean AND hits the deny-hint regex, one is malformed (raises). All
    three evaluate to the same default ``ask`` for ``bash__run``: the parser's
    outcome is invisible to the permission face, and the deny hint stays
    advisory -- never a deny.
    """
    policy = PermissionPolicy(boot_id="boot_shellparse")
    cases: list[tuple[str, str | None]] = [
        ("ls", None),
        ("rm -rf /", "recursive root/home deletion"),
        ("echo 'hi", None),
    ]
    for command, expected_label in cases:
        try:
            parse_bash(command)
        except ShellParseError:
            pass
        assert _deny_label(command) == expected_label, command
        decision = policy.evaluate(
            session_id="ses_000001",
            action="bash__run",
            resources=(),
            input_hash="a" * 64,
            working_dir=str(tmp_path),
            side_effects=True,
        )
        assert (decision.effect, decision.source) == ("ask", "default"), command


def test_package_docstring_carries_the_three_red_lines() -> None:
    """The advisory-only / not-a-sandbox / pure-function red lines are pinned in the docs."""
    doc = (opencode.shellparse.__doc__ or "").lower()
    assert "advisory only" in doc
    assert "not an os sandbox" in doc
    assert "ask/deny" in doc
