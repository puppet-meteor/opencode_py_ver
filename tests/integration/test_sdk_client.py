"""P2-S5 / M5-C integration tests: the Python client SDK over the real wire.

The SDK acceptance face (PHASE2-MODULES §7 L103/L110/L112, p2-slices §2.2
L103): the client creates a session, prompts, and reads the run's terminal
state; disconnect/reconnect (SSE interrupted -> Last-Event-ID cursor ->
missed events with zero overlap and evt_ dedup), duplicate request_id, error
codes, unauthenticated requests, approval, and shutdown all have integration
tests — with a single-worker server boot as the positive case. The L110 tail
sentence (a second App refusing the writer lock, reload duplication, the
data-directory lock) is the 054 server-face coverage and carries over
unchanged — the SDK adds no new semantics there.

④ disclosures: the carrier is a real ``uvicorn.Server`` on a worker thread
(the 054/056 smoke form, boot helpers replicated here per the slice's
no-import rule) plus the real SDK client — no mock urllib anywhere, no
TestClient/httpx (the 054 red line). One module-scoped server serves the
per-session tests (each test creates fresh sessions; sessions are isolated
by construction); the scripted provider is keyed by the prompt's user text
so a shared server stays test-order independent — every test that needs a
special flow registers its script under a text unique to that test, and the
default is a finishing script. Two carrier deviations are deliberate and
pinned by the rulings: the OpenAPI structure test pulls ``/openapi.json``
directly over the same token'd wire (ruling f allows "via SDK or direct",
and the runtime endpoint is the single source — no static spec ships); the
SSE parser faces that a healthy wire cannot produce (the terminal
``event: error`` frame, a duplicate evt_ id, unknown fields) are fed to the
decode layer directly — the 056 suite already owns the server-side trigger
faces, and the SDK side is exactly this decode layer. The approval test
reads the durable SSE stream for ``py.permission.requested`` (the
SDK-native discovery face) and retries the reply across the legal 404
window (the 054 ``_await_registered_reply`` race disclosure). The parser
tests build real DurableEvent envelopes through the contracts models.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn

import opencode.sdk
from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.events import (
    PY_PERMISSION_REQUESTED,
    PY_PROMPT_ACCEPTED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_SESSION_CREATED,
    DurableEvent,
    RunStartedData,
    default_event_registry,
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
from opencode.contracts.messages import (
    AssistantMessage,
    IdleMessage,
    JsonObject,
    ToolTextContent,
    UserMessage,
)
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.httpapi import create_app
from opencode.httpapi.serving import _server_config
from opencode.sdk import (
    DEFAULT_BASE_URL,
    OpencodeApiError,
    OpencodeClient,
    OpencodeClientError,
    OpencodeConnectionError,
    OpencodeStreamError,
)
from opencode.sdk.sse import stream_durable_events
from opencode.session.service import DecisionReceipt, RunRef, RunStatus, RunStatusValue, SessionRef
from opencode.tools import ToolContext
from opencode.tools.registry import ToolAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_SNAPSHOT_BODY = _SNAPSHOT.model_dump(mode="json")
_MODEL = ModelRef(provider="maas", model="test-model")
_TOKEN = "test-token-1234"
_TERMINAL = {"succeeded", "failed", "interrupted"}

#: A loopback client that never routes through a system proxy (the 054 form).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: The stream read timeout, decoupled from the server's 15 s keepalive
#: interval (the 056 note: 60 > 15, so a healthy stream never hits it).
_STREAM_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# migration helper (the test_httpapi_server.py form)
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


# ---------------------------------------------------------------------------
# scripted provider, keyed by the prompt's user text (the shared-server form)
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


def _ask_script(tag: str, *, path: str = "out.txt", name: str = "tst.write", call_id: str = "call_1") -> _Script:
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


def _user_text(request: GenerationRequest) -> str:
    """The last user text of a generation request — the script queue's key."""
    for message in reversed(request.messages):
        if message.role == "user":
            for part in message.content:
                if isinstance(part, LLMTextPart):
                    return part.text
    return ""


