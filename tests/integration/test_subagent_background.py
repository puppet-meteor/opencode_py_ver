"""Integration tests for the ``subagent__run`` always-background face (T-C3-005).

The upstream 2.3.2 semantics flip: the ``background`` input key is GONE
and every call returns immediately with the running output. What is pinned
here:

* the always-background full chain (§4-1): every invocation answers the
  ``backgroundResult`` Output face (``status: "running"`` + the TS
  four-line text VERBATIM), the child run is durable and in flight, the
  parent log carries exactly one ``py.subagent.backgrounded`` marker, and
  the settled child delivers through the S1 synthetic inbox seam -- the
  exact XML wrapper, the ``{source, childID, agent, state}`` metadata
  carrier, the ``kind="synthetic"`` / ``request_id=None`` admission, and
  the enqueued/delivered event pair (idle parent: queue + immediate
  promote);
* the three-state mapping (§4-2): completed with text, completed without
  text (the TS sentence), error (the step-ceiling recipe -- the only
  deterministic PY FAILED producer), and cancelled (a cooperative
  interrupt -> ``Subagent cancelled``);
* the ``session.background`` faces (§4-5): T-C3-005 deleted the foreground
  wait the endpoint used to convert, so the wire face survives as the
  structural no-op (204 no-content on idle and live arms alike, 404 for a
  missing session; the later job-system port re-gives it teeth);
* the restart faces (§4-6): a delivered marker never re-delivers after a
  reopen (the delivery also deletes the outbox row), and an in-flight
  background child folds to interrupted on restart and delivers the
  cancelled state exactly once;
* the Spec/schema pins (§4-7): NO ``background`` Input field (the flip),
  the four v2 properties, and the always-background guidance lines.

Deleted with the v1 faces (upstream 2.3.2 removed them): the foreground
zero-delivery proof (no foreground arm exists) and the block-conversion
chain (nothing blocks, so nothing converts).

Harness note: the migration/sqlite/scripted-provider/app shapes are the
``test_tool_subagent.py`` (S5) forms COPIED here, and the SDK round-trip
carrier is the ``test_sdk_client.py`` uvicorn-on-a-worker-thread form --
the copies keep this file's failure surface self-contained. The recorded
deviations (progress file): the idle delivery is queue+promote (PY has no
TS ``resume:false`` suspension face) and the final-text join is ``\\n``
(TS ``\\n\\n``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import pytest
import uvicorn
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import (
    PY_INBOX_DELIVERED,
    PY_INBOX_ENQUEUED,
    PY_RUN_CANCEL_REQUESTED,
    PY_SESSION_CREATED,
    PY_SUBAGENT_BACKGROUNDED,
)
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
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
    JsonObject,
    ToolTextContent,
    UserMessage,
)
from opencode.httpapi import create_app
from opencode.httpapi.serving import _server_config
from opencode.sdk import OpencodeApiError, OpencodeClient
from opencode.session.runner import DEFAULT_MAX_STEPS
from opencode.session.service import RunStatusValue
from opencode.tools.builtin.subagent import (
    NO_TEXT_RESPONSE,
    SUBAGENT_DESCRIPTION,
    SUBAGENT_INPUT_SCHEMA,
    SubagentRunAdapter,
)
from opencode.tools.invoker import ToolContext
from opencode.tools.policy import PermissionRule

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_SNAPSHOT_BODY = _SNAPSHOT.model_dump(mode="json")
_MODEL = ModelRef(provider="maas", model="test-model")
_TOKEN = "test-token-1234"
_TERMINAL = {"succeeded", "failed", "interrupted"}
_PROVIDE_CHUNKS = (
    TextDeltaChunk,
    ReasoningDeltaChunk,
    ToolCallStartChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    UsageChunk,
    FinishChunk,
)

#: The happy-path invocation input (schema-complete, agent "task"). T-C3-005:
#: there is no ``background`` key anymore -- every call backgrounds.
_INPUT: JsonObject = {"agent": "task", "description": "summarize the note", "prompt": "Read nothing; just answer."}


def _four_lines(child_id: str) -> str:
    """The TS ``backgroundResult`` text, reconstructed for byte comparison."""
    return "\n".join(
        (
            f"The subagent is working in the background (sessionID: {child_id}). "
            "You will receive a <subagent> message when it goes idle, and again after each later round of work.",
            "DO NOT sleep, poll, proactively check on its progress, or duplicate this subagent's work.",
            "Work on non-overlapping tasks, or briefly tell the user what you launched and end your response.",
            "To send it follow-ups or change its instructions while it runs, call subagent again with this sessionID.",
        )
    )


def _wrapped(child_id: str, state: str, description: str, text: str) -> str:
    """The TS XML wrapper, reconstructed for byte comparison."""
    return f'<subagent sessionID="{child_id}" state="{state}" description="{description}">\n{text}\n</subagent>'


# ---------------------------------------------------------------------------
# migration + sqlite probe helpers (the test_tool_subagent.py forms)
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
    """The sessions whose durable created payload names this parent."""
    rows = _fetch_all(db_path, "SELECT session_id, data FROM events WHERE type = ?", (PY_SESSION_CREATED,))
    return [session_id for session_id, data in rows if json.loads(str(data)).get("parent_id") == parent_id]


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    """Every event payload of one type in one session's log, seq order."""
    rows = _fetch_all(
        db_path, "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq", (session_id, event_type)
    )
    return [json.loads(str(row[0])) for row in rows]


