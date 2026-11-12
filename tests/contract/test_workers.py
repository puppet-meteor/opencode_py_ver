"""Contract tests for the P3-S3 worker faces (T-C1-078; CORE-CONTRACTS §5.4).

The worker face splits by the T-C1-078 pre-ruling 6:

* the **structured return** is a wire contract (subprocess stdout crossing a
  process boundary) -- pinned here and by the schema-lock trio
  (``workers_{valid,missing_field,bad_extension}.json``);
* the **brief** is an in-process protocol (dataclass + renderer) -- pinned
  here only: the seven fixed sections (goal/scope/context/constraints/
  outputs/acceptance/budget), the whitelist rendering, the budget lines;
* the proactive-interception **host config document**, the acceptance-gate
  whitelist matcher, the budget per-line resolution and the stdout parsing
  are pure functions pinned here;
* the engine registration-gate faces: the default (unwired) rejection stays
  byte-identical to the S2 message (pinned by the unauthorized S2 tests, so
  this file only re-pins it as a documented invariant), while the worker-wired
  face names its executed set and gains the ``opencode_agent`` per-type
  constraint (goal prompt template).

No subprocess spawns here -- the subprocess lifecycle, budget kills and the
real-CLI e2e live in ``tests/integration/test_workers.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import JobBudget
from opencode.contracts.pipelines import PipelineSpec
from opencode.contracts.workers import WorkerReturn
from opencode.orchestrator.engine import (
    DEFAULT_IMPLEMENTED,
    PipelineSpecRejected,
    validate_pipeline_spec,
)
from opencode.orchestrator.workers import (
    BRIEF_SECTIONS,
    TaskBrief,
    WorkerError,
    WorkerManager,
    host_config_document,
    parse_worker_return,
    render_task_brief,
    resolve_budget,
    whitelist_allows,
    worker_prompt_words,
)
from tests.contract.test_pipelines import _spec_raw

_AGENT_IMPLEMENTED = DEFAULT_IMPLEMENTED | frozenset({"opencode_agent"})

#: The S2 default-face message, byte-identical (pinned by the unauthorized S2
#: tests; re-pinned here as the S3 invariant the wired face must not disturb).
_DEFAULT_FACE_MESSAGE = (
    "unimplemented node type 'opencode_agent' on node 'agent': "
    "S2 executes llm_call/tool nodes only "
    "(opencode_agent lands with S3 workers; human_gate and subworkflow with S5a)"
)


def _agent_raw(**overrides: Any) -> dict[str, Any]:
    """One minimal single-opencode_agent-node spec; overrides splice in violations."""
    raw: dict[str, Any] = _spec_raw(
        pipeline_id="workerflow",
        entry="agent",
        nodes=[{"node_id": "agent", "node_type": "opencode_agent", "prompt_template": "Ship the digest notes."}],
        edges=[],
    )
    raw.update(overrides)
    return raw


def _brief(**overrides: Any) -> TaskBrief:
    fields: dict[str, Any] = {
        "goal": "Ship the digest notes.",
        "context": "The repository carries one markdown note to update.",
        "acceptance": "The note names the current release.",
        "authorized_files": ("notes/release.md", "notes/"),
        "budget": JobBudget(max_tool_calls=5, max_tokens=2000, timeout_s=120.0),
    }
    fields.update(overrides)
    return TaskBrief(**fields)


class TestTaskBriefRender:
    """Acceptance judge 1: the seven-section structure (PHASE3 §5.3)."""

    def test_renders_the_seven_fixed_sections_in_order(self) -> None:
        text = render_task_brief(_brief())
        positions = [text.index(f"## {section}") for section in BRIEF_SECTIONS]
        assert BRIEF_SECTIONS == ("goal", "scope", "context", "constraints", "outputs", "acceptance", "budget")
        assert positions == sorted(positions), "the seven sections must appear in the fixed order"

    def test_scope_renders_the_worktree_boundary_and_whitelist(self) -> None:
        text = render_task_brief(_brief())
        assert "git worktree" in text
        assert "- notes/release.md" in text
        assert "- notes/" in text

    def test_empty_whitelist_renders_the_read_only_line(self) -> None:
        text = render_task_brief(_brief(authorized_files=()))
        assert "read-only" in text

    def test_budget_lines_render_the_three_limits(self) -> None:
        text = render_task_brief(_brief())
        assert "- max tool calls: 5" in text
        assert "- max tokens: 2000" in text
        assert "- wall-clock timeout: 120s" in text

    def test_unlimited_budget_lines_render_explicitly(self) -> None:
        text = render_task_brief(_brief(budget=JobBudget()))
        assert "- max tool calls: unlimited" in text
        assert "- max tokens: unlimited" in text
        assert "- wall-clock timeout: unlimited" in text

    def test_goal_context_and_acceptance_sections_carry_the_texts(self) -> None:
        text = render_task_brief(_brief())
        assert "Ship the digest notes." in text
        assert "The repository carries one markdown note to update." in text
        assert "The note names the current release." in text

    def test_render_is_deterministic(self) -> None:
        assert render_task_brief(_brief()) == render_task_brief(_brief())


class TestWorkerReturnContract:
    """Acceptance judge 4: the structured-return wire shape (path rules included)."""

    def test_valid_full_return_parses(self) -> None:
        returned = WorkerReturn.model_validate(
            {
                "schema_version": 1,
                "summary": "Done; the note names the release.",
                "files_changed": ["notes/release.md"],
                "artifacts": ["notes/verification.md"],
                "tests_passed": True,
            }
        )
        assert returned.files_changed == ["notes/release.md"]
        assert returned.tests_passed is True

    def test_path_lists_default_to_empty(self) -> None:
        returned = WorkerReturn.model_validate(
            {"schema_version": 1, "summary": "Nothing to change.", "tests_passed": True}
        )
        assert returned.files_changed == []
        assert returned.artifacts == []

    @pytest.mark.parametrize(
        "field",
        ["files_changed", "artifacts"],
    )
    @pytest.mark.parametrize(
        "bad_entry",
        [
            "",
            "/etc/passwd",
            "C:/windows/system32",
            "src\\feature.py",
            "..",
            "notes/../outside.md",
        ],
    )
    def test_path_entries_reject_non_relative_shapes(self, field: str, bad_entry: str) -> None:
        payload = {
            "schema_version": 1,
            "summary": "s",
            "files_changed": [],
            "artifacts": [],
            "tests_passed": True,
            field: [bad_entry],
        }
        with pytest.raises(ValidationError):
            WorkerReturn.model_validate(payload)

    def test_schema_version_rejects_bool_and_zero(self) -> None:
        base: dict[str, Any] = {"summary": "s", "tests_passed": True}
        with pytest.raises(ValidationError, match="not a boolean"):
            WorkerReturn.model_validate({"schema_version": True, **base})
        with pytest.raises(ValidationError):
            WorkerReturn.model_validate({"schema_version": 0, **base})

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkerReturn.model_validate({"schema_version": 1, "summary": "s", "tests_passed": True, "exit_code": 0})

    def test_empty_summary_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkerReturn.model_validate({"schema_version": 1, "summary": "", "tests_passed": True})


class TestParseWorkerReturn:
    """The stdout contract: the LAST [final] marker, the [run] line cut, fail-closed parse."""

    def test_parses_the_final_marker_and_cuts_the_run_line(self) -> None:
        stdout = (
            "[user] prompt\n[final] "
            + '{"schema_version": 1, "summary": "ok", "files_changed": [], "artifacts": [], "tests_passed": true}'
            + "\n[run] succeeded\n"
        )
        returned = parse_worker_return(stdout)
        assert returned.summary == "ok"
        assert returned.tests_passed is True

    def test_the_last_marker_wins(self) -> None:
        first = '{"schema_version": 1, "summary": "stale", "files_changed": [], "artifacts": [], "tests_passed": false}'
        second = (
            '{"schema_version": 1, "summary": "final answer", "files_changed": [], '
            '"artifacts": [], "tests_passed": true}'
        )
        stdout = f"[final] {first}\n[final] {second}\n[run] succeeded\n"
        assert parse_worker_return(stdout).summary == "final answer"

    def test_missing_marker_fails_closed(self) -> None:
        with pytest.raises(WorkerError) as error:
            parse_worker_return("just text, no marker\n[run] succeeded\n")
        assert error.value.code == CoreErrorCode.TOOL_FAILED

    def test_non_json_payload_fails_closed(self) -> None:
        with pytest.raises(WorkerError) as error:
            parse_worker_return("[final] this is not json\n[run] failed\n")
        assert error.value.code == CoreErrorCode.TOOL_FAILED

    def test_non_object_json_fails_closed(self) -> None:
        with pytest.raises(WorkerError) as error:
            parse_worker_return('[final] ["not", "an", "object"]\n[run] succeeded\n')
        assert error.value.code == CoreErrorCode.TOOL_FAILED

    def test_contract_violating_payload_fails_closed(self) -> None:
        payload = '{"schema_version": 1, "summary": "ok"}'
        with pytest.raises(WorkerError) as error:
            parse_worker_return(f"[final] {payload}\n[run] succeeded\n")
        assert error.value.code == CoreErrorCode.TOOL_FAILED


class TestHostConfig:
    """The proactive-interception trio document (PHASE3 §5.3; loader-shaped)."""

    def test_roots_pin_the_worktree(self, tmp_path: Path) -> None:
        document = host_config_document(tmp_path / "wt")
        assert document["roots"] == [str(tmp_path / "wt")]

    def test_the_trio_rules_are_present_with_legal_shapes(self, tmp_path: Path) -> None:
        rules = host_config_document(tmp_path / "wt")["permission"]["rules"]
        by_action = {rule["action"]: rule for rule in rules}
        assert by_action["git__worktree_add"]["effect"] == "deny"
        assert by_action["git__worktree_remove"]["effect"] == "deny"
        assert by_action["fs__write"]["effect"] == "allow"
        assert by_action["fs__edit"]["effect"] == "allow"
        for rule in rules:
            assert set(rule) <= {"action", "effect", "reason"}
            assert rule["effect"] in ("allow", "ask", "deny")
            assert rule.get("reason"), "every rule carries its reason"

    def test_document_is_json_serializable_and_deterministic(self, tmp_path: Path) -> None:
        import json

        first = json.dumps(host_config_document(tmp_path / "wt"), sort_keys=True)
        second = json.dumps(host_config_document(tmp_path / "wt"), sort_keys=True)
        assert first == second


class TestWhitelist:
    """The acceptance-gate matcher: exact file or directory prefix; empty = read-only."""

    def test_exact_file_match(self) -> None:
        assert whitelist_allows(["notes/release.md"], "notes/release.md")

    def test_directory_prefix_match_including_collapsed_untracked_dir(self) -> None:
        assert whitelist_allows(["notes/"], "notes/release.md")
        assert whitelist_allows(["notes"], "notes/")
        assert whitelist_allows(["notes"], "notes/sub/deep.md")

    def test_prefix_must_respect_the_slash_boundary(self) -> None:
        assert not whitelist_allows(["notes"], "notesx/file.md")
        assert not whitelist_allows(["notes"], "notes.md")

    def test_empty_whitelist_allows_nothing(self) -> None:
        assert not whitelist_allows([], "notes/release.md")
        assert not whitelist_allows((), "any/path")

    def test_entries_and_paths_are_separator_normalized(self) -> None:
        assert whitelist_allows(["notes\\release.md"], "notes/release.md")
        assert whitelist_allows(["notes"], "notes\\release.md")


class TestResolveBudget:
    """The per-line merge: the job's line wins, the manager default fills."""

    def test_none_job_budget_uses_the_whole_default(self) -> None:
        default = JobBudget(max_tool_calls=7, max_tokens=9000, timeout_s=60.0)
        assert resolve_budget(None, default) == default

    def test_job_lines_win_and_missing_lines_inherit(self) -> None:
        default = JobBudget(max_tool_calls=7, max_tokens=9000, timeout_s=60.0)
        merged = resolve_budget(JobBudget(max_tokens=10), default)
        assert merged.max_tokens == 10
        assert merged.max_tool_calls == 7
        assert merged.timeout_s == 60.0

    def test_all_none_merges_to_the_full_default(self) -> None:
        default = JobBudget(max_tool_calls=7, max_tokens=9000, timeout_s=60.0)
        assert resolve_budget(JobBudget(), default) == default


