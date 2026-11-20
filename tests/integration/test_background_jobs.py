"""T-C3-002 contract tests: background-jobs persistence + restart replay.

Red-first specification for the capability banked in
doc/plans/port-232-increments.md T-C3-002. The module under contract is
``opencode.tools.background`` (table ``background_job``), the threshold
lives on ``ToolLimits.background_after_s``, and the registry arms the
whole face with an optional ``database``. Every test here names the API
it expects; the implementation aligns to this file, not the reverse.

Upstream anchors (2.3.2 ``core/src/job.ts`` + ``job/{sql,recovery}.ts``):

- persistence timing -- the row is upserted the moment a call
  *backgrounds* (``start(background)`` before the fork), settles to a
  terminal payload when the underlying work finishes, and is deleted
  once the notification is admitted (here: replay delivery);
- restart replay -- settled rows replay their payload as a synthetic
  notification; a *running* shell row cannot be reattached in-process
  (no shell daemon), so it replays as an honest error ("interrupted by
  server restart"), never a fake success;
- per-session cleanup -- ``cancelSession`` deletes the session's rows.

The store-level tests drive the row API directly (deterministic, no
process timing); the two seam tests go through ``registry.invoke`` with
a real interpreter, one event loop per scenario so the background
continuation outlives the foreground await.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

from opencode.contracts.messages import ToolTextContent
from opencode.session.service import IdFactory
from opencode.storage import open_database
from opencode.storage.db import Database
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import BashAdapter
from tests.integration.test_tool_bash import (
    _FIXED_TIME,
    _FixedIds,
    _interpreter_or_skip,
    _RecordingSink,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_SESSION_ID = "ses_itbgjob"
_RUN_ID = "run_itbgjob"
_MESSAGE_ID = "msg_itbgjob"
_TOOL_CALL_ID = "call_itbgjob"
_THRESHOLD_S = 1.0
_SETTLE_DEADLINE_S = 8.0
_POLL_INTERVAL_S = 0.1


def _sleep_command(seconds: float, marker: str) -> str:
    if _PLATFORM == "win32":
        return f"Start-Sleep -Seconds {seconds}; Write-Output '{marker}'"
    return f"sleep {seconds} && echo '{marker}'"


def _text(outcome: ToolOutcome) -> str:
    return "\n".join(part.text for part in outcome.content if isinstance(part, ToolTextContent))


def _registry(database: Database | None = None) -> ToolRegistry:
    registry = ToolRegistry(
        sink=cast("Any", _RecordingSink()),
        clock=lambda: _FIXED_TIME,
        ids=cast(IdFactory, _FixedIds()),
        database=database,
    )
    registry.register(BashAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


def _context(limits: ToolLimits) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=".",
        limits=limits,
    )


def _row_status(db_path: Path, notification_id: str | None = None) -> list[tuple[str, str]]:
    """Raw sqlite read of ``background_job`` -- pins the table + column names."""
    conn = sqlite3.connect(db_path)
    try:
        if notification_id is None:
            return cast(
                "list[tuple[str, str]]",
                conn.execute("select notification_id, status from background_job").fetchall(),
            )
        return cast(
            "list[tuple[str, str]]",
            conn.execute(
                "select notification_id, status from background_job where notification_id = ?",
                (notification_id,),
            ).fetchall(),
        )
    finally:
        conn.close()


def _migrated_db(tmp_path: Path, name: str = "jobs.db") -> Path:
    """A tmp database migrated to head via the real alembic CLI.

    The compaction-engine precedent: migrations run on the sync engine
    (``migrations/env.py``), never inside ``open_database``, so a test
    database reaches head explicitly before the async engine opens it.
    """
    db_path = tmp_path / name
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"alembic failed\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return db_path


async def _wait_settled(db_path: Path, notification_id: str) -> None:
    deadline = time.monotonic() + _SETTLE_DEADLINE_S
    while time.monotonic() < deadline:
        rows = _row_status(db_path, notification_id)
        if rows and rows[0][1] not in {"backgrounded", "running"}:
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"row {notification_id} did not settle within {_SETTLE_DEADLINE_S}s")


# ---------------------------------------------------------------------------
# API surface (fields, constructor params, module faces)
# ---------------------------------------------------------------------------


def test_background_after_s_field_on_limits() -> None:
    """``ToolLimits.background_after_s`` exists; default None keeps the inline face."""
    defaults = ToolLimits()
    assert getattr(defaults, "background_after_s", "missing") is None
    wired = ToolLimits(background_after_s=_THRESHOLD_S)
    assert wired.background_after_s == _THRESHOLD_S


def test_registry_database_param_optional() -> None:
    """``ToolRegistry`` takes ``database``; None keeps the pre-capability face."""
    without = _registry(None)
    assert without is not None
    db_path = Path(__import__("tempfile").mkdtemp()) / "api.db"

    async def _with_database() -> None:
        async with open_database(db_path, mode="create") as database:
            _registry(database)

    asyncio.run(_with_database())


# ---------------------------------------------------------------------------
# seam behavior (real interpreter, one event loop per scenario)
# ---------------------------------------------------------------------------


def test_short_call_stays_inline_no_row(tmp_path: Path) -> None:
    """Below the threshold nothing changes: inline delivery, no persisted row."""
    _interpreter_or_skip()
    db_path = _migrated_db(tmp_path)

    async def _scenario() -> None:
        async with open_database(db_path, mode="create") as database:
            registry = _registry(database)
            outcome = await registry.invoke(
                _context(ToolLimits(background_after_s=_THRESHOLD_S)),
                "bash__run",
                {"command": _sleep_command(0.1, "done-short")},
                "exec_itbgjob_short",
            )
            assert "done-short" in _text(outcome)
            assert _row_status(db_path) == []

    asyncio.run(_scenario())


def test_long_call_backgrounds_settles_persists(tmp_path: Path) -> None:
    """Past the threshold: bounded marker returns fast, the row appears with the
    backgrounded status, settles to a terminal payload carrying the output."""
    _interpreter_or_skip()
    db_path = _migrated_db(tmp_path)

    async def _scenario() -> str:
        async with open_database(db_path, mode="create") as database:
            registry = _registry(database)
            started = time.monotonic()
            outcome = await registry.invoke(
                _context(ToolLimits(background_after_s=_THRESHOLD_S)),
                "bash__run",
                {"command": _sleep_command(3, "done-bg")},
                "exec_itbgjob_long",
            )
            wall = time.monotonic() - started
            # The foreground await is bounded by the threshold, not the call.
            assert wall < 2.5, f"backgrounding did not bound the wall: {wall:.2f}s"
            text = _text(outcome)
            assert "done-bg" not in text, "full output leaked inline past the threshold"
            assert "background" in text.lower()
            # The row exists from the backgrounding moment (upsert BEFORE return).
            rows = _row_status(db_path)
            assert len(rows) == 1, f"expected exactly one background_job row, got {rows}"
            notification_id, status = rows[0]
            assert status in {"backgrounded", "running"}
            # The background continuation settles the row with the real output.
            await _wait_settled(db_path, notification_id)
            return notification_id

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# store + replay semantics (deterministic, row API driven)
# ---------------------------------------------------------------------------


def _running_row(notification_id: str, session_id: str = _SESSION_ID) -> Any:
    from opencode.tools.background import BackgroundJobRow

    return BackgroundJobRow(
        notification_id=notification_id,
        session_id=session_id,
        tool_call_id=f"call_{notification_id}",
        kind="shell",
        status="backgrounded",
        title="sleep 30",
        payload=None,
    )


def _settled_row(notification_id: str, session_id: str = _SESSION_ID) -> Any:
    from opencode.tools.background import BackgroundJobRow

    return BackgroundJobRow(
        notification_id=notification_id,
        session_id=session_id,
        tool_call_id=f"call_{notification_id}",
        kind="shell",
        status="succeeded",
        title="echo",
        payload={"text": "done-settled"},
    )


def test_replay_empty_database_is_empty(tmp_path: Path) -> None:
    from opencode.tools.background import replay_pending

    db_path = _migrated_db(tmp_path)

    async def _scenario() -> None:
        async with open_database(db_path, mode="create") as database:
            assert await replay_pending(database) == []

    asyncio.run(_scenario())


def test_replay_settled_row_delivers_payload_and_deletes(tmp_path: Path) -> None:
    """A settled-but-unadmitted row survives a restart and replays its payload."""
    from opencode.tools.background import replay_pending, upsert_row

    db_path = _migrated_db(tmp_path)

    async def _seed() -> None:
        async with open_database(db_path, mode="create") as database:
            await upsert_row(database, _settled_row("ntf_settled_1"))

    async def _replay() -> Any:
        async with open_database(db_path, mode="readwrite") as database:
            return await replay_pending(database)

    asyncio.run(_seed())
    replayed = asyncio.run(_replay())
    assert len(replayed) == 1
    entry = replayed[0]
    assert entry.notification_id == "ntf_settled_1"
    assert entry.session_id == _SESSION_ID
    assert entry.status == "succeeded"
    assert "done-settled" in entry.text
    # Admission deletes: a second replay on the reopened database is empty.
    assert asyncio.run(_replay()) == []


def test_replay_running_shell_row_replays_honest_error(tmp_path: Path) -> None:
    """A running shell row cannot reattach (no daemon): restart replays an
    error naming the interruption, never a fake success, then deletes."""
    from opencode.tools.background import replay_pending, upsert_row

    db_path = _migrated_db(tmp_path)

    async def _seed() -> None:
        async with open_database(db_path, mode="create") as database:
            await upsert_row(database, _running_row("ntf_running_1"))

    async def _replay() -> Any:
        async with open_database(db_path, mode="readwrite") as database:
            return await replay_pending(database)

    asyncio.run(_seed())
    replayed = asyncio.run(_replay())
    assert len(replayed) == 1
    entry = replayed[0]
    assert entry.notification_id == "ntf_running_1"
    assert entry.status == "error"
    assert "restart" in entry.text.lower()
    assert asyncio.run(_replay()) == []


def test_delete_for_session_scopes_to_session(tmp_path: Path) -> None:
    """Session cancellation removes only that session's rows and reports the count."""
    from opencode.tools.background import delete_for_session, upsert_row

    db_path = _migrated_db(tmp_path)

    async def _scenario() -> None:
        async with open_database(db_path, mode="create") as database:
            await upsert_row(database, _settled_row("ntf_a1", session_id="ses_A"))
            await upsert_row(database, _settled_row("ntf_a2", session_id="ses_A"))
            await upsert_row(database, _settled_row("ntf_b1", session_id="ses_B"))
            removed = await delete_for_session(database, "ses_A")
            assert removed == 2
            assert _row_status(db_path) == [("ntf_b1", "succeeded")]

    asyncio.run(_scenario())
