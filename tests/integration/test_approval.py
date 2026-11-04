"""R3-S2 integration tests: the approval protocol through the full App composition.

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos/runner
through :class:`opencode.app.App`, with the App-minted boot_id, one
ApprovalCoordinator and one PermissionPolicy injected into the ToolRegistry and
the SessionService. Contract anchors: CORE-CONTRACTS §5.2 L265-270 (the gate),
§5.1 L259 (the gate between validation and py.tool.started), §5.2 L268 (the
approval protocol + the unconnected channel), §7 L173 (first transition wins),
§7 L316 (reply_permission/revoke_permission independent of the run locks),
§7 L335 (cancel during approval), §7 L337/OP-9 (close during approval), §3.1
L140 + §3.3 L212 (closed-first pairing, same-batch reply/terminal pairs).

④ disclosures: the races run on the real asyncio scheduler (no injected
clock) -- expiry uses a short real ``approval_timeout_s=0.05`` instead of the
300 s default; the boot_id restart is simulated by a *second App* over the
same data directory after the first fully closed (same process, sequential
writers -- no real process restart); the read_only_diagnostic quarantine is
injected by a direct sqlite UPDATE (n-3: an out-of-band write, the only way
to quarantine a session whose run is parked on an approval).
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
from opencode.contracts.events import (
    PY_PERMISSION_CLOSED,
    PY_PERMISSION_REPLIED,
    PY_PERMISSION_REQUESTED,
    PY_PERMISSION_REVOKED,
    PY_PERMISSION_SAVED,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    PermissionClosedData,
    PermissionRequestedData,
    ToolFinishedData,
)
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session import RunRef, RunStatusValue, SessionService, SessionStatus
from opencode.session.context import RuntimeSettings
from opencode.session.service import ApprovalExpiredError, DecisionReceipt, RunStatus
from opencode.storage import ConflictError, NotFoundError, OpenMode, SessionReadOnlyError, StoreValidationError
from opencode.tools import DEFAULT_APPROVAL_TIMEOUT_S, ToolAdapter, ToolContext, ToolRegistry
from opencode.tools.policy import PermissionPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ToolCallStartChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    UsageChunk,
    FinishChunk,
)


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


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


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _event_rows(db_path: Path, session_id: str) -> list[tuple[str, Any]]:
    return _fetch_all(db_path, "SELECT type, data FROM events WHERE session_id = ? ORDER BY seq", (session_id,))


def _event_types(db_path: Path, session_id: str) -> list[str]:
    return [str(row[0]) for row in _event_rows(db_path, session_id)]


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [json.loads(str(row[1])) for row in _event_rows(db_path, session_id) if str(row[0]) == event_type]


def _run_status_value(db_path: Path, run_id: str) -> str | None:
    row = _fetch_scalar(db_path, "SELECT status FROM runs WHERE run_id = ?", (run_id,))
    return None if row is None else str(row[0])


async def _await_run_status(
    service: SessionService, run_id: str, wanted: frozenset[RunStatusValue], *, timeout_s: float = 5.0
) -> RunStatus:
    """Poll run_status until the run reaches one of ``wanted`` (bounded)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status in wanted:
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"run {run_id} did not reach {wanted} (stuck at {status.status}: {status.error})")
        await asyncio.sleep(0.01)


