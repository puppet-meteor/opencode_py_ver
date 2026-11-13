"""Integration: the production pipeline-engine composition (P4-S1, T-C1-082).

The exact assembly the CLI and the HTTP server now wire -- ``EngineCheckpointer``
at the DB-derived sqlite path (M-7: its own file), the real ``MaasLlmCall``
body over the ported ``MaasAdapter``, the honest fail-closed tool body, and
``App(pipeline_engine=...)`` routing the jobs service onto the engine -- driven
end to end against ``tests/r0/fake_openai_server.py`` over real loopback HTTP:
register a pipeline with an ``llm_call`` node, submit a job, and pin that
``node_results`` carries the fake model's text. The scripted HTTP 500 pins the
``ProviderUnavailable`` §8 face, and the host-only test pins that the provider
assembly values (base_url/api_key/resolved model id) never leak into any event
payload or the advisory-cursor file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.llm_call import MaasLlmCall, checkpointer_path_for, unsupported_tool_body
from opencode.session.context import RuntimeSettings
from tests.integration.test_engine import _upgrade_head
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
)

_API_KEY = "llm-e2e-key"
_MODEL_ID = "gpt-fake-mini"


def _pipeline_spec() -> JsonObject:
    """One single-node llm_call pipeline: tier 'small', one param placeholder."""
    return {
        "schema_version": 1,
        "pipeline_id": "digest",
        "entry": "summarize",
        "nodes": [
            {
                "node_id": "summarize",
                "node_type": "llm_call",
                "model": "small",
                "prompt_template": "Summarize: {topic}",
            }
        ],
        "edges": [],
    }


def _ok_response(*deltas: str) -> ScriptedResponse:
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model=_MODEL_ID),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


async def _drive_one_job(
    tmp_path: Path,
    server: FakeOpenAIServer,
) -> tuple[str, JsonValue | None, JsonObject | None]:
    """The production composition over the fake server: one job, driven to terminal."""
    db_path = tmp_path / "main.db"
    assert _upgrade_head(db_path).returncode == 0
    provider = MaasAdapter(f"{server.base_url}/v1", _API_KEY)
    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
        llm_call=MaasLlmCall(provider=provider, default_model=ModelRef(provider="maas", model=_MODEL_ID)),
        tool=unsupported_tool_body,
    )
    await engine.register(_pipeline_spec())
    async with App(
        db_path,
        working_dir=str(tmp_path),
        provider=provider,
        settings=_settings(),
        pipeline_engine=engine,
    ) as app:
        handle = await app.jobs.submit("digest", {"topic": "the release notes"}, "api")
        await app.jobs.run(handle.job_id)
        record = await app.jobs.get_job(handle.job_id)
        return record.status, record.result, record.error


async def test_production_engine_drives_an_llm_call_node_over_the_fake_provider(tmp_path: Path) -> None:
    server = FakeOpenAIServer(responses=[_ok_response("sum", "mary")])
    await server.start()
    try:
        status, result, error = await _drive_one_job(tmp_path, server)
    finally:
        await server.aclose()
    assert status == "done", f"error: {error}"
    assert result == {
        "pipeline_id": "digest",
        "nodes": {"summarize": {"text": "summary", "model": "small", "finish_reason": "stop"}},
    }
    # The wire face: exactly one provider call, the tier fell back to the
    # default model (no tier table in the production composition), and the
    # prompt rendered the submitted param into the user message.
    assert len(server.requests) == 1
    body = server.requests[0].body
    assert isinstance(body, dict)
    assert body["model"] == _MODEL_ID
    assert body["messages"] == [{"role": "user", "content": "Summarize: the release notes"}]
    assert body["stream"] is True
    assert body["max_tokens"] == 4096


async def test_a_scripted_500_fails_the_job_with_the_provider_unavailable_face(tmp_path: Path) -> None:
    server = FakeOpenAIServer(responses=[ScriptedResponse(status=500)])
    await server.start()
    try:
        status, result, error = await _drive_one_job(tmp_path, server)
    finally:
        await server.aclose()
    assert status == "failed"
    assert result is None
    assert error is not None
    assert error["code"] == "ProviderUnavailable"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["node_id"] == "summarize"
    assert details["node_type"] == "llm_call"


async def test_no_host_provider_values_leak_into_events_or_the_checkpointer(tmp_path: Path) -> None:
    """The host-only pin: base_url/api_key/resolved model id never reach a durable face."""
    server = FakeOpenAIServer(responses=[_ok_response("summary")])
    await server.start()
    try:
        status, _, error = await _drive_one_job(tmp_path, server)
    finally:
        await server.aclose()
    assert status == "done", f"error: {error}"
    db_path = tmp_path / "main.db"
    needles = (server.base_url, _API_KEY, _MODEL_ID)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM events").fetchall()
    assert rows, "the job committed no events"
    events_blob = "\n".join(str(row) for row in rows)
    for needle in needles:
        assert needle not in events_blob, f"host-only leak into an event payload: {needle}"
    checkpoint_bytes = checkpointer_path_for(db_path).read_bytes()
    for needle in needles:
        assert needle.encode() not in checkpoint_bytes, f"host-only leak into the advisory cursor: {needle}"
