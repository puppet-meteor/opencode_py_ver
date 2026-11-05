"""MCP -> ToolAdapter bridge (R4-S2; CORE-CONTRACTS §5.1 L253, r4-slices §2.1).

Maps an MCP ``CallToolResult`` (TS client.ts L96-100
``{isError, structured, content}``) onto the registry's
:class:`~opencode.contracts.tools.ToolResult` ``content`` /
``structured_output`` / ``error`` faces. The namespace mapping (MCP
server id participating in the wire name, §5.1 L255) is deliberately
NOT here -- that is the S3 face (r4-slices §2.2).
"""

from __future__ import annotations

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.mcp.manager import (
    MCPCallToolResult,
    McpManager,
    MCPMediaContent,
    MCPRemoteTool,
    MCPTextContent,
)
from opencode.tools.invoker import ToolContext


def to_tool_result(call: MCPCallToolResult, *, server: str) -> ToolResult:
    """Fill ``ToolResult.content``/``structured_output`` from an MCP call result (§5.1 L253).

    - text content maps 1:1 onto ``ToolTextContent``;
    - media content (``{type: "media", data, mimeType}``) maps onto
      ``ToolFileContent`` with a self-contained ``data:`` URI -- the payload
      is preserved and the media gate (the URI is not read or sent to the
      model, §2.1 L72) is a runtime rule either way;
    - ``structuredContent`` becomes ``structured_output``;
    - ``isError`` becomes a ``ToolFailed`` :class:`CoreError` while the
      content (which usually carries the error text for the model) passes
      through unchanged.
    """
    parts: list[ToolTextContent | ToolFileContent] = []
    for item in call.content:
        if isinstance(item, MCPTextContent):
            parts.append(ToolTextContent(type="text", text=item.text))
        elif isinstance(item, MCPMediaContent):
            parts.append(
                ToolFileContent(
                    type="file",
                    uri=f"data:{item.mime_type};base64,{item.data}",
                    mime=item.mime_type,
                )
            )
    error: CoreError | None = None
    if call.is_error:
        text = " ".join(part.text for part in parts if isinstance(part, ToolTextContent)).strip()
        error = CoreError(
            code=CoreErrorCode.TOOL_FAILED,
            message=text or f"MCP server {server} reported a tool error",
            retryable=False,
            details={"mcp_server": server},
        )
    return ToolResult(
        content=parts,
        structured_output=call.structured,
        metadata={"mcp_server": server},
        error=error,
    )


class MCPToolAdapter:
    """A :class:`~opencode.tools.registry.ToolAdapter` over one MCP tool.

    ``spec`` is injected by the caller: the namespace/name policy (the
    MCP server id must participate in the wire name, §5.1 L255) is the
    S3 face, so this class takes the finished :class:`ToolSpec` rather
    than inventing a name. ``execute`` routes through the manager (the
    execution timeout and the unknown-on-disconnect semantics live
    there); a definitive server answer maps through :func:`to_tool_result`,
    and manager errors propagate -- the Invoker's post-start rule already
    folds them into ``failed/unknown`` with ``record_unknown``
    (invoker.py L1094-1120), which is exactly the §6 L304 face.
    """

    def __init__(self, *, manager: McpManager, server: str, tool: MCPRemoteTool, spec: ToolSpec) -> None:
        self._manager = manager
        self._server = server
        self._tool = tool
        self._spec = spec

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        call = await self._manager.call_tool(self._server, self._tool.name, input)
        return to_tool_result(call, server=self._server)
