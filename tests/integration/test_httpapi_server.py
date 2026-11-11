"""P2-S3 / M5-A integration tests: the local single-user HTTP server face.

The six §7 command routes over one :class:`~opencode.app.App` composition,
through the transport in :mod:`opencode.httpapi.app` (bearer token, Origin
refusal, the three transport caps, the 22-code error mapping, the App
lifespan). Contract anchors: PHASE2-MODULES §7 L103 (the six commands), L104
(one server process / one ASGI worker / one SessionService), L106-107
(loopback+token, limits, Origin refusal, idempotent request_id, disconnect
never cancels), L108 (shutdown reclaims the scheduled runs), L110 (single
worker boot as the positive case; a second App over the same data directory
fails at the writer lock); CORE-CONTRACTS §8 (the CoreError wire body and the
closed 22-code registry); p2-slices §2.2/§4 (SSE and SDK faces are later
slices and absent here).

④ disclosures: the request carrier is a hand-built raw-ASGI driver (own
scope/receive/send per call; a receive-call counter pins that the prompt
handler consumes exactly one body message and never polls for
http.disconnect), except ONE real uvicorn smoke test at the end
(``uvicorn.Server(Config(...))`` on a worker thread with an ephemeral
127.0.0.1 port and a stdlib ``urllib`` client -- uvicorn itself disables
signal capture off the main thread). Time-based faces use shortened real
durations instead of an injected clock (``close_timeout_s=0.2`` for the
shutdown-reclaim test); the app "restart" is a second App over the same data
directory after the first fully closed; the 429 test relies on the guard's
check-and-increment being one synchronous block, so exactly the first eight
concurrent cancels are admitted in creation order and the ninth is refused.
"""

from __future__ import annotations

import asyncio
import email.message
import importlib
import inspect
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeGuard

import pytest
import uvicorn
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.errors import CoreError
from opencode.contracts.events import PY_PERMISSION_REQUESTED
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
from opencode.httpapi import ServerExtrasMissingError, _require_server_extras, create_app, serve
from opencode.httpapi.app import (
    _HTTP_STATUS_BY_CODE,
    DEFAULT_MESSAGES_LIMIT,
    MAX_CONCURRENT_REQUESTS,
    MAX_MESSAGES_LIMIT,
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    _json_response,
    _TransportError,
)
from opencode.httpapi.serving import (
    DEFAULT_PORT,
    HOST,
    PORT_ENV,
    TOKEN_ENV,
    ServerConfigurationError,
    _builtin_adapters,
    _maas_env,
    _port_from_env,
    _run_migrations,
    _server_config,
    _token_from_env,
)
from opencode.session.service import DEFAULT_CLOSE_TIMEOUT_S, RunStatus
from opencode.storage import StoreError, WriterLockHeldError
from opencode.tools import DEFAULT_APPROVAL_TIMEOUT_S, ToolContext
from opencode.tools.registry import ToolAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_SNAPSHOT_BODY = _SNAPSHOT.model_dump(mode="json")
_MODEL = ModelRef(provider="maas", model="test-model")
_TOKEN = "test-token-1234"
_TERMINAL = {"succeeded", "failed", "interrupted"}
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ToolCallStartChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    UsageChunk,
    FinishChunk,
)

#: Ruling e: the full closed-registry mapping, pinned row by row below.
_EXPECTED_STATUS_TABLE: dict[CoreErrorCode, int] = {
    CoreErrorCode.VALIDATION_ERROR: 422,
    CoreErrorCode.UNSUPPORTED_CAPABILITY: 501,
    CoreErrorCode.NOT_FOUND: 404,
    CoreErrorCode.SESSION_BUSY: 409,
    CoreErrorCode.CONFLICT: 409,
    CoreErrorCode.IDEMPOTENCY_CONFLICT: 409,
    CoreErrorCode.INVALID_CURSOR: 400,
    CoreErrorCode.PERMISSION_DENIED: 403,
    CoreErrorCode.APPROVAL_UNAVAILABLE: 409,
    CoreErrorCode.APPROVAL_EXPIRED: 410,
    CoreErrorCode.TOOL_TIMEOUT: 504,
    CoreErrorCode.TOOL_FAILED: 500,
    CoreErrorCode.EXECUTION_UNKNOWN: 500,
    CoreErrorCode.PROVIDER_UNAVAILABLE: 502,
    CoreErrorCode.PROVIDER_STREAM_INTERRUPTED: 502,
    CoreErrorCode.CONTEXT_LIMIT_EXCEEDED: 422,
    CoreErrorCode.SLOW_CONSUMER: 429,
    CoreErrorCode.STORAGE_CORRUPT: 500,
    CoreErrorCode.MIGRATION_REQUIRED: 503,
    CoreErrorCode.SESSION_READ_ONLY: 409,
    CoreErrorCode.SESSION_CLOSED: 410,
    CoreErrorCode.LIFECYCLE_CLEANUP_FAILED: 500,
}

#: A loopback client that never routes through a system proxy (stdlib urllib
#: would otherwise consult the Windows proxy set for plain HTTP).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


async def _unserved_asgi_app(scope: Any, receive: Any, send: Any) -> None:
    """A never-served ASGI callable: the config pin inspects, it does not serve."""
    raise AssertionError("the config pin must never serve a request")


# ---------------------------------------------------------------------------
# migration + sqlite probe helpers (the test_approval.py forms)
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


