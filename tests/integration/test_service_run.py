"""R2-S7 integration tests: service scheduling, cancel, run_status, close lifecycle.

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos/runner
through the :class:`opencode.app.App` composition root. Contract anchors: plan
§2 R2-S7 (r2-slices L136-144), CORE-CONTRACTS §7 L331 (schedule + idempotent
retry + strong task reference), §7 L335 (cancel full semantics), §7 L337
(close full order), §2.1 L55-63/L60 (the cancel/started race: first durable
commit wins), §3.2 L183-188 + §3.2.1 (the runs read model, fail closed).

Race injection (④ answer, tested both branches): branch 1 (cancel first) uses
an App without a provider -- prompt accepts, cancel commits the intent, then a
manually built Runner over the same engine/bus/ids drives the run and must
finalize interrupted without ever landing ``run.started``; branch 2 (started
first) uses a gated provider stream -- run.started/step.started are committed
before the stream parks, cancel flips running->cancelling, the gate opens, the
run finalizes interrupted with the received text preserved.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard

import pytest
from pydantic import BaseModel
from sqlalchemy import event as sa_event

from opencode.app import App
from opencode.bus import VolatileBus
from opencode.bus.eventlog import EventLog
from opencode.contracts import ConfigSnapshot, CoreErrorCode
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
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session import RunRef, RunStatusValue, SessionBusyError, SessionProjector, SessionService, SessionStatus
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.runner import EventCommitter, Runner
from opencode.session.service import (
    DEFAULT_CLOSE_TIMEOUT_S,
    REASON_RUN_ERROR_UNDECODABLE,
    REASON_RUN_OWNER_MISSING,
    REASON_RUN_PROJECTION_MISSING,
    REASON_RUN_STATUS_UNKNOWN,
    RunStatus,
)
from opencode.storage import (
    LifecycleCleanupFailedError,
    NotFoundError,
    ReadRepositories,
    SessionClosedError,
    SessionStore,
    StorageCorruptError,
    StoreValidationError,
    open_database,
)
from opencode.tools import ToolContext, ToolRegistry
from opencode.tools.registry import ToolAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_CREATED_AT = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
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
    assert result.returncode == 0, f"alembic failed\nstdout:{result.stdout}\nstderr:{result.stderr}"


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _event_count(db_path: Path, session_id: str) -> int:
    row = _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,))
    return int(row[0]) if row is not None else 0


def _event_rows(db_path: Path, session_id: str) -> list[tuple[str, Any]]:
    return _fetch_all(db_path, "SELECT type, data FROM events WHERE session_id = ? ORDER BY seq", (session_id,))


def _event_types(db_path: Path, session_id: str) -> list[str]:
    return [str(row[0]) for row in _event_rows(db_path, session_id)]


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [json.loads(str(row[1])) for row in _event_rows(db_path, session_id) if str(row[0]) == event_type]


def _run_status_value(db_path: Path, run_id: str) -> str | None:
    row = _fetch_scalar(db_path, "SELECT status FROM runs WHERE run_id = ?", (run_id,))
    return None if row is None else str(row[0])


def _session_status_value(db_path: Path, session_id: str) -> str | None:
    row = _fetch_scalar(db_path, "SELECT status FROM sessions WHERE session_id = ?", (session_id,))
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
            raise AssertionError(f"run {run_id} did not reach {wanted} (stuck at {status.status})")
        await asyncio.sleep(0.01)


async def _await_tool_start(adapter: _CancellableAdapter | _GatedAdapter, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not adapter.starts:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("the planned tool never started")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# scripted provider and tool adapters (test_runner.py patterns)
# ---------------------------------------------------------------------------


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    def open(self) -> None:
        self.event.set()


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


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
                await item.event.wait()

    async def close(self) -> None:
        self.close_count += 1


class _CancellableAdapter:
    """``app.wait``: parks on the cooperative cancel signal, then returns cleanly."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self._spec = ToolSpec(
            namespace="app",
            name="wait",
            version=1,
            description="wait for the cooperative cancel signal",
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
        self.starts.append(tag)
        await context.cancel.wait()
        return ToolResult(content=[ToolTextContent(type="text", text=f"cancelled-cleanly {tag}")])


