"""P2-S4 / M5-B integration tests: the durable SSE stream face.

The seventh §7 route (L105) over one :class:`~opencode.app.App` composition,
through the transport in :mod:`opencode.httpapi.app`:
``GET /session/{session_id}/events`` streams the durable log via
:meth:`opencode.bus.eventlog.EventLog.follow` (transport-assembled over the
App's engine + bus), with ``Last-Event-ID`` as the ``"{session_id}:{seq}"``
resume cursor. Contract anchors: PHASE2-MODULES §7 L105 (durable SSE over
EventLog.follow; the cursor refuses cross-session/ahead-of-H; a reconnect
re-reads from the cursor with event-id dedup on the client; no ephemeral
token replay in the first version) and L108 (a slow SSE consumer never
blocks the runner — the bus is a wake signal, not a data path — and after a
disconnect the durable log can be re-read), L103 (the SSE face outputs the
core contract directly: zero business-semantic re-implementation);
CORE-CONTRACTS §4 L222-248 (the cursor shape and its state-dependent
validation, the four-step follow with exactly one Synced marker, bad pages
failing closed after their healthy events, durable-only — no ephemeral
replay); p2-slices §2.2 L103 (this card is the L105 sentence; the SDK /
OpenAPI face is out of scope).

④ disclosures: the request carrier is the hand-built raw-ASGI driver of
test_httpapi_server.py (helpers replicated here per the slice's no-import
rule), extended with a streaming harness — the receive channel parks until
the test answers ``http.disconnect`` (starlette's task-group path then
cancels the stream and unwinds the follow context), the send channel
accumulates ``http.response.body`` chunks and parses complete
``\\n\\n``-terminated frames as they arrive, and a paced mode gates every
body chunk behind ``release`` (the slow-reader probe: the follow generator
provably suspends at its yield while the gate holds). Fixtures reach behind
the store's back through direct sqlite surgery (the ``read_only_diagnostic``
status flip, the undecodable row, and the > 4 MiB valid ``py.text.finalized``
row for the frame cap — the runner caps provider text streams at 1 MiB, so
the frame cap is unreachable through a real prompt) — all three are test-only
faces, the runtime detects the first two and fails closed, and the third
exercises a transport-only size refusal. The keepalive test patches
``SSE_KEEPALIVE_SECONDS``; live-delivery waits rely on the follow's own
<= 1 s poll fallback with generous deadlines. Except ONE real uvicorn smoke
test at the end (programmatic ``uvicorn.Server`` on a worker thread, the
stdlib ``urllib`` client reading the stream line by line over a real
socket). No TestClient/httpx anywhere (the 054 red line carries over).
Shutdown with a still-open stream is not exercised here: the 054 lifespan
tests already cover the App-close reclaim face and the disconnect machinery
is the same cancellation path proven below.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.events import PY_RUN_FINISHED, PY_SESSION_CREATED, PY_TEXT_FINALIZED
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    Usage,
    UsageChunk,
)
from opencode.httpapi import create_app
from opencode.httpapi.app import MAX_CONCURRENT_SSE_STREAMS, MAX_SSE_FRAME_BYTES, SSE_KEEPALIVE_SECONDS
from opencode.httpapi.serving import _server_config
from opencode.session.service import DEFAULT_CLOSE_TIMEOUT_S
from opencode.storage.txn import REASON_STORED_DECODE_FAILED

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_SNAPSHOT_BODY = _SNAPSHOT.model_dump(mode="json")
_MODEL = ModelRef(provider="maas", model="test-model")
_TOKEN = "test-token-1234"
_TERMINAL = {"succeeded", "failed", "interrupted"}

#: A loopback client that never routes through a system proxy (the 054 form).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


# ---------------------------------------------------------------------------
# scripted provider (the test_httpapi_server.py form, gates trimmed away)
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
    """Fake Provider: one scripted stream per request; every request recorded."""

    def __init__(self, scripts: Sequence[Sequence[ProviderChunk | _Script]]) -> None:
        self.scripts = [
            [item for entry in script for item in (entry.items if isinstance(entry, _Script) else (entry,))]
            for script in scripts
        ]
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        self.close_count += 1


# ---------------------------------------------------------------------------
# app harness (the test_httpapi_server.py forms)
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
    close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
) -> App:
    """The full composition: provider wired into the service (tools unused here)."""
    return App(
        db_path,
        mode="create",
        working_dir="C:/work",
        provider=provider,
        settings=_settings(),
        close_timeout_s=close_timeout_s,
    )


def _plain_app(db_path: Path) -> App:
    """The provider-less composition: storage + service only."""
    return App(db_path, working_dir="C:/work")


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


# ---------------------------------------------------------------------------
# raw-ASGI request driver (the test_httpapi_server.py forms)
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


def _json_body(payload: dict[str, Any]) -> bytes:
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
    messages = [{"type": "http.request", "body": body, "more_body": False}] if body is not None else []
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
) -> tuple[int, dict[bytes, bytes], Any, _ReceiveRecorder]:
    status, headers, raw, receive = await _asgi_request(
        api, method, path, token=token, body=body, extra_headers=extra_headers, query_string=query_string
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


# ---------------------------------------------------------------------------
# the SSE streaming harness (parking receive + frame-collecting send)
# ---------------------------------------------------------------------------


def _sse_field(line: bytes, prefix: bytes) -> str:
    """One field value with the SSE rule applied: a single leading space is stripped."""
    value = line[len(prefix) :]
    if value.startswith(b" "):
        value = value[1:]
    return value.decode("utf-8")


def _parse_sse_frame(raw: bytes) -> dict[str, Any]:
    """One ``\\n\\n``-terminated block as ``{id, event, data, comments}``.

    Mirrors the SSE line rules: a leading colon is a comment (one optional
    space after it is stripped), ``id:``/``event:``/``data:`` are single-line
    fields (one optional space after the colon is stripped), and the data
    line is one JSON document.
    """
    frame: dict[str, Any] = {"id": None, "event": None, "data": None, "comments": []}
    for line in raw.split(b"\n"):
        if not line:
            continue
        if line.startswith(b":"):
            comment = line[1:].decode("utf-8", "replace")
            frame["comments"].append(comment[1:] if comment.startswith(" ") else comment)
        elif line.startswith(b"id:"):
            frame["id"] = _sse_field(line, b"id:")
        elif line.startswith(b"event:"):
            frame["event"] = _sse_field(line, b"event:")
        elif line.startswith(b"data:"):
            frame["data"] = json.loads(_sse_field(line, b"data:"))
        else:
            raise AssertionError(f"unknown SSE line: {line!r}")
    return frame


def _seq_of(frame: dict[str, Any]) -> int:
    """The frame's seq, off its ``id: {session_id}:{seq}`` line."""
    assert frame["id"] is not None
    return int(str(frame["id"]).rsplit(":", 1)[1])


