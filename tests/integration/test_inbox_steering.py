"""T-C1-100 (P7-S1) integration tests: the steering inbox + interrupt face.

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos/runner
through :class:`opencode.app.App` (the test_approval.py composition form),
with a gated scripted provider: a stream may park on a ``threading.Event``
before yielding, which is how every test holds a run mid-step so busy-session
admissions, step-boundary steer consumption, the settle recheck (the idle-wake
doorbell) and the interrupt face are all driven deterministically instead of
by sleep-and-hope.

Contract anchors (ticket T-C1-100 §3): §3-1 the ``delivery`` opt-in (None =
the §7 busy hard-refusal face byte-identical), §3-3 step-boundary steer
consumption (at most one per step, ``py.inbox.delivered``), §3-4 the
settle-recheck idle-wake (queue rows only), §3-5 interrupt = the cancel
machinery + ``{interrupted: bool}`` + the resume drive (steers only, queued
prompts stay parked), §3-6 the three inbox endpoints, §3-10 pending rows
survive the restart and the post-scan wake.

④ disclosures: the admission race (a queue prompt landing inside the
settle window) is pinned by its INVARIANT (never stranded, never
duplicated), not by one interleaving -- the window is not injectable
without hooks; the "process restart" is the approval-file precedent (a
second App over the same data directory after the first fully closed,
``mode="readwrite"``); the wire leg boots a real ``uvicorn.Server`` on a
worker thread (the 054/056 form, helpers replicated per the no-import
rule) and the interrupt call that must block rides its own helper thread
while the test polls the durable ``cancelling`` state and releases the
provider gate -- cross-loop safety is why the gates are threading.Events.
The other half of the determinism story: every busy-session admission in
these tests waits for the provider's stream entry first
(``_await_parked`` / ``_await_parked_sync`` over the recorded-request
count). ``requests.append`` is the first statement of ``stream()``, so
once the count is observed the run is provably inside its provider call
and cannot cross another step boundary until its gate opens -- without
that handoff, an admission racing between the accept and the first
boundary would be consumed by the boundary's steer recheck (§3-3), which
is legal behavior but not the interleaving under test.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import (
    PY_INBOX_CANCELLED,
    PY_INBOX_DELIVERED,
    PY_INBOX_DELIVERY_CHANGED,
    PY_INBOX_ENQUEUED,
    PY_PROMPT_ACCEPTED,
    PY_RUN_CANCEL_REQUESTED,
)
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
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
from opencode.httpapi import create_app
from opencode.httpapi.serving import _server_config
from opencode.sdk import OpencodeClient
from opencode.sdk.client import InboxRef as ClientInboxRef
from opencode.sdk.client import InterruptReceipt as ClientInterruptReceipt
from opencode.sdk.client import PendingInboxItem
from opencode.session import RunStatusValue, SessionService
from opencode.session.context import RuntimeSettings
from opencode.session.service import (
    DEFAULT_CLOSE_TIMEOUT_S,
    InboxRef,
    InterruptReceipt,
    RunRef,
    RunStatus,
    SessionBusyError,
)
from opencode.storage import ConflictError, IdempotencyConflictError, NotFoundError, OpenMode
from opencode.tools import ToolAdapter, ToolContext

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_SNAPSHOT_BODY = _SNAPSHOT.model_dump(mode="json")
_MODEL = ModelRef(provider="maas", model="test-model")
_TOKEN = "test-token-1234"
_TERMINAL = {RunStatusValue.SUCCEEDED, RunStatusValue.FAILED, RunStatusValue.INTERRUPTED}


# ---------------------------------------------------------------------------
# migration + sqlite helpers (the test_approval.py / test_sdk_client.py forms)
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


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    rows = _fetch_all(
        db_path, "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq", (session_id, event_type)
    )
    return [json.loads(str(row[0])) for row in rows]


def _event_count(db_path: Path, session_id: str, event_type: str) -> int:
    row = _fetch_scalar(
        db_path, "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = ?", (session_id, event_type)
    )
    assert row is not None
    return int(row[0])


def _batch_ids(db_path: Path, session_id: str, event_types: Sequence[str]) -> list[tuple[str, str]]:
    """``(type, batch_id)`` rows for the given types, in seq order."""
    marks = ",".join("?" for _ in event_types)
    return [
        (str(row[0]), str(row[1]))
        for row in _fetch_all(
            db_path,
            f"SELECT type, batch_id FROM events WHERE session_id = ? AND type IN ({marks}) ORDER BY seq",
            (session_id, *event_types),
        )
    ]


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


async def _await_events(
    db_path: Path, session_id: str, event_type: str, count: int, *, timeout_s: float = 5.0
) -> list[dict[str, Any]]:
    """Poll the events table until ``count`` rows of one type exist (live sqlite read)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            rows = _fetch_all(
                db_path,
                "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
                (session_id, event_type),
            )
        except sqlite3.OperationalError:
            rows = None  # a concurrent writer holds the lock; retry after a tick
        if rows is not None and len(rows) >= count:
            return [json.loads(str(row[0])) for row in rows[:count]]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"only {0 if rows is None else len(rows)} {event_type} rows appeared for {session_id}")
        await asyncio.sleep(0.01)