def _run_status_value(db_path: Path, run_id: str) -> str | None:
    row = _fetch_all(db_path, "SELECT status FROM runs WHERE run_id = ?", (run_id,))
    return None if not row else str(row[0][0])


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


# ---------------------------------------------------------------------------
# scripted provider + approval tool (the test_approval/test_service_run forms)
# ---------------------------------------------------------------------------


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        # Set the moment the scripted stream reaches the gate: proof that the
        # run task is parked mid-stream ("running" alone pins only run.started).
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
                item.entered.set()
                await item.event.wait()

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


def _run_app(
    db_path: Path,
    provider: _ScriptedProvider,
    *,
    adapters: Sequence[ToolAdapter] = (),
    approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
    close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> App:
    """The full composition: provider + tools + runner wired into the service."""
    return App(
        db_path,
        mode="create",
        working_dir="C:/work",
        provider=provider,
        tools=adapters,
        settings=_settings(),
        approval_timeout_s=approval_timeout_s,
        close_timeout_s=close_timeout_s,
    )


def _plain_app(db_path: Path) -> App:
    """The provider-less composition: storage + service only (guard/docs face tests)."""
    return App(db_path, working_dir="C:/work")


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


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


# ---------------------------------------------------------------------------
# raw-ASGI request driver (no httpx/TestClient: zero new test dependencies)
# ---------------------------------------------------------------------------


class _ReceiveRecorder:
    """Receive-channel fake: scripted messages, then a standing disconnect; counts calls."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        if self._messages:
            return self._messages.pop(0)
        return {"type": "http.disconnect"}


def _json_body(payload: JsonObject) -> bytes:
    return json.dumps(payload).encode()


async def _asgi_request(
    api: Any,
    method: str,
    path: str,
    *,
    token: str | None = _TOKEN,
    body: bytes | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
    scripted_receive: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[bytes, bytes], bytes, _ReceiveRecorder]:
    """Drive one hand-built HTTP scope through the ASGI app; collect the wire answer."""
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
    if extra_headers is not None:
        headers.extend(extra_headers)
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.11", 4096),
    }
    if scripted_receive is not None:
        messages = scripted_receive
    elif body is not None:
        messages = [{"type": "http.request", "body": body, "more_body": False}]
    else:
        messages = []
    receive = _ReceiveRecorder(messages)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await api(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    status = int(start["status"])
    response_headers = {bytes(name).lower(): bytes(value) for name, value in start["headers"]}
    response_body = b"".join(
        bytes(message.get("body", b"")) for message in sent if message["type"] == "http.response.body"
    )
    return status, response_headers, response_body, receive


async def _asgi_json(
    api: Any,
    method: str,
    path: str,
    *,
    token: str | None = _TOKEN,
    body: bytes | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
    scripted_receive: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[bytes, bytes], Any, _ReceiveRecorder]:
    status, headers, raw, receive = await _asgi_request(
        api,
        method,
        path,
        token=token,
        body=body,
        extra_headers=extra_headers,
        query_string=query_string,
        scripted_receive=scripted_receive,
    )
    payload: Any = json.loads(raw) if raw else None
    return status, headers, payload, receive


def _assert_core_error(payload: Any, code: CoreErrorCode, *, retryable: bool) -> None:
    """Every error answer carries the §8 CoreError wire shape exactly."""
    assert isinstance(payload, dict)
    assert set(payload) == {"code", "message", "retryable", "details"}
    assert payload["code"] == code.value
    assert payload["retryable"] is retryable
    assert isinstance(payload["message"], str) and payload["message"]
    assert isinstance(payload["details"], dict)


async def _http_create_session(api: Any) -> str:
    status, _, payload, _ = await _asgi_json(api, "POST", "/session", body=_json_body(_SNAPSHOT_BODY))
    assert status == 200
    assert isinstance(payload, dict) and set(payload) == {"session_id"}
    return str(payload["session_id"])


async def _http_prompt(api: Any, session_id: str, text: str, request_id: str) -> dict[str, Any]:
    status, _, payload, _ = await _asgi_json(
        api, "POST", f"/session/{session_id}/prompt", body=_json_body({"text": text, "request_id": request_id})
    )
    assert status == 200
    assert isinstance(payload, dict)
    return payload


async def _await_http_status(api: Any, run_id: str, wanted: set[str], *, timeout_s: float = 5.0) -> dict[str, Any]:
    """Poll GET /run/{id}/status over raw ASGI until the run reaches ``wanted`` (bounded)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status, _, payload, _ = await _asgi_json(api, "GET", f"/run/{run_id}/status")
        assert status == 200
        assert isinstance(payload, dict)
        if payload["status"] in wanted:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"run {run_id} did not reach {wanted} (stuck at {payload['status']})")
        await asyncio.sleep(0.01)


def _assert_run_status_wire(payload: Any, session_id: str, run_id: str, request_id: str, status: str) -> None:
    assert isinstance(payload, dict)
    assert set(payload) == {"session_id", "run_id", "request_id", "status", "error"}
    assert payload["session_id"] == session_id
    assert payload["run_id"] == run_id
    assert payload["request_id"] == request_id
    assert payload["status"] == status


