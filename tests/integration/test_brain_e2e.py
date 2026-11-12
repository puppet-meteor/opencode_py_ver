"""P3-S4 e2e (T-C1-079): the main-brain default path over the real full stack.

The genuine full chain, end to end: scripted OpenAI server on real loopback
HTTP -> MaasAdapter SSE -> App (provider composition, ``workflow_tools=True``)
-> the resident BrainDriver session -> the runner's tool round -> the real
``workflow__run`` submit (shared params gate + A fork-3 commit + spawned
engine drive) -> the real LangGraph engine over its own checkpointer file ->
``workflow__status`` poll -> the model's summary.

Two deterministic brain turns (PHASE3 path, the message -> main brain ->
submit -> poll -> reinject -> summarize shape):

* turn 1 prompts the brain; the scripted model calls ``workflow__run``; the
  tool returns the ``job_id`` immediately (two-phase M-5: the run is still
  live in the background), the tool result feeds back as the follow-up
  request's role:"tool" message, and the second generation closes the run.
  The test (not the model) then polls the job projection to its terminal --
  that poll lives outside the model by design.
* turn 2 prompts again; the scripted model calls ``workflow__status`` with
  the CONCRETE job id minted in turn 1 (the reason the scripted server needs
  an append face: the second turn's arguments cannot be written before the
  first turn has run), reads the terminal projection, and summarizes.

Honest-label note: the engine's ``llm_call``/``tool`` bodies are the S2
recording fakes' shape (fixed JSON payloads); the brain, the runner, the
invoker, the permission policy, the jobs commit spine and the compiled
graph around them are all real.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts.events import ToolFinishedData
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import JsonObject
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.session.context import RuntimeSettings
from opencode.storage.jobs_store import JobRecord
from opencode.tools.policy import PermissionRule
from tests.contract.test_pipelines import _spec_raw
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_RAW_SPEC: JsonObject = _spec_raw(
    pipeline_id="digest",
    params_schema={
        "type": "object",
        "properties": {"run": {"type": "string"}},
        "required": ["run"],
        "additionalProperties": False,
    },
)


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model="fake-model"),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


def _text_response(*deltas: str) -> ScriptedResponse:
    """One text stream: a delta per fragment, then a STOP finish (the test_e2e idiom)."""
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def _tool_call_response(call_id: str, name: str, arguments: JsonObject) -> ScriptedResponse:
    """One scripted generation that issues exactly one tool call, then finishes."""
    return ScriptedResponse(
        frames=[
            Frame(tool_call_start_frame(0, call_id, name)),
            Frame(tool_call_args_frame(0, json.dumps(arguments))),
            Frame(finish_frame("tool_calls")),
        ]
    )


class _AppendableServer(FakeOpenAIServer):
    """The scripted server plus an ``append`` face.

    The status turn's tool arguments must name the concrete ``job_id``, which
    only exists after the first turn's submit has committed -- so the second
    turn's script lands through ``append`` between the turns (the base class
    pops one scripted response per request from its list).
    """

    def append(self, response: ScriptedResponse) -> None:
        self._responses.append(response)


class _FakeLlm:
    """The honest S2 fake llm body (the test_engine shape): a fixed summary payload."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"summary": "ok", "model": node.model}


class _FakeTool:
    """The honest S2 fake tool body (the test_engine shape): a fixed collected payload."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"collected": True}


def _wire_messages(body: object) -> list[dict[str, object]]:
    """The request body's message list, narrowed for tolerant wire assertions."""
    assert isinstance(body, dict)
    messages = body.get("messages")
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, dict)]


def _content_str(content: object) -> str:
    """Wire content as text: a plain string or OpenAI content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts)
    return ""


async def _await_job(app: App, job_id: str, *, timeout_s: float = 10.0) -> JobRecord:
    """Poll the job projection to a terminal -- the test drives this poll, never the model."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await app.jobs.get_job(job_id)
        if record.status in {"done", "failed", "cancelled"}:
            return record
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"job {job_id} did not settle within {timeout_s}s")
        await asyncio.sleep(0.02)


