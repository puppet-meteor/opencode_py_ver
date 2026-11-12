"""Contract tests for the py.job.* event family + the job-card protocol (T-C1-076, P3-S1).

Pins the wire face (CORE-CONTRACTS §3.1 rows; PHASE3-ORCHESTRATOR §5.4):

- the seven durable v1 types are registered in ``default_event_registry``
  and NOT run-scoped (jobs are an orchestrator-domain entity, not a §7 run:
  no envelope ``metadata.run_id`` requirement -- the whenever-present ^run_
  guard still applies);
- every data model carries the four trace fields as the nested ``trace``
  object (PHASE3 §4 M-6; nested because the §5.4 job field ``depth``
  collides with the trace depth on py.job.created);
- the §1 integer-semantic bool guards on every integer field;
- the committed fixtures decode through the registry (the lock-diff census
  in test_schema_lock.py pins the schema side);
- the cards bijection: ``JOB_CARD_KINDS`` maps every jobs-family type to
  exactly one card kind, and a ``JobCard`` validates over the folded status
  domain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.cards import JOB_CARD_KINDS, JobCard, JobCardKind, JobStatusLiteral
from opencode.contracts.events import (
    JOB_EVENT_DEFINITIONS,
    PY_JOB_CREATED,
    PY_JOB_FINISHED,
    PY_JOB_GATED,
    PY_JOB_REDISPATCHED,
    PY_JOB_SPEC_BOUND,
    PY_JOB_STALLED,
    PY_JOB_STARTED,
    RUN_SCOPED_EVENT_TYPES,
    DurabilityMismatchError,
    EventDefinition,
    JobBudget,
    JobCreatedData,
    JobFinishedData,
    JobGatedData,
    JobLineage,
    JobRedispatchedData,
    JobSpecBoundData,
    JobStalledData,
    JobStartedData,
    JobTrace,
    default_event_registry,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

JOB_TYPES = (
    PY_JOB_CREATED,
    PY_JOB_SPEC_BOUND,
    PY_JOB_STARTED,
    PY_JOB_STALLED,
    PY_JOB_REDISPATCHED,
    PY_JOB_GATED,
    PY_JOB_FINISHED,
)

JOB_DATA_MODELS: dict[str, type[Any]] = {
    PY_JOB_CREATED: JobCreatedData,
    PY_JOB_SPEC_BOUND: JobSpecBoundData,
    PY_JOB_STARTED: JobStartedData,
    PY_JOB_STALLED: JobStalledData,
    PY_JOB_REDISPATCHED: JobRedispatchedData,
    PY_JOB_GATED: JobGatedData,
    PY_JOB_FINISHED: JobFinishedData,
}

#: The seven committed valid fixtures (written by T-C1-076, one per type).
VALID_FIXTURES = tuple(
    f"events_job_{name}_valid.json"
    for name in ("created", "spec_bound", "started", "stalled", "redispatched", "gated", "finished")
)


def _trace() -> JobTrace:
    return JobTrace(trace_id="trc_1", span_id="spn_1", parent_id=None, depth=0)


class TestRegistryMembership:
    """The seven durable v1 rows resolve; ephemeral decode of a durable type is the mismatch error."""

    def test_seven_definitions_all_durable_version_one(self) -> None:
        assert len(JOB_EVENT_DEFINITIONS) == 7
        for definition in JOB_EVENT_DEFINITIONS:
            assert isinstance(definition, EventDefinition)
            assert definition.durability == "durable"
            assert definition.version == 1
            assert definition.event_type in JOB_TYPES

    def test_default_registry_resolves_every_jobs_type(self) -> None:
        registry = default_event_registry()
        for event_type in JOB_TYPES:
            definition = registry.resolve_durable(event_type, 1)
            assert definition.data_model is JOB_DATA_MODELS[event_type]
            with pytest.raises(DurabilityMismatchError):
                registry.resolve_ephemeral(event_type)

    def test_jobs_types_are_not_run_scoped(self) -> None:
        """A job is not a §7 run: no metadata.run_id is required on any envelope."""
        assert not (set(JOB_TYPES) & RUN_SCOPED_EVENT_TYPES)


class TestTraceField:
    """The four trace fields ride every py.job.* data model as the nested object."""

    @pytest.mark.parametrize("event_type", JOB_TYPES)
    def test_every_data_model_carries_the_nested_trace(self, event_type: str) -> None:
        assert "trace" in JOB_DATA_MODELS[event_type].model_fields

    def test_trace_rejects_bool_depth_and_missing_span(self) -> None:
        with pytest.raises(ValidationError, match="integer, not a boolean"):
            JobTrace(trace_id="trc_1", span_id="spn_1", parent_id=None, depth=True)
        with pytest.raises(ValidationError):
            JobTrace(trace_id="trc_1", depth=0)  # type: ignore[call-arg] # span_id is required
        with pytest.raises(ValidationError):
            JobTrace(trace_id="trc_1", span_id="spn_1", parent_id=None, depth=0, extra=1)  # type: ignore[call-arg]

    def test_created_depth_is_the_workflow_nesting_not_trace_depth(self) -> None:
        """The §5.4 job ``depth`` (workflow nesting) is a separate created field."""
        data = JobCreatedData(
            job_id="job_1",
            pipeline_id="p",
            depth=2,
            lineage=JobLineage(origin="api"),
            budget=JobBudget(),
            trace=JobTrace(trace_id="trc_1", span_id="spn_1", parent_id=None, depth=0),
        )
        assert data.depth == 2
        assert data.trace.depth == 0
        with pytest.raises(ValidationError, match="integer, not a boolean"):
            JobCreatedData(
                job_id="job_1",
                pipeline_id="p",
                depth=True,
                lineage=JobLineage(origin="api"),
                budget=JobBudget(),
                trace=_trace(),
            )


class TestBoolGuards:
    """§1 repo rule: integer-semantic fields reject bools before type conversion."""

    def test_budget_integer_fields_reject_bools(self) -> None:
        with pytest.raises(ValidationError, match="integer, not a boolean"):
            JobBudget(max_tool_calls=True)
        with pytest.raises(ValidationError, match="integer, not a boolean"):
            JobBudget(max_tokens=True)

    def test_redispatched_retry_count_rejects_bool(self) -> None:
        with pytest.raises(ValidationError, match="integer, not a boolean"):
            JobRedispatchedData(job_id="job_1", attempt_id="att_1", retry_count=True, trace=_trace())


class TestWireGuards:
    """Lineage/anchor wire rules beyond the JSON Schema face."""

    def test_lineage_session_id_must_be_ses_prefixed(self) -> None:
        with pytest.raises(ValidationError, match="ses_"):
            JobLineage(origin="session", session_id="run_1")

    def test_spec_bound_anchor_must_be_api_or_ses(self) -> None:
        with pytest.raises(ValidationError, match="session_anchor"):
            JobSpecBoundData(
                job_id="job_1",
                params={"q": 1},
                params_hash="a" * 64,
                session_anchor="worker",
                trace=_trace(),
            )

    def test_finished_outcome_is_the_closed_literal(self) -> None:
        with pytest.raises(ValidationError):
            JobFinishedData(job_id="job_1", outcome="succeeded", trace=_trace())  # type: ignore[arg-type]


class TestFixturesDecode:
    """The seven committed valid fixtures decode through the registry with their own type."""

    @pytest.mark.parametrize("fixture_name", VALID_FIXTURES)
    def test_valid_fixture_decodes_durable(self, fixture_name: str) -> None:
        raw = json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))
        event = default_event_registry().decode_durable(raw)
        assert event.type == raw["type"]
        assert event.type in JOB_TYPES
        assert event.session_id.startswith("ses_job_")
        assert event.metadata == {}  # jobs events are not run-scoped


class TestCardsBijection:
    """The closed py.job.* -> card-kind mapping (the frontend rendering contract)."""

    def test_every_jobs_type_maps_to_exactly_one_kind(self) -> None:
        assert set(JOB_CARD_KINDS) == set(JOB_TYPES)
        assert len(set(JOB_CARD_KINDS.values())) == 7

    def test_a_card_validates_over_the_folded_status_domain(self) -> None:
        card = JobCard(
            kind="job_created",
            job_id="job_1",
            status="created",
            title="Job created",
            trace=_trace(),
            detail={"pipeline_id": "research"},
        )
        assert card.kind == "job_created"
        with pytest.raises(ValidationError):
            JobCard(
                kind="job_created",
                job_id="job_1",
                status="running-along",  # type: ignore[arg-type]
                title="t",
                trace=_trace(),
            )
        with pytest.raises(ValidationError):
            JobCard(
                kind="job_exploded",  # type: ignore[arg-type]
                job_id="job_1",
                status="created",
                title="t",
                trace=_trace(),
            )

    def test_card_kinds_and_statuses_cover_the_contract_domains(self) -> None:
        import typing

        kinds = set(typing.get_args(JobCardKind))
        assert kinds == set(JOB_CARD_KINDS.values())
        statuses = set(typing.get_args(JobStatusLiteral))
        assert statuses == {"created", "running", "stalled", "gated", "done", "failed", "cancelled"}
