"""T-C3-006 evaluation harness: MCP events (watch_mcp_event), before/after levels.

A measurement face, not an assertion face (``test_automation_eval.py``
precedent): every probe records a metric and passes, whether or not the
MCP-events capability exists. Running this module on pre-implementation
code captures the BEFORE baseline; the identical inputs on
post-implementation code give the AFTER column. The delta is the ticket's
evidence against doc/GOAL.md's unattended-operation gate (事件驱动按需
唤醒免轮询 -- the token-budget door):

* M1 list face -- a ``watch_mcp_event`` call with no event lists the
  connected servers' event types (unreachable/extension-less -> empty);
* M2 single-event wake -- ``once`` (default) delivers exactly one synthetic
  wake carrying the upstream verbatim guard sentence + fenced data, and
  the stream stops (no second delivery);
* M3 cursor resume -- a stream the server ends cleanly reopens from the
  last cursor (second ``events/stream`` request carries it; event ids stay
  contiguous);
* M4 terminated terminal -- ``notifications/events/terminated`` ends the
  watch without a reopen, and the session gets the "watch stopped" wake;
* M5 injection hardening -- crafted data (raw newlines, a ```-run, a
  >16KiB blob) arrives single-line-escaped, truncated at DATA_LIMIT and
  behind a longer fence;
* M6 tools/list_changed hot refresh -- the notification re-lists the
  catalog (BEFORE: the current transport ignores notifications, catalog
  stays flat -- the honest ``ignored`` reading).

Capability probes are pinned to the banked T-C3-006 seam contract (see
doc/plans/port-232-increments.md): the ``io.modelcontextprotocol/events``
extension literals in the ``opencode.mcp`` package source; tool adapter
module ``opencode.tools.builtin.mcp_events`` exposing
``WatchMcpEventAdapter(manager, sessions)`` registering the
``opencode__watch_mcp_event`` wire name; wake delivery port
``sessions.deliver_event_wake(session_id, *, inbox_id, text, metadata)``
-- the T-C3-005 synthetic inbox seam. A post-implementation
``api-mismatch`` status means the harness and the implementation drifted:
fix the alignment, never the reading.

Timing parameters are injected short per the repo discipline (时序参数必须
可配置, 测试注短值防抖): fixture event delay 0.2s, heartbeat 1.0s, list-
changed notify delay 0.3s, probe deadline 15s.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from opencode.mcp import McpManager, StdioServerDefinition
from opencode.storage import open_database
from opencode.tools import ToolContext
from opencode.tools.registry import ToolRegistry
from tests.integration.test_tool_bash import _FIXED_TIME, _FixedIds, _RecordingSink

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER = Path(__file__).parent / "mcp_stdio_server.py"

_SESSION_ID = "ses_mcpeval"
_SERVER = "fixture"
_WATCH_TOOL = "opencode__watch_mcp_event"
#: upstream verbatim markers (mcp-events.ts L154-156, L173, L176, L147).
_GUARD_MARK = "This isn't from the user"
_STOPPED_MARK = "stopped:"
_TRUNCATION_MARK = "truncated to the first"
#: upstream DATA_LIMIT (mcp-events.ts L13).
_DATA_LIMIT = 16_384
#: injected timing (repo discipline: short values, anti-flake).
_EVENT_DELAY_S = 0.2
_HEARTBEAT_S = 1.0
_PAGES = 2
_DEADLINE_S = 15.0
#: how long M2 waits for a (buggy) second delivery before calling it once.
_ONCE_GRACE_S = 1.5
#: bound on one adapter execute call; a watch face must return immediately.
_EXEC_BOUND_S = 10.0


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _adapter_module() -> Any | None:
    if importlib.util.find_spec("opencode.tools.builtin.mcp_events") is None:
        return None
    return importlib.import_module("opencode.tools.builtin.mcp_events")


def _extension_literals() -> dict[str, bool]:
    """Source-level probe: the events extension wire literals in opencode.mcp."""
    import opencode.mcp as mcp_pkg

    mcp_dir = Path(mcp_pkg.__file__).resolve().parent
    sources = "".join(path.read_text(encoding="utf-8") for path in sorted(mcp_dir.glob("*.py")))
    return {
        "extension_declared": "io.modelcontextprotocol/events" in sources,
        "stream_requested": "events/stream" in sources,
        "notification_routed": "io.modelcontextprotocol/subscriptionId" in sources,
    }


def _migrate(db_path: Path) -> None:
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if migrated.returncode != 0:
        raise RuntimeError(f"alembic failed: {migrated.stderr[-400:]}")


class _RecordingSessions:
    """Fake for the wake-delivery port (structural: the synthetic seam, no LLM)."""

    def __init__(self) -> None:
        self.wakes: list[dict[str, Any]] = []

    async def deliver_event_wake(self, session_id: str, *, inbox_id: str, text: str, metadata: dict[str, Any]) -> None:
        self.wakes.append({"session": session_id, "inbox_id": inbox_id, "text": text, "metadata": dict(metadata)})


def _definition(mode: str, tmp_path: Path, **extra_env: str) -> StdioServerDefinition:
    env = {
        "MCP_FIXTURE_MODE": mode,
        "MCP_FIXTURE_LOG": str(tmp_path / f"{mode}.log.jsonl"),
        "PYTHONUNBUFFERED": "1",
        "MCP_FIXTURE_EVENT_DELAY_S": str(_EVENT_DELAY_S),
        "MCP_FIXTURE_HEARTBEAT_S": str(_HEARTBEAT_S),
        "MCP_FIXTURE_EVENT_PAGES": str(_PAGES),
    }
    env.update(extra_env)
    return StdioServerDefinition(name=_SERVER, command=sys.executable, args=(str(SERVER),), env=env)


def _log_events(tmp_path: Path, mode: str) -> list[dict[str, Any]]:
    log = tmp_path / f"{mode}.log.jsonl"
    if not log.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
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


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id="run_mcpeval",
        message_id="msg_mcpeval",
        tool_call_id="call_mcpeval_1",
        working_dir=str(tmp_path),
    )


def _result_text(result: Any) -> str:
    parts = getattr(result, "content", None) or ()
    return "\n".join(str(getattr(part, "text", "")) for part in parts if getattr(part, "text", ""))


async def _await_wakes(sessions: _RecordingSessions, count: int) -> float:
    """Poll until ``count`` wakes (deadline-bounded); return the wait in s."""
    started = time.monotonic()
    while len(sessions.wakes) < count and time.monotonic() - started < _DEADLINE_S:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    return time.monotonic() - started


def _longest_backtick_run(text: str) -> int:
    """The longest consecutive backtick run (the fence must out-length any inside)."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest


def test_p_capability_probe(tmp_path: Path) -> None:
    """Module + literals + tool-registration probes; BEFORE reports capability-absent."""
    status: str
    tool_names: list[str] = []
    literals: dict[str, bool] = {}
    module = _adapter_module()
    if module is None:
        status = "capability-absent"
    else:
        try:
            literals = _extension_literals()
            db_path = tmp_path / "probe.db"
            _migrate(db_path)

            async def _wired() -> list[str]:
                async with open_database(db_path, mode="create") as database:
                    manager = McpManager()
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, _RecordingSessions())
                    registry = ToolRegistry(
                        sink=cast("Any", _RecordingSink()),
                        clock=lambda: _FIXED_TIME,
                        ids=cast("Any", _FixedIds()),
                        database=database,
                    )
                    registry.register(adapter)
                    catalog = registry.snapshot(_SESSION_ID)
                    return [name for name in (_WATCH_TOOL,) if catalog.resolve(name) is not None]

            tool_names = asyncio.run(_wired())
            status = "probed" if tool_names else "tools-missing"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    if not literals:
        try:
            literals = _extension_literals()
        except Exception:
            literals = {}
    _report(
        {
            "scenario": "P-capability",
            "status": status,
            "module": module is not None,
            "tools_registered": tool_names,
            **literals,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch", "tools-missing"))


def test_m1_list_face_lists_event_types(tmp_path: Path) -> None:
    """No event -> the connected server's event types (both list pages)."""
    status = "capability-absent"
    names: list[str] = []
    module = _adapter_module()
    if module is not None:
        try:

            async def _run() -> list[str]:
                manager = McpManager()
                manager.add_server(_definition("events_ok", tmp_path))
                try:
                    await manager.start_server(_SERVER)
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, _RecordingSessions())
                    result = await asyncio.wait_for(adapter.execute(_ctx(tmp_path), {}), timeout=_EXEC_BOUND_S)
                    text = _result_text(result)
                    structured = getattr(result, "structured_output", None)
                    if isinstance(structured, dict):
                        text += "\n" + json.dumps(structured, ensure_ascii=False, default=str)
                    return [name for name in ("deploys", "drift") if name in text]
                finally:
                    await manager.close()

            names = asyncio.run(_run())
            status = "probed" if names else "empty-list"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report({"scenario": "M1-list-face", "status": status, "event_types": names, "pages": _PAGES})
    assert status.startswith(("capability-absent", "probed", "empty-list", "api-mismatch"))


def test_m2_single_event_wakes_once(tmp_path: Path) -> None:
    """once (default): one wake with the guard sentence + fenced data; no second."""
    status = "capability-absent"
    wakes: int | None = None
    wall_s: float | None = None
    guard = fenced = False
    inbox_id: str | None = None
    metadata: dict[str, Any] = {}
    module = _adapter_module()
    if module is not None:
        try:

            async def _run() -> tuple[int, float, bool, bool, str | None, dict[str, Any]]:
                sessions = _RecordingSessions()
                manager = McpManager()
                manager.add_server(_definition("events_ok", tmp_path))
                try:
                    await manager.start_server(_SERVER)
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, sessions)
                    await asyncio.wait_for(
                        adapter.execute(_ctx(tmp_path), {"server": _SERVER, "event": "deploys"}),
                        timeout=_EXEC_BOUND_S,
                    )
                    waited = await _await_wakes(sessions, 1)
                    await asyncio.sleep(_ONCE_GRACE_S)
                    first = sessions.wakes[0] if sessions.wakes else None
                    return (
                        len(sessions.wakes),
                        waited,
                        bool(first and _GUARD_MARK in first["text"]),
                        bool(first and "```" in first["text"]),
                        first["inbox_id"] if first else None,
                        first["metadata"] if first else {},
                    )
                finally:
                    await manager.close()

            wakes, wall_s, guard, fenced, inbox_id, metadata = asyncio.run(_run())
            status = (
                "delivered-once"
                if wakes == 1 and guard and fenced
                else ("no-delivery" if wakes == 0 else "delivered-multi")
            )
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M2-single-wake",
            "status": status,
            "wakes": wakes,
            "wake_wall_s": None if wall_s is None else round(wall_s, 2),
            "guard_sentence": guard,
            "fenced": fenced,
            "inbox_id": inbox_id,
            "metadata": metadata,
        }
    )
    assert status.startswith(("capability-absent", "delivered-once", "no-delivery", "delivered-multi", "api-mismatch"))