async def test_the_brain_submits_polls_and_summarizes_one_workflow(tmp_path: Path) -> None:
    """The main-brain default path: prompt -> workflow__run submit -> (external
    wait) -> workflow__status poll -> summary, over the real full stack."""
    db_path = tmp_path / "brain.db"
    _require_success(_upgrade_head(db_path))

    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(tmp_path / "advisory-cursor.sqlite3"),
        llm_call=_FakeLlm(),
        tool=_FakeTool(),
    )
    await engine.register(_RAW_SPEC)

    server = _AppendableServer(
        responses=[
            _tool_call_response("call_run", "workflow__run", {"pipeline_id": "digest", "params": {"run": "app"}}),
            _text_response("submitted the digest pipeline"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            settings=_settings(),
            pipeline_engine=engine,
            workflow_tools=True,
            rules=(PermissionRule(action="workflow__run", effect="allow"),),
        ) as app:
            assert app.brain is not None
            assert app.registry is not None
            brain = app.brain

            session_id = await brain.ensure_session()
            assert session_id.startswith("ses_")

            # Turn 1: the model submits; the tool returns the job id immediately
            # while the engine drive keeps running in the background (M-5).
            turn_one = await asyncio.wait_for(brain.send(session_id, "run the digest pipeline", "req_1"), 10.0)
            assert turn_one.status.value == "succeeded"
            assert turn_one.session_id == session_id
            assert turn_one.request_id == "req_1"

            job_id: str | None = None
            for event in turn_one.events:
                if event.type != "py.tool.finished" or not isinstance(event.data, ToolFinishedData):
                    continue
                structured = event.data.structured_output
                if isinstance(structured, dict) and "job_id" in structured:
                    candidate = structured["job_id"]
                    assert isinstance(candidate, str)
                    job_id = candidate
            assert job_id is not None

            # The external wait: the test polls the projection to its terminal.
            record = await _await_job(app, job_id)
            assert record.status == "done"
            result = record.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            assert nodes["collect"] == {"collected": True}

            # Turn 2: the scripted status call can now name the concrete job id.
            server.append(_tool_call_response("call_status", "workflow__status", {"job_id": job_id}))
            server.append(_text_response("the digest job is done"))
            turn_two = await asyncio.wait_for(brain.send(session_id, "check the digest job", "req_2"), 10.0)
            assert turn_two.status.value == "succeeded"

            # Wire: four provider requests -- each turn is one tool round (the
            # tool_calls generation plus the result-fed follow-up).
            assert len(server.requests) == 4
            first_body = server.requests[0].body
            assert isinstance(first_body, dict)
            offered = json.dumps(first_body)
            assert "workflow__run" in offered  # the resident brain's catalog carries the pair
            assert "workflow__status" in offered
            first_messages = _wire_messages(first_body)
            assert first_messages[-1].get("role") == "user"
            assert _content_str(first_messages[-1].get("content")) == "run the digest pipeline"

            submit_follow_up = _wire_messages(server.requests[1].body)
            submit_tools = [
                message
                for message in submit_follow_up
                if message.get("role") == "tool" and message.get("tool_call_id") == "call_run"
            ]
            assert len(submit_tools) == 1
            assert job_id in _content_str(submit_tools[0].get("content"))  # the submit reinjection

            poll_follow_up = _wire_messages(server.requests[3].body)
            poll_tools = [
                message
                for message in poll_follow_up
                if message.get("role") == "tool" and message.get("tool_call_id") == "call_status"
            ]
            assert len(poll_tools) == 1
            poll_text = _content_str(poll_tools[0].get("content"))
            assert "status=done" in poll_text  # the poll reinjection carries the terminal projection
            assert "collected" in poll_text  # ...and the terminal result payload

            # The two-channel discipline (PHASE3 §4): job events never ride the
            # brain's session stream -- the brain saw only its own tool round.
            assert not [event for event in turn_one.events if event.type.startswith("py.job.")]
            # The symmetric turn-2 zero (T-B-094 R5 addendum, the S5a rider):
            # the status-poll round stays on the tool channel too -- no py.job.*
            # event ever rode the brain's session stream in either turn.
            assert not [event for event in turn_two.events if event.type.startswith("py.job.")]

            # The brain-direction registry pin (C#5): the resident brain's own
            # registry resolves the workflow pair for its session.
            catalog = app.registry.snapshot(session_id)
            assert catalog.resolve("workflow__run") is not None
            assert catalog.resolve("workflow__status") is not None
    finally:
        await server.aclose()
