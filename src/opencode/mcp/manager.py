"""MCP server manager (R4-S2; CORE-CONTRACTS §6 L304, r4-slices §2.1).

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
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from opencode.contracts.messages import JsonObject, JsonValue
from opencode.mcp.stdio import McpTransportError, StdioTransport

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

#: The MCP protocol revision this minimal client speaks. Fixed, not
#: negotiated: the server's reply is accepted as-is (the TS SDK's era
#: negotiation is SDK-internal and not ported -- ④ disclosure).
MCP_PROTOCOL_VERSION = "2025-06-18"

#: The client identity announced in the ``initialize`` handshake.
CLIENT_NAME = "opencode-py"
CLIENT_VERSION = "0.0.1"

McpServerStatus = Literal["pending", "connected", "failed", "stopped"]


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


class _ServerEntry:
    """Per-server bookkeeping (the status faces mirror TS index.ts ServerEntry)."""

    __slots__ = ("definition", "error", "status", "tools", "transport")

    def __init__(self, definition: StdioServerDefinition) -> None:
        self.definition = definition
        self.transport: StdioTransport | None = None
        self.tools: tuple[MCPRemoteTool, ...] | None = None
        self.status: McpServerStatus = "pending"
        self.error: Exception | None = None


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
        self._servers: dict[str, _ServerEntry] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False

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

    @property
    def closed(self) -> bool:
        """True once :meth:`close` finished."""
        return self._closed

    # -- internals ---------------------------------------------------------

    def _require_entry(self, name: str) -> _ServerEntry:
        entry = self._servers.get(name)
        if entry is None:
            raise ValueError(f"unknown MCP server {name!r}")
        return entry

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

    async def _connect(self, entry: _ServerEntry) -> StdioTransport:
        """Spawn + initialize + initialized + tools/list (the TS connect chain)."""
        definition = entry.definition
        entry.status = "pending"
        entry.error = None
        entry.tools = None
        transport = StdioTransport(
            command=definition.command,
            args=definition.args,
            cwd=definition.cwd,
            env=definition.env,
            server=definition.name,
            on_unexpected_exit=lambda error: self._mark_lost(definition.name, error),
        )
        try:
            await transport.start()
            initialize_result = await transport.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                timeout_s=self._startup_timeout_s,
            )
            if not isinstance(initialize_result, dict) or not isinstance(initialize_result.get("serverInfo"), dict):
                raise McpTransportError("initialize result has no serverInfo object")
            await transport.notify("notifications/initialized")
            listing = await transport.request("tools/list", {}, timeout_s=self._catalog_timeout_s)
            tools = _parse_tools(listing)
        except (TimeoutError, McpTransportError, ValueError) as error:
            await transport.close()
            entry.status = "failed"
            entry.error = error
            raise McpConnectError(f"MCP server {definition.name!r} failed to start: {error}") from error
        entry.transport = transport
        entry.tools = tools
        entry.status = "connected"
        return transport


def _parse_tools(listing: object) -> tuple[MCPRemoteTool, ...]:
    """Validate a ``tools/list`` result into remote-tool descriptors."""
    if not isinstance(listing, dict) or not isinstance(listing.get("tools"), list):
        raise ValueError("tools/list result has no tools array")
    tools: list[MCPRemoteTool] = []
    for item in listing["tools"]:
        if not isinstance(item, dict) or isinstance(item.get("name"), bool) or not isinstance(item.get("name"), str):
            raise ValueError("tools/list entry has no string name")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"tools/list entry {item['name']!r} description is not a string")
        schema = item.get("inputSchema", {"type": "object"})
        if not isinstance(schema, dict):
            raise ValueError(f"tools/list entry {item['name']!r} inputSchema is not an object")
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
