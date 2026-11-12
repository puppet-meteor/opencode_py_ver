"""Integration tests for the five /jobs HTTP routes (T-C1-076, P3-S1).

Drives the real ASGI app over a migrated, provider-less App composition (the
test_httpapi_sse.py raw-ASGI harness forms): the POST /jobs structural
validation set, the status wire, the cancel idempotency, the gate-reply 409
shell, the 401 auth face, and the SSE trace pass-through over the
``ses_job_{job_id}`` stream (catch-up + live + Last-Event-ID resume + the
400 cursor faces + the stream-slot routing of ``_is_sse_request``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencode.app import App
from opencode.httpapi import create_app
from opencode.httpapi.app import _is_sse_request
from opencode.storage.jobs_store import job_stream_key

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "test-token-1234"


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


class _ReceiveDisconnect:
    async def __call__(self) -> dict[str, Any]:
        await asyncio.Event().wait()  # never resolves: the client never sends more
        raise AssertionError("unreachable")


class _ReceiveParked:
    """Receive channel that parks until the test disconnects (the SSE driver form)."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    def disconnect(self) -> None:
        self._closed.set()

    async def __call__(self) -> dict[str, Any]:
        await self._closed.wait()
        return {"type": "http.disconnect"}


async def _asgi_request(
    api: Any,
    method: str,
    path: str,
    *,
    token: str | None = _TOKEN,
    body: bytes | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
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
        "query_string": b"",
        "root_root": "",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51236),
        "server": ("127.0.0.11", 4096),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}] if body is not None else []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await api(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    status = int(start["status"])
    response_headers = {bytes(name).lower(): bytes(value) for name, value in start["headers"]}
    response_body = b"".join(
        bytes(message.get("body", b"")) for message in sent if message["type"] == "http.response.body"
    )
    return status, response_headers, response_body


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


class _JobStream:
    """Task-driven SSE connection (the test_httpapi_sse.py _Stream form, trimmed)."""

    def __init__(self, api: Any, job_id: str, *, last_event_id: str | None = None, token: str | None = _TOKEN) -> None:
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if last_event_id is not None:
            headers.append((b"last-event-id", last_event_id.encode()))
        path = f"/jobs/{job_id}/events"
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
            "client": ("127.0.0.1", 51237),
            "server": ("127.0.0.11", 4096),
        }
        self._api = api
        self._receive = _ReceiveParked()
        self.status: int | None = None
        self.frames: list[dict[str, Any]] = []
        self.finished = False
        self._buffer = b""
        self._task: asyncio.Task[None] | None = None

    def open(self) -> None:
        self._task = asyncio.create_task(self._drive())

    async def _drive(self) -> None:
        await self._api(self._scope, self._receive, self._send)

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
        elif message["type"] == "http.response.body":
            self._buffer += bytes(message.get("body", b""))
            self._parse_frames()
            if not message.get("more_body", False):
                self.finished = True

    def _parse_frames(self) -> None:
        while b"\n\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n\n", 1)
            frame: dict[str, Any] = {"id": None, "event": None, "data": None, "comments": []}
            for line in raw.decode("utf-8").split("\n"):
                if line.startswith(":"):
                    frame["comments"].append(line[1:].strip())
                elif line.startswith("id:"):
                    frame["id"] = line[3:].strip()
                elif line.startswith("event:"):
                    frame["event"] = line[6:].strip()
                elif line.startswith("data:"):
                    frame["data"] = json.loads(line[5:].strip())
            self.frames.append(frame)

    async def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 5.0
    ) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            matches = [frame for frame in self.frames if predicate(frame)]
            if matches:
                return matches
            assert asyncio.get_running_loop().time() < deadline, f"timeout; frames: {self.frames}"
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self._receive.disconnect()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task


async def _plain_app(db_path: Path) -> App:
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\n{result.stderr}"
    return App(db_path, working_dir="C:/work")


