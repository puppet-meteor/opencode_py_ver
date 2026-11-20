"""MCP server manager (R4-S2; CORE-CONTRACTS §6 L304, r4-slices §2.1; T-C3-006).

Owns each stdio server's lifecycle: spawn -> ``initialize`` handshake ->
``tools/list`` -> ``tools/call``, with the three timeout faces of TS
``mcp/client.ts`` L35-40 (startup/catalog/execution). A disconnect marks
the server failed with the transport's exit diagnostics; the in-flight
call surfaces as *unknown* and is never auto-retried (§8 L354), and at
most a fresh connection is built for the next explicit call (§6 L304
"最多重建连接供下一次显式调用") -- there is no background reconnect.

``tools/list`` failing marks the server failed instead of leaving it
connected with a silently empty tool list (TS ``mcp/index.ts`` L455-479
startServer precedent). The TS ``recover``/``recovering`` HTTP-session
semantics are deliberately not ported (r4-slices §4 difference 3).

Config is not read here: the manager takes explicit caller-constructed
definitions. "启动前宿主确认完整本机权限" (§6 L304) is the caller's
explicit injection act; ConfigSnapshot/MCP refs are the S3 face
(r4-slices §2.2) -- a server definition carries the complete minimal
environment with secret refs already resolved.

T-C3-006 (upstream 2.3.2 mcp cluster, stdio-only trim):

- **Connection events**: the TS ``McpConnection.Events`` callback table
  maps onto :meth:`McpManager.add_event_listener` -- ``connecting`` /
  ``connected`` / ``failed`` / ``closed`` / ``changed`` fire inline, in
  order, so an owner (the future event-trigger ticket) can keep its own
  state in step with the connection. The TS pool/Location face is not
  ported (single process, single directory -- every connection private).
- **Protocol**: the client declares ``2026-07-28`` (decision 3: declare
  + accept the server's reply as-is; the TS SDK's discover/pin legs stay
  unported) and announces the MCP Events draft extension
  (``io.modelcontextprotocol/events``); a server that answers the
  capability is exposed through :meth:`events_supported`.
- **Notification routing**: ``notifications/tools/list_changed`` re-lists
  the catalog in place (the hot refresh); ``notifications/events/*``
  notifications route by their ``io.modelcontextprotocol/subscriptionId``
  meta (the stream request's JSON-RPC id) into
  :meth:`McpManager.stream_events` queues -- late notifications (after
  the stream ended) are dropped by id.
- **MCP Events** (SEP-3415 draft, experimental):
  :meth:`list_events` paginates ``events/list`` (up to
  :data:`EVENT_LIST_PAGES` pages) and :meth:`stream_events` holds one
  ``events/stream`` request open, yielding ``active`` / ``event`` /
  ``heartbeat`` / ``error`` messages; :data:`EVENT_STREAM_IDLE_S` of
  silence (no heartbeats) means the stream is dead. The two terminal
  faces raise :class:`EventStreamRejectedError` (the request can never
  succeed as sent) and :class:`EventStreamTerminatedError` (the server
  ended the subscription) -- a clean stream END is not an error: the
  generator returns and the caller may reopen from the last cursor.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from opencode.contracts.messages import JsonObject, JsonValue
from opencode.mcp.stdio import McpRemoteError, McpTransportError, StdioTransport

#: OP-R4-5 (TS client.ts L35 ``DEFAULT_STARTUP_TIMEOUT = 30_000``): the
#: ``initialize`` handshake deadline. Kept at the TS value.
DEFAULT_STARTUP_TIMEOUT_S = 30.0

#: OP-R4-5 (TS client.ts L36 ``DEFAULT_CATALOG_TIMEOUT = 30_000``): the
#: ``tools/list`` deadline. Kept at the TS value.
DEFAULT_CATALOG_TIMEOUT_S = 30.0

#: OP-R4-5 (TS client.ts L37 ``DEFAULT_EXECUTION_TIMEOUT = 12h``): the
#: ``tools/call`` deadline. Kept at the TS value: the contract pins only
#: that a timeout exists (§6 L304), shortening it would break long but
#: legitimate tools, so the sheer length is a backstop, not a policy.
DEFAULT_EXECUTION_TIMEOUT_S = 12 * 60 * 60.0

#: T-C3-006 decision 3: the protocol revision this client DECLARES. The
#: server's reply is accepted as-is (the TS SDK's era negotiation --
#: discover/pin -- is SDK-internal and not ported, ④ disclosure).
MCP_PROTOCOL_VERSION = "2026-07-28"

#: The MCP Events draft extension (SEP-3415, upstream client.ts L72-73):
#: the capability key announced in ``initialize`` and the notification
#: routing meta key (the stream request's JSON-RPC id).
EVENTS_EXTENSION = "io.modelcontextprotocol/events"
SUBSCRIPTION_ID_META = "io.modelcontextprotocol/subscriptionId"

#: Upstream client.ts L74-76: servers heartbeat at least every 30s; twice
#: that in silence means the stream is dead.
EVENT_STREAM_IDLE_S = 60.0

#: Upstream client.ts L76: the ``events/list`` pagination bound.
EVENT_LIST_PAGES = 100

#: Upstream client.ts L135: JSON-RPC error codes that mean an
#: ``events/stream`` request can never succeed as sent.
EVENT_STREAM_REJECTED_CODES = frozenset({-32601, -32602, -32023, -32024, -32026})

#: The client identity announced in the ``initialize`` handshake.
CLIENT_NAME = "opencode-py"
CLIENT_VERSION = "0.0.1"

McpServerStatus = Literal["pending", "connected", "failed", "stopped"]

#: One message on an event stream (upstream client.ts L117-131
#: ``EventMessage``): a plain dict with a ``type`` key of ``active`` /
#: ``event`` / ``heartbeat`` / ``error``.
MCPEventMessage = dict[str, Any]

#: A connection callback: ``(server, event, payload)`` with event one of
#: ``connecting`` / ``connected`` / ``failed{error}`` / ``closed{reason}``
#: / ``changed{kind}`` (TS connection.ts Events, tools-only trim).
McpEventListener = Callable[[str, str, dict[str, Any]], None]


def resolve_cwd(cwd: str | None, base: str | None = None) -> str | None:
    """Resolve a definition's ``cwd`` (T-C3-006 decision 7, TS stdio.ts).

    ``~``/``~user`` expand first; a relative path resolves against
    ``base`` (the host's working directory) when one is given, else it
    passes through unchanged (the subprocess resolves it against the
    process cwd). Absolute paths normalize only.
    """
    if cwd is None:
        return None
    expanded = os.path.expanduser(cwd)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    if base is not None:
        return os.path.normpath(os.path.join(base, expanded))
    return expanded


@dataclass(frozen=True, slots=True)
class StdioServerDefinition:
    """One explicitly trusted stdio server (§6 L304 first sentence).

    ``env`` is the complete minimal environment the server gets -- argv
    is exec'd directly (never through a shell) and no host environment is
    merged (secret refs are the caller's to resolve before injection;
    the config face is S3).
    """

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPRemoteTool:
    """One entry of a server's ``tools/list`` (the raw server-declared shape)."""

    name: str
    description: str = ""
    input_schema: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPTextContent:
    """An MCP text content item (``{type: "text", text}``)."""

    text: str


@dataclass(frozen=True, slots=True)
class MCPMediaContent:
    """An MCP media content item (``{type: "media", data, mimeType}``)."""

    data: str
    mime_type: str


MCPContent = MCPTextContent | MCPMediaContent


@dataclass(frozen=True, slots=True)
class MCPCallToolResult:
    """TS client.ts L96-100 ``CallToolResult = {isError, structured, content}``."""

    is_error: bool = False
    structured: JsonValue | None = None
    content: tuple[MCPContent, ...] = ()


class McpConnectError(Exception):
    """A server failed to spawn/handshake/catalog (TS ``MCP.ConnectError`` analog)."""


class McpUnknownOutcomeError(Exception):
    """A call timed out or the connection dropped mid-call: the result is unknown.

    §6 L304: never auto-retried here (§8 L354: MCP does not auto-retry);
    at most the next explicit call builds a fresh connection.
    """


class EventStreamRejectedError(Exception):
    """The server refused the subscription (unknown event, bad arguments, no access).

    The request can never succeed as sent; the terminal "rejected" face
    (upstream event-errors.ts, experimental draft extension).
    """

    def __init__(self, server: str, code: int, message: str) -> None:
        super().__init__(f"MCP server {server!r} rejected the event stream ({code}): {message}")
        self.server = server
        self.code = code
        self.message = message


class EventStreamTerminatedError(Exception):
    """The server ended the subscription (access revoked, event type removed).

    The terminal "terminated" face: a watch that receives it must not
    reopen (upstream event-errors.ts, experimental draft extension).
    """

    def __init__(self, server: str, code: int, message: str) -> None:
        super().__init__(f"MCP server {server!r} terminated the event stream ({code}): {message}")
        self.server = server
        self.code = code
        self.message = message


class _ServerEntry:
    """Per-server bookkeeping (the status faces mirror TS index.ts ServerEntry)."""

    __slots__ = ("definition", "error", "events", "status", "tools", "transport")

    def __init__(self, definition: StdioServerDefinition) -> None:
        self.definition = definition
        self.transport: StdioTransport | None = None
        self.tools: tuple[MCPRemoteTool, ...] | None = None
        self.status: McpServerStatus = "pending"
        self.error: Exception | None = None
        self.events = False


class McpManager:
    """The per-server lifecycle owner (§6 L304: the manager owns subprocesses).

    All timeouts are injectable for tests; the defaults are the OP-R4-5
    conservative values above. ``close`` kills every server's process
    tree (graceful EOF -> escalated tree kill, the R3-S5 discipline).
    """

    def __init__(
        self,
        *,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
        catalog_timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
        execution_timeout_s: float = DEFAULT_EXECUTION_TIMEOUT_S,
        base_dir: str | None = None,
    ) -> None:
        for label, value in (
            ("startup_timeout_s", startup_timeout_s),
            ("catalog_timeout_s", catalog_timeout_s),
            ("execution_timeout_s", execution_timeout_s),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
                raise ValueError(f"{label} must be a positive number of seconds")
        self._startup_timeout_s = float(startup_timeout_s)
        self._catalog_timeout_s = float(catalog_timeout_s)
        self._execution_timeout_s = float(execution_timeout_s)
        self._base_dir = base_dir
        self._servers: dict[str, _ServerEntry] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._listeners: list[McpEventListener] = []
        #: Open event streams keyed by (server, str(request id)) -- the
        #: routing table the notifications/events/* notifications hit.
        self._event_queues: dict[tuple[str, str], asyncio.Queue[Any]] = {}

    # -- registration ------------------------------------------------------

    def add_server(self, definition: StdioServerDefinition) -> None:
        """Register one explicitly trusted server (§6 L304: the trust act is the injection)."""
        if self._closed:
            raise ValueError("McpManager is closed")
        if not isinstance(definition.name, str) or not definition.name:
            raise ValueError("definition.name must be a non-empty string")
        if not isinstance(definition.command, str) or not definition.command:
            raise ValueError(f"MCP server {definition.name!r}: command must be a non-empty string")
        if definition.name in self._servers:
            raise ValueError(f"MCP server {definition.name!r} is already defined")
        self._servers[definition.name] = _ServerEntry(definition)

    def add_event_listener(self, listener: McpEventListener) -> None:
        """Observe connection events (TS McpConnection.Events, tools-only trim)."""
        self._listeners.append(listener)

    # -- lifecycle ---------------------------------------------------------

    async def start_server(self, name: str) -> tuple[MCPRemoteTool, ...]:
        """Spawn + handshake + catalog one server.

        A failure anywhere in the chain marks the server failed (never
        connected-with-empty-tools, TS index.ts L455-479) and raises
        :class:`McpConnectError`; the transport is closed, killing the tree.
        """
        entry = self._require_entry(name)
        if self._closed:
            raise ValueError("McpManager is closed")
        await self._connect(entry)
        tools = entry.tools
        assert tools is not None  # a connected entry always carries a catalog
        return tools

    async def call_tool(self, server: str, tool: str, arguments: JsonObject) -> MCPCallToolResult:
        """Call one tool on ``server`` under the execution timeout.

        A timeout or a transport failure raises
        :class:`McpUnknownOutcomeError` (§6 L304): the call is NOT retried.
        A broken/failed server is re-connected here and only here -- the
        rebuild serves this explicit call, never a background task.
        """
        entry = self._require_entry(server)
        if self._closed:
            raise ValueError("McpManager is closed")
        if entry.status != "connected" or entry.transport is None:
            await self._connect(entry)
        transport = entry.transport
        assert transport is not None
        try:
            result = await transport.request(
                "tools/call",
                {"name": tool, "arguments": dict(arguments)},
                timeout_s=self._execution_timeout_s,
            )
        except TimeoutError as error:
            raise McpUnknownOutcomeError(
                f"MCP tool {server}:{tool} timed out after {self._execution_timeout_s}s (result unknown)"
            ) from error
        except McpTransportError as error:
            # Any wire-level failure during the call (lost process, broken
            # stdin, torn-down frame) leaves the effect unknown -- the server
            # may have received and executed the request.
            raise McpUnknownOutcomeError(f"MCP tool {server}:{tool} outcome is unknown ({error})") from error
        return _parse_call_result(result)

    async def close(self) -> None:
        """Close every server (graceful EOF -> escalated tree kill); idempotent.

        Best-effort per server: a close failure on one server does not
        stop the others from being shut down (the shutdown path never
        abandons the rest for one straggler).
        """
        async with self._close_lock:
            if self._closed:
                return
            for entry in self._servers.values():
                transport = entry.transport
                if transport is not None:
                    try:
                        await transport.close()
                    except Exception:
                        pass  # best-effort shutdown: the next server still gets its close
                entry.status = "stopped"
            self._closed = True

    # -- observation -------------------------------------------------------

    def status(self, name: str) -> McpServerStatus:
        """The server's lifecycle status (pending/connected/failed/stopped)."""
        return self._require_entry(name).status

    def server_error(self, name: str) -> Exception | None:
        """The recorded failure diagnostic of ``name`` (None while healthy)."""
        return self._require_entry(name).error

    def tools(self, name: str) -> tuple[MCPRemoteTool, ...] | None:
        """The server's catalog; None unless a start succeeded (no empty-table fiction)."""
        return self._require_entry(name).tools

    def server_names(self) -> tuple[str, ...]:
        """Every registered server, registration order."""
        return tuple(self._servers)

    def events_supported(self, name: str) -> bool:
        """True once a server's ``initialize`` answer declared the events extension."""
        return self._require_entry(name).events

    @property
    def closed(self) -> bool:
        """True once :meth:`close` finished."""
        return self._closed

    # -- MCP events (T-C3-006) ---------------------------------------------

    async def list_events(self, server: str) -> tuple[dict[str, Any], ...]:
        """Every event type the server offers (``events/list``, paginated).

        Follows ``nextCursor`` up to :data:`EVENT_LIST_PAGES` pages (more
        pages than read logs-and-stops, the upstream face). Raises
        :class:`McpConnectError` when the server is not connected or did
        not declare the extension.
        """
        entry = self._require_entry(server)
        transport = entry.transport
        if entry.status != "connected" or transport is None or not entry.events:
            raise McpConnectError(f"MCP server {server!r} is not connected or offers no events")
        events: list[dict[str, Any]] = []
        cursor: str | None = None
        for _page in range(EVENT_LIST_PAGES):
            params: dict[str, object] = {} if cursor is None else {"cursor": cursor}
            result = await transport.request("events/list", params, timeout_s=self._catalog_timeout_s)
            page = _parse_event_list(result)
            events.extend(page["events"])
            cursor = page["nextCursor"]
            if cursor is None:
                return tuple(events)
        return tuple(events)  # more pages than read: stop, as upstream does

    async def stream_events(
        self,
        server: str,
        event_name: str,
        *,
        arguments: JsonObject | None = None,
        cursor: str | None = None,
        idle_timeout_s: float = EVENT_STREAM_IDLE_S,
    ) -> AsyncGenerator[MCPEventMessage, None]:
        """Hold one ``events/stream`` request open, yielding its messages.

        ``active`` / ``event`` / ``heartbeat`` / ``error`` messages arrive
        as ``notifications/events/*`` routed by the subscription id; a
        quiet stream dies after ``idle_timeout_s`` (heartbeats, not a
        request timeout, tell quiet from dead -- TS client.ts L594-595).
        The server closing the stream ENDS the iterator cleanly (reopen
        from the last cursor is the caller's call); the two terminal
        faces raise :class:`EventStreamRejectedError` (request can never
        succeed) and :class:`EventStreamTerminatedError` (subscription
        ended; do not reopen).
        """
        entry = self._require_entry(server)
        transport = entry.transport
        if entry.status != "connected" or transport is None or not entry.events:
            raise McpConnectError(f"MCP server {server!r} is not connected or offers no events")
        queue: asyncio.Queue[Any] = asyncio.Queue()
        params: dict[str, object] = {"name": event_name}
        if arguments is not None:
            params["arguments"] = dict(arguments)
        if cursor is not None:
            params["cursor"] = cursor
        assigned: dict[str, int] = {}

        def on_id(request_id: int) -> None:
            assigned["id"] = request_id
            self._event_queues[(server, str(request_id))] = queue

        request_task = asyncio.create_task(transport.request("events/stream", params, timeout_s=None, on_id=on_id))

        def _request_done(task: asyncio.Task[object]) -> None:
            if task.cancelled():
                return
            error = task.exception()
            queue.put_nowait(error if error is not None else None)

        request_task.add_done_callback(_request_done)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=idle_timeout_s)
                except TimeoutError as error:
                    raise McpTransportError(
                        f"MCP server {server!r} sent nothing on the event stream for {idle_timeout_s}s"
                    ) from error
                if isinstance(item, BaseException):
                    if isinstance(item, McpRemoteError) and item.code in EVENT_STREAM_REJECTED_CODES:
                        raise EventStreamRejectedError(server, item.code, item.message) from item
                    raise item
                if item is None:
                    return  # the server closed the stream: a clean end
                kind, payload = item
                try:
                    message = _event_message(kind, payload)
                except _TerminatedSignal as signal:
                    # The terminal face leaves the generator as its public
                    # exception (the consumer never sees the internal class).
                    raise EventStreamTerminatedError(server, signal.code, signal.message) from None
                if message is not None:
                    yield message
        finally:
            request_id = assigned.get("id")
            if request_id is not None:
                self._event_queues.pop((server, str(request_id)), None)
            if not request_task.done():
                request_task.cancel()
                try:
                    await request_task
                except (asyncio.CancelledError, Exception):
                    pass
                # The server may keep pushing on an abandoned stream: tell it
                # the request is gone (JSON-RPC cancelled notification).
                if not transport.closed:
                    try:
                        await transport.notify("notifications/cancelled", {"id": request_id})
                    except McpTransportError:
                        pass  # the pipe died with the stream: nothing to tell

    # -- internals ---------------------------------------------------------

    def _require_entry(self, name: str) -> _ServerEntry:
        entry = self._servers.get(name)
        if entry is None:
            raise ValueError(f"unknown MCP server {name!r}")
        return entry

    def _emit(self, server: str, event: str, payload: dict[str, Any] | None = None) -> None:
        """Run the listeners inline, in order (TS Events callback contract)."""
        for listener in tuple(self._listeners):
            try:
                listener(server, event, payload or {})
            except Exception:
                pass  # one listener's failure never breaks the connection face

    def _mark_lost(self, name: str, error: Exception) -> None:
        """Unexpected-exit callback: a connected server is demoted to failed.

        The in-flight call (if any) fails through the transport's pending
        futures and surfaces as unknown at the caller; no reconnect is
        scheduled (§6 L304 -- at most the next explicit call rebuilds).
        """
        entry = self._servers.get(name)
        if entry is None or entry.status != "connected":
            return
        entry.status = "failed"
        entry.error = error
        entry.events = False
        self._emit(name, "closed", {"reason": str(error)})

    def _make_notification_handler(self, server: str) -> Callable[[str, dict[str, object] | None], None]:
        """One transport's notification router (hot refresh + stream queues)."""

        def handler(method: str, params: dict[str, object] | None) -> None:
            if method == "notifications/tools/list_changed":
                asyncio.get_running_loop().create_task(self._refresh_tools(server))
                return
            if not method.startswith("notifications/events/"):
                return
            kind = method[len("notifications/events/") :]
            subscription: object = None
            if params is not None:
                meta = params.get("_meta")
                if isinstance(meta, dict):
                    subscription = meta.get(SUBSCRIPTION_ID_META)
            queue = self._event_queues.get((server, str(subscription)))
            if queue is not None:
                queue.put_nowait((kind, params))
            # No queue (or a late notification after the stream ended):
            # dropped by id, as the correlation discards late responses.

        return handler

    async def _refresh_tools(self, server: str) -> None:
        """``notifications/tools/list_changed``: re-list the catalog in place.

        A failed refresh keeps the old catalog (the connection is not
        torn down for a straggler list -- the next explicit call decides);
        a successful one swaps the catalog and emits ``changed``.
        """
        entry = self._servers.get(server)
        transport = entry.transport if entry is not None else None
        if entry is None or transport is None or entry.status != "connected":
            return
        try:
            listing = await transport.request("tools/list", {}, timeout_s=self._catalog_timeout_s)
            tools = _parse_tools(listing)
        except (TimeoutError, McpTransportError, ValueError):
            return  # keep the last known catalog; no reconnect (§6 L304)
        entry.tools = tools
        self._emit(server, "changed", {"kind": "tools"})

    async def _connect(self, entry: _ServerEntry) -> StdioTransport:
        """Spawn + initialize + initialized + tools/list (the TS connect chain)."""
        definition = entry.definition
        server = definition.name
        entry.status = "pending"
        entry.error = None
        entry.tools = None
        entry.events = False
        self._emit(server, "connecting")
        transport = StdioTransport(
            command=definition.command,
            args=definition.args,
            cwd=resolve_cwd(definition.cwd, self._base_dir),
            env=definition.env,
            server=server,
            on_unexpected_exit=lambda error: self._mark_lost(server, error),
            on_notification=self._make_notification_handler(server),
        )
        try:
            await transport.start()
            initialize_result = await transport.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"extensions": {EVENTS_EXTENSION: {}}},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                timeout_s=self._startup_timeout_s,
            )
            if not isinstance(initialize_result, dict) or not isinstance(initialize_result.get("serverInfo"), dict):
                raise McpTransportError("initialize result has no serverInfo object")
            entry.events = _declares_events(initialize_result)
            await transport.notify("notifications/initialized")
            listing = await transport.request("tools/list", {}, timeout_s=self._catalog_timeout_s)
            tools = _parse_tools(listing)
        except (TimeoutError, McpTransportError, ValueError) as error:
            await transport.close()
            entry.status = "failed"
            entry.error = error
            self._emit(server, "failed", {"error": str(error)})
            raise McpConnectError(f"MCP server {server!r} failed to start: {error}") from error
        entry.transport = transport
        entry.tools = tools
        entry.status = "connected"
        self._emit(server, "connected")
        return transport