async def _await_permission_requested(
    db_path: Path, session_id: str, *, count: int = 1, timeout_s: float = 5.0
) -> list[dict[str, Any]]:
    """Poll the events table until ``count`` py.permission.requested rows exist (live sqlite read)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            rows = _fetch_all(
                db_path,
                "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
                (session_id, PY_PERMISSION_REQUESTED),
            )
        except sqlite3.OperationalError:
            rows = None  # a concurrent writer holds the lock; retry after a tick
        if rows is not None and len(rows) >= count:
            return [json.loads(str(row[0])) for row in rows[:count]]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no py.permission.requested row appeared for {session_id}")
        await asyncio.sleep(0.01)


async def _reply_registered(service: SessionService, permission_id: str, reply: str) -> DecisionReceipt:
    """Reply once the coordinator holds the permission pending.

    ④ race disclosure: the durable py.permission.requested commit (aiosqlite
    worker thread) becomes reader-visible strictly before the Invoker's
    in-process registration -- a reply in that window legitimately answers
    NotFound, so poll-retry instead of assuming registration."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        try:
            return await service.reply_permission(permission_id, reply)
        except NotFoundError:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"permission {permission_id} was never registered") from None
            await asyncio.sleep(0.01)


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending chunk indexes."""

    def __init__(self) -> None:
        self.items: list[BaseModel] = []
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            TextDeltaChunk(type="text_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta)
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


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request; every request and close recorded."""

    def __init__(self, scripts: Sequence[Sequence[BaseModel | _Script]]) -> None:
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

    async def close(self) -> None:
        self.close_count += 1


class _AskAdapter:
    """``tst.write``: a side-effecting tool whose every execution is recorded."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self._spec = _ask_spec()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        tag = str(input.get("tag", ""))
        self.starts.append(tag)
        return ToolResult(content=[ToolTextContent(type="text", text=f"wrote {tag}")])


class _BoomAdapter:
    """``tst.write`` variant whose execution always raises (effect_status unknown)."""

    def __init__(self) -> None:
        self._spec = _ask_spec()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        raise RuntimeError("boom")


def _ask_spec() -> ToolSpec:
    return ToolSpec(
        namespace="tst",
        name="write",
        version=1,
        description="side-effecting test tool subject to approval",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "tag": {"type": "string"}},
            "required": ["path", "tag"],
            "additionalProperties": False,
        },
        supported_content_types=["text"],
        side_effects=True,
        idempotency_support=True,
    )


class _RecordingSink:
    """Direct-registry test sink: records every committed draft batch."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[Any], ...]] = []

    async def __call__(self, drafts: Sequence[EventDraft[Any]]) -> None:
        self.batches.append(tuple(drafts))


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "model": _MODEL,
        "context_limit": 100_000,
        "output_reserve": 4096,
        "timeout_s": 30.0,
    }
    values.update(overrides)
    return RuntimeSettings(**values)


def _run_app(
    db_path: Path,
    provider: _ScriptedProvider,
    *,
    adapters: Sequence[ToolAdapter] = (),
    approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
    mode: OpenMode = "create",
) -> App:
    """The full R3-S2 composition: provider + tools + policy + runner wired into the service."""
    return App(
        db_path,
        mode=mode,
        working_dir="C:/work",
        provider=provider,
        tools=adapters,
        settings=_settings(),
        approval_timeout_s=approval_timeout_s,
    )


async def _create_prompted(
    service: SessionService, *, text: str = "hello", request_id: str = "req_1"
) -> tuple[str, RunRef]:
    session = await service.create(_SNAPSHOT)
    run_ref = await service.prompt(session.session_id, text, request_id)
    return session.session_id, run_ref


def _ask_script(tag: str, *, path: str = "out.txt", name: str = "tst.write", call_id: str = "call_1") -> _Script:
    # ``call_id`` must be unique across the session history (§2.1 L37), so every
    # second tool-carrying run in one session passes its own id.
    arguments = json.dumps({"path": path, "tag": tag})
    return (
        _Script()
        .text("p1", "let me write")
        .call_start(0, tool_call_id=call_id, name=name)
        .call_args(arguments, 0)
        .call_end(call_id, name, arguments, call_index=0)
        .usage()
        .finish()
    )


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


# ---------------------------------------------------------------------------
# §5.2 L268: once / reject / expiry
# ---------------------------------------------------------------------------


