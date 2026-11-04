"""S8 closeout: the full-chain e2e over the real stack (T-C1-024 ①-④).

Three pins over the genuine full stack -- scripted OpenAI server on real
loopback HTTP -> MaasAdapter SSE -> App -> SessionService/runner/step engine
-> Invoker -> the real ``fs__read`` builtin reading a real file in the
session working directory:

① ``test_main_chain_multiround_text_then_tool_round`` -- two pure-text rounds
   (the second a multi-segment delta stream finalized into a single part),
   then a full tool round: the scripted tool_calls fan out to a real
   ``fs__read`` on a tmp-tree file, the rendered page feeds back verbatim as
   the role:"tool" message of the follow-up request, and the SECOND
   generation closes the run. Store projection order, user-text round-trips,
   wire tool_call_id pairing, and the ③ generation/tool-execution counts
   (durable EventLog SQL) are all asserted.
② ``test_interrupted_partial_stream_next_request_starts_fresh`` -- a scripted
   mid-stream break (clean body end without the SSE [DONE] terminal) lands
   the run in INTERRUPTED; the next request on the SAME session starts fresh:
   its own user turn drives a clean generation whose finalized text is
   exactly the new script -- no fragment rejoin.
④ ``test_event_log_records_the_full_lifecycle_in_order`` -- the durable
   EventLog carries the full lifecycle as an ordered subsequence
   (session.created -> prompt.accepted -> run.started -> step lifecycle ->
   tool lifecycle -> run.finished) and never persists ephemeral deltas.

Fixture reuse is read-only: the FakeOpenAIServer/Frame/ScriptedResponse
helpers come from ``tests/r0`` and the alembic ``_upgrade_head`` /
``_await_terminal`` idioms are reconstructed in the same shape as
``tests/integration/test_messages_union.py``; no existing file is modified.
Direct sqlite reads happen strictly after durable terminals and app close
(the S7 incident discipline).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    UserMessage,
)
from opencode.session.context import RuntimeSettings
from opencode.session.service import RunStatus, SessionService
from opencode.tools.builtin.read import FsReadAdapter
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


async def _await_terminal(service: SessionService, run_id: str, *, timeout_s: float = 10.0) -> RunStatus:
    """Poll the durable run status (§7 L314) until it reaches a terminal."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status.is_terminal:
            return status
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} did not reach a terminal status within {timeout_s}s")
        await asyncio.sleep(0.02)


def _text_response(*deltas: str) -> ScriptedResponse:
    """One text stream: a delta per fragment, then a STOP finish (the test_cli idiom)."""
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def _tool_round_response() -> ScriptedResponse:
    """The scripted first generation of a tool round: preamble text, one
    ``fs__read`` call on ``note.txt`` (relative to the working directory),
    and a ``tool_calls`` finish."""
    return ScriptedResponse(
        frames=[
            Frame(text_delta_frame("let me check")),
            Frame(tool_call_start_frame(0, "call_read_note", "fs__read")),
            Frame(tool_call_args_frame(0, json.dumps({"path": "note.txt"}))),
            Frame(finish_frame("tool_calls")),
        ]
    )


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


def _assistant_text(message: AssistantMessage) -> str:
    """Concatenated text parts of an assistant message."""
    return "".join(part.text for part in message.parts if isinstance(part, AssistantTextPart))