async def _wait_status(api: Any, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        _status, _headers, body = await _asgi_request(api, "GET", f"/jobs/{job_id}")
        payload = json.loads(body)
        assert payload["status"] in statuses | {"created", "running"}, payload
        if payload["status"] in statuses:
            return payload
        assert asyncio.get_running_loop().time() < deadline, f"job never settled: {payload}"
        await asyncio.sleep(0.01)


class TestPostJobsValidation:
    async def test_submit_returns_the_id_and_the_run_settles(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            status, _headers, body = await _asgi_request(
                api, "POST", "/jobs", body=_json_body({"pipeline_id": "research", "params": {"query": "hi"}})
            )
            assert status == 200
            job_id = json.loads(body)["id"]
            assert job_id.startswith("job_")
            payload = await _wait_status(api, job_id, {"done"})
            assert payload["pipeline_id"] == "research"
            assert payload["result"] == {"executor": "fake"}
        finally:
            await app.close()

    async def test_structural_rejections_answer_422(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            cases: list[dict[str, Any] | bytes] = [
                _json_body({"params": {}}),  # pipeline_id missing
                _json_body({"pipeline_id": "", "params": {}}),  # empty pipeline_id
                _json_body({"pipeline_id": "p"}),  # params missing
                _json_body({"pipeline_id": "p", "params": [1, 2]}),  # params not an object
                _json_body({"pipeline_id": "p", "params": {}, "session_anchor": "worker"}),  # bad anchor
                _json_body({"pipeline_id": "p", "params": {}, "unexpected": 1}),  # unknown field
                b"not-json",  # malformed body
            ]
            for case in cases:
                body = case if isinstance(case, bytes) else _json_body(case)
                status, _headers, raw = await _asgi_request(api, "POST", "/jobs", body=body)
                assert status == 422, raw
                assert json.loads(raw)["code"] == "ValidationError"
            # the ses_-anchored form is accepted
            status, _h, raw = await _asgi_request(
                api,
                "POST",
                "/jobs",
                body=_json_body({"pipeline_id": "p", "params": {}, "session_anchor": "ses_0001"}),
            )
            assert status == 200
            assert json.loads(raw)["id"].startswith("job_")
        finally:
            await app.close()


class TestJobStatusAndCancel:
    async def test_unknown_job_answers_404(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            for method, path in (("GET", "/jobs/job_missing"), ("POST", "/jobs/job_missing/cancel")):
                status, _headers, body = await _asgi_request(api, method, path)
                assert status == 404
                assert json.loads(body)["code"] == "NotFound"
        finally:
            await app.close()

    async def test_cancel_is_idempotent_and_terminal_wins(self, tmp_path: Path) -> None:
        db_path = tmp_path / "api.db"
        app = await _plain_app(db_path)
        api = create_app(app, token=_TOKEN)
        try:
            handle = await app.jobs.submit("research", {}, "api")
            # cancel before any run: created -> cancelled, and the second call is a no-op.
            status, _headers, body = await _asgi_request(api, "POST", f"/jobs/{handle.job_id}/cancel")
            assert status == 200
            assert json.loads(body)["status"] == "cancelled"
            stream = job_stream_key(handle.job_id)
            events_before = _fetch_all(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (stream,))[0][0]
            status, _headers, body = await _asgi_request(api, "POST", f"/jobs/{handle.job_id}/cancel")
            assert json.loads(body)["status"] == "cancelled"
            events_after = _fetch_all(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (stream,))[0][0]
            assert events_after == events_before  # idempotent: no duplicate terminal event
        finally:
            await app.close()


class TestGateReplyShell:
    async def test_body_enum_first_then_404_then_409(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            # unknown fields / bad enum -> 422 (validated first, the wire contract is real)
            for bad in ({"reply": "explode"}, {"reply": "approve", "extra": 1}, {}):
                status, _headers, raw = await _asgi_request(
                    api, "POST", "/jobs/job_x/gate/gate_1/reply", body=_json_body(bad)
                )
                assert status == 422, raw
            # unknown job -> 404 (after body validation)
            status, _headers, raw = await _asgi_request(
                api, "POST", "/jobs/job_x/gate/gate_1/reply", body=_json_body({"reply": "approve"})
            )
            assert status == 404
            # existing job -> the S5a shell: 409 Conflict, nothing applied
            handle = await app.jobs.submit("research", {}, "api")
            status, _headers, raw = await _asgi_request(
                api, "POST", f"/jobs/{handle.job_id}/gate/gate_1/reply", body=_json_body({"reply": "approve"})
            )
            assert status == 409
            payload = json.loads(raw)
            assert payload["code"] == "Conflict"
            assert payload["details"]["gate_id"] == "gate_1"
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "created"  # no reply was applied
        finally:
            await app.close()


class TestAuthFace:
    async def test_missing_token_answers_401_on_every_jobs_route(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            status, headers, _body = await _asgi_request(
                api, "POST", "/jobs", token=None, body=_json_body({"pipeline_id": "p", "params": {}})
            )
            assert status == 401
            assert headers[b"www-authenticate"] == b"Bearer"
            handle = await app.jobs.submit("research", {}, "api")
            for method, path in (
                ("GET", f"/jobs/{handle.job_id}"),
                ("GET", f"/jobs/{handle.job_id}/events"),
                ("POST", f"/jobs/{handle.job_id}/cancel"),
            ):
                status, _headers, _raw = await _asgi_request(api, method, path, token=None)
                assert status == 401
        finally:
            await app.close()


class TestJobsSse:
    async def test_stream_replays_the_lifecycle_and_the_live_pass_through(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            # submit without spawning: the stream opens over a created job.
            handle = await app.jobs.submit("research", {"query": "hi"}, "api")
            stream_key = job_stream_key(handle.job_id)
            client = _JobStream(api, handle.job_id)
            client.open()
            await client.wait_for(lambda frame: any("synced" in comment for comment in frame["comments"]))
            # catch-up: created + spec_bound are already on the wire
            await client.wait_for(lambda frame: frame["event"] == "py.job.spec_bound")
            # live: drive the fake run inline; started/finished must arrive on the open stream
            await app.jobs.run(handle.job_id)
            await client.wait_for(lambda frame: frame["event"] == "py.job.finished", timeout_s=10.0)
            ids = [frame["id"] for frame in client.frames if frame["id"] is not None]
            assert ids == [f"{stream_key}:1", f"{stream_key}:2", f"{stream_key}:3", f"{stream_key}:4"]
            finished = next(frame for frame in client.frames if frame["event"] == "py.job.finished")
            assert finished["data"]["data"]["trace"]["trace_id"].startswith("trc_")
            assert finished["data"]["session_id"] == stream_key
        finally:
            await client.close()
            await app.close()

    async def test_last_event_id_resumes_and_the_cursor_faces_answer_400(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            handle = await app.jobs.submit("research", {}, "api")
            await app.jobs.run(handle.job_id)
            stream_key = job_stream_key(handle.job_id)
            client = _JobStream(api, handle.job_id, last_event_id=f"{stream_key}:2")
            client.open()
            frames = await client.wait_for(lambda frame: frame["event"] == "py.job.finished")
            assert [frame["id"] for frame in frames] == [f"{stream_key}:4"]
            await client.close()
            # malformed + cross-stream cursors are transport-level 400s
            status, _h, raw = await _asgi_request(
                api, "GET", f"/jobs/{handle.job_id}/events", extra_headers=[(b"last-event-id", b"garbage")]
            )
            assert status == 400
            assert json.loads(raw)["code"] == "InvalidCursor"
            status, _h, raw = await _asgi_request(
                api,
                "GET",
                f"/jobs/{handle.job_id}/events",
                extra_headers=[(b"last-event-id", b"ses_other:1")],
            )
            assert status == 400
            # a cursor beyond H is the follow-entry 400 (never an empty wait)
            status, _h, raw = await _asgi_request(
                api,
                "GET",
                f"/jobs/{handle.job_id}/events",
                extra_headers=[(b"last-event-id", f"{stream_key}:99".encode())],
            )
            assert status == 400
        finally:
            await app.close()

    async def test_unknown_job_events_answers_404_before_the_stream(self, tmp_path: Path) -> None:
        app = await _plain_app(tmp_path / "api.db")
        api = create_app(app, token=_TOKEN)
        try:
            status, _headers, raw = await _asgi_request(api, "GET", "/jobs/job_none/events")
            assert status == 404
            assert json.loads(raw)["code"] == "NotFound"
        finally:
            await app.close()


class TestStreamSlotRouting:
    """``_is_sse_request`` generalization: both stream families hold stream slots."""

    @staticmethod
    def _scope(method: str, path: str) -> dict[str, Any]:
        return {"type": "http", "method": method, "path": path}

    def test_the_two_stream_families_and_only_them(self) -> None:
        assert _is_sse_request(self._scope("GET", "/session/ses_1/events"))
        assert _is_sse_request(self._scope("GET", "/jobs/job_1/events"))
        assert not _is_sse_request(self._scope("POST", "/jobs/job_1/events"))
        assert not _is_sse_request(self._scope("GET", "/jobs/job_1"))
        assert not _is_sse_request(self._scope("GET", "/jobs/job_1/cancel"))
        assert not _is_sse_request(self._scope("GET", "/jobs"))
        assert not _is_sse_request(self._scope("GET", "/run/run_1/status"))