async def test_once_approval_pairs_reply_and_started_then_succeeds(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        permission_id = str(requested[0]["permission_id"])
        execution_id = str(requested[0]["execution_id"])

        # §7 L316: the Invoker parks without the run locks -- run_status and the
        # reply both answer while the approval is pending.
        status = await app.service.run_status(run_ref.run_id)
        assert status.status is RunStatusValue.RUNNING
        receipt = await _reply_registered(app.service, permission_id, "once")
        assert isinstance(receipt, DecisionReceipt)
        assert (receipt.permission_id, receipt.decision, receipt.execution_id) == (permission_id, "allow", execution_id)

        final = await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final.error is None
        assert write.starts == ["t1"]

        assert _event_types(db_path, session_id).count(PY_PERMISSION_REQUESTED) == 1
        # ④-2: the reply and the started confirmation share one batch; the started
        # event carries the "allow" decision that took effect.
        pair = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session_id, PY_PERMISSION_REPLIED, PY_TOOL_STARTED),
        )
        assert [row[0] for row in pair] == [PY_PERMISSION_REPLIED, PY_TOOL_STARTED]
        assert pair[0][1] == pair[1][1]
        started = _event_data(db_path, session_id, PY_TOOL_STARTED)[0]
        assert started["permission_decision"] == "allow"
        # §3.2 projection: the permission row landed with the pinned execution.
        rows = _fetch_all(
            db_path,
            "SELECT execution_id, action FROM permissions WHERE session_id = ? AND permission_id = ?",
            (session_id, permission_id),
        )
        assert len(rows) == 1
        assert (str(rows[0][0]), str(rows[0][1])) == (execution_id, "tst__write")
        # §7 L173: a second reply over the standing terminal is a conflict.
        with pytest.raises(ConflictError):
            await app.service.reply_permission(permission_id, "once")


async def test_reject_denies_the_tool_in_the_same_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        receipt = await _reply_registered(app.service, str(requested[0]["permission_id"]), "reject")
        assert receipt.decision == "deny"

        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert write.starts == []
        pair = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session_id, PY_PERMISSION_REPLIED, PY_TOOL_FINISHED),
        )
        assert [row[0] for row in pair] == [PY_PERMISSION_REPLIED, PY_TOOL_FINISHED]
        assert pair[0][1] == pair[1][1]
        finished = _event_data(db_path, session_id, PY_TOOL_FINISHED)[0]
        assert (finished["state"], finished["effect_status"]) == ("denied", "not_started")
        assert finished["error"]["code"] == CoreErrorCode.PERMISSION_DENIED.value
        assert _event_data(db_path, session_id, PY_TOOL_STARTED) == []


async def test_unconnected_approval_channel_cancels_without_silent_allow() -> None:
    """§5.2 L268: a policy without an approval channel folds the ask to
    closed(cancelled) + the cancelled/not_started terminal carrying
    ApprovalUnavailable -- never a silent allow (direct registry, no runner)."""
    sink = _RecordingSink()
    policy = PermissionPolicy(boot_id="boot_unconnected", approvals=None)
    registry = ToolRegistry(sink=sink, policy=policy)
    registry.register(_AskAdapter())
    registry.snapshot("ses_000001")
    context = ToolContext(
        session_id="ses_000001",
        run_id="run_000001",
        message_id="msg_000001",
        tool_call_id="call_1",
        working_dir="C:/work",
    )

    outcome = await registry.invoke(context, "tst__write", {"path": "out.txt", "tag": "t1"}, "exec_000001")

    assert (outcome.state, outcome.effect_status) == ("cancelled", "not_started")
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.APPROVAL_UNAVAILABLE
    assert [[draft.type for draft in batch] for batch in sink.batches] == [
        [PY_TOOL_REQUESTED],
        [PY_PERMISSION_REQUESTED],
        [PY_PERMISSION_CLOSED, PY_TOOL_FINISHED],
    ]
    requested = sink.batches[1][0].data
    assert isinstance(requested, PermissionRequestedData)
    closed = sink.batches[2][0].data
    assert isinstance(closed, PermissionClosedData)
    assert (closed.permission_id, closed.outcome, closed.reason) == (
        requested.permission_id,
        "cancelled",
        "approval channel not connected",
    )
    finished = sink.batches[2][1].data
    assert isinstance(finished, ToolFinishedData)
    assert (finished.state, finished.effect_status) == ("cancelled", "not_started")
    assert finished.error is not None and finished.error.code is CoreErrorCode.APPROVAL_UNAVAILABLE