async def _await_parked(provider: _GatedProvider, count: int = 1, *, timeout_s: float = 5.0) -> None:
    """Wait until the provider has served ``count`` stream entries (④ determinism).

    ``requests.append`` is the first statement of ``stream()``, so once the
    count is observed the run is inside its provider call and cannot cross
    another step boundary until its gate opens: the busy-session writes
    that follow land mid-step, never inside the boundary-1 steer-consume
    window (§3-3 -- consuming an admission that raced ahead of the stream
    would be legal, but it is not the interleaving under test).
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while len(provider.requests) < count:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"only {len(provider.requests)} provider streams started (wanted {count})")
        await asyncio.sleep(0.005)


def _await_parked_sync(provider: _GatedProvider, count: int = 1, *, timeout_s: float = 10.0) -> None:
    """The wire-leg form of :func:`_await_parked` (no loop; server thread owns the run)."""
    deadline = time.monotonic() + timeout_s
    while len(provider.requests) < count:
        assert time.monotonic() < deadline, f"only {len(provider.requests)} provider streams started (wanted {count})"
        time.sleep(0.01)


def _user_texts(request: GenerationRequest) -> list[str]:
    """Every user text of one recorded generation request (the history face)."""
    texts: list[str] = []
    for message in request.messages:
        if message.role == "user":
            for part in message.content:
                if isinstance(part, LLMTextPart):
                    texts.append(part.text)
    return texts


# ---------------------------------------------------------------------------
# the gated scripted provider (threading gates: both loops must be able to wait)
# ---------------------------------------------------------------------------


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending chunk indexes."""

    def __init__(self) -> None:
        self.items: list[ProviderChunk] = []
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


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


def _tool_script(*, tag: str = "s1", call_id: str = "call_s1") -> _Script:
    """A one-tool-call turn: forces a second step so a mid-run steer has a boundary."""
    arguments = json.dumps({"tag": tag})
    return (
        _Script()
        .text("p1", "let me read")
        .call_start(0, tool_call_id=call_id, name="tst.read")
        .call_args(arguments, 0)
        .call_end(call_id, "tst.read", arguments, call_index=0)
        .usage()
        .finish()
    )


class _GatedProvider:
    """Scripted provider: one stream per request, optional gate before the chunks.

    A gate parks the run inside its provider stream (mid-step, RUNNING), which
    is how each test times its busy-session writes against the run lifecycle.
    The gates are ``threading.Event`` -- the same provider instance serves the
    direct compositions (the test loop) and the booted-server wire leg (the
    server thread's loop), so a loop-bound asyncio.Event would be a race of
    its own (④: the poll loop below is cross-loop safe and needs no executor).
    """

    def __init__(self, streams: Sequence[object] = ()) -> None:
        self.streams: list[object] = list(streams)
        self.requests: list[GenerationRequest] = []

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        entry = self.streams.pop(0) if self.streams else None
        items: Sequence[ProviderChunk]
        if entry is None:
            items = _final_script().items
        elif isinstance(entry, tuple):
            gate: threading.Event = entry[0]
            items = entry[1]
            while not gate.is_set():  # noqa: ASYNC110 -- threading.Event on purpose (④ cross-loop gates)
                await asyncio.sleep(0.005)
        else:
            items = entry  # type: ignore[assignment]
        for chunk in items:
            yield chunk

    async def close(self) -> None:
        pass