def _payload(frame: dict[str, Any]) -> Any:
    """The event's business payload: the envelope's nested ``data`` object."""
    assert frame["data"] is not None
    return frame["data"]["data"]


def _event_frames(stream: _SseStream) -> list[dict[str, Any]]:
    return [frame for frame in stream.frames if frame["id"] is not None]


def _comments(stream: _SseStream) -> list[str]:
    return [comment for frame in stream.frames for comment in frame["comments"]]


def _synced_marker(comment: str) -> bool:
    return comment.startswith("synced ")


class _SseStream:
    """One hand-driven SSE connection over the raw ASGI app.

    The receive channel parks until :meth:`close` answers ``http.disconnect``
    (starlette's task-group path then cancels the stream, which unwinds the
    follow context); the send channel records the response start and
    accumulates body chunks, parsing complete frames as they arrive. With
    ``paced=True`` every body chunk also parks behind :meth:`release` — the
    slow-reader probe: the follow generator suspends at its yield while the
    gate holds, exactly like a TCP peer that stopped reading.
    """

    def __init__(
        self,
        api: Any,
        session_id: str,
        *,
        last_event_id: str | None = None,
        token: str | None = _TOKEN,
        origin: str | None = None,
        paced: bool = False,
    ) -> None:
        self._api = api
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if last_event_id is not None:
            headers.append((b"last-event-id", str(last_event_id).encode()))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        path = f"/session/{session_id}/events"
        self._scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 51235),
            "server": ("127.0.0.11", 4096),
        }
        self.status: int | None = None
        self.headers: dict[bytes, bytes] = {}
        self.frames: list[dict[str, Any]] = []
        self.finished = False
        self._buffer = b""
        self._closed = asyncio.Event()
        self._release = asyncio.Event()
        if not paced:
            self._release.set()
        self._task: asyncio.Task[None] | None = None

    def open(self) -> None:
        """Start the connection (the response streams asynchronously)."""
        self._task = asyncio.create_task(self._drive())

    async def _drive(self) -> None:
        await self._api(self._scope, self._receive, self._send)

    async def _receive(self) -> dict[str, Any]:
        await self._closed.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = {bytes(name).lower(): bytes(value) for name, value in message["headers"]}
        elif message["type"] == "http.response.body":
            if not self._release.is_set():
                await self._release.wait()
            self._buffer += bytes(message.get("body", b""))
            self._parse_frames()
            if not message.get("more_body", False):
                self.finished = True

    def _parse_frames(self) -> None:
        while b"\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            self.frames.append(_parse_sse_frame(raw))

    def release(self) -> None:
        """Unpark the paced send channel (no-op for an unpaced stream)."""
        self._release.set()

    async def close(self) -> None:
        """Disconnect: the ASGI call unwinds and the follow context exits."""
        self._release.set()
        self._closed.set()
        if self._task is not None:
            await self._task

    async def wait_started(self, *, timeout_s: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while self.status is None:
            assert asyncio.get_running_loop().time() < deadline, f"the stream never answered; frames: {self.frames}"
            await asyncio.sleep(0.01)

    async def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 5.0
    ) -> list[dict[str, Any]]:
        """Wait until at least one parsed frame satisfies ``predicate`` (bounded)."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            matches = [frame for frame in self.frames if predicate(frame)]
            if matches:
                return matches
            assert not self.finished, f"the stream closed without the wanted frame; frames: {self.frames}"
            assert asyncio.get_running_loop().time() < deadline, f"timeout waiting for the frame; frames: {self.frames}"
            await asyncio.sleep(0.01)

    async def wait_finished(self, *, timeout_s: float = 5.0) -> None:
        """Wait until the response terminates (the terminal empty body chunk)."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while not self.finished:
            assert asyncio.get_running_loop().time() < deadline, f"the stream never closed; frames: {self.frames}"
            await asyncio.sleep(0.01)