class _GatedAdapter:
    """``app.slow``: blocks on a gate that never opens; ignores the cancel signal."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.starts: list[str] = []
        self._spec = ToolSpec(
            namespace="app",
            name="slow",
            version=1,
            description="gated test tool that never completes",
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
        self.starts.append(tag)
        await self.gate.wait()
        return ToolResult(content=[ToolTextContent(type="text", text=f"done {tag}")])


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
    close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> App:
    """The full R2 composition: provider + tools + runner wired into the service."""
    return App(
        db_path,
        working_dir="C:/work",
        provider=provider,
        tools=adapters,
        settings=_settings(),
        close_timeout_s=close_timeout_s,
    )


def _plain_app(db_path: Path) -> App:
    """The R1 composition: no provider, but the engine (runs read model) stays wired."""
    return App(db_path, working_dir="C:/work")


async def _create_prompted(
    service: SessionService, *, text: str = "hello", request_id: str = "req_1"
) -> tuple[str, RunRef]:
    session = await service.create(_SNAPSHOT)
    run_ref = await service.prompt(session.session_id, text, request_id)
    return session.session_id, run_ref


def _tool_script(tool_name: str, tag: str) -> _Script:
    arguments = json.dumps({"tag": tag})
    return (
        _Script()
        .text("p1", "let me check")
        .call_start(0, tool_call_id="call_1", name=tool_name)
        .call_args(arguments, 0)
        .call_end("call_1", tool_name, arguments, call_index=0)
        .usage()
        .finish()
    )


# ---------------------------------------------------------------------------
# §7 L331: scheduling
# ---------------------------------------------------------------------------


async def test_prompt_schedules_the_runner_and_completes_the_run(tmp_path: Path) -> None:
    """prompt schedules the runner task after the accepted commit: the run
    reaches run.started, the generation commits and run.finished(succeeded)
    releases the slot and projects the idle message."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_Script().text("p1", "hello").usage().finish()]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)

        final = await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert (final.session_id, final.run_id, final.request_id) == (session_id, run_ref.run_id, "req_1")
        assert final.error is None
        assert len(provider.requests) == 1

        types = _event_types(db_path, session_id)
        for expected in (
            "py.prompt.accepted",
            "py.run.started",
            "py.step.started",
            "py.text.finalized",
            "py.step.generated",
            "py.step.finished",
            "py.run.finished",
        ):
            assert expected in types
        # The fold-side projections: the slot is released, the idle message exists.
        assert _fetch_scalar(db_path, "SELECT active_run_id FROM sessions WHERE session_id = ?", (session_id,)) == (
            None,
        )
        assert (
            _fetch_scalar(
                db_path, "SELECT session_id FROM messages WHERE message_id = ?", (f"msg_idle_{run_ref.run_id}",)
            )
            is not None
        )


async def test_idempotent_retry_does_not_reschedule_or_append_events(tmp_path: Path) -> None:
    """A same-request_id retry after completion returns the original RunRef:
    no second provider request, no new events (§7 L331 幂等不再调度)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_Script().text("p1", "hello").finish()]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        events_before = _event_count(db_path, session_id)
        assert len(provider.requests) == 1

        retried = await app.service.prompt(session_id, "hello", "req_1")
        assert retried == run_ref
        assert len(provider.requests) == 1
        assert _event_count(db_path, session_id) == events_before


# ---------------------------------------------------------------------------
# §7 L323/A-S6: the busy gate with a cancelling run
# ---------------------------------------------------------------------------


async def test_cancelling_run_occupies_the_busy_slot(tmp_path: Path) -> None:
    """A cancelling run still holds the active-run slot (A-S6): a new
    request_id is SessionBusy, the same request_id stays idempotent, and
    repeated cancels never re-commit the intent."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _plain_app(db_path) as app:
        session_id, run_ref = await _create_prompted(app.service)

        first = await app.service.cancel(run_ref.run_id)
        assert first.status is RunStatusValue.CANCELLING
        second = await app.service.cancel(run_ref.run_id)
        assert second.status is RunStatusValue.CANCELLING
        assert len(_event_data(db_path, session_id, "py.run.cancel_requested")) == 1

        with pytest.raises(SessionBusyError) as busy:
            await app.service.prompt(session_id, "another", "req_2")
        assert busy.value.core.code == CoreErrorCode.SESSION_BUSY

        idempotent = await app.service.prompt(session_id, "hello", "req_1")
        assert idempotent == run_ref


