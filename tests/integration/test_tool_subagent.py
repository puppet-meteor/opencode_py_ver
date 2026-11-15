"""Integration tests for the ``subagent__run`` builtin (P6-S5; T-C1-098 §4).

What is pinned here, per the ticket's §4 list:

* the guard matrix (§4-1): an unbound adapter is a wiring defect
  (ToolInvokeError), a missing parent session answers ValidationError, an
  unknown agent is rejected, the depth guard refuses a session that is
  already a child (``SUBAGENT_MAX_DEPTH == 1``), and the resume face
  refuses a non-child session id (``not a child``) and a missing one.
  The pre-S2 ``background: true`` rejection row is GONE (the face is
  T-C1-101's import -- the background acceptance chain lives in
  ``test_subagent_background.py``);
* the resume-reuse face (§4-2): a second invocation with ``sessionID``
  reuses the same child session -- a second run, message growth, no
  new session;
* the foreground full chain (§4-3): a direct adapter invocation over a
  real App (provider + runner + registry) returns the exact Output
  face ``{sessionID, status: "completed", output}`` as
  structured_output/metadata/content, with the durable evidence probed
  in sqlite (the ``py.session.created`` payload carrying ``parent_id``,
  the ``py.prompt.accepted`` chain event, the succeeded runs row) and
  the parent link read back through the service face;
* the registry-driven chain (the deadlock pin): a REAL parent run whose
  scripted stream emits a ``subagent__run`` tool call, whose child run
  itself calls ``fs__read`` -- two sessions inside one registry. This
  pins the T-C1-098 mechanism correction (per-session serial locks in
  ``ToolRegistry.invoke``): under the pre-correction registry-wide lock
  this shape deadlocked deterministically, and a regression goes red
  BOUNDED (the adapter's ``SUBAGENT_TIMEOUT_S`` foreground-wait face
  fails the tool; ``close_timeout_s`` bounds the teardown) -- never an
  unbounded hang;
* the child-failure terminal (§4-4): a scripted provider error makes
  the child run fail and the tool answers the fail-closed TOOL_FAILED
  face (status + ids, never a silent ``completed``);
* the no-text terminal (§4-5): a reasoning-only child run settles
  successfully and the output is the TS sentence verbatim
  (``Subagent completed without a text response.``);
* the bounded wait faces (§3-4 ⑦, the recorded seat ruling): a gated
  child run surfaces the ToolTimeout face (retryable, the child ids as
  the resume hint, the run keeps going), and a cooperative cancel
  surfaces the TOOL_FAILED face after a bounded best-effort
  ``service.cancel``;
* the policy/Spec pins (§4-6): the ``ask`` default row
  (``PermissionPolicy.evaluate``), the eight Spec catalog fields, and
  the wire-name registration on a real App registry.

Harness note (the S4 form): the scripted provider, script builder, and
sqlite probes are the ``test_httpapi_server.py`` shapes COPIED here
(the cross-file import of test helpers is legal but the copy keeps this
file's failure surface self-contained); everything else is real -- a
real migrated database, the real App composition (provider, runner,
registry, policy), and the real session service.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, TypeGuard

import pytest
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.errors import CoreError
from opencode.contracts.events import PY_PROMPT_ACCEPTED, PY_SESSION_CREATED
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantTextPart,
    AssistantToolPart,
    JsonObject,
    ToolTextContent,
)
from opencode.session.service import RunStatus, RunStatusValue, SessionService
from opencode.tools.builtin import subagent as subagent_module
from opencode.tools.builtin.read import FsReadAdapter
from opencode.tools.builtin.subagent import (
    NO_TEXT_RESPONSE,
    SUBAGENT_AGENTS,
    SUBAGENT_DESCRIPTION,
    SUBAGENT_INPUT_SCHEMA,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_SPEC,
    SubagentRunAdapter,
)
from opencode.tools.invoker import ToolContext, ToolInvokeError
from opencode.tools.policy import PermissionPolicy, PermissionRule

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
_TERMINAL = {"succeeded", "failed", "interrupted"}
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ReasoningDeltaChunk,
    ToolCallStartChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    UsageChunk,
    FinishChunk,
    ErrorChunk,
)

#: The happy-path invocation input (schema-complete, agent "task").
_INPUT: JsonObject = {"agent": "task", "description": "summarize the note", "prompt": "Read nothing; just answer."}


# ---------------------------------------------------------------------------
# migration + sqlite probe helpers (the test_httpapi_server.py forms)
# ---------------------------------------------------------------------------


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
    assert result.returncode == 0, f"alembic failed\nstdout:{result.stdout}\nstderr:{result.stderr}"


def _migrated_db(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    _require_success(_upgrade_head(db_path))
    return db_path


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _child_ids(db_path: Path, parent_id: str) -> list[str]:
    """The sessions whose durable created payload names this parent (mechanism ①)."""
    rows = _fetch_all(db_path, "SELECT session_id, data FROM events WHERE type = ?", (PY_SESSION_CREATED,))
    return [session_id for session_id, data in rows if json.loads(str(data)).get("parent_id") == parent_id]


async def _await_child(db_path: Path, parent_id: str, *, timeout_s: float = 5.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        ids = _child_ids(db_path, parent_id)
        if ids:
            return ids[0]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no child session appeared for parent {parent_id}")
        await asyncio.sleep(0.01)


async def _await_runs_terminal(db_path: Path, session_id: str, *, timeout_s: float = 10.0) -> None:
    """Poll the runs read model until every run of the session is terminal."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            rows = _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (session_id,))
        except sqlite3.OperationalError:
            rows = None  # a concurrent writer holds the lock; retry after a tick
        if rows is not None and rows and all(str(row[0]) in _TERMINAL for row in rows):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"the runs of session {session_id} did not settle")
        await asyncio.sleep(0.01)


