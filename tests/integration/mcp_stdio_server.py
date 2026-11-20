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
- ``events_ok`` (T-C3-006): declare the ``io.modelcontextprotocol/events``
  extension, answer ``events/list`` (paginated when
  ``MCP_FIXTURE_EVENT_PAGES`` > 1) and hold ``events/stream`` open on a
  background thread, pushing ``notifications/events/active|event|heartbeat``
  (``_meta`` subscriptionId = the stream request's JSON-RPC id).
  ``MCP_FIXTURE_STREAM_MODE`` selects ``open`` (default), ``clean_break``
  (resolve the request after ``MCP_FIXTURE_BREAK_AFTER`` events -- the
  client must reopen from the last cursor), ``terminate`` (send
  ``notifications/events/terminated`` first -- terminal, no reopen) or
  ``reject`` (JSON-RPC -32601 -- the rejected face). Event cadence
  ``MCP_FIXTURE_EVENT_DELAY_S``, heartbeat ``MCP_FIXTURE_HEARTBEAT_S``,
  data payload ``MCP_FIXTURE_EVENT_DATA`` (JSON). A
  ``notifications/cancelled`` for the request id stops the thread.
- ``list_changed`` (T-C3-006): after ``notifications/initialized`` send
  ``notifications/tools/list_changed`` (``MCP_FIXTURE_NOTIFY_DELAY_S``
  later); from the second ``tools/list`` on, an extra ``late_tool`` appears
  (unless ``MCP_FIXTURE_LATE_TOOL=0``) -- the hot-refresh face.

Every lifecycle fact is appended to ``$MCP_FIXTURE_LOG`` as one JSON
object per line: ``{"event": "start"|"child"|"call"|"eof"|"eof_ignored"|
"events_list"|"stream_request"|"stream_active"|"stream_event"|
"stream_end"|"stream_terminated"|"stream_cancelled"|"tools_list"|
"list_changed_sent", ...}`` -- the tests read it for pids, call counts,
cursors and crash evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

MODE = os.environ.get("MCP_FIXTURE_MODE", "ok")
LOG_PATH = os.environ.get("MCP_FIXTURE_LOG", "")
FRAME_BYTES = int(os.environ.get("MCP_FIXTURE_FRAME_BYTES", "4096"))

#: T-C3-006 events face (SEP-3415 draft literals, upstream client.ts L72-73).
EVENTS_EXT = "io.modelcontextprotocol/events"
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

#: Stream cadence (s) -- the eval harness injects these short (repo discipline).
EVENT_DELAY_S = float(os.environ.get("MCP_FIXTURE_EVENT_DELAY_S", "0.2"))
HEARTBEAT_S = float(os.environ.get("MCP_FIXTURE_HEARTBEAT_S", "1.0"))
STREAM_MODE = os.environ.get("MCP_FIXTURE_STREAM_MODE", "open")
BREAK_AFTER = int(os.environ.get("MCP_FIXTURE_BREAK_AFTER", "0"))
EVENT_PAGES = int(os.environ.get("MCP_FIXTURE_EVENT_PAGES", "1"))
NOTIFY_DELAY_S = float(os.environ.get("MCP_FIXTURE_NOTIFY_DELAY_S", "0.3"))

_SEND_LOCK = threading.Lock()
_STREAM_STOP: dict[Any, threading.Event] = {}
_TOOLS_LIST_CALLS = 0
#: Event ids and cursors are GLOBAL and monotonic: a reopened stream (clean
#: break) continues the sequence, so cursor-resume tests see contiguous ids.
_EVENT_SEQ = 0
_EVENT_LOCK = threading.Lock()


def _next_seq() -> int:
    global _EVENT_SEQ
    with _EVENT_LOCK:
        _EVENT_SEQ += 1
        return _EVENT_SEQ


def _last_cursor() -> str | None:
    with _EVENT_LOCK:
        return f"c{_EVENT_SEQ}" if _EVENT_SEQ else None


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
    with _SEND_LOCK:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _notify(method: str, params: dict[str, Any]) -> None:
    """One server notification (no id; thread-safe through _send's lock)."""
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def _spawn_child() -> None:
    """A descendant the tree-kill tests can observe outliving the server."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _log({"event": "child", "pid": child.pid})


def _answer_initialize(request_id: Any, params: dict[str, Any] | None = None) -> None:
    if MODE == "events_ok":
        declared = params.get("protocolVersion") if isinstance(params, dict) else None
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": declared if isinstance(declared, str) else "2026-07-28",
                    "capabilities": {"tools": {}, "extensions": {EVENTS_EXT: {}}},
                    "serverInfo": {"name": "fixture-events", "version": "0.0.1"},
                },
            }
        )
        return
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


def _answer_events_list(request_id: Any, params: dict[str, Any]) -> None:
    cursor = params.get("cursor")
    page_one = [{"name": "deploys", "description": "A deploy finished.", "delivery": ["push"]}]
    page_two = [{"name": "drift", "description": "Second-page event type.", "delivery": ["poll"]}]
    if EVENT_PAGES > 1 and cursor is None:
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"events": page_one, "nextCursor": "page2"}})
    else:
        events = page_one + page_two if cursor == "page2" else page_one
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"events": events}})
    _log({"event": "events_list", "cursor": cursor})


def _run_stream(request_id: Any, name: str) -> None:
    """Hold one events/stream open: active, then events/heartbeats on cadence.

    clean_break resolves the request (the client must reopen from the last
    cursor); terminate announces the terminal notification first. A
    notifications/cancelled sets the stop event and the thread exits WITHOUT
    a final response (the client already gave up on it).
    """
    meta = {SUBSCRIPTION_ID: request_id}
    try:
        data = json.loads(os.environ.get("MCP_FIXTURE_EVENT_DATA", '{"ok": true}'))
    except ValueError:
        data = {"ok": True}
    _notify("notifications/events/active", {"cursor": None, "_meta": meta})
    _log({"event": "stream_active", "id": request_id})
    stop = _STREAM_STOP.get(request_id)
    sent = 0
    next_event = time.monotonic() + EVENT_DELAY_S
    next_beat = time.monotonic() + HEARTBEAT_S
    while stop is not None and not stop.is_set():
        now = time.monotonic()
        if now >= next_event:
            sent += 1
            seq = _next_seq()
            cursor = f"c{seq}"
            _notify(
                "notifications/events/event",
                {
                    "eventId": f"evt-{seq}",
                    "name": name,
                    "timestamp": "2026-11-20T00:00:00Z",
                    "data": data,
                    "cursor": cursor,
                    "_meta": meta,
                },
            )
            _log({"event": "stream_event", "id": request_id, "eventId": f"evt-{seq}", "cursor": cursor})
            next_event = time.monotonic() + EVENT_DELAY_S
            if BREAK_AFTER and sent >= BREAK_AFTER:
                if STREAM_MODE == "clean_break":
                    _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                    _log({"event": "stream_end", "id": request_id, "reason": "clean"})
                    break
                if STREAM_MODE == "terminate":
                    _notify(
                        "notifications/events/terminated",
                        {"error": {"code": -32001, "message": "subscription revoked"}, "_meta": meta},
                    )
                    _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
                    _log({"event": "stream_terminated", "id": request_id})
                    break
        elif now >= next_beat:
            _notify("notifications/events/heartbeat", {"cursor": _last_cursor(), "_meta": meta})
            next_beat = time.monotonic() + HEARTBEAT_S
        else:
            time.sleep(min(0.05, max(0.0, min(next_event, next_beat) - now)))


def _answer_events_stream(request_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "missing event name"}})
        return
    if STREAM_MODE == "reject":
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "events/stream not supported"}})
        return
    _log({"event": "stream_request", "id": request_id, "params": params})
    stop = threading.Event()
    _STREAM_STOP[request_id] = stop
    threading.Thread(target=_run_stream, args=(request_id, name), daemon=True).start()


def _notify_list_changed_later() -> None:
    """list_changed mode: push notifications/tools/list_changed after a delay."""

    def _fire() -> None:
        time.sleep(NOTIFY_DELAY_S)
        _notify("notifications/tools/list_changed", {})
        _log({"event": "list_changed_sent"})

    threading.Thread(target=_fire, daemon=True).start()


def _answer_tools_list(request_id: Any) -> None:
    global _TOOLS_LIST_CALLS
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
    tools = [dict(tool) for tool in _TOOLS]
    if MODE == "list_changed":
        _TOOLS_LIST_CALLS += 1
        if _TOOLS_LIST_CALLS > 1 and os.environ.get("MCP_FIXTURE_LATE_TOOL", "1") == "1":
            tools.append(
                {
                    "name": "late_tool",
                    "description": "Appears from the second tools/list on.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            )
        _log({"event": "tools_list", "call": _TOOLS_LIST_CALLS, "count": len(tools)})
    _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})


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
        params = message.get("params")
        _answer_initialize(request_id, params if isinstance(params, dict) else None)
        return
    if method == "notifications/initialized":
        if MODE == "list_changed":
            _notify_list_changed_later()
        return  # notification: no response by definition
    if method == "notifications/cancelled":
        params = message.get("params")
        rid = params.get("id") if isinstance(params, dict) else None
        stop = _STREAM_STOP.get(rid)
        if stop is not None:
            stop.set()
            _log({"event": "stream_cancelled", "id": rid})
        return
    if method == "tools/list":
        _answer_tools_list(request_id)
        return
    if method == "events/list":
        params = message.get("params")
        _answer_events_list(request_id, params if isinstance(params, dict) else {})
        return
    if method == "events/stream":
        params = message.get("params")
        _answer_events_stream(request_id, params if isinstance(params, dict) else {})
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