class _KeyedProvider:
    """Fake Provider: one scripted stream per request, keyed by the prompt's user text.

    Tests run sequentially against one shared server, so keying by user text
    keeps them order-independent: a test that needs a special flow registers
    its script under a text unique to that test before prompting; everything
    else gets the default finishing script. An approval flow registers two
    scripts under its key (the tool-call turn, then the post-tool turn).
    """

    def __init__(self) -> None:
        self._scripts: dict[str, list[list[ProviderChunk]]] = {}
        self.requests: list[GenerationRequest] = []

    def register(self, key: str, script: _Script) -> None:
        self._scripts.setdefault(key, []).append(list(script.items))

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        queue = self._scripts.get(_user_text(request))
        items = queue.pop(0) if queue else list(_final_script().items)
        for chunk in items:
            yield chunk

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# the approval tool (the test_httpapi_server.py form)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# app harness (the test_httpapi_server.py form)
# ---------------------------------------------------------------------------


def _settings() -> Any:
    from opencode.session.context import RuntimeSettings

    return RuntimeSettings(
        model=_MODEL,
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


def _run_app(db_path: Path, provider: _KeyedProvider, adapters: Sequence[ToolAdapter]) -> App:
    """The full composition: provider + the approval tool wired into the service."""
    return App(
        db_path,
        mode="create",
        working_dir="C:/work",
        provider=provider,
        tools=adapters,
        settings=_settings(),
    )


@dataclass
class _Running:
    """One live module server: the base URL plus the composition it serves."""

    base: str
    db_path: Path
    app: App
    provider: _KeyedProvider
    write: _AskAdapter
    server: uvicorn.Server
    thread: threading.Thread


def _boot_server(db_path: Path, provider: _KeyedProvider) -> _Running:
    """Boot one programmatic uvicorn.Server on a worker thread (the 054/056 smoke form)."""
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    app = _run_app(db_path, provider, [write])
    api = create_app(app, token=_TOKEN)
    config = _server_config(api, port=0)  # loopback host, ephemeral port
    assert config.workers == 1 and config.reload is False  # L110: the single-worker positive case
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="opencode-sdk-test-server")
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
        write=write,
        server=server,
        thread=thread,
    )


def _stop_server(running: _Running) -> None:
    """Stop the server thread; the ASGI lifespan closes the App on the way down."""
    running.server.should_exit = True
    running.thread.join(timeout=15)
    assert not running.thread.is_alive(), "the server did not shut down"
    assert running.app.service.closed is True


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Running]:
    root = tmp_path_factory.mktemp("sdk_module_server")
    handle = _boot_server(root / "app.db", _KeyedProvider())
    try:
        yield handle
    finally:
        _stop_server(handle)


@pytest.fixture(scope="module")
def client(running: _Running) -> OpencodeClient:
    return OpencodeClient(base_url=running.base, token=_TOKEN)


# ---------------------------------------------------------------------------
# SDK-side helpers
# ---------------------------------------------------------------------------


def _create(client: OpencodeClient) -> SessionRef:
    return client.create_session(_SNAPSHOT_BODY)


def _await_status(client: OpencodeClient, run_id: str, wanted: set[str], *, timeout_s: float = 10.0) -> RunStatus:
    """Poll run_status over the SDK until the run reaches ``wanted`` (bounded)."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = client.run_status(run_id)
        if status.status in wanted:
            return status
        assert time.monotonic() < deadline, f"run {run_id} did not reach {wanted} (stuck at {status.status})"
        time.sleep(0.02)


def _await_reply(client: OpencodeClient, permission_id: str, reply: str, *, timeout_s: float = 5.0) -> DecisionReceipt:
    """Reply once the coordinator holds the permission pending (the 404 window is legal).

    The 054 race disclosure carries over: the durable py.permission.requested
    commit becomes reader-visible (the SSE stream delivers it) strictly before
    the Invoker's in-process registration — a reply in that window
    legitimately answers 404, so poll-retry instead of assuming registration.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return client.reply_permission(permission_id, reply)
        except OpencodeApiError as error:
            if error.status_code != 404:
                raise
            assert time.monotonic() < deadline, f"permission {permission_id} was never registered"
            time.sleep(0.02)


def _collect(
    stream: Iterator[DurableEvent[Any]], wanted: Callable[[DurableEvent[Any]], bool]
) -> list[DurableEvent[Any]]:
    """Read the stream until one event satisfies ``wanted``; everything read is returned."""
    events: list[DurableEvent[Any]] = []
    for event in stream:
        events.append(event)
        if wanted(event):
            return events
    raise AssertionError(f"the stream ended before the wanted event; got {len(events)} events")


