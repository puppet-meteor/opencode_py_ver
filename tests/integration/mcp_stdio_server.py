"""Minimal MCP stdio fixture server (R4-S2; PHASE1-SLIM §4 L105).

Run as a REAL subprocess by ``tests/integration/test_mcp_stdio.py``
(``<python> mcp_stdio_server.py``): the "测试自有的最小 MCP server" +
"MCP SDK 集成测在进程边界运行" requirement -- an actual process boundary
with real pipes and real process trees, never an in-process stub. It
speaks just enough newline-delimited JSON-RPC 2.0 for the
transport/manager contract tests.

Behavior is selected with ``MCP_FIXTURE_MODE``:

- ``ok`` (default): initialize -> tools/list (echo/structured/fail) ->
  tools/call; every call attempt is appended to ``$MCP_FIXTURE_LOG``.
- ``startup_hang``: read initialize, never answer (startup-timeout leg).
- ``catalog_hang``: answer initialize, hang on tools/list.
- ``tools_list_error``: answer initialize, reply a JSON-RPC error to tools/list.
- ``big_frame``: answer initialize/tools/list, emit one oversized frame
  on tools/call (``MCP_FIXTURE_FRAME_BYTES``, default 4096).
- ``exit_during_call``: write a stderr line and exit(3) on tools/call
  without answering (unknown-outcome + stderr-tail leg).
- ``crash_once``: crash on the first tools/call ever seen in the log,
  echo normally afterwards -- the side-effect counter (PHASE1-SLIM L105
  "用计数器证明副作用次数") that pins no-auto-retry + rebuild-on-next-call.
- ``ignore_eof_with_child``: spawn a child after initialize and ignore
  the stdin EOF (close-escalation tree-kill leg); the child pid is logged.
- ``crash_with_child``: spawn a child after initialize, then exit(3) on
  the first tools/call (crash-path tree-sweep leg).

Every lifecycle fact is appended to ``$MCP_FIXTURE_LOG`` as one JSON
object per line: ``{"event": "start"|"child"|"call"|"eof"|"eof_ignored",
...}`` -- the tests read it for pids, call counts and crash evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

MODE = os.environ.get("MCP_FIXTURE_MODE", "ok")
LOG_PATH = os.environ.get("MCP_FIXTURE_LOG", "")
FRAME_BYTES = int(os.environ.get("MCP_FIXTURE_FRAME_BYTES", "4096"))

_TOOLS = (
    {
        "name": "echo",
        "description": "Echo the text argument back as a text result.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "structured",
        "description": "Answer with a structuredContent payload.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fail",
        "description": "Answer isError=true with an error text.",
        "inputSchema": {"type": "object", "properties": {}},
    },
)


def _log(event: dict[str, Any]) -> None:
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
        handle.flush()


def _logged_events() -> list[dict[str, Any]]:
    if not LOG_PATH or not os.path.isfile(LOG_PATH):
        return []
    events: list[dict[str, Any]] = []
    with open(LOG_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
    return events


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _spawn_child() -> None:
    """A descendant the tree-kill tests can observe outliving the server."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _log({"event": "child", "pid": child.pid})


def _answer_initialize(request_id: Any) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "0.0.1"},
            },
        }
    )
    if MODE in ("ignore_eof_with_child", "crash_with_child"):
        _spawn_child()


def _answer_tools_list(request_id: Any) -> None:
    if MODE == "catalog_hang":
        return  # hang: the manager's catalog timeout must fire
    if MODE == "tools_list_error":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fixture catalog failure"},
            }
        )
        return
    _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [dict(tool) for tool in _TOOLS]}})


def _answer_tools_call(request_id: Any, params: dict[str, Any]) -> None:
    if MODE == "big_frame":
        sys.stdout.write("x" * FRAME_BYTES + "\n")
        sys.stdout.flush()
        return
    if MODE == "exit_during_call" or MODE == "crash_with_child":
        sys.stderr.write("fixture: boom during call\n")
        sys.stderr.flush()
        sys.exit(3)
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if MODE == "crash_once" and not any(event.get("event") == "call" for event in _logged_events()):
        _log({"event": "call", "tool": name})
        sys.stderr.write("fixture: first call ever\n")
        sys.stderr.flush()
        sys.exit(3)
    _log({"event": "call", "tool": name})
    if name == "echo":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}],
                    "isError": False,
                },
            }
        )
    elif name == "structured":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "structured ok"}],
                    "isError": False,
                    "structuredContent": {"answer": 42, "nested": {"ok": True}},
                },
            }
        )
    elif name == "fail":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "intentional fixture failure"}],
                    "isError": True,
                },
            }
        )
    else:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown tool {name!r}"},
            }
        )


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    if not isinstance(method, str):
        return  # a response or a bare object: nothing to answer
    request_id = message.get("id")
    if method == "initialize":
        if MODE == "startup_hang":
            _log({"event": "initialize_received"})
            return  # hang: the manager's startup timeout must fire
        _answer_initialize(request_id)
        return
    if method == "notifications/initialized":
        return  # notification: no response by definition
    if method == "tools/list":
        _answer_tools_list(request_id)
        return
    if method == "tools/call":
        params = message.get("params")
        _answer_tools_call(request_id, params if isinstance(params, dict) else {})
        return
    if request_id is not None:
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main() -> int:
    _log({"event": "start", "pid": os.getpid(), "mode": MODE})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            _handle(message)
    if MODE == "ignore_eof_with_child":
        # Ignore the stdin EOF: only the close escalation may end this
        # process (the CLOSE_GRACE -> terminate-tree leg).
        _log({"event": "eof_ignored"})
        time.sleep(120)
    _log({"event": "eof"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