def _inbox_event_data(db_path: Path, session_id: str, event_type: str, inbox_id: str) -> list[dict[str, Any]]:
    return [data for data in _event_data(db_path, session_id, event_type) if data.get("inbox_id") == inbox_id]


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


async def _await_inbox_event(
    db_path: Path, session_id: str, event_type: str, inbox_id: str, *, timeout_s: float = 15.0
) -> dict[str, Any]:
    """Poll until one event of the type lands under the inbox id; return its data."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = _inbox_event_data(db_path, session_id, event_type, inbox_id)
        if rows:
            return rows[0]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no {event_type} under {inbox_id} in session {session_id}")
        await asyncio.sleep(0.02)


def _await_inbox_event_sync(
    db_path: Path, session_id: str, event_type: str, inbox_id: str, *, timeout_s: float = 15.0
) -> dict[str, Any]:
    """The sync twin of ``_await_inbox_event`` for the wire tests."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _inbox_event_data(db_path, session_id, event_type, inbox_id)
        if rows:
            return rows[0]
        assert time.monotonic() < deadline, f"no {event_type} under {inbox_id} in session {session_id}"
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# scripted provider (the test_tool_subagent.py shape, copied; + the boom marker)
# ---------------------------------------------------------------------------


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDE_CHUNKS)


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

    def gate(self) -> _Gate:
        gate = _Gate()
        self.items.append(gate)
        return gate


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


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


# ---------------------------------------------------------------------------
# app harness (the test_tool_subagent.py form; no fs__read needed here)
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
    mode: Any = "create",
    max_steps: int = DEFAULT_MAX_STEPS,
) -> App:
    """The full composition: provider + runner + registry, the subagent tool
    registered, and a host rule allowing ``subagent__run``."""
    return App(
        db_path,
        mode=mode,
        working_dir=str(tmp_path),
        provider=provider,
        tools=(subagent,),
        settings=_settings(),
        rules=(PermissionRule(action="subagent__run", effect="allow"),),
        close_timeout_s=close_timeout_s,
        max_steps=max_steps,
    )


def _ctx(session_id: str, tmp_path: Path, *, tool_call_id: str = "call_direct_1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_direct",
        message_id="msg_direct",
        tool_call_id=tool_call_id,
        working_dir=str(tmp_path),
    )