class _ReadAdapter:
    """``tst.read``: a read-only tool (no approval face) whose executions are recorded."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self._spec = _read_spec()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        tag = str(input.get("tag", ""))
        self.starts.append(tag)
        return ToolResult(content=[ToolTextContent(type="text", text=f"read {tag}")])


def _read_spec() -> ToolSpec:
    return ToolSpec(
        namespace="tst",
        name="read",
        version=1,
        description="read-only test tool (no approval)",
        input_schema={
            "type": "object",
            "properties": {"tag": {"type": "string"}},
            "required": ["tag"],
            "additionalProperties": False,
        },
        supported_content_types=["text"],
        side_effects=False,
        idempotency_support=False,
    )


# ---------------------------------------------------------------------------
# app harness (the test_approval.py composition form)
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
    provider: _GatedProvider,
    *,
    adapters: Sequence[ToolAdapter] = (),
    mode: OpenMode = "create",
    close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> App:
    """The full composition: provider + tools + runner + the inbox face wired into the service."""
    return App(
        db_path,
        mode=mode,
        working_dir="C:/work",
        provider=provider,
        tools=adapters,
        settings=_settings(),
        close_timeout_s=close_timeout_s,
    )


async def _create_session(service: SessionService) -> str:
    return (await service.create(_SNAPSHOT)).session_id


# ---------------------------------------------------------------------------
# §4-1: the admission matrix (busy + None/steer/queue, idle, parked retries)
# ---------------------------------------------------------------------------


async def test_admission_matrix_busy_faces_parked_retries_and_idle_promotion(tmp_path: Path) -> None:
    """§4-1: busy+None keeps the §7 hard refusal; busy+delivery parks; a parked
    same-text retry stays parked while busy and promotes once idle; the queue
    item auto-wakes at the settle -- and the wake run's own first boundary
    consumes the still-pending steer (steer consumption is session-scoped,
    §3-3: a steer never strands, §5 disclosure)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate_a = threading.Event()
    gate_b = threading.Event()
    provider = _GatedProvider(
        [(gate_a, _final_script().items), _final_script().items, (gate_b, _final_script().items), _final_script().items]
    )
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)

        run_a = await service.prompt(session_id, "hold this", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)

        # busy + None: the §7 zero-regression face, unchanged.
        with pytest.raises(SessionBusyError):
            await service.prompt(session_id, "no delivery", "r_none")

        # busy + steer / busy + queue: the admission face (§4-1).
        steer = await service.prompt(session_id, "steer body", "r3", delivery="steer")
        assert isinstance(steer, InboxRef)
        assert (steer.session_id, steer.kind, steer.delivery, steer.request_id) == (session_id, "user", "steer", "r3")
        queued = await service.prompt(session_id, "queue body", "r4", delivery="queue")
        assert isinstance(queued, InboxRef)
        assert queued.delivery == "queue"

        rows = await service.inbox_list(session_id)
        assert [row.request_id for row in rows] == ["r3", "r4"]
        assert [row.delivery for row in rows] == ["steer", "queue"]
        assert [row.message.text for row in rows] == ["steer body", "queue body"]
        assert all(row.kind == "user" for row in rows)
        assert rows[0].enqueued_seq < rows[1].enqueued_seq
        assert _event_count(db_path, session_id, PY_INBOX_ENQUEUED) == 2

        # Parked retries (§3-1 step 2.5): same text + still busy = the busy
        # face again (the item stays parked, never a second admission); a
        # different text under the parked request_id = the §7 conflict.
        with pytest.raises(SessionBusyError):
            await service.prompt(session_id, "steer body", "r3", delivery="steer")
        with pytest.raises(IdempotencyConflictError):
            await service.prompt(session_id, "a different text", "r3", delivery="steer")
        assert len(await service.inbox_list(session_id)) == 2

        # Settle run A: the queue item auto-wakes (§3-4) into its own run,
        # and that successor run's first boundary consumes the pending steer
        # (session-scoped §3-3): both texts ride the successor's context.
        gate_a.set()
        final_a = await _await_run_status(service, run_a.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final_a.status is RunStatusValue.SUCCEEDED
        accepted = await _await_events(db_path, session_id, PY_PROMPT_ACCEPTED, 2)
        assert accepted[1]["request_id"] == "r4"  # the parked item's original request id
        wake_run = str(accepted[1]["run_id"])
        wake_final = await _await_run_status(service, wake_run, frozenset(_TERMINAL))
        assert wake_final.status is RunStatusValue.SUCCEEDED
        assert "queue body" in _user_texts(provider.requests[1])
        assert "steer body" in _user_texts(provider.requests[1])
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERED) == 2  # the wake + the boundary consume
        assert await service.inbox_list(session_id) == []

        # A steer parked on a run that settles WITHOUT a successor stays
        # parked: the idle recheck wakes queue rows only (§3-4), so the
        # parked-retry promotion leg (§3-1 step 2.5) has its own turn.
        run_c = await service.prompt(session_id, "hold again", "r5")
        assert isinstance(run_c, RunRef)
        await _await_parked(provider, 3)
        parked_steer = await service.prompt(session_id, "pivot five", "r6", delivery="steer")
        assert isinstance(parked_steer, InboxRef)
        with pytest.raises(SessionBusyError):
            await service.prompt(session_id, "pivot five", "r6", delivery="steer")
        gate_b.set()
        final_c = await _await_run_status(service, run_c.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final_c.status is RunStatusValue.SUCCEEDED
        assert [row.request_id for row in await service.inbox_list(session_id)] == ["r6"]

        # Idle + parked same-text retry (§3-1 step 2.5): promotes now.
        promoted = await service.prompt(session_id, "pivot five", "r6", delivery="steer")
        assert isinstance(promoted, RunRef)
        assert promoted.request_id == "r6"
        assert promoted.run_id != run_c.run_id
        await _await_run_status(service, promoted.run_id, frozenset(_TERMINAL))
        assert "pivot five" in _user_texts(provider.requests[3])
        assert await service.inbox_list(session_id) == []
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERED) == 3
        assert _event_count(db_path, session_id, PY_INBOX_ENQUEUED) == 3  # no second admission

        # §3-1 step 2.5 retry face: the promoted run (an unkeyed accept -- a
        # keyed batch is single-row) answers the same-text retry with the
        # original RunRef through the runs read model, never a second run;
        # a different text under the same request_id is the §7 conflict.
        retried = await service.prompt(session_id, "pivot five", "r6", delivery="steer")
        assert isinstance(retried, RunRef)
        assert (retried.run_id, retried.request_id) == (promoted.run_id, "r6")
        with pytest.raises(IdempotencyConflictError):
            await service.prompt(session_id, "a third text", "r6", delivery="steer")
        assert _event_count(db_path, session_id, PY_INBOX_ENQUEUED) == 3  # still no new admission
        assert _event_count(db_path, session_id, PY_PROMPT_ACCEPTED) == 4  # r1, the wake, r5, the promotion


