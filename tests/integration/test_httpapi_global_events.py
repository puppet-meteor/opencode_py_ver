"""T-C1-095 (P6-S2) integration tests: the global volatile event stream face.

The twentieth route over one :class:`~opencode.app.App` composition, through
the transport in :mod:`opencode.httpapi.app`: ``GET /events`` streams the
volatile global broadcast — every live bus notification across every session
(durable committed + ephemeral) — over
:meth:`opencode.bus.bus.VolatileBus.subscribe_all`. Contract anchors: the
ticket T-C1-095 §3 (the scheduler's pre-rulings: PY plural-collection route
name vs TS ``/api/event``, comma-separated ``?types=`` with 422 strict form,
the bus's DEFAULT_QUEUE_LIMIT 1000 as the TS-4096 replacement, the synthetic
``server.connected`` first frame, ``_sse_frames`` keepalive discipline, the
``_GuardMiddleware`` stream budget covering the route structurally); the TS
authority the ticket quotes (``packages/protocol/src/groups/event.ts``:
"Volatile by contract: a slow consumer overflows and fails the stream, and
events during disconnection are missed";
``packages/server/src/handlers/event.ts``: the initial ``server.connected``
event before the live stream; ``packages/server/src/event-feed.ts``: the
subscriber-level type filter and the overflow-fails-the-stream face).

The design separation under test: this stream is *volatile* — no replay, no
backfill, no resume (a Last-Event-ID header is refused 422, never honored) —
while ``GET /session/{id}/events`` (test_httpapi_sse.py) stays the *durable*
catch-up + follow face; the two never share machinery beyond the frame
discipline itself.

④ disclosures: the harness is the test_httpapi_sse.py raw-ASGI driver,
imported cross-file (the T-C1-094 precedent — test_httpapi_session_resources
imports the same way), with one subclass building the ``/events`` scope; the
slow-consumer probe composes the App over ``VolatileBus(queue_limit=2)``
(the bus's own constructor face — the route itself always uses the default
1000) and parks the stream's send channel behind ``release`` exactly like
the paced session-SSE probe; the keepalive test patches
``SSE_KEEPALIVE_SECONDS``; the one SDK test boots one real programmatic
uvicorn server on a worker thread (the test_sdk_client form). The SDK
parser's fail-closed faces get one offline unit probe — a healthy wire
cannot produce them (the test_sdk_client ruling-e precedent).

DIFFERENT-BY-DESIGN (ticket §3/§5, recorded on the progress doc): route name
``/events`` not ``/api/event``; queue capacity 1000 not 4096; no
isOpenCodeEvent public-face filter (every bus event broadcasts); no TS
rpc.* / location faces; PY keeps its ``Cache-Control: no-store`` header form
and adds none of the TS X-Accel-Buffering/nosniff headers.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.bus import VolatileBus
from opencode.contracts import CoreErrorCode
from opencode.contracts.events import (
    PY_PROMPT_ACCEPTED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_SESSION_CREATED,
    PY_TEXT_DELTA,
    DurableEvent,
    EphemeralEvent,
    TextDeltaData,
    default_event_registry,
)
from opencode.httpapi import create_app
from opencode.httpapi.app import SERVER_CONNECTED, _is_sse_request
from opencode.sdk import OpencodeClient
from opencode.sdk.errors import OpencodeClientError, OpencodeStreamError
from opencode.sdk.sse import SERVER_CONNECTED as SDK_SERVER_CONNECTED
from opencode.sdk.sse import ServerConnected, stream_global_events
from tests.integration.test_httpapi_sse import (
    _SNAPSHOT_BODY,
    _TOKEN,
    _asgi_json,
    _assert_core_error,
    _await_http_status,
    _comments,
    _final_script,
    _http_create_session,
    _http_prompt,
    _plain_app,
    _require_success,
    _run_app,
    _ScriptedProvider,
    _settings,
    _SseStream,
    _upgrade_head,
)
from tests.integration.test_sdk_client import (
    _STREAM_TIMEOUT_S,
    _boot_server,
    _frame_lines,
    _KeyedProvider,
    _probe_event,
    _stop_server,
)

# ---------------------------------------------------------------------------
# the /events harness: the test_httpapi_sse.py driving machinery, one scope
# ---------------------------------------------------------------------------


class _GlobalSseStream(_SseStream):
    """One hand-driven ``GET /events`` connection (the _SseStream machinery).

    Same parking receive channel, frame-collecting send channel and paced
    slow-reader gate as the session-stream harness; only the scope differs —
    the bare ``/events`` path with its optional ``?types=`` query (given as
    the raw, pre-encoded comma-separated value).
    """

    def __init__(
        self,
        api: Any,
        *,
        types: str | None = None,
        token: str | None = _TOKEN,
        origin: str | None = None,
        paced: bool = False,
    ) -> None:
        self._api = api
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        query = b""
        if types is not None:
            query = b"types=" + urllib.parse.quote(types, safe=",").encode("utf-8")
        self._scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": query,
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 51236),
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


def _bus_frames(stream: _SseStream) -> list[dict[str, Any]]:
    """The bus event frames on the stream: durable (id line) or ephemeral (none).

    The synthetic ``server.connected`` preface and the terminal ``error`` frame are
    stream-level frames, not bus events, and are excluded on both counts.
    """
    return [
        frame
        for frame in stream.frames
        if frame["event"] is not None and frame["event"] not in {SERVER_CONNECTED, "error"}
    ]


def _durable_frames(stream: _SseStream) -> list[dict[str, Any]]:
    return [frame for frame in _bus_frames(stream) if frame["id"] is not None]


def _ephemeral_frames(stream: _SseStream) -> list[dict[str, Any]]:
    return [frame for frame in _bus_frames(stream) if frame["id"] is None]


def _small_queue_app(db_path: Path, provider: _ScriptedProvider) -> App:
    """The full composition over a two-slot bus (the slow-consumer probe).

    The route itself always subscribes with the bus's constructor-wide queue
    limit (DEFAULT_QUEUE_LIMIT 1000 in production); shrinking it here is the
    bus's own injection face, so the overflow probe needs no route-level knob.
    """
    return App(
        db_path,
        mode="create",
        working_dir="C:/work",
        provider=provider,
        settings=_settings(),
        bus=VolatileBus(queue_limit=2),
    )


# ---------------------------------------------------------------------------
# §4-1: the server.connected first frame + the live wire shape
# ---------------------------------------------------------------------------


async def test_connection_answers_server_connected_first_then_live_frames(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        stream = _GlobalSseStream(api)
        stream.open()
        try:
            connected = await stream.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            assert stream.status == 200
            assert stream.headers[b"content-type"] == b"text/event-stream; charset=utf-8"
            assert stream.headers[b"cache-control"] == b"no-store"  # the PY SSE header form
            # the synthetic marker is first on the wire, exactly once, with the TS payload
            assert stream.frames[0] is connected[0]
            assert connected[0]["id"] is None  # no id line: nothing advances a Last-Event-ID
            assert connected[0]["data"] == {"type": SERVER_CONNECTED, "data": {}}
            # a live event follows: a fresh session's creation rides the global stream
            session_id = await _http_create_session(api)
            created = await stream.wait_for(lambda frame: frame["event"] == PY_SESSION_CREATED)
            assert created[0]["data"]["session_id"] == session_id
            # the durable frame form is the session-stream form exactly: id {session_id}:{seq}
            assert created[0]["id"] == f"{session_id}:1"
            markers = [frame for frame in stream.frames if frame["event"] == SERVER_CONNECTED]
            assert len(markers) == 1
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# §4-2: the cross-session broadcast (one stream, both sessions, both envelopes)
# ---------------------------------------------------------------------------


async def test_one_global_stream_broadcasts_across_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()], [_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        stream = _GlobalSseStream(api)
        stream.open()
        try:
            await stream.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            first = await _http_create_session(api)
            ref_one = await _http_prompt(api, first, "hello one", "req_global_1")
            second = await _http_create_session(api)
            ref_two = await _http_prompt(api, second, "hello two", "req_global_2")
            await _await_http_status(api, str(ref_one["run_id"]), {"succeeded", "failed", "interrupted"})
            await _await_http_status(api, str(ref_two["run_id"]), {"succeeded", "failed", "interrupted"})
            await stream.wait_for(
                lambda frame: frame["event"] == PY_RUN_FINISHED and frame["data"]["session_id"] == second
            )
            # both sessions literally ride the one stream; an omitted types filter receives every type
            events = _bus_frames(stream)
            assert {frame["data"]["session_id"] for frame in events} == {first, second}
            types = {frame["event"] for frame in events}
            assert {PY_SESSION_CREATED, PY_PROMPT_ACCEPTED, PY_RUN_FINISHED} <= types
            # durable frames keep the session-stream wire shape: id + event + the 8-field envelope
            for frame in _durable_frames(stream):
                envelope = frame["data"]
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
                assert frame["id"] == f"{envelope['session_id']}:{envelope['seq']}"
                assert frame["event"] == envelope["type"]
            # ephemeral bus events ride too: the run's text delta has no seq, so no id line
            ephemeral = _ephemeral_frames(stream)
            assert any(frame["event"] == PY_TEXT_DELTA for frame in ephemeral)
            for frame in ephemeral:
                assert frame["id"] is None
                assert set(frame["data"]) == {"id", "type", "session_id", "created_at", "data", "metadata"}
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# §4-3: the ?types= server-side filter
# ---------------------------------------------------------------------------


async def test_types_query_filters_the_stream_server_side(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        stream = _GlobalSseStream(api, types=PY_PROMPT_ACCEPTED)
        stream.open()
        try:
            await stream.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            session_id = await _http_create_session(api)  # py.session.created: filtered out
            ref = await _http_prompt(api, session_id, "filtered hello", "req_global_filter")
            await _await_http_status(api, str(ref["run_id"]), {"succeeded", "failed", "interrupted"})
            accepted = await stream.wait_for(lambda frame: frame["event"] == PY_PROMPT_ACCEPTED)
            assert accepted[0]["data"]["session_id"] == session_id
            # the run settled, so every filtered-out notification already happened:
            # only the one type ever arrived — no session.created, no run events, no deltas
            events = _bus_frames(stream)
            assert events
            assert all(frame["event"] == PY_PROMPT_ACCEPTED for frame in events)
            assert not any(frame["event"] == PY_SESSION_CREATED for frame in events)
            assert not any(frame["event"] == PY_TEXT_DELTA for frame in events)
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# §4-4: the volatile contract — no replay, no backfill, missed while offline
# ---------------------------------------------------------------------------


async def test_the_stream_is_volatile_no_replay_no_backfill_on_reconnect(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        first = _GlobalSseStream(api)
        first.open()
        try:
            await first.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            session_a = await _http_create_session(api)
            await first.wait_for(
                lambda frame: frame["event"] == PY_SESSION_CREATED and frame["data"]["session_id"] == session_a
            )
            assert app.bus.subscription_count == 1  # one live global subscription
        finally:
            await first.close()
        assert app.bus.subscription_count == 0  # the disconnect released it (no leak)
        # events published while nobody watches are missed by design: session B settles offline
        session_b = await _http_create_session(api)
        ref = await _http_prompt(api, session_b, "offline events", "req_global_offline")
        await _await_http_status(api, str(ref["run_id"]), {"succeeded", "failed", "interrupted"})
        # reconnect fresh: the marker re-appears, but no history frame for A or B ever does
        second = _GlobalSseStream(api)
        second.open()
        try:
            await second.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            assert second.frames[0]["event"] == SERVER_CONNECTED
            # a NEW live event proves the negative: had replay existed, it would precede this frame
            session_c = await _http_create_session(api)
            await second.wait_for(
                lambda frame: frame["event"] == PY_SESSION_CREATED and frame["data"]["session_id"] == session_c
            )
            history = [
                frame for frame in _bus_frames(second) if frame["data"].get("session_id") in {session_a, session_b}
            ]
            assert not history  # volatile: sessions A and B never replay onto the fresh stream
            assert all(frame["data"]["session_id"] == session_c for frame in _bus_frames(second))
        finally:
            await second.close()


# ---------------------------------------------------------------------------
# §4-5: the slow consumer — bounded queue, accepted prefix, terminal error frame
# ---------------------------------------------------------------------------


async def test_a_slow_global_consumer_overflows_into_the_terminal_error_frame(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _small_queue_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        stream = _GlobalSseStream(api, paced=True)  # body chunks park behind release
        stream.open()
        try:
            await stream.wait_started()
            assert stream.status == 200
            session_id = await _http_create_session(api)
            ref = await _http_prompt(api, session_id, "too slow", "req_global_slow")
            final = await _await_http_status(api, str(ref["run_id"]), {"succeeded", "failed", "interrupted"})
            assert final["status"] == "succeeded"
            # the overflow already disconnected the subscription loudly, server-side:
            # more than two events were notified while this reader was parked
            assert app.bus.subscription_count == 0
            stream.release()  # unpark: the accepted prefix drains, then the terminal frame
            errors = await stream.wait_for(lambda frame: frame["event"] == "error", timeout_s=10.0)
            await stream.wait_finished()
            core = errors[0]["data"]
            assert core["code"] == CoreErrorCode.SLOW_CONSUMER.value
            assert core["retryable"] is False
            assert core["details"]["scope"] == "global"
            assert core["details"]["session_id"] == session_id
            assert core["details"]["queue_limit"] == 2
            assert core["details"]["seq"] == 3  # the first undeliverable event: run.started
            # the accepted prefix was delivered before the terminal frame — never silently dropped
            delivered = _bus_frames(stream)
            assert [frame["event"] for frame in delivered] == [PY_SESSION_CREATED, PY_PROMPT_ACCEPTED]
            assert core["details"]["event_id"] not in {frame["data"]["id"] for frame in delivered}
            assert stream.frames[-1] is errors[0]  # the error frame is the last thing on the wire
        finally:
            stream.release()
            await stream.close()


# ---------------------------------------------------------------------------
# §4-6: the keepalive cadence on an idle stream
# ---------------------------------------------------------------------------


async def test_keepalive_comments_flow_on_an_idle_global_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opencode.httpapi.app.SSE_KEEPALIVE_SECONDS", 0.05)
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        stream = _GlobalSseStream(api)
        stream.open()
        try:
            await stream.wait_for(lambda frame: frame["event"] == SERVER_CONNECTED)
            # an idle stream (no sessions exist at all) emits keepalive comments, paced
            await stream.wait_for(lambda frame: "keepalive" in frame["comments"])
            await stream.wait_for(
                lambda frame: len([comment for comment in _comments(stream) if comment == "keepalive"]) >= 2
            )
            assert all(
                frame["id"] is None and frame["event"] is None
                for frame in stream.frames
                if "keepalive" in frame["comments"]
            )  # a bare comment
            assert not _bus_frames(stream)  # an empty bus: no event frame ever flowed
        finally:
            await stream.close()


# ---------------------------------------------------------------------------
# the shared guard + the strict transport faces of the new route
# ---------------------------------------------------------------------------


async def test_the_guard_and_the_strict_faces_of_the_global_route(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        # the shared guard: 401 anonymous, 403 with a browser Origin (§3-10 zero-change proof)
        status, headers, payload, _ = await _asgi_json(api, "GET", "/events", token=None)
        assert status == 401
        assert headers.get(b"www-authenticate") == b"Bearer"
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        status, _, payload, _ = await _asgi_json(
            api, "GET", "/events", extra_headers=[(b"origin", b"http://127.0.0.1:5173")]
        )
        assert status == 403
        _assert_core_error(payload, CoreErrorCode.PERMISSION_DENIED, retryable=False)
        # the strict query face: an empty value or an empty segment is a 422, fail-closed
        for query in (b"types=", b"types=,", b"types=a,,b"):
            status, _, payload, _ = await _asgi_json(api, "GET", "/events", query_string=query)
            assert status == 422, query
            _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # the volatile contract on the wire: a Last-Event-ID header is refused, not honored
        status, _, payload, _ = await _asgi_json(
            api, "GET", "/events", extra_headers=[(b"last-event-id", b"ses_000001:5")]
        )
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)


def test_is_sse_request_routes_the_global_stream_onto_the_stream_budget() -> None:
    """The middleware's structural SSE check covers GET /events (the jobs-API precedent)."""

    def _scope(method: str, path: str) -> dict[str, Any]:
        return {"type": "http", "method": method, "path": path}

    assert _is_sse_request(_scope("GET", "/events"))
    assert not _is_sse_request(_scope("POST", "/events"))
    assert not _is_sse_request(_scope("GET", "/events/extra"))
    assert not _is_sse_request(_scope("GET", "/eventsx"))
    # the existing stream families stay covered, unchanged
    assert _is_sse_request(_scope("GET", "/session/ses_1/events"))
    assert _is_sse_request(_scope("GET", "/jobs/job_1/events"))
    assert not _is_sse_request(_scope("GET", "/session/ses_1"))