async def test_approval_expiry_cancels_the_tool_and_late_reply_expires(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider, adapters=[write], approval_timeout_s=0.05) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        permission_id = str(requested[0]["permission_id"])

        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert write.starts == []
        pair = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session_id, PY_PERMISSION_CLOSED, PY_TOOL_FINISHED),
        )
        assert [row[0] for row in pair] == [PY_PERMISSION_CLOSED, PY_TOOL_FINISHED]
        assert pair[0][1] == pair[1][1]
        closed = _event_data(db_path, session_id, PY_PERMISSION_CLOSED)[0]
        assert closed["outcome"] == "expired"
        finished = _event_data(db_path, session_id, PY_TOOL_FINISHED)[0]
        assert (finished["state"], finished["effect_status"]) == ("cancelled", "not_started")
        assert finished["error"]["code"] == CoreErrorCode.APPROVAL_UNAVAILABLE.value
        # §7 L173/§5.2 L268: the reply that arrives after expiry gets its own code.
        with pytest.raises(ApprovalExpiredError):
            await _reply_registered(app.service, permission_id, "once")


# ---------------------------------------------------------------------------
# §7 L335/L337: cancel and close during approval
# ---------------------------------------------------------------------------


async def test_cancel_during_approval_folds_interrupted_without_started(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")]])  # no second request: the run ends interrupted

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)

        # cancel() awaits the drained run task: the whole fold can complete
        # inside the call, so the returned snapshot may already be INTERRUPTED.
        status = await app.service.cancel(run_ref.run_id)
        assert status.status in {RunStatusValue.CANCELLING, RunStatusValue.INTERRUPTED}
        final = await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.INTERRUPTED}))
        assert final.error is None
        assert write.starts == []
        pair = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session_id, PY_PERMISSION_CLOSED, PY_TOOL_FINISHED),
        )
        assert [row[0] for row in pair] == [PY_PERMISSION_CLOSED, PY_TOOL_FINISHED]
        assert pair[0][1] == pair[1][1]
        closed = _event_data(db_path, session_id, PY_PERMISSION_CLOSED)[0]
        assert closed["outcome"] == "cancelled"
        assert str(requested[0]["permission_id"]) == closed["permission_id"]
        # fold-illegal started is never committed over a cancelled approval run
        assert _event_data(db_path, session_id, PY_TOOL_STARTED) == []
        assert _event_data(db_path, session_id, PY_PERMISSION_REPLIED) == []