def _insert_bad_event_row(db_path: Path, session_id: str, seq: int) -> None:
    """One stored row the registry cannot decode (unknown type): the page fails closed."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, batch_id, batch_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt_corrupt_probe",
                session_id,
                seq,
                "py.corrupt.probe",
                1,
                int(time.time() * 1000),
                "{}",
                "{}",
                "bat_corrupt_probe",
                0,
            ),
        )
        connection.commit()


def _insert_big_event_row(db_path: Path, session_id: str, seq: int, run_id: str) -> None:
    """One *valid* ``py.text.finalized`` row whose text dwarfs MAX_SSE_FRAME_BYTES.

    The runner caps provider text streams at 1 MiB (§5.3 L276), so a > 4 MiB
    durable event cannot be produced through a real run; the frame-cap face
    is probed with a row the registry decodes cleanly, inserted behind the
    store's back exactly like the corruption fixture. ``py.text.finalized``
    is run-scoped, so the envelope metadata carries the real run's id.
    """
    payload = {"message_id": "msg_frame_cap_probe", "part_id": "p1", "ordinal": 0, "text": "a" * 4_300_000}
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, batch_id, batch_ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt_frame_cap_probe",
                session_id,
                seq,
                PY_TEXT_FINALIZED,
                1,
                int(time.time() * 1000),
                json.dumps(payload),
                json.dumps({"run_id": run_id}),
                "bat_frame_cap_probe",
                0,
            ),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# module faces: constants (ruling d/g/h)
# ---------------------------------------------------------------------------


def test_sse_transport_constants_are_pinned() -> None:
    assert SSE_KEEPALIVE_SECONDS == 15.0
    assert MAX_CONCURRENT_SSE_STREAMS == 8
    assert MAX_SSE_FRAME_BYTES == 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# the positive stream: catch-up, Synced, live, wire shape (L105/§4 L239-246)
# ---------------------------------------------------------------------------


async def test_stream_answers_the_sse_wire_shape_with_catchup_and_one_synced_marker(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            synced = await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            assert stream.status == 200
            assert stream.headers[b"content-type"] == b"text/event-stream; charset=utf-8"
            assert stream.headers[b"cache-control"] == b"no-store"
            # the catch-up replayed the whole committed log, strictly by seq, with no gaps
            events = _event_frames(stream)
            assert [_seq_of(frame) for frame in events] == list(range(1, len(events) + 1))
            assert events[0]["event"] == PY_SESSION_CREATED
            assert events[-1]["event"] == PY_RUN_FINISHED
            assert _payload(events[-1])["outcome"] == "succeeded"
            # exactly one Synced marker; it is a bare comment (no id, no event type) naming H
            assert [comment for comment in _comments(stream) if _synced_marker(comment)] == [
                f"synced {_seq_of(events[-1])}"
            ]
            assert synced[0]["id"] is None and synced[0]["event"] is None
        finally:
            await stream.close()


async def test_absent_or_zero_last_event_id_replays_from_the_beginning(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        for last_event_id in (None, f"{session_id}:0"):
            stream = _SseStream(api, session_id, last_event_id=last_event_id)
            stream.open()
            try:
                await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
                events = _event_frames(stream)
                assert events, "the seq-0 cursor replays the whole log"
                assert _seq_of(events[0]) == 1
                assert events[0]["event"] == PY_SESSION_CREATED
            finally:
                await stream.close()


async def test_last_event_id_resumes_strictly_after_the_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        stream = _SseStream(api, session_id, last_event_id=f"{session_id}:2")
        stream.open()
        try:
            await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            events = _event_frames(stream)
            assert events, "a mid-log cursor still has a remainder to stream"
            assert _seq_of(events[0]) == 3
            assert all(_seq_of(frame) >= 3 for frame in events)
            assert PY_SESSION_CREATED not in [frame["event"] for frame in events]
        finally:
            await stream.close()


async def test_a_cursor_at_the_high_watermark_is_the_live_only_tail(tmp_path: Path) -> None:
    """``seq == H`` is the boundary case: beyond-H is refused (400), at-H resumes live-only."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()], [_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        high = int(
            _fetch_all(db_path, "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,))[0][0]
        )
        stream = _SseStream(api, session_id, last_event_id=f"{session_id}:{high}")
        stream.open()
        try:
            synced = await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            assert synced[0]["comments"] == [f"synced {high}"]
            assert not _event_frames(stream)  # the whole committed log is behind the cursor
            # the stream is live: a second run's events arrive strictly after H, no replay
            ref2 = await _http_prompt(api, session_id, "more", "req_2")
            await stream.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED, timeout_s=10.0)
            events = _event_frames(stream)
            assert [_seq_of(frame) for frame in events] == list(range(high + 1, high + 1 + len(events)))
            await _await_http_status(api, str(ref2["run_id"]), _TERMINAL)
        finally:
            await stream.close()