def test_m3_clean_break_reopens_from_cursor(tmp_path: Path) -> None:
    """A stream the server ends cleanly reopens carrying the last cursor."""
    status = "capability-absent"
    streams: int | None = None
    cursor_on_second: str | None = None
    event_ids: list[str] = []
    module = _adapter_module()
    if module is not None:
        try:

            async def _run() -> tuple[int, str | None, list[str]]:
                sessions = _RecordingSessions()
                manager = McpManager()
                manager.add_server(
                    _definition(
                        "events_ok", tmp_path, MCP_FIXTURE_STREAM_MODE="clean_break", MCP_FIXTURE_BREAK_AFTER="1"
                    )
                )
                try:
                    await manager.start_server(_SERVER)
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, sessions)
                    await asyncio.wait_for(
                        adapter.execute(_ctx(tmp_path), {"server": _SERVER, "event": "deploys", "once": False}),
                        timeout=_EXEC_BOUND_S,
                    )
                    await _await_wakes(sessions, 2)
                finally:
                    await manager.close()
                log = _log_events(tmp_path, "events_ok")
                requests = [item for item in log if item.get("event") == "stream_request"]
                second = requests[1] if len(requests) > 1 else None
                cursor = None
                if second is not None:
                    params = second.get("params")
                    value = params.get("cursor") if isinstance(params, dict) else None
                    cursor = value if isinstance(value, str) else None
                ids = [
                    str(item.get("eventId"))
                    for item in log
                    if item.get("event") == "stream_event" and item.get("eventId")
                ]
                return len(requests), cursor, ids

            streams, cursor_on_second, event_ids = asyncio.run(_run())
            if streams is not None and streams >= 2 and cursor_on_second == "c1":
                status = "reopened-with-cursor" if {"evt-1", "evt-2"} <= set(event_ids) else "reopen-gap"
            elif streams == 1:
                status = "single-stream"
            else:
                status = "no-reopen"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M3-cursor-resume",
            "status": status,
            "stream_requests": streams,
            "cursor_on_second": cursor_on_second,
            "event_ids": event_ids,
        }
    )
    assert status.startswith(
        (
            "capability-absent",
            "reopened-with-cursor",
            "reopen-gap",
            "single-stream",
            "no-reopen",
            "api-mismatch",
        )
    )


