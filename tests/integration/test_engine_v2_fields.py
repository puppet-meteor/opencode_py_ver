"""The v2 node-field engine semantics end to end over the real graph.

``transform`` / ``output_format`` / ``instance_binding`` / ``fallback_tools``
(the T-C1-086 four fields) and the T-C1-084 execution faces (``params``
refs, ``condition`` skips, ``extract`` projection, the structured-output
fallback) are native v1 spec faces (the four-field mapping is the
framework repo's emitter face, ruling-18; the pydialect converter that
once gated them was deleted by T-C1-087 -- a dialect graph.json no longer
routes anywhere), so these tests register plain v1 wire objects carrying
the fields and drive them through the same real-compiled-graph +
advisory-cursor spine as ``tests/integration/test_engine.py``, with
recording fake bodies (the S1 fake-executor discipline: the injected body
is the single tested seam; the semantic core -- the write point, the
trigger predicate, the chain, the render -- is the production engine,
never mocked).

The ported semantics under test (framework line references in the engine's
docstrings): transform computes derived fields onto the node's own result and
never fails the node (a failing op is skipped, disclosed); output_format is
the deterministic zero-LLM field_map render and becomes the written result;
the fallback chain fires on the ported trigger predicate (empty primary /
empty extract / missing expected fields), resolves each attempt's own params
through the existing node-params machinery, adopts the first attempt with no
remaining reasons, records every attempt, and an exhausted chain keeps the
primary; instance_binding resolves the query id into the node's params view
without ever overriding params (the authority). ``TestV2Execution`` below is
the T-C1-087 native recast of the retired converter-seam tests: params refs
resolve at node entry into the ephemeral body view, a false condition writes
the explicit skip record, refs onto skipped steps fail the job honestly, and
a skipped terminal node completes with the skip record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from opencode.orchestrator.engine import is_skip_record, node_params_of
from opencode.orchestrator.llm_call import render_prompt
from tests.integration.test_engine import _checkpoint_state, _FakeLlm, _FakeTool, _Stack


class _ScriptedTool(_FakeTool):
    """Records every call (node, tool_ref, resolved params); returns scripted payloads.

    The script maps ``tool_ref`` -> the payload the fake body returns (an
    exception instance raises); calls record the resolved ``node_params``
    view so the tests can prove the primary and the fallback attempts each
    bound their own params through the existing machinery.
    """

    def __init__(self, script: dict[str, Any]) -> None:
        super().__init__()
        self.script = script
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        from opencode.orchestrator.engine import node_params_of

        params = node_params_of(state)
        self.seen.append((node.node_id, str(node.tool_ref), dict(params)))
        scripted = self.script.get(str(node.tool_ref), {"collected": True})
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted


def _raw(pipeline_id: str, nodes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """One native v1 wire object (entry = the first node); extra keys splice in."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": nodes[0]["node_id"],
        "nodes": nodes,
        "edges": extra.pop("edges", []),
    }
    raw.update(extra)
    return raw


