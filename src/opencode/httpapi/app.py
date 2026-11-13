"""FastAPI transport over the §7 command surface (P2-S3 / M5-A, PHASE2-MODULES §7).

Seven routes over one :class:`~opencode.app.App` composition. The transport is
pure (L101: error-to-HTTP mapping, no runner rewrite): validation, idempotency
and conflict semantics stay in
:class:`~opencode.session.service.SessionService`, so this module only shapes
requests and responses:

* ``POST /session``                          -> ``{session_id}``
* ``POST /session/{session_id}/prompt``      -> RunRef wire (three fields)
* ``POST /run/{run_id}/cancel``              -> RunStatus wire (five fields)
* ``GET  /run/{run_id}/status``              -> RunStatus wire
* ``GET  /session/{session_id}/messages``    -> MessagePage wire (limit 1..200, default 100)
* ``POST /permission/{permission_id}/reply`` -> DecisionReceipt wire
* ``GET  /session/{session_id}/events``      -> durable SSE stream (§7 L105)

P3-S1 (T-C1-076) adds the five /jobs routes over
:meth:`~opencode.app.App.jobs` (PHASE3-ORCHESTRATOR §5.4/§7):

* ``POST /jobs``                             -> ``{id}`` (structural validation; params JSON-Schema lands in S2)
* ``GET  /jobs/{job_id}``                    -> JobStatus wire (the folded row)
* ``GET  /jobs/{job_id}/events``             -> durable SSE over ``ses_job_{job_id}`` (the trace pass-through)
* ``POST /jobs/{job_id}/cancel``             -> JobStatus wire (idempotent, propagates to descendants)
* ``POST /jobs/{job_id}/gate/{gate_id}/reply`` -> the gate-reply receipt (approve/reject/revise;
  body enum validated first; 409 Conflict on every service-side rejection)

Durable SSE (§7 L105, CORE-CONTRACTS §4): ``GET /session/{session_id}/events``
streams the durable log through :meth:`opencode.bus.eventlog.EventLog.follow`,
assembled here over ``app.database.engine`` + ``app.bus`` (the core
composition is untouched). ``GET /jobs/{job_id}/events`` (P3-S1) is the same
machinery over the synthesized ``ses_job_{job_id}`` stream key: the job's
existence is pre-checked (404) before the follow context is entered, the
frame ids generalize to ``{stream_key}:{seq}``, and the four trace fields ride
each ``py.job.*`` envelope's data (PHASE3 §4 M-6 double-stream correlation).
``Last-Event-ID`` carries the
``"{session_id}:{seq}"`` cursor: a malformed or cross-session header is
rejected at the transport (400 InvalidCursor), while a well-formed cursor of
an unknown session (404) or beyond the committed high watermark H (400, never
an empty wait) fails at the follow context entry — the context is entered in
the handler, so every pre-stream failure answers on the normal error face.
Event frames carry ``id: {session_id}:{seq}`` (the client resume cursor) with
the eight-field :class:`~opencode.contracts.events.DurableEvent` envelope as
the data JSON (its ``evt_`` id is the client dedup key); the catch-up marker
renders as the comment ``: synced {H}`` (no id — it never advances
Last-Event-ID); an idle stream emits ``: keepalive`` comments every
``SSE_KEEPALIVE_SECONDS``. The stream is durable-only (§4 L248: no ephemeral
token replay in the first version) and strictly pull-shaped — the follow
generator iterates inline in the response, so a slow consumer parks this
generator at its yield (zero prefetch, zero unbounded buffers) and never
blocks the runner (§7 L108: the bus is a wake signal, not a data path; the
client may re-read the durable log after disconnecting). A frame over
``MAX_SSE_FRAME_BYTES`` or a mid-stream storage failure answers one terminal
``event: error`` frame with the §8 CoreError body and closes the stream (the
200 is already on the wire, so the status cannot change; the non-retryable
faces should not be blindly reconnected). Streams draw from their own
concurrency budget (``MAX_CONCURRENT_SSE_STREAMS``) and never take one of the
eight request slots — the two 429 faces share the SlowConsumer code with
disjoint causes (connection capacity vs. slow-but-tolerated consumers, so a
slow SSE reader is never an error).

Security face (PHASE2-MODULES L106): the loopback binding is the host's face
(:mod:`opencode.httpapi.serving`); this module enforces the single-user bearer
token (timing-safe comparison, 401 on failure), refuses every request that
carries an ``Origin`` header (403 — first-version consumers are SDK/CLI with
no Origin header; a browser allowlist is a later slice's decision), and caps
the three transport faces (request body 1 MiB, in-flight requests 8, response
body 4 MiB). The token is not readable from the environment here: the host
validates ``OPENCODE_HTTP_TOKEN`` fail-closed before listening (ruling: the
transport only receives an already-validated token).

Lifespan (L108): startup awaits the recovery gate and starts the MCP servers
(a failed server prints one stderr line and the boot continues — the same
order as the CLI's ``_drive``); shutdown closes the App, which reclaims the
scheduled runs through the SessionService close lifecycle (§7 L337). A second
App over the same data directory fails at construction (writer lock), which
is the L110 explicit failure case, not something this layer absorbs.

Every error body is the §8 CoreError wire shape ``{code, message, retryable,
details?}`` (CoreError carries ``details`` with an empty-object default, the
same form the projector persists). The closed 22-code registry maps onto the
fixed HTTP status table below; transport-level rejections reuse existing
codes (401/403 PermissionDenied, 413 ValidationError, 429 SlowConsumer with
``retryable=true``). Uncaught exceptions fall through to the ASGI default 500
(§8 does not cover unexpected paths — a closed face there needs a new code,
OP-P2-5).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any, Literal, cast

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from opencode.app import App
from opencode.bus.eventlog import EventCursor, EventLog, LogItem, Synced
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.orchestrator.jobs import JobTransitionError
from opencode.orchestrator.registration import register_pipelines
from opencode.session.service import DecisionReceipt, RunRef, RunStatus, SessionRef
from opencode.storage import StoreError
from opencode.storage.jobs_store import JobRecord, job_stream_key
from opencode.storage.repos import MessagePage

#: §7 L106 "限制请求/并发/输出大小" — the transport-level caps (module-level,
#: pinned by tests). Request bodies above the cap answer 413 before any
#: parsing; responses are checked after serialization, before the first byte
#: is sent.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_CONCURRENT_REQUESTS = 8
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: §7 L105 durable SSE faces. ``SSE_KEEPALIVE_SECONDS`` paces the ``: keepalive``
#: comments on an otherwise idle stream; ``MAX_SSE_FRAME_BYTES`` caps one
#: serialized event frame (the 4 MiB response cap above applies to the six
#: single-JSON routes, not to an unbounded stream); ``MAX_CONCURRENT_SSE_STREAMS``
#: is the streams' own in-flight budget — a stream never takes a request slot.
SSE_KEEPALIVE_SECONDS = 15.0
MAX_CONCURRENT_SSE_STREAMS = 8
MAX_SSE_FRAME_BYTES = 4 * 1024 * 1024

#: §7 L310 message pages over HTTP: the transport domain is narrower than the
#: repository's 1..1000 clamp — out-of-domain values are 422 ValidationError
#: (they never reach the silent clamp).
DEFAULT_MESSAGES_LIMIT = 100
MAX_MESSAGES_LIMIT = 200

#: The full §8 closed-registry mapping onto HTTP statuses (ruling e; the
#: table-driven test pins all 22 rows). Transport-level rejections specify
#: their status directly and reuse codes from this registry.
_HTTP_STATUS_BY_CODE: dict[CoreErrorCode, int] = {
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


class _TransportError(Exception):
    """A transport-level rejection with its HTTP status and §8 body."""

    def __init__(self, status_code: int, core: CoreError) -> None:
        super().__init__(core.message)
        self.status_code = status_code
        self.core = core


def _validation(message: str) -> CoreError:
    return CoreError(code=CoreErrorCode.VALIDATION_ERROR, message=message, retryable=False)


def _error_response(core: CoreError, status_code: int) -> Response:
    """One §8 CoreError body with the given HTTP status (compact JSON)."""
    body = json.dumps(core.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return Response(content=body, status_code=status_code, media_type="application/json")


def _json_response(payload: JsonValue) -> Response:
    """Serialize one success payload, enforcing MAX_RESPONSE_BYTES (413)."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise _TransportError(
            413, _validation(f"response body exceeds MAX_RESPONSE_BYTES ({MAX_RESPONSE_BYTES} bytes)")
        )
    return Response(content=body, media_type="application/json")


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body incrementally, refusing bodies over the cap."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_REQUEST_BODY_BYTES:
            raise _TransportError(
                413, _validation(f"request body exceeds MAX_REQUEST_BODY_BYTES ({MAX_REQUEST_BODY_BYTES} bytes)")
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_object(request: Request) -> JsonObject:
    """The request body as a JSON object (§8-shaped transport rejections)."""
    raw = await _read_capped_body(request)
    try:
        payload: Any = json.loads(raw)
    except ValueError:
        raise _TransportError(422, _validation("request body is not valid JSON")) from None
    if not isinstance(payload, dict):
        raise _TransportError(422, _validation("request body must be a JSON object"))
    return payload


def _strict_string_fields(payload: JsonObject, keys: frozenset[str]) -> dict[str, str]:
    """Exact-shape extraction: every key present, no unknown keys, str values."""
    for key in sorted(keys):
        if key not in payload:
            raise _TransportError(422, _validation(f"request body field {key!r} is required"))
        if not isinstance(payload[key], str):
            raise _TransportError(422, _validation(f"request body field {key!r} must be a string"))
    unknown = sorted(set(payload) - keys)
    if unknown:
        raise _TransportError(422, _validation(f"request body has unknown fields: {', '.join(unknown)}"))
    fields: dict[str, str] = {}
    for key in keys:
        value = payload[key]
        assert isinstance(value, str)  # the loop above already rejected non-strings
        fields[key] = value
    return fields


def _run_status_wire(status: RunStatus) -> JsonObject:
    """§7 L314: ``{session_id, run_id, request_id, status, error?}``."""
    return {
        "session_id": status.session_id,
        "run_id": status.run_id,
        "request_id": status.request_id,
        "status": str(status.status),
        "error": status.error,
    }


def _messages_wire(page: MessagePage) -> JsonObject:
    """The S6 read-repository page, directly serialized (§7 L310)."""
    return {
        "items": [item.model_dump(mode="json") for item in page.items],
        "next_after_message_id": page.next_after_message_id,
        "has_more": page.has_more,
    }


def _job_wire(record: JobRecord) -> JsonObject:
    """The P3-S1 job status wire: the folded ``jobs`` row (PHASE3 §5.4 field set).

    ``id``/``status``/``pipeline_id``/``session_anchor``/``params_hash``/
    ``depth``/``retry_count``/``trace_id`` carry the §5.4 identity and fold
    faces; ``result``/``error`` ride the terminal event (None before it);
    ``created_at_ms``/``updated_at_ms`` are the storage epoch-ms stamps.
    """
    return {
        "id": record.job_id,
        "status": record.status,
        "pipeline_id": record.pipeline_id,
        "session_anchor": record.session_anchor,
        "params_hash": record.params_hash,
        "depth": record.depth,
        "retry_count": record.retry_count,
        "trace_id": record.trace_id,
        "result": record.result,
        "error": record.error,
        "created_at_ms": record.created_epoch_ms,
        "updated_at_ms": record.updated_epoch_ms,
    }


# -- §7 L105: the durable SSE stream (CORE-CONTRACTS §4) --------------------


def _last_event_id_cursor(request: Request, session_id: str) -> EventCursor | None:
    """The ``Last-Event-ID`` header as this session's cursor (§4 L222).

    An absent header is ``None`` — the seq-0 cursor, a full replay from the
    beginning of the log. A present header must read ``"{session_id}:{seq}"``
    (rsplit on the last colon, so a session id containing colons still parses)
    with an ASCII-digit, non-negative seq for the path's own session; anything
    else is a transport-level 400 InvalidCursor. A well-formed cursor beyond
    the committed high watermark is NOT rejected here — the follow context
    entry owns that state-dependent check (400, never an empty wait).
    """
    raw = request.headers.get("last-event-id")
    if raw is None:
        return None
    header_session, separator, raw_seq = raw.rpartition(":")
    if not separator or not header_session or not raw_seq.isascii() or not raw_seq.isdigit():
        raise _TransportError(
            400,
            CoreError(
                code=CoreErrorCode.INVALID_CURSOR,
                message=f"Last-Event-ID {raw!r} is not a well-formed '{{session_id}}:{{seq}}' cursor",
                retryable=False,
                details={"session_id": session_id, "reason": "last_event_id_malformed"},
            ),
        )
    if header_session != session_id:
        raise _TransportError(
            400,
            CoreError(
                code=CoreErrorCode.INVALID_CURSOR,
                message=f"Last-Event-ID belongs to session {header_session!r}, not {session_id!r}",
                retryable=False,
                details={
                    "session_id": session_id,
                    "cursor_session_id": header_session,
                    "reason": "last_event_id_session_mismatch",
                },
            ),
        )
    return EventCursor(session_id=session_id, seq=int(raw_seq))


def _is_sse_request(scope: Scope) -> bool:
    """``GET /session/{id}/events`` and ``GET /jobs/{id}/events``: the stream-slot routes.

    Matched structurally (three path segments: ``session``/``jobs`` + id +
    ``events``) so the stream budget only ever counts requests the router
    itself would accept as a stream route; anything else stays on the request
    budget. The P3-S1 generalization keeps the two stream families on the
    same ``MAX_CONCURRENT_SSE_STREAMS`` budget (disjoint causes are not
    split per family in v1).
    """
    if scope.get("method") != "GET":
        return False
    parts = (scope.get("path") or "").split("/")
    return (
        len(parts) == 4
        and parts[0] == ""
        and parts[1] in ("session", "jobs")
        and bool(parts[2])
        and parts[3] == "events"
    )


def _frame_for(item: LogItem) -> bytes:
    """One SSE wire block: an event frame, or the ``: synced {H}`` comment.

    Event frames carry ``id: {session_id}:{seq}`` — the exact Last-Event-ID
    form a reconnecting client sends back — plus the full eight-field
    DurableEvent envelope as one compact JSON data line; the Synced catch-up
    marker is a comment (no id, no event type: it never advances
    Last-Event-ID).
    """
    if isinstance(item, Synced):
        return f": synced {item.cursor.seq}\n\n".encode()
    data = json.dumps(item.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"id: {item.session_id}:{item.seq}\nevent: {item.type}\ndata: {data}\n\n".encode()


def _error_frame(core: CoreError) -> bytes:
    """The terminal ``event: error`` frame (the §8 CoreError wire, in-stream)."""
    data = json.dumps(core.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"event: error\ndata: {data}\n\n".encode()


async def _sse_frames(
    follow: AbstractAsyncContextManager[AsyncIterator[LogItem]],
    items: AsyncIterator[LogItem],
) -> AsyncIterator[bytes]:
    """Stream the already-entered follow context as SSE bytes (pull-shaped).

    Exactly one ``__anext__`` is outstanding at a time, wrapped in a task so
    an idle stream can still emit ``: keepalive`` comments — a plain
    ``wait_for`` would cancel the follow at its park point and kill the
    stream, so the timeout only ends the outer wait while the follow keeps
    parking (§7 L108: a slow consumer suspends this generator, never the
    runner; at most one decoded item is ever buffered). A StoreError raised
    by the follow (a bad page mid-stream, after its healthy events were
    already yielded) or a frame over MAX_SSE_FRAME_BYTES answers one terminal
    ``event: error`` frame and ends the response. Leaving the generator —
    normally, on disconnect, or on cancellation — cancels the outstanding
    ``__anext__`` and leaves the follow context, which unregisters the
    wakeup (断线/取消安全).
    """
    try:
        pending: asyncio.Task[LogItem] | None = None
        while True:
            if pending is None:
                pending = asyncio.ensure_future(items.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=SSE_KEEPALIVE_SECONDS)
            if not done:
                yield b": keepalive\n\n"
                continue
            task, pending = pending, None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            except StoreError as error:
                yield _error_frame(error.core)
                return
            frame = _frame_for(item)
            if len(frame) > MAX_SSE_FRAME_BYTES:
                yield _error_frame(
                    _validation(f"a single SSE event frame exceeds MAX_SSE_FRAME_BYTES ({MAX_SSE_FRAME_BYTES} bytes)")
                )
                return
            yield frame
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, StoreError):
                await pending
        with suppress(Exception):
            await follow.__aexit__(None, None, None)


class _GuardMiddleware:
    """The single-user transport guard: token, Origin, concurrency (L106/L105).

    Pure ASGI (no BaseHTTPMiddleware): the checks run synchronously in loop
    order, so the two in-flight counters (requests, SSE streams) need no lock
    — the check and the increment happen without an await between them. A
    stream counts against MAX_CONCURRENT_SSE_STREAMS for its whole response
    lifetime (the middleware wraps the streaming body too), everything else
    against MAX_CONCURRENT_REQUESTS, and neither budget feeds the other.
    Rejections answer with a §8 CoreError body directly; admitted requests
    flow to the inner app.
    """

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token_bytes = token.encode("utf-8")
        self._inflight = 0
        self._sse_inflight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        if _authorized(headers, self._token_bytes) is False:
            response = _error_response(
                CoreError(
                    code=CoreErrorCode.PERMISSION_DENIED,
                    message="missing or invalid Authorization bearer token",
                    retryable=False,
                ),
                401,
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            await response(scope, receive, send)
            return
        if any(name == b"origin" for name, _ in headers):
            await _error_response(
                CoreError(
                    code=CoreErrorCode.PERMISSION_DENIED,
                    message="requests carrying an Origin header are refused (no browser face in this version)",
                    retryable=False,
                ),
                403,
            )(scope, receive, send)
            return
        if _is_sse_request(scope):
            # §7 L105: streams draw from their own budget and never take a
            # request slot — eight hanging streams must not starve the six
            # command routes (and a busy request face must not refuse streams).
            if self._sse_inflight >= MAX_CONCURRENT_SSE_STREAMS:
                await _error_response(
                    CoreError(
                        code=CoreErrorCode.SLOW_CONSUMER,
                        message=(
                            f"the server is at its concurrent SSE stream limit ({MAX_CONCURRENT_SSE_STREAMS}); "
                            "retry later"
                        ),
                        retryable=True,
                    ),
                    429,
                )(scope, receive, send)
                return
            self._sse_inflight += 1
            try:
                await self.app(scope, receive, send)
            finally:
                self._sse_inflight -= 1
            return
        if self._inflight >= MAX_CONCURRENT_REQUESTS:
            await _error_response(
                CoreError(
                    code=CoreErrorCode.SLOW_CONSUMER,
                    message=f"the server is at its concurrent-request limit ({MAX_CONCURRENT_REQUESTS}); retry later",
                    retryable=True,
                ),
                429,
            )(scope, receive, send)
            return
        self._inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._inflight -= 1


def _authorized(headers: list[tuple[bytes, bytes]], token_bytes: bytes) -> bool:
    """``Authorization: Bearer <token>`` compared timing-safe (§7 L106)."""
    value = next((v for name, v in headers if name == b"authorization"), None)
    if value is None:
        return False
    scheme, _, credential = value.partition(b" ")
    if scheme != b"Bearer" or not credential:
        return False
    return secrets.compare_digest(credential, token_bytes)


def _lifespan_for(app: App) -> Any:
    """Startup/shutdown over the App lifecycle (L108; the _drive order).

    Startup awaits the startup-recovery scan (fail closed: a failed scan
    aborts the ASGI startup) and then starts the MCP servers — a failed
    server is one stderr line and the boot continues. P5-S1 (T-C1-085):
    an App carrying the pipeline engine and the skills root also registers
    the manifest's pipelines here — the startup twin of the shutdown
    reclaim below, on this same serving loop. Shutdown closes the
    App: the SessionService close lifecycle reclaims the scheduled runs
    (§7 L337/L108), then the database handle releases the writer lock.
    """

    @asynccontextmanager
    async def lifespan(_api: FastAPI) -> AsyncIterator[None]:
        await app.recovery_gate.wait()
        for server, error in await app.start_mcp_servers():
            print(f"opencode-http: MCP server {server!r} failed to start: {error}", file=sys.stderr)
        # P5-S1 (T-C1-085): the manifest -> engine registration face — the
        # startup twin of the shutdown reclaim below, on this same serving
        # loop (the checkpointer's aiosqlite connection is loop-bound). The
        # disclosure policy is the MCP boot precedent (ADR-0002 L60): a
        # missing skills directory/manifest is the silent fresh-project
        # zero state; a broken manifest or a rejected entry is exactly one
        # stderr line each and the boot continues.
        if app.pipeline_engine is not None and app.skills_dir is not None:
            report = await register_pipelines(app.pipeline_engine, app.skills_dir)
            if report.manifest_error is not None:
                print(f"opencode-http: skills manifest discovery failed: {report.manifest_error}", file=sys.stderr)
            for rejection in report.rejected:
                print(
                    f"opencode-http: skill {rejection.skill_id!r} pipeline {rejection.pipeline!r} "
                    f"rejected: {rejection.error_type}: {rejection.message}",
                    file=sys.stderr,
                )
        try:
            yield
        finally:
            await app.close()

    return lifespan


async def _transport_error_handler(_request: Request, error: Exception) -> Response:
    assert isinstance(error, _TransportError)
    return _error_response(error.core, error.status_code)


async def _store_error_handler(_request: Request, error: Exception) -> Response:
    assert isinstance(error, StoreError)
    return _error_response(error.core, _HTTP_STATUS_BY_CODE[error.core.code])


def create_app(app: App, *, token: str) -> FastAPI:
    """Build the FastAPI transport over one App composition (the public face).

    ``token`` is the already-validated single-user token (the host reads and
    fail-closes ``OPENCODE_HTTP_TOKEN`` before listening); the docs faces
    (``/docs``, ``/redoc``, ``/openapi.json``) sit behind the same guard as
    the seven routes — nothing on this app answers anonymously.
    """
    service = app.service
    jobs = app.jobs
    api = FastAPI(
        title="opencode",
        version="0.0.1",
        description="Single-user local HTTP face over the §7 command surface "
        "(loopback-only host, bearer-token auth, browser Origin refused).",
        lifespan=_lifespan_for(app),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    @api.post("/session")
    async def create_session(request: Request) -> Response:
        payload = await _read_json_object(request)
        ref: SessionRef = await service.create(payload)
        return _json_response({"session_id": ref.session_id})

    @api.post("/session/{session_id}/prompt")
    async def prompt(session_id: str, request: Request) -> Response:
        payload = await _read_json_object(request)
        fields = _strict_string_fields(payload, frozenset({"text", "request_id"}))
        ref: RunRef = await service.prompt(session_id, fields["text"], fields["request_id"])
        return _json_response({"session_id": ref.session_id, "run_id": ref.run_id, "request_id": ref.request_id})

    @api.post("/run/{run_id}/cancel")
    async def cancel_run(run_id: str) -> Response:
        status = await service.cancel(run_id)
        return _json_response(_run_status_wire(status))

    @api.get("/run/{run_id}/status")
    async def run_status(run_id: str) -> Response:
        return _json_response(_run_status_wire(await service.run_status(run_id)))

    @api.get("/session/{session_id}/messages")
    async def messages(session_id: str, request: Request) -> Response:
        limit = DEFAULT_MESSAGES_LIMIT
        raw_limit = request.query_params.get("limit")
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                raise _TransportError(422, _validation(f"limit must be an integer, got {raw_limit!r}")) from None
            if not 1 <= limit <= MAX_MESSAGES_LIMIT:
                raise _TransportError(422, _validation(f"limit must be within 1..{MAX_MESSAGES_LIMIT}, got {limit}"))
        page = await service.messages(
            session_id,
            after_message_id=request.query_params.get("after_message_id"),
            limit=limit,
        )
        return _json_response(_messages_wire(page))

    @api.get("/session/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> Response:
        """§7 L105: the durable SSE stream (CORE-CONTRACTS §4 follow, over HTTP).

        The EventLog is transport-assembled (engine + bus off the App, the
        default registry — the same shape the runner composes). The follow
        context is entered here, so its state-dependent validation answers on
        the normal error face (404 unknown session, 400 cursor beyond H, 500
        read-only diagnostic) before any stream byte is on the wire; the
        generator only ever iterates the already-entered context and leaves
        it on disconnect (its finally unregisters the wakeup).
        """
        cursor = _last_event_id_cursor(request, session_id)
        log = EventLog(app.database.engine, bus=app.bus)
        follow = log.follow(session_id, cursor)
        items = await follow.__aenter__()
        return StreamingResponse(
            _sse_frames(follow, items),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    @api.post("/permission/{permission_id}/reply")
    async def reply_permission(permission_id: str, request: Request) -> Response:
        payload = await _read_json_object(request)
        fields = _strict_string_fields(payload, frozenset({"reply"}))
        receipt: DecisionReceipt = await service.reply_permission(permission_id, fields["reply"])
        return _json_response(
            {
                "permission_id": receipt.permission_id,
                "decision": receipt.decision,
                "execution_id": receipt.execution_id,
            }
        )

    # -- P3-S1 (T-C1-076): the five /jobs routes over app.jobs ---------------

    @api.post("/jobs")
    async def create_job(request: Request) -> Response:
        """Submit one job (PHASE3 §7 A fork-3): created + spec_bound atomically.

        Structural validation only in S1: ``pipeline_id`` non-empty string,
        ``params`` JSON object, ``session_anchor`` ``"api"`` (default) or a
        ``ses_``-prefixed session id, unknown fields rejected. Validating
        params against the pipeline's JSON Schema lands with spec loading
        (S2). The fake executor is spawned after the submit commits, so the
        response returns while the run drives the lifecycle events.
        """
        payload = await _read_json_object(request)
        unknown = sorted(set(payload) - {"pipeline_id", "params", "session_anchor"})
        if unknown:
            raise _TransportError(422, _validation(f"request body has unknown fields: {', '.join(unknown)}"))
        pipeline_id = payload.get("pipeline_id")
        if not isinstance(pipeline_id, str) or not pipeline_id:
            raise _TransportError(422, _validation("request body field 'pipeline_id' must be a non-empty string"))
        params = payload.get("params")
        if not isinstance(params, dict):
            raise _TransportError(422, _validation("request body field 'params' must be a JSON object"))
        session_anchor = payload.get("session_anchor", "api")
        if not isinstance(session_anchor, str) or (session_anchor != "api" and not session_anchor.startswith("ses_")):
            raise _TransportError(
                422, _validation("request body field 'session_anchor' must be 'api' or a ses_-prefixed session id")
            )
        handle = await jobs.submit(pipeline_id, params, session_anchor)
        jobs.spawn(handle.job_id)
        return _json_response({"id": handle.job_id})

    @api.get("/jobs/{job_id}")
    async def job_status(job_id: str) -> Response:
        return _json_response(_job_wire(await jobs.get_job(job_id)))

    @api.get("/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request) -> Response:
        """The jobs SSE stream (PHASE3 §5.4): the same follow machinery over ``ses_job_{job_id}``.

        The job's existence is pre-checked (404 with a job-flavored body)
        before the follow context is entered; the frame ids are the
        generalized ``{stream_key}:{seq}`` resume cursors and each
        ``py.job.*`` envelope's data carries the four trace fields, so a
        client correlates this stream with the session stream on
        ``trace_id`` (PHASE3 §4 M-6 double-stream envelope).
        """
        await jobs.get_job(job_id)
        stream_key = job_stream_key(job_id)
        cursor = _last_event_id_cursor(request, stream_key)
        log = EventLog(app.database.engine, bus=app.bus)
        follow = log.follow(stream_key, cursor)
        items = await follow.__aenter__()
        return StreamingResponse(
            _sse_frames(follow, items),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    @api.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> Response:
        return _json_response(_job_wire(await jobs.cancel(job_id)))

    @api.post("/jobs/{job_id}/gate/{gate_id}/reply")
    async def gate_reply(job_id: str, gate_id: str, request: Request) -> Response:
        """The S5a gate-reply face: validate the shape, then resolve through the service.

        The body enum (approve/reject/revise) is validated first (the wire
        contract is real), the job's existence second (404), then the reply
        resolves through the jobs service's gate chain: an approve/revise
        resumes the job from its advisory cursor (the re-drive runs in the
        background -- the M-5 non-blocking discipline), a reject resolves
        it terminally with ``PermissionDenied`` naming the gate. A revise may
        carry revised ``params`` (a JSON object, legal only on the revise
        enum -- any other pairing is a 422). Every service-side rejection
        (job not gated, gate mismatch, expired gate, chain not wired in
        this composition) answers 409 Conflict with the job id and gate id
        in ``details`` -- the exact body shape the S1 shell pinned.
        """
        payload = await _read_json_object(request)
        unknown = sorted(set(payload) - {"reply", "params"})
        if unknown:
            raise _TransportError(422, _validation(f"request body has unknown fields: {', '.join(unknown)}"))
        reply = payload.get("reply")
        if not isinstance(reply, str) or reply not in ("approve", "reject", "revise"):
            raise _TransportError(
                422, _validation("request body field 'reply' must be one of: approve, reject, revise")
            )
        params = payload.get("params")
        if "params" in payload:
            if reply != "revise":
                raise _TransportError(422, _validation("request body field 'params' is legal only on a revise reply"))
            if not isinstance(params, dict):
                raise _TransportError(422, _validation("request body field 'params' must be a JSON object"))
        await jobs.get_job(job_id)
        try:
            record = await jobs.reply_gate(
                job_id,
                gate_id,
                cast(Literal["approve", "reject", "revise"], reply),
                params=params if isinstance(params, dict) else None,
            )
        except JobTransitionError as error:
            raise _TransportError(
                409,
                CoreError(
                    code=CoreErrorCode.CONFLICT,
                    message=f"job gate reply was rejected: {error}",
                    retryable=False,
                    details={"job_id": job_id, "gate_id": gate_id, **error.details},
                ),
            ) from error
        return _json_response({"job_id": job_id, "gate_id": gate_id, "reply": reply, "status": record.status})

    api.add_exception_handler(_TransportError, _transport_error_handler)
    api.add_exception_handler(StoreError, _store_error_handler)
    api.add_middleware(_GuardMiddleware, token=token)
    return api