# ---------------------------------------------------------------------------
# §7 L335: cancel branches
# ---------------------------------------------------------------------------


async def test_cancel_on_a_terminal_run_returns_the_original_status(tmp_path: Path) -> None:
    """A finished run's cancel returns the original RunStatus and appends
    nothing (run.finished first -> the late cancel never overwrites)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_Script().text("p1", "hello").finish()]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        events_before = _event_count(db_path, session_id)

        status = await app.service.cancel(run_ref.run_id)
        assert status.status is RunStatusValue.SUCCEEDED
        assert (status.session_id, status.run_id, status.request_id) == (session_id, run_ref.run_id, "req_1")
        assert _event_count(db_path, session_id) == events_before


async def test_cancel_mid_run_commits_intent_and_finalizes_interrupted(tmp_path: Path) -> None:
    """§2.1 L60 branch 2 (run.started first): the running run enters
    cancelling, the received text is preserved, no new tool starts, and cancel
    waits for the interrupted terminal."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello")
    gate = script.gate()
    script.finish()
    provider = _ScriptedProvider([[script]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.RUNNING}))

        cancel_task = asyncio.create_task(app.service.cancel(run_ref.run_id))
        cancelling = await _await_run_status(
            app.service, run_ref.run_id, frozenset({RunStatusValue.CANCELLING}), timeout_s=10.0
        )
        assert cancelling.status is RunStatusValue.CANCELLING

        gate.open()
        final = await cancel_task
        assert final.status is RunStatusValue.INTERRUPTED
        assert len(provider.requests) == 1  # §8 L350: no resend
        texts = _event_data(db_path, session_id, "py.text.finalized")
        assert [entry["text"] for entry in texts] == ["hello"]  # the received text is kept
        assert "py.tool.started" not in _event_types(db_path, session_id)


async def test_concurrent_cancels_commit_one_intent_and_wait_one_cleanup(tmp_path: Path) -> None:
    """Two concurrent cancels of the same run: one cancel_requested event,
    both await the same scheduled task and return the same terminal."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = _Gate()
    script = _Script().text("p1", "hello")
    script.items.append(gate)
    script.finish()
    provider = _ScriptedProvider([[script]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.RUNNING}))

        first = asyncio.create_task(app.service.cancel(run_ref.run_id))
        second = asyncio.create_task(app.service.cancel(run_ref.run_id))
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.CANCELLING}), timeout_s=10.0)

        gate.open()
        results = await asyncio.gather(first, second)
        assert [result.status for result in results] == [RunStatusValue.INTERRUPTED, RunStatusValue.INTERRUPTED]
        assert len(_event_data(db_path, session_id, "py.run.cancel_requested")) == 1


async def test_cancel_first_rejects_run_started_and_finalizes_interrupted(tmp_path: Path) -> None:
    """§2.1 L60 branch 1 (cancel first): the accepted run goes straight to
    cancelling; a runner that starts afterwards finalizes interrupted without
    ever landing run.started."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    ids = _FixedIds()

    async with App(db_path, working_dir="C:/work", clock=_clock, ids=ids) as app:
        session_id, run_ref = await _create_prompted(app.service)
        status = await app.service.cancel(run_ref.run_id)
        assert status.status is RunStatusValue.CANCELLING
        assert "py.run.started" not in _event_types(db_path, session_id)

        # A runner over the same engine/bus/ids (the scheduler that lost the
        # race): it must observe the durable cancel and finalize interrupted.
        engine = app.database.engine
        store = SessionStore(engine, projector=SessionProjector())
        repos = ReadRepositories(engine)
        committer = EventCommitter(repos, store, app.bus, ids=ids, clock=_clock)
        registry = ToolRegistry(sink=committer.sink, clock=_clock, ids=ids)
        runner = Runner(
            provider=_ScriptedProvider([]),
            context_builder=ContextBuilder(_settings()),
            registry=registry,
            committer=committer,
            log=EventLog(engine, repos=repos, bus=app.bus),
            bus=app.bus,
            ids=ids,
            clock=_clock,
        )
        result = await runner.run(session_id, run_ref.run_id)
        assert result.outcome == "interrupted"
        assert result.steps == 0
        assert "py.run.started" not in _event_types(db_path, session_id)
        assert _run_status_value(db_path, run_ref.run_id) == "interrupted"
        assert (await app.service.run_status(run_ref.run_id)).status is RunStatusValue.INTERRUPTED