class TestTransform:
    """The transform write face: derived fields computed onto the node's own result."""

    async def test_transform_computes_derived_fields_at_the_write_point(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {"occur_utc": 1690000000000, "region": "cn", "status": "ready"}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "transforms",
                [
                    {
                        "node_id": "compute",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "transform": {
                            "start_time": {"op": "subtract", "args": [{"field": "occur_utc"}, {"literal": 7200000}]},
                            "label": {"op": "concat", "args": [{"field": "region"}, {"literal": "-prod"}]},
                            "flag": {
                                "op": "if_equals",
                                "source": {"field": "status"},
                                "value": "ready",
                                "then": 1,
                                "else": 0,
                            },
                            "code": {"op": "int", "args": [{"literal": "42"}]},
                            "fallback_free": {"op": "default", "args": [{"field": "missing"}], "default": "filled"},
                        },
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("transforms", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "transforms",
                "nodes": {
                    "compute": {
                        "occur_utc": 1690000000000,
                        "region": "cn",
                        "status": "ready",
                        "start_time": 1689992800000,
                        "label": "cn-prod",
                        "flag": 1,
                        "code": 42,
                        "fallback_free": "filled",
                    }
                },
            }
        finally:
            await stack.close()

    async def test_transform_failures_and_reserved_names_skip_without_failing_the_node(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {"value": "a,b"}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "skipping",
                [
                    {
                        "node_id": "compute",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "transform": {
                            "boom": {"op": "copy", "args": [{"field": "ghost"}]},
                            "explode": {"op": "divide", "args": [{"literal": 1}, {"literal": 0}]},
                            "split_csv": {"source": "value", "target": "items"},
                        },
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("skipping", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # The failing op and the reserved framework compute transform (no
            # native executor) are both skipped: no derived fields, no failure.
            assert record.result == {
                "pipeline_id": "skipping",
                "nodes": {"compute": {"value": "a,b"}},
            }
        finally:
            await stack.close()


class TestOutputFormat:
    """The output_format write face: the deterministic field_map render."""

    async def test_output_format_renders_the_field_map_as_the_written_result(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {"direct": "d", "deep": {"nested": "n"}, "scalar": "s"}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "rendered",
                [
                    {
                        "node_id": "shape",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "output_format": {
                            "kind": "field_map",
                            "fields": {
                                "direct": {"type": "string"},
                                "nested": {"type": "string", "example": "never"},
                                "filled": {"default": "the-default"},
                                "scalar": {"type": "array"},
                            },
                        },
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("rendered", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # Direct key resolution wins over the example; the nested finder
            # resolves before the example/default fallback; a scalar coerces
            # to the declared array. The rendered map is the written result.
            assert record.result == {
                "pipeline_id": "rendered",
                "nodes": {"shape": {"direct": "d", "nested": "n", "filled": "the-default", "scalar": ["s"]}},
            }
        finally:
            await stack.close()


class TestFallbackTools:
    """The fallback chain: trigger predicate, chain order, adoption, disclosure."""

    async def test_the_fallback_chain_adopts_the_first_non_empty_attempt_with_history(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"src": {"data": "d1"}, "primary": {}, "fb_empty": {}, "fb_good": {"collected": True}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "chain",
                [
                    {"node_id": "step_1", "node_type": "tool", "tool_ref": "src"},
                    {
                        "node_id": "step_2",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "fallback_tools": [
                            {"tool": "fb_empty", "params": {"payload": {"ref": "step_1.data"}}},
                            {"tool": "fb_good", "params": {"payload": {"ref": "step_1.data"}}},
                        ],
                    },
                ],
                edges=[{"from_node": "step_1", "to_node": "step_2"}],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("chain", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "chain",
                "nodes": {
                    "step_1": {"data": "d1"},
                    "step_2": {
                        "collected": True,
                        "fallback_history": [
                            {
                                "tool": "fb_empty",
                                "trigger_reasons": ["primary_data_empty"],
                                "status": "rejected",
                                "missing_after": ["primary_data_empty"],
                            },
                            {"tool": "fb_good", "trigger_reasons": ["primary_data_empty"], "status": "adopted"},
                        ],
                    },
                },
            }
            # The primary ran, then the chain in declared order; each attempt
            # resolved its own params through the existing node-params
            # machinery and rode the same injected tool body.
            assert [(node, ref) for node, ref, _ in tool.seen] == [
                ("step_1", "src"),
                ("step_2", "primary"),
                ("step_2", "fb_empty"),
                ("step_2", "fb_good"),
            ]
            assert tool.seen[2][2] == {"payload": "d1"}
            assert tool.seen[3][2] == {"payload": "d1"}
        finally:
            await stack.close()

    async def test_the_fallback_trigger_fires_on_missing_expected_fields(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {"other": 1}, "fb": {"x": 9}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "gated",
                [
                    {
                        "node_id": "needs_x",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "output_format": {"kind": "field_map", "fields": {"x": {"type": "integer"}}},
                        "fallback_tools": [{"tool": "fb", "params": {}}],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("gated", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # A non-empty primary still trips the predicate when the declared
            # output fields stay empty in the materialized value -- the ported
            # framework nuance (fallback is not only about the empty envelope).
            assert record.result == {
                "pipeline_id": "gated",
                "nodes": {
                    "needs_x": {
                        "x": 9,
                        "fallback_history": [
                            {"tool": "fb", "trigger_reasons": ["missing_expected_fields:x"], "status": "adopted"}
                        ],
                    }
                },
            }
        finally:
            await stack.close()

    async def test_an_exhausted_fallback_chain_keeps_the_primary_result(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {"kept": "primary-value"}, "fb1": {}, "fb2": {}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "exhausted",
                [
                    {
                        "node_id": "node",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "output_format": {"kind": "field_map", "fields": {"x": {"type": "string"}}},
                        "fallback_tools": [
                            {"tool": "fb1", "params": {}},
                            {"tool": "fb2", "params": {}},
                        ],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("exhausted", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # The framework re-evaluates the FULL trigger predicate over each
            # attempt (pydag_engine.py L6461-6474): an empty attempt trips both
            # the empty-envelope and the missing-field reasons, so both entries
            # stay rejected and the chain exhausts keeping the raw primary.
            assert record.result == {
                "pipeline_id": "exhausted",
                "nodes": {
                    "node": {
                        "kept": "primary-value",
                        "fallback_history": [
                            {
                                "tool": "fb1",
                                "trigger_reasons": ["missing_expected_fields:x"],
                                "status": "rejected",
                                "missing_after": ["primary_data_empty", "missing_expected_fields:x"],
                            },
                            {
                                "tool": "fb2",
                                "trigger_reasons": ["missing_expected_fields:x"],
                                "status": "rejected",
                                "missing_after": ["primary_data_empty", "missing_expected_fields:x"],
                            },
                        ],
                    }
                },
            }
        finally:
            await stack.close()

    async def test_a_failing_attempt_is_recorded_and_the_chain_continues(self, tmp_path: Path) -> None:
        tool = _ScriptedTool(
            {"primary": {}, "fb_boom": RuntimeError("fake fallback exploded"), "fb_good": {"collected": True}}
        )
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "resilient",
                [
                    {
                        "node_id": "node",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "fallback_tools": [
                            {"tool": "fb_boom", "params": {}},
                            {"tool": "fb_good", "params": {}},
                        ],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("resilient", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # The raised attempt is an exception record, never a job failure;
            # the chain continues and the next entry adopts.
            assert record.result == {
                "pipeline_id": "resilient",
                "nodes": {
                    "node": {
                        "collected": True,
                        "fallback_history": [
                            {
                                "tool": "fb_boom",
                                "trigger_reasons": ["primary_data_empty"],
                                "status": "exception",
                                "error": "fake fallback exploded",
                            },
                            {"tool": "fb_good", "trigger_reasons": ["primary_data_empty"], "status": "adopted"},
                        ],
                    }
                },
            }
        finally:
            await stack.close()

    async def test_the_adopted_fallback_result_flows_through_extract_and_transform(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"primary": {}, "fb": {"result": {"answer": "yes"}}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "projected",
                [
                    {
                        "node_id": "node",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "extract": {"answer": "result.answer"},
                        "transform": {"tag": {"op": "concat", "args": [{"field": "answer"}, {"literal": "-ok"}]}},
                        "fallback_tools": [{"tool": "fb", "params": {}}],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("projected", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # The primary trips both the empty-envelope and the empty-extract
            # reasons; the adopted attempt's own result flows through the same
            # extract projection and transform semantics before adoption.
            assert record.result == {
                "pipeline_id": "projected",
                "nodes": {
                    "node": {
                        "answer": "yes",
                        "tag": "yes-ok",
                        "fallback_history": [
                            {
                                "tool": "fb",
                                "trigger_reasons": ["primary_data_empty", "extracted_empty"],
                                "status": "adopted",
                            }
                        ],
                    }
                },
            }
        finally:
            await stack.close()

    async def test_a_non_dict_adopted_value_rides_the_wrapped_consumer_form(self, tmp_path: Path) -> None:
        """T-C1-087 scheduler rider (the T-B-097 suggestion-1 commitment): the
        non-dict value that reaches the consumer is not the dict-merge face --
        it rides the wrapped ``{"value": ..., "fallback_history": [...]}``
        form (the 086 OPEN item's native-defined shape).

        Reachability note (framework authority, pydag_engine.py
        ``_fallback_trigger_reasons`` L7214-7247): a non-dict materialization
        is ITSELF a trigger reason, so a non-dict attempt can never be
        adopted -- the wrapped form is the EXHAUSTED-chain face: the primary
        below is a non-empty list (not "primary_data_empty", but
        ``materialized_output_missing`` fires the chain), the attempt is an
        empty dict (rejected on its own re-evaluation), the chain exhausts,
        and the surviving non-dict primary carries the history record in the
        wrapped form."""
        tool = _ScriptedTool({"primary": ["wrapped", "value"], "fb": {}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "wrapped",
                [
                    {
                        "node_id": "node",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "fallback_tools": [{"tool": "fb", "params": {}}],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("wrapped", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # The consumer face: the exhausted chain keeps the non-dict
            # primary, and it rides the wrapped form (a dict would have
            # merged fallback_history alongside its keys).
            assert record.result == {
                "pipeline_id": "wrapped",
                "nodes": {
                    "node": {
                        "value": ["wrapped", "value"],
                        "fallback_history": [
                            {
                                "tool": "fb",
                                "trigger_reasons": ["materialized_output_missing"],
                                "status": "rejected",
                                "missing_after": ["primary_data_empty"],
                            }
                        ],
                    }
                },
            }
        finally:
            await stack.close()

    async def test_each_fallback_attempt_prints_one_compact_stderr_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ticket's stderr disclosure (the ADR-0002 L60 precedent -- a
        compact, non-fatal one-liner per attempt): rejected for the empty
        attempt, exception for the raised one, adopted for the winner."""
        tool = _ScriptedTool(
            {
                "primary": {},
                "fb_empty": {},
                "fb_boom": RuntimeError("fake fallback exploded"),
                "fb_good": {"collected": True},
            }
        )
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "disclosed",
                [
                    {
                        "node_id": "node",
                        "node_type": "tool",
                        "tool_ref": "primary",
                        "fallback_tools": [
                            {"tool": "fb_empty", "params": {}},
                            {"tool": "fb_boom", "params": {}},
                            {"tool": "fb_good", "params": {}},
                        ],
                    }
                ],
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("disclosed", {}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # One line per attempt on the operator channel; the durable
            # structured record is already pinned by the tests above.
            err = capsys.readouterr().err
            assert "opencode: node node fallback fb_empty: rejected" in err
            assert "(missing_after: primary_data_empty)" in err
            assert "opencode: node node fallback fb_boom: exception" in err
            assert "(fake fallback exploded)" in err
            assert "opencode: node node fallback fb_good: adopted" in err
        finally:
            await stack.close()


class TestInstanceBinding:
    """The instance_binding face: the query id renders into the params view."""

    _BINDING: ClassVar[dict[str, Any]] = {
        "node_type": "nas",
        "source": {"source": {"need": "nas_instances", "from": "any_previous"}},
        "resource_id": {"ref": "request.resource_id"},
        "query_id_param": "instance_id",
        "query_id_template": "{cluster_id={cluster_id},instance_id={resource_id}}",
    }

    async def test_instance_binding_renders_the_query_id_into_the_params_view(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"probe": {"collected": True}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "binding",
                [
                    {
                        "node_id": "probe",
                        "node_type": "tool",
                        "tool_ref": "probe",
                        "params": {"cluster_id": {"ref": "inputs.cluster_id"}},
                        "instance_binding": self._BINDING,
                    }
                ],
                params_schema={
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}, "cluster_id": {"type": "string"}},
                },
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("binding", {"resource_id": "r-1", "cluster_id": "c-1"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # The deterministic template face: literal outer braces, the
            # resource_id resolved from the request envelope (the submitted
            # params) and cluster_id from the node's own resolved params.
            assert tool.seen == [
                ("probe", "probe", {"cluster_id": "c-1", "instance_id": "{cluster_id=c-1,instance_id=r-1}"})
            ]
        finally:
            await stack.close()

    async def test_instance_binding_never_overrides_params_the_authority(self, tmp_path: Path) -> None:
        tool = _ScriptedTool({"probe": {"collected": True}})
        stack = _Stack(tmp_path, llm=_FakeLlm(), tool=tool)
        try:
            raw = _raw(
                "authority",
                [
                    {
                        "node_id": "probe",
                        "node_type": "tool",
                        "tool_ref": "probe",
                        "params": {
                            "cluster_id": {"ref": "inputs.cluster_id"},
                            "instance_id": "explicit-id",
                        },
                        "instance_binding": self._BINDING,
                    }
                ],
                params_schema={
                    "type": "object",
                    "properties": {"resource_id": {"type": "string"}, "cluster_id": {"type": "string"}},
                },
            )
            await stack.engine.register(raw)
            handle = await stack.service.submit("authority", {"resource_id": "r-1", "cluster_id": "c-1"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            # The binding is provenance: the step's own params carry the
            # query id, so the render stays inert (params are the authority).
            assert tool.seen == [("probe", "probe", {"cluster_id": "c-1", "instance_id": "explicit-id"})]
        finally:
            await stack.close()


# ---------------------------------------------------------------------------
# The T-C1-084 execution faces, native recast (T-C1-087): params refs,
# condition skips, extract projection, the structured-output fallback --
# the semantics the retired pydialect converter-seam tests drove through
# converted graphs, now pinned over plain v1 wire objects.
# ---------------------------------------------------------------------------

#: The structured-output schema step_2 declares (strict: exactly one string
#: "answer" key -- a completion the scripted provider returns either
#: satisfies or clearly does not).
_V2_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _v2_raw(*, condition: str = "step_1.collected is true") -> dict[str, Any]:
    """One native v2 spec: tool step_1 -> llm step_2 -> tool step_3.

    The declared ``params_schema`` is the closure root for the ``inputs``
    refs (the dialect form carried ``input_vars``; the retired converter
    derived the schema from them). step_1 binds an ``inputs`` ref param,
    step_2 carries a condition, a prompt with a step ref (the
    ``{step_1[sop_data]}`` index-chain form), the structured-output schema
    and its fallback, and step_3 binds a step-ref param and projects its
    own result through ``extract`` (a first-hit-wins chain plus a plain
    path).
    """
    return {
        "schema_version": 1,
        "pipeline_id": "demo_v2",
        "entry": "step_1",
        "params_schema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}, "sop_name": {"type": "string"}},
        },
        "nodes": [
            {
                "node_id": "step_1",
                "node_type": "tool",
                "tool_ref": "demo_fetch",
                "params": {"name": {"ref": "inputs.sop_name"}, "ticket_id": "T-1"},
            },
            {
                "node_id": "step_2",
                "node_type": "llm_call",
                "model": "small",
                "prompt_template": "Summarize {step_1[sop_data]} for {sop_name}.",
                "condition": condition,
                "output_schema": _V2_OUTPUT_SCHEMA,
                "fallback": {"answer": "no summary"},
            },
            {
                "node_id": "step_3",
                "node_type": "tool",
                "tool_ref": "demo_publish",
                "params": {"payload": {"ref": "step_2.answer"}},
                "extract": {"published": ["result.data", "result"], "answer": "result.answer"},
            },
        ],
        "edges": [
            {"from_node": "step_1", "to_node": "step_2"},
            {"from_node": "step_2", "to_node": "step_3"},
        ],
    }


async def _run_v2(stack: _Stack, *, condition: str = "step_1.collected is true") -> Any:
    """Register the v2 spec and drive one job; returns the settled record."""
    await stack.engine.register(_v2_raw(condition=condition))
    handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
    await stack.service.run(handle.job_id)
    return await stack.service.get_job(handle.job_id)


class _V2Tool(_FakeTool):
    """The v2 fake tool: records the resolved ``node_params`` view per node."""

    def __init__(self) -> None:
        super().__init__()
        self.params: dict[str, dict[str, Any]] = {}

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("ticket_id", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        resolved = node_params_of(state)
        self.params[node.node_id] = dict(resolved)
        if node.node_id == "step_1":
            return {
                "collected": True,
                "name": resolved["name"],
                "sop_data": {"steps": ["stop", "start"], "title": "restart"},
            }
        payload = resolved["payload"]
        return {"result": {"answer": payload, "data": {"answer": payload, "ok": True}}}


class _V2Llm(_FakeLlm):
    """The v2 fake llm: renders through the production renderer over the merged context."""

    def __init__(self) -> None:
        super().__init__()
        self.rendered: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        marker = str(state.get("params", {}).get("ticket_id", "?"))
        self.calls.setdefault(marker, []).append(node.node_id)
        params = dict(state.get("params", {}))
        results = {
            key: value for key, value in dict(state.get("node_results", {})).items() if not is_skip_record(value)
        }
        text = render_prompt(node.prompt_template, {**params, **results})
        self.rendered.append(text)
        return {"answer": text, "model": node.model}


class TestV2Execution:
    async def test_params_refs_condition_extract_flow_end_to_end(self, tmp_path: Path) -> None:
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            bound = await stack.engine.register(_v2_raw())
            assert bound.spec.nodes[0].params == {
                "name": {"ref": "inputs.sop_name"},
                "ticket_id": "T-1",
            }
            assert bound.spec.nodes[1].condition == "step_1.collected is true"
            assert bound.spec.nodes[1].output_schema == _V2_OUTPUT_SCHEMA
            assert bound.spec.nodes[1].fallback == {"answer": "no summary"}
            assert bound.spec.nodes[2].extract == {
                "published": ["result.data", "result"],
                "answer": "result.answer",
            }

            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            # The step ref resolved into the real JSON object (canonical
            # JSON, sorted keys), the inputs ref into the submitted param.
            answer = 'Summarize {"steps":["stop","start"],"title":"restart"} for runbook-a.'
            assert record.result == {
                "pipeline_id": "demo_v2",
                "nodes": {
                    "step_1": {
                        "collected": True,
                        "name": "runbook-a",
                        "sop_data": {"steps": ["stop", "start"], "title": "restart"},
                    },
                    "step_2": {"answer": answer, "model": "small"},
                    # extract replaced step_3's downstream-visible result:
                    # the chain's first hit won and the plain path resolved.
                    "step_3": {"published": {"answer": answer, "ok": True}, "answer": answer},
                },
            }
            # The wrapper resolved the refs and handed the bodies the view.
            assert tool.params["step_1"] == {"name": "runbook-a", "ticket_id": "T-1"}
            assert tool.params["step_3"] == {"payload": answer}
            assert llm.rendered == [answer]
            assert tool.calls["T-9"] == ["step_1", "step_3"]
            assert llm.calls["T-9"] == ["step_2"]
            # node_params is the ephemeral body view, never a channel: the
            # pinned checkpoint channel set gains no v2 key.
            values = await _checkpoint_state(stack, handle.job_id)
            assert "node_params" not in values
            assert set(values) <= {
                "params",
                "params_hash",
                "pipeline_id",
                "pipeline_spec_hash",
                "attempt_id",
                "node_results",
                "outcome",
                "result",
            }
        finally:
            await stack.close()

    async def test_a_false_condition_skips_the_node_and_downstream_refs_fail_honestly(self, tmp_path: Path) -> None:
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            record = await _run_v2(stack, condition="step_1.collected is false")
            assert record.status == "failed"
            assert record.error is not None
            # The WorkerError keeps its own §8 code out of the node failure.
            assert record.error["code"] == "ValidationError"
            assert record.error["message"] is not None
            assert "references skipped step 'step_2'" in str(record.error["message"])
            details = record.error["details"]
            assert isinstance(details, dict)
            assert details["node_id"] == "step_3"
            # The llm body never ran; the tool ran only its non-skipped part.
            assert llm.rendered == []
            assert tool.calls["T-9"] == ["step_1"]
        finally:
            await stack.close()

    async def test_a_condition_ref_to_a_skipped_step_fails_the_job_honestly(self, tmp_path: Path) -> None:
        """step_1's own condition is false; step_2's condition refs the skipped step."""
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            raw = _v2_raw()
            raw["nodes"][0]["condition"] = "inputs.skip_first is true"
            raw["nodes"][1]["condition"] = "step_1.collected is true"
            await stack.engine.register(raw)
            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "ValidationError"
            assert "references skipped step 'step_1'" in str(record.error["message"])
            details = record.error["details"]
            assert isinstance(details, dict)
            assert details["node_id"] == "step_2"
            # Neither body ever ran: step_1 skipped, step_2 failed at its gate.
            assert tool.calls == {}
            assert llm.calls == {}
        finally:
            await stack.close()

    async def test_a_skipped_terminal_node_completes_with_the_skip_record(self, tmp_path: Path) -> None:
        """A false condition on the terminal node: outcome done, the explicit skip record."""
        llm = _V2Llm()
        tool = _V2Tool()
        stack = _Stack(tmp_path, llm=llm, tool=tool)
        try:
            raw = _v2_raw(condition="step_1.collected is false")
            raw["nodes"].pop()  # step_3 gone: step_2 becomes the terminal node
            raw["edges"].pop()
            # An extract on the skipped node never projects: the skip record
            # is the marker, not a projectable result.
            raw["nodes"][1]["extract"] = {"answer": "answer"}
            await stack.engine.register(raw)
            handle = await stack.service.submit("demo_v2", {"ticket_id": "T-9", "sop_name": "runbook-a"}, "api")
            await stack.service.run(handle.job_id)
            record = await stack.service.get_job(handle.job_id)
            assert record.status == "done"
            assert record.error is None
            assert record.result == {
                "pipeline_id": "demo_v2",
                "nodes": {
                    "step_1": {
                        "collected": True,
                        "name": "runbook-a",
                        "sop_data": {"steps": ["stop", "start"], "title": "restart"},
                    },
                    "step_2": {
                        "skipped": True,
                        "condition": "step_1.collected is false",
                        "reason": "condition evaluated to false: step_1.collected is false",
                    },
                },
            }
            assert llm.rendered == []
        finally:
            await stack.close()