class TestManagerConstruction:
    """The manager's own fail-closed defaults (no subprocess here)."""

    def test_default_budget_without_timeout_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            WorkerManager(
                repo_root=tmp_path,
                runs_root=tmp_path,
                default_budget=JobBudget(),
                base_url="http://127.0.0.1:1",
                api_key="k",
                model="m",
                context_limit=1000,
            )

    def test_timeout_carrying_default_constructs(self, tmp_path: Path) -> None:
        manager = WorkerManager(
            repo_root=tmp_path,
            runs_root=tmp_path,
            default_budget=JobBudget(timeout_s=30.0),
            base_url="http://127.0.0.1:1",
            api_key="k",
            model="m",
            context_limit=1000,
        )
        assert manager is not None


class TestRegistrationGateFaces:
    """The S3 engine faces: default byte-identity, the wired set, the goal constraint."""

    def test_default_face_message_stays_byte_identical_to_s2(self) -> None:
        spec = PipelineSpec.model_validate(_agent_raw())
        with pytest.raises(PipelineSpecRejected) as error:
            validate_pipeline_spec(spec)
        assert str(error.value) == _DEFAULT_FACE_MESSAGE
        assert error.value.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert error.value.details == {"node_id": "agent", "node_type": "opencode_agent"}

    def test_wired_face_executes_the_agent_node(self) -> None:
        spec = PipelineSpec.model_validate(_agent_raw())
        validate_pipeline_spec(spec, implemented=_AGENT_IMPLEMENTED)

    def test_wired_face_rejection_names_the_executed_set_and_s5a(self) -> None:
        raw = _agent_raw(
            nodes=[
                {"node_id": "gate", "node_type": "human_gate"},
                {"node_id": "agent", "node_type": "opencode_agent", "prompt_template": "g"},
            ],
            entry="agent",
            edges=[{"from_node": "agent", "to_node": "gate"}],
        )
        spec = PipelineSpec.model_validate(raw)
        with pytest.raises(PipelineSpecRejected) as error:
            validate_pipeline_spec(spec, implemented=_AGENT_IMPLEMENTED)
        assert str(error.value) == (
            "unimplemented node type 'human_gate' on node 'gate': "
            "this engine executes llm_call/tool/opencode_agent nodes only "
            "(human_gate and subworkflow land with S5a)"
        )

    def test_wired_agent_node_requires_its_goal_prompt_template(self) -> None:
        raw = _agent_raw(nodes=[{"node_id": "agent", "node_type": "opencode_agent"}], edges=[])
        spec = PipelineSpec.model_validate(raw)
        with pytest.raises(PipelineSpecRejected) as error:
            validate_pipeline_spec(spec, implemented=_AGENT_IMPLEMENTED)
        assert error.value.code == CoreErrorCode.VALIDATION_ERROR
        assert "prompt template" in str(error.value)

    def test_llm_call_constraint_message_stays_byte_identical(self) -> None:
        raw = _agent_raw(
            nodes=[
                {"node_id": "agent", "node_type": "opencode_agent", "prompt_template": "g"},
                {"node_id": "summarize", "node_type": "llm_call"},
            ],
            edges=[{"from_node": "agent", "to_node": "summarize"}],
        )
        spec = PipelineSpec.model_validate(raw)
        with pytest.raises(PipelineSpecRejected) as error:
            validate_pipeline_spec(spec, implemented=_AGENT_IMPLEMENTED)
        assert str(error.value) == (
            "llm_call node 'summarize' must carry both a model tier ('model') and a prompt template ('prompt_template')"
        )


class TestWorkerPromptWords:
    """The worker's first instruction (PHASE3 §5.3): fixed, path-free, brief-naming."""

    def test_joins_into_the_fixed_sentence(self) -> None:
        assert " ".join(worker_prompt_words()) == "Read the task brief at TASK-BRIEF.md and execute it."

    def test_names_the_brief_without_path_separators(self) -> None:
        words = worker_prompt_words()
        assert "TASK-BRIEF.md" in words
        assert all("/" not in word and "\\" not in word for word in words)
