"""The Python client over the §7 HTTP face (P2-S5 / M5-C, PHASE2-MODULES §7).

One :class:`OpencodeClient` mirrors the seven routes of the single-user
server (the 054/056 faces) over the real wire — stdlib ``urllib`` only: no
fastapi, no uvicorn, no ``opencode.httpapi`` import (a pure client stays on
the core dependency surface; L101: the SDK directory is new and the runner is
not rewritten; L112: with M5 alone an external UI attaches to the
single-process service):

* ``POST /session``                          -> :meth:`OpencodeClient.create_session`
* ``POST /session/{session_id}/prompt``      -> :meth:`OpencodeClient.prompt`
* ``POST /run/{run_id}/cancel``              -> :meth:`OpencodeClient.cancel_run`
* ``GET  /run/{run_id}/status``              -> :meth:`OpencodeClient.run_status`
* ``GET  /session/{session_id}/messages``    -> :meth:`OpencodeClient.list_messages`
* ``POST /permission/{permission_id}/reply`` -> :meth:`OpencodeClient.reply_permission`
* ``GET  /session/{session_id}/events``      -> :meth:`OpencodeClient.stream_events`

Return types are the core models themselves (``service.RunRef`` /
``RunStatus`` / ``DecisionReceipt`` / ``SessionRef``, ``repos.MessagePage``,
``contracts.DurableEvent``) — the wire JSON is decoded here and never
re-shaped into SDK-local types, so an SDK consumer sees exactly the session
layer's vocabulary.

Transport (the 054/056 smoke forms): the bearer token rides every request,
JSON bodies are UTF-8 ``application/json``, every call takes its own timeout
(the constructor default is per-request overridable), and every request
opens a fresh connection — stdlib ``urllib`` holds no pool and the first
version deliberately does not introduce one. The opener never routes
through a system proxy (a loopback face). Every non-2xx answer raises
:class:`OpencodeApiError` carrying the §8 CoreError body; a transport failure
raises :class:`OpencodeConnectionError`; the terminal SSE ``event: error``
frame raises :class:`OpencodeStreamError`.

``stream_events`` is the SSE face (see :mod:`opencode.sdk.sse`) — a lazily
connected generator — and :meth:`resume_cursor` exposes the Last-Event-ID
state, so after a disconnect the caller reconnects with
``stream_events(session_id, after=client.resume_cursor(session_id))`` and
receives exactly the missed events.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from typing import Any

from pydantic import ValidationError

from opencode.contracts.errors import CoreError
from opencode.contracts.events import DurableEvent, default_event_registry
from opencode.contracts.messages import (
    AssistantMessage,
    IdleMessage,
    JsonObject,
    SessionMessage,
    UserMessage,
)
from opencode.sdk.errors import OpencodeApiError, OpencodeClientError, OpencodeConnectionError
from opencode.sdk.sse import stream_durable_events
from opencode.session.service import DecisionReceipt, RunRef, RunStatus, RunStatusValue, SessionRef
from opencode.storage.repos import MessagePage

#: The default base URL — the loopback face of ``serving.DEFAULT_PORT``,
#: pinned here as a literal because the SDK never imports the server package.
DEFAULT_BASE_URL = "http://127.0.0.1:4096"

#: The default per-request timeout (seconds). Generous by design: a slow
#: generation never holds the HTTP request, but a loaded first-version host
#: can take seconds to answer even a status read.
DEFAULT_TIMEOUT_S = 30.0

#: §2.1 message-union dispatch: the projected ``data["type"]`` tag selects the
#: wire model (the repos' own table; the server only emits decodable rows).
_MESSAGE_MODELS: dict[str, type[SessionMessage]] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "idle": IdleMessage,
}


def _positive_timeout(value: float, *, param: str) -> float:
    """Reject bools and non-positive values (the codebase's numeric-arg guard)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{param} must be a positive number of seconds (bools are rejected)")
    return float(value)


def _path(identifier: str) -> str:
    """One path segment, percent-encoded defensively (ids are ``ses_``/``run_``-safe anyway)."""
    return urllib.parse.quote(identifier, safe="")


def _expect_object(payload: Any, keys: frozenset[str]) -> dict[str, Any]:
    """The success payload as exactly the pinned key set — the wire contract, checked."""
    if not isinstance(payload, dict) or set(payload) != keys:
        shape = sorted(map(str, payload)) if isinstance(payload, dict) else type(payload).__name__
        raise OpencodeClientError(f"unexpected wire shape: expected the keys {sorted(keys)}, got {shape}")
    return payload


def _string_field(fields: dict[str, Any], key: str) -> str:
    value = fields[key]
    if not isinstance(value, str) or not value:
        raise OpencodeClientError(f"wire field {key!r} must be a non-empty string, got {value!r}")
    return value


def _session_ref(payload: Any) -> SessionRef:
    fields = _expect_object(payload, frozenset({"session_id"}))
    return SessionRef(session_id=_string_field(fields, "session_id"))


def _run_ref(payload: Any) -> RunRef:
    fields = _expect_object(payload, frozenset({"session_id", "run_id", "request_id"}))
    return RunRef(
        session_id=_string_field(fields, "session_id"),
        run_id=_string_field(fields, "run_id"),
        request_id=_string_field(fields, "request_id"),
    )


def _run_status(payload: Any) -> RunStatus:
    fields = _expect_object(payload, frozenset({"session_id", "run_id", "request_id", "status", "error"}))
    raw_status = fields["status"]
    try:
        status = RunStatusValue(raw_status)
    except ValueError as error:
        raise OpencodeClientError(f"unknown run status on the wire: {raw_status!r}") from error
    raw_error = fields["error"]
    if raw_error is not None and not isinstance(raw_error, str):
        raise OpencodeClientError(f"wire field 'error' must be a string or null, got {raw_error!r}")
    return RunStatus(
        session_id=_string_field(fields, "session_id"),
        run_id=_string_field(fields, "run_id"),
        request_id=_string_field(fields, "request_id"),
        status=status,
        error=raw_error,
    )


def _decision_receipt(payload: Any) -> DecisionReceipt:
    fields = _expect_object(payload, frozenset({"permission_id", "decision", "execution_id"}))
    decision = fields["decision"]
    if decision not in ("allow", "deny"):
        raise OpencodeClientError(f"wire field 'decision' must be 'allow' or 'deny', got {decision!r}")
    return DecisionReceipt(
        permission_id=_string_field(fields, "permission_id"),
        decision=decision,
        execution_id=_string_field(fields, "execution_id"),
    )


def _message_page(payload: Any) -> MessagePage:
    fields = _expect_object(payload, frozenset({"items", "next_after_message_id", "has_more"}))
    raw_items = fields["items"]
    if not isinstance(raw_items, list):
        raise OpencodeClientError(f"wire field 'items' must be a list, got {raw_items!r}")
    items: list[SessionMessage] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise OpencodeClientError(f"a message item must be a JSON object, got {raw_item!r}")
        model = _MESSAGE_MODELS.get(str(raw_item.get("type")))
        if model is None:
            raise OpencodeClientError(f"unknown projected message type tag {raw_item.get('type')!r}")
        try:
            items.append(model.model_validate(raw_item))
        except ValidationError as error:
            raise OpencodeClientError(f"a message item did not decode: {error}") from error
    next_after = fields["next_after_message_id"]
    if next_after is not None and not isinstance(next_after, str):
        raise OpencodeClientError(f"wire field 'next_after_message_id' must be a string or null, got {next_after!r}")
    has_more = fields["has_more"]
    if not isinstance(has_more, bool):
        raise OpencodeClientError(f"wire field 'has_more' must be a boolean, got {has_more!r}")
    return MessagePage(items=items, next_after_message_id=next_after, has_more=has_more)


def _api_error(error: urllib.error.HTTPError) -> OpencodeApiError:
    """One non-2xx answer as :class:`OpencodeApiError` (the §8 body when present)."""
    try:
        raw = error.read()
    except OSError:
        raw = b""  # the error body is already buffered; read failing is not the answer's face
    core: CoreError | None = None
    if raw:
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            try:
                core = CoreError.model_validate(payload)
            except ValidationError:
                core = None
    return OpencodeApiError(status_code=error.code, core=core)


class OpencodeClient:
    """The seven-route client over one single-user opencode server (§7 L110).

    Construct with the server's base URL and its single-user token; every
    method maps one §7 route, returns the core model, and takes an optional
    ``timeout_s`` override of the constructor default.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, token: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout_s = _positive_timeout(timeout_s, param="timeout_s")
        #: A loopback client that never routes through a system proxy (the
        #: 054 form: stdlib urllib would otherwise consult the Windows proxy
        #: set for plain HTTP). No connection pool — one fresh connection
        #: per request, the minimal first-version surface.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._registry = default_event_registry()
        self._last_event_ids: dict[str, str] = {}

    # -- transport (one JSON request per call, fail-closed decode) ----------

    def _timeout(self, override: float | None) -> float:
        return self._timeout_s if override is None else _positive_timeout(override, param="timeout_s")

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: JsonObject | None = None,
        query: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout(timeout_s)) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise _api_error(error) from None
        except (urllib.error.URLError, OSError) as error:
            raise OpencodeConnectionError(f"{method} {path} never answered") from error
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as error:
            raise OpencodeClientError(f"{method} {path} answered a non-JSON body") from error

    def _open_stream(
        self,
        session_id: str,
        *,
        after: str | None,
        timeout_s: float | None,
    ) -> http.client.HTTPResponse:
        """Open the SSE route; the caller owns closing the response."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "text/event-stream"}
        if after is not None:
            headers["Last-Event-ID"] = after
        path = f"/session/{_path(session_id)}/events"
        request = urllib.request.Request(self._base + path, headers=headers, method="GET")
        try:
            response: http.client.HTTPResponse = self._opener.open(request, timeout=self._timeout(timeout_s))
        except urllib.error.HTTPError as error:
            raise _api_error(error) from None
        except (urllib.error.URLError, OSError) as error:
            raise OpencodeConnectionError(f"GET {path} never answered") from error
        return response

    # -- the seven routes ---------------------------------------------------

    def create_session(self, config: JsonObject, *, timeout_s: float | None = None) -> SessionRef:
        """``POST /session``: one new session under the §6.1 config snapshot.

        ``config`` is the snapshot JSON object (e.g.
        ``ConfigSnapshot.create().model_dump(mode="json")``); an illegal one
        answers 422 ValidationError on the same error face.
        """
        return _session_ref(self._call("POST", "/session", body=config, timeout_s=timeout_s))

    def prompt(self, session_id: str, text: str, request_id: str, *, timeout_s: float | None = None) -> RunRef:
        """``POST /session/{id}/prompt`` — the request_id idempotency core (§7 L107).

        An identical ``(request_id, text)`` retry returns the original
        :class:`~opencode.session.service.RunRef`; a different text under the
        same request_id answers 409 IdempotencyConflict.
        """
        path = f"/session/{_path(session_id)}/prompt"
        return _run_ref(self._call("POST", path, body={"text": text, "request_id": request_id}, timeout_s=timeout_s))

    def cancel_run(self, run_id: str, *, timeout_s: float | None = None) -> RunStatus:
        """``POST /run/{id}/cancel`` — a late cancel returns the original terminal status."""
        return _run_status(self._call("POST", f"/run/{_path(run_id)}/cancel", timeout_s=timeout_s))

    def run_status(self, run_id: str, *, timeout_s: float | None = None) -> RunStatus:
        """``GET /run/{id}/status`` — the five-field run status wire."""
        return _run_status(self._call("GET", f"/run/{_path(run_id)}/status", timeout_s=timeout_s))

    def list_messages(
        self,
        session_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 100,
        timeout_s: float | None = None,
    ) -> MessagePage:
        """``GET /session/{id}/messages`` — cursor pages (§7 L310).

        ``limit`` is the transport domain 1..200 (default 100; outside the
        domain the server answers 422 ValidationError, the pass-through keeps
        a single source of truth) and ``after_message_id`` is the exclusive
        cursor of the previous page's ``next_after_message_id``.
        """
        query: dict[str, str] = {"limit": str(limit)}
        if after_message_id is not None:
            query["after_message_id"] = after_message_id
        path = f"/session/{_path(session_id)}/messages"
        return _message_page(self._call("GET", path, query=query, timeout_s=timeout_s))

    def reply_permission(self, permission_id: str, reply: str, *, timeout_s: float | None = None) -> DecisionReceipt:
        """``POST /permission/{id}/reply`` — the host approval channel (§7 L316).

        The vocabulary is ``once``/``always``/``reject``; the first legal
        transition wins, and a late reply answers 409 Conflict (the receipt
        is an ack, not a reusable permission).
        """
        path = f"/permission/{_path(permission_id)}/reply"
        return _decision_receipt(self._call("POST", path, body={"reply": reply}, timeout_s=timeout_s))

    def stream_events(
        self,
        session_id: str,
        *,
        after: str | None = None,
        timeout_s: float | None = None,
    ) -> Generator[DurableEvent[Any], None, None]:
        """``GET /session/{id}/events`` — the durable SSE stream as typed events (§7 L105).

        The connection is issued lazily on the first ``next()``: a refused
        stream (401/404, or 400 for a malformed / cross-session / beyond-H
        ``after`` cursor) raises :class:`OpencodeApiError` from the first
        iteration, not from this call. ``after`` is the raw Last-Event-ID
        cursor ``"{session_id}:{seq}"`` — see :meth:`resume_cursor`. The
        terminal ``event: error`` frame raises :class:`OpencodeStreamError`;
        comments (keepalive, ``synced``) are skipped; envelope ``evt_`` ids
        are deduplicated within this one stream. The first version is a
        single connection with no auto-reconnect: close the generator, hold
        :meth:`resume_cursor`, and call again.
        """

        def _generate() -> Generator[DurableEvent[Any], None, None]:
            response = self._open_stream(session_id, after=after, timeout_s=timeout_s)
            try:
                yield from stream_durable_events(
                    response,
                    registry=self._registry,
                    on_cursor=lambda cursor: self._on_cursor(session_id, cursor),
                )
            finally:
                response.close()

        return _generate()

    def resume_cursor(self, session_id: str) -> str | None:
        """The Last-Event-ID resume cursor of the most recent ``stream_events`` read.

        Updated per event frame as the stream reads (before dedup), so after
        a disconnect the caller reconnects with
        ``stream_events(session_id, after=client.resume_cursor(session_id))``
        and receives exactly the missed events. ``None`` until a stream for
        this session has yielded a frame.
        """
        return self._last_event_ids.get(session_id)

    def _on_cursor(self, session_id: str, cursor: str) -> None:
        self._last_event_ids[session_id] = cursor
