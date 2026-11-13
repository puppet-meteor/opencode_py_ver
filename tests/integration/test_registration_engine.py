"""Integration tests for the P5-S1 registration face (T-C1-085) over the REAL engine.

The manifest -> engine glue driven end to end: a tmp skills tree whose
graph.json is a real pipeline spec (the v1 wire form and the
``pydag-editable-graph-v1`` dialect -- the two production sources), the
real ``SkillCatalog`` discovery gate, the real ``PipelineEngine``
three-layer registration gate and its real ``EngineCheckpointer``
(aiosqlite, loop-bound). The ``llm_call``/``tool`` bodies are the
test_engine.py fakes (the S2 discipline) -- registration never executes
node bodies, and the one driven job uses the digest chain they serve.

Coverage (the T-C1-085 acceptance judges):

* the v1 wire form: registered and bound under the spec's own
  ``pipeline_id``, with the engine's spec hash (the pinned v1 face);
* the pydag dialect: routed through the converter at the loading seam and
  bound with the converted spec's hash;
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
from opencode.contracts.pydag_import import convert_pydag_graph
from opencode.orchestrator.registration import register_pipelines
from tests.contract.test_pipelines import _spec_raw
from tests.contract.test_pydag_import import _pydag_raw
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


def _write_langgraph_skills(skills: Path, graph_payload: object) -> Path:
    """One tmp skills tree: the manifest with one langgraph entry + its graph.json."""
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "skills.json").write_text(json.dumps({"skills": [_LANGGRAPH_ENTRY]}), encoding="utf-8")
    pipeline: str = _LANGGRAPH_ENTRY["pipeline"]
    target = skills / pipeline
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        graph_payload if isinstance(graph_payload, str) else json.dumps(graph_payload),
        encoding="utf-8",
    )
    return skills


class TestRealEngineRegistration:
    async def test_v1_graph_json_registers_and_binds_the_spec_hash(self, tmp_path: Path) -> None:
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

    async def test_pydag_dialect_registers_through_the_converter_seam(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=_FakeTool())
        try:
            raw = _pydag_raw()
            skills = _write_langgraph_skills(tmp_path / "skills", raw)
            report = await register_pipelines(stack.engine, skills)
            assert report.rejected == ()
            (registered,) = report.registered
            assert registered.pipeline_id == "demo"
            bound = stack.engine.bound("demo")
            assert bound is not None
            assert bound.spec_hash == compute_spec_hash(convert_pydag_graph(raw))
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
