"""R2-S6 integration tests: the runner generation loop on a real store.

Fake scripted providers drive the real ``Runner`` through the real
``SessionStore``/projector/``EventLog`` stack (``tmp_path`` redirected by
``tests/integration/conftest.py``). Contract anchors: plan §2 R2-S6 (r2-slices
L124-134), CORE-CONTRACTS §2.3 L88-102 (L100 aggregate->plan->replay +
persist-before-execute), §3.1 L124-144 (L142 cancels keep received text),
§5.1 L253-257 (catalog snapshot, ToolContext, invoke order), §5.3 L276
(generation caps), §7 L339-341 (budget, max_steps), §8 L350 (no concatenation,
no resend), §10 A-006/A-007 (replay consistency, ToolContext identity).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard

import pytest
from pydantic import BaseModel

from opencode.bus.bus import Event, VolatileBus
from opencode.bus.eventlog import EventCursor, EventLog
from opencode.contracts import (
    ConfigSnapshot,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    PY_REASONING_DELTA,
    PY_TEXT_DELTA,
    PY_TOOL_INPUT_DELTA,
    DurableEvent,
    EventDraft,
    RunCancelRequestedData,
)
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
    LLMToolCallPart,
    LLMToolResultPart,
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
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    JsonObject,
    ToolTextContent,
)
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session import SessionProjector
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.runner import (
    DEFAULT_MAX_STEPS,
    EventCommitter,
    Runner,
    RunResult,
)
from opencode.session.state import EffectStatus, RunStatus, SessionState, StepStatus, ToolState, apply
from opencode.storage import Database, EventBatch, SessionStore, open_database
from opencode.storage.repos import ReadRepositories
from opencode.storage.txn import ConflictError
from opencode.tools import ToolContext, ToolLimits, ToolRegistry
from opencode.tools.registry import ToolAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]

_SESSION_ID = "ses_r2s6"
_RUN_ID = "run_r2s6"
_REQUEST_ID = "req_r2s6"
_USER_MESSAGE_ID = "msg_r2s6_user"
_CREATED_AT = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
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


def _clock() -> datetime:
    return _CREATED_AT


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


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


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


# ---------------------------------------------------------------------------
# scripted providers and adapters
# ---------------------------------------------------------------------------


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def wait(self) -> None:
        await self.event.wait()


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending chunk indexes."""

    def __init__(self) -> None:
        self.items: list[BaseModel | BaseException | _Gate] = []
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
                type="reasoning_delta",
                request_id="req_script",
                index=self._next(),
                part_id=part_id,
                delta=delta,
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
    """Fake Provider: one scripted stream per request; every request is recorded.

    A stream that runs out of script (or a script that simply ends) produces
    the EOF-without-terminal-chunk face; the recorded ``requests`` make the
    §8 L350 "no resend" assertion a length check.
    """

    def __init__(self, scripts: Sequence[Sequence[BaseModel | BaseException | _Gate]]) -> None:
        self.scripts = [list(script) for script in scripts]
        self.requests: list[GenerationRequest] = []

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if _is_chunk(item):
                yield item
            elif isinstance(item, _Gate):
                await item.wait()
            elif isinstance(item, BaseException):
                raise item