async def _await_registered_reply(
    api: Any, permission_id: str, reply: str, *, timeout_s: float = 5.0
) -> dict[str, Any]:
    """POST the reply once the coordinator holds the permission pending.

    ④ race disclosure: the durable py.permission.requested commit (aiosqlite
    worker thread) becomes reader-visible strictly before the Invoker's
    in-process registration -- a reply in that window legitimately answers
    404, so poll-retry instead of assuming registration (the
    ``_reply_registered`` form of test_approval.py, over the HTTP face).
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/permission/{permission_id}/reply", body=_json_body({"reply": reply})
        )
        if status == 200:
            assert isinstance(payload, dict)
            return payload
        if status != 404:
            raise AssertionError(f"permission reply answered {status}: {payload}")
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"permission {permission_id} was never registered")
        await asyncio.sleep(0.01)


async def _drive_lifespan(
    api: Any,
    *,
    after_startup: Callable[[], Awaitable[None]] | None = None,
    expect_startup_failure: bool = False,
) -> list[dict[str, Any]]:
    """Drive the ASGI lifespan protocol by hand and collect the answered events."""
    events: list[dict[str, Any]] = []
    state = {"next": "startup"}

    async def receive() -> dict[str, Any]:
        message: dict[str, Any] = {"type": f"lifespan.{state['next']}"}
        state["next"] = "shutdown"
        return message

    async def send(message: dict[str, Any]) -> None:
        events.append(message)
        if message["type"] == "lifespan.startup.complete" and after_startup is not None:
            await after_startup()

    try:
        await api({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}, receive, send)
    except Exception as error:
        # starlette re-raises the startup exception after answering startup.failed
        if not expect_startup_failure:
            raise
        assert "recovery scan failed" in str(error)
    return events


# ---------------------------------------------------------------------------
# module faces: constants, host env, assembly, migration (sync)
# ---------------------------------------------------------------------------


def test_transport_constants_and_the_full_22_row_status_table_are_pinned() -> None:
    assert MAX_REQUEST_BODY_BYTES == 1024 * 1024
    assert MAX_CONCURRENT_REQUESTS == 8
    assert MAX_RESPONSE_BYTES == 4 * 1024 * 1024
    assert DEFAULT_MESSAGES_LIMIT == 100
    assert MAX_MESSAGES_LIMIT == 200
    assert HOST == "127.0.0.1"
    assert DEFAULT_PORT == 4096
    assert TOKEN_ENV == "OPENCODE_HTTP_TOKEN"
    assert PORT_ENV == "OPENCODE_HTTP_PORT"
    # the mapping covers exactly the closed §8 registry, row for row (ruling e)
    assert len(_EXPECTED_STATUS_TABLE) == 22
    assert set(_EXPECTED_STATUS_TABLE) == set(CoreErrorCode)
    assert _HTTP_STATUS_BY_CODE == _EXPECTED_STATUS_TABLE


def test_serving_face_pins_loopback_single_worker_and_no_reload_knobs() -> None:
    assert list(inspect.signature(serve).parameters) == ["db_path"]
    assert set(inspect.signature(_server_config).parameters) == {"api", "port"}
    config = _server_config(_unserved_asgi_app, port=4711)
    assert config.host == HOST == "127.0.0.1"
    assert config.port == 4711
    assert config.lifespan == "on"
    assert config.workers == 1
    assert config.reload is False


def test_token_from_env_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(ServerConfigurationError) as missing:
        _token_from_env()
    assert TOKEN_ENV in str(missing.value)
    for bad in ("", "   \t "):
        monkeypatch.setenv(TOKEN_ENV, bad)
        with pytest.raises(ServerConfigurationError) as rejected:
            _token_from_env()
        assert TOKEN_ENV in str(rejected.value)
    monkeypatch.setenv(TOKEN_ENV, "token-with interior space")
    assert _token_from_env() == "token-with interior space"  # used verbatim, never trimmed


def test_port_from_env_defaults_and_rejects_out_of_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PORT_ENV, raising=False)
    assert _port_from_env() == DEFAULT_PORT == 4096
    monkeypatch.setenv(PORT_ENV, "8123")
    assert _port_from_env() == 8123
    for bad in ("0", "65536", "-1", "abc", ""):
        monkeypatch.setenv(PORT_ENV, bad)
        with pytest.raises(ServerConfigurationError) as rejected:
            _port_from_env()
        assert PORT_ENV in str(rejected.value)
    monkeypatch.setenv(PORT_ENV, "65535")
    assert _port_from_env() == 65535
    monkeypatch.setenv(PORT_ENV, "1")
    assert _port_from_env() == 1


def test_maas_env_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_PY_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_PY_API_KEY", raising=False)
    with pytest.raises(ServerConfigurationError) as missing:
        _maas_env()
    assert "OPENCODE_PY_BASE_URL" in str(missing.value) and "OPENCODE_PY_API_KEY" in str(missing.value)
    monkeypatch.setenv("OPENCODE_PY_BASE_URL", "https://maas.example/v1")
    with pytest.raises(ServerConfigurationError):
        _maas_env()
    monkeypatch.setenv("OPENCODE_PY_API_KEY", "sk-test")
    assert _maas_env() == ("https://maas.example/v1", "sk-test")


def test_builtin_adapters_are_the_cli_tools_in_order_per_platform() -> None:
    """Platform-conditional adapter census (T-B-087 ruling 2 / T-C1-073).

    POSIX asserts the original twelve-name tuple unchanged; win32 asserts
    the twelve plus the appended PTY T1 ``PtyRunAdapter`` (13 names), the
    ``*pty_adapters()`` splice in ``_builtin_adapters``.
    """
    names = tuple(type(adapter).__name__ for adapter in _builtin_adapters(("C:/work",)))
    expected = (
        "FsReadAdapter",
        "FsLsAdapter",
        "FsGlobAdapter",
        "FsGrepAdapter",
        "FsWriteAdapter",
        "FsEditAdapter",
        "BashAdapter",
        "GitStatusAdapter",
        "GitDiffAdapter",
        "GitWorktreeAddAdapter",
        "GitWorktreeListAdapter",
        "GitWorktreeRemoveAdapter",
    )
    if sys.platform == "win32":
        assert names == (*expected, "PtyRunAdapter")
    else:
        assert names == expected


def test_run_migrations_upgrades_a_fresh_database_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "serve.db"
    _run_migrations(db_path)
    _run_migrations(db_path)  # idempotent, like the CLI boot
    rows = _fetch_all(db_path, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'events'")
    assert rows and int(rows[0][0]) == 1


# ---------------------------------------------------------------------------
# the deferred-import gate (ruling a / p2-slices §4 L142)
# ---------------------------------------------------------------------------


def test_require_server_extras_gate_reports_unsupported_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastapi", None)
    with pytest.raises(ServerExtrasMissingError) as refused:
        _require_server_extras()
    assert refused.value.core.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "server" in refused.value.core.message  # the message names the extras face
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(ServerExtrasMissingError):
        _require_server_extras()


def test_package_import_gate_refuses_without_the_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fastapi", None)
    removed = {
        name: module
        for name, module in sys.modules.items()
        if name == "opencode.httpapi" or name.startswith("opencode.httpapi.")
    }
    for name in removed:
        del sys.modules[name]
    try:
        # ④ the re-executed package mints a NEW ServerExtrasMissingError class object, so the
        # match goes through the unchanged StoreError base plus the §8 code on .core
        with pytest.raises(StoreError) as refused:
            importlib.import_module("opencode.httpapi")
        assert type(refused.value).__name__ == "ServerExtrasMissingError"
        assert refused.value.core.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert "opencode.httpapi" not in sys.modules  # the failed import leaves no module behind
    finally:
        sys.modules.update(removed)
        importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# authentication, docs family, Origin (rulings b/g)
# ---------------------------------------------------------------------------


async def test_missing_bearer_token_answers_401_with_core_error_body(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        status, headers, payload, _ = await _asgi_json(
            api, "POST", "/session", body=_json_body(_SNAPSHOT_BODY), token=None
        )
        assert status == 401
        assert headers.get(b"www-authenticate") == b"Bearer"
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)


async def test_wrong_or_malformed_bearer_tokens_answer_401(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(
            api, "POST", "/session", body=_json_body(_SNAPSHOT_BODY), token="wrong"
        )
        assert status == 401
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        for malformed in (b"Token abc", b"Bearer", b"Bearer "):
            status, _, payload, _ = await _asgi_json(
                api,
                "POST",
                "/session",
                body=_json_body(_SNAPSHOT_BODY),
                token=None,
                extra_headers=[(b"authorization", malformed)],
            )
            assert status == 401
            _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)


async def test_docs_family_is_token_gated(tmp_path: Path) -> None:
    """Ruling b: /docs, /redoc and /openapi.json answer 401 anonymously, 200 with the token."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        for path in ("/docs", "/redoc", "/openapi.json"):
            status, _, payload, _ = await _asgi_json(api, "GET", path, token=None)
            assert status == 401
            _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        status, headers, _, _ = await _asgi_request(api, "GET", "/docs")
        assert status == 200
        assert b"text/html" in headers.get(b"content-type", b"")
        status, headers, _, _ = await _asgi_request(api, "GET", "/redoc")
        assert status == 200
        assert b"text/html" in headers.get(b"content-type", b"")
        status, _, payload, _ = await _asgi_json(api, "GET", "/openapi.json")
        assert status == 200
        assert isinstance(payload, dict) and "openapi" in payload and "paths" in payload


