"""The v2 step-condition evaluator (T-C1-084): a safe eval subset, no ``eval``.

A faithful port of ``agent_dsl_flow_framework/relay/dag_engine/dsl/
condition_eval.py`` (the framework's own predicate grammar, re-anchored for
the ported pipeline engine): a tiny expression language over dotted refs --

    step_1.ready is true
    step_1.ready isNot true
    step_1.count > 0
    status is 'completed' or error_message isNot empty

Design pins (ported, do not drift):

* **No arbitrary eval** -- the grammar is a hand parser (top-level
  ``or``/``and`` splits, one comparison per atom); unsupported syntax raises
  :class:`ConditionSyntaxError` so a DSL mistake is never silently treated
  as a skip.
* **Missing-field semantics** -- an unresolvable left ref is the ``MISSING``
  sentinel, not an error: ``is null``/``is empty`` match it, ``is`` a value
  does not, ordered comparisons only survive via ``!=``. The engine's
  runtime resolver deliberately raises
  :class:`~opencode.orchestrator.workers.WorkerError` for refs onto skipped /
  not-yet-completed steps -- ``WorkerError`` is not a
  ``KeyError``/``TypeError``/``ValueError``, so it propagates out of the
  evaluator as the honest node failure instead of degrading to "missing".
* **Tolerant key lookup** -- the dict walk falls back camelCase <->
  snake_case at every level (framework parity; the params/prompt/extract
  walks are exact -- a documented asymmetry).

Public faces: :func:`evaluate_step_condition` / :func:`extract_condition_refs`
(the ported pair), :func:`validate_condition_syntax` (the registration-gate
syntax check) and :func:`resolve_ref` (the tolerant dotted-path resolver the
engine's condition resolver delegates to).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ConditionSyntaxError(ValueError):
    """Raised when a condition expression uses unsupported syntax."""


@dataclass(frozen=True, slots=True)
class ConditionEvaluation:
    """One evaluation outcome: the verdict, a human-readable reason, the refs."""

    passed: bool
    reason: str
    refs: list[str]


#: The in-band "field not present" sentinel (distinct from a legit ``None``).
MISSING = object()

_STEP_OR_INPUT_REF_RE = re.compile(r"\b(?:inputs\.\w[\w.]*|step_[0-9][0-9A-Za-z_]*\.\w[\w.]*)\b")
_ATOM_RE = re.compile(
    r"^\s*(?P<left>[A-Za-z_]\w*(?:\.\w[\w.]*)?)\s*"
    r"(?P<op>isNot|is\s+not|is|==|!=|>=|<=|>|<)\s*"
    r"(?P<right>.+?)\s*$",
    flags=re.IGNORECASE,
)


def evaluate_step_condition(
    condition: str | None,
    context: dict[str, Any],
    resolver: Callable[[str, dict[str, Any]], Any] | None = None,
) -> ConditionEvaluation:
    """Evaluate a workflow condition against *context*.

    Empty conditions pass.  Field-level misses use missing/empty semantics, but
    unsupported syntax raises ``ConditionSyntaxError`` so DSL mistakes are not
    silently treated as skips.  Callers may supply a resolver for runtime-aware
    context lookup; lookup failures retain the existing missing-field semantics
    (only ``KeyError``/``TypeError``/``ValueError`` degrade to missing -- a
    ``WorkerError`` from the engine's skip guards propagates as a failure).
    """
    if not condition or not str(condition).strip():
        return ConditionEvaluation(True, "no condition", [])
    expr = str(condition).strip()
    _validate_expr_syntax(expr)
    ref_resolver = resolver if resolver is not None else resolve_ref
    passed = bool(_eval_expr(expr, context or {}, ref_resolver))
    reason = f"condition evaluated to true: {expr}" if passed else f"condition evaluated to false: {expr}"
    return ConditionEvaluation(passed, reason, extract_condition_refs(expr))


def extract_condition_refs(condition: str | None) -> list[str]:
    """Return step/input refs mentioned in *condition* in occurrence order."""
    if not condition:
        return []
    refs: list[str] = []
    for match in _STEP_OR_INPUT_REF_RE.finditer(str(condition)):
        ref = match.group(0)
        if ref not in refs and _outside_quotes(str(condition), match.start()):
            refs.append(ref)
    return refs


def validate_condition_syntax(condition: str) -> None:
    """The registration-gate face: check the grammar without evaluating.

    Blank conditions are the "no condition" case (``evaluate_step_condition``
    passes them) and therefore valid; anything else must parse as the ported
    subset or raises :class:`ConditionSyntaxError`.
    """
    expr = condition.strip()
    if not expr:
        return
    _validate_expr_syntax(expr)


def _eval_expr(
    expr: str,
    context: dict[str, Any],
    resolver: Callable[[str, dict[str, Any]], Any],
) -> bool:
    or_parts = _split_top_level_word(expr, "or")
    if len(or_parts) > 1:
        return any(_eval_expr(part, context, resolver) for part in or_parts)

    and_parts = _split_top_level_word(expr, "and")
    if len(and_parts) > 1:
        return all(_eval_expr(part, context, resolver) for part in and_parts)

    return _eval_atom(expr, context, resolver)


def _eval_atom(
    expr: str,
    context: dict[str, Any],
    resolver: Callable[[str, dict[str, Any]], Any],
) -> bool:
    left_ref, op, right = _parse_atom(expr)

    try:
        actual = resolver(left_ref, context)
    except (KeyError, TypeError, ValueError):
        actual = MISSING

    if op in {"isnot", "is not"}:
        return not _compare_is(actual, right)
    if op == "is":
        return _compare_is(actual, right)
    return _compare_operator(actual, op, right)


def _validate_expr_syntax(expr: str) -> None:
    for or_part in _split_top_level_word(expr, "or"):
        for and_part in _split_top_level_word(or_part, "and"):
            _parse_atom(and_part)


def _parse_atom(expr: str) -> tuple[str, str, Any]:
    if "===" in expr or "!==" in expr:
        raise ConditionSyntaxError(f"unsupported condition operator in: {expr}")
    match = _ATOM_RE.match(expr)
    if not match:
        raise ConditionSyntaxError(f"unsupported condition syntax: {expr}")

    left_ref = match.group("left")
    op = re.sub(r"\s+", " ", match.group("op").strip().lower())
    right = _parse_literal(match.group("right").strip())
    return left_ref, op, right


def _compare_is(actual: Any, expected: Any) -> bool:
    if expected == "empty":
        return _is_empty(actual)
    if expected is None:
        return actual is MISSING or actual is None
    if actual is MISSING:
        return False
    if isinstance(expected, bool):
        coerced = _coerce_bool(actual)
        return coerced is expected if coerced is not None else actual == expected
    return actual == expected


def _compare_operator(actual: Any, op: str, expected: Any) -> bool:
    if actual is MISSING:
        return op == "!="
    try:
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        left, right = _coerce_ordered_values(actual, expected)
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
    except (TypeError, ValueError):
        return False
    raise ConditionSyntaxError(f"unsupported condition operator: {op}")


def _parse_literal(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise ConditionSyntaxError("missing condition literal")
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        return text[1:-1]
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none"}:
        return None
    if lower == "empty":
        return "empty"
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        if re.fullmatch(r"[A-Za-z_]\w[\w.-]*", text):
            return text
        raise ConditionSyntaxError(f"unsupported condition literal: {raw}") from None


def resolve_ref(path: str, context: dict[str, Any]) -> Any:
    """The tolerant dotted-path resolver: walk dicts (camel/snake fallback), index sequences.

    A segment the walk cannot resolve yields the :data:`MISSING` sentinel
    (never an exception) -- the evaluator's missing-field semantics. Object
    attribute access is not part of the grammar.
    """
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict):
            value, found = _find_key(current, part)
            if not found:
                return MISSING
            current = value
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            idx = int(part)
            if idx < len(current):
                current = current[idx]
                continue
        return MISSING
    return current


def _find_key(data: dict[str, Any], key: str) -> tuple[Any, bool]:
    if key in data:
        return data[key], True
    snake = _camel_to_snake(key)
    if snake != key and snake in data:
        return data[snake], True
    camel = _snake_to_camel(key)
    if camel != key and camel in data:
        return data[camel], True
    return None, False


def _is_empty(value: Any) -> bool:
    if value is MISSING or value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return not value or all(_is_empty(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return not value
    return False


def _coerce_ordered_values(left: Any, right: Any) -> tuple[Any, Any]:
    if isinstance(left, (int, float)) and isinstance(right, str):
        return left, float(right) if "." in right else int(right)
    if isinstance(left, str) and isinstance(right, (int, float)):
        return float(left) if "." in left else int(left), right
    return left, right


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _split_top_level_word(expr: str, word: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_single = False
    in_double = False
    lower = expr.lower()
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_double:
            end = i + len(word)
            if (
                lower[i:end] == word
                and (i == 0 or not lower[i - 1].isalnum())
                and (end == len(expr) or not lower[end].isalnum())
            ):
                parts.append(expr[start:i].strip())
                start = end
                i = end
                continue
        i += 1
    if parts:
        parts.append(expr[start:].strip())
        if any(not part for part in parts):
            raise ConditionSyntaxError(f"empty boolean operand in: {expr}")
    return parts or [expr.strip()]


def _outside_quotes(text: str, pos: int) -> bool:
    in_single = False
    in_double = False
    for ch in text[:pos]:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
    return not in_single and not in_double


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")
_SNAKE_RE = re.compile("_([a-zA-Z])")


def _camel_to_snake(name: str) -> str:
    return _CAMEL_RE.sub("_", name).lower()


def _snake_to_camel(name: str) -> str:
    return _SNAKE_RE.sub(lambda m: m.group(1).upper(), name)