async def test_cancel_without_the_runs_read_model_is_rejected(tmp_path: Path) -> None:
    """R1 direct construction (no engine): cancel fails closed instead of
    returning a status it cannot observe honestly."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        service = SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=VolatileBus(),
            working_dir="C:/work",
        )
        _session_id, run_ref = await _create_prompted(service)
        with pytest.raises(StoreValidationError):
            await service.cancel(run_ref.run_id)


# ---------------------------------------------------------------------------
# §3.2 L187: run_status through the runs read model
# ---------------------------------------------------------------------------


async def test_run_status_resolves_historical_runs_without_scanning_events(tmp_path: Path) -> None:
    """A finished run (slot released) still resolves by its runs row -- the
    R1 face would answer NotFound here -- and the query never touches the
    events log (OP-2, T-A-005)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_Script().text("p1", "hello").finish()]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert _fetch_scalar(db_path, "SELECT active_run_id FROM sessions WHERE session_id = ?", (session_id,)) == (
            None,
        )

        statements: list[str] = []

        def _record(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
            statements.append(str(statement))

        sa_event.listen(app.database.engine.sync_engine, "before_cursor_execute", _record)
        try:
            status = await app.service.run_status(run_ref.run_id)
        finally:
            sa_event.remove(app.database.engine.sync_engine, "before_cursor_execute", _record)

        assert status.status is RunStatusValue.SUCCEEDED
        assert (status.session_id, status.request_id) == (session_id, "req_1")
        assert not any("FROM events" in statement.upper() for statement in statements)


async def test_run_status_fails_closed_on_missing_and_corrupt_projection(tmp_path: Path) -> None:
    """The runs-read-model faces fail closed (§3.2.1): a missing projection
    over surviving traces, an unknown status, an undecodable error payload,
    a missing owner row -- each StorageCorrupt with its sanitized reason; a
    never-existed run is NotFound."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider(
        [
            [_Script().text("p1", "hello").usage().finish()],
            [_Script().text("p1", "done").finish()],
        ]
    )

    async with _run_app(db_path, provider) as app:
        # Every variant quiesces its run first: the sync sqlite mutations must
        # never race a live writer transaction (a suspended runner holds the
        # SQLite lock while the loop is blocked, deadlocking the aiosqlite
        # futures). The corruption faces are read-model classifications; they
        # need a quiet DB, not a live task.
        # (a) finished run, runs row deleted -> missing projection over traces.
        _session_a, run_a = await _create_prompted(app.service, request_id="req_a")
        await _await_run_status(app.service, run_a.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_a.run_id,))
        with pytest.raises(StorageCorruptError) as corrupt_a:
            await app.service.run_status(run_a.run_id)
        assert corrupt_a.value.core.details["reason"] == REASON_RUN_PROJECTION_MISSING

        # (b) finished run, idle message survives, runs row deleted.
        _session_b, run_b = await _create_prompted(app.service, text="done", request_id="req_b")
        await _await_run_status(app.service, run_b.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM runs WHERE run_id = ?", (run_b.run_id,))
        with pytest.raises(StorageCorruptError) as corrupt_b:
            await app.service.run_status(run_b.run_id)
        assert corrupt_b.value.core.details["reason"] == REASON_RUN_PROJECTION_MISSING

        # (c) unknown status value -> status corruption.
        _session_c, run_c = await _create_prompted(app.service, request_id="req_c")
        await _await_run_status(app.service, run_c.run_id, frozenset({RunStatusValue.INTERRUPTED}))
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE runs SET status = 'bogus' WHERE run_id = ?", (run_c.run_id,))
        with pytest.raises(StorageCorruptError) as corrupt_c:
            await app.service.run_status(run_c.run_id)
        assert corrupt_c.value.core.details["reason"] == REASON_RUN_STATUS_UNKNOWN

        # (d) undecodable error payload -> status corruption.
        _session_d, run_d = await _create_prompted(app.service, request_id="req_d")
        await _await_run_status(app.service, run_d.run_id, frozenset({RunStatusValue.INTERRUPTED}))
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE runs SET error = '{nope' WHERE run_id = ?", (run_d.run_id,))
        with pytest.raises(StorageCorruptError) as corrupt_d:
            await app.service.run_status(run_d.run_id)
        assert corrupt_d.value.core.details["reason"] == REASON_RUN_ERROR_UNDECODABLE

        # (e) runs row over a missing sessions row -> ownership corruption.
        session_e, run_e = await _create_prompted(app.service, request_id="req_e")
        await _await_run_status(app.service, run_e.run_id, frozenset({RunStatusValue.INTERRUPTED}))
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_e,))
        with pytest.raises(StorageCorruptError) as corrupt_e:
            await app.service.run_status(run_e.run_id)
        assert corrupt_e.value.core.details["reason"] == REASON_RUN_OWNER_MISSING

        # (f) a never-existed run is NotFound, never fabricated.
        with pytest.raises(NotFoundError) as missing:
            await app.service.run_status("run_never_existed")
        assert missing.value.core.code == CoreErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# §7 L337: the close lifecycle
# ---------------------------------------------------------------------------


async def test_close_commits_shutdown_intent_and_closes_the_provider(tmp_path: Path) -> None:
    """Normal close: one shutdown intent for the live run, the runner
    finalizes interrupted through the cooperative signal, no cleanup_failed,
    the provider closes once and the service/DB gates close in order."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_tool_script("app.wait", "t1")]])
    waiter = _CancellableAdapter()

    async with _run_app(db_path, provider, adapters=[waiter]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_tool_start(waiter)

        await app.close()

        intents = _event_data(db_path, session_id, "py.run.cancel_requested")
        assert [intent["reason"] for intent in intents] == ["shutdown"]
        assert intents[0]["run_id"] == run_ref.run_id
        assert _run_status_value(db_path, run_ref.run_id) == "interrupted"
        assert _session_status_value(db_path, session_id) == "active"  # no cleanup_failed
        assert provider.close_count == 1
        assert app.service.closed is True
        assert app.database.closed is True

        with pytest.raises(SessionClosedError):
            await app.service.prompt(session_id, "more", "req_2")

        await app.close()  # repeated close reuses the finished cleanup task
        assert provider.close_count == 1


async def test_concurrent_close_reuses_one_cleanup_task(tmp_path: Path) -> None:
    """Concurrent + repeated close: one cleanup task, one shutdown intent,
    one provider close (§7 L337 末句)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_tool_script("app.wait", "t1")]])
    waiter = _CancellableAdapter()

    async with _run_app(db_path, provider, adapters=[waiter]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_tool_start(waiter)

        await asyncio.gather(app.close(), app.close())
        await app.close()

        assert len(_event_data(db_path, session_id, "py.run.cancel_requested")) == 1
        assert provider.close_count == 1
        assert _run_status_value(db_path, run_ref.run_id) == "interrupted"


async def test_close_timeout_injects_unknown_and_marks_cleanup_failed(tmp_path: Path) -> None:
    """Close timeout with an unkillable-in-time tool: the started tool gets
    its honest unknown/unknown terminal, the run ends interrupted, the
    session is marked cleanup_failed (never reported as cleaned), and the
    session is no longer executable after reopen."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_tool_script("app.slow", "t1")]])
    slow = _GatedAdapter()

    async with _run_app(db_path, provider, adapters=[slow], close_timeout_s=0.2) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_tool_start(slow)

        await app.close()

        terminals = _event_data(db_path, session_id, "py.tool.finished")
        assert len(terminals) == 1
        assert terminals[0]["state"] == "unknown"
        assert terminals[0]["effect_status"] == "unknown"
        assert _run_status_value(db_path, run_ref.run_id) == "interrupted"
        assert _session_status_value(db_path, session_id) == "cleanup_failed"
        assert app.service.closed is True
        assert provider.close_count == 1

    async with _plain_app(db_path) as reopened:  # the writer lock was released
        assert (await reopened.service.session_status(session_id)) is SessionStatus.CLEANUP_FAILED
        with pytest.raises(LifecycleCleanupFailedError):
            await reopened.service.prompt(session_id, "more", "req_2")


async def test_close_keeps_storage_only_accepted_runs_for_recovery(tmp_path: Path) -> None:
    """A storage-only accepted run (no live task) is not cancelled by close:
    records kept, nothing claimed cleaned (§7 L325/L337, R4 owns the
    terminal)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _plain_app(db_path) as app:
        session_id, run_ref = await _create_prompted(app.service)

        await app.close()

        assert _event_count(db_path, session_id) == 2  # created + accepted, no cancel intent
        assert _run_status_value(db_path, run_ref.run_id) == "accepted"
        assert _session_status_value(db_path, session_id) == "active"

    async with _plain_app(db_path) as reopened:
        assert (await reopened.service.run_status(run_ref.run_id)).status is RunStatusValue.ACCEPTED


# ---------------------------------------------------------------------------
# T-C1-022 (R2-S7 review follow-up): untested defenses, close patches
# ---------------------------------------------------------------------------


class _CrashingProvider(_ScriptedProvider):
    """A provider whose stream raises a non-CoreError mid-stream.

    The scheduled task crashes (the runner lets unexpected exceptions
    propagate); _drive_run records the crash for cancel() to report.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.crashed = asyncio.Event()

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        yield TextDeltaChunk(type="text_delta", request_id="req_script", index=1, part_id="p1", delta="partial")
        self.crashed.set()
        raise RuntimeError("provider exploded")


class _GatedCloseProvider(_ScriptedProvider):
    """A provider whose close() parks until the test releases it.

    Keeps the engine alive while close() sits in its Phase 3 provider wait so
    a concurrent cancel() can still re-read the runs table (the m-1
    determinism window: the re-read must finish before the engine goes away).
    """

    def __init__(self, scripts: Sequence[Sequence[BaseModel | _Gate | _Script]]) -> None:
        super().__init__(scripts)
        self._close_gate = asyncio.Event()

    async def close(self) -> None:
        await self._close_gate.wait()
        await super().close()

    def release_close(self) -> None:
        self._close_gate.set()


async def test_cancel_reports_a_crashed_task_with_the_original_cause(tmp_path: Path) -> None:
    """M-1①: a scheduled task that crashes (a non-CoreError escaping the
    runner) is reported by cancel() as LifecycleCleanupFailedError chained to
    the original exception; the session is marked cleanup_failed with the
    sanitized diagnostic and the run keeps its honest cancelling state."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _CrashingProvider()

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await provider.crashed.wait()
        await asyncio.sleep(0.1)  # the crash reaches _drive_run's recorder

        with pytest.raises(LifecycleCleanupFailedError) as failed:
            await app.service.cancel(run_ref.run_id)

        assert "RuntimeError" in str(failed.value)
        assert isinstance(failed.value.__cause__, RuntimeError)
        assert str(failed.value.__cause__) == "provider exploded"
        assert _session_status_value(db_path, session_id) == "cleanup_failed"
        row = _fetch_scalar(db_path, "SELECT diagnostic FROM sessions WHERE session_id = ?", (session_id,))
        assert row is not None
        diagnostic = json.loads(str(row[0]))
        assert diagnostic["reason"] == "cleanup_unconfirmed"
        assert _run_status_value(db_path, run_ref.run_id) == "cancelling"


async def test_cancel_without_a_live_task_commits_exactly_one_intent(tmp_path: Path) -> None:
    """M-1②: a storage-only accepted run (no live task) cancels to CANCELLING
    with exactly one durable cancel_requested event -- repeated cancels never
    re-commit the intent (R4 owns the terminal)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _plain_app(db_path) as app:
        session_id, run_ref = await _create_prompted(app.service)

        final = await app.service.cancel(run_ref.run_id)
        again = await app.service.cancel(run_ref.run_id)

        assert final.status is RunStatusValue.CANCELLING
        assert again.status is RunStatusValue.CANCELLING
        intents = _event_data(db_path, session_id, "py.run.cancel_requested")
        assert len(intents) == 1
        assert intents[0]["reason"] == "user"
        assert _run_status_value(db_path, run_ref.run_id) == "cancelling"


async def test_run_status_fails_closed_on_unknown_owner_status(tmp_path: Path) -> None:
    """M-1③: a runs row whose owner session carries an unknown status is
    StorageCorrupt with the sanitized owner reason (§3.2.1 fail-closed --
    never fabricated, never skipped)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_Script().text("p1", "hello").usage().finish()]])

    async with _run_app(db_path, provider) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))

        with sqlite3.connect(db_path) as connection:  # quiesced run: sync mutation is safe
            connection.execute("UPDATE sessions SET status = 'bogus' WHERE session_id = ?", (session_id,))
        with pytest.raises(StorageCorruptError) as corrupt:
            await app.service.run_status(run_ref.run_id)
        assert corrupt.value.core.details["reason"] == REASON_RUN_OWNER_MISSING


async def test_close_timeout_s_rejects_non_finite_numbers(tmp_path: Path) -> None:
    """n-2: +inf (and NaN, and -inf) close timeouts are rejected at
    construction -- the close lifecycle's waits must stay bounded."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        for bad in (float("inf"), float("nan"), float("-inf")):
            with pytest.raises(StoreValidationError):
                SessionService(
                    store=SessionStore(database.engine, projector=SessionProjector()),
                    repos=ReadRepositories(database.engine),
                    bus=VolatileBus(),
                    working_dir="C:/work",
                    close_timeout_s=bad,
                )


async def test_cancel_survives_a_hard_cancelled_runnable_task(tmp_path: Path) -> None:
    """m-1: awaiting a scheduled task that exits through its own hard cancel
    (the close-timeout path) must not tear down cancel() -- the CancelledError
    from the awaited task is distinguished from the caller's own cancellation
    and the re-read reports the committed interrupted terminal."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _GatedCloseProvider([[_tool_script("app.slow", "t1")]])
    slow = _GatedAdapter()

    async with _run_app(db_path, provider, adapters=[slow], close_timeout_s=0.2) as app:
        _session_id, run_ref = await _create_prompted(app.service)
        await _await_tool_start(slow)

        cancel_task = asyncio.create_task(app.service.cancel(run_ref.run_id))
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.CANCELLING}))
        close_task = asyncio.create_task(app.close())

        final = await cancel_task  # must NOT raise CancelledError

        assert final.status is RunStatusValue.INTERRUPTED
        provider.release_close()
        await close_task
        assert provider.close_count == 1
        assert app.service.closed is True


async def test_close_tolerates_a_quarantined_session(tmp_path: Path) -> None:
    """④-4 (A-010): a session quarantined mid-run must not break close -- the
    shutdown intent commit raises SessionReadOnlyError, close skips that
    session and still drains the run, closes the provider and the gates; the
    quarantined session keeps its records and watermarks untouched."""
    from opencode.storage import SessionReadOnlyError

    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_tool_script("app.wait", "t1")]])
    waiter = _CancellableAdapter()

    async with _run_app(db_path, provider, adapters=[waiter]) as app:
        session_id, run_ref = await _create_prompted(app.service)
        await _await_tool_start(waiter)

        # Quarantine mid-run through the async engine (the sync sqlite3
        # discipline forbids mutations beside a live runner).
        async with app.database.engine.connect() as connection:
            await connection.exec_driver_sql(
                "UPDATE sessions SET status = 'read_only_diagnostic', diagnostic = ? WHERE session_id = ?",
                ('{"reason": "test_quarantine"}', session_id),
            )
            await connection.commit()
        events_before = _event_count(db_path, session_id)
        applied_before = _fetch_scalar(db_path, "SELECT applied_seq FROM sessions WHERE session_id = ?", (session_id,))

        await app.close()  # must not raise despite the quarantined session

        assert _event_count(db_path, session_id) == events_before
        assert (
            _fetch_scalar(db_path, "SELECT applied_seq FROM sessions WHERE session_id = ?", (session_id,))
            == applied_before
        )
        assert "py.run.cancel_requested" not in _event_types(db_path, session_id)
        assert "py.tool.finished" not in _event_types(db_path, session_id)
        assert _run_status_value(db_path, run_ref.run_id) == "running"
        assert _session_status_value(db_path, session_id) == "read_only_diagnostic"
        assert provider.close_count == 1
        assert app.service.closed is True
        assert app.database.closed is True

        # The quarantine gate (step 1) precedes the service-level closed gate.
        with pytest.raises(SessionReadOnlyError):
            await app.service.prompt(session_id, "more", "req_2")

        await app.close()  # repeated close reuses the finished cleanup task
        assert provider.close_count == 1
