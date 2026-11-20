"""T-C3-004 contract set: automation EVENT triggers.

The upstream ``packages/core/test/automation-event.test.ts`` (724 lines,
12+4 parameterized families) distilled into the py repo's contract face:
RED against the T-C3-003 service (the event faces are absent), GREEN
after the T-C3-004 implementation. The MCP source is the shared
``mcp_stdio_server.py`` fixture as a real subprocess driven through the
T-C3-006 ``McpManager``; the dedupe/heartbeat family uses a scripted
manager subclass (the two faces the watch machine touches:
``list_events`` + ``stream_events``).

Pinned wording (upstream-verbatim or banked deviations, see
doc/plans/port-232-increments.md T-C3-004):

- ``event_prompt`` format: ``{text}\\n\\n{guard}:\\n    {compact JSON}``
  with ``{"id","timestamp","data"}`` key order, JSON.stringify-style
  compact separators and U+2028/U+2029 always escaped, never raw.
- ``Event prompt exceeds 32 KiB`` (upstream runAction cap).
- ``Invalid MCP event`` (per-message fail face: name/size/schema).
- ``...recreate...`` on a truncated ``active`` (replay-impossible face).
- park rows: ``status='paused'`` + ``last_outcome='failed'`` with the
  reason in ``last_output`` (the recorded py deviation for an upstream
  stream left for reconcile).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from opencode.automation.service import (
    AutomationNotFoundError,
    AutomationService,
    InvalidAutomationError,
    event_prompt,
)
from opencode.mcp import McpManager
from opencode.storage import open_database
from tests.integration.test_automation_event_eval import (
    _GUARD,
    _PROJECT,
    _REOPEN_DELAYS_S,
    _SESSION_ID,
    _STREAM_IDLE_S,
    _definition,
    _log_events,
    _mcp_manager_module,
    _migrate,
    _RecordingSessions,
    _row,
    _stream_requests,
    _wait_for,
)

_Invalid = InvalidAutomationError
_NotFound = AutomationNotFoundError

#: The ``deploys`` event type exactly as the fixture lists it (resolve face).
_DEPLOYS = {
    "name": "deploys",
    "description": "A deploy finished.",
    "delivery": ["push"],
    "inputSchema": {
        "type": "object",
        "properties": {"build": {"type": "integer"}},
        "required": ["build"],
    },
    "payloadSchema": {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    },
}


def _trigger(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "event",
        "server": "fixture",
        "event": "deploys",
        "args": {"build": 7},
    }
    base.update(overrides)
    return base


def _action(prompt: str = "Deploy finished: react.") -> dict[str, Any]:
    return {"type": "prompt", "prompt": prompt, "to": {"session": _SESSION_ID}}


def _manager(tmp_path: Path, **extra_env: str) -> McpManager:
    mcp = _mcp_manager_module()
    if mcp is None:
        raise RuntimeError("opencode.mcp is not importable")
    manager = mcp.McpManager()
    manager.add_server(_definition("fixture", "events_ok", tmp_path, **extra_env))
    return manager


class _AlwaysPendingSessions(_RecordingSessions):
    """The coalesce face always reports the previous prompt pending."""

    def prompt_pending(self, session_id: str, message_id: str) -> bool:
        return True


class _ScriptedManager(McpManager):
    """Overrides exactly the two faces the watch machine touches."""

    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self._scripts = list(scripts)
        self.stream_calls: list[dict[str, Any]] = []

    async def start_server(self, name: str) -> tuple[Any, ...]:
        return ()

    async def list_events(self, server: str) -> tuple[dict[str, Any], ...]:
        return (_DEPLOYS,)

    async def stream_events(
        self,
        server: str,
        event_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        cursor: str | None = None,
        idle_timeout_s: float = 1.0,
    ) -> Any:
        self.stream_calls.append(
            {"server": server, "name": event_name, "arguments": dict(arguments or {}), "cursor": cursor}
        )
        script = self._scripts.pop(0) if self._scripts else []
        for message in script:
            yield message
        await asyncio.Event().wait()  # hold quiet; teardown cancels


async def _make_service(database: Any, sessions: Any, manager: McpManager) -> Any:
    return AutomationService(
        database=database,
        sessions=sessions,
        mcp=manager,
        reopen_delays_s=_REOPEN_DELAYS_S,
        stream_idle_s=_STREAM_IDLE_S,
    )


async def _scenario(
    tmp_path: Path,
    name: str,
    manager: McpManager,
    body: Any,
    *,
    trigger: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    sessions: Any = None,
    scheduler: bool = True,
) -> Any:
    """Migrate, wire the service, create one event automation, run ``body``."""
    db_path = tmp_path / f"{name}.db"
    _migrate(db_path)
    if sessions is None:
        sessions = _RecordingSessions()
    scheduler_mod = importlib.import_module("opencode.automation.scheduler")
    service: Any = None
    try:
        async with open_database(db_path, mode="create") as database:
            service = await _make_service(database, sessions, manager)
            if not isinstance(manager, _ScriptedManager):
                await manager.start_server("fixture")
            created = await service.create(
                {
                    "name": f"ct-{name}",
                    "project_id": _PROJECT,
                    "directory": str(tmp_path),
                    "session_id": _SESSION_ID,
                    "trigger": trigger if trigger is not None else _trigger(),
                    "action": action if action is not None else _action(),
                }
            )
            if not scheduler:
                return await body(service, sessions, created)
            async with scheduler_mod.AutomationScheduler(service=service, max_sleep_s=0.05, leader_ttl_s=1.0):
                return await body(service, sessions, created)
    finally:
        if service is not None:
            await service.stop_watches()
        await manager.close()


def _position(tmp_path: Path, name: str, automation_id: str) -> dict[str, Any] | None:
    raw = _row(tmp_path / f"{name}.db", automation_id).get("event_position")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------------------
# the interpolator (decision 8: an independent pure function)
# ---------------------------------------------------------------------------


def test_event_prompt_is_the_verbatim_interpolator() -> None:
    message = {
        "type": "event",
        "eventId": "evt-9",
        "name": "deploys",
        "timestamp": "2026-11-21T00:00:00Z",
        "data": {"status": "finished"},
        "cursor": "c9",
    }
    text = event_prompt("Deploy done.", message)
    assert text == (
        "Deploy done.\n\n"
        + _GUARD
        + ':\n    {"id":"evt-9","timestamp":"2026-11-21T00:00:00Z","data":{"status":"finished"}}'
    )
    evil = event_prompt("R.", {**message, "data": {"a": "x\u2028y\u2029z"}})
    assert "\\u2028" in evil and "\\u2029" in evil
    assert "\u2028" not in evil and "\u2029" not in evil
    ctrl = event_prompt("R.", {**message, "data": {"a": "b\nc"}})
    last_line = ctrl.splitlines()[-1]
    assert "b\\nc" in last_line and "\n" not in last_line


# ---------------------------------------------------------------------------
# create validation (decision 1: the fourth trigger kind + resolve at create)
# ---------------------------------------------------------------------------


def test_create_event_trigger_validation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db_path = tmp_path / "create.db"

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        assert created["status"] == "active"
        assert created["next_due_at"] is None
        row = _row(db_path, created["id"])
        assert json.loads(row["trigger"])["type"] == "event"
        assert row["event_position"] is None
        assert row["next_due_at"] is None
        await service.remove(created["id"])

    async def main() -> None:
        db = db_path
        _migrate(db)
        service = None
        try:
            async with open_database(db, mode="create") as database:
                service = await _make_service(database, _RecordingSessions(), manager)
                await manager.start_server("fixture")
                base = {
                    "name": "ct-create",
                    "project_id": _PROJECT,
                    "directory": str(tmp_path),
                    "session_id": _SESSION_ID,
                }
                with pytest.raises(_Invalid, match="push"):
                    await service.create({**base, "trigger": _trigger(event="drift"), "action": _action()})
                with pytest.raises(_Invalid, match="args"):
                    await service.create({**base, "trigger": _trigger(args={"build": "seven"}), "action": _action()})
                with pytest.raises(_Invalid, match="args"):
                    await service.create({**base, "trigger": _trigger(args={}), "action": _action()})
                with pytest.raises(_Invalid, match="nope"):
                    await service.create({**base, "trigger": _trigger(server="nope"), "action": _action()})
                await body(service, None, await service.create({**base, "trigger": _trigger(), "action": _action()}))
        finally:
            if service is not None:
                await service.stop_watches()
            await manager.close()

    asyncio.run(main())


def test_create_event_trigger_requires_the_mcp_port(tmp_path: Path) -> None:
    db_path = tmp_path / "nomcp.db"

    async def main() -> None:
        _migrate(db_path)
        async with open_database(db_path, mode="create") as database:
            service = AutomationService(database=database, sessions=_RecordingSessions())
            with pytest.raises(_Invalid, match="mcp"):
                await service.create(
                    {
                        "name": "ct-nomcp",
                        "project_id": _PROJECT,
                        "directory": str(tmp_path),
                        "session_id": _SESSION_ID,
                        "trigger": _trigger(),
                        "action": _action(),
                    }
                )

    asyncio.run(main())


# ---------------------------------------------------------------------------
# the watch machine (delivery, dedupe, reopen, park)
# ---------------------------------------------------------------------------


def test_first_event_delivers_interpolated_prompt_zero_polling(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        # Same coherent-snapshot discipline as the clean-break test: the
        # bare ``sends >= 1`` wait can resume between a send and its settle.
        snapshot: dict[str, Any] = {}

        def _settled() -> bool:
            row = _row(tmp_path / "first.db", created["id"])
            if row.get("run_count") != 1 or len(sessions.sends) != 1:
                return False
            if json.loads(row["event_position"]) == {"cursor": "c1", "id": "evt-1"}:
                snapshot["row"] = row
                return True
            return False

        await _wait_for(_settled, what="the first delivery settled")
        expected = (
            "Deploy finished: react.\n\n"
            + _GUARD
            + ':\n    {"id":"evt-1","timestamp":"2026-11-20T00:00:00Z","data":{"status":"finished"}}'
        )
        assert sessions.sends[0][1] == expected
        row = snapshot["row"]
        assert row["run_count"] == 1
        assert row["status"] == "active"
        assert json.loads(row["event_position"]) == {"cursor": "c1", "id": "evt-1"}
        log = _log_events(tmp_path, "fixture")
        assert not [e for e in log if e.get("event") == "events_poll"]
        assert not [e for e in log if e.get("event") == "call"]
        assert [e for e in log if e.get("event") == "stream_error"]  # observed, stream survived
        # reconcile is idempotent: an already-watched row gains no second stream
        before = len(_stream_requests(tmp_path))
        await service.reconcile_event_watches()
        await asyncio.sleep(0.3)
        assert len(_stream_requests(tmp_path)) == before

    asyncio.run(_scenario(tmp_path, "first", manager, body))


def test_duplicate_event_id_skipped_and_heartbeat_commits_cursor(tmp_path: Path) -> None:
    event = {
        "type": "event",
        "eventId": "evt-1",
        "name": "deploys",
        "timestamp": "2026-11-20T00:00:00Z",
        "data": {"status": "finished"},
        "cursor": "c1",
    }
    manager = _ScriptedManager([[dict(event), dict(event), {"type": "heartbeat", "cursor": "c5"}]])

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        await _wait_for(lambda: len(sessions.sends) >= 1, what="the delivery")
        await _wait_for(
            lambda: (_position(tmp_path, "dedupe", created["id"]) or {}).get("cursor") == "c5",
            what="the heartbeat cursor commit",
        )
        assert len(sessions.sends) == 1  # the duplicate eventId never re-delivers
        assert _position(tmp_path, "dedupe", created["id"]) == {"cursor": "c5", "id": "evt-1"}
        assert manager.stream_calls[0]["arguments"] == {"build": 7}
        assert manager.stream_calls[0]["cursor"] is None  # a fresh watch has no cursor
        assert manager.stream_calls[0]["name"] == "deploys"
        await service.remove(created["id"])

    asyncio.run(_scenario(tmp_path, "dedupe", manager, body, scheduler=False))


def test_clean_break_reopens_from_the_committed_cursor(tmp_path: Path) -> None:
    manager = _manager(tmp_path, MCP_FIXTURE_STREAM_MODE="clean_break", MCP_FIXTURE_BREAK_AFTER="1")

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        # The fixture stream runs forever (one event per clean break, reopen
        # from the committed cursor, repeat), so ``run_count`` and the sends
        # log race forward together. A bare ``sends >= 2`` wait can resume
        # while the watcher sits between a send and its settle (an await
        # apart -- the CI flake in run 44796687823). Poll instead for one
        # coherent snapshot: the predicate is synchronous, so no watcher
        # step can interleave inside it.
        snapshot: dict[str, Any] = {}

        def _settled() -> bool:
            row = _row(tmp_path / "brk.db", created["id"])
            count = row.get("run_count", 0)
            if count < 2 or count != len(sessions.sends):
                return False
            position = json.loads(row["event_position"])
            if position == {"cursor": f"c{count}", "id": f"evt-{count}"}:
                snapshot["row"] = row
                return True
            return False

        await _wait_for(_settled, what="the settled post-reopen delivery")
        requests = _stream_requests(tmp_path)
        assert len(requests) >= 2
        assert requests[1]["params"]["cursor"] == "c1"  # the committed cursor
        row = snapshot["row"]
        assert row["run_count"] == len(sessions.sends) >= 2
        position = json.loads(row["event_position"])
        assert position == {"cursor": f"c{row['run_count']}", "id": f"evt-{row['run_count']}"}

    asyncio.run(_scenario(tmp_path, "brk", manager, body))


def test_three_silent_reconnects_park(tmp_path: Path) -> None:
    manager = _manager(tmp_path, MCP_FIXTURE_STREAM_MODE="silent")

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "silent.db"
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", timeout_s=6.0, what="park")
        row = _row(db_path, created["id"])
        assert row["last_outcome"] == "failed"
        assert "exhaust" in (row["last_output"] or "").lower()
        assert len(sessions.sends) == 0
        assert len(_stream_requests(tmp_path)) >= 4  # the open plus three reopens

    asyncio.run(_scenario(tmp_path, "silent", manager, body))


def test_terminated_notification_parks_without_reopen(tmp_path: Path) -> None:
    manager = _manager(tmp_path, MCP_FIXTURE_STREAM_MODE="terminate", MCP_FIXTURE_BREAK_AFTER="1")

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "term.db"
        await _wait_for(lambda: len(sessions.sends) >= 1, what="the first delivery")
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", what="the terminal park")
        row = _row(db_path, created["id"])
        assert "terminated" in (row["last_output"] or "").lower()
        requests = len(_stream_requests(tmp_path))
        await asyncio.sleep(0.4)
        assert len(_stream_requests(tmp_path)) == requests  # terminal: never reopened

    asyncio.run(_scenario(tmp_path, "term", manager, body))


def test_rejected_stream_parks(tmp_path: Path) -> None:
    manager = _manager(tmp_path, MCP_FIXTURE_STREAM_MODE="reject")

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "rej.db"
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", timeout_s=6.0, what="park")
        row = _row(db_path, created["id"])
        assert "reject" in (row["last_output"] or "").lower()
        assert len(sessions.sends) == 0
        # reject mode answers before logging stream_request: the proof of
        # "never opened, never reopened" is no stream lifecycle entries.
        assert not [
            e
            for e in _log_events(tmp_path, "fixture")
            if e.get("event") in ("stream_request", "stream_active", "stream_event")
        ]

    asyncio.run(_scenario(tmp_path, "rej", manager, body))


def test_truncated_active_demands_recreate(tmp_path: Path) -> None:
    manager = _ScriptedManager([[{"type": "active", "cursor": None, "truncated": True}]])

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "trunc.db"
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", timeout_s=6.0, what="park")
        row = _row(db_path, created["id"])
        assert "recreate" in (row["last_output"] or "").lower()
        assert len(sessions.sends) == 0
        assert len(manager.stream_calls) == 1  # fail face parks, no reopen

    asyncio.run(_scenario(tmp_path, "trunc", manager, body, scheduler=False))


# ---------------------------------------------------------------------------
# lifecycle (pause/resume/remove) + scheduler cohabitation
# ---------------------------------------------------------------------------


def test_pause_cancels_the_stream_resume_reopens_from_cursor(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        await _wait_for(lambda: len(sessions.sends) >= 1, what="the first delivery")
        info = await service.pause(created["id"])
        assert info["status"] == "paused"
        await _wait_for(
            lambda: any(e.get("event") == "stream_cancelled" for e in _log_events(tmp_path, "fixture")),
            what="pause to cancel the stream",
        )
        frozen = len(sessions.sends)
        position = _position(tmp_path, "pr", created["id"])
        await asyncio.sleep(0.45)  # paused: several event periods pass silently
        assert len(sessions.sends) == frozen
        before = len(_stream_requests(tmp_path))
        await service.resume(created["id"])
        await _wait_for(lambda: len(_stream_requests(tmp_path)) > before, what="resume to reopen")
        last = _stream_requests(tmp_path)[-1]
        assert last["params"]["cursor"] == (position or {}).get("cursor")
        await _wait_for(lambda: len(sessions.sends) > frozen, what="delivery to resume")

    asyncio.run(_scenario(tmp_path, "pr", manager, body))


def test_remove_cancels_the_watch_and_deletes_the_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        await _wait_for(lambda: len(sessions.sends) >= 1, what="the first delivery")
        await service.remove(created["id"])
        await _wait_for(
            lambda: any(e.get("event") == "stream_cancelled" for e in _log_events(tmp_path, "fixture")),
            what="remove to cancel the stream",
        )
        with pytest.raises(_NotFound):
            await service.get(created["id"])
        sends = len(sessions.sends)
        requests = len(_stream_requests(tmp_path))
        await asyncio.sleep(0.35)
        assert len(sessions.sends) == sends
        assert len(_stream_requests(tmp_path)) == requests

    asyncio.run(_scenario(tmp_path, "rm", manager, body))


def test_event_prompts_never_coalesce(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        await _wait_for(lambda: len(sessions.sends) >= 2, timeout_s=6.0, what="two pending-resistant deliveries")

    asyncio.run(_scenario(tmp_path, "coal", manager, body, sessions=_AlwaysPendingSessions()))


def test_tick_and_wake_ignore_event_automations(tmp_path: Path) -> None:
    manager = _ScriptedManager([[]])

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        assert await service.tick() == 0
        assert await service.next_wake_after(int(time.time() * 1000)) is None
        await service.remove(created["id"])

    asyncio.run(_scenario(tmp_path, "tick", manager, body, scheduler=False))


# ---------------------------------------------------------------------------
# fail faces (size caps)
# ---------------------------------------------------------------------------


def test_oversized_event_data_is_invalid(tmp_path: Path) -> None:
    huge = '{"status": "' + "x" * 20000 + '"}'
    manager = _manager(tmp_path, MCP_FIXTURE_EVENT_DATA=huge)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "big.db"
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", timeout_s=6.0, what="park")
        row = _row(db_path, created["id"])
        assert "invalid mcp event" in (row["last_output"] or "").lower()
        assert row["run_count"] == 0
        assert len(sessions.sends) == 0
        assert len(_stream_requests(tmp_path)) == 1  # the per-message fail parks, no reopen

    asyncio.run(_scenario(tmp_path, "big", manager, body))


def test_event_prompt_over_32kib_parks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    async def body(service: Any, sessions: Any, created: dict[str, Any]) -> None:
        db_path = tmp_path / "cap.db"
        await _wait_for(lambda: _row(db_path, created["id"])["status"] == "paused", timeout_s=6.0, what="park")
        row = _row(db_path, created["id"])
        assert "exceeds 32 kib" in (row["last_output"] or "").lower()
        assert len(sessions.sends) == 0

    asyncio.run(_scenario(tmp_path, "cap", manager, body, action=_action("x" * 40000)))