async def test_idle_delivery_admits_and_starts_atomically(tmp_path: Path) -> None:
    """§4-1 idle legs: an explicit delivery on an idle session never parks --
    enqueued + accepted + delivered ride one atomic batch (steer and queue)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _GatedProvider()
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)

        run_ref = await service.prompt(session_id, "immediate queue", "rq1", delivery="queue")
        assert isinstance(run_ref, RunRef)
        await _await_run_status(service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert await service.inbox_list(session_id) == []

        # §3-1 step 2.5 retry face: the unkeyed atomic accept answers the
        # same-text retry with the original RunRef; a different text is the
        # §7 conflict -- never a second admission, never a second run.
        retried = await service.prompt(session_id, "immediate queue", "rq1", delivery="queue")
        assert isinstance(retried, RunRef)
        assert retried.run_id == run_ref.run_id
        with pytest.raises(IdempotencyConflictError):
            await service.prompt(session_id, "different", "rq1", delivery="queue")

        ids = _batch_ids(db_path, session_id, (PY_INBOX_ENQUEUED, PY_PROMPT_ACCEPTED, PY_INBOX_DELIVERED))
        assert [row[0] for row in ids] == [PY_INBOX_ENQUEUED, PY_PROMPT_ACCEPTED, PY_INBOX_DELIVERED]
        assert len({row[1] for row in ids}) == 1  # one batch, one commit

        session_id2 = await _create_session(service)
        run_ref2 = await service.prompt(session_id2, "immediate steer", "rs1", delivery="steer")
        assert isinstance(run_ref2, RunRef)
        await _await_run_status(service, run_ref2.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert await service.inbox_list(session_id2) == []


# ---------------------------------------------------------------------------
# §4-2: the steer full chain (step-boundary consumption into the next context)
# ---------------------------------------------------------------------------


async def test_steer_consumed_at_step_boundary_into_next_context(tmp_path: Path) -> None:
    """§4-2: run A holds mid-step, a steer parks, the next step boundary
    consumes it (one ``py.inbox.delivered``) and the steered text is in the
    next step's request history; the endpoint faces on the delivered item fail
    closed (update = ConflictError, cancel = the no-op)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider = _GatedProvider([(gate, _tool_script().items), _final_script().items])
    read = _ReadAdapter()
    async with _run_app(db_path, provider, adapters=[read]) as app:
        service = app.service
        session_id = await _create_session(service)

        run_a = await service.prompt(session_id, "run with a tool step", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)
        steer = await service.prompt(session_id, "please pivot", "r2", delivery="steer")
        assert isinstance(steer, InboxRef)

        gate.set()
        final = await _await_run_status(service, run_a.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final.status is RunStatusValue.SUCCEEDED
        assert read.starts == ["s1"]  # the tool step executed

        # §3-3: exactly one delivered, at the step boundary, into step 2's context.
        delivered = _event_data(db_path, session_id, PY_INBOX_DELIVERED)
        assert [row["inbox_id"] for row in delivered] == [steer.inbox_id]
        assert "please pivot" in _user_texts(provider.requests[1])
        assert "please pivot" not in _user_texts(provider.requests[0])
        assert await service.inbox_list(session_id) == []

        # §3-6 fail-closed legs on the delivered item: update is a conflict,
        # cancel is the honest no-op, and neither commits anything.
        with pytest.raises(ConflictError):
            await service.inbox_update(session_id, steer.inbox_id, "queue")
        assert await service.inbox_cancel(session_id, steer.inbox_id) is False
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERY_CHANGED) == 0
        assert _event_count(db_path, session_id, PY_INBOX_CANCELLED) == 0