async def _drive_background_chain(
    app: App,
    db_path: Path,
    parent_id: str,
    subagent: SubagentRunAdapter,
    tmp_path: Path,
) -> tuple[str, str, dict[str, Any]]:
    """Invoke the always-background call and settle the child; return (child_id,
    run_id, the delivered enqueued payload). The caller supplies the provider
    scripts (the child's, then the wake turn's)."""
    result = await subagent.execute(_ctx(parent_id, tmp_path), _INPUT)
    assert result.error is None
    assert isinstance(result.structured_output, dict)
    child_id = str(result.structured_output["sessionID"])
    assert result.structured_output["status"] == "running"
    assert result.structured_output["output"] == _four_lines(child_id)
    assert [content.text for content in result.content if isinstance(content, ToolTextContent)] == [
        _four_lines(child_id)
    ]
    # The child run is durable (the background work IS the durable run).
    child_run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
    await _await_runs_terminal(db_path, child_id)
    # Exactly one marker in the PARENT log, carrying the generation identity.
    markers = _event_data(db_path, parent_id, PY_SUBAGENT_BACKGROUNDED)
    assert markers == [
        {
            "child_session_id": child_id,
            "run_id": child_run_id,
            "agent": "task",
            "description": "summarize the note",
            "notification_id": f"subagent:{child_run_id}",
        }
    ]
    # The completion arrives through the S1 synthetic seam.
    enqueued = await _await_inbox_event(db_path, parent_id, PY_INBOX_ENQUEUED, f"subagent:{child_run_id}")
    await _await_inbox_event(db_path, parent_id, PY_INBOX_DELIVERED, f"subagent:{child_run_id}")
    return child_id, child_run_id, enqueued


# ---------------------------------------------------------------------------
# §4-1 the background full chain
# ---------------------------------------------------------------------------