async def _await_terminal(service: SessionService, run_id: str, *, timeout_s: float = 15.0) -> RunStatus:
    """Poll the durable run_status face until a terminal status (the e2e_r4 form)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status.is_terminal:
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"run {run_id} did not settle: {status.status}")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# scripted provider (the test_httpapi_server.py shape, copied)
# ---------------------------------------------------------------------------


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.entered = asyncio.Event()

    def open(self) -> None:
        self.event.set()


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending chunk indexes."""

    def __init__(self) -> None:
        self.items: list[BaseModel | _Gate] = []
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            TextDeltaChunk(type="text_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta)
        )
        return self

    def reasoning(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            ReasoningDeltaChunk(
                type="reasoning_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta
            )
        )
        return self

    def call_start(self, call_index: int = 0, *, tool_call_id: str | None = None, name: str | None = None) -> _Script:
        self.items.append(
            ToolCallStartChunk(
                type="tool_call_start",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                tool_call_id=tool_call_id,
                name=name,
            )
        )
        return self

    def call_args(self, delta: str, call_index: int = 0) -> _Script:
        self.items.append(
            ToolCallArgsDeltaChunk(
                type="tool_call_args_delta",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                delta=delta,
            )
        )
        return self

    def call_end(self, tool_call_id: str, name: str, raw_arguments: str, *, call_index: int = 0) -> _Script:
        self.items.append(
            ToolCallEndChunk(
                type="tool_call_end",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                tool_call_id=tool_call_id,
                name=name,
                raw_arguments=raw_arguments,
            )
        )
        return self

    def usage(self, input_tokens: int = 5, output_tokens: int = 7) -> _Script:
        self.items.append(
            UsageChunk(
                type="usage",
                request_id="req_script",
                index=self._next(),
                usage=Usage(input=input_tokens, output=output_tokens),
                cost_usd="0.0012",
            )
        )
        return self

    def finish(self, reason: FinishReason = FinishReason.STOP) -> _Script:
        self.items.append(FinishChunk(type="finish", request_id="req_script", index=self._next(), reason=reason))
        return self

    def error(self, error: CoreError) -> _Script:
        self.items.append(ErrorChunk(type="error", request_id="req_script", index=self._next(), error=error))
        return self

    def gate(self) -> _Gate:
        gate = _Gate()
        self.items.append(gate)
        return gate


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request; every request and close recorded."""

    def __init__(self, scripts: Sequence[Sequence[BaseModel | _Gate | _Script]]) -> None:
        self.scripts = [
            [item for entry in script for item in (entry.items if isinstance(entry, _Script) else (entry,))]
            for script in scripts
        ]
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if _is_chunk(item):
                yield item
            elif isinstance(item, _Gate):
                item.entered.set()
                await item.event.wait()

    async def close(self) -> None:
        self.close_count += 1


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


# ---------------------------------------------------------------------------
# app harness
# ---------------------------------------------------------------------------


def _settings() -> Any:
    from opencode.session.context import RuntimeSettings

    return RuntimeSettings(
        model=_MODEL,
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


def _subagent_app(
    db_path: Path,
    provider: _ScriptedProvider,
    tmp_path: Path,
    subagent: SubagentRunAdapter,
    *,
    close_timeout_s: float = 5.0,
) -> App:
    """The full composition: provider + runner + registry, the subagent tool and
    ``fs__read`` registered, and a host rule allowing ``subagent__run`` (the
    production hosts carry the ask default; the rule keeps the approval
    channel out of these runs' way)."""
    return App(
        db_path,
        mode="create",
        working_dir=str(tmp_path),
        provider=provider,
        tools=(FsReadAdapter(roots=(str(tmp_path),)), subagent),
        settings=_settings(),
        rules=(PermissionRule(action="subagent__run", effect="allow"),),
        close_timeout_s=close_timeout_s,
    )


def _ctx(session_id: str, tmp_path: Path, *, tool_call_id: str = "call_direct_1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_direct",
        message_id="msg_direct",
        tool_call_id=tool_call_id,
        working_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# §4-1 the guard matrix
# ---------------------------------------------------------------------------


async def test_an_unbound_adapter_is_a_wiring_defect(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "unbound.db")
    async with App(db_path, working_dir=str(tmp_path)) as app:
        await app.recovery_gate.wait()
        parent = await app.service.create(_SNAPSHOT)
        with pytest.raises(ToolInvokeError):
            await SubagentRunAdapter().execute(_ctx(parent.session_id, tmp_path), _INPUT)


async def test_the_guard_matrix_rejects_every_fail_closed_face(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "guards.db")
    async with App(db_path, working_dir=str(tmp_path)) as app:
        await app.recovery_gate.wait()
        service = app.service
        adapter = SubagentRunAdapter()
        adapter.bind(service)
        root = await service.create(_SNAPSHOT)
        # A session that is itself a child: the depth-1 chain root -> mid.
        mid = await service.create(_SNAPSHOT, parent_id=root.session_id)
        # An unrelated root session (a valid session, not a child of root).
        stranger = await service.create(_SNAPSHOT)
        cases: list[tuple[ToolContext, JsonObject, CoreErrorCode, str]] = [
            # ① a parent session that does not exist on the resource face.
            (
                _ctx("ses_missing_parent", tmp_path),
                _INPUT,
                CoreErrorCode.VALIDATION_ERROR,
                "Parent session ses_missing_parent does not exist",
            ),
            # ② the depth guard: mid is already a subagent; the limit value rides the message.
            (
                _ctx(mid.session_id, tmp_path),
                _INPUT,
                CoreErrorCode.VALIDATION_ERROR,
                "maximum depth of 1",
            ),
            # ③ an unknown agent (the TS unknown-agent rejection).
            (
                _ctx(root.session_id, tmp_path),
                {**_INPUT, "agent": "explore"},
                CoreErrorCode.VALIDATION_ERROR,
                "Unknown agent 'explore'",
            ),
            # ④ the resume face: an existing session that is not a child of this one.
            (
                _ctx(root.session_id, tmp_path),
                {**_INPUT, "sessionID": stranger.session_id},
                CoreErrorCode.VALIDATION_ERROR,
                "is not a child of the current session",
            ),
            # ④ the resume face: a child session id that does not exist.
            (
                _ctx(root.session_id, tmp_path),
                {**_INPUT, "sessionID": "ses_gone"},
                CoreErrorCode.VALIDATION_ERROR,
                "Session ses_gone does not exist",
            ),
            # ④ the resume face: a malformed session id.
            (
                _ctx(root.session_id, tmp_path),
                {**_INPUT, "sessionID": "not-a-session"},
                CoreErrorCode.VALIDATION_ERROR,
                "^ses_",
            ),
        ]
        for context, payload, code, fragment in cases:
            result = await adapter.execute(context, payload)
            assert result.error is not None, fragment
            assert result.error.code is code, (fragment, result.error.code)
            assert fragment in result.error.message
            assert result.structured_output is None, fragment
            # fail-closed: no guard face ever answers a completed payload
            assert result.structured_output != {"status": "completed"}, fragment


# ---------------------------------------------------------------------------
# §4-3 the foreground full chain (direct invocation) + durable evidence
# ---------------------------------------------------------------------------


async def test_the_foreground_chain_returns_the_output_face_with_durable_evidence(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "full_chain.db")
    provider = _ScriptedProvider([[_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        # The Output face, exact shape (§3-4 ⑨).
        assert result.error is None
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        assert result.structured_output == {"sessionID": child_id, "status": "completed", "output": "all done"}
        assert result.metadata == result.structured_output
        assert len(result.content) == 1
        assert isinstance(result.content[0], ToolTextContent)
        assert result.content[0].text == "all done"
        # The child is a real child of THIS parent (mechanism ①, durable payload).
        assert child_id != parent.session_id
        assert _child_ids(db_path, parent.session_id) == [child_id]
        created = json.loads(
            str(_fetch_all(db_path, "SELECT data FROM events WHERE session_id = ?", (child_id,))[0][0])
        )
        assert created["parent_id"] == parent.session_id
        # The service face reads the parent link back (the fork-precedent read).
        assert (await app.service.session_created_data(child_id)).parent_id == parent.session_id
        # The prompt chain event is durable and the run settled succeeded.
        assert _fetch_all(
            db_path, "SELECT 1 FROM events WHERE session_id = ? AND type = ?", (child_id, PY_PROMPT_ACCEPTED)
        )
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("succeeded",)]
        # The snapshot is inherited verbatim from the parent (the fork precedent).
        assert (await app.service.session_created_data(child_id)).snapshot_hash == (
            await app.service.session_created_data(parent.session_id)
        ).snapshot_hash
        # The registration census: the wire name resolves on the real registry.
        registry = app.registry
        assert registry is not None
        assert registry.snapshot(parent.session_id).resolve("subagent__run") is not None
        # Exactly one child provider request, driven by the adapter's prompt.
        assert len(provider.requests) == 1
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-2 the resume face: reuse, not a new child
# ---------------------------------------------------------------------------


async def test_a_resume_with_session_id_reuses_the_same_child_session(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "resume.db")
    first_answer = _Script().text("p1", "first answer").usage().finish()
    second_answer = _Script().text("p1", "second answer").usage().finish()
    provider = _ScriptedProvider([[first_answer], [second_answer]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        first = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        assert first.error is None
        assert isinstance(first.structured_output, dict)
        child_id = str(first.structured_output["sessionID"])
        assert first.structured_output["output"] == "first answer"
        # The resume: the same child id in, the same child id out.
        second = await subagent.execute(
            _ctx(parent.session_id, tmp_path, tool_call_id="call_resume_1"),
            {**_INPUT, "sessionID": child_id, "prompt": "Go deeper."},
        )
        assert second.error is None
        assert second.structured_output == {"sessionID": child_id, "status": "completed", "output": "second answer"}
        # Reuse, not a new session: still exactly one child, now with two runs.
        assert _child_ids(db_path, parent.session_id) == [child_id]
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [
            ("succeeded",),
            ("succeeded",),
        ]
        # The child's history grew: two settled runs, each with its idle row.
        page = await app.service.messages(child_id)
        kinds = [message.type for message in page.items]
        assert kinds == ["user", "assistant", "idle", "user", "assistant", "idle"]
        assert len(provider.requests) == 2
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# the deadlock pin: a real parent run, a tool-using child, one registry
# ---------------------------------------------------------------------------


async def test_a_parent_run_drives_a_tool_using_child_without_deadlock(tmp_path: Path) -> None:
    """The registry-driven chain (the T-C1-098 mechanism-correction pin).

    The parent's scripted stream emits a ``subagent__run`` tool call; the
    child's own run calls ``fs__read`` through the SAME registry while the
    parent's invoke is still on the stack. Under the pre-correction
    registry-wide serial lock this deadlocked deterministically (the child's
    invoke could never take the lock the parent's adapter still held); with
    the per-session locks both runs settle. A regression of the lock shape
    turns this red BOUNDED -- the adapter's foreground-wait deadline answers
    the parent's tool call with the ToolTimeout face and ``close_timeout_s``
    bounds the teardown -- never an unbounded hang.
    """
    db_path = _migrated_db(tmp_path, "registry_chain.db")
    note = tmp_path / "note.txt"
    note.write_text("note body", encoding="utf-8")
    sub_arguments = json.dumps({"agent": "task", "description": "read the note", "prompt": "Read note.txt and report."})
    read_arguments = json.dumps({"path": str(note)})
    parent_step_1 = (
        _Script()
        .text("p1", "delegating now")
        .call_start(0, tool_call_id="call_sub", name="subagent__run")
        .call_args(sub_arguments, 0)
        .call_end("call_sub", "subagent__run", sub_arguments, call_index=0)
        .usage()
        .finish()
    )
    child_step_1 = (
        _Script()
        .text("c1", "reading the file")
        .call_start(0, tool_call_id="call_read", name="fs__read")
        .call_args(read_arguments, 0)
        .call_end("call_read", "fs__read", read_arguments, call_index=0)
        .usage()
        .finish()
    )
    child_step_2 = _Script().text("c2", "child read done").usage().finish()
    parent_step_2 = _Script().text("p2", "parent final").usage().finish()
    provider = _ScriptedProvider([[parent_step_1], [child_step_1], [child_step_2], [parent_step_2]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent, close_timeout_s=5.0) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        run = await app.service.prompt(parent.session_id, "delegate the read", "req_parent_chain")
        status = await _await_terminal(app.service, run.run_id)
        assert status.status is RunStatusValue.SUCCEEDED
        # The child exists, is linked, and settled with its tool call done.
        child_ids = _child_ids(db_path, parent.session_id)
        assert len(child_ids) == 1
        child_id = child_ids[0]
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("succeeded",)]
        # The parent's durable tool part carries the child's final text and
        # the structured Output face (the model-visible delivery).
        page = await app.service.messages(parent.session_id)
        tool_parts = [
            part
            for message in page.items
            if isinstance(message, AssistantMessage)
            for part in message.parts
            if isinstance(part, AssistantToolPart) and part.name == "subagent__run"
        ]
        assert len(tool_parts) == 1
        part = tool_parts[0]
        assert part.state == "succeeded"
        assert part.structured_output == {"sessionID": child_id, "status": "completed", "output": "child read done"}
        # The parent run continued past the tool and produced its final text.
        assistants = [message for message in page.items if isinstance(message, AssistantMessage)]
        final_texts = [
            p.text
            for message in assistants
            for p in message.parts
            if isinstance(p, AssistantTextPart) and message is assistants[-1]
        ]
        assert final_texts == ["parent final"]
        # The strictly-sequential FIFO: four provider requests in run order.
        assert len(provider.requests) == 4
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-4 / §4-5 the fail-closed terminals
# ---------------------------------------------------------------------------


async def test_a_failed_child_run_answers_the_tool_failed_face(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "child_failed.db")
    failing = (
        _Script()
        .text("p1", "partial work")
        .error(
            CoreError(
                code=CoreErrorCode.PROVIDER_UNAVAILABLE,
                message="provider exploded",
                retryable=False,
            )
        )
    )
    provider = _ScriptedProvider([[failing]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        # Fail-closed: the child's terminal status rides the face, never a
        # silent completed (ticket §8). A provider stream error settles the
        # run as ``interrupted`` (the PROVIDER_STREAM_INTERRUPTED semantics)
        # -- the fail-closed arm is every non-succeeded terminal.
        assert result.error is not None
        assert result.error.code is CoreErrorCode.TOOL_FAILED
        child_ids = _child_ids(db_path, parent.session_id)
        assert len(child_ids) == 1
        assert child_ids[0] in result.error.message
        assert "ended in status interrupted" in result.error.message
        assert "provider exploded" in result.error.message
        assert result.structured_output is None
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_ids[0],)) == [
            ("interrupted",)
        ]
    assert provider.close_count == 1


async def test_a_child_without_assistant_text_answers_the_ts_sentence(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "no_text.db")
    reasoning_only = _Script().reasoning("r1", "thinking hard, saying nothing").usage().finish()
    provider = _ScriptedProvider([[reasoning_only]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        assert result.error is None
        assert result.structured_output == {
            "sessionID": _child_ids(db_path, parent.session_id)[0],
            "status": "completed",
            "output": NO_TEXT_RESPONSE,
        }
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §3-4 ⑦ the bounded wait faces (the recorded seat ruling)
# ---------------------------------------------------------------------------


async def test_an_unsettling_child_surfaces_the_bounded_timeout_face(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subagent_module, "SUBAGENT_TIMEOUT_S", 0.05)
    db_path = _migrated_db(tmp_path, "timeout.db")
    slow = _Script().text("p1", "slow")
    gate = slow.gate()
    slow.usage().finish()
    provider = _ScriptedProvider([[slow]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        # The bounded face: retryable (the run keeps going), with the child
        # session id as the resume hint.
        assert result.error is not None
        assert result.error.code is CoreErrorCode.TOOL_TIMEOUT
        assert result.error.retryable is True
        child_id = await _await_child(db_path, parent.session_id)
        assert child_id in result.error.message
        assert result.structured_output is None
        # The orphan is not cancelled: it keeps running (resume semantics).
        statuses = _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,))
        assert all(str(row[0]) not in _TERMINAL for row in statuses)
        # Let the parked stream finish so the teardown settles honestly.
        gate.open()
        await _await_runs_terminal(db_path, child_id)
    assert provider.close_count == 1


async def test_a_cooperative_cancel_answers_tool_failed_after_a_bounded_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subagent_module, "SUBAGENT_CANCEL_WAIT_S", 0.1)
    db_path = _migrated_db(tmp_path, "cancel.db")
    parked = _Script().text("p1", "working")
    gate = parked.gate()
    parked.usage().finish()
    provider = _ScriptedProvider([[parked]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        cancel = asyncio.Event()
        context = ToolContext(
            session_id=parent.session_id,
            run_id="run_cancel",
            message_id="msg_cancel",
            tool_call_id="call_cancel",
            working_dir=str(tmp_path),
            cancel=cancel,
        )
        task = asyncio.create_task(subagent.execute(context, _INPUT))
        child_id = await _await_child(db_path, parent.session_id)
        # The child run must be live on the loop before the signal fires.
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            statuses = _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,))
            if statuses and str(statuses[0][0]) in {"accepted", "running"}:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"the child run never went live: {statuses}")
            await asyncio.sleep(0.01)
        cancel.set()
        result = await task
        assert result.error is not None
        assert result.error.code is CoreErrorCode.TOOL_FAILED
        assert "cancelled before it settled" in result.error.message
        assert result.structured_output is None
        # The bounded best-effort cleanup left the child recoverable: let the
        # parked stream finish so the teardown settles honestly.
        gate.open()
        await _await_runs_terminal(db_path, child_id)
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-6 the policy + Spec pins
# ---------------------------------------------------------------------------


def test_the_policy_default_table_asks_for_subagent_runs(tmp_path: Path) -> None:
    policy = PermissionPolicy(boot_id="boot_subagent")
    decision = policy.evaluate(
        session_id="ses_000001",
        action="subagent__run",
        resources=("*",),
        input_hash="a" * 64,
        working_dir=str(tmp_path),
        side_effects=True,
    )
    assert (decision.effect, decision.source) == ("ask", "default")


def test_the_subagent_spec_pins_the_eight_catalog_fields() -> None:
    assert (SUBAGENT_SPEC.namespace, SUBAGENT_SPEC.name, SUBAGENT_SPEC.version) == ("subagent", "run", 1)
    assert SUBAGENT_SPEC.description == SUBAGENT_DESCRIPTION
    assert SUBAGENT_SPEC.input_schema == SUBAGENT_INPUT_SCHEMA
    assert SUBAGENT_SPEC.supported_content_types == ["text"]
    assert (SUBAGENT_SPEC.side_effects, SUBAGENT_SPEC.idempotency_support) == (True, False)
    assert SUBAGENT_AGENTS == frozenset({"task"})
    assert SUBAGENT_MAX_DEPTH == 1