# ---------------------------------------------------------------------------
# §4-3: queue + idle-wake (+ the doorbell-window invariant)
# ---------------------------------------------------------------------------


async def test_queued_input_auto_wakes_after_settle(tmp_path: Path) -> None:
    """§4-3: a queue item parked while A is busy auto-wakes into run B at A's
    settle; the promotion reuses the parked item's original request id and
    pairs accepted + delivered in one batch."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider = _GatedProvider([(gate, _final_script().items), _final_script().items])
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)

        run_a = await service.prompt(session_id, "first", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)
        queued = await service.prompt(session_id, "queued one", "r2", delivery="queue")
        assert isinstance(queued, InboxRef)

        gate.set()
        await _await_run_status(service, run_a.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        accepted = await _await_events(db_path, session_id, PY_PROMPT_ACCEPTED, 2)
        assert accepted[1]["request_id"] == "r2"
        run_b = str(accepted[1]["run_id"])
        await _await_run_status(service, run_b, frozenset({RunStatusValue.SUCCEEDED}))

        assert "queued one" in _user_texts(provider.requests[1])
        promotion = _batch_ids(db_path, session_id, (PY_PROMPT_ACCEPTED, PY_INBOX_DELIVERED))
        # The wake's accepted+delivered pair shares one batch (the last two of each type).
        assert promotion[-2][1] == promotion[-1][1]
        assert await service.inbox_list(session_id) == []


async def test_doorbell_window_never_strands_or_duplicates(tmp_path: Path) -> None:
    """§4-3 race leg (④ invariant form): a queue prompt issued in the settle
    window lands on one of two legal paths -- parked-then-woken or
    admitted-and-started directly. Whichever runs, the item is never stranded
    and never consumed twice: exactly one successor run exists."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider = _GatedProvider([(gate, _final_script().items), _final_script().items, _final_script().items])
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)
        run_a = await service.prompt(session_id, "first", "r1")
        assert isinstance(run_a, RunRef)

        # The prompt and the settle run on the same loop: the busy-gate read
        # and the _drive_run recheck interleave under the per-session lock.
        gate.set()
        raced = await service.prompt(session_id, "race text", "r2", delivery="queue")
        accepted = await _await_events(db_path, session_id, PY_PROMPT_ACCEPTED, 2)
        assert len(accepted) == 2
        successor = str(accepted[1]["run_id"])
        assert successor != run_a.run_id
        await _await_run_status(service, successor, frozenset(_TERMINAL))
        await asyncio.sleep(0.2)  # any illegal duplicate wake would surface here

        assert len(_event_data(db_path, session_id, PY_PROMPT_ACCEPTED)) == 2
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERED) == 1
        assert await service.inbox_list(session_id) == []
        assert "race text" in _user_texts(provider.requests[1])
        assert isinstance(raced, (RunRef, InboxRef))  # both answers are legal here


# ---------------------------------------------------------------------------
# §4-4: the interrupt face (active / idle / resume drive)
# ---------------------------------------------------------------------------


