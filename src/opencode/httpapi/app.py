"""FastAPI transport over the §7 command surface (P2-S3 / M5-A, PHASE2-MODULES §7).

Six routes over one :class:`~opencode.app.App` composition. The transport is
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

import json
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from opencode.app import App
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.session.service import DecisionReceipt, RunRef, RunStatus, SessionRef
from opencode.storage import StoreError
from opencode.storage.repos import MessagePage

#: §7 L106 "限制请求/并发/输出大小" — the transport-level caps (module-level,
#: pinned by tests). Request bodies above the cap answer 413 before any
#: parsing; responses are checked after serialization, before the first byte
#: is sent.
MAX_REQUEST_BODY_BYTES = 1024 * 1024
MAX_CONCURRENT_REQUESTS = 8
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

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


class _GuardMiddleware:
    """The single-user transport guard: token, Origin, concurrency (L106).

    Pure ASGI (no BaseHTTPMiddleware): the three checks run synchronously in
    loop order, so the in-flight counter needs no lock — the check and the
    increment happen without an await between them. Rejections answer with a
    §8 CoreError body directly; admitted requests flow to the inner app.
    """

    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self._token_bytes = token.encode("utf-8")
        self._inflight = 0

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
    server is one stderr line and the boot continues. Shutdown closes the
    App: the SessionService close lifecycle reclaims the scheduled runs
    (§7 L337/L108), then the database handle releases the writer lock.
    """

    @asynccontextmanager
    async def lifespan(_api: FastAPI) -> AsyncIterator[None]:
        await app.recovery_gate.wait()
        for server, error in await app.start_mcp_servers():
            print(f"opencode-http: MCP server {server!r} failed to start: {error}", file=sys.stderr)
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
    the six command routes — nothing on this app answers anonymously.
    """
    service = app.service
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

    api.add_exception_handler(_TransportError, _transport_error_handler)
    api.add_exception_handler(StoreError, _store_error_handler)
    api.add_middleware(_GuardMiddleware, token=token)
    return api