class _EchoAdapter:
    """``app/echo``: echoes the input text; captures every ToolContext (③-4)."""

    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []
        self._spec = ToolSpec(
            namespace="app",
            name="echo",
            version=1,
            description="echo the text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            supported_content_types=["text"],
            side_effects=False,
            idempotency_support=True,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        self.contexts.append(context)
        text = str(input.get("text", ""))
        return ToolResult(content=[ToolTextContent(type="text", text=f"echo: {text}")])


class _GatedAdapter:
    """``app/slow``: blocks on one gate, recording execution starts."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.executions: list[str] = []
        self._spec = ToolSpec(
            namespace="app",
            name="slow",
            version=1,
            description="gated test tool",
            input_schema={"type": "object", "properties": {"tag": {"type": "string"}}},
            supported_content_types=["text"],
            side_effects=False,
            idempotency_support=True,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        tag = str(input.get("tag", ""))
        self.executions.append(tag)
        await self.gate.wait()
        return ToolResult(content=[ToolTextContent(type="text", text=f"done {tag}")])


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    """The full R2 stack wired onto one engine (bootstrap not included)."""

    store: SessionStore
    repos: ReadRepositories
    bus: VolatileBus
    eventlog: EventLog
    committer: EventCommitter
    registry: ToolRegistry
    provider: _ScriptedProvider
    runner: Runner


def _settings(**overrides: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "model": _MODEL,
        "context_limit": 100_000,
        "output_reserve": 4096,
        "timeout_s": 30.0,
    }
    values.update(overrides)
    return RuntimeSettings(**values)


def _wire(
    database: Database,
    provider: _ScriptedProvider,
    adapters: Sequence[ToolAdapter] = (),
    *,
    settings: RuntimeSettings | None = None,
    limits: ToolLimits | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> _Harness:
    """Wire the runner stack: committer -> registry(sink) -> runner (③-24 true sink)."""
    engine = database.engine
    store = SessionStore(engine, projector=SessionProjector())
    repos = ReadRepositories(engine)
    bus = VolatileBus()
    eventlog = EventLog(engine, repos=repos, bus=bus)
    # One shared deterministic id source: every component mints unique ids
    # from the same counter (production AscendingIds are unique per call).
    ids = _FixedIds()
    committer = EventCommitter(repos, store, bus, ids=ids, clock=_clock)
    registry = ToolRegistry(sink=committer.sink, clock=_clock, ids=ids)
    for adapter in adapters:
        registry.register(adapter)
    runner = Runner(
        provider=provider,
        context_builder=ContextBuilder(settings if settings is not None else _settings()),
        registry=registry,
        committer=committer,
        log=eventlog,
        bus=bus,
        ids=ids,
        clock=_clock,
        limits=limits,
        max_steps=max_steps,
    )
    return _Harness(
        store=store,
        repos=repos,
        bus=bus,
        eventlog=eventlog,
        committer=committer,
        registry=registry,
        provider=provider,
        runner=runner,
    )


def _created_draft() -> EventDraft[SessionCreatedData]:
    return EventDraft[SessionCreatedData](
        id="evt_boot1",
        type="py.session.created",
        version=1,
        session_id=_SESSION_ID,
        created_at=_CREATED_AT,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=_SNAPSHOT,
            snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
        ),
    )


def _prompt_draft(text: str = "hello") -> EventDraft[PromptAcceptedData]:
    return EventDraft[PromptAcceptedData](
        id="evt_boot2",
        type="py.prompt.accepted",
        version=1,
        session_id=_SESSION_ID,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id=_REQUEST_ID,
            run_id=_RUN_ID,
            message=UserMessage(id=_USER_MESSAGE_ID, type="user", created_at=_CREATED_AT, metadata={}, text=text),
        ),
        metadata={"run_id": _RUN_ID},
    )


async def _bootstrap(harness: _Harness, *, text: str = "hello") -> None:
    """session.created + prompt.accepted (the accepted run the runner drives)."""
    await harness.store.commit(
        _SESSION_ID,
        EventBatch("batch_boot", (_created_draft(), _prompt_draft(text))),
        0,
    )


async def _commit_cancel(
    harness: _Harness, *, reason: Literal["user", "shutdown"] = "user", event_id: str = "evt_cancel_1"
) -> None:
    """Commit py.run.cancel_requested directly (the S7 face: durable truth first)."""
    high = await harness.repos.log_high(_SESSION_ID)
    draft = EventDraft[RunCancelRequestedData](
        id=event_id,
        type="py.run.cancel_requested",
        version=1,
        session_id=_SESSION_ID,
        created_at=_CREATED_AT,
        data=RunCancelRequestedData(run_id=_RUN_ID, reason=reason),
        metadata={"run_id": _RUN_ID},
    )
    await harness.store.commit(_SESSION_ID, EventBatch(f"batch_cancel_{event_id}", (draft,)), high)


async def _fold(harness: _Harness) -> SessionState:
    """Fold the session's whole durable log (test-side mirror of _load_state)."""
    state = SessionState()
    after: EventCursor | None = None
    while True:
        page = await harness.eventlog.read(_SESSION_ID, after=after)
        for event in page.items:
            state = apply(state, event)
        if not page.has_more:
            return state
        after = page.next_cursor


def _assistant(state: SessionState, message_id: str) -> Any:
    return next(message for message in state.messages if message.id == message_id)


def _tool(state: SessionState, execution_id: str) -> Any:
    return state.tools[execution_id]


async def _wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    """Poll an awaitable-free predicate until true (bounded; fails the test on timeout)."""
    for _ in range(int(timeout_s / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached within timeout")


# ---------------------------------------------------------------------------
# acceptance A: the normal loop
# ---------------------------------------------------------------------------


async def test_single_step_succeeds(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").usage().finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=1)
        assert len(harness.provider.requests) == 1
        state = await _fold(harness)
        assert state.active_run_id is None
        run = state.runs[_RUN_ID]
        assert run.status is RunStatus.SUCCEEDED
        assert run.error is None
        assert list(state.steps) == ["step_r2s6_0001"]
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.COMPLETED
        assert step.generated
        message = _assistant(state, step.message_id)
        assert message.status == "completed"
        assert message.model == _MODEL
        assert message.finish is FinishReason.STOP
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"
        assert message.parts == [AssistantTextPart(type="text", part_id="p1", text="hello")]
        assert state.finalized[(step.message_id, "p1")].text == "hello"
        assert not state.tools
        assert [item.id for item in state.messages] == [_USER_MESSAGE_ID, step.message_id, f"msg_idle_{_RUN_ID}"]
    assert _fetch_all(
        db_path, "SELECT status FROM runs WHERE session_id = ? AND run_id = ?", (_SESSION_ID, _RUN_ID)
    ) == [("succeeded",)]
    assert _fetch_all(db_path, "SELECT active_run_id FROM sessions WHERE session_id = ?", (_SESSION_ID,)) == [(None,)]
    assert _fetch_all(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = 'py.tool.requested'", (_SESSION_ID,)
    ) == [(0,)]


async def test_reasoning_and_text_finalize_in_ordinal_order(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().reasoning("r1", "think ").text("p1", "a").text("p1", "b").reasoning("r1", "!").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=1)
        state = await _fold(harness)
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.parts == [
            AssistantReasoningPart(type="reasoning", part_id="r1", text="think !"),
            AssistantTextPart(type="text", part_id="p1", text="ab"),
        ]
        assert state.finalized[(message.id, "r1")].ordinal == 0
        assert state.finalized[(message.id, "p1")].ordinal == 1
        assert message.usage is None
        assert message.cost_usd is None


async def test_tool_round_drives_second_request(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script1 = (
        _Script()
        .text("p1", "let me echo")
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "hi"}', 0)
        .call_end("call_1", "app.echo", '{"text": "hi"}', call_index=0)
        .usage(input_tokens=11, output_tokens=13)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = _Script().text("p2", "done").finish()
    provider = _ScriptedProvider([script1.items, script2.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=2)
        assert len(provider.requests) == 2
        first, second = provider.requests
        assert first.request_id == "req_r2s6_0001"
        assert second.request_id == "req_r2s6_0002"
        assert first.model == _MODEL
        assert first.max_output_tokens == 4096
        assert first.tools == [echo.spec]
        assert second.tools == [echo.spec]
        assert [item.role for item in first.messages] == ["user"]
        assert first.messages[0].content == [LLMTextPart(type="text", text="hello")]
        # A-006 replay consistency: the folded plan rides verbatim -- the
        # dotted name, the arguments, and the paired tool result.
        assert [item.role for item in second.messages] == ["user", "assistant", "tool"]
        call_part = next(part for part in second.messages[1].content if isinstance(part, LLMToolCallPart))
        assert call_part.tool_call_id == "call_1"
        assert call_part.name == "app.echo"
        assert call_part.input == {"text": "hi"}
        result_part = next(part for part in second.messages[2].content if isinstance(part, LLMToolResultPart))
        assert result_part.tool_call_id == "call_1"
        assert result_part.name == "app.echo"
        assert result_part.content == [ToolTextContent(type="text", text="echo: hi")]

        state = await _fold(harness)
        tool = _tool(state, "exec_r2s6_0001_0001")
        assert tool.state is ToolState.SUCCEEDED
        assert tool.effect_status is EffectStatus.COMPLETED
        assert tool.tool_call_id == "call_1"
        assert tool.content == (ToolTextContent(type="text", text="echo: hi"),)
        step1 = state.steps["step_r2s6_0001"]
        message1 = _assistant(state, step1.message_id)
        assert message1.status == "completed"
        assert message1.finish is FinishReason.TOOL_CALLS
        assert message1.usage == Usage(input=11, output=13)
        tool_part = next(part for part in message1.parts if isinstance(part, AssistantToolPart))
        assert tool_part.execution_id == "exec_r2s6_0001_0001"
        assert tool_part.state == "succeeded"
        assert tool_part.content == [ToolTextContent(type="text", text="echo: hi")]
        message2 = _assistant(state, state.steps["step_r2s6_0002"].message_id)
        assert message2.parts == [AssistantTextPart(type="text", part_id="p2", text="done")]
        assert message2.usage is None
        assert message2.finish is FinishReason.STOP


async def test_unknown_tool_fails_not_started_and_still_pairs(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script1 = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.missing")
        .call_end("call_1", "app.missing", "{}", call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = _Script().text("p2", "recovered").finish()
    provider = _ScriptedProvider([script1.items, script2.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)  # empty catalog: everything is unknown
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=2)
        state = await _fold(harness)
        tool = _tool(state, "exec_r2s6_0001_0001")
        assert tool.state is ToolState.FAILED
        assert tool.effect_status is EffectStatus.NOT_STARTED
        assert tool.error is not None
        assert tool.error.code is CoreErrorCode.NOT_FOUND
        # §3.3 L212: the failed call keeps its tool_result pairing in replay.
        result_part = next(
            part for part in provider.requests[1].messages[2].content if isinstance(part, LLMToolResultPart)
        )
        assert result_part.tool_call_id == "call_1"
        assert result_part.name == "app.missing"


async def test_tool_context_carries_run_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script1 = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "hi"}', 0)
        .call_end("call_1", "app.echo", '{"text": "hi"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = _Script().text("p2", "done").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script1.items, script2.items]), [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=2)
        state = await _fold(harness)
        assert len(echo.contexts) == 1
        context = echo.contexts[0]
        assert context.session_id == _SESSION_ID
        assert context.run_id == _RUN_ID
        assert context.message_id == state.steps["step_r2s6_0001"].message_id
        assert context.tool_call_id == "call_1"
        assert context.working_dir == "C:/work"
        assert context.limits == ToolLimits()
        assert context.cancel is harness.runner.cancel_event(_RUN_ID)
        assert context.progress is not None
    # A-007 envelope: the durable tool events carry the run id in metadata.
    rows = _fetch_all(
        db_path, "SELECT metadata FROM events WHERE session_id = ? AND type = 'py.tool.requested'", (_SESSION_ID,)
    )
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == {"run_id": _RUN_ID}


async def test_committed_events_reach_true_sink_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script1 = (
        _Script()
        .text("p1", "let me echo")
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "hi"}', 0)
        .call_end("call_1", "app.echo", '{"text": "hi"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = _Script().text("p2", "done").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script1.items, script2.items]), [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="succeeded", steps=2)
        state = await _fold(harness)
        message_id = state.steps["step_r2s6_0001"].message_id
    assert _fetch_all(
        db_path,
        "SELECT run_id, session_id, request_id, status, error FROM runs WHERE session_id = ?",
        (_SESSION_ID,),
    ) == [(_RUN_ID, _SESSION_ID, _REQUEST_ID, "succeeded", None)]
    assert _fetch_all(
        db_path,
        "SELECT execution_id, run_id, step_id, message_id, tool_call_id, state FROM tool_executions"
        " WHERE session_id = ?",
        (_SESSION_ID,),
    ) == [("exec_r2s6_0001_0001", _RUN_ID, "step_r2s6_0001", message_id, "call_1", "succeeded")]
    assert _fetch_all(db_path, "SELECT COUNT(*) FROM messages WHERE session_id = ?", (_SESSION_ID,)) == [(4,)]
    assert _fetch_all(db_path, "SELECT status, active_run_id FROM sessions WHERE session_id = ?", (_SESSION_ID,)) == [
        ("active", None)
    ]
    # ③-24: every runner/invoker commit landed through the one true sink.
    rows = dict(
        _fetch_all(db_path, "SELECT type, COUNT(*) FROM events WHERE session_id = ? GROUP BY type", (_SESSION_ID,))
    )
    assert rows == {
        "py.session.created": 1,
        "py.prompt.accepted": 1,
        "py.run.started": 1,
        "py.step.started": 2,
        "py.text.finalized": 2,
        "py.step.generated": 2,
        "py.tool.requested": 1,
        "py.tool.started": 1,
        "py.tool.finished": 1,
        "py.step.finished": 2,
        "py.run.finished": 1,
    }


async def test_generation_deltas_publish_live_and_never_persist(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .reasoning("r1", "think")
        .text("p1", "a")
        .text("p1", "b")
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"te', 0)
        .call_args('xt": "hi"}', 0)
        .call_end("call_1", "app.echo", '{"text": "hi"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = _Script().text("p2", "done").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items, script2.items]), [echo])
        await _bootstrap(harness)
        collected: list[Event] = []
        async with harness.bus.subscribe(
            _SESSION_ID, {PY_TEXT_DELTA, PY_REASONING_DELTA, PY_TOOL_INPUT_DELTA}
        ) as stream:

            async def collect() -> None:
                async for event in stream:
                    collected.append(event)
                    if len(collected) >= 5:
                        return

            collector = asyncio.create_task(collect())
            result = await harness.runner.run(_SESSION_ID, _RUN_ID)
            await _wait_until(lambda: len(collected) >= 5)
            await collector

        assert result == RunResult(outcome="succeeded", steps=2)
        assert [event.type for event in collected] == [
            PY_REASONING_DELTA,
            PY_TEXT_DELTA,
            PY_TEXT_DELTA,
            PY_TOOL_INPUT_DELTA,
            PY_TOOL_INPUT_DELTA,
        ]
        assert collected[0].data.delta == "think"
        assert collected[1].data.delta == "a"
        assert collected[2].data.delta == "b"
        assert collected[3].data.delta == '{"te'
        assert collected[4].data.delta == 'xt": "hi"}'
        assert all(event.metadata.get("run_id") == _RUN_ID for event in collected)
    # §4 L226: live-only events never touch the durable log.
    assert _fetch_all(
        db_path,
        "SELECT COUNT(*) FROM events WHERE type IN ('py.text.delta', 'py.reasoning.delta', 'py.tool.input.delta')",
        (),
    ) == [(0,)]


# ---------------------------------------------------------------------------
# acceptance C: interrupt and violation faces
# ---------------------------------------------------------------------------


async def test_eof_without_finish_ends_interrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "partial")  # no finish/error chunk: EOF face
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "interrupted"
        assert result.steps == 1
        assert result.error is not None
        assert result.error.code is CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
        assert result.error.retryable is True
        assert len(provider.requests) == 1  # §8 L350: no resend
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.INTERRUPTED
        message = _assistant(state, step.message_id)
        assert message.status == "interrupted"
        assert message.parts == [AssistantTextPart(type="text", part_id="p1", text="partial")]
        assert message.error is not None
        assert message.error.code is CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
        assert state.finalized[(step.message_id, "p1")].text == "partial"


async def test_error_chunk_rides_verbatim(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    boom = CoreError(code=CoreErrorCode.PROVIDER_UNAVAILABLE, message="maas is down", retryable=False)
    script = _Script().text("p1", "so far").error(boom)
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "interrupted"
        assert result.error == boom  # verbatim, never synthesized
        assert len(provider.requests) == 1
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.runs[_RUN_ID].error == boom
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "interrupted"
        assert message.error == boom
        # §3.1 L142: the received text is committed once, never resent.
        assert message.parts == [AssistantTextPart(type="text", part_id="p1", text="so far")]
        assert state.finalized[(message.id, "p1")].text == "so far"


async def test_truncated_arguments_fail_generation_without_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .text("p1", "will fail")
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "tr', 0)
        .call_end("call_1", "app.echo", '{"text": "tr', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.steps == 1
        assert result.error is not None
        assert result.error.code is CoreErrorCode.VALIDATION_ERROR
        assert result.error.retryable is False
        assert result.error.details == {"violation": "tool_arguments_not_json"}
        assert len(provider.requests) == 1
        assert echo.contexts == []  # §2.3 L100: no tool of the plan executes
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.FAILED
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.FAILED
        message = _assistant(state, step.message_id)
        assert message.status == "failed"
        assert message.parts == []
        assert message.error is not None
        assert message.error.details == {"violation": "tool_arguments_not_json"}
        assert not state.tools
        assert not state.finalized  # violations are not cancels: nothing is kept
    assert _fetch_all(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = 'py.tool.requested'", (_SESSION_ID,)
    ) == [(0,)]


async def test_non_object_arguments_fail_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_end("call_1", "app.echo", '["a"]', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "tool_arguments_not_object"}
        assert echo.contexts == []
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.FAILED
        assert not state.tools


async def test_duplicate_tool_call_id_within_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_end("call_1", "app.echo", "{}", call_index=0)
        .call_start(1, tool_call_id="call_1", name="app.echo")
        .call_end("call_1", "app.echo", "{}", call_index=1)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "duplicate_tool_call_id"}
        assert echo.contexts == []
        assert not (await _fold(harness)).tools


async def test_duplicate_tool_call_id_across_steps(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script1 = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "first"}', 0)
        .call_end("call_1", "app.echo", '{"text": "first"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    script2 = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")  # §2.1 L37: ids never recycle
        .call_end("call_1", "app.echo", "{}", call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script1.items, script2.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.steps == 2
        assert result.error is not None
        assert result.error.details == {"violation": "duplicate_tool_call_id"}
        assert len(provider.requests) == 2
        state = await _fold(harness)
        assert state.steps["step_r2s6_0001"].status is StepStatus.COMPLETED
        assert state.steps["step_r2s6_0002"].status is StepStatus.FAILED
        # only the first step's call ever executed
        assert list(state.tools) == ["exec_r2s6_0001_0001"]
        assert state.tools["exec_r2s6_0001_0001"].state is ToolState.SUCCEEDED


async def test_tool_call_without_end_fails_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args("{}", 0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "tool_call_without_end"}
        assert echo.contexts == []
        assert not (await _fold(harness)).tools


async def test_part_id_reused_across_kinds_fails_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "a").reasoning("p1", "b").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "part_id_reused"}
        state = await _fold(harness)
        assert not state.finalized
        assert not state.tools


# ---------------------------------------------------------------------------
# §5.3 L276 generation caps, §7 budget/ceiling
# ---------------------------------------------------------------------------


async def test_text_stream_cap_counts_reasoning_too(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().reasoning("r1", "12345").text("p1", "123456").finish()  # 11 bytes > 10
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, limits=ToolLimits(max_text_stream_bytes=10))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.steps == 1
        assert result.error is not None
        assert result.error.details == {"violation": "max_text_stream_bytes"}
        state = await _fold(harness)
        assert not state.finalized
        assert not state.tools


async def test_argument_cap_fails_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_args('{"text": "way too long"}', 0)
        .call_end("call_1", "app.echo", '{"text": "way too long"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo], limits=ToolLimits(max_argument_bytes=4))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "max_argument_bytes"}
        assert echo.contexts == []
        assert not (await _fold(harness)).tools


async def test_tool_call_count_cap_fails_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_end("call_1", "app.echo", "{}", call_index=0)
        .call_start(1, tool_call_id="call_2", name="app.echo")
        .call_end("call_2", "app.echo", "{}", call_index=1)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo], limits=ToolLimits(max_tool_calls_per_generation=1))
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.error is not None
        assert result.error.details == {"violation": "max_tool_calls_per_generation"}
        assert echo.contexts == []
        assert not (await _fold(harness)).tools


async def test_step_ceiling_fails_the_run(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_1", name="app.echo")
        .call_end("call_1", "app.echo", "{}", call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo], max_steps=1)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.steps == 1
        assert result.error is not None
        assert result.error.code is CoreErrorCode.VALIDATION_ERROR
        assert result.error.details == {"violation": "max_steps", "max_steps": 1}
        assert len(provider.requests) == 1  # the ceiling ends the run, not the model
        state = await _fold(harness)
        assert state.steps["step_r2s6_0001"].status is StepStatus.COMPLETED
        assert state.runs[_RUN_ID].status is RunStatus.FAILED
        run_error = state.runs[_RUN_ID].error
        assert run_error is not None
        assert run_error.details == {"violation": "max_steps", "max_steps": 1}


async def test_context_limit_fails_run_without_step_footprint(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").finish()
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, settings=_settings(context_limit=60, output_reserve=16))
        await _bootstrap(harness, text="x" * 400)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "failed"
        assert result.steps == 0
        assert result.error is not None
        assert result.error.code is CoreErrorCode.CONTEXT_LIMIT_EXCEEDED
        assert provider.requests == []  # the builder rejected before the provider saw it
        state = await _fold(harness)
        assert not state.steps
        assert state.runs[_RUN_ID].status is RunStatus.FAILED
        run_error = state.runs[_RUN_ID].error
        assert run_error is not None
        assert run_error.code is CoreErrorCode.CONTEXT_LIMIT_EXCEEDED
        # §7 L339: history is preserved for a new session, not trimmed here.
        assert len(state.messages) == 2
        assert state.messages[0].id == _USER_MESSAGE_ID
        assert state.messages[1].type == "idle"
    assert _fetch_all(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type LIKE 'py.step.%'", (_SESSION_ID,)
    ) == [(0,)]


# ---------------------------------------------------------------------------
# §7 L335 cooperative cancellation
# ---------------------------------------------------------------------------


async def test_cancel_before_run_started_finalizes_interrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)
        await _commit_cancel(harness)  # the S7 face: durable truth first

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="interrupted", steps=0)
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.active_run_id is None
        assert not state.steps
    assert _fetch_all(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = 'py.run.started'", (_SESSION_ID,)
    ) == [(0,)]
    assert _fetch_all(
        db_path, "SELECT status FROM runs WHERE session_id = ? AND run_id = ?", (_SESSION_ID, _RUN_ID)
    ) == [("interrupted",)]


async def test_cancel_between_tools_keeps_started_and_synthesizes_pending(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    slow = _GatedAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_A", name="app.slow")
        .call_args('{"tag": "A"}', 0)
        .call_end("call_A", "app.slow", '{"tag": "A"}', call_index=0)
        .call_start(1, tool_call_id="call_B", name="app.slow")
        .call_args('{"tag": "B"}', 1)
        .call_end("call_B", "app.slow", '{"tag": "B"}', call_index=1)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [slow])
        await _bootstrap(harness)
        task = asyncio.create_task(harness.runner.run(_SESSION_ID, _RUN_ID))
        await _wait_until(lambda: slow.executions == ["A"])
        await _commit_cancel(harness)
        harness.runner.cancel_event(_RUN_ID).set()
        slow.gate.set()

        result = await task

        assert result == RunResult(outcome="interrupted", steps=1)
        state = await _fold(harness)
        tool_a = _tool(state, "exec_r2s6_0001_0001")
        assert tool_a.state is ToolState.SUCCEEDED
        assert tool_a.content == (ToolTextContent(type="text", text="done A"),)
        tool_b = _tool(state, "exec_r2s6_0001_0002")
        assert tool_b.state is ToolState.CANCELLED
        assert tool_b.effect_status is EffectStatus.NOT_STARTED
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.INTERRUPTED
        message = _assistant(state, step.message_id)
        part_b = next(
            part for part in message.parts if isinstance(part, AssistantToolPart) and part.tool_call_id == "call_B"
        )
        assert part_b.state == "cancelled"
        assert part_b.effect_status == "not_started"
        assert part_b.content == [
            ToolTextContent(type="text", text="Tool execution exec_r2s6_0001_0002 was cancelled before it started.")
        ]


async def test_cancel_signal_mid_stream_synthesizes_pending_tool(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    echo = _EchoAdapter()
    script = _Script().text("p1", "partial")
    gate = script.gate()
    script.call_start(0, tool_call_id="call_1", name="app.echo")
    script.call_args('{"text": "hi"}', 0)
    script.call_end("call_1", "app.echo", '{"text": "hi"}', call_index=0)
    script.finish(FinishReason.TOOL_CALLS)
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [echo])
        await _bootstrap(harness)
        async with harness.bus.subscribe(_SESSION_ID, {PY_TEXT_DELTA}) as stream:
            task = asyncio.create_task(harness.runner.run(_SESSION_ID, _RUN_ID))
            first = await anext(stream)
        assert first.data.delta == "partial"
        await _commit_cancel(harness)
        harness.runner.cancel_event(_RUN_ID).set()
        gate.event.set()

        result = await task

        assert result == RunResult(outcome="interrupted", steps=1)
        assert len(provider.requests) == 1
        assert echo.contexts == []  # no new tool start after the cancel intent
        state = await _fold(harness)
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.INTERRUPTED
        assert step.generated  # the generation batch still landed durably
        tool = _tool(state, "exec_r2s6_0001_0001")
        assert tool.state is ToolState.CANCELLED
        assert tool.effect_status is EffectStatus.NOT_STARTED
        message = _assistant(state, step.message_id)
        assert message.status == "interrupted"
        assert [part.type for part in message.parts] == ["text", "tool"]
        assert message.parts[0].text == "partial"
        assert message.parts[1].state == "cancelled"
    assert _fetch_all(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = 'py.tool.requested'", (_SESSION_ID,)
    ) == [(0,)]


async def test_hard_cancel_mid_tool_finalizes_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    slow = _GatedAdapter()
    script = (
        _Script()
        .call_start(0, tool_call_id="call_A", name="app.slow")
        .call_end("call_A", "app.slow", '{"tag": "A"}', call_index=0)
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [slow])
        await _bootstrap(harness)
        task = asyncio.create_task(harness.runner.run(_SESSION_ID, _RUN_ID))
        await _wait_until(lambda: slow.executions == ["A"])
        await _commit_cancel(harness)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        state = await _fold(harness)
        tool = _tool(state, "exec_r2s6_0001_0001")
        assert tool.state is ToolState.UNKNOWN
        assert tool.effect_status is EffectStatus.UNKNOWN
        assert tool.content == (
            ToolTextContent(
                type="text",
                text="Tool execution exec_r2s6_0001_0001 was cancelled while running;"
                " its result could not be confirmed.",
            ),
        )
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.steps["step_r2s6_0001"].status is StepStatus.INTERRUPTED


async def test_hard_cancel_mid_stream_commits_received_text(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "Hel")
    script.gate()  # park the stream after the text so the hard cancel lands mid-stream
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)
        async with harness.bus.subscribe(_SESSION_ID, {PY_TEXT_DELTA}) as stream:
            task = asyncio.create_task(harness.runner.run(_SESSION_ID, _RUN_ID))
            first = await anext(stream)
        assert first.data.delta == "Hel"
        await _commit_cancel(harness)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        step = state.steps["step_r2s6_0001"]
        assert step.status is StepStatus.INTERRUPTED
        message = _assistant(state, step.message_id)
        assert message.status == "interrupted"
        # §3.1 L142: the received text survived the hard cancel.
        assert message.parts == [AssistantTextPart(type="text", part_id="p1", text="Hel")]
        assert state.finalized[(step.message_id, "p1")].text == "Hel"


# ---------------------------------------------------------------------------
# §2.1 L55 run-entry guards
# ---------------------------------------------------------------------------


async def test_unknown_run_is_a_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)

        with pytest.raises(ConflictError, match="unknown in session"):
            await harness.runner.run(_SESSION_ID, "run_missing")


async def test_terminal_run_cannot_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").finish()
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, _ScriptedProvider([script.items]))
        await _bootstrap(harness)
        assert await harness.runner.run(_SESSION_ID, _RUN_ID) == RunResult(outcome="succeeded", steps=1)

        with pytest.raises(ConflictError, match="already succeeded"):
            await harness.runner.run(_SESSION_ID, _RUN_ID)


async def test_max_steps_ctor_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    async with open_database(db_path, mode="create") as database:
        with pytest.raises(ValueError, match="max_steps"):
            _wire(database, _ScriptedProvider([]), max_steps=0)
        with pytest.raises(ValueError, match="max_steps"):
            _wire(database, _ScriptedProvider([]), max_steps=True)


# ---------------------------------------------------------------------------
# T-C1-022 (R2-S7 review A-009③): observed finish/usage survive an interrupt
# ---------------------------------------------------------------------------


async def test_interrupted_after_usage_keeps_the_observed_usage(tmp_path: Path) -> None:
    """A-009③: an error chunk after a usage chunk must not lose the observed
    usage/cost on the interrupted terminal message (mirroring the succeeded
    read); finish stays None -- none was observed."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    boom = CoreError(code=CoreErrorCode.PROVIDER_UNAVAILABLE, message="maas is down", retryable=False)
    script = _Script().text("p1", "so far").usage().error(boom)
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "interrupted"
        assert result.error == boom  # verbatim, never synthesized
        assert len(provider.requests) == 1  # §8 L350: no resend
        state = await _fold(harness)
        assert not state.tools  # zero tool executions
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.runs[_RUN_ID].error == boom
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "interrupted"
        assert message.error == boom
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"
        assert message.finish is None
        assert message.raw_finish is None


async def test_interrupted_after_finish_and_usage_keeps_both_observations(tmp_path: Path) -> None:
    """A-009③: an error chunk after finish+usage still interrupts (the
    defense is not relaxed) AND both observations survive onto the terminal."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    boom = CoreError(code=CoreErrorCode.PROVIDER_UNAVAILABLE, message="maas is down", retryable=False)
    script = _Script().text("p1", "so far").usage().finish().error(boom)
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "interrupted"
        assert result.error == boom
        assert len(provider.requests) == 1
        state = await _fold(harness)
        assert not state.tools  # zero tool executions
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "interrupted"
        assert message.error == boom
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"
        assert message.finish is FinishReason.STOP
        assert message.raw_finish is None


async def test_eof_without_terminal_keeps_the_terminal_fields_conservative(tmp_path: Path) -> None:
    """A-009③ (defense): an EOF without finish/usage chunks observes nothing
    -- the interrupted terminal keeps all four fields conservative None."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "partial")  # no finish/error/usage chunk: EOF face
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result.outcome == "interrupted"
        assert result.error is not None
        assert result.error.code is CoreErrorCode.PROVIDER_STREAM_INTERRUPTED
        assert len(provider.requests) == 1
        state = await _fold(harness)
        assert not state.tools  # zero tool executions
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "interrupted"
        assert message.usage is None
        assert message.cost_usd is None
        assert message.finish is None
        assert message.raw_finish is None


# ---------------------------------------------------------------------------
# T-C1-022c (T-B-022 follow-ups): m-3 non-generated symmetry + the
# succeeded->interrupted race window
# ---------------------------------------------------------------------------


async def test_interrupted_after_usage_without_text_keeps_the_observed_usage(tmp_path: Path) -> None:
    """m-3: the non-generated construction of the interrupted terminal -- an
    error after a usage chunk with zero text parts commits nothing finalized,
    so the open step carries no generated plan and the terminal is rebuilt
    from the (empty) finalized truth: parts stay empty while the observed
    usage/cost survive. m-4 attribution: like the three A-009③ tests above,
    this exercises the non-generated branch -- ``py.step.generated`` commits
    only on the success path and ``_apply_finalized`` never sets
    ``step.generated``; the generated branch is pinned separately by the m-4
    test at the end of this file."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    boom = CoreError(code=CoreErrorCode.PROVIDER_UNAVAILABLE, message="maas is down", retryable=False)
    script = _Script().usage().error(boom)  # zero text chunks: nothing to finalize
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider)
        await _bootstrap(harness)

        result = await harness.runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="interrupted", steps=1, error=boom)
        assert len(provider.requests) == 1  # §8 L350: no resend
        state = await _fold(harness)
        assert not state.tools  # zero tool executions
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.runs[_RUN_ID].error == boom
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "interrupted"
        assert message.error == boom
        assert message.parts == []
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"
        assert message.finish is None
        assert message.raw_finish is None


class _CancelAfterStepFinishedCommitter(EventCommitter):
    """A committer that injects ``py.run.cancel_requested`` right after the
    first ``py.step.finished`` batch commits -- the deterministic construction
    of the succeeded->interrupted race window (the cancel lands between
    step.finished and run.finished(succeeded), making succeeded illegal from
    cancelling)."""

    armed: bool = True

    async def commit(self, session_id: str, drafts: Sequence[EventDraft[Any]]) -> list[DurableEvent[Any]]:
        events = await super().commit(session_id, drafts)
        if self.armed and any(draft.type == "py.step.finished" for draft in drafts):
            self.armed = False  # exactly one injection, never the finalize batch
            high = await self._repos.log_high(session_id)
            draft = EventDraft[RunCancelRequestedData](
                id="evt_cancel_inject",
                type="py.run.cancel_requested",
                version=1,
                session_id=session_id,
                created_at=_CREATED_AT,
                data=RunCancelRequestedData(run_id=_RUN_ID, reason="user"),
                metadata={"run_id": _RUN_ID},
            )
            await self._store.commit(session_id, EventBatch("batch_cancel_inject", (draft,)), high)
        return events


async def test_cancel_between_step_finished_and_run_finished_interrupts_the_run(tmp_path: Path) -> None:
    """T-C1-022c (#92): a cancel landing between ``py.step.finished`` and
    ``run.finished(succeeded)`` makes succeeded illegal from cancelling
    (§2.1 L59-61) -- the fallback finalizes the run interrupted while the step
    keeps its already-committed completed terminal (the finish/usage survive
    via the succeeded-path step.finished read; the ``observed=`` expansion is
    inert on this branch because the step is already terminal)."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello").usage().finish()
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        # _wire inlined with the injection committer (_wire hardcodes the
        # plain EventCommitter; the runner only consumes its commit face).
        engine = database.engine
        store = SessionStore(engine, projector=SessionProjector())
        repos = ReadRepositories(engine)
        bus = VolatileBus()
        eventlog = EventLog(engine, repos=repos, bus=bus)
        ids = _FixedIds()
        committer = _CancelAfterStepFinishedCommitter(repos, store, bus, ids=ids, clock=_clock)
        registry = ToolRegistry(sink=committer.sink, clock=_clock, ids=ids)
        runner = Runner(
            provider=provider,
            context_builder=ContextBuilder(_settings()),
            registry=registry,
            committer=committer,
            log=eventlog,
            bus=bus,
            ids=ids,
            clock=_clock,
        )
        harness = _Harness(
            store=store,
            repos=repos,
            bus=bus,
            eventlog=eventlog,
            committer=committer,
            registry=registry,
            provider=provider,
            runner=runner,
        )
        await _bootstrap(harness)

        result = await runner.run(_SESSION_ID, _RUN_ID)

        assert result == RunResult(outcome="interrupted", steps=1)
        assert len(provider.requests) == 1
        state = await _fold(harness)
        assert not state.tools  # zero tool executions
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        assert state.runs[_RUN_ID].error is None
        # the step terminal is the succeeded-path completed read, kept verbatim
        message = _assistant(state, state.steps["step_r2s6_0001"].message_id)
        assert message.status == "completed"
        assert message.finish is FinishReason.STOP
        assert message.raw_finish is None
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"


# ---------------------------------------------------------------------------
# T-C1-023 (T-B-023 m-4): the generated branch of the interrupted terminal
# ---------------------------------------------------------------------------


async def test_interrupted_from_generated_plan_keeps_observed_usage_and_finish(tmp_path: Path) -> None:
    """m-4: cancel lands while the planned tools execute, AFTER the generation
    batch committed ``py.step.generated`` -- the interrupted terminal is then
    rebuilt from the generated plan (the L1097-1152 branch): the observed
    finish/raw_finish/usage/cost ride the plan verbatim (the L1146-1149
    application face) while the unfinished tool parts are synthesized."""
    db_path = tmp_path / "runner.db"
    _require_success(_upgrade_head(db_path))
    slow = _GatedAdapter()
    arguments_a = '{"tag": "A"}'
    arguments_b = '{"tag": "B"}'
    script = (
        _Script()
        .text("p1", "let me check")
        .call_start(0, tool_call_id="call_A", name="app.slow")
        .call_args(arguments_a, 0)
        .call_end("call_A", "app.slow", arguments_a, call_index=0)
        .call_start(1, tool_call_id="call_B", name="app.slow")
        .call_args(arguments_b, 1)
        .call_end("call_B", "app.slow", arguments_b, call_index=1)
        .usage()
        .finish(FinishReason.TOOL_CALLS)
    )
    provider = _ScriptedProvider([script.items])
    async with open_database(db_path, mode="create") as database:
        harness = _wire(database, provider, [slow])
        await _bootstrap(harness)
        task = asyncio.create_task(harness.runner.run(_SESSION_ID, _RUN_ID))
        await _wait_until(lambda: slow.executions == ["A"])
        await _commit_cancel(harness)
        harness.runner.cancel_event(_RUN_ID).set()
        slow.gate.set()

        result = await task

        assert result == RunResult(outcome="interrupted", steps=1)
        assert len(provider.requests) == 1  # single provider request (§8 L350 no resend)
        state = await _fold(harness)
        assert state.runs[_RUN_ID].status is RunStatus.INTERRUPTED
        step = state.steps["step_r2s6_0001"]
        assert step.generated  # the plan committed before the cancel: generated branch
        assert len(state.tools) == 2  # A executed, B synthesized cancelled-before-start
        message = _assistant(state, step.message_id)
        assert message.status == "interrupted"
        assert message.finish is FinishReason.TOOL_CALLS  # observed, kept verbatim
        assert message.raw_finish is None
        assert message.usage == Usage(input=5, output=7)
        assert message.cost_usd == "0.0012"
        assert [part.type for part in message.parts] == ["text", "tool", "tool"]
        tool_a = next(
            part for part in message.parts if isinstance(part, AssistantToolPart) and part.tool_call_id == "call_A"
        )
        tool_b = next(
            part for part in message.parts if isinstance(part, AssistantToolPart) and part.tool_call_id == "call_B"
        )
        assert tool_a.state == "succeeded"
        assert tool_b.state == "cancelled"
        assert tool_b.effect_status == "not_started"