async def test_a_failed_run_streams_its_failure_events_and_the_stream_stays_healthy(tmp_path: Path) -> None:
    """The durable-only face carries failure too: a failed run's events stream live, no error frame.

    An empty provider script produces a run that ends ``failed``/``interrupted``
    — the stream still replays the whole attempt gap-free and ends on
    ``py.run.finished`` with the same outcome the status face reports; a failed
    run is a normal durable payload, not a stream error.
    """
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[]])  # an empty script: no chunks, no finish -> the run fails
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            await stream.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED, timeout_s=10.0)
            final = await _await_http_status(api, str(ref["run_id"]), {"failed", "interrupted"})
            events = _event_frames(stream)
            seqs = [_seq_of(frame) for frame in events]
            assert seqs == list(range(1, len(seqs) + 1))  # no gaps even across the failure
            outcome = _payload(events[-1])["outcome"]
            assert outcome == final["status"]  # the streamed outcome mirrors the status face
            assert outcome != "succeeded"
            assert all(frame["event"] != "error" for frame in stream.frames)  # healthy stream, failed run
        finally:
            await stream.close()


async def test_reconnect_after_disconnect_receives_the_missed_events_without_overlap(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()], [_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        first = _SseStream(api, session_id)
        first.open()
        # read a prefix of the log, then drop the connection mid-stream
        await first.wait_for(lambda frame: frame["id"] is not None and _seq_of(frame) >= 2)
        seen_ids = [str(frame["data"]["id"]) for frame in _event_frames(first)]
        last_seq = _seq_of(_event_frames(first)[-1])
        await first.close()
        # a second run commits while the client is gone
        ref2 = await _http_prompt(api, session_id, "more", "req_2")
        await _await_http_status(api, str(ref2["run_id"]), _TERMINAL)
        # reconnect from the last seen frame id: the missed events arrive, nothing re-sent
        second = _SseStream(api, session_id, last_event_id=f"{session_id}:{last_seq}")
        second.open()
        try:
            await second.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED)
            events = _event_frames(second)
            seqs = [_seq_of(frame) for frame in events]
            assert seqs == list(range(last_seq + 1, last_seq + 1 + len(seqs)))
            assert not ({str(frame["data"]["id"]) for frame in events} & set(seen_ids))
            assert _payload(events[-1])["outcome"] == "succeeded"
        finally:
            await second.close()