async def test_interrupt_active_idle_and_resume_drive(tmp_path: Path) -> None:
    """§4-4: interrupt lands the interrupted terminal over the cancel
    machinery (reason="user"); the idle session answers the no-op; resume=True
    promotes exactly the oldest pending steer, the queued prompt stays parked
    mid-resume, and the resumed run's own settle then chains the queued item
    (the §3-4 auto-wake)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate_a = threading.Event()
    gate_b = threading.Event()
    provider = _GatedProvider([(gate_a, _final_script().items), (gate_b, _final_script().items), _final_script().items])
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)
        idle_session = await _create_session(service)

        # The idle no-op: no run, no event, the honest False.
        idle_receipt = await service.interrupt(idle_session)
        assert isinstance(idle_receipt, InterruptReceipt)
        assert idle_receipt.interrupted is False
        assert idle_receipt.run_id is None and idle_receipt.status is None and idle_receipt.resumed_run_id is None
        assert _event_count(db_path, idle_session, PY_RUN_CANCEL_REQUESTED) == 0

        run_a = await service.prompt(session_id, "hold this", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)
        steer = await service.prompt(session_id, "resume me", "r2", delivery="steer")
        assert isinstance(steer, InboxRef)
        queued = await service.prompt(session_id, "stay parked", "r3", delivery="queue")
        assert isinstance(queued, InboxRef)

        # interrupt blocks until the run settles, so it rides a task; the
        # durable cancel_requested row is the deterministic handoff point.
        interrupt_task = asyncio.create_task(service.interrupt(session_id, resume=True))
        await _await_events(db_path, session_id, PY_RUN_CANCEL_REQUESTED, 1)
        gate_a.set()  # A's stream may now end; the cancelling fold rejects the
        # would-be succeeded terminal and finalizes interrupted instead.
        receipt = await interrupt_task
        assert isinstance(receipt, InterruptReceipt)
        assert receipt.interrupted is True
        assert receipt.run_id == run_a.run_id
        assert receipt.status is RunStatusValue.INTERRUPTED
        assert receipt.resumed_run_id is not None
        assert _run_status_value(db_path, run_a.run_id) == "interrupted"
        assert _event_data(db_path, session_id, PY_RUN_CANCEL_REQUESTED)[0]["reason"] == "user"

        # The resume drive: run B over the oldest steer, gated mid-flight.
        resumed_status = await _await_run_status(
            service, receipt.resumed_run_id, frozenset({RunStatusValue.ACCEPTED, RunStatusValue.RUNNING})
        )
        assert resumed_status.run_id == receipt.resumed_run_id
        await _await_parked(provider, 2)
        assert "resume me" in _user_texts(provider.requests[1])
        pending = await service.inbox_list(session_id)
        assert [row.request_id for row in pending] == ["r3"]  # §4-4: queued stays parked

        # B settles; ITS settle recheck then chains the queued item (§3-4).
        gate_b.set()
        await _await_run_status(service, receipt.resumed_run_id, frozenset({RunStatusValue.SUCCEEDED}))
        accepted = await _await_events(db_path, session_id, PY_PROMPT_ACCEPTED, 3)
        assert accepted[2]["request_id"] == "r3"
        chained = await _await_run_status(service, str(accepted[2]["run_id"]), frozenset(_TERMINAL))
        assert chained.status is RunStatusValue.SUCCEEDED
        assert "stay parked" in _user_texts(provider.requests[2])
        assert await service.inbox_list(session_id) == []


async def test_interrupt_without_resume_keeps_queue_parked(tmp_path: Path) -> None:
    """§4-4: an interrupted settle must NOT auto-continue the queue (the
    resume drive is the explicit control point); the queued item survives the
    interrupt untouched."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider = _GatedProvider([(gate, _final_script().items), _final_script().items])
    async with _run_app(db_path, provider) as app:
        service = app.service
        session_id = await _create_session(service)
        run_a = await service.prompt(session_id, "hold this", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)
        await service.prompt(session_id, "still here", "r2", delivery="queue")

        interrupt_task = asyncio.create_task(service.interrupt(session_id, resume=False))
        await _await_events(db_path, session_id, PY_RUN_CANCEL_REQUESTED, 1)
        gate.set()
        receipt = await interrupt_task
        assert receipt.interrupted is True
        assert receipt.resumed_run_id is None

        await asyncio.sleep(0.2)  # an illegal auto-wake would surface here
        assert _event_count(db_path, session_id, PY_PROMPT_ACCEPTED) == 1
        pending = await service.inbox_list(session_id)
        assert [row.request_id for row in pending] == ["r2"]
        assert pending[0].message.text == "still here"
        # The context exit is clean: no live run, the item stays durable-pending.


# ---------------------------------------------------------------------------
# §4-5: the inbox endpoints (list / cancel / update)
# ---------------------------------------------------------------------------


