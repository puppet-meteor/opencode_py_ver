"""The MCP client stack (R4-S2): stdio transport, server manager, tool bridge.

A minimal self-built JSON-RPC stdio client per the OP-R4-1 user ruling
(2026-11-05): zero new dependencies. The namespace mapping, catalog
binding and policy wiring are the S3 face (r4-slices §2.2) and are
deliberately absent from this package.
"""

from opencode.mcp.bridge import MCPToolAdapter, to_tool_result
from opencode.mcp.manager import (
    DEFAULT_CATALOG_TIMEOUT_S,
    DEFAULT_EXECUTION_TIMEOUT_S,
    DEFAULT_STARTUP_TIMEOUT_S,
    MCP_PROTOCOL_VERSION,
    MCPCallToolResult,
    McpConnectError,
    MCPContent,
    McpManager,
    MCPMediaContent,
    MCPRemoteTool,
    MCPTextContent,
    McpUnknownOutcomeError,
    StdioServerDefinition,
)
from opencode.mcp.stdio import (
    CLOSE_GRACE_S,
    EXIT_WAIT_S,
    FORCE_KILL_AFTER_S,
    MAX_FRAME_BYTES,
    STDERR_TAIL_CHARS,
    McpConnectionLostError,
    McpFrameTooLargeError,
    McpNotConnectedError,
    McpProtocolError,
    McpRemoteError,
    McpTransportError,
    StdioTransport,
)

__all__ = [
    "CLOSE_GRACE_S",
    "DEFAULT_CATALOG_TIMEOUT_S",
    "DEFAULT_EXECUTION_TIMEOUT_S",
    "DEFAULT_STARTUP_TIMEOUT_S",
    "EXIT_WAIT_S",
    "FORCE_KILL_AFTER_S",
    "MAX_FRAME_BYTES",
    "MCP_PROTOCOL_VERSION",
    "STDERR_TAIL_CHARS",
    "MCPCallToolResult",
    "MCPContent",
    "MCPMediaContent",
    "MCPRemoteTool",
    "MCPTextContent",
    "MCPToolAdapter",
    "McpConnectError",
    "McpConnectionLostError",
    "McpFrameTooLargeError",
    "McpManager",
    "McpNotConnectedError",
    "McpProtocolError",
    "McpRemoteError",
    "McpTransportError",
    "McpUnknownOutcomeError",
    "StdioServerDefinition",
    "StdioTransport",
    "to_tool_result",
]