async def test_close_during_approval_folds_interrupted_op9(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")]])  # the run ends interrupted, no second request

    app = _run_app(db_path, provider, adapters=[write])
    session_id, run_ref = await _create_prompted(app.service)
    await _await_permission_requested(db_path, session_id)

    # OP-9 (revised ordering): close() sets the cooperative signal and commits
    # the run-cancel intent while the parked Invoker is quiet, then close_all
    # resolves the pending approval; the closed-first pair lands and the run
    # folds interrupted.
    await app.close()

    assert write.starts == []
    closed = _event_data(db_path, session_id, PY_PERMISSION_CLOSED)
    assert len(closed) == 1
    assert (closed[0]["outcome"], closed[0]["reason"]) == ("cancelled", "service close while awaiting approval")
    pair = _fetch_all(
        db_path,
        "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
        (session_id, PY_PERMISSION_CLOSED, PY_TOOL_FINISHED),
    )
    assert [row[0] for row in pair] == [PY_PERMISSION_CLOSED, PY_TOOL_FINISHED]
    assert pair[0][1] == pair[1][1]
    assert _event_data(db_path, session_id, PY_TOOL_STARTED) == []
    assert _run_status_value(db_path, run_ref.run_id) == "interrupted"
    await app.close()  # idempotent


# ---------------------------------------------------------------------------
# §5.2 L270: always / boot restart / revoke / unknown-effect retry
# ---------------------------------------------------------------------------


async def test_always_reply_saves_the_decision_and_auto_allows_later_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider(
        [[_ask_script("t1")], [_final_script()], [_ask_script("t2", call_id="call_2")], [_final_script()]]
    )

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        receipt = await _reply_registered(app.service, str(requested[0]["permission_id"]), "always")
        assert receipt.decision == "allow"
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))

        saved = _event_data(db_path, session_id, PY_PERMISSION_SAVED)
        assert len(saved) == 1
        assert saved[0]["scope"] == {"session_id": session_id, "boot_id": saved[0]["boot_id"]}
        assert saved[0]["rule"] == {"action": "tst__write", "resources": ["out.txt"]}
        decision_id = str(saved[0]["decision_id"])
        # ④-2: replied + saved + started shared one batch.
        trio = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?, ?)",
            (session_id, PY_PERMISSION_REPLIED, PY_PERMISSION_SAVED, PY_TOOL_STARTED),
        )
        assert [row[0] for row in trio] == [PY_PERMISSION_REPLIED, PY_PERMISSION_SAVED, PY_TOOL_STARTED]
        assert len({row[1] for row in trio}) == 1

        # Second run, same session + same resource binding: the saved decision
        # activates -- no new ask, the tool starts straight away.
        run_ref2 = await app.service.prompt(session_id, "again", "req_2")
        await _await_run_status(app.service, run_ref2.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert write.starts == ["t1", "t2"]
        assert _event_types(db_path, session_id).count(PY_PERMISSION_REQUESTED) == 1
        started = _event_data(db_path, session_id, PY_TOOL_STARTED)
        assert [row["permission_decision"] for row in started] == ["allow", "allow"]
        decision_rows = _fetch_all(
            db_path,
            "SELECT decision_id, scope FROM permissions WHERE session_id = ? AND decision_id = ?",
            (session_id, decision_id),
        )
        assert len(decision_rows) == 1
        assert json.loads(str(decision_rows[0][1]))["session_id"] == session_id


async def test_boot_restart_asks_again_and_never_activates_old_decisions(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider1 = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider1, adapters=[write]) as app1:
        session_id, run_ref = await _create_prompted(app1.service)
        requested = await _await_permission_requested(db_path, session_id)
        await _reply_registered(app1.service, str(requested[0]["permission_id"]), "always")
        await _await_run_status(app1.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))

    # ④ disclosure: the restart is a second App over the same data directory (new
    # boot_id), not a real process restart -- the in-memory registry is empty by
    # construction either way.
    provider2 = _ScriptedProvider([[_ask_script("t2", call_id="call_2")], [_final_script()]])
    write2 = _AskAdapter()
    async with _run_app(db_path, provider2, adapters=[write2], mode="readwrite") as app2:
        run_ref2 = await app2.service.prompt(session_id, "after restart", "req_2")
        requested2 = await _await_permission_requested(db_path, session_id, count=2)
        assert requested2[1]["permission_id"] != requested2[0]["permission_id"]
        await _reply_registered(app2.service, str(requested2[1]["permission_id"]), "once")
        final = await _await_run_status(app2.service, run_ref2.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final.error is None
        assert write2.starts == ["t2"]
        # only the first boot ever saved a decision; the audit row stays dormant
        assert _event_types(db_path, session_id).count(PY_PERMISSION_SAVED) == 1


async def test_revoke_permission_re_asks_and_gates_repeats(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider(
        [[_ask_script("t1")], [_final_script()], [_ask_script("t2", call_id="call_2")], [_final_script()]]
    )

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        await _reply_registered(app.service, str(requested[0]["permission_id"]), "always")
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        decision_id = str(_event_data(db_path, session_id, PY_PERMISSION_SAVED)[0]["decision_id"])

        await app.service.revoke_permission(decision_id)
        revoked = _event_data(db_path, session_id, PY_PERMISSION_REVOKED)
        assert len(revoked) == 1
        assert revoked[0]["decision_id"] == decision_id

        # The revoked rule stops matching: the identical next call is asked again.
        run_ref2 = await app.service.prompt(session_id, "again", "req_2")
        requested2 = await _await_permission_requested(db_path, session_id, count=2)
        await _reply_registered(app.service, str(requested2[1]["permission_id"]), "once")
        await _await_run_status(app.service, run_ref2.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert write.starts == ["t1", "t2"]
        assert _event_types(db_path, session_id).count(PY_PERMISSION_REQUESTED) == 2

        with pytest.raises(ConflictError):
            await app.service.revoke_permission(decision_id)
        with pytest.raises(NotFoundError):
            await app.service.revoke_permission("dec_missing")


async def test_unknown_effect_retry_is_re_asked_despite_saved_decision(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    boom = _BoomAdapter()
    # both ask scripts carry byte-identical arguments: the retry has the same input hash
    provider = _ScriptedProvider(
        [[_ask_script("t1")], [_final_script()], [_ask_script("t1", call_id="call_2")], [_final_script()]]
    )

    async with _run_app(db_path, provider, adapters=[boom]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        await _reply_registered(app.service, str(requested[0]["permission_id"]), "always")
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        finished = _event_data(db_path, session_id, PY_TOOL_FINISHED)[0]
        assert (finished["state"], finished["effect_status"]) == ("failed", "unknown")
        assert finished["error"] is not None

        # §5.2 L270: an identical retry after an unknown effect is re-asked even
        # though the saved always-decision still matches the resource binding.
        run_ref2 = await app.service.prompt(session_id, "retry", "req_2")
        requested2 = await _await_permission_requested(db_path, session_id, count=2)
        assert requested2[1]["permission_id"] != requested2[0]["permission_id"]
        await _reply_registered(app.service, str(requested2[1]["permission_id"]), "once")
        await _await_run_status(app.service, run_ref2.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert _event_types(db_path, session_id).count(PY_PERMISSION_REQUESTED) == 2


# ---------------------------------------------------------------------------
# §7 L316 service faces: validation, unknown, diagnostic gate
# ---------------------------------------------------------------------------


async def test_reply_permission_validation_and_missing_channel(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider, adapters=[write]) as app:
        with pytest.raises(StoreValidationError):
            await app.service.reply_permission("prm_missing", "maybe")
        with pytest.raises(NotFoundError):
            await app.service.reply_permission("prm_missing", "once")
        session_id, run_ref = await _create_prompted(app.service)
        await _await_permission_requested(db_path, session_id)
        with pytest.raises(StoreValidationError):
            await app.service.reply_permission("", "once")
        await app.service.cancel(run_ref.run_id)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.INTERRUPTED}))

    async with App(db_path, working_dir="C:/work") as plain:
        # no provider -> no approval channel: the face refuses instead of guessing
        with pytest.raises(StoreValidationError):
            await plain.service.reply_permission("prm_000001", "once")


async def test_diagnostic_session_refuses_the_reply(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])

    async with _run_app(db_path, provider, adapters=[write]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        requested = await _await_permission_requested(db_path, session_id)
        permission_id = str(requested[0]["permission_id"])

        # n-3 (④): an out-of-band quarantine -- the only way a live run's session
        # flips read_only_diagnostic mid-approval. §3.2.1 L203: the Invoker's
        # paired commit could never land on a quarantined session, so the reply
        # is refused before the CAS.
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (SessionStatus.READ_ONLY_DIAGNOSTIC.value, session_id),
            )
        with pytest.raises(SessionReadOnlyError):
            await _reply_registered(app.service, permission_id, "once")

        # restored, the same reply lands: the refusal gated the input, not the CAS.
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (SessionStatus.ACTIVE.value, session_id),
            )
        receipt = await _reply_registered(app.service, permission_id, "once")
        assert receipt.decision == "allow"
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