def _declares_events(initialize_result: dict[str, object]) -> bool:
    """The server answered the events extension (capabilities.extensions)."""
    capabilities = initialize_result.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    extensions = capabilities.get("extensions")
    return isinstance(extensions, dict) and EVENTS_EXTENSION in extensions


def _parse_event_list(result: object) -> dict[str, Any]:
    """Validate one ``events/list`` page (``{events, nextCursor?}``)."""
    if not isinstance(result, dict) or not isinstance(result.get("events"), list):
        raise ValueError("events/list result has no events array")
    events: list[dict[str, Any]] = []
    for item in result["events"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            continue  # a malformed entry lists nothing (skip, never fail the page)
        events.append(item)
    cursor = result.get("nextCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("events/list nextCursor is not a string")
    return {"events": events, "nextCursor": cursor}


def _event_message(kind: str, params: dict[str, object] | None) -> MCPEventMessage | None:
    """Normalize one ``notifications/events/<kind>`` into an EventMessage.

    Unknown kinds return None (ignored, the upstream SDK-handler trim);
    ``terminated`` is NOT a message -- it is the terminal exception the
    stream machine raises (the caller of :meth:`McpManager.stream_events`
    sees :class:`EventStreamTerminatedError`).
    """
    payload: dict[str, object] = params or {}
    if kind == "terminated":
        error = payload.get("error")
        detail = error if isinstance(error, dict) else {}
        raise _TerminatedSignal(
            int(detail.get("code", 0)) if isinstance(detail.get("code"), int) else 0,
            str(detail.get("message", "")),
        )
    if kind == "active":
        return {
            "type": "active",
            "cursor": payload.get("cursor"),
            "truncated": bool(payload.get("truncated", False)),
        }
    if kind == "event":
        return {
            "type": "event",
            "eventId": str(payload.get("eventId", "")),
            "name": str(payload.get("name", "")),
            "timestamp": str(payload.get("timestamp", "")),
            "data": payload.get("data"),
            "cursor": payload.get("cursor"),
        }
    if kind == "heartbeat":
        return {"type": "heartbeat", "cursor": payload.get("cursor")}
    if kind == "error":
        return {"type": "error", "error": payload.get("error")}
    return None


class _TerminatedSignal(Exception):
    """Internal: a terminated notification surfaces as the terminal exception."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"terminated ({code}): {message}")
        self.code = code
        self.message = message


def _parse_tools(listing: object) -> tuple[MCPRemoteTool, ...]:
    """Validate a ``tools/list`` result into remote-tool descriptors.

    T-C3-006 decision 8 (upstream client.ts L633-639): per-tool -- one
    malformed entry is SKIPPED, it no longer fails the whole list (a
    broken tool must not take the server's healthy tools down).
    """
    if not isinstance(listing, dict) or not isinstance(listing.get("tools"), list):
        raise ValueError("tools/list result has no tools array")
    tools: list[MCPRemoteTool] = []
    for item in listing["tools"]:
        if not isinstance(item, dict) or isinstance(item.get("name"), bool) or not isinstance(item.get("name"), str):
            continue  # skip + keep going: one bad entry is not the server's fault
        description = item.get("description", "")
        if not isinstance(description, str):
            continue
        schema = item.get("inputSchema", {"type": "object"})
        if not isinstance(schema, dict):
            continue
        tools.append(MCPRemoteTool(name=item["name"], description=description, input_schema=schema))
    return tuple(tools)


def _parse_call_result(result: object) -> MCPCallToolResult:
    """Validate a ``tools/call`` result into :class:`MCPCallToolResult`."""
    if not isinstance(result, dict):
        raise ValueError("tools/call result is not an object")
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        raise ValueError("tools/call isError is not a boolean")
    structured = result.get("structuredContent")
    raw_content = result.get("content", [])
    if not isinstance(raw_content, list):
        raise ValueError("tools/call content is not an array")
    content: list[MCPContent] = []
    for item in raw_content:
        if not isinstance(item, dict):
            raise ValueError("tools/call content item is not an object")
        if item.get("type") == "text":
            text = item.get("text", "")
            if not isinstance(text, str):
                raise ValueError("tools/call text content is not a string")
            content.append(MCPTextContent(text=text))
            continue
        data = item.get("data")
        mime_type = item.get("mimeType")
        if isinstance(data, str) and isinstance(mime_type, str):
            content.append(MCPMediaContent(data=data, mime_type=mime_type))
        # Any other content type is dropped (④ disclosed).
    return MCPCallToolResult(
        is_error=is_error,
        structured=structured,
        content=tuple(content),
    )
