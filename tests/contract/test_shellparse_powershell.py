"""P2-S14 M2 shell-parsing slice C2 contract tests: the PowerShell prototype (T-C1-065).

PHASE2-MODULES L66 authority sentence (translated): "Bash and PowerShell each
get a prototype; compare behavior against selected TS cases; a parse failure
keeps the current ask/deny path; AST analysis is not an OS sandbox." This
file pins the PowerShell side through the public face
``opencode.shellparse.parse_powershell`` against the golden corpus
``tests/contract/shellparse_fixtures/shellparse_powershell_golden.json`` (the
C1 ``shellparse_bash_golden.json`` precedent, its own fixture directory).
Zero new TS file:line references -- the PowerShell prototype has no TS
carrier among the four recorded shell sources in doc/source-baseline.json
(shell/scan.ts, shell/select.ts, shell/result.ts, tool/plugin/shell.ts are
the bash comparison face); the PS parser is a Python-side design compared
against PowerShell's documented language semantics.

Same shape as the bash C1 tests (``test_shellparse_bash.py``):

- normal: golden positives -- statement separators (``;``/newline),
  pipelines (incl. the PS implicit newline continuation after ``|``), quote
  metadata per token part, the PS single-quote ``''`` doubling, backtick
  escapes (the PS escape character, vs bash's backslash),
  recognized-never-evaluated variables (incl. ``$?``; ``$1`` stays
  literal), the redirect approximation, comments, blank/empty input;
- failure: golden negatives -- every malformed family raises
  ShellParseError with a stable code and offset/line/column (fail-closed:
  no guessed repair, no partial result); the PS-specific unsupported list
  (subexpression, array/hash literals, splatting, script blocks, type
  literals/indexing, comma arrays, the reserved ``<``, the call operator,
  the pipeline-chain operators) fails closed by design;
- purity: the package imports nothing outside the allowlist (AST audit),
  opens no file while parsing (runtime trap over the whole PS corpus), and
  the intermediate form is frozen (immutability);
- red lines: the PS parse outcome is invisible to the permission face
  (default ask for clean, deny-hint-hitting and malformed commands alike)
  and the prototype is unwired -- a script-level tripwire pins that the
  bash tool face never references the PS parser; the package docstring
  carries the three red lines plus the PS prototype sentence.

(4) disclosures: the redirect-operator tokens are advisory-grade
approximations (digit folds, ``>&``); `` `e `` decodes as ESC (PS 6+
semantics; PS 5.1 would keep ``e`` literal); `` `u{...}`` reaches the
script-block rejection through ``{`` rather than its own diagnosis;
expression mode (assignments, member access, ranges, casts) is not
modeled -- words are argument-mode tokens only.
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
import opencode.tools.builtin.bash as bash_tool_module
from opencode.shellparse import (
    BashCommand,
    CommandSegment,
    LiteralPart,
    QuoteKind,
    ShellParseError,
    Statement,
    Token,
    VariablePart,
    parse_powershell,
)
from opencode.tools.policy import PermissionPolicy

_FIXTURE_PATH = Path(__file__).parent / "shellparse_fixtures" / "shellparse_powershell_golden.json"
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
    """Legal PowerShell commands parse into the pinned structured intermediate form."""
    assert parse_powershell(case["command"]) == _expected(case)


# ---------------------------------------------------------------------------
# failure: the golden negatives (fail-closed with position info)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _NEGATIVE, ids=[str(case["name"]) for case in _NEGATIVE])
def test_golden_negatives_fail_closed_with_position(case: dict[str, Any]) -> None:
    """Malformed input raises the explicit error type -- never a guess, never a partial result."""
    with pytest.raises(ShellParseError) as info:
        parse_powershell(case["command"])
    error = info.value
    assert type(error) is ShellParseError, "the caller sees the exact parse error, not a generic wrapper"
    assert error.code == case["code"]
    assert (error.offset, error.line, error.column) == (case["offset"], case["line"], case["column"])
    assert error.message, "a human-readable diagnosis always accompanies the code"


def test_negative_family_codes_are_the_declared_set() -> None:
    """The PS malformed-family taxonomy reuses the C1 seven verbatim (zero new codes)."""
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
    assert {p.name for p in sources} >= {"__init__.py", "bash.py", "ir.py", "powershell.py"}
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
    """Runtime trap: parsing the whole PS golden corpus never opens a file."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the parser opened a file mid-parse")

    monkeypatch.setattr(builtins, "open", _boom)
    for command in _ALL_COMMANDS:
        try:
            parse_powershell(command)
        except ShellParseError:
            continue