# ---------------------------------------------------------------------------
# module faces: exports, defaults, the dependency red line (rulings a/b)
# ---------------------------------------------------------------------------


def test_sdk_exports_and_defaults_are_pinned() -> None:
    assert sorted(opencode.sdk.__all__) == sorted(
        [
            "DEFAULT_BASE_URL",
            "DEFAULT_TIMEOUT_S",
            "OpencodeApiError",
            "OpencodeClient",
            "OpencodeClientError",
            "OpencodeConnectionError",
            "OpencodeStreamError",
        ]
    )
    assert DEFAULT_BASE_URL == "http://127.0.0.1:4096"
    assert isinstance(OpencodeClient(token="t"), OpencodeClient)
    with pytest.raises(TypeError):
        OpencodeClient()  # type: ignore[call-arg] # no anonymous client: the token is required
    for bad in (0, -1.0, True):
        with pytest.raises(ValueError):
            OpencodeClient(token="t", timeout_s=bad)


def test_the_sdk_never_imports_the_server_face() -> None:
    """Ruling b red line: a pure client on the core dependency surface (fresh interpreter)."""
    code = (
        "import sys, opencode.sdk; "
        "leaked = [m for m in sys.modules if m in ('fastapi', 'uvicorn') or m.startswith('opencode.httpapi')]; "
        "assert not leaked, leaked"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# ① create session -> prompt -> terminal state (+ the message page)
# ---------------------------------------------------------------------------


def test_create_prompt_and_terminal_state_over_the_real_wire(client: OpencodeClient, running: _Running) -> None:
    ref = _create(client)
    assert isinstance(ref, SessionRef)
    assert ref.session_id.startswith("ses_")
    provider_calls = len(running.provider.requests)
    run = client.prompt(ref.session_id, "sdk-happy-path", "req_sdk_1")
    assert isinstance(run, RunRef)
    assert (run.session_id, run.request_id) == (ref.session_id, "req_sdk_1")
    assert run.run_id.startswith("run_")
    final = _await_status(client, run.run_id, _TERMINAL)
    assert final.status is RunStatusValue.SUCCEEDED
    assert final.error is None
    assert len(running.provider.requests) - provider_calls == 1  # exactly one generation served the run
    # the projected conversation: the user prompt, the assistant answer, the run-end idle row
    page = client.list_messages(ref.session_id)
    assert page.has_more is False
    assert page.next_after_message_id == page.items[-1].id
    assert [item.type for item in page.items] == ["user", "assistant", "idle"]
    user, assistant, idle = page.items
    assert isinstance(user, UserMessage)
    assert isinstance(assistant, AssistantMessage)
    assert isinstance(idle, IdleMessage)
    assert user.text == "sdk-happy-path"
    assert assistant.model is not None
    assert idle.run_id == run.run_id
    assert idle.outcome == "succeeded"


def test_message_pages_cursor_paginate_over_the_real_wire(client: OpencodeClient) -> None:
    ref = _create(client)
    run = client.prompt(ref.session_id, "sdk-pages", "req_sdk_pages")
    _await_status(client, run.run_id, _TERMINAL)
    first = client.list_messages(ref.session_id, limit=1)
    assert len(first.items) == 1
    assert first.has_more is True
    second = client.list_messages(ref.session_id, limit=1, after_message_id=first.next_after_message_id)
    assert second.has_more is True  # three projected rows: two pages of one leave one more
    third = client.list_messages(ref.session_id, limit=1, after_message_id=second.next_after_message_id)
    assert third.has_more is False
    ids = [item.id for item in first.items + second.items + third.items]
    assert len(ids) == len(set(ids))  # zero overlap across cursor pages
    whole = client.list_messages(ref.session_id)
    assert ids == [item.id for item in whole.items]  # the pages cover exactly the full page


# ---------------------------------------------------------------------------
# ③ duplicate request_id, both branches (§7 L107)
# ---------------------------------------------------------------------------


def test_duplicate_request_id_with_identical_input_returns_the_original_runref(
    client: OpencodeClient, running: _Running
) -> None:
    ref = _create(client)
    original = client.prompt(ref.session_id, "sdk-dedupe", "req_sdk_dedupe")
    _await_status(client, original.run_id, _TERMINAL)
    before = len(running.provider.requests)
    retry = client.prompt(ref.session_id, "sdk-dedupe", "req_sdk_dedupe")
    assert retry == original  # the original RunRef, field for field
    assert len(running.provider.requests) == before  # the dedupe answered before any second scheduling


def test_duplicate_request_id_with_different_input_answers_idempotency_conflict(client: OpencodeClient) -> None:
    ref = _create(client)
    run = client.prompt(ref.session_id, "sdk-conflict-a", "req_sdk_conflict")
    _await_status(client, run.run_id, _TERMINAL)
    with pytest.raises(OpencodeApiError) as excinfo:
        client.prompt(ref.session_id, "sdk-conflict-b", "req_sdk_conflict")
    assert excinfo.value.status_code == 409
    assert excinfo.value.core is not None
    assert excinfo.value.core.code is CoreErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.core.retryable is False


# ---------------------------------------------------------------------------
# ④ error codes: 404 / 400 / 422 over the SDK error face (§8 wire bodies)
# ---------------------------------------------------------------------------


def test_unknown_ids_answer_404_core_errors(client: OpencodeClient) -> None:
    ref = _create(client)
    for probe in (
        lambda: client.run_status("run_00000missing"),
        lambda: client.prompt("ses_missing", "hi", "req_sdk_404"),
        lambda: client.list_messages("ses_missing"),
        lambda: client.cancel_run("run_00000missing"),
        lambda: client.reply_permission("prm_missing", "once"),
    ):
        with pytest.raises(OpencodeApiError) as excinfo:
            probe()
        assert excinfo.value.status_code == 404
        assert excinfo.value.core is not None
        assert excinfo.value.core.code is CoreErrorCode.NOT_FOUND
        assert excinfo.value.core.retryable is False
    assert ref.session_id  # the fresh session is only the carrier; nothing above touched it


def test_message_limit_domain_answers_422_validation_errors(client: OpencodeClient) -> None:
    ref = _create(client)
    for limit in (0, 201, -5):
        with pytest.raises(OpencodeApiError) as excinfo:
            client.list_messages(ref.session_id, limit=limit)
        assert excinfo.value.status_code == 422
        assert excinfo.value.core is not None
        assert excinfo.value.core.code is CoreErrorCode.VALIDATION_ERROR
        assert excinfo.value.core.retryable is False


def test_malformed_and_foreign_stream_cursors_answer_400(client: OpencodeClient) -> None:
    ref = _create(client)
    other = _create(client)
    for after in ("garbage", f"{other.session_id}:1", f"{ref.session_id}:1000000000"):
        stream = client.stream_events(ref.session_id, after=after)
        with pytest.raises(OpencodeApiError) as excinfo:
            next(stream)  # the connection is issued lazily: the first read carries the refusal
        assert excinfo.value.status_code == 400
        assert excinfo.value.core is not None
        assert excinfo.value.core.code is CoreErrorCode.INVALID_CURSOR
        assert excinfo.value.core.retryable is False


# ---------------------------------------------------------------------------
# ⑤ unauthenticated requests (401 on both the command and the stream face)
# ---------------------------------------------------------------------------


def _retry_on_connection_error[T](fn: Callable[[], T], *, attempts: int = 3, backoff_s: float = 0.25) -> T:
    """Bounded re-send on the SDK connection face only (T-C1-061 face 4).

    A Windows loopback abort (WinError 10053) surfaces as
    ``OpencodeConnectionError`` (errors.py L65, an ``OpencodeClientError``
    child; raised at client.py L250/L276); ``OpencodeApiError`` (errors.py
    L38) is a *sister* class, not a child, so an HTTP answer -- 401 included
    -- is never caught and never retried: it propagates on the first attempt.
    ``OpencodeStreamError`` (errors.py L53) only wraps a terminal SSE
    ``event: error`` frame after a 200, so it cannot occur on a 401 probe
    and is not caught either. A persistent connection failure re-raises once
    the budget is exhausted (fail-visible). Guard rider (T-B-070
    recommendation 1 / T-C1-062): ``attempts < 1`` is the only input that
    could fall through the loop to the unreachable tail line, so it is
    rejected up front -- the assertion face below stays frozen.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OpencodeConnectionError:
            if attempt == attempts:
                raise
            time.sleep(backoff_s * attempt)
    raise AssertionError("unreachable: the retry loop always returns or re-raises")


def test_unauthenticated_requests_answer_401_on_both_faces(client: OpencodeClient, running: _Running) -> None:
    wrong = OpencodeClient(base_url=running.base, token="wrong-token")
    with pytest.raises(OpencodeApiError) as excinfo:
        _retry_on_connection_error(lambda: wrong.create_session(_SNAPSHOT_BODY))
    assert excinfo.value.status_code == 401
    assert excinfo.value.core is not None
    assert excinfo.value.core.code is CoreErrorCode.PERMISSION_DENIED
    assert excinfo.value.core.retryable is False
    ref = _create(client)  # a real session for the stream probe
    # a fresh stream per attempt: a broken iterator must not be reused (the
    # connection is issued lazily on the first read, so a re-send is whole)
    with pytest.raises(OpencodeApiError) as excinfo:
        _retry_on_connection_error(lambda: next(wrong.stream_events(ref.session_id)))
    assert excinfo.value.status_code == 401
    assert excinfo.value.core is not None
    assert excinfo.value.core.code is CoreErrorCode.PERMISSION_DENIED


def test_connection_retry_helper_recovers_exhausts_and_keeps_api_errors_first_attempt() -> None:
    """Injectability proof (T-C1-061 face 4): the retry is connection-face-only.

    N-1 connection errors then success recover; a persistent connection
    failure exhausts the budget and re-raises; an ``OpencodeApiError`` (any
    HTTP answer) is never retried -- call count 1 -- so the deterministic
    401 face stays fail-visible.
    """
    calls: list[int] = []

    def flaky() -> int:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise OpencodeConnectionError("POST /session never answered")
        return 42

    assert _retry_on_connection_error(flaky, attempts=3, backoff_s=0.0) == 42
    assert len(calls) == 3

    calls.clear()

    def persistent() -> int:
        calls.append(len(calls) + 1)
        raise OpencodeConnectionError("POST /session never answered")

    with pytest.raises(OpencodeConnectionError):
        _retry_on_connection_error(persistent, attempts=3, backoff_s=0.0)
    assert len(calls) == 3

    calls.clear()

    def refused() -> int:
        calls.append(len(calls) + 1)
        raise OpencodeApiError(401, None)

    with pytest.raises(OpencodeApiError):
        _retry_on_connection_error(refused, attempts=3, backoff_s=0.0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# ⑥ approval: prompt -> permission pending -> reply -> terminal (all via the SDK)
# ---------------------------------------------------------------------------


def test_approval_flow_over_the_stream_reply_and_finish(client: OpencodeClient, running: _Running) -> None:
    ref = _create(client)
    executions = len(running.write.starts)
    running.provider.register("sdk-approval", _ask_script("t1"))
    running.provider.register("sdk-approval", _final_script())
    run = client.prompt(ref.session_id, "sdk-approval", "req_sdk_approval")
    stream = client.stream_events(ref.session_id, timeout_s=_STREAM_TIMEOUT_S)
    try:
        events = _collect(stream, lambda event: event.type == PY_PERMISSION_REQUESTED)
        requested = events[-1]
        assert str(requested.data.permission_id).startswith("prm_")
        receipt = _await_reply(client, str(requested.data.permission_id), "once")
        assert receipt == DecisionReceipt(
            permission_id=str(requested.data.permission_id),
            decision="allow",
            execution_id=str(requested.data.execution_id),
        )
        finished = _collect(stream, lambda event: event.type == PY_RUN_FINISHED and event.data.run_id == run.run_id)
        assert finished[-1].data.outcome == "succeeded"
    finally:
        stream.close()
    assert running.write.starts[executions:] == ["t1"]  # the allowed tool executed exactly once


# ---------------------------------------------------------------------------
# cancel: the approval park resolves cancelled (the 054 form, SDK face)
# ---------------------------------------------------------------------------


def test_cancel_run_interrupts_a_parked_approval(client: OpencodeClient, running: _Running) -> None:
    ref = _create(client)
    executions = len(running.write.starts)
    running.provider.register("sdk-cancel", _ask_script("t1"))
    run = client.prompt(ref.session_id, "sdk-cancel", "req_sdk_cancel")
    stream = client.stream_events(ref.session_id, timeout_s=_STREAM_TIMEOUT_S)
    try:
        _collect(stream, lambda event: event.type == PY_PERMISSION_REQUESTED)  # the run parks on the approval
        cancelled = client.cancel_run(run.run_id)
        assert cancelled.run_id == run.run_id
        assert cancelled.status in {"cancelling", "interrupted"}
        final = _await_status(client, run.run_id, _TERMINAL)
        assert final.status is RunStatusValue.INTERRUPTED
        late = client.cancel_run(run.run_id)  # a late cancel returns the original terminal
        assert late.status is final.status
        assert late.run_id == final.run_id
    finally:
        stream.close()
    assert running.write.starts[executions:] == []  # the approval folded cancelled; the tool never ran


# ---------------------------------------------------------------------------
# ② disconnect / reconnect: missed events, zero overlap, dedup (L105 client)
# ---------------------------------------------------------------------------


def test_stream_disconnect_and_cursor_reconnect_receives_missed_events_without_overlap(client: OpencodeClient) -> None:
    ref = _create(client)
    first = client.prompt(ref.session_id, "sdk-reconnect-1", "req_sdk_recon_1")
    _await_status(client, first.run_id, _TERMINAL)
    stream = client.stream_events(ref.session_id, timeout_s=_STREAM_TIMEOUT_S)
    before: list[DurableEvent[Any]] = []
    for event in stream:
        before.append(event)
        if len(before) == 2:  # session.created + prompt.accepted: a mid-catch-up disconnect
            break
    assert [event.type for event in before] == [PY_SESSION_CREATED, PY_PROMPT_ACCEPTED]
    assert [event.seq for event in before] == [1, 2]
    stream.close()  # the socket dies; the server cancels the follow on disconnect
    assert client.resume_cursor(ref.session_id) == f"{ref.session_id}:{before[-1].seq}"
    # while offline, a second run commits events the first stream never saw
    second = client.prompt(ref.session_id, "sdk-reconnect-2", "req_sdk_recon_2")
    _await_status(client, second.run_id, _TERMINAL)
    resumed = client.stream_events(
        ref.session_id, after=client.resume_cursor(ref.session_id), timeout_s=_STREAM_TIMEOUT_S
    )
    after_events: list[DurableEvent[Any]] = []
    try:
        for event in resumed:
            after_events.append(event)
            if event.type == PY_RUN_FINISHED and event.data.run_id == second.run_id:
                break
    finally:
        resumed.close()
    seqs = [event.seq for event in after_events]
    assert seqs[0] == 3  # strictly after the cursor: nothing replayed from before it
    assert seqs == list(range(seqs[0], seqs[-1] + 1))  # contiguous: nothing missed in the catch-up
    # zero overlap across the two connections + the evt_ dedup within one stream
    all_ids = [event.id for event in before + after_events]
    assert len(all_ids) == len(set(all_ids))
    assert client.resume_cursor(ref.session_id) == f"{ref.session_id}:{after_events[-1].seq}"


# ---------------------------------------------------------------------------
# the SSE decode layer: faces a healthy wire cannot produce (ruling e)
# ---------------------------------------------------------------------------


def _probe_event(seq: int, event_id: str) -> str:
    """One committed ``py.run.started`` envelope as the compact data-line JSON."""
    envelope = DurableEvent[RunStartedData](
        id=event_id,
        type=PY_RUN_STARTED,
        version=1,
        session_id="ses_probe",
        created_at=datetime.now(UTC),
        data=RunStartedData(run_id="run_probe"),
        metadata={"run_id": "run_probe"},
        seq=seq,
    )
    return json.dumps(envelope.model_dump(mode="json"), separators=(",", ":"))


def _frame_lines(*lines: str) -> list[bytes]:
    return [line.encode("utf-8") + b"\n" for line in lines] + [b"\n"]


def test_the_sse_parser_yields_typed_events_and_skips_comments() -> None:
    cursors: list[str] = []
    comments = [b": keepalive\n", b"\n", b": synced 5\n", b"\n"]
    frame_one = _frame_lines("id: ses_probe:1", f"event: {PY_RUN_STARTED}", f"data: {_probe_event(1, 'evt_probe_1')}")
    frame_two = _frame_lines("id:ses_probe:2", f"event:{PY_RUN_STARTED}", f"data:{_probe_event(2, 'evt_probe_2')}")
    events = list(
        stream_durable_events(
            [*comments, *frame_one, *frame_two],
            registry=default_event_registry(),
            on_cursor=cursors.append,
        )
    )
    assert [event.id for event in events] == ["evt_probe_1", "evt_probe_2"]  # comments skipped
    assert [event.seq for event in events] == [1, 2]
    assert events[0].data.run_id == "run_probe"  # typed: the registry decoded the data model
    assert cursors == ["ses_probe:1", "ses_probe:2"]  # with and without the optional space


def test_the_sse_parser_deduplicates_ids_and_advances_the_cursor() -> None:
    cursors: list[str] = []
    block = _frame_lines("id: ses_probe:2", f"event: {PY_RUN_STARTED}", f"data: {_probe_event(2, 'evt_probe_2')}")
    events = list(stream_durable_events(block * 2, registry=default_event_registry(), on_cursor=cursors.append))
    assert [event.id for event in events] == ["evt_probe_2"]  # one yield: the re-sent frame skipped
    assert cursors == ["ses_probe:2", "ses_probe:2"]  # the duplicate still advanced the wire cursor


def test_the_terminal_error_frame_raises_the_stream_error() -> None:
    core = json.dumps({"code": "SlowConsumer", "message": "paced", "retryable": False, "details": {}})
    with pytest.raises(OpencodeStreamError) as excinfo:
        list(
            stream_durable_events(
                _frame_lines("event: error", f"data: {core}"),
                registry=default_event_registry(),
                on_cursor=lambda _cursor: None,
            )
        )
    assert excinfo.value.core.code is CoreErrorCode.SLOW_CONSUMER
    assert excinfo.value.core.retryable is False


def test_malformed_sse_lines_and_frames_fail_closed() -> None:
    for bad in (
        [b"retry: 10000\n", b"\n"],  # an unknown field: the wire emits id/event/data/comments only
        [b"garbage line\n", b"\n"],  # no field colon
        [b"id: ses_probe:1\n", b"\n"],  # an id without a data line
        [b"data: {}\n", b"\n"],  # a data line without an id
    ):
        with pytest.raises(OpencodeClientError):
            list(stream_durable_events(bad, registry=default_event_registry(), on_cursor=lambda _cursor: None))


# ---------------------------------------------------------------------------
# f: the runtime OpenAPI document (direct pull per the ruling; 054 owns the 401 face)
# ---------------------------------------------------------------------------


def test_the_runtime_openapi_document_answers_the_seven_paths(running: _Running) -> None:
    request = urllib.request.Request(
        running.base + "/openapi.json", headers={"Authorization": f"Bearer {_TOKEN}"}, method="GET"
    )
    with _OPENER.open(request, timeout=30) as response:
        assert int(response.status) == 200
        document: Any = json.loads(response.read())
    assert isinstance(document, dict)
    paths = document.get("paths")
    assert isinstance(paths, dict)
    assert set(paths) == {
        "/session",
        "/session/{session_id}/prompt",
        "/session/{session_id}/messages",
        "/session/{session_id}/events",
        "/run/{run_id}/cancel",
        "/run/{run_id}/status",
        "/permission/{permission_id}/reply",
    }
    for path, method in (
        ("/session", "post"),
        ("/session/{session_id}/prompt", "post"),
        ("/session/{session_id}/messages", "get"),
        ("/session/{session_id}/events", "get"),
        ("/run/{run_id}/cancel", "post"),
        ("/run/{run_id}/status", "get"),
        ("/permission/{permission_id}/reply", "post"),
    ):
        assert isinstance(paths[path], dict) and method in paths[path], path


# ---------------------------------------------------------------------------
# ⑦ shutdown: after the server stops, SDK requests answer connection errors
# ---------------------------------------------------------------------------


def test_requests_after_shutdown_answer_connection_errors(tmp_path_factory: pytest.TempPathFactory) -> None:
    root = tmp_path_factory.mktemp("sdk_shutdown_server")
    handle = _boot_server(root / "app.db", _KeyedProvider())
    _stop_server(handle)
    client = OpencodeClient(base_url=handle.base, token=_TOKEN, timeout_s=5.0)
    with pytest.raises(OpencodeConnectionError):
        client.create_session(_SNAPSHOT_BODY)
    stream = client.stream_events("ses_whatever")
    with pytest.raises(OpencodeConnectionError):
        next(stream)
