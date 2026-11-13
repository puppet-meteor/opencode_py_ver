"""The v2 condition evaluator contract (T-C1-084): the ported safe-subset grammar.

Locks the ported behavior of ``opencode.orchestrator.conditions`` against the
framework source semantics: the no-eval grammar (or/and splits, one
comparison per atom, quoted/named/numeric literals), the missing-field
semantics (``is null``/``is empty`` match, ``!=`` survives, everything else
reads false), the camel/snake-tolerant key walk, the ref extraction order and
quote exclusion, the ``ConditionSyntaxError`` faces for unsupported syntax,
and the runtime seam contract: lookup failures (KeyError/TypeError/ValueError)
degrade to missing while a ``WorkerError`` from an engine skip-guard
propagates out as the honest failure (the T-C1-084 skipped-step decision).
"""

from __future__ import annotations

from typing import Any

import pytest

from opencode.contracts.errors import CoreErrorCode
from opencode.orchestrator.conditions import (
    MISSING,
    ConditionEvaluation,
    ConditionSyntaxError,
    evaluate_step_condition,
    extract_condition_refs,
    resolve_ref,
    validate_condition_syntax,
)
from opencode.orchestrator.workers import WorkerError


def _raising_resolver(path: str, context: dict[str, Any]) -> Any:
    raise KeyError(path)


class TestEvaluateStepCondition:
    def test_blank_conditions_pass_with_the_no_condition_reason(self) -> None:
        for blank in (None, "", "   "):
            evaluation = evaluate_step_condition(blank, {})
            assert evaluation == ConditionEvaluation(True, "no condition", [])

    def test_is_true_and_false_over_bools_and_coerced_strings(self) -> None:
        ctx = {"step_1": {"ready": True}, "step_2": {"ready": "false"}}
        assert evaluate_step_condition("step_1.ready is true", ctx).passed
        assert not evaluate_step_condition("step_2.ready is true", ctx).passed
        assert evaluate_step_condition("step_2.ready is false", ctx).passed

    def test_isnot_inverts_without_the_equality_fallback(self) -> None:
        ctx = {"step_1": {"ready": True}}
        assert evaluate_step_condition("step_1.ready isNot true", ctx).passed is False
        assert evaluate_step_condition("step_1.ready is not true", ctx).passed is False

    def test_quoted_string_literals_compare_verbatim(self) -> None:
        ctx = {"step_1": {"status": "completed"}}
        assert evaluate_step_condition("step_1.status is 'completed'", ctx).passed
        assert not evaluate_step_condition('step_1.status is "pending"', ctx).passed

    def test_null_and_empty_literals_have_the_ported_semantics(self) -> None:
        ctx = {"step_1": {"value": None, "text": "   "}}
        assert evaluate_step_condition("step_1.value is null", ctx).passed
        assert evaluate_step_condition("step_1.text is empty", ctx).passed
        assert evaluate_step_condition("step_1.missing is empty", ctx).passed
        assert not evaluate_step_condition("step_1.missing is 'x'", ctx).passed

    def test_ordered_comparisons_coerce_across_string_number(self) -> None:
        ctx = {"step_1": {"count": 3}, "step_2": {"count": "5"}}
        assert evaluate_step_condition("step_1.count > 2", ctx).passed
        assert evaluate_step_condition("step_2.count >= 5", ctx).passed
        assert evaluate_step_condition("step_1.count < 3", ctx).passed is False

    def test_or_and_and_split_top_level_only(self) -> None:
        ctx = {"step_1": {"a": True, "b": False}}
        assert evaluate_step_condition("step_1.a is true or step_1.b is true", ctx).passed
        assert not evaluate_step_condition("step_1.a is true and step_1.b is true", ctx).passed
        # quoted words never split: the operand is inside quotes
        assert evaluate_step_condition("step_1.status is 'or'", {"step_1": {"status": "or"}}).passed

    def test_camel_and_snake_keys_resolve_tolerantly(self) -> None:
        camel_ctx = {"step_1": {"errorMessage": "boom"}}
        snake_ctx = {"step_1": {"error_message": "boom"}}
        assert evaluate_step_condition("step_1.errorMessage is 'boom'", camel_ctx).passed
        assert evaluate_step_condition("step_1.error_message is 'boom'", camel_ctx).passed
        assert evaluate_step_condition("step_1.errorMessage is 'boom'", snake_ctx).passed

    def test_list_and_nested_walks_resolve(self) -> None:
        ctx = {"step_1": {"items": [{"ok": True}]}}
        assert evaluate_step_condition("step_1.items.0.ok is true", ctx).passed

    def test_the_reason_names_the_expression(self) -> None:
        evaluation = evaluate_step_condition("step_1.ready is true", {"step_1": {"ready": False}})
        assert evaluation.reason == "condition evaluated to false: step_1.ready is true"
        assert evaluation.passed is False

    def test_bare_word_refs_resolve_from_the_context_root(self) -> None:
        assert evaluate_step_condition("status is 'completed'", {"status": "completed"}).passed