async def test_stream_opened_right_after_prompt_sees_the_whole_run_with_no_gaps(tmp_path: Path) -> None:
    """The wakeup-before-H design: opening the stream immediately after the prompt loses nothing."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            await stream.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED, timeout_s=10.0)
            events = _event_frames(stream)
            seqs = [_seq_of(frame) for frame in events]
            assert seqs == list(range(1, len(seqs) + 1))
            envelope_ids = [str(frame["data"]["id"]) for frame in events]
            assert len(set(envelope_ids)) == len(envelope_ids)  # the evt_ dedup anchor
            assert _payload(events[-1])["outcome"] == "succeeded"
            assert [comment for comment in _comments(stream) if _synced_marker(comment)]
        finally:
            await stream.close()
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)  # settle before close


async def test_event_frames_carry_the_full_envelope_with_the_client_dedup_key(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            events = _event_frames(stream)
            assert events
            for frame in events:
                envelope = frame["data"]
                assert isinstance(envelope, dict)
                assert set(envelope) == {
                    "id",
                    "type",
                    "version",
                    "session_id",
                    "created_at",
                    "data",
                    "metadata",
                    "seq",
                }
                assert str(envelope["id"]).startswith("evt_")
                assert envelope["session_id"] == session_id
                assert envelope["type"] == frame["event"]
                assert envelope["seq"] == _seq_of(frame)
                assert frame["id"] == f"{session_id}:{envelope['seq']}"
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# Last-Event-ID rejections (ruling c: malformed / cross-session / ahead-of-H)
# ---------------------------------------------------------------------------


async def test_malformed_last_event_id_answers_400_invalid_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        for header in (
            "garbage",  # no colon
            f"{session_id}:",  # empty seq
            f"{session_id}:abc",  # non-integer
            f"{session_id}:-1",  # negative
            f"{session_id}:1.5",  # fractional
            f"{session_id}: 1",  # padded seq
            ":5",  # empty session
        ):
            status, _, payload, _ = await _asgi_json(
                api, "GET", f"/session/{session_id}/events", extra_headers=[(b"last-event-id", header.encode())]
            )
            assert status == 400, header
            _assert_core_error(payload, CoreErrorCode.INVALID_CURSOR, retryable=False)
            assert payload["details"]["reason"] == "last_event_id_malformed"


async def test_cross_session_last_event_id_answers_400(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        first = await _http_create_session(api)
        second = await _http_create_session(api)
        status, _, payload, _ = await _asgi_json(
            api, "GET", f"/session/{first}/events", extra_headers=[(b"last-event-id", f"{second}:1".encode())]
        )
        assert status == 400
        _assert_core_error(payload, CoreErrorCode.INVALID_CURSOR, retryable=False)
        assert payload["details"]["reason"] == "last_event_id_session_mismatch"
        assert payload["details"]["cursor_session_id"] == second


async def test_last_event_id_beyond_the_high_watermark_answers_400_without_waiting(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        high = int(
            _fetch_all(db_path, "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,))[0][0]
        )
        started = time.monotonic()
        status, _, payload, _ = await _asgi_json(
            api,
            "GET",
            f"/session/{session_id}/events",
            extra_headers=[(b"last-event-id", f"{session_id}:{high + 5}".encode())],
        )
        assert status == 400
        _assert_core_error(payload, CoreErrorCode.INVALID_CURSOR, retryable=False)
        assert payload["details"]["reason"] == "cursor_seq_beyond_high_watermark"
        assert payload["details"]["high_watermark"] == high
        assert time.monotonic() - started < 2.0  # an explicit refusal, never an empty wait


async def test_unknown_session_answers_404(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(api, "GET", "/session/ses_missing/events")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


# ---------------------------------------------------------------------------
# the shared guard: 401 / 403 / read-only sessions
# ---------------------------------------------------------------------------


async def test_the_sse_route_shares_the_guard_401_and_403(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        status, headers, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/events", token=None)
        assert status == 401
        assert headers.get(b"www-authenticate") == b"Bearer"
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        status, _, payload, _ = await _asgi_json(
            api, "GET", f"/session/{session_id}/events", extra_headers=[(b"origin", b"http://127.0.0.1:5173")]
        )
        assert status == 403
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)


async def test_read_only_diagnostic_session_answers_500_at_connect(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        with sqlite3.connect(db_path) as connection:  # the quarantine fixture: flip the status
            connection.execute(
                "UPDATE sessions SET status = 'read_only_diagnostic' WHERE session_id = ?", (session_id,)
            )
            connection.commit()
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/events")
        assert status == 500
        _assert_core_error(payload, CoreErrorCode.STORAGE_CORRUPT, retryable=False)
        assert payload["details"]["reason"] == "session_read_only_diagnostic"


# ---------------------------------------------------------------------------
# keepalive and the slow consumer (ruling d/f: L108 pull model)
# ---------------------------------------------------------------------------


async def test_keepalive_comments_flow_on_an_idle_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opencode.httpapi.app.SSE_KEEPALIVE_SECONDS", 0.05)
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            await stream.wait_for(lambda frame: "keepalive" in frame["comments"])
            # the cadence keeps flowing while the stream idles (polled, not raced)
            await stream.wait_for(
                lambda frame: len([comment for comment in _comments(stream) if comment == "keepalive"]) >= 2
            )
            assert all(
                frame["id"] is None and frame["event"] is None
                for frame in stream.frames
                if "keepalive" in frame["comments"]
            )  # a bare comment
            # the stream is still healthy after idling: a live run flows through
            ref = await _http_prompt(api, session_id, "hello", "req_1")
            await stream.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED, timeout_s=10.0)
            await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        finally:
            await stream.close()


async def test_a_slow_sse_reader_never_blocks_the_runner(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        stream = _SseStream(api, session_id, paced=True)  # body chunks park behind release
        stream.open()
        try:
            await stream.wait_started()
            assert stream.status == 200
            # the run reaches its terminal state while the stream is parked at its first chunk
            ref = await _http_prompt(api, session_id, "hello", "req_1")
            final = await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
            assert final["status"] == "succeeded"
            # unpark: the suspended generator now drains the whole log, with no gaps
            stream.release()
            await stream.wait_for(lambda frame: frame["event"] == PY_RUN_FINISHED, timeout_s=5.0)
            seqs = [_seq_of(frame) for frame in _event_frames(stream)]
            assert seqs == list(range(1, len(seqs) + 1))
        finally:
            stream.release()
            await stream.close()


# ---------------------------------------------------------------------------
# the stream concurrency budget (ruling g: two independent 429 faces)
# ---------------------------------------------------------------------------


async def test_the_stream_budget_is_independent_of_the_request_budget(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        streams = [_SseStream(api, session_id) for _ in range(MAX_CONCURRENT_SSE_STREAMS)]
        try:
            for stream in streams:
                stream.open()
            for stream in streams:
                await stream.wait_started()
                assert stream.status == 200
            # the ninth stream is refused by the stream budget alone
            status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/events")
            assert status == 429
            _assert_core_error(payload, CoreErrorCode.SLOW_CONSUMER, retryable=True)
            # the request face keeps serving with all eight stream slots held
            status, _, payload, _ = await _asgi_json(api, "POST", "/session", body=_json_body(_SNAPSHOT_BODY))
            assert status == 200
            status, _, payload, _ = await _asgi_json(api, "GET", "/run/run_000001/status")
            assert status == 404  # answered (unknown run), not starved into a 429
        finally:
            for stream in streams:
                await stream.close()
        # the stream slots are released on disconnect: a fresh stream is admitted again
        fresh = _SseStream(api, session_id)
        fresh.open()
        try:
            await fresh.wait_started()
            assert fresh.status == 200
        finally:
            await fresh.close()


# ---------------------------------------------------------------------------
# frame cap and mid-stream failures (ruling h/i)
# ---------------------------------------------------------------------------


async def test_an_oversize_event_frame_answers_an_error_frame_and_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)  # the run itself succeeds
        high = int(
            _fetch_all(db_path, "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?", (session_id,))[0][0]
        )
        # the > 4 MiB event lands behind the store's back: the runner's 1 MiB
        # provider-text cap makes it unreachable through a real prompt
        _insert_big_event_row(db_path, session_id, high + 1, str(ref["run_id"]))
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            errors = await stream.wait_for(lambda frame: frame["event"] == "error", timeout_s=10.0)
            await stream.wait_finished()
            core = errors[0]["data"]
            assert core["code"] == CoreErrorCode.VALIDATION_ERROR.value
            assert core["retryable"] is False
            assert stream.frames[-1] is errors[0]  # the error frame is the last thing on the wire
            # the healthy catch-up before the oversized event flowed normally
            healthy = _event_frames(stream)
            assert [_seq_of(frame) for frame in healthy] == list(range(1, high + 1))
            assert healthy[0]["event"] == PY_SESSION_CREATED
        finally:
            await stream.close()


async def test_a_mid_stream_storage_failure_answers_an_error_frame_and_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
        stream = _SseStream(api, session_id)
        stream.open()
        try:
            synced = await stream.wait_for(lambda frame: any(_synced_marker(c) for c in frame["comments"]))
            high = int(synced[0]["comments"][0].removeprefix("synced "))
            healthy = len(_event_frames(stream))
            # an undecodable row lands behind the store's back: the next live page fails closed
            _insert_bad_event_row(db_path, session_id, high + 1)
            errors = await stream.wait_for(lambda frame: frame["event"] == "error", timeout_s=10.0)
            await stream.wait_finished()
            core = errors[0]["data"]
            assert core["code"] == CoreErrorCode.STORAGE_CORRUPT.value
            assert core["retryable"] is False
            assert core["details"]["reason"] == REASON_STORED_DECODE_FAILED
            assert core["details"]["first_bad_seq"] == high + 1
            # the healthy events yielded before the bad page stay valid on the wire
            assert len(_event_frames(stream)) == healthy
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# the one real uvicorn smoke test (L110 form: the SSE face over a real socket)
# ---------------------------------------------------------------------------


def _urllib_call(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    data = _json_body(body) if body is not None else None
    headers: dict[str, str] = {"Authorization": f"Bearer {_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=10) as response:
            payload: Any = json.loads(response.read())
            lowered = {name.lower(): value for name, value in response.headers.items()}
            return int(response.status), lowered, payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        payload = json.loads(raw) if raw else None
        lowered = {name.lower(): value for name, value in error.headers.items()}
        return int(error.code), lowered, payload if isinstance(payload, dict) else None


def test_uvicorn_smoke_serves_the_sse_stream_over_the_real_wire(tmp_path: Path) -> None:
    """One programmatic uvicorn.Server on a worker thread: catch-up, live push, headers (④)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    app = _run_app(db_path, provider)
    api = create_app(app, token=_TOKEN)
    config = _server_config(api, port=0)  # loopback host, ephemeral port
    assert config.workers == 1 and config.reload is False
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), name="opencode-http-sse-smoke")
    thread.start()
    try:
        deadline = time.monotonic() + 30.0
        while not server.started:
            assert thread.is_alive(), "the smoke server thread died during startup"
            assert time.monotonic() < deadline, "the smoke server never started"
            time.sleep(0.05)
        sockets = server.servers[0].sockets
        assert sockets is not None
        address = sockets[0].getsockname()
        assert address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{int(address[1])}"

        status, _, session_payload = _urllib_call(base, "POST", "/session", _SNAPSHOT_BODY)
        assert status == 200 and session_payload is not None
        session_id = str(session_payload["session_id"])

        request = urllib.request.Request(
            base + f"/session/{session_id}/events",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            method="GET",
        )
        frames: list[dict[str, Any]] = []
        buffer = b""

        def _pump(wanted: Callable[[list[dict[str, Any]]], bool], timeout_s: float) -> None:
            nonlocal buffer
            deadline = time.monotonic() + timeout_s
            while not wanted(frames):
                assert time.monotonic() < deadline, f"the smoke stream stalled; frames so far: {frames}"
                line = response.readline()
                assert line, f"the smoke stream ended early; frames so far: {frames}"
                buffer += line
                if buffer.endswith(b"\n\n"):
                    frames.append(_parse_sse_frame(buffer[:-2]))
                    buffer = b""

        with _OPENER.open(request, timeout=60) as response:
            lowered = {name.lower(): value for name, value in response.headers.items()}
            assert int(response.status) == 200
            assert lowered["content-type"] == "text/event-stream; charset=utf-8"
            assert lowered["cache-control"] == "no-store"
            # the catch-up replays session.created, then the marker confirms the window
            _pump(lambda fs: any(frame["event"] == PY_SESSION_CREATED for frame in fs), 30.0)
            # with the stream open over the real socket, a prompt must arrive as live push
            status, _, ref = _urllib_call(
                base, "POST", f"/session/{session_id}/prompt", {"text": "hello", "request_id": "req_1"}
            )
            assert status == 200 and ref is not None
            _pump(lambda fs: any(frame["event"] == PY_RUN_FINISHED for frame in fs), 30.0)
            events = [frame for frame in frames if frame["id"] is not None]
            seqs = [_seq_of(frame) for frame in events]
            assert seqs == list(range(1, len(seqs) + 1))
            assert _payload(events[-1])["outcome"] == "succeeded"
            assert [comment for frame in frames for comment in frame["comments"] if _synced_marker(comment)]
        # closing the response closes the socket: the server cancels the stream on disconnect
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    assert not thread.is_alive(), "the smoke server did not shut down"
    assert app.service.closed is True  # the ASGI lifespan closed the App on the way down