def test_m4_terminated_ends_watch(tmp_path: Path) -> None:
    """terminated: no reopen, and the session is told the watch stopped."""
    status = "capability-absent"
    streams: int | None = None
    wakes: int | None = None
    stopped_notice = False
    module = _adapter_module()
    if module is not None:
        try:

            async def _run() -> tuple[int, int, bool]:
                sessions = _RecordingSessions()
                manager = McpManager()
                manager.add_server(
                    _definition("events_ok", tmp_path, MCP_FIXTURE_STREAM_MODE="terminate", MCP_FIXTURE_BREAK_AFTER="1")
                )
                try:
                    await manager.start_server(_SERVER)
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, sessions)
                    await asyncio.wait_for(
                        adapter.execute(_ctx(tmp_path), {"server": _SERVER, "event": "deploys", "once": False}),
                        timeout=_EXEC_BOUND_S,
                    )
                    # One event wake + the stopped notice, or the deadline.
                    deadline = time.monotonic() + _DEADLINE_S
                    while len(sessions.wakes) < 2 and time.monotonic() < deadline:  # noqa: ASYNC110
                        await asyncio.sleep(0.05)
                finally:
                    await manager.close()
                log = _log_events(tmp_path, "events_ok")
                streams = len([item for item in log if item.get("event") == "stream_request"])
                stopped = any(_STOPPED_MARK in wake["text"] for wake in sessions.wakes)
                return streams, len(sessions.wakes), stopped

            streams, wakes, stopped_notice = asyncio.run(_run())
            if streams == 1 and stopped_notice and (wakes or 0) >= 2:
                status = "terminated-clean"
            elif streams is not None and streams > 1:
                status = "reopened-after-terminal"
            elif wakes == 0:
                status = "no-delivery"
            else:
                status = "no-stopped-notice"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M4-terminated",
            "status": status,
            "stream_requests": streams,
            "wakes": wakes,
            "stopped_notice": stopped_notice,
        }
    )
    assert status.startswith(
        (
            "capability-absent",
            "terminated-clean",
            "reopened-after-terminal",
            "no-delivery",
            "no-stopped-notice",
            "api-mismatch",
        )
    )