class TestMissingFieldSemantics:
    def test_unresolvable_refs_are_missing_not_errors(self) -> None:
        evaluation = evaluate_step_condition("step_9.ghost is null", {})
        assert evaluation.passed  # missing is null

    def test_missing_refs_only_survive_not_equal(self) -> None:
        assert evaluate_step_condition("step_9.ghost != 'x'", {}).passed
        assert not evaluate_step_condition("step_9.ghost == 'x'", {}).passed
        assert not evaluate_step_condition("step_9.ghost > 0", {}).passed

    def test_a_resolver_lookup_failure_degrades_to_missing(self) -> None:
        # KeyError/TypeError/ValueError from the resolver keep the ported
        # missing-field semantics instead of failing the node.
        evaluation = evaluate_step_condition("step_1.ready is null", {}, _raising_resolver)
        assert evaluation.passed

    def test_a_worker_error_from_the_resolver_propagates_as_the_failure(self) -> None:
        """The T-C1-084 skip-guard seam: WorkerError is not a lookup failure."""

        def guard(path: str, context: dict[str, Any]) -> Any:
            raise WorkerError(
                "condition ref references skipped step 'step_1'",
                code=CoreErrorCode.VALIDATION_ERROR,
            )

        with pytest.raises(WorkerError, match="skipped step 'step_1'"):
            evaluate_step_condition("step_1.ready is true", {}, guard)


class TestResolveRef:
    def test_walks_dotted_paths_over_dicts_and_lists(self) -> None:
        ctx = {"step_1": {"items": [{"ok": True}]}}
        assert resolve_ref("step_1.items.0.ok", ctx) is True
        assert resolve_ref("step_1.items", ctx) == [{"ok": True}]

    def test_unresolvable_paths_yield_the_missing_sentinel(self) -> None:
        assert resolve_ref("step_1.ghost", {"step_1": {}}) is MISSING
        assert resolve_ref("inputs", {}) is MISSING
        assert resolve_ref("step_1.items.9", {"step_1": {"items": []}}) is MISSING

    def test_the_sentinel_is_distinct_from_a_legit_null(self) -> None:
        assert resolve_ref("step_1.value", {"step_1": {"value": None}}) is None
        assert resolve_ref("step_1.value", {"step_1": {}}) is MISSING


class TestExtractRefs:
    def test_refs_come_back_in_occurrence_order_without_duplicates(self) -> None:
        refs = extract_condition_refs("step_1.a is true or step_2.b > 0 or step_1.a isNot false")
        assert refs == ["step_1.a", "step_2.b"]

    def test_inputs_refs_extract_too(self) -> None:
        assert extract_condition_refs("inputs.mode is fast") == ["inputs.mode"]

    def test_refs_inside_quotes_do_not_extract(self) -> None:
        # the quoted step_2.done is a string literal, not a ref; the
        # unquoted step_1.status still extracts.
        assert extract_condition_refs("step_1.status is 'step_2.done'") == ["step_1.status"]

    def test_no_refs_means_an_empty_list(self) -> None:
        assert extract_condition_refs(None) == []
        assert extract_condition_refs("status is ok") == []


class TestSyntaxValidation:
    def test_valid_expressions_pass(self) -> None:
        for condition in (
            "step_1.ready is true",
            "inputs.mode is fast or step_1.count > 0",
            "step_1.status is 'completed' and step_2.error_message isNot empty",
            "step_1.a != 3",
        ):
            validate_condition_syntax(condition)

    def test_blank_conditions_are_the_no_condition_case(self) -> None:
        validate_condition_syntax("   ")

    @pytest.mark.parametrize(
        "condition",
        [
            "step_1.ready === true",
            "step_1.ready !== true",
            "step_1 ready is true",
            "step_1.ready is",
            "step_1.a is true or",
            "and step_1.a is true",
            "step_1.a is ?",
        ],
    )
    def test_unsupported_syntax_raises(self, condition: str) -> None:
        with pytest.raises(ConditionSyntaxError):
            validate_condition_syntax(condition)
        with pytest.raises(ConditionSyntaxError):
            evaluate_step_condition(condition, {"step_1": {"ready": True}})

    def test_the_syntax_error_is_a_value_error(self) -> None:
        with pytest.raises(ValueError):
            validate_condition_syntax("step_1.ready && true")