async def test_requests_with_an_origin_header_are_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(
            api,
            "POST",
            "/session",
            body=_json_body(_SNAPSHOT_BODY),
            extra_headers=[(b"origin", b"http://127.0.0.1:5173")],
        )
        assert status == 403
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        status, _, payload, _ = await _asgi_json(
            api, "GET", "/run/run_000001/status", extra_headers=[(b"origin", b"https://evil.example")]
        )
        assert status == 403
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)


# ---------------------------------------------------------------------------
# the six command routes: positive wire forms, field by field (ruling d)
# ---------------------------------------------------------------------------


async def test_create_session_answers_the_session_id_wire(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _run_app(db_path, _ScriptedProvider([[_final_script()]])) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        assert session_id.startswith("ses_")


async def test_prompt_answers_the_runref_wire(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        payload = await _http_prompt(api, session_id, "hello", "req_1")
        assert set(payload) == {"session_id", "run_id", "request_id"}
        assert payload["session_id"] == session_id
        assert payload["run_id"].startswith("run_")
        assert payload["request_id"] == "req_1"
        await _await_http_status(api, str(payload["run_id"]), _TERMINAL)  # settle before close


async def test_run_status_answers_the_runstatus_wire(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        final = await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        assert final["status"] == "succeeded"
        status, _, payload, _ = await _asgi_json(api, "GET", f"/run/{ref['run_id']}/status")
        assert status == 200
        _assert_run_status_wire(payload, session_id, str(ref["run_id"]), "req_1", "succeeded")
        assert payload["error"] is None


async def test_cancel_answers_the_wire_and_a_late_cancel_returns_the_original_terminal(tmp_path: Path) -> None:
    """L107: explicit cancel folds the live run interrupted; a late cancel re-reads, never overwrites."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")]])  # the run parks on the approval; cancel resolves it
    async with _run_app(db_path, provider, adapters=[write]) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        run_id = str(ref["run_id"])
        await _await_permission_requested(db_path, session_id)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/run/{run_id}/cancel")
        assert status == 200
        assert isinstance(payload, dict)
        assert set(payload) == {"session_id", "run_id", "request_id", "status", "error"}
        assert payload["session_id"] == session_id
        assert payload["run_id"] == run_id
        assert payload["request_id"] == "req_1"
        assert payload["status"] in {"cancelling", "interrupted"}
        assert payload["error"] is None
        assert write.starts == []  # the approval folded cancelled, the tool never ran
        final = await _await_http_status(api, run_id, _TERMINAL)
        assert final["status"] == "interrupted"
        # a second cancel over the standing terminal returns the original status (§7 L335)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/run/{run_id}/cancel")
        assert status == 200
        _assert_run_status_wire(payload, session_id, run_id, "req_1", "interrupted")


async def test_messages_answers_the_page_wire_with_default_and_capped_limits(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages")
        assert status == 200
        assert isinstance(payload, dict)
        assert set(payload) == {"items", "next_after_message_id", "has_more"}
        items = payload["items"]
        assert isinstance(items, list) and len(items) == 3  # user, assistant, and the run-end idle row
        user = items[0]
        assert set(user) == {"id", "type", "created_at", "metadata", "text"}
        assert user["type"] == "user"
        assert user["text"] == "hello"
        assert user["id"].startswith("msg_")
        assert isinstance(user["metadata"], dict)
        assert items[1]["type"] == "assistant"
        assert items[2]["type"] == "idle"
        assert payload["next_after_message_id"] == items[2]["id"]
        assert payload["has_more"] is False
        # the capped limit stays in the transport domain (1..200) and reaches the repo verbatim
        status, _, payload, _ = await _asgi_json(
            api, "GET", f"/session/{session_id}/messages", query_string=b"limit=200"
        )
        assert status == 200
        assert payload["has_more"] is False


async def test_permission_reply_answers_the_receipt_wire(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])
    async with _run_app(db_path, provider, adapters=[write]) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        requested = await _await_permission_requested(db_path, session_id)
        permission_id = str(requested[0]["permission_id"])
        execution_id = str(requested[0]["execution_id"])
        payload = await _await_registered_reply(api, permission_id, "once")
        assert payload == {"permission_id": permission_id, "decision": "allow", "execution_id": execution_id}
        final = await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        assert final["status"] == "succeeded" and final["error"] is None
        assert write.starts == ["t1"]


async def test_permission_reply_conflict_missing_and_bad_vocabulary(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    write = _AskAdapter()
    provider = _ScriptedProvider([[_ask_script("t1")], [_final_script()]])
    async with _run_app(db_path, provider, adapters=[write]) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        requested = await _await_permission_requested(db_path, session_id)
        permission_id = str(requested[0]["permission_id"])
        await _await_registered_reply(api, permission_id, "once")
        # the permission is terminal now: a second reply conflicts (§7 L173)
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/permission/{permission_id}/reply", body=_json_body({"reply": "once"})
        )
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.CONFLICT, retryable=False)
        # unknown permission with a valid vocabulary -> 404
        status, _, payload, _ = await _asgi_json(
            api, "POST", "/permission/prm_missing/reply", body=_json_body({"reply": "once"})
        )
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # unknown vocabulary -> 422 (validated before existence)
        status, _, payload, _ = await _asgi_json(
            api, "POST", "/permission/prm_missing/reply", body=_json_body({"reply": "maybe"})
        )
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)  # settle before close


# ---------------------------------------------------------------------------
# request_id idempotency, both branches (ruling d / L107)
# ---------------------------------------------------------------------------


async def test_request_id_with_identical_input_returns_the_original_runref(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])  # exactly one provider call: the retry dedupes
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        original = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(original["run_id"]), _TERMINAL)
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{session_id}/prompt", body=_json_body({"text": "hello", "request_id": "req_1"})
        )
        assert status == 200
        assert payload == original  # the original RunRef, field for field
        assert len(provider.requests) == 1  # the dedupe answered before any second scheduling


async def test_request_id_with_different_input_conflicts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        await _http_prompt(api, session_id, "hello", "req_1")
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{session_id}/prompt", body=_json_body({"text": "different", "request_id": "req_1"})
        )
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.IDEMPOTENCY_CONFLICT, retryable=False)


# ---------------------------------------------------------------------------
# transport validation and unknown-id paths (rulings d/e)
# ---------------------------------------------------------------------------


async def test_prompt_body_shape_is_validated_with_422_core_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _run_app(db_path, _ScriptedProvider([[]])) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        cases: list[tuple[bytes, str]] = [
            (_json_body({"text": "hi"}), "request_id"),
            (_json_body({"request_id": "req_1"}), "text"),
            (_json_body({"text": 5, "request_id": "req_1"}), "text"),
            (_json_body({"text": "hi", "request_id": "req_1", "extra": 1}), "extra"),
            (b"[1, 2, 3]", "object"),
            (b"\x00 not json", "JSON"),
            (b"", "JSON"),
        ]
        for body, marker in cases:
            status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{session_id}/prompt", body=body)
            assert status == 422, (marker, body[:40])
            _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # the missing-field message names the offending field
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{session_id}/prompt", body=_json_body({"text": "hi"})
        )
        assert "request_id" in str(payload["message"])


async def test_create_body_rejects_non_snapshots_and_non_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        for body in (b"{}", b"[1, 2]", b"garbage"):
            status, _, payload, _ = await _asgi_json(api, "POST", "/session", body=body)
            assert status == 422
            _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)


async def test_unknown_ids_answer_404_core_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(
            api, "POST", "/session/ses_missing/prompt", body=_json_body({"text": "hi", "request_id": "req_1"})
        )
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "GET", "/run/run_000009/status")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "POST", "/run/run_000009/cancel")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


async def test_messages_limit_domain_cursors_and_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        # out-of-domain limits are refused at the transport, before the repo clamp
        for query in (b"limit=0", b"limit=201", b"limit=abc"):
            status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages", query_string=query)
            assert status == 422
            _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # limit=1 walks the three-message history (user, assistant, idle) by cursor
        status, _, first, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages", query_string=b"limit=1")
        assert status == 200
        assert len(first["items"]) == 1
        assert first["items"][0]["type"] == "user"
        assert first["has_more"] is True
        assert first["next_after_message_id"] == first["items"][0]["id"]
        status, _, second, _ = await _asgi_json(
            api,
            "GET",
            f"/session/{session_id}/messages",
            query_string=f"after_message_id={first['next_after_message_id']}&limit=1".encode(),
        )
        assert status == 200
        assert len(second["items"]) == 1
        assert second["items"][0]["type"] == "assistant"
        assert second["has_more"] is True
        status, _, third, _ = await _asgi_json(
            api,
            "GET",
            f"/session/{session_id}/messages",
            query_string=f"after_message_id={second['next_after_message_id']}&limit=1".encode(),
        )
        assert status == 200
        assert len(third["items"]) == 1
        assert third["items"][0]["type"] == "idle"
        assert third["has_more"] is False
        assert third["next_after_message_id"] == third["items"][0]["id"]
        # an empty cursor is passed through and rejected by the service layer
        status, _, payload, _ = await _asgi_json(
            api, "GET", f"/session/{session_id}/messages", query_string=b"after_message_id="
        )
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # a cursor that does not belong to the session is 400 InvalidCursor (S6)
        status, _, payload, _ = await _asgi_json(
            api, "GET", f"/session/{session_id}/messages", query_string=b"after_message_id=msg_999999"
        )
        assert status == 400
        _assert_core_error(payload, CoreErrorCode.INVALID_CURSOR, retryable=False)
        # a never-created session is NotFound, never faked as an empty history
        status, _, payload, _ = await _asgi_json(api, "GET", "/session/ses_missing/messages")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


# ---------------------------------------------------------------------------
# the three transport caps (ruling f)
# ---------------------------------------------------------------------------


async def test_request_body_over_the_cap_answers_413(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        # exactly at the cap: the reader accepts it (the cap is >, not >=) and parsing answers 422
        status, _, payload, _ = await _asgi_json(api, "POST", "/session", body=b"a" * MAX_REQUEST_BODY_BYTES)
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # one byte over the cap: refused before any parsing
        status, _, payload, _ = await _asgi_json(api, "POST", "/session", body=b"a" * (MAX_REQUEST_BODY_BYTES + 1))
        assert status == 413
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)


def test_json_response_refuses_payloads_over_the_cap() -> None:
    with pytest.raises(_TransportError) as refused:
        _json_response({"text": "a" * (MAX_RESPONSE_BYTES + 1)})
    assert refused.value.status_code == 413
    assert refused.value.core.code is CoreErrorCode.VALIDATION_ERROR


async def test_response_body_over_the_cap_answers_413(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    scripts = [[_Script().text("p1", f"ok {index}").usage().finish()] for index in range(5)]
    provider = _ScriptedProvider(scripts)
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        big = "a" * 900_000  # under the request cap, but five of them overflow the response cap
        for index in range(5):
            ref = await _http_prompt(api, session_id, big, f"req_{index}")
            await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages")
        assert status == 413
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)


async def test_concurrency_limit_answers_429_slow_consumer(tmp_path: Path) -> None:
    """Eight in-flight requests saturate the guard; the ninth answers 429 SlowConsumer (retryable)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello")
    gate = script.gate()  # the run parks mid-stream: every cancel parks on the same task
    provider = _ScriptedProvider([[script]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        run_id = str(ref["run_id"])
        await _await_http_status(api, run_id, {"running"})
        # "running" only proves py.run.started committed: between it and the
        # gate the run task still crosses awaits where a cancel-first intent
        # folds it early (§2.1 L59/L65, §3.1 L125) and the cancels would all
        # answer at once. Park proof first (T-C1-061b), then fire the cancels.
        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)
        tasks = [asyncio.create_task(_asgi_json(api, "POST", f"/run/{run_id}/cancel")) for _ in range(9)]
        done, pending = await asyncio.wait(tasks, timeout=1.0)
        assert len(done) == 1 and len(pending) == 8
        status, _, payload, _ = next(iter(done)).result()
        assert status == 429
        _assert_core_error(payload, CoreErrorCode.SLOW_CONSUMER, retryable=True)
        gate.open()  # release the parked stream: the run folds interrupted, the eight cancels answer
        done2, pending2 = await asyncio.wait(pending, timeout=5.0)
        assert not pending2
        results = [task.result() for task in done2]
        assert [result[0] for result in results] == [200] * 8
        assert {result[2]["status"] for result in results} == {"interrupted"}
        _assert_run_status_wire(results[0][2], session_id, run_id, "req_1", "interrupted")


# ---------------------------------------------------------------------------
# client disconnect (L107: 断线不默认取消 run)
# ---------------------------------------------------------------------------


async def test_client_disconnect_does_not_cancel_the_run(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        body = _json_body({"text": "hello", "request_id": "req_1"})
        status, _, payload, receive = await _asgi_json(
            api,
            "POST",
            f"/session/{session_id}/prompt",
            body=body,
            scripted_receive=[
                {"type": "http.request", "body": body, "more_body": False},
                {"type": "http.disconnect"},
            ],
        )
        assert status == 200
        assert receive.calls == 1  # the body was read once; the transport never polled for the disconnect
        final = await _await_http_status(api, str(payload["run_id"]), _TERMINAL)
        assert final["status"] == "succeeded" and final["error"] is None


# ---------------------------------------------------------------------------
# the closed 22-code mapping, row by row (ruling e)
# ---------------------------------------------------------------------------


class _SyntheticStoreError(StoreError):
    """One synthetic code carrier: the table test overrides ``core`` per row."""

    code = CoreErrorCode.VALIDATION_ERROR


async def test_every_registered_code_maps_to_its_pinned_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        for code, expected_status in _EXPECTED_STATUS_TABLE.items():
            error = _SyntheticStoreError("synthetic mapping probe")
            error.core = CoreError(code=code, message="synthetic mapping probe", retryable=False)

            async def _raise_status(_run_id: str, _error: StoreError = error) -> RunStatus:
                raise _error

            monkeypatch.setattr(app.service, "run_status", _raise_status)
            status, _, payload, _ = await _asgi_json(api, "GET", "/run/run_000001/status")
            assert status == expected_status, f"{code}: expected HTTP {expected_status}, got {status}"
            _assert_core_error(payload, code, retryable=False)


# ---------------------------------------------------------------------------
# the second-App writer lock (ruling h / L110)
# ---------------------------------------------------------------------------


async def test_second_app_on_the_same_data_dir_refuses_the_writer_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path):
        with pytest.raises(WriterLockHeldError):
            _plain_app(db_path)  # one data directory, one writer: the second server process cannot boot
    async with _plain_app(db_path) as second:  # the lock released at close: a fresh App opens again
        assert second.service.closed is False


# ---------------------------------------------------------------------------
# the ASGI lifespan (ruling h / L108)
# ---------------------------------------------------------------------------


async def test_lifespan_shutdown_closes_the_app_and_reclaims_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello")
    script.gate()  # the run parks mid-stream for good: only the shutdown may reclaim it
    provider = _ScriptedProvider([[script]])
    app = _run_app(db_path, provider, close_timeout_s=0.2)
    api = create_app(app, token=_TOKEN)
    holder: dict[str, str] = {}

    async def after_startup() -> None:
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        holder["session_id"] = session_id
        holder["run_id"] = str(ref["run_id"])
        await _await_http_status(api, holder["run_id"], {"running"})

    events = await _drive_lifespan(api, after_startup=after_startup)
    assert [event["type"] for event in events] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert app.service.closed is True
    assert provider.close_count == 1
    assert _run_status_value(db_path, holder["run_id"]) == "interrupted"  # the shutdown reclaimed the run


async def test_lifespan_startup_failure_bubbles_as_startup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    app = _plain_app(db_path)

    class _BrokenGate:
        async def wait(self) -> None:
            raise RuntimeError("recovery scan failed")

    monkeypatch.setattr(app, "_recovery_gate", _BrokenGate())
    api = create_app(app, token=_TOKEN)
    events = await _drive_lifespan(api, expect_startup_failure=True)
    assert [event["type"] for event in events] == ["lifespan.startup.failed"]
    assert "recovery scan failed" in str(events[0].get("message", ""))
    assert app.service.closed is False  # the lifespan never entered, so it never closed the App
    await app.close()


# ---------------------------------------------------------------------------
# the one real uvicorn smoke test (L110: single-worker boot over the real wire)
# ---------------------------------------------------------------------------


def _is_connection_abort(error: BaseException) -> bool:
    """True only for the loopback abort/reset faces (WinError 10053/10054 kin)."""
    if isinstance(error, (ConnectionAbortedError, ConnectionResetError)):
        return True
    return isinstance(error, urllib.error.URLError) and isinstance(
        error.reason, (ConnectionAbortedError, ConnectionResetError)
    )


def _urllib_call(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = _TOKEN,
    attempts: int = 3,
    backoff_s: float = 0.25,
    open_fn: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """One real-wire urllib call with a bounded retry on the connection-abort face.

    Windows loopback can abort an established connection in the window right
    after server boot (WinError 10053, T-C1-061 face 3); only that face is
    retried -- an HTTP answer (an HTTPError of any status) is returned as-is
    on the first attempt and a non-abort URLError is re-raised untouched, so
    deterministic failures stay red. Three attempts with a short linear
    backoff bound the green-path cost; an abort that exhausts the budget
    re-raises on the last attempt (fail-visible). Guard rider (T-B-070
    recommendation 1 / T-C1-062): ``attempts < 1`` is the only input that
    could fall through the loop to the unreachable tail line, so it is
    rejected up front -- the assertion face below stays frozen.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    data = _json_body(body) if body is not None else None
    headers: dict[str, str] = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = open_fn if open_fn is not None else _OPENER.open
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=10) as response:
                payload: Any = json.loads(response.read())
                lowered = {name.lower(): value for name, value in response.headers.items()}
                return int(response.status), lowered, payload if isinstance(payload, dict) else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            payload = json.loads(raw) if raw else None
            lowered = {name.lower(): value for name, value in error.headers.items()}
            return int(error.code), lowered, payload if isinstance(payload, dict) else None
        except (ConnectionAbortedError, ConnectionResetError, urllib.error.URLError) as error:
            if attempt == attempts or not _is_connection_abort(error):
                raise
            time.sleep(backoff_s * attempt)
    raise AssertionError("unreachable: the retry loop always returns or re-raises")


class _FakeResponse:
    """Minimal urllib response stand-in: only the faces _urllib_call touches."""

    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status = status
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_urllib_call_retries_connection_aborts_only() -> None:
    """Injectability proof (T-C1-061 face 3): the retry is abort-face-only.

    N-1 aborts then success recovers; a persistent abort exhausts the budget
    and re-raises; an HTTP answer and a non-abort URLError are never retried
    (call count 1) -- the deterministic faces stay fail-visible.
    """
    calls: list[int] = []

    def abort_twice_then_answer(request: object, timeout: int) -> _FakeResponse:
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise ConnectionAbortedError(10053, "the established connection was aborted")
        return _FakeResponse(200, b'{"ok": true}', {"Content-Type": "application/json"})

    status, headers, payload = _urllib_call(
        "http://127.0.0.1", "POST", "/session", attempts=3, backoff_s=0.0, open_fn=abort_twice_then_answer
    )
    assert status == 200 and payload == {"ok": True} and headers["content-type"] == "application/json"

    calls.clear()

    def always_abort(request: object, timeout: int) -> _FakeResponse:
        calls.append(len(calls) + 1)
        raise ConnectionAbortedError(10053, "the established connection was aborted")

    with pytest.raises(ConnectionAbortedError):
        _urllib_call("http://127.0.0.1", "GET", "/run/x/status", attempts=3, backoff_s=0.0, open_fn=always_abort)
    assert len(calls) == 3

    calls.clear()

    def refuse_with_401(request: object, timeout: int) -> _FakeResponse:
        calls.append(len(calls) + 1)
        raise urllib.error.HTTPError(
            "http://127.0.0.1/session", 401, "Unauthorized", email.message.Message(), io.BytesIO(b"")
        )

    status, _, payload = _urllib_call(
        "http://127.0.0.1", "POST", "/session", token=None, attempts=3, backoff_s=0.0, open_fn=refuse_with_401
    )
    assert status == 401 and payload is None and len(calls) == 1

    calls.clear()

    def fail_dns(request: object, timeout: int) -> _FakeResponse:
        calls.append(len(calls) + 1)
        raise urllib.error.URLError(OSError("name resolution failed"))

    with pytest.raises(urllib.error.URLError):
        _urllib_call("http://127.0.0.1", "GET", "/", attempts=3, backoff_s=0.0, open_fn=fail_dns)
    assert len(calls) == 1


def test_uvicorn_smoke_serves_the_real_wire_single_worker(tmp_path: Path) -> None:
    """One programmatic uvicorn.Server on a worker thread: boot, wire, auth, shutdown (④)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    app = _run_app(db_path, provider)
    api = create_app(app, token=_TOKEN)
    config = _server_config(api, port=0)  # loopback host, ephemeral port
    assert config.workers == 1 and config.reload is False
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="opencode-http-smoke")
    thread.start()
    try:
        deadline = time.monotonic() + 15.0
        while not server.started:
            assert thread.is_alive(), "the smoke server thread died during startup"
            assert time.monotonic() < deadline, "the smoke server never started"
            time.sleep(0.05)
        listeners = server.servers
        assert listeners, "the smoke server bound no sockets"
        sockets = listeners[0].sockets
        assert sockets is not None
        address = sockets[0].getsockname()
        assert address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{int(address[1])}"

        # the wire answers 401 without the token
        status, headers, payload = _urllib_call(base, "POST", "/session", _SNAPSHOT_BODY, token=None)
        assert status == 401
        assert headers.get("www-authenticate") == "Bearer"
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)

        # the six-command surface works over the real socket: create -> prompt -> terminal
        status, _, session_payload = _urllib_call(base, "POST", "/session", _SNAPSHOT_BODY)
        assert status == 200 and session_payload is not None and set(session_payload) == {"session_id"}
        session_id = str(session_payload["session_id"])
        status, _, ref = _urllib_call(
            base, "POST", f"/session/{session_id}/prompt", {"text": "hello", "request_id": "req_1"}
        )
        assert status == 200 and ref is not None and set(ref) == {"session_id", "run_id", "request_id"}
        run_id = str(ref["run_id"])
        deadline = time.monotonic() + 10.0
        while True:
            status, _, status_payload = _urllib_call(base, "GET", f"/run/{run_id}/status")
            assert status == 200 and status_payload is not None
            if status_payload.get("status") in _TERMINAL:
                assert status_payload["status"] == "succeeded"
                break
            assert time.monotonic() < deadline, (
                f"the smoke run never finalized (stuck at {status_payload.get('status')})"
            )
            time.sleep(0.05)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    assert not thread.is_alive(), "the smoke server did not shut down"
    assert app.service.closed is True  # the ASGI lifespan closed the App on the way down