# ---------------------------------------------------------------------------
# §4-7: the SDK iterator face over one real server
# ---------------------------------------------------------------------------


def test_sdk_stream_global_events_receives_connected_then_live_frames(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("sdk_global_events_server")
    running = _boot_server(root / "app.db", _KeyedProvider())
    try:
        client = OpencodeClient(base_url=running.base, token=_TOKEN)
        stream = client.stream_global_events(timeout_s=_STREAM_TIMEOUT_S)
        first = next(stream)
        assert isinstance(first, ServerConnected)  # the synthetic marker precedes every live frame
        ref = client.create_session(_SNAPSHOT_BODY)
        live = next(stream)
        assert isinstance(live, DurableEvent)
        assert live.type == PY_SESSION_CREATED
        assert live.session_id == ref.session_id
        assert live.seq == 1
        stream.close()
        # the types face reaches the server: the filtered stream receives its own type
        filtered = client.stream_global_events(types=(PY_SESSION_CREATED,), timeout_s=_STREAM_TIMEOUT_S)
        try:
            assert isinstance(next(filtered), ServerConnected)
            second = client.create_session(_SNAPSHOT_BODY)
            event = next(filtered)
            assert isinstance(event, DurableEvent)
            assert event.type == PY_SESSION_CREATED and event.session_id == second.session_id
        finally:
            filtered.close()
    finally:
        _stop_server(running)


# ---------------------------------------------------------------------------
# the SDK decode layer: faces a healthy wire cannot produce
# ---------------------------------------------------------------------------


def test_the_connected_marker_literals_are_pinned_across_the_two_faces() -> None:
    assert SERVER_CONNECTED == SDK_SERVER_CONNECTED == "server.connected"


def _ephemeral_line(event_id: str) -> str:
    """One live ``py.text.delta`` envelope as the compact data-line JSON."""
    envelope = EphemeralEvent[TextDeltaData](
        id=event_id,
        type=PY_TEXT_DELTA,
        session_id="ses_probe",
        created_at=datetime.now(UTC),
        data=TextDeltaData(message_id="msg_1", part_id="p1", delta="probe"),
        metadata={"run_id": "run_probe"},
    )
    return json.dumps(envelope.model_dump(mode="json"), separators=(",", ":"))


def test_the_global_sse_parser_decodes_both_envelopes_and_fails_closed() -> None:
    registry = default_event_registry()
    connected = _frame_lines("event: server.connected", 'data: {"type": "server.connected", "data": {}}')
    keepalive = [b": keepalive\n", b"\n"]
    durable = _frame_lines("id: ses_probe:1", f"event: {PY_RUN_STARTED}", f"data: {_probe_event(1, 'evt_probe_1')}")
    ephemeral = _frame_lines(f"event: {PY_TEXT_DELTA}", f"data: {_ephemeral_line('evt_probe_2')}")  # no id line
    items = list(stream_global_events([*connected, *keepalive, *durable, *ephemeral], registry=registry))
    assert isinstance(items[0], ServerConnected) and items[0].type == SERVER_CONNECTED
    assert isinstance(items[1], DurableEvent) and items[1].seq == 1  # typed through the registry
    assert isinstance(items[2], EphemeralEvent) and items[2].id == "evt_probe_2"
    # the terminal error frame raises the stream error with the §8 body
    core = json.dumps({"code": "SlowConsumer", "message": "overflowed", "retryable": False, "details": {}})
    with pytest.raises(OpencodeStreamError) as excinfo:
        list(stream_global_events(_frame_lines("event: error", f"data: {core}"), registry=registry))
    assert excinfo.value.core.code is CoreErrorCode.SLOW_CONSUMER
    assert excinfo.value.core.retryable is False
    # malformed lines and frames fail closed, never silently skipped
    for bad in (
        [b"garbage line\n", b"\n"],  # no field colon
        [b"retry: 10000\n", b"\n"],  # an unknown field: id/event/data/comments only
        [b"id: ses_probe:1\n", b"\n"],  # an id without event/data lines
        [b"data: {}\n", b"\n"],  # a data line without an event line
    ):
        with pytest.raises(OpencodeClientError):
            list(stream_global_events(bad, registry=registry))
