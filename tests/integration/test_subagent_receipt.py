"""T-C3-005 red contract set: the always-background subagent receipt face.

Pins the banked seam contract of doc/plans/port-232-increments.md (the
upstream 2.3.2 subagent semantics diff, quantified against 2.2.2):

* §1 the always-background tool face -- spec v2 with NO ``background`` input
  key, the running-only output face, and a spawn that returns in
  milliseconds while the child is still parked (the v1 foreground block
  chain is gone);
* §2 per-round receipts -- each settled child run delivers exactly ONE
  synthetic inbox entry under ``subagent:{run_id}`` with the exact XML
  wrapper, the TS metadata carrier, and the child's final text; a follow-up
  round delivers a SECOND entry under the new run's id (never a repeat of
  the first);
* §3 outstanding keep-alive -- a running background shell row under the
  child at settle time flips the wrapper state to ``running`` (the metadata
  keeps the true state) and appends the "Background work still running:"
  tail with the shell line (whitespace-collapsed command); without
  outstanding work the state is flat;
* §4 the error terminal -- a failed child run delivers ``state="error"``
  with the child's error message as the body;
* §5 the depth guard regression row -- a session that is itself a child
  still refuses to spawn (the standing v1 face, kept green);
* §6 follow-up steering -- a follow-up call on a RUNNING child delivers
  into the current run (steer; never a SessionBusy rejection) and the
  non-child follow-up is still refused;
* §7 the restart replay construction -- a settled subagent row replays as
  the wrapped ``<subagent ...>`` report built from the row's recovery
  snapshot (upstream deliverSubagentSafe); a row whose parent session left
  no trace is cleared, not delivered.

Red on the pre-implementation tree by construction: §1's v2 pins fail on
v1 (version 1, ``background`` key present, foreground block), §2/§3/§4
fail on the absent always-background delivery, §6 fails on the busy face,
§7 fails on the kind-agnostic replay. Harness note: the scripted provider
and sqlite probes are the test_tool_subagent.py shapes COPIED here (the
S4 form); everything else is real -- a real migrated database, the real
App composition, and the real session service.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, TypeGuard

import pytest
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.errors import CoreError
from opencode.contracts.events import PY_INBOX_ENQUEUED
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
from opencode.session.runner import DEFAULT_MAX_STEPS
from opencode.tools.background import BackgroundJobRow
from opencode.tools.builtin.subagent import (
    SUBAGENT_INPUT_SCHEMA,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_VERSION,
    SubagentRunAdapter,
)
from opencode.tools.invoker import ToolContext
from opencode.tools.policy import PermissionRule

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
_TERMINAL = {"succeeded", "failed", "interrupted"}
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ReasoningDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    UsageChunk,
    FinishChunk,
    ErrorChunk,
)

#: The always-background invocation: NO ``background`` key (the T-C3-005 face).
_INPUT: dict[str, Any] = {
    "agent": "task",
    "description": "summarize the note",
    "prompt": "Read nothing; just answer.",
}

#: An always-background spawn must return well inside this bound while its
#: child is parked (the v1 foreground face blocks past it).
_RETURN_BOUND_S = 1.0
#: The hard bound on ANY adapter call in this suite: the v1 foreground face
#: parks for ``SUBAGENT_TIMEOUT_S`` (600s), so a red run must fail FAST -- a
#: call that outlives this bound is the blocking-face evidence itself.
_SPAWN_BOUND_S = 3.0
#: Durable-observation poll deadlines.
_OBSERVE_DEADLINE_S = 15.0


# ---------------------------------------------------------------------------
# migration + sqlite probe helpers (the test_tool_subagent.py forms)
# ---------------------------------------------------------------------------


def _migrated_db(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert migrated.returncode == 0, f"alembic failed\nstdout:{migrated.stdout}\nstderr:{migrated.stderr}"
    return db_path


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _inbox_entries(db_path: Path, session_id: str, inbox_id: str) -> list[dict[str, Any]]:
    rows = _fetch_all(db_path, "SELECT session_id, data FROM events WHERE type = ?", (PY_INBOX_ENQUEUED,))
    out: list[dict[str, Any]] = []
    for row_session, data in rows:
        if row_session != session_id:
            continue
        try:
            decoded = json.loads(str(data))
        except ValueError:
            continue
        if isinstance(decoded, dict) and decoded.get("inbox_id") == inbox_id:
            out.append(decoded)
    return out


async def _await(predicate: Any, *, timeout_s: float, what: str) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{what} did not arrive within {timeout_s}s")
        await asyncio.sleep(0.01)


async def _await_run_terminal(db_path: Path, session_id: str) -> None:
    def settled() -> bool:
        try:
            rows = _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (session_id,))
        except sqlite3.OperationalError:
            return False
        return bool(rows) and all(str(row[0]) in _TERMINAL for row in rows)

    await _await(settled, timeout_s=_OBSERVE_DEADLINE_S, what=f"the runs of {session_id} to settle")


async def _await_run_count(db_path: Path, session_id: str, count: int) -> list[tuple[Any, ...]]:
    def rows() -> list[tuple[Any, ...]]:
        try:
            found = _fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (session_id,))
        except sqlite3.OperationalError:
            return []
        return found if len(found) >= count else []

    return await _await(lambda: rows(), timeout_s=_OBSERVE_DEADLINE_S, what=f"{count} runs of {session_id}")


async def _await_receipt(db_path: Path, session_id: str, inbox_id: str) -> dict[str, Any]:
    entries = await _await(
        lambda: _inbox_entries(db_path, session_id, inbox_id),
        timeout_s=_OBSERVE_DEADLINE_S,
        what=f"the receipt under {inbox_id}",
    )
    return entries[0]


def _receipt_text(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    assert isinstance(message, dict)
    return str(message.get("text", ""))


def _receipt_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    message = entry.get("message")
    assert isinstance(message, dict)
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _wrapped(child_id: str, state: str, description: str, body: str) -> str:
    return f'<subagent sessionID="{child_id}" state="{state}" description="{description}">\n{body}\n</subagent>'


# ---------------------------------------------------------------------------
# scripted provider + gates (the test_tool_subagent.py shape, copied)
# ---------------------------------------------------------------------------


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.entered = asyncio.Event()

    def open(self) -> None:
        self.event.set()


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending indexes."""

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


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request; every request recorded."""

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
    max_steps: int = DEFAULT_MAX_STEPS,
) -> App:
    return App(
        db_path,
        mode="create",
        working_dir=str(tmp_path),
        provider=provider,
        tools=(subagent,),
        settings=_settings(),
        rules=(PermissionRule(action="subagent__run", effect="allow"),),
        close_timeout_s=close_timeout_s,
        max_steps=max_steps,
    )


def _ctx(session_id: str, tmp_path: Path, *, tool_call_id: str = "call_receipt_1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_receipt",
        message_id="msg_receipt",
        tool_call_id=tool_call_id,
        working_dir=str(tmp_path),
    )


def _gated_final(text: str) -> tuple[_Script, _Gate]:
    script = _Script()
    gate = script.gate()
    return script.text("p2", text).usage().finish(), gate


async def _spawn_bounded(subagent: SubagentRunAdapter, context: ToolContext, payload: dict[str, Any]) -> Any:
    """Run one adapter call under the hard bound; a block IS the red evidence.

    The v1 foreground face parks the call for ``SUBAGENT_TIMEOUT_S`` (600s);
    failing fast here keeps a red run inside seconds while the teardown's
    ``close_timeout_s`` reaps the parked child.
    """
    try:
        return await asyncio.wait_for(subagent.execute(context, payload), timeout=_SPAWN_BOUND_S)
    except TimeoutError:
        pytest.fail(
            f"the adapter call blocked past {_SPAWN_BOUND_S}s: the always-background face is unmet"
            " (the v1 foreground wait is still in place)"
        )


# ---------------------------------------------------------------------------
# §1 the always-background tool face
# ---------------------------------------------------------------------------


async def test_the_spec_is_the_v2_always_background_face() -> None:
    """v2, no ``background`` input key, the always-background description."""
    from opencode.tools.builtin import subagent as module

    raw_properties = SUBAGENT_INPUT_SCHEMA.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    assert SUBAGENT_VERSION == 2
    assert set(properties) == {"agent", "description", "prompt", "sessionID"}
    description = str(getattr(module, "SUBAGENT_DESCRIPTION", ""))
    assert "always run in the background" in description


async def test_spawn_returns_immediately_while_the_child_is_parked(tmp_path: Path) -> None:
    """No ``background`` key: the call returns in milliseconds, running face."""
    script, gate = _gated_final("all done")
    provider = _ScriptedProvider([[script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_immediate.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        started = time.perf_counter()
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        elapsed = time.perf_counter() - started
        assert elapsed < _RETURN_BOUND_S, f"the spawn blocked {elapsed:.2f}s (the foreground face is gone)"
        assert result.error is None
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        assert result.structured_output["status"] == "running"
        output = str(result.structured_output["output"])
        assert child_id in output
        assert "working in the background" in output
        # The guidance names the per-round receipt contract.
        assert "when it goes idle" in output
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        gate.open()
        await _await_run_terminal(db_path, child_id)


# ---------------------------------------------------------------------------
# §2 per-round receipts
# ---------------------------------------------------------------------------


async def test_the_first_round_delivers_the_wrapped_receipt(tmp_path: Path) -> None:
    script, gate = _gated_final("all done")
    provider = _ScriptedProvider([[script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_first.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        gate.open()
        await _await_run_terminal(db_path, child_id)
        runs = _fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))
        run_id = str(runs[0][0])
        entry = await _await_receipt(db_path, parent.session_id, f"subagent:{run_id}")
        assert entry.get("kind") == "synthetic"
        assert entry.get("request_id") is None
        assert _receipt_text(entry) == _wrapped(child_id, "completed", "summarize the note", "all done")
        assert _receipt_metadata(entry) == {
            "source": "subagent",
            "childID": child_id,
            "agent": "task",
            "state": "completed",
        }
        # Exactly one admission for this generation, never a repeat.
        await asyncio.sleep(0.3)
        assert len(_inbox_entries(db_path, parent.session_id, f"subagent:{run_id}")) == 1


async def test_a_followup_round_delivers_a_second_receipt(tmp_path: Path) -> None:
    """A follow-up starts a second run; its receipt rides the NEW run id."""
    script, gate = _gated_final("first round")
    second = _Script().text("p2", "second round").usage().finish()
    provider = _ScriptedProvider([[script], [second]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_second.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        gate.open()
        await _await_run_terminal(db_path, child_id)
        first_run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        await _await_receipt(db_path, parent.session_id, f"subagent:{first_run_id}")
        # The follow-up (an IDLE child here) starts round two.
        followup = await _spawn_bounded(
            subagent,
            _ctx(parent.session_id, tmp_path, tool_call_id="call_receipt_followup"),
            {**_INPUT, "sessionID": child_id, "prompt": "Now also cover the appendix."},
        )
        assert followup.error is None
        assert isinstance(followup.structured_output, dict)
        assert followup.structured_output["status"] == "running"
        await _await_run_count(db_path, child_id, 2)
        await _await_run_terminal(db_path, child_id)
        second_run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[1][0])
        entry = await _await_receipt(db_path, parent.session_id, f"subagent:{second_run_id}")
        assert _receipt_text(entry) == _wrapped(child_id, "completed", "summarize the note", "second round")
        # Both receipts are single deliveries under their own ids.
        assert len(_inbox_entries(db_path, parent.session_id, f"subagent:{first_run_id}")) == 1
        assert len(_inbox_entries(db_path, parent.session_id, f"subagent:{second_run_id}")) == 1


# ---------------------------------------------------------------------------
# §3 outstanding keep-alive
# ---------------------------------------------------------------------------


async def test_an_outstanding_shell_row_keeps_the_receipt_alive(tmp_path: Path) -> None:
    """A running shell row under the child: wrapper state=running + the tail."""
    from opencode.tools.background import BackgroundJobRow, upsert_row

    script, gate = _gated_final("all done")
    provider = _ScriptedProvider([[script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_tail.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        # The outstanding row must exist BEFORE the child settles. Written
        # through the APP'S database handle -- the writer lock is single
        # owner, a second open_database inside the app context is
        # WriterLockHeldError by design.
        await upsert_row(
            app.database,
            BackgroundJobRow(
                notification_id="ntf_tail_shell",
                session_id=child_id,
                tool_call_id="call_tail_shell",
                kind="shell",
                status="running",
                title="long shell",
                payload={"command": "ping   -t localhost"},
            ),
        )
        gate.open()
        await _await_run_terminal(db_path, child_id)
        run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        entry = await _await_receipt(db_path, parent.session_id, f"subagent:{run_id}")
        text = _receipt_text(entry)
        # The wrapper keeps the "still working" state; the metadata keeps the truth.
        assert text.startswith(f'<subagent sessionID="{child_id}" state="running"')
        assert _receipt_metadata(entry)["state"] == "completed"
        assert "Background work still running:" in text
        assert "- shell (" in text
        assert "ping -t localhost" in text  # whitespace-collapsed command


async def test_without_outstanding_work_the_state_is_flat_completed(tmp_path: Path) -> None:
    script, gate = _gated_final("all done")
    provider = _ScriptedProvider([[script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_flat.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        gate.open()
        await _await_run_terminal(db_path, child_id)
        run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        entry = await _await_receipt(db_path, parent.session_id, f"subagent:{run_id}")
        text = _receipt_text(entry)
        assert text == _wrapped(child_id, "completed", "summarize the note", "all done")
        assert "Background work still running:" not in text


# ---------------------------------------------------------------------------
# §4 the error terminal
# ---------------------------------------------------------------------------


async def test_a_failed_child_delivers_the_error_state(tmp_path: Path) -> None:
    """A failed child run delivers ``state="error"`` with the run's error.

    The only deterministic PY producer of a FAILED run is the step ceiling
    (the v1 recipe): one tool-call turn inside a max_steps=1 runner -- the
    call itself fails on the depth guard (a child cannot spawn a child),
    the loop iterates, the ceiling fails the run. (A provider ErrorChunk
    lands as "interrupted" -- PY stream-interrupt semantics -- never
    "failed"; recorded.)
    """
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
    provider = _ScriptedProvider([[child]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_error.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent, max_steps=1) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await_run_terminal(db_path, child_id)
        assert _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,)) == [("failed",)]
        run_id = str(_fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))[0][0])
        status = await app.service.run_status(run_id)
        expected_error = status.error if status.error is not None else "Subagent failed"
        entry = await _await_receipt(db_path, parent.session_id, f"subagent:{run_id}")
        assert _receipt_text(entry) == _wrapped(child_id, "error", "summarize the note", expected_error)
        assert _receipt_metadata(entry)["state"] == "error"


# ---------------------------------------------------------------------------
# §5 the depth guard regression row
# ---------------------------------------------------------------------------


async def test_a_child_session_still_refuses_to_spawn(tmp_path: Path) -> None:
    """The standing v1 depth face stays green under the new semantics."""
    provider = _ScriptedProvider([[]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_depth.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        root = await app.service.create(_SNAPSHOT)
        mid = await app.service.create(_SNAPSHOT, parent_id=root.session_id)
        result = await _spawn_bounded(subagent, _ctx(mid.session_id, tmp_path), dict(_INPUT))
        assert result.error is not None
        assert "depth" in str(result.error.message).lower()
        assert SUBAGENT_MAX_DEPTH == 1


# ---------------------------------------------------------------------------
# §6 follow-up steering
# ---------------------------------------------------------------------------


async def test_a_followup_on_a_running_child_steers_not_busy(tmp_path: Path) -> None:
    """Mid-run follow-up joins the current run; the busy face is gone."""
    script, gate = _gated_final("first round")
    resumed = _Script().text("p2", " joined answer").usage().finish()
    provider = _ScriptedProvider([[script], [resumed]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_steer.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(subagent, _ctx(parent.session_id, tmp_path), dict(_INPUT))
        assert isinstance(result.structured_output, dict)
        child_id = str(result.structured_output["sessionID"])
        await _await(lambda: gate.entered.is_set(), timeout_s=_OBSERVE_DEADLINE_S, what="the gated child to park")
        followup = await _spawn_bounded(
            subagent,
            _ctx(parent.session_id, tmp_path, tool_call_id="call_receipt_steer"),
            {**_INPUT, "sessionID": child_id, "prompt": "Also check the appendix."},
        )
        assert followup.error is None, f"the running-child follow-up must steer, got {followup.error}"
        assert isinstance(followup.structured_output, dict)
        assert followup.structured_output["status"] == "running"
        gate.open()
        await _await_run_terminal(db_path, child_id)


async def test_a_followup_still_rejects_a_non_child(tmp_path: Path) -> None:
    """The parentID ownership check stays (the standing v1 face)."""
    provider = _ScriptedProvider([[]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "receipt_nonchild.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        root = await app.service.create(_SNAPSHOT)
        stranger = await app.service.create(_SNAPSHOT)
        result = await _spawn_bounded(
            subagent, _ctx(root.session_id, tmp_path), {**_INPUT, "sessionID": stranger.session_id}
        )
        assert result.error is not None
        assert "not a child" in str(result.error.message)


# ---------------------------------------------------------------------------
# §7 the restart replay construction
# ---------------------------------------------------------------------------


def _replay_row(notification_id: str, session_id: str, child_id: str, tool_call_id: str) -> BackgroundJobRow:
    return BackgroundJobRow(
        notification_id=notification_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        kind="subagent",
        status="succeeded",
        title="summarize the note",
        payload={
            "kind": "subagent",
            "parentSessionID": session_id,
            "childSessionID": child_id,
            "agent": "task",
            "description": "summarize the note",
            "output": "all done",
        },
    )


async def test_replay_skips_a_deleted_parent(tmp_path: Path) -> None:
    """A row whose parent session left no trace is cleared, not delivered."""
    from opencode.storage import open_database
    from opencode.tools.background import replay_pending, upsert_row

    db_path = _migrated_db(tmp_path, "receipt_replay_gone.db")
    async with open_database(db_path) as database:
        await upsert_row(database, _replay_row("subagent:run_gone", "ses_gone_parent", "ses_gone_child", "call_gone"))
        replayed = await replay_pending(database)
    assert [item for item in replayed if item.kind == "subagent"] == []
    rows = _fetch_all(db_path, "SELECT COUNT(*) FROM background_job WHERE notification_id = ?", ("subagent:run_gone",))
    assert int(rows[0][0]) == 0


async def test_replay_delivers_for_a_live_parent(tmp_path: Path) -> None:
    """A live parent receives the wrapped replay, exactly once, keyed by id.

    (The wrapped-construction pins ride here: a fake parent with no sessions
    row would take the deleted-parent branch, so the row-shape assertions
    belong on the live arm.)
    """
    from opencode.storage import open_database
    from opencode.tools.background import replay_pending, upsert_row

    db_path = _migrated_db(tmp_path, "receipt_replay_live.db")
    provider = _ScriptedProvider([[]])
    subagent = SubagentRunAdapter()
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        parent = await app.service.create(_SNAPSHOT)
    async with open_database(db_path) as database:
        await upsert_row(database, _replay_row("subagent:run_live", parent.session_id, "ses_live_child", "call_live"))
        replayed = await replay_pending(database)
    matches = [item for item in replayed if item.kind == "subagent"]
    assert len(matches) == 1
    assert matches[0].notification_id == "subagent:run_live"
    assert matches[0].session_id == parent.session_id
    assert matches[0].text == _wrapped("ses_live_child", "completed", "summarize the note", "all done")
    # The replay is destructive: a second pass replays nothing.
    async with open_database(db_path) as database:
        assert [item for item in await replay_pending(database) if item.kind == "subagent"] == []