async def test_inbox_endpoints_list_cancel_update_and_their_no_ops(tmp_path: Path) -> None:
    """§4-5: list answers in enqueue order; a same-value update commits no
    event; a queue->steer flip rings the active run's doorbell (the run's
    next step boundary consumes the flipped item, §3-6); cancel withdraws
    once and the second cancel is the no-op; the update face fails closed
    on unknown items and on already-delivered ones (ConflictError), and
    cancel on a delivered item is the honest no-op."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider = _GatedProvider([(gate, _tool_script().items), _final_script().items])
    read = _ReadAdapter()
    async with _run_app(db_path, provider, adapters=[read]) as app:
        service = app.service
        session_id = await _create_session(service)
        run_a = await service.prompt(session_id, "hold", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider)
        first = await service.prompt(session_id, "first item", "r2", delivery="queue")
        second = await service.prompt(session_id, "second item", "r3", delivery="queue")
        assert isinstance(first, InboxRef)
        assert isinstance(second, InboxRef)

        # update: a real flip commits delivery_changed and rings the active
        # run's doorbell; a same-value flip is the documented no-op.
        flipped = await service.inbox_update(session_id, first.inbox_id, "steer")
        assert flipped.delivery == "steer"
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERY_CHANGED) == 1
        again = await service.inbox_update(session_id, first.inbox_id, "steer")
        assert again.delivery == "steer"
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERY_CHANGED) == 1

        # the read face keeps the enqueued order across the re-arm.
        assert [row.inbox_id for row in await service.inbox_list(session_id)] == [first.inbox_id, second.inbox_id]

        # cancel: withdraw once, then the no-op; the update face on the gone
        # item is the §3-6 conflict (fail closed).
        assert await service.inbox_cancel(session_id, second.inbox_id) is True
        assert _event_count(db_path, session_id, PY_INBOX_CANCELLED) == 1
        assert await service.inbox_cancel(session_id, second.inbox_id) is False
        assert _event_count(db_path, session_id, PY_INBOX_CANCELLED) == 1
        with pytest.raises(ConflictError):
            await service.inbox_update(session_id, second.inbox_id, "queue")

        # unknown session / unknown item: the honest store faces.
        with pytest.raises(NotFoundError):
            await service.inbox_list("ses_does_not_exist")
        with pytest.raises(ConflictError):
            await service.inbox_update(session_id, "inbox_does_not_exist", "steer")

        # validation fail-closed: unknown delivery values never reach a lock.
        with pytest.raises(Exception, match="delivery"):
            await service.inbox_update(session_id, first.inbox_id, "urgent")  # type: ignore[arg-type]
        with pytest.raises(Exception, match="delivery"):
            await service.prompt(session_id, "bad", "r9", delivery="urgent")  # type: ignore[call-overload]

        # The doorbell answers: A's second step boundary consumes the flipped
        # item -- one delivered, its text in step 2's context, run A settles.
        gate.set()
        final = await _await_run_status(service, run_a.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final.status is RunStatusValue.SUCCEEDED
        assert read.starts == ["s1"]  # the tool step executed
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERED) == 1
        assert "first item" in _user_texts(provider.requests[1])
        assert await service.inbox_list(session_id) == []

        # §3-6 fail-closed legs on the delivered item: update is the
        # conflict, cancel the honest no-op, and neither commits anything.
        with pytest.raises(ConflictError):
            await service.inbox_update(session_id, first.inbox_id, "queue")
        assert await service.inbox_cancel(session_id, first.inbox_id) is False
        assert _event_count(db_path, session_id, PY_INBOX_DELIVERY_CHANGED) == 1
        assert _event_count(db_path, session_id, PY_INBOX_CANCELLED) == 1
        # nothing was pending at the settle: no successor run woke (§3-4).
        assert _event_count(db_path, session_id, PY_PROMPT_ACCEPTED) == 1


# ---------------------------------------------------------------------------
# §4-6: durability / recovery (the restart scan + the post-scan wake)
# ---------------------------------------------------------------------------


async def test_pending_inbox_survives_restart_and_wakes(tmp_path: Path) -> None:
    """§4-6 (R4): a queued item parked before a shutdown survives the close
    (the run lands interrupted; the item stays pending), and the next App's
    post-recovery-scan wake auto-starts it."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    gate = threading.Event()
    provider_one = _GatedProvider([(gate, _final_script().items)])
    async with _run_app(db_path, provider_one, close_timeout_s=0.2) as app:
        service = app.service
        session_id = await _create_session(service)
        run_a = await service.prompt(session_id, "doomed", "r1")
        assert isinstance(run_a, RunRef)
        await _await_parked(provider_one)
        await service.prompt(session_id, "survivor", "r2", delivery="queue")
        # The close cancels the gated run (it cannot observe the intent
        # mid-stream); the queued item is untouched by the shutdown.

    assert _run_status_value(db_path, run_a.run_id) == "interrupted"
    row = _fetch_scalar(db_path, "SELECT COUNT(*) FROM inbox WHERE session_id = ?", (session_id,))
    assert row is not None and int(row[0]) == 1  # §3-10: pending survives the restart

    provider_two = _GatedProvider()
    async with _run_app(db_path, provider_two, mode="readwrite") as app:
        service_two = app.service
        await app.recovery_gate.wait()  # the scan itself: nothing left to close
        accepted = await _await_events(db_path, session_id, PY_PROMPT_ACCEPTED, 2)
        assert accepted[1]["request_id"] == "r2"
        woken_run = str(accepted[1]["run_id"])
        final = await _await_run_status(service_two, woken_run, frozenset(_TERMINAL))
        assert final.status is RunStatusValue.SUCCEEDED
        assert "survivor" in _user_texts(provider_two.requests[0])
        assert await service_two.inbox_list(session_id) == []


# ---------------------------------------------------------------------------
# §4-7: the SDK / wire leg (one real server; the census itself lives in
# test_sdk_client.py, which owns the route-count pin)
# ---------------------------------------------------------------------------


