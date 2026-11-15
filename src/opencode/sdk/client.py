"""The Python client over the §7 HTTP face (P2-S5 / M5-C, PHASE2-MODULES §7).

One :class:`OpencodeClient` mirrors the routes of the single-user
server (the 054/056 faces + the T-C1-094 session resource group) over the
real wire — stdlib ``urllib`` only: no fastapi, no uvicorn, no
``opencode.httpapi`` import (a pure client stays on the core dependency
surface; L101: the SDK directory is new and the runner is not rewritten;
L112: with M5 alone an external UI attaches to the single-process
service):

* ``POST /session``                          -> :meth:`OpencodeClient.create_session`
* ``POST /session/{session_id}/prompt``      -> :meth:`OpencodeClient.prompt`
* ``POST /run/{run_id}/cancel``              -> :meth:`OpencodeClient.cancel_run`
* ``GET  /run/{run_id}/status``              -> :meth:`OpencodeClient.run_status`
* ``GET  /session/{session_id}/messages``    -> :meth:`OpencodeClient.list_messages`
* ``POST /permission/{permission_id}/reply`` -> :meth:`OpencodeClient.reply_permission`
* ``GET  /session/{session_id}/events``      -> :meth:`OpencodeClient.stream_events`

T-C1-094 (P6-S1) adds the seven session resource routes (the TS session
group subset, same flat JSON wire):

* ``GET    /session``                               -> :meth:`OpencodeClient.list_sessions`
* ``GET    /session/{session_id}``                  -> :meth:`OpencodeClient.get_session`
* ``DELETE /session/{session_id}``                  -> :meth:`OpencodeClient.delete_session`
* ``POST   /session/{session_id}/fork``             -> :meth:`OpencodeClient.fork_session`
* ``GET    /session/{session_id}/export``           -> :meth:`OpencodeClient.export_session`
* ``POST   /session/import``                        -> :meth:`OpencodeClient.import_session`
* ``GET    /session/{session_id}/message/{message_id}`` -> :meth:`OpencodeClient.get_message`

T-C1-095 (P6-S2) adds the global volatile event stream (the TS
``event.subscribe`` face, ``GET /events``):

* ``GET    /events``                                -> :meth:`OpencodeClient.stream_global_events`

T-C1-096 (P6-S3) adds the manual compaction route (the synchronous manual
twin of the server's post-settlement auto trigger):

* ``POST   /session/{session_id}/compact``          -> :meth:`OpencodeClient.compact_session`

Return types are the core models themselves (``service.RunRef`` /
``RunStatus`` / ``DecisionReceipt`` / ``SessionRef`` / ``SessionInfo`` /
``SessionSummary`` / ``SessionExport``, ``repos.MessagePage``,
``contracts.DurableEvent``, ``messages.SessionMessage``) — the wire JSON
is decoded here and never re-shaped into SDK-local types, so an SDK
consumer sees exactly the session layer's vocabulary.

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
receives exactly the missed events. :meth:`stream_global_events` is the
global volatile twin (T-C1-095): it yields the synthetic
:class:`~opencode.sdk.sse.ServerConnected` marker first, then every live bus
event across sessions — and it never resumes: a reconnect starts fresh, and
events published while disconnected are missed (the session stream above
owns durable replay).
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator, Iterable
from dataclasses import dataclass
from typing import Any, Literal, overload

from pydantic import ValidationError

from opencode.contracts.errors import CoreError
from opencode.contracts.events import DurableEvent, default_event_registry
from opencode.contracts.llm import Usage
from opencode.contracts.messages import (
    AssistantMessage,
    IdleMessage,
    JsonObject,
    SessionMessage,
    UserMessage,
)
from opencode.sdk.errors import OpencodeApiError, OpencodeClientError, OpencodeConnectionError
from opencode.sdk.sse import GlobalStreamItem, stream_durable_events, stream_global_events
from opencode.session.service import (
    DecisionReceipt,
    RunRef,
    RunStatus,
    RunStatusValue,
    SessionExport,
    SessionInfo,
    SessionRef,
    SessionSummary,
)
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
    items = [_wire_message(raw_item) for raw_item in raw_items]
    next_after = fields["next_after_message_id"]
    if next_after is not None and not isinstance(next_after, str):
        raise OpencodeClientError(f"wire field 'next_after_message_id' must be a string or null, got {next_after!r}")
    has_more = fields["has_more"]
    if not isinstance(has_more, bool):
        raise OpencodeClientError(f"wire field 'has_more' must be a boolean, got {has_more!r}")
    return MessagePage(items=items, next_after_message_id=next_after, has_more=has_more)


def _wire_message(raw: Any) -> SessionMessage:
    """One §2.1 message wire object through the tag dispatch, fail closed.

    Shared by the messages page and the single-message / export faces (the
    server only emits decodable rows, so an undecodable item is a wire
    contract break, never a skipped entry).
    """
    if not isinstance(raw, dict):
        raise OpencodeClientError(f"a message item must be a JSON object, got {raw!r}")
    model = _MESSAGE_MODELS.get(str(raw.get("type")))
    if model is None:
        raise OpencodeClientError(f"unknown projected message type tag {raw.get('type')!r}")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise OpencodeClientError(f"a message item did not decode: {error}") from error


def _int_field(fields: dict[str, Any], key: str) -> int:
    """A wire integer field, bools rejected (the codebase's numeric-arg guard)."""
    value = fields[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpencodeClientError(f"wire field {key!r} must be an integer, got {value!r}")
    return value


def _nullable_string_field(fields: dict[str, Any], key: str) -> str | None:
    """A wire string-or-null field (``active_run_id`` on the get/list faces)."""
    value = fields[key]
    if value is not None and not isinstance(value, str):
        raise OpencodeClientError(f"wire field {key!r} must be a string or null, got {value!r}")
    return value


def _session_info(payload: Any) -> SessionInfo:
    """The T-C1-094 four-field get wire, checked exactly."""
    fields = _expect_object(payload, frozenset({"session_id", "status", "applied_seq", "active_run_id"}))
    return SessionInfo(
        session_id=_string_field(fields, "session_id"),
        status=_string_field(fields, "status"),
        applied_seq=_int_field(fields, "applied_seq"),
        active_run_id=_nullable_string_field(fields, "active_run_id"),
    )


def _session_summary(payload: Any) -> SessionSummary:
    """One T-C1-094 list element: the get wire plus the created stamp."""
    fields = _expect_object(
        payload,
        frozenset({"session_id", "status", "applied_seq", "active_run_id", "created_at_ms"}),
    )
    return SessionSummary(
        session_id=_string_field(fields, "session_id"),
        status=_string_field(fields, "status"),
        applied_seq=_int_field(fields, "applied_seq"),
        active_run_id=_nullable_string_field(fields, "active_run_id"),
        created_at_ms=_int_field(fields, "created_at_ms"),
    )


def _session_export(payload: Any) -> SessionExport:
    """The T-C1-094 transfer payload, checked exactly (import's input shape)."""
    fields = _expect_object(
        payload,
        frozenset(
            {"session_id", "status", "working_dir", "config_snapshot", "snapshot_hash", "created_at_ms", "messages"}
        ),
    )
    config_snapshot = fields["config_snapshot"]
    if not isinstance(config_snapshot, dict):
        raise OpencodeClientError(f"wire field 'config_snapshot' must be a JSON object, got {config_snapshot!r}")
    raw_messages = fields["messages"]
    if not isinstance(raw_messages, list):
        raise OpencodeClientError(f"wire field 'messages' must be a list, got {raw_messages!r}")
    return SessionExport(
        session_id=_string_field(fields, "session_id"),
        status=_string_field(fields, "status"),
        working_dir=_string_field(fields, "working_dir"),
        config_snapshot=config_snapshot,
        snapshot_hash=_string_field(fields, "snapshot_hash"),
        created_at_ms=_int_field(fields, "created_at_ms"),
        messages=tuple(_wire_message(raw) for raw in raw_messages),
    )


@dataclass(frozen=True)
class InboxRef:
    """The T-C1-100 parked-admission wire (P7-S1 §4-1).

    The prompt face's explicit-delivery busy answer: the item parks in the
    durable inbox. An idle session never returns this ref — its item is
    admitted and started atomically, answering the three-field
    :class:`~opencode.session.service.RunRef` wire instead (the decode
    branch dispatches on ``inbox_id``).
    """

    session_id: str
    inbox_id: str
    kind: str
    delivery: str
    request_id: str | None


@dataclass(frozen=True)
class PendingInboxItem:
    """One pending inbox row of the T-C1-100 §3-6 list/update faces.

    The parked user turn rides ``message`` (a §2.1 wire message, decoded
    fail closed); ``enqueued_seq`` is the enqueue-order key.
    """

    inbox_id: str
    kind: str
    delivery: str
    request_id: str | None
    enqueued_seq: int
    message: SessionMessage


@dataclass(frozen=True)
class InterruptReceipt:
    """The T-C1-100 §3-5 interrupt wire.

    ``interrupted`` is True only when a live run settled on the interrupted
    terminal; an idle session answers the no-op (run_id/status both None).
    ``resumed_run_id`` carries the resume drive's fresh run (None without
    one).
    """

    interrupted: bool
    run_id: str | None
    status: str | None
    resumed_run_id: str | None


@dataclass(frozen=True)
class CompactionReceipt:
    """The decoded manual-compaction outcome wire (T-C1-096 §3 decision 3).

    ``status`` is the engine's three-value enum; every other field is set
    only for its status face — ``compacted`` carries the settled boundary
    (``covered_seq``/``retained_from``/``message_count``), the summary
    identity (``summary_hash``), the committed event's id and the summary
    call's ``usage``; ``discarded`` carries only the spent ``usage`` of the
    dropped summary (zero commits); ``below_threshold`` carries nothing
    else (no summary call happened). The server's per-status key sets are
    checked exactly, so a field here is evidence the server sent it.
    """

    status: Literal["compacted", "below_threshold", "discarded"]
    covered_seq: int | None = None
    retained_from: int | None = None
    message_count: int | None = None
    summary_hash: str | None = None
    event_id: str | None = None
    usage: Usage | None = None


def _usage_field(fields: dict[str, Any]) -> Usage:
    """The wire usage object decoded through the §2.1 ``Usage`` model, fail closed."""
    raw = fields["usage"]
    if not isinstance(raw, dict):
        raise OpencodeClientError(f"wire field 'usage' must be a JSON object, got {raw!r}")
    try:
        return Usage.model_validate(raw)
    except ValidationError as error:
        raise OpencodeClientError(f"wire field 'usage' did not decode: {error}") from error


def _compaction_receipt(payload: Any) -> CompactionReceipt:
    """The T-C1-096 outcome wire: per-status exact key sets, checked (no shared shape)."""
    if not isinstance(payload, dict):
        raise OpencodeClientError(f"unexpected wire shape: expected a JSON object, got {type(payload).__name__}")
    status = payload.get("status")
    if status == "compacted":
        fields = _expect_object(
            payload,
            frozenset({"status", "covered_seq", "retained_from", "message_count", "summary_hash", "event_id", "usage"}),
        )
        return CompactionReceipt(
            status="compacted",
            covered_seq=_int_field(fields, "covered_seq"),
            retained_from=_int_field(fields, "retained_from"),
            message_count=_int_field(fields, "message_count"),
            summary_hash=_string_field(fields, "summary_hash"),
            event_id=_string_field(fields, "event_id"),
            usage=_usage_field(fields),
        )
    if status == "below_threshold":
        _expect_object(payload, frozenset({"status"}))
        return CompactionReceipt(status="below_threshold")
    if status == "discarded":
        fields = _expect_object(payload, frozenset({"status", "usage"}))
        return CompactionReceipt(status="discarded", usage=_usage_field(fields))
    raise OpencodeClientError(f"unknown compaction status on the wire: {status!r}")


def _inbox_ref(payload: Any) -> InboxRef:
    """The T-C1-100 parked-admission wire, exact key set checked."""
    fields = _expect_object(payload, frozenset({"session_id", "inbox_id", "kind", "delivery", "request_id"}))
    raw_request = fields["request_id"]
    if raw_request is not None and not isinstance(raw_request, str):
        raise OpencodeClientError(f"wire field 'request_id' must be a string or null, got {raw_request!r}")
    return InboxRef(
        session_id=_string_field(fields, "session_id"),
        inbox_id=_string_field(fields, "inbox_id"),
        kind=_string_field(fields, "kind"),
        delivery=_string_field(fields, "delivery"),
        request_id=raw_request,
    )


def _pending_inbox_item(payload: Any) -> PendingInboxItem:
    """One T-C1-100 pending row, exact key set checked (the parked turn decodes fail closed)."""
    fields = _expect_object(
        payload,
        frozenset({"inbox_id", "kind", "delivery", "request_id", "enqueued_seq", "message"}),
    )
    raw_request = fields["request_id"]
    if raw_request is not None and not isinstance(raw_request, str):
        raise OpencodeClientError(f"wire field 'request_id' must be a string or null, got {raw_request!r}")
    return PendingInboxItem(
        inbox_id=_string_field(fields, "inbox_id"),
        kind=_string_field(fields, "kind"),
        delivery=_string_field(fields, "delivery"),
        request_id=raw_request,
        enqueued_seq=_int_field(fields, "enqueued_seq"),
        message=_wire_message(fields["message"]),
    )


def _inbox_cancel_receipt(payload: Any) -> bool:
    """The T-C1-100 cancel wire — True when withdrawn now, False is the no-op."""
    fields = _expect_object(payload, frozenset({"inbox_id", "cancelled"}))
    cancelled = fields["cancelled"]
    if not isinstance(cancelled, bool):
        raise OpencodeClientError(f"wire field 'cancelled' must be a boolean, got {cancelled!r}")
    _string_field(fields, "inbox_id")
    return cancelled


def _interrupt_receipt(payload: Any) -> InterruptReceipt:
    """The T-C1-100 interrupt wire, exact key set checked."""
    fields = _expect_object(payload, frozenset({"interrupted", "run_id", "status", "resumed_run_id"}))
    interrupted = fields["interrupted"]
    if not isinstance(interrupted, bool):
        raise OpencodeClientError(f"wire field 'interrupted' must be a boolean, got {interrupted!r}")
    for key in ("run_id", "status", "resumed_run_id"):
        value = fields[key]
        if value is not None and not isinstance(value, str):
            raise OpencodeClientError(f"wire field {key!r} must be a string or null, got {value!r}")
    return InterruptReceipt(
        interrupted=interrupted,
        run_id=fields["run_id"],
        status=fields["status"],
        resumed_run_id=fields["resumed_run_id"],
    )


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
    """The route-mapped client over one single-user opencode server (§7 L110).

    Construct with the server's base URL and its single-user token; every
    method maps one route, returns the core model, and takes an optional
    ``timeout_s`` override of the constructor default. T-C1-094 adds the
    session resource group (list/get/delete/fork/export/import/message)
    beside the original seven routes.
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

    def _open_global_stream(
        self,
        *,
        types: Iterable[str] | None,
        timeout_s: float | None,
    ) -> http.client.HTTPResponse:
        """Open the global SSE route; the caller owns closing the response.

        ``types`` rides as the ``?types=a,b,c`` query (server-side filter);
        the client-side shape check mirrors the bus's own argument guard, so
        a malformed filter fails before the wire.
        """
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "text/event-stream"}
        path = "/events"
        if types is not None:
            if isinstance(types, str):
                raise ValueError("types must be an iterable of event type names, not a single string")
            names = list(types)
            for name in names:
                if not isinstance(name, str) or not name:
                    raise ValueError("every entry of types must be a non-empty event type name")
            path += "?" + urllib.parse.urlencode({"types": ",".join(names)})
        request = urllib.request.Request(self._base + path, headers=headers, method="GET")
        try:
            response: http.client.HTTPResponse = self._opener.open(request, timeout=self._timeout(timeout_s))
        except urllib.error.HTTPError as error:
            raise _api_error(error) from None
        except (urllib.error.URLError, OSError) as error:
            raise OpencodeConnectionError(f"GET {path} never answered") from error
        return response

    # -- the §7 routes ------------------------------------------------------

    def create_session(self, config: JsonObject, *, timeout_s: float | None = None) -> SessionRef:
        """``POST /session``: one new session under the §6.1 config snapshot.

        ``config`` is the snapshot JSON object (e.g.
        ``ConfigSnapshot.create().model_dump(mode="json")``); an illegal one
        answers 422 ValidationError on the same error face.
        """
        return _session_ref(self._call("POST", "/session", body=config, timeout_s=timeout_s))

    # T-C1-100: same static pin as the service face -- ``delivery=None``
    # (or omitted) is the plain RunRef wire and can never answer InboxRef.
    @overload
    def prompt(
        self, session_id: str, text: str, request_id: str, *, delivery: None = ..., timeout_s: float | None = ...
    ) -> RunRef: ...

    @overload
    def prompt(
        self,
        session_id: str,
        text: str,
        request_id: str,
        *,
        delivery: Literal["steer", "queue"],
        timeout_s: float | None = ...,
    ) -> RunRef | InboxRef: ...

    def prompt(
        self,
        session_id: str,
        text: str,
        request_id: str,
        *,
        delivery: Literal["steer", "queue"] | None = None,
        timeout_s: float | None = None,
    ) -> RunRef | InboxRef:
        """``POST /session/{id}/prompt`` — the request_id idempotency core (§7 L107).

        An identical ``(request_id, text)`` retry returns the original
        :class:`~opencode.session.service.RunRef`; a different text under the
        same request_id answers 409 IdempotencyConflict. T-C1-100 (P7-S1)
        adds the opt-in ``delivery`` passthrough: ``None`` keeps the busy
        hard-refusal face exactly; an explicit ``"steer"``/``"queue"`` parks
        the prompt on a busy session (the returned :class:`InboxRef`) or
        admits-and-starts it on an idle one (the ordinary RunRef wire).
        """
        if delivery is not None and delivery not in ("steer", "queue"):
            raise ValueError('delivery must be None, "steer" or "queue"')
        path = f"/session/{_path(session_id)}/prompt"
        body: JsonObject = {"text": text, "request_id": request_id}
        if delivery is not None:
            body["delivery"] = delivery
        payload = self._call("POST", path, body=body, timeout_s=timeout_s)
        if isinstance(payload, dict) and "inbox_id" in payload:
            return _inbox_ref(payload)
        return _run_ref(payload)

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

    # -- T-C1-094 (P6-S1): the session resource routes ----------------------

    def list_sessions(self, *, timeout_s: float | None = None) -> list[SessionSummary]:
        """``GET /session`` — every live session, newest-created first.

        The v1 face is the full list: no cursor pagination, no filter or
        sort parameters; ``deleted`` sessions never appear (the tombstone's
        projection filter).
        """
        payload = self._call("GET", "/session", timeout_s=timeout_s)
        if not isinstance(payload, list):
            raise OpencodeClientError(f"GET /session answered a non-list body: {payload!r}")
        return [_session_summary(item) for item in payload]

    def get_session(self, session_id: str, *, timeout_s: float | None = None) -> SessionInfo:
        """``GET /session/{id}`` — the four-field get wire.

        A missing or deleted session raises :class:`OpencodeApiError` with
        the 404 §8 body.
        """
        return _session_info(self._call("GET", f"/session/{_path(session_id)}", timeout_s=timeout_s))

    def delete_session(self, session_id: str, *, timeout_s: float | None = None) -> None:
        """``DELETE /session/{id}`` — the tombstone commit; no body comes back (``None``).

        A repeated delete answers 404 and a session with an in-flight run
        answers 409 — both raise :class:`OpencodeApiError` here.
        """
        self._call("DELETE", f"/session/{_path(session_id)}", timeout_s=timeout_s)

    def fork_session(self, session_id: str, *, before: str | None = None, timeout_s: float | None = None) -> SessionRef:
        """``POST /session/{id}/fork`` — copy the projected history into a fresh session.

        ``before`` truncates the copy to the messages before that id
        (exclusive); an omitted ``before`` copies the full history. The
        source session is never changed (a read-only copy); a missing
        source or a ``before`` id that is not one of its messages answers
        404.
        """
        body: JsonObject = {"before": before} if before is not None else {}
        path = f"/session/{_path(session_id)}/fork"
        return _session_ref(self._call("POST", path, body=body, timeout_s=timeout_s))

    def export_session(self, session_id: str, *, timeout_s: float | None = None) -> SessionExport:
        """``GET /session/{id}/export`` — the self-describing transfer payload.

        The result feeds :meth:`import_session` unchanged (the round trip
        is the contract: import restores exactly what export read).
        """
        return _session_export(self._call("GET", f"/session/{_path(session_id)}/export", timeout_s=timeout_s))

    def import_session(self, payload: SessionExport | JsonObject, *, timeout_s: float | None = None) -> SessionRef:
        """``POST /session/import`` — restore one export payload under its own id.

        ``payload`` is the export wire — a :class:`SessionExport` straight
        from :meth:`export_session`, or its JSON object form. A session
        already living at the payload's id answers 409 Conflict
        (:class:`OpencodeApiError`).
        """
        body = payload.to_payload() if isinstance(payload, SessionExport) else payload
        return _session_ref(self._call("POST", "/session/import", body=body, timeout_s=timeout_s))

    def get_message(self, session_id: str, message_id: str, *, timeout_s: float | None = None) -> SessionMessage:
        """``GET /session/{id}/message/{message_id}`` — one projected message (§2.1 wire).

        A message id that exists in another session, or nowhere, answers
        the same 404 (never a cross-session leak).
        """
        path = f"/session/{_path(session_id)}/message/{_path(message_id)}"
        return _wire_message(self._call("GET", path, timeout_s=timeout_s))

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

    # -- T-C1-095 (P6-S2): the global volatile event stream -------------------

    def stream_global_events(
        self,
        *,
        types: Iterable[str] | None = None,
        timeout_s: float | None = None,
    ) -> Generator[GlobalStreamItem, None, None]:
        """``GET /events`` — the global volatile SSE stream as typed items (T-C1-095).

        The TS ``event.subscribe`` parity face: one live broadcast across
        every session. The first ``next()`` issues the connection lazily and
        yields the synthetic :class:`~opencode.sdk.sse.ServerConnected`
        marker; live bus events then arrive as
        :class:`~opencode.contracts.events.DurableEvent` or
        :class:`~opencode.contracts.events.EphemeralEvent` envelopes.
        ``types`` narrows the stream server-side (``?types=a,b``; ``None``
        receives every event type). Volatile by contract — no resume, no
        backfill: a reconnect starts fresh, and events published while
        disconnected are missed (the session stream owns durable replay);
        there is no cursor callback and no dedup on this face. The terminal
        ``event: error`` frame raises :class:`OpencodeStreamError` (e.g. the
        SlowConsumer overflow after this reader stopped consuming); comments
        (keepalive) are skipped. The first version is a single connection
        with no auto-reconnect: close the generator and call again.
        """

        def _generate() -> Generator[GlobalStreamItem, None, None]:
            response = self._open_global_stream(types=types, timeout_s=timeout_s)
            try:
                yield from stream_global_events(response, registry=self._registry)
            finally:
                response.close()

        return _generate()

    # -- T-C1-096 (P6-S3): the manual compaction route --------------------------

    def compact_session(self, session_id: str, *, timeout_s: float | None = None) -> CompactionReceipt:
        """``POST /session/{id}/compact`` — one manual compaction pass (T-C1-096).

        The synchronous single-trip face: the server runs one §2.4 engine
        pass (one summary model call when the threshold holds) and answers
        the flat outcome wire, decoded here into
        :class:`~opencode.sdk.client.CompactionReceipt` —
        ``below_threshold`` is a normal result, never an error. The error
        faces raise :class:`OpencodeApiError`: 404 for a missing or deleted
        session, 409 while a run is in flight, and 422 on a composition
        whose compaction engine is unavailable (no provider wired — the
        fail-closed guard). The timeout should cover one summary call:
        the pass runs inline in this request.
        """
        path = f"/session/{_path(session_id)}/compact"
        return _compaction_receipt(self._call("POST", path, body={}, timeout_s=timeout_s))

    # -- T-C1-100 (P7-S1): the inbox + interrupt faces --------------------------

    def inbox_list(self, session_id: str, *, timeout_s: float | None = None) -> list[PendingInboxItem]:
        """``GET /session/{id}/inbox`` — every pending row, in enqueue order."""
        path = f"/session/{_path(session_id)}/inbox"
        fields = _expect_object(self._call("GET", path, timeout_s=timeout_s), frozenset({"items"}))
        raw_items = fields["items"]
        if not isinstance(raw_items, list):
            raise OpencodeClientError(f"wire field 'items' must be a list, got {raw_items!r}")
        return [_pending_inbox_item(raw) for raw in raw_items]

    def inbox_cancel(self, session_id: str, inbox_id: str, *, timeout_s: float | None = None) -> bool:
        """``DELETE /session/{id}/inbox/{inbox_id}`` — True when withdrawn now, False = the no-op."""
        path = f"/session/{_path(session_id)}/inbox/{_path(inbox_id)}"
        return _inbox_cancel_receipt(self._call("DELETE", path, timeout_s=timeout_s))

    def inbox_update(
        self, session_id: str, inbox_id: str, delivery: Literal["steer", "queue"], *, timeout_s: float | None = None
    ) -> PendingInboxItem:
        """``PATCH /session/{id}/inbox/{inbox_id}`` — re-arm one pending item's delivery.

        A flip to ``"steer"`` is the doorbell: the active run's next step
        boundary consumes it. An item no longer pending answers 409
        Conflict (the §3-6 update face is not a no-op, unlike cancel).
        """
        if delivery not in ("steer", "queue"):
            raise ValueError('delivery must be "steer" or "queue"')
        path = f"/session/{_path(session_id)}/inbox/{_path(inbox_id)}"
        return _pending_inbox_item(self._call("PATCH", path, body={"delivery": delivery}, timeout_s=timeout_s))

    def interrupt(self, session_id: str, *, resume: bool = False, timeout_s: float | None = None) -> InterruptReceipt:
        """``POST /session/{id}/interrupt?resume=`` — stop the active run; resume drives pending steering.

        ``interrupted`` is True only when a live run settled on the
        interrupted terminal; an idle session answers the no-op. With
        ``resume=True`` the settled interrupt promotes the oldest pending
        steer into a fresh run (``resumed_run_id``); queued prompts stay
        parked under both faces.
        """
        query: dict[str, str] | None = {"resume": "true"} if resume else None
        path = f"/session/{_path(session_id)}/interrupt"
        return _interrupt_receipt(self._call("POST", path, query=query, timeout_s=timeout_s))