def test_m5_wake_text_hardened(tmp_path: Path) -> None:
    """Crafted data arrives escaped, truncated at DATA_LIMIT, behind a longer fence."""
    status = "capability-absent"
    payload_escaped = truncated = fence_safe = False
    longest_run: int | None = None
    module = _adapter_module()
    if module is not None:
        try:
            crafted = {"msg": "line1\nline2\n```fence", "blob": "A" * (_DATA_LIMIT + 4_096)}

            async def _run() -> tuple[bool, bool, bool, int]:
                sessions = _RecordingSessions()
                manager = McpManager()
                manager.add_server(_definition("events_ok", tmp_path, MCP_FIXTURE_EVENT_DATA=json.dumps(crafted)))
                try:
                    await manager.start_server(_SERVER)
                    adapter = cast("Any", module).WatchMcpEventAdapter(manager, sessions)
                    await asyncio.wait_for(
                        adapter.execute(_ctx(tmp_path), {"server": _SERVER, "event": "deploys"}),
                        timeout=_EXEC_BOUND_S,
                    )
                    await _await_wakes(sessions, 1)
                finally:
                    await manager.close()
                if not sessions.wakes:
                    return False, False, False, 0
                text = str(sessions.wakes[0]["text"])
                escaped = "line1\\nline2" in text  # JSON-escaped, not a raw newline
                cut = _TRUNCATION_MARK in text and "A" * _DATA_LIMIT not in text
                longest = _longest_backtick_run(text)
                # The data carries a 3-backtick run; the fence must be longer.
                safe = longest > 3 and "```" in text
                return escaped, cut, safe, longest

            payload_escaped, truncated, fence_safe, longest_run = asyncio.run(_run())
            if payload_escaped and truncated and fence_safe:
                status = "sanitized"
            elif longest_run == 0:
                status = "no-delivery"
            else:
                status = "unsanitized"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M5-injection-hardening",
            "status": status,
            "payload_escaped": payload_escaped,
            "truncated_at_limit": truncated,
            "fence_safe": fence_safe,
            "longest_fence_run": longest_run,
            "data_limit": _DATA_LIMIT,
        }
    )
    assert status.startswith(("capability-absent", "sanitized", "unsanitized", "no-delivery", "api-mismatch"))


def test_m6_tools_list_changed_hot_refresh(tmp_path: Path) -> None:
    """notifications/tools/list_changed re-lists the catalog (BEFORE: ignored)."""
    status = "api-mismatch"
    before: list[str] = []
    after: list[str] = []
    notified = False
    try:

        async def _run() -> tuple[list[str], list[str], bool]:
            manager = McpManager()
            manager.add_server(_definition("list_changed", tmp_path))
            try:
                tools = await manager.start_server(_SERVER)
                names_before = sorted(tool.name for tool in tools)
                deadline = time.monotonic() + _DEADLINE_S
                names_after = names_before
                sent = False
                while time.monotonic() < deadline:
                    log = _log_events(tmp_path, "list_changed")
                    sent = sent or any(item.get("event") == "list_changed_sent" for item in log)
                    listed = manager.tools(_SERVER)
                    if listed is not None:
                        names_after = sorted(tool.name for tool in listed)
                    if sent and "late_tool" in names_after:
                        break
                    await asyncio.sleep(0.05)
                return names_before, names_after, sent
            finally:
                await manager.close()

        before, after, notified = asyncio.run(_run())
        if notified and "late_tool" in after:
            status = "refreshed"
        elif notified:
            status = "ignored"
        else:
            status = "notification-absent"
    except Exception as error:
        status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M6-list-changed",
            "status": status,
            "tools_before": before,
            "tools_after": after,
            "notification_sent": notified,
        }
    )
    assert status.startswith(("refreshed", "ignored", "notification-absent", "api-mismatch"))
