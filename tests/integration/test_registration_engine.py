"""Integration tests for the P5-S1 registration face (T-C1-085) over the REAL engine.

The manifest -> engine glue driven end to end: a tmp skills tree whose
langgraph entry declares a dialect graph.json while the CONSUMED file is
its same-directory sibling ``pipeline.opencode.json`` (the framework
repo's emitter publish form -- the only consumption form, ruling-18/19;
T-C1-087), the real ``SkillCatalog`` discovery gate, the real
``PipelineEngine`` three-layer registration gate and its real
``EngineCheckpointer`` (aiosqlite, loop-bound). The ``llm_call``/``tool``
bodies are the test_engine.py fakes (the S2 discipline) -- registration
never executes node bodies, and the one driven job uses the digest chain
they serve.

Coverage (the acceptance judges):

* the native spec registers and binds under the spec's own
  ``pipeline_id``, with the engine's spec hash (the pinned v1 face);
* the declared dialect graph.json is never consumed: with BOTH files
  present the native sibling registers (the priority face, ruling-18 --
  a garbage declared file would be a decode rejection had it been opened);
* a missing native spec is the named ``NativeSpecMissing`` rejection row
  and the pipeline stays unbound (the 6-missing legacy form, ruling-19);
* a semantically rejected pipeline stays unbound (the submit face keeps
  its NotFound behavior) and lands as exactly one report row;
* e2e-lite: after a glued registration, a JobService submit naming the
  pipeline resolves (no longer NotFound) and the driven job settles done
  (the test_engine.py submit pattern).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opencode.contracts.pipelines import PipelineSpec, compute_spec_hash
from opencode.orchestrator.registration import NATIVE_SPEC_NAME, register_pipelines
from tests.contract.test_pipelines import _spec_raw
from tests.integration.test_engine import _FakeLlm, _FakeTool, _Stack

#: The locked skills-fixtures entry shape (T-C1-080), carrying the S5a
#: langgraph pair: the glue's only manifest routing input.
_LANGGRAPH_ENTRY: dict[str, Any] = {
    "id": "code-review",
    "name": "Code Review",
    "path": "code-review/SKILL.md",
    "size": 78,
    "hash": "70152fa897ce94831325a87efae0039c6af197ef1e70527e5551e14da3731ef8",
    "source": "project",
    "execution": "langgraph",
    "pipeline": "code-review/graph.json",
}


def _write_langgraph_skills(
    skills: Path,
    native_payload: object | None,
    *,
    declared_payload: object | None = None,
) -> Path:
    """One tmp skills tree: the manifest with one langgraph entry + the files it names.

    The native sibling ``pipeline.opencode.json`` carries the consumed
    payload (``None`` leaves it missing); ``declared_payload`` optionally
    also writes the declared graph.json -- which the glue must never open
    (routing metadata, ruling-18).
    """
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "skills.json").write_text(json.dumps({"skills": [_LANGGRAPH_ENTRY]}), encoding="utf-8")
    pipeline: str = _LANGGRAPH_ENTRY["pipeline"]
    if native_payload is not None:
        native = (skills / pipeline).parent / NATIVE_SPEC_NAME
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(
            native_payload if isinstance(native_payload, str) else json.dumps(native_payload),
            encoding="utf-8",
        )
    if declared_payload is not None:
        declared = skills / pipeline
        declared.parent.mkdir(parents=True, exist_ok=True)
        declared.write_text(
            declared_payload if isinstance(declared_payload, str) else json.dumps(declared_payload),
            encoding="utf-8",
        )
    return skills


class TestRealEngineRegistration:
    async def test_the_native_spec_registers_and_binds_the_spec_hash(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            raw = _spec_raw(pipeline_id="digest")
            skills = _write_langgraph_skills(tmp_path / "skills", raw)
            report = await register_pipelines(stack.engine, skills)
            assert report.manifest_error is None
            assert report.rejected == ()
            (registered,) = report.registered
            assert (registered.skill_id, registered.pipeline_id) == ("code-review", "digest")
            bound = stack.engine.bound("digest")
            assert bound is not None
            expected_hash = compute_spec_hash(PipelineSpec.model_validate(raw))
            assert bound.spec_hash == expected_hash
            assert registered.spec_hash == expected_hash
        finally:
            await stack.close()

    async def test_the_declared_dialect_graph_is_never_consumed(self, tmp_path: Path) -> None:
        """The priority face: with BOTH files present the native sibling registers.

        Before T-C1-087 a dialect graph.json routed through the converter
        (retired by ruling-18); the declared body below is a dialect-shaped
        object that would never validate as a native spec -- the registered
        native binding proves it was never opened.
        """
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            raw = _spec_raw(pipeline_id="digest")
            dialect = {"schema_version": "pydag-editable-graph-v1", "skill_name": "demo", "nodes": [], "edges": []}
            skills = _write_langgraph_skills(tmp_path / "skills", raw, declared_payload=dialect)
            report = await register_pipelines(stack.engine, skills)
            assert report.rejected == ()
            (registered,) = report.registered
            assert registered.pipeline_id == "digest"
            bound = stack.engine.bound("digest")
            assert bound is not None
            assert bound.spec_hash == compute_spec_hash(PipelineSpec.model_validate(raw))
        finally:
            await stack.close()

    async def test_a_missing_native_spec_is_one_structured_rejection_row(self, tmp_path: Path) -> None:
        """The 6-missing legacy form (ruling-19): the declared graph.json is present
        but its native sibling is not -> one named rejection row, nothing bound."""
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            dialect = {"schema_version": "pydag-editable-graph-v1", "skill_name": "demo", "nodes": [], "edges": []}
            skills = _write_langgraph_skills(tmp_path / "skills", None, declared_payload=dialect)
            report = await register_pipelines(stack.engine, skills)
            assert report.registered == ()
            (rejection,) = report.rejected
            assert rejection.skill_id == "code-review"
            assert rejection.pipeline == "code-review/graph.json"
            assert rejection.error_type == "NativeSpecMissing"
            assert "pipeline.opencode.json" in rejection.message
            assert "agent_dsl_flow_framework" in rejection.message
            assert stack.engine.bound("digest") is None
        finally:
            await stack.close()

    async def test_a_rejected_pipeline_stays_unbound_with_one_report_row(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            skills = _write_langgraph_skills(tmp_path / "skills", _spec_raw(pipeline_id="digest", entry="ghost"))
            report = await register_pipelines(stack.engine, skills)
            assert report.registered == ()
            (rejection,) = report.rejected
            assert rejection.skill_id == "code-review"
            assert rejection.pipeline == "code-review/graph.json"
            assert rejection.error_type == "PipelineSpecRejected"
            assert stack.engine.bound("digest") is None
        finally:
            await stack.close()

    async def test_a_glued_pipeline_resolves_through_the_submit_face_no_longer_not_found(self, tmp_path: Path) -> None:
        """e2e-lite: the JobService submit/drive path over a glued registration."""
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            skills = _write_langgraph_skills(tmp_path / "skills", _spec_raw(pipeline_id="digest"))
            report = await register_pipelines(stack.engine, skills)
            assert [entry.pipeline_id for entry in report.registered] == ["digest"]
            job_id = await stack.submit("glued-run")
            await stack.service.run(job_id)
            record = await stack.service.get_job(job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "digest",
                "nodes": {"collect": {"collected": True}, "summarize": {"summary": "ok", "model": "small"}},
            }
        finally:
            await stack.close()