async def test_every_call_returns_the_result_and_delivers_the_completion(tmp_path: Path) -> None:
    """§4-1 with the flag GONE: a plain invocation answers the running face,
    and the settled child delivers through the S1 synthetic seam."""
    db_path = _migrated_db(tmp_path, "bg_chain.db")
    child_script = _Script().text("p2", "all done").usage().finish()
    parent_final = _Script().text("p1", "parent woke").usage().finish()
    provider = _ScriptedProvider([[child_script], [parent_final]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        child_id, child_run_id, enqueued = await _drive_background_chain(
            app, db_path, parent.session_id, subagent, tmp_path
        )
        # The synthetic admission: kind, no request_id, idle queue delivery.
        assert enqueued["kind"] == "synthetic"
        assert enqueued["request_id"] is None
        assert enqueued["delivery"] == "queue"
        # The exact XML wrapper + the TS metadata carrier.
        assert enqueued["message"]["text"] == _wrapped(child_id, "completed", "summarize the note", "all done")
        assert enqueued["message"]["metadata"] == {
            "source": "subagent",
            "childID": child_id,
            "agent": "task",
            "state": "completed",
        }
        # The delivered event carries only the id (the payload rides enqueued).
        delivered = _inbox_event_data(db_path, parent.session_id, PY_INBOX_DELIVERED, f"subagent:{child_run_id}")
        assert delivered == [{"inbox_id": f"subagent:{child_run_id}"}]
        # The promoted parent run consumed the wake and settled.
        await _await_runs_terminal(db_path, parent.session_id)
        page = await app.service.messages(parent.session_id)
        kinds = [message.type for message in page.items]
        assert kinds == ["user", "assistant", "idle"]
        assert isinstance(page.items[0], UserMessage)
        assert page.items[0].text == _wrapped(child_id, "completed", "summarize the note", "all done")
        assert page.items[0].metadata == enqueued["message"]["metadata"]
        # No second admission: the observation pop consumed the marker.
        await asyncio.sleep(0.3)
        assert len(_inbox_event_data(db_path, parent.session_id, PY_INBOX_ENQUEUED, f"subagent:{child_run_id}")) == 1
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-2 the three-state mapping
# ---------------------------------------------------------------------------


async def test_a_no_text_child_delivers_the_ts_sentence(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "bg_no_text.db")
    reasoning_only = _Script().reasoning("r1", "thinking hard, saying nothing").usage().finish()
    provider = _ScriptedProvider([[reasoning_only], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        child_id, _, enqueued = await _drive_background_chain(app, db_path, parent.session_id, subagent, tmp_path)
        assert enqueued["message"]["text"] == _wrapped(child_id, "completed", "summarize the note", NO_TEXT_RESPONSE)
        assert enqueued["message"]["metadata"]["state"] == "completed"
    assert provider.close_count == 1


async def test_a_failed_child_delivers_the_error_state(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "bg_failed.db")
    # The only deterministic PY producer of a FAILED run: the step ceiling
    # (§7 L341). One tool-call turn inside a max_steps=1 runner -- the tool
    # itself fails on the depth guard (a child cannot spawn a child), the
    # loop iterates, and the ceiling fails the run.
    recurse_arguments = json.dumps({"agent": "task", "description": "recurse", "prompt": "nope"})
    child = (
        _Script()
        .text("c1", "calling a tool first")
        .call_start(0, tool_call_id="call_self", name="subagent__run")
        .call_args(recurse_arguments, 0)
        .call_end("call_self", "subagent__run", recurse_arguments, call_index=0)
        .usage()
        .finish()
    )
    provider = _ScriptedProvider([[child], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent, max_steps=1) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        child_id, child_run_id, enqueued = await _drive_background_chain(
            app, db_path, parent.session_id, subagent, tmp_path
        )
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("failed",)]
        # TS deliver: the error arm carries the run's error verbatim (the
        # None arm would fold into the "Subagent failed" sentence).
        status = await app.service.run_status(child_run_id)
        expected_error = status.error if status.error is not None else "Subagent failed"
        assert enqueued["message"]["metadata"]["state"] == "error"
        assert enqueued["message"]["text"] == _wrapped(child_id, "error", "summarize the note", expected_error)
    assert provider.close_count == 1


async def test_an_interrupted_child_delivers_the_cancelled_state(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "bg_cancelled.db")
    working = _Script()
    gate = working.gate()
    working.text("c2", "never reached").usage().finish()
    provider = _ScriptedProvider([[working], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        assert result.error is None
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        child_run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        await asyncio.wait_for(gate.entered.wait(), timeout=10.0)
        # The cancel face (the machinery the interrupt route rides verbatim):
        # the intent commits while the stream parks -- no concurrent writer --
        # then the gate opens, the boundary honors the intent, and cancel's
        # bounded await settles on the interrupted terminal.
        cancel_task = asyncio.create_task(app.service.cancel(child_run_id))
        deadline = asyncio.get_running_loop().time() + 10.0
        while not _fetch_all(
            db_path, "SELECT 1 FROM events WHERE session_id = ? AND type = ?", (child_id, PY_RUN_CANCEL_REQUESTED)
        ):
            assert asyncio.get_running_loop().time() < deadline, "the cancel intent never committed"
            await asyncio.sleep(0.01)
        gate.open()
        status = await asyncio.wait_for(cancel_task, timeout=10.0)
        assert status.status is RunStatusValue.INTERRUPTED
        await _await_runs_terminal(db_path, child_id)
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("interrupted",)]
        enqueued = await _await_inbox_event(db_path, parent.session_id, PY_INBOX_ENQUEUED, f"subagent:{child_run_id}")
        assert enqueued["message"]["metadata"]["state"] == "cancelled"
        # The TS cancelled three-tier, tier 1: the child's own last output
        # wins -- the released stream finalized "never reached" before the
        # boundary honored the intent (the tier-3 constant arm is pinned by
        # the restart-fold test, whose child never produced text).
        assert enqueued["message"]["text"] == _wrapped(child_id, "cancelled", "summarize the note", "never reached")
        await _await_inbox_event(db_path, parent.session_id, PY_INBOX_DELIVERED, f"subagent:{child_run_id}")
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-3 the foreground zero-delivery proof -- DELETED with the v1 face: the
# upstream 2.3.2 flip removed the foreground arm entirely, so every call
# backgrounds and the zero-delivery row has no face left to pin.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §4-4 the block conversion -- DELETED with the v1 face: nothing blocks
# anymore (the tool returns at once), so there is no foreground wait for a
# parked queue item to convert. The parked-queue survival itself is the S1
# inbox machinery, pinned in its own suite.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §4-4 the block conversion: a REAL parent run, a queue item parks behind it
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §4-5 the session.background faces (service arm)
# ---------------------------------------------------------------------------


async def test_session_background_is_the_structural_no_op_now(tmp_path: Path) -> None:
    """T-C3-005: the always-background flip deleted the foreground wait the
    endpoint used to convert -- the wire face survives (upstream 2.3.2 keeps
    the endpoint over the job system's ``backgroundAll``), but both arms
    answer ``False`` and commit nothing: idle, and a session with a LIVE
    always-background child (its call already returned; nothing waits)."""
    db_path = _migrated_db(tmp_path, "endpoint.db")
    child = _Script().text("c2", "child read done").usage().finish()
    provider = _ScriptedProvider([[child], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        idle = await app.service.create(_SNAPSHOT)
        # The idle arm: structurally nothing is registered, nothing commits.
        assert await app.service.request_session_background(idle.session_id) is False
        assert _event_data(db_path, idle.session_id, PY_SUBAGENT_BACKGROUNDED) == []
        # The live-child arm: the spawn's own marker is all there ever is --
        # the endpoint adds nothing on top of it.
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        assert result.error is None
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        assert await app.service.request_session_background(parent.session_id) is False
        markers = _event_data(db_path, parent.session_id, PY_SUBAGENT_BACKGROUNDED)
        assert len(markers) == 1
        child_run_id = str(markers[0]["run_id"])
        # The delivery chain still runs to completion, untouched by the call.
        await _await_runs_terminal(db_path, child_id)
        enqueued = await _await_inbox_event(db_path, parent.session_id, PY_INBOX_ENQUEUED, f"subagent:{child_run_id}")
        assert enqueued["message"]["text"] == _wrapped(child_id, "completed", "summarize the note", "child read done")
        await _await_inbox_event(db_path, parent.session_id, PY_INBOX_DELIVERED, f"subagent:{child_run_id}")
    assert provider.close_count == 1


# ---------------------------------------------------------------------------
# §4-6 the restart faces
# ---------------------------------------------------------------------------


async def test_a_delivered_marker_never_redelivers_after_a_reopen(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "restart_done.db")
    child_script = _Script().text("p2", "all done").usage().finish()
    provider = _ScriptedProvider([[child_script], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        child_id, child_run_id, _ = await _drive_background_chain(app, db_path, parent.session_id, subagent, tmp_path)
        await _await_runs_terminal(db_path, parent.session_id)
        notification_id = f"subagent:{child_run_id}"
    # A fresh process face over the same durable state.
    provider2 = _ScriptedProvider([[_final_script()]])
    subagent2 = SubagentRunAdapter()
    async with _subagent_app(db_path, provider2, tmp_path, subagent2, mode="readwrite") as app2:
        await app2.recovery_gate.wait()
        # The re-arm scan folds the parent log: the admission already popped
        # the observation, so nothing is outstanding and nothing re-arms.
        armed = await app2.service.recover_subagent_observations()
        assert armed == []
        await asyncio.sleep(0.5)
        observers: dict[str, Any] = app2.service._subagent_observers
        assert not observers
        assert len(_inbox_event_data(db_path, parent.session_id, PY_INBOX_ENQUEUED, notification_id)) == 1
        assert len(_inbox_event_data(db_path, parent.session_id, PY_INBOX_DELIVERED, notification_id)) == 1
        assert _child_ids(db_path, parent.session_id) == [child_id]
    assert provider2.close_count == 1


async def test_an_inflight_background_child_folds_and_delivers_cancelled_once(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path, "restart_inflight.db")
    child = _Script()
    gate = child.gate()  # never opened in this process face
    provider = _ScriptedProvider([[child], [_final_script()], [_final_script()]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await subagent.execute(_ctx(parent.session_id, tmp_path), _INPUT)
        assert result.error is None
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        child_run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        await gate.entered.wait()
    # The reopen: R4 folds the in-flight child run, the re-arm re-observes the
    # still-outstanding marker, and the observer delivers the cancelled state.
    provider2 = _ScriptedProvider([[_final_script()], [_final_script()]])
    subagent2 = SubagentRunAdapter()
    async with _subagent_app(db_path, provider2, tmp_path, subagent2, mode="readwrite") as app2:
        await app2.recovery_gate.wait()
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("interrupted",)]
        armed = await app2.service.recover_subagent_observations()
        notification_id = f"subagent:{child_run_id}"
        # The face returns the armed NOTIFICATION ids (one per outstanding
        # generation), not session ids.
        assert armed == [notification_id]
        enqueued = await _await_inbox_event(db_path, parent.session_id, PY_INBOX_ENQUEUED, notification_id)
        assert enqueued["message"]["metadata"]["state"] == "cancelled"
        assert enqueued["message"]["text"] == _wrapped(
            child_id, "cancelled", "summarize the note", "Subagent cancelled"
        )
        await _await_inbox_event(db_path, parent.session_id, PY_INBOX_DELIVERED, notification_id)
        await asyncio.sleep(0.3)
        assert len(_inbox_event_data(db_path, parent.session_id, PY_INBOX_ENQUEUED, notification_id)) == 1
        # T-C3-005: the outbox row died with its delivery (the replay arm
        # has nothing to retry either).
        assert _fetch_all(db_path, "SELECT 1 FROM background_job WHERE notification_id = ?", (notification_id,)) == []
    assert provider2.close_count == 1


# ---------------------------------------------------------------------------
# §4-7 the Spec/schema pins
# ---------------------------------------------------------------------------


def test_the_input_schema_has_no_background_field_now() -> None:
    """The T-C3-005 flip: the opt-in key is gone and its absence is strict."""
    properties = SUBAGENT_INPUT_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"agent", "description", "prompt", "sessionID"}
    assert SUBAGENT_INPUT_SCHEMA["required"] == ["agent", "description", "prompt"]
    assert SUBAGENT_INPUT_SCHEMA["additionalProperties"] is False


def test_the_spec_description_carries_the_always_background_guidance() -> None:
    assert "- Subagents always run in the background: the call returns immediately with" in SUBAGENT_DESCRIPTION
    assert "a sessionID, and you receive a `<subagent>` message each time the child" in SUBAGENT_DESCRIPTION
    schema_description = SUBAGENT_INPUT_SCHEMA["description"]
    assert isinstance(schema_description, str)
    assert "always run in the background" in schema_description
    assert "each time the child goes idle" in schema_description


# ---------------------------------------------------------------------------
# §4-5 the SDK round trip (the test_sdk_client.py uvicorn carrier form)
# ---------------------------------------------------------------------------


def _user_text(request: GenerationRequest) -> str:
    """The last user text of a generation request -- the script queue's key."""
    for message in reversed(request.messages):
        if message.role == "user":
            for part in message.content:
                if isinstance(part, LLMTextPart):
                    return part.text
    return ""


class _KeyedProvider:
    """Fake Provider for the shared server: scripts keyed by the prompt's user
    text (order-independent), gates supported, an unregistered key gets the
    default finishing script (the promoted wake runs)."""

    def __init__(self) -> None:
        self._scripts: dict[str, list[list[BaseModel | _Gate]]] = {}
        self.requests: list[GenerationRequest] = []

    def register(self, key: str, script: _Script) -> None:
        self._scripts.setdefault(key, []).append(list(script.items))

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        queue = self._scripts.get(_user_text(request))
        items = queue.pop(0) if queue else list(_final_script().items)
        for item in items:
            if _is_chunk(item):
                yield item
            elif isinstance(item, _Gate):
                item.entered.set()
                await item.event.wait()

    async def close(self) -> None:
        pass


@dataclass
class _Running:
    """One live module server: the base URL plus the composition it serves."""

    base: str
    db_path: Path
    app: App
    provider: _KeyedProvider
    server: uvicorn.Server
    thread: threading.Thread


def _boot_server(root: Path, provider: _KeyedProvider, subagent: SubagentRunAdapter) -> _Running:
    """Boot one programmatic uvicorn.Server on a worker thread (the 054/056 form)."""
    db_path = root / "app.db"
    _require_success(_upgrade_head(db_path))
    app = App(
        db_path,
        mode="create",
        working_dir=str(root),
        provider=provider,
        tools=(subagent,),
        settings=_settings(),
        rules=(PermissionRule(action="subagent__run", effect="allow"),),
    )
    subagent.bind(app.service)
    api = create_app(app, token=_TOKEN)
    config = _server_config(api, port=0)  # loopback host, ephemeral port
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="opencode-bg-test-server")
    thread.start()
    deadline = time.monotonic() + 30.0
    while not server.started:
        assert thread.is_alive(), "the server thread died during startup"
        assert time.monotonic() < deadline, "the server never started"
        time.sleep(0.05)
    sockets = server.servers[0].sockets
    assert sockets is not None
    address = sockets[0].getsockname()
    assert address[0] == "127.0.0.1"
    return _Running(
        base=f"http://127.0.0.1:{int(address[1])}",
        db_path=db_path,
        app=app,
        provider=provider,
        server=server,
        thread=thread,
    )


def _stop_server(running: _Running) -> None:
    """Stop the server thread; the ASGI lifespan closes the App on the way down."""
    running.server.should_exit = True
    running.thread.join(timeout=15)
    assert not running.thread.is_alive(), "the server did not shut down"


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Running]:
    root = tmp_path_factory.mktemp("bg_module_server")
    subagent = SubagentRunAdapter()
    handle = _boot_server(root, _KeyedProvider(), subagent)
    try:
        yield handle
    finally:
        _stop_server(handle)


@pytest.fixture(scope="module")
def client(running: _Running) -> OpencodeClient:
    return OpencodeClient(base_url=running.base, token=_TOKEN)


def test_the_sdk_background_face_covers_idle_noop_and_missing(client: OpencodeClient, running: _Running) -> None:
    session = client.create_session(_SNAPSHOT_BODY)
    # Idle: structurally nothing registered -- 204 no-content, nothing committed.
    client.background(session.session_id)
    assert _event_data(running.db_path, session.session_id, PY_SUBAGENT_BACKGROUNDED) == []
    assert _event_data(running.db_path, session.session_id, PY_INBOX_ENQUEUED) == []
    # A missing session answers the 404 face.
    with pytest.raises(OpencodeApiError) as info:
        client.background("ses_missing_over_the_wire")
    assert info.value.status_code == 404


def test_the_sdk_background_face_is_a_no_op_over_the_wire(client: OpencodeClient, running: _Running) -> None:
    """T-C3-005 over the wire: the endpoint survives as the structural
    no-op -- a delegating run whose tool call already returned (nothing
    waits anymore) answers 204 and commits nothing beyond the spawn's own
    marker, the run settles untouched, and the receipt still lands once."""
    provider = running.provider
    sub_arguments = json.dumps({"agent": "task", "description": "read the note", "prompt": "Read the note and report."})
    parent_key = "delegate the read over the wire"
    provider.register(
        parent_key,
        _Script()
        .text("p1", "delegating now")
        .call_start(0, tool_call_id="call_sub", name="subagent__run")
        .call_args(sub_arguments, 0)
        .call_end("call_sub", "subagent__run", sub_arguments, call_index=0)
        .usage()
        .finish(),
    )
    provider.register(parent_key, _Script().text("p2", "parent final").usage().finish())
    child = _Script()
    gate = child.gate()
    child.text("c2", "child read done").usage().finish()
    provider.register("Read the note and report.", child)
    session = client.create_session(_SNAPSHOT_BODY)
    run = client.prompt(session.session_id, parent_key, "req_wire_bg_1")
    # The no-op round trip: 204 no-content over the real wire, mid-flight or
    # settled -- both arms are the same nothing now.
    client.background(session.session_id)
    status = client.run_status(run.run_id)
    deadline = time.monotonic() + 10.0
    while status.status != "succeeded":
        assert time.monotonic() < deadline, f"the parent run did not settle: {status.status}"
        time.sleep(0.02)
        status = client.run_status(run.run_id)
    # The spawn's own marker is all there is -- the endpoint added nothing.
    markers = _event_data(running.db_path, session.session_id, PY_SUBAGENT_BACKGROUNDED)
    assert len(markers) == 1
    child_ids = _child_ids(running.db_path, session.session_id)
    assert len(child_ids) == 1
    child_id = child_ids[0]
    child_run_id = str(markers[0]["run_id"])
    # Release the child: the observer delivers into the (now idle) parent and
    # the promoted wake run consumes the default script.
    gate.open()
    notification_id = f"subagent:{child_run_id}"
    enqueued = _await_inbox_event_sync(running.db_path, session.session_id, PY_INBOX_ENQUEUED, notification_id)
    assert enqueued["message"]["text"] == _wrapped(child_id, "completed", "read the note", "child read done")
    assert enqueued["message"]["metadata"] == {
        "source": "subagent",
        "childID": child_id,
        "agent": "task",
        "state": "completed",
    }
    _await_inbox_event_sync(running.db_path, session.session_id, PY_INBOX_DELIVERED, notification_id)
    assert len(_inbox_event_data(running.db_path, session.session_id, PY_INBOX_ENQUEUED, notification_id)) == 1