def _event_types(db_path: Path, session_id: str) -> list[str]:
    """Durable EventLog types for one session, in seq order (post-close read)."""
    with sqlite3.connect(db_path) as connection:
        return [
            str(row[0])
            for row in connection.execute("SELECT type FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
        ]


async def test_main_chain_multiround_text_then_tool_round(tmp_path: Path) -> None:
    """①+③: two text rounds (one multi-segment) then the full tool chain --
    user -> assistant(tool_calls) -> real fs__read on a real file -> verbatim
    result fed back -> SECOND generation -> run.finished."""
    db_path = tmp_path / "e2e.db"
    _require_success(_upgrade_head(db_path))
    note_path = tmp_path / "note.txt"
    note_path.write_text("alpha line\nbeta line\n", encoding="utf-8")

    server = FakeOpenAIServer(
        responses=[
            _text_response("hello one"),
            _text_response("seg one ", "seg two"),
            _tool_round_response(),
            _text_response("the note is ready"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsReadAdapter(),),
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            for request_id, prompt_text in (("req_1", "first"), ("req_2", "second"), ("req_3", "read the note")):
                run = await app.service.prompt(session_id, prompt_text, request_id)
                terminal = await _await_terminal(app.service, run.run_id)
                assert terminal.status.value == "succeeded", f"{prompt_text}: {terminal.status.value}"

            # store: projection order across all three runs (the tool round
            # projects user -> tool-call assistant -> answer assistant -> idle)
            page = await app.service.messages(session_id, limit=20)
            assert [message.type for message in page.items] == [
                "user",
                "assistant",
                "idle",
                "user",
                "assistant",
                "idle",
                "user",
                "assistant",
                "assistant",
                "idle",
            ]
            assert page.has_more is False
            users = [message for message in page.items if isinstance(message, UserMessage)]
            assert [message.text for message in users] == ["first", "second", "read the note"]
            assistants = [message for message in page.items if isinstance(message, AssistantMessage)]
            idles = [message for message in page.items if isinstance(message, IdleMessage)]
            assert [message.status for message in assistants] == ["completed", "completed", "completed", "completed"]
            assert [message.outcome for message in idles] == ["succeeded", "succeeded", "succeeded"]
            assert _assistant_text(assistants[0]) == "hello one"
            # the multi-segment delta stream finalized into exactly one text part
            assert _assistant_text(assistants[1]) == "seg one seg two"
            assert [part.type for part in assistants[1].parts] == ["text"]
            # the tool-call assistant keeps its preamble text and the wire call id
            assert _assistant_text(assistants[2]) == "let me check"
            tool_parts = [part for part in assistants[2].parts if isinstance(part, AssistantToolPart)]
            assert [part.tool_call_id for part in tool_parts] == ["call_read_note"]
            # the SECOND generation (after the tool result) finalized its own answer
            assert _assistant_text(assistants[3]) == "the note is ready"

            # wire: four provider requests -- two text rounds, then the tool
            # round's two generations (tool_calls, then the result-fed follow-up)
            assert len(server.requests) == 4
            tool_request = _wire_messages(server.requests[2].body)
            assert tool_request[-1].get("role") == "user"
            assert _content_str(tool_request[-1].get("content")) == "read the note"

            follow_up = _wire_messages(server.requests[3].body)
            tool_messages = [message for message in follow_up if message.get("role") == "tool"]
            assert len(tool_messages) == 1
            assert tool_messages[0].get("tool_call_id") == "call_read_note"
            # the real fs__read result feeds back verbatim: the rendered numbered page
            expected_page = "Read file note.txt, lines 1-2\n1: alpha line\n2: beta line"
            assert _content_str(tool_messages[0].get("content")) == expected_page
            wire_calls: list[dict[str, object]] = []
            for message in follow_up:
                if message.get("role") != "assistant":
                    continue
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    wire_calls.extend(call for call in calls if isinstance(call, dict))
            assert len(wire_calls) == 1
            assert wire_calls[0].get("id") == "call_read_note"
            function = wire_calls[0].get("function")
            assert isinstance(function, dict)
            assert function.get("name") == "fs__read"
            arguments = function.get("arguments")
            parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            assert parsed_arguments == {"path": "note.txt"}
    finally:
        await server.aclose()

    # ③ counts, from the durable EventLog: three finished runs, four generated
    # steps (one per text round, two in the tool round), one tool execution
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.run.finished") == 3
    assert event_types.count("py.step.generated") == 4
    assert event_types.count("py.tool.finished") == 1


async def test_interrupted_partial_stream_next_request_starts_fresh(tmp_path: Path) -> None:
    """②+③: a mid-stream break (EOF without the SSE [DONE] terminal) lands the
    run in INTERRUPTED; a new request on the SAME session starts fresh -- its
    own user turn, a clean generation, and no fragment rejoin."""
    db_path = tmp_path / "e2e.db"
    _require_success(_upgrade_head(db_path))

    server = FakeOpenAIServer(
        responses=[
            ScriptedResponse(frames=[Frame(text_delta_frame("partial tex"))], omit_done=True),
            _text_response("fresh answer"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsReadAdapter(),),
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id

            broken = await app.service.prompt(session_id, "break mid-stream", "req_1")
            broken_terminal = await _await_terminal(app.service, broken.run_id)
            assert broken_terminal.status.value == "interrupted"

            fresh = await app.service.prompt(session_id, "start over", "req_2")
            fresh_terminal = await _await_terminal(app.service, fresh.run_id)
            assert fresh_terminal.status.value == "succeeded"

            # store: the interrupted run leaves its own assistant+idle pair with
            # interrupted markers; the fresh run completes normally
            page = await app.service.messages(session_id, limit=20)
            assert [message.type for message in page.items] == [
                "user",
                "assistant",
                "idle",
                "user",
                "assistant",
                "idle",
            ]
            interrupted_assistant, interrupted_idle = page.items[1], page.items[2]
            fresh_assistant, fresh_idle = page.items[4], page.items[5]
            assert isinstance(interrupted_assistant, AssistantMessage)
            assert interrupted_assistant.status == "interrupted"
            assert isinstance(interrupted_idle, IdleMessage)
            assert interrupted_idle.outcome == "interrupted"
            assert isinstance(fresh_assistant, AssistantMessage)
            assert fresh_assistant.status == "completed"
            assert isinstance(fresh_idle, IdleMessage)
            assert fresh_idle.outcome == "succeeded"
            # no fragment rejoin: the fresh generation finalized exactly its own
            # script, with none of the interrupted fragment prepended
            assert _assistant_text(fresh_assistant) == "fresh answer"

            # wire: the second request went out as its own user turn
            assert len(server.requests) == 2
            fresh_request = _wire_messages(server.requests[1].body)
            assert fresh_request[-1].get("role") == "user"
            assert _content_str(fresh_request[-1].get("content")) == "start over"
            # r-1 (R2-S8-closeout-review L87), the two-sided zero-rejoin proof.
            # This side: the second request's history DOES carry the interrupted
            # assistant message with its partial fragment (the ContextBuilder's
            # normal history selection); together with the fresh generation's
            # exact-own-script finalization above, zero-rejoin is proven
            # independently of which history rows were selected.
            interrupted_wire = [message for message in fresh_request if message.get("role") == "assistant"]
            assert len(interrupted_wire) == 1
            assert _content_str(interrupted_wire[0].get("content")) == "partial tex"
    finally:
        await server.aclose()

    # ③ counts: both runs reached durable terminals, but only the completed run
    # generated a step; no tool round ever ran
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.run.finished") == 2
    assert event_types.count("py.step.generated") == 1
    assert event_types.count("py.tool.finished") == 0


async def test_event_log_records_the_full_lifecycle_in_order(tmp_path: Path) -> None:
    """④: one full tool round leaves the complete durable lifecycle in the
    EventLog as an ordered subsequence, and no ephemeral delta event is
    ever persisted."""
    db_path = tmp_path / "e2e.db"
    _require_success(_upgrade_head(db_path))
    note_path = tmp_path / "note.txt"
    note_path.write_text("alpha line\nbeta line\n", encoding="utf-8")

    server = FakeOpenAIServer(
        responses=[
            _tool_round_response(),
            _text_response("all done"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsReadAdapter(),),
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            run = await app.service.prompt(session_id, "check the note", "req_1")
            assert (await _await_terminal(app.service, run.run_id)).status.value == "succeeded"
    finally:
        await server.aclose()

    # subsequence (not exact equality): the durable lifecycle order is pinned,
    # extra bookkeeping events between them are permitted
    expected_lifecycle = [
        "py.session.created",
        "py.prompt.accepted",
        "py.run.started",
        "py.step.started",
        "py.text.finalized",
        "py.step.generated",
        "py.tool.requested",
        "py.tool.started",
        "py.tool.finished",
        "py.step.started",
        "py.text.finalized",
        "py.step.generated",
        "py.run.finished",
    ]
    remaining = iter(_event_types(db_path, session_id))
    for expected in expected_lifecycle:
        assert any(observed == expected for observed in remaining), f"missing or out of order: {expected}"

    # ephemeral stream events (deltas) are never persisted
    assert not any("delta" in event_type for event_type in _event_types(db_path, session_id))