def test_intermediate_form_is_frozen() -> None:
    """No global mutable state starts at the value level: the shared form is immutable."""
    first = parse_powershell("Write-Output 'a b'").statements[0].segments[0].tokens[1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, first).parts = ()


# ---------------------------------------------------------------------------
# red lines: prototype only; the PS parse outcome is invisible to every tool face
# ---------------------------------------------------------------------------


def test_parse_outcome_never_changes_the_ask_face(tmp_path: Path) -> None:
    """L66: a parse failure keeps the current ask/deny path; a parse result gates nothing.

    Three representative PS commands: one parses clean with no deny hint, one
    parses clean AND hits the deny-hint regex, one is malformed (raises). All
    three evaluate to the same default ``ask`` for ``bash__run``: the PS
    parser's outcome is invisible to the permission face.
    """
    policy = PermissionPolicy(boot_id="boot_shellparse_ps")
    cases: list[str] = [
        "Write-Output hi",
        "Write-Output 'mkfs somewhere'",
        "Write-Output 'hi",
    ]
    for command in cases:
        try:
            parse_powershell(command)
        except ShellParseError:
            pass
        decision = policy.evaluate(
            session_id="ses_000001",
            action="bash__run",
            resources=(),
            input_hash="a" * 64,
            working_dir=str(tmp_path),
            side_effects=True,
        )
        assert (decision.effect, decision.source) == ("ask", "default"), command


def test_powershell_prototype_is_unwired_from_the_tool_faces() -> None:
    """The PS parse products enter no tool face: the bash tool never references the PS parser."""
    source = Path(bash_tool_module.__file__).read_text(encoding="utf-8")
    assert "parse_powershell" not in source


def test_package_docstring_carries_the_red_lines_and_the_ps_sentence() -> None:
    """The advisory-only / not-a-sandbox / pure-function red lines and the PS prototype note."""
    doc = (opencode.shellparse.__doc__ or "").lower()
    assert "advisory only" in doc
    assert "not an os sandbox" in doc
    assert "ask/deny" in doc
    assert "powershell" in doc


# ---------------------------------------------------------------------------
# T-C1-066 additions: the `` `u{...}`` negative pin and the source-level
# code-family pin
# ---------------------------------------------------------------------------


def test_unicode_escape_form_reaches_the_script_block_rejection() -> None:
    """T-B-077 S-2: `` `u{...}`` (the PS 6+ unicode escape form) is not decoded
    -- the ``{`` reaches the script-block rejection (no dedicated diagnosis).

    The golden negative ``unicode-escape-form`` pins the face positionally
    (offset 15 = the ``{``); this pin labels the disclosed approximation."""
    with pytest.raises(ShellParseError) as info:
        parse_powershell("Write-Output `u{263A}")
    assert info.value.code == "unsupported-construct"


def _error_call_code_literals(module_filename: str) -> set[str]:
    """AST-scan a parser module: the first-argument literals of every ``self._error(...)`` call."""
    source_path = _PACKAGE_DIR / module_filename
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_error"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            continue
        code = node.args[0]
        assert isinstance(code, ast.Constant) and isinstance(code.value, str), (module_filename, node.lineno)
        codes.add(code.value)
    return codes


def test_source_error_calls_stay_within_the_declared_seven() -> None:
    """T-B-077 S-3: every ``_error(...)`` call site in powershell.py raises a
    code *literal* from the declared seven-code family.

    The corpus pin above covers corpus->codes; this covers source->codes, so
    a future eighth code cannot drift into the parser without golden
    coverage (the closed set is pinned at both ends)."""
    codes = _error_call_code_literals("powershell.py")
    assert codes, "the parser does raise"
    assert codes <= {
        "unclosed-single-quote",
        "unclosed-double-quote",
        "unclosed-brace",
        "empty-variable-name",
        "dangling-escape",
        "empty-command",
        "unsupported-construct",
    }