def _boot_server(db_path: Path, provider: _GatedProvider) -> tuple[uvicorn.Server, threading.Thread, str, App]:
    """Boot one programmatic uvicorn.Server on a worker thread (the 054/056 form)."""
    _require_success(_upgrade_head(db_path))
    app = _run_app(db_path, provider)
    api = create_app(app, token=_TOKEN)
    config = _server_config(api, port=0)  # loopback host, ephemeral port
    assert config.workers == 1 and config.reload is False
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="opencode-inbox-test-server")
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
    return server, thread, f"http://127.0.0.1:{int(address[1])}", app


def test_sdk_inbox_and_interrupt_roundtrip_over_the_wire(tmp_path: Path) -> None:
    """§4-7: the four SDK methods + the delivery passthrough over the real
    wire: prompt parks (InboxRef), list orders, update flips, cancel
    no-ops, interrupt blocks to the terminal and the resume drive answers
    ``resumed_run_id`` while the queued prompt stays parked."""
    db_path = tmp_path / "app.db"
    gate_a = threading.Event()
    gate_b = threading.Event()
    provider = _GatedProvider([(gate_a, _final_script().items), (gate_b, _final_script().items)])
    server, thread, base, _app = _boot_server(db_path, provider)
    try:
        client = OpencodeClient(base_url=base, token=_TOKEN)
        session_id = client.create_session(_SNAPSHOT_BODY).session_id

        run_a = client.prompt(session_id, "hold this", "wr1")
        assert isinstance(run_a, RunRef)
        _await_parked_sync(provider)  # run A is mid-step: admissions park, never boundary-consume
        steer = client.prompt(session_id, "steer body", "wr2", delivery="steer")
        assert isinstance(steer, ClientInboxRef)
        assert (steer.session_id, steer.kind, steer.delivery, steer.request_id) == (
            session_id,
            "user",
            "steer",
            "wr2",
        )
        queued = client.prompt(session_id, "queue body", "wr3", delivery="queue")
        assert isinstance(queued, ClientInboxRef)

        items = client.inbox_list(session_id)
        assert [item.inbox_id for item in items] == [steer.inbox_id, queued.inbox_id]
        assert [item.request_id for item in items] == ["wr2", "wr3"]
        assert items[0].message.model_dump(mode="json")["text"] == "steer body"

        # update: the doorbell flip while the run is active; same-value via wire too.
        flipped = client.inbox_update(session_id, queued.inbox_id, "steer")
        assert isinstance(flipped, PendingInboxItem)
        assert flipped.delivery == "steer"
        assert client.inbox_update(session_id, queued.inbox_id, "steer").delivery == "steer"

        # cancel: True once, then the honest no-op.
        assert client.inbox_cancel(session_id, queued.inbox_id) is True
        assert client.inbox_cancel(session_id, queued.inbox_id) is False
        assert [item.inbox_id for item in client.inbox_list(session_id)] == [steer.inbox_id]

        # interrupt(resume=True): blocks to the terminal on its own thread
        # while the test drives the gate off the durable cancelling state.
        receipts: list[ClientInterruptReceipt] = []

        def _interrupt() -> None:
            receipts.append(client.interrupt(session_id, resume=True))

        interrupt_thread = threading.Thread(target=_interrupt, name="opencode-inbox-test-interrupt")
        interrupt_thread.start()
        deadline = time.monotonic() + 5.0
        while client.run_status(run_a.run_id).status is not RunStatusValue.CANCELLING:
            assert interrupt_thread.is_alive(), "the interrupt call returned before the cancelling state"
            assert time.monotonic() < deadline, "run A never reached cancelling"
            time.sleep(0.02)
        gate_a.set()
        interrupt_thread.join(timeout=15)
        assert not interrupt_thread.is_alive()
        receipt = receipts[0]
        assert receipt.interrupted is True
        assert receipt.run_id == run_a.run_id
        assert receipt.status == "interrupted"
        assert receipt.resumed_run_id is not None

        resumed = client.run_status(receipt.resumed_run_id)
        assert resumed.status in {RunStatusValue.ACCEPTED, RunStatusValue.RUNNING}
        assert client.inbox_list(session_id) == []  # the only pending item was the consumed steer

        gate_b.set()
        deadline = time.monotonic() + 10.0
        while client.run_status(receipt.resumed_run_id).status not in _TERMINAL:
            assert time.monotonic() < deadline, "the resumed run never settled"
            time.sleep(0.02)
        assert client.run_status(receipt.resumed_run_id).status is RunStatusValue.SUCCEEDED

        # idle no-op over the wire.
        idle_receipt = client.interrupt(session_id)
        assert idle_receipt.interrupted is False
        assert idle_receipt.run_id is None and idle_receipt.resumed_run_id is None
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "the server did not shut down"
