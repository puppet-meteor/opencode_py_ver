"""T-C3-015 red set: the boot background-row replay sweep, hard contracts.

The assertion face over ``test_bg_replay_eval.py``'s measurement face. Every
test here is a strict pin on the seam the eval measured gray:

* ``SessionService.replay_background_pending()`` -- the per-row boot sweep.
  Per row: map (the SAME mapping ``replay_pending`` uses -- zero drift),
  deliver via ``deliver_event_wake`` (inbox_id=notification_id), and
  deliver-then-delete: success INCLUDING the idempotent seen-noop consumes
  the row; a missing session (NotFoundError) acknowledges by deleting; any
  other failure keeps the row (at-least-once) and never breaks the sweep.
* mapping pins (exact-equality, the zero-drift proof): a settled shell row
  replays ``shell_report_text(payload)`` verbatim; a still-running shell row
  replays the restart interruption text; a settled subagent row replays the
  wrapped ``<subagent>`` report with outstanding read LIVE; a running
  subagent row is KEPT (PY has no ``execution.resume``); a subagent row
  whose parent left no trace is dropped silently.
* provenance rides the parked message's metadata carrier
  (``{source: "background", kind, status, title}``, the settlement face's
  shape; ``status`` is the MAPPED status).
* the App boot follow-up ``_background_replay_task`` (the third post-scan
  task, the wake/rearm house style, subagent stays last per the upstream
  wake order).

Upstream anchor: 2.3.2 ``session/execution/restart.ts`` ``recoverToolJob``
(delivered -> completeBackground; NotFound -> acknowledge; conflict/other
failure -> keep the row) and ``restartMessage`` (running -> honest
interruption, terminal -> settled payload).
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opencode.bus import VolatileBus
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import PY_INBOX_ENQUEUED
from opencode.session import SessionProjector, SessionService
from opencode.storage import ReadRepositories, SessionStore, open_database
from opencode.tools.background import (
    _RESTART_ERROR_TEXT,
    KIND_SHELL,
    KIND_SUBAGENT,
    STATUS_BACKGROUNDED,
    STATUS_SUCCEEDED,
    BackgroundJobRow,
    shell_report_text,
    subagent_report_text,
    upsert_row,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_TITLE = "echo bg-replay-red"
_SETTLED_TEXT = "BG-REPLAY-RED-SETTLED"


def _migrated_db(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert migrated.returncode == 0, f"alembic failed\nstdout:{migrated.stdout}\nstderr:{migrated.stderr}"
    return db_path


def _rows(db_path: Path, notification_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "select notification_id, status from background_job where notification_id = ?",
            (notification_id,),
        ).fetchall()


def _inbox_events(db_path: Path, session_id: str | None = None) -> list[dict[str, Any]]:
    """Every ``py.inbox.enqueued`` data payload (optionally one session's)."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT session_id, data FROM events WHERE type = ?", (PY_INBOX_ENQUEUED,)).fetchall()
    decoded: list[dict[str, Any]] = []
    for row_session, data in rows:
        if session_id is not None and row_session != session_id:
            continue
        try:
            payload = json.loads(str(data))
        except ValueError:
            continue
        if isinstance(payload, dict):
            decoded.append(payload)
    return decoded


def _matched(events: list[dict[str, Any]], inbox_id: str) -> list[dict[str, Any]]:
    return [event for event in events if event.get("inbox_id") == inbox_id]


def _service(database: Any) -> SessionService:
    return SessionService(
        store=SessionStore(database.engine, projector=SessionProjector()),
        repos=ReadRepositories(database.engine),
        bus=VolatileBus(),
        working_dir="C:/work",
        database=database,
    )


async def _seed(
    database: Any,
    *,
    notification_id: str,
    session_id: str,
    kind: str = KIND_SHELL,
    status: str = STATUS_SUCCEEDED,
    payload: dict[str, Any] | None = None,
) -> None:
    if payload is None:
        payload = {"text": _SETTLED_TEXT, "command": _TITLE} if status == STATUS_SUCCEEDED else {"command": _TITLE}
    await upsert_row(
        database,
        BackgroundJobRow(
            notification_id=notification_id,
            session_id=session_id,
            tool_call_id=f"call_{notification_id}",
            kind=kind,
            status=status,
            title=_TITLE,
            payload=payload,
        ),
    )


def _message(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message")
    assert isinstance(message, dict)
    return message


def test_p0_seam_faces_wired() -> None:
    """The service sweep method exists and the App boots the third task."""
    from opencode.app import App

    assert hasattr(SessionService, "replay_background_pending"), (
        "SessionService.replay_background_pending is the sweep's owner (T-C3-015 decision 1)"
    )
    assert "_background_replay_task" in inspect.getsource(App.__init__), (
        "App.__init__ must schedule the post-scan background replay task (wake/rearm house style)"
    )


async def test_r1_settled_shell_row_delivered_verbatim_and_consumed(tmp_path: Path) -> None:
    """M1 strict: exact mapping text, provenance metadata, row consumed."""
    db_path = _migrated_db(tmp_path, "r1.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(database, notification_id="ntf_r1", session_id=created.session_id)
            await service.replay_background_pending()
            matched = _matched(_inbox_events(db_path, created.session_id), "ntf_r1")
            assert len(matched) == 1, f"exactly one delivery, got {len(matched)}"
            message = _message(matched[0])
            assert message.get("text") == shell_report_text({"text": _SETTLED_TEXT, "command": _TITLE})
            metadata = message.get("metadata")
            assert isinstance(metadata, dict)
            assert metadata.get("source") == "background"
            assert metadata.get("kind") == KIND_SHELL
            assert metadata.get("status") == STATUS_SUCCEEDED
            assert metadata.get("title") == _TITLE
            assert _rows(db_path, "ntf_r1") == []
        finally:
            await service.close()


async def test_r2_running_shell_row_replays_restart_error_and_consumes(tmp_path: Path) -> None:
    """M2 strict: the honest interruption text, mapped status, row consumed."""
    db_path = _migrated_db(tmp_path, "r2.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(database, notification_id="ntf_r2", session_id=created.session_id, status=STATUS_BACKGROUNDED)
            await service.replay_background_pending()
            matched = _matched(_inbox_events(db_path, created.session_id), "ntf_r2")
            assert len(matched) == 1, f"exactly one delivery, got {len(matched)}"
            message = _message(matched[0])
            assert message.get("text") == _RESTART_ERROR_TEXT.format(title=_TITLE)
            metadata = message.get("metadata")
            assert isinstance(metadata, dict)
            assert metadata.get("status") == "error", "the MAPPED status, not the row's backgrounded"
            assert _rows(db_path, "ntf_r2") == []
        finally:
            await service.close()


async def test_r3_missing_session_row_acknowledged_deleted(tmp_path: Path) -> None:
    """M3 strict: the notice is owed to nobody -- acknowledge, deliver nowhere."""
    db_path = _migrated_db(tmp_path, "r3.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            await _seed(database, notification_id="ntf_r3", session_id="ses_gone_r3")
            await service.replay_background_pending()
            assert _inbox_events(db_path) == [], "no delivery anywhere"
            assert _rows(db_path, "ntf_r3") == [], "the row is acknowledged by deletion"
        finally:
            await service.close()


async def test_r4_delivery_failure_keeps_row_and_sweep_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 strict: a raising delivery keeps the row and never breaks the sweep."""
    db_path = _migrated_db(tmp_path, "r4.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(database, notification_id="ntf_r4", session_id=created.session_id)

            async def _boom(*args: Any, **kwargs: Any) -> None:
                raise RuntimeError("wake destination gone")

            monkeypatch.setattr(service, "deliver_event_wake", _boom)
            await service.replay_background_pending()  # must not raise
            assert len(_rows(db_path, "ntf_r4")) == 1, "the row stands for the next boot"
            await service.replay_background_pending()  # still calm, still kept
            assert len(_rows(db_path, "ntf_r4")) == 1
        finally:
            await service.close()


async def test_r5_seen_delivery_is_noop_and_consumes(tmp_path: Path) -> None:
    """Idempotent re-settle: the seen-noop still consumes (no dup, no loss)."""
    db_path = _migrated_db(tmp_path, "r5.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(database, notification_id="ntf_r5", session_id=created.session_id)
            await service.deliver_event_wake(
                created.session_id,
                inbox_id="ntf_r5",
                text=_SETTLED_TEXT,
                metadata={"source": "test"},
            )
            await service.replay_background_pending()
            matched = _matched(_inbox_events(db_path, created.session_id), "ntf_r5")
            assert len(matched) == 1, f"the durable idempotency admits exactly once, got {len(matched)}"
            assert _rows(db_path, "ntf_r5") == [], "the seen row is consumed, not leaked"
        finally:
            await service.close()


async def test_r6_settled_subagent_row_replays_wrapped_report(tmp_path: Path) -> None:
    """The subagent arm: the wrapped report, outstanding read live, consumed."""
    db_path = _migrated_db(tmp_path, "r6.db")
    child = "ses_child_r6"
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(
                database,
                notification_id="ntf_r6",
                session_id=created.session_id,
                kind=KIND_SUBAGENT,
                payload={
                    "childSessionID": child,
                    "description": "explore the seam",
                    "output": "child body text",
                },
            )
            await service.replay_background_pending()
            matched = _matched(_inbox_events(db_path, created.session_id), "ntf_r6")
            assert len(matched) == 1, f"exactly one delivery, got {len(matched)}"
            expected = subagent_report_text(
                child_session_id=child,
                state="completed",
                description="explore the seam",
                body="child body text",
                outstanding=(),  # the child has no live rows in this database
            )
            assert _message(matched[0]).get("text") == expected
            metadata = _message(matched[0]).get("metadata")
            assert isinstance(metadata, dict)
            assert metadata.get("kind") == KIND_SUBAGENT
            assert _rows(db_path, "ntf_r6") == []
        finally:
            await service.close()


async def test_r7_running_subagent_row_kept_undelivered(tmp_path: Path) -> None:
    """A running child is honest debt: keep the row, deliver nothing."""
    db_path = _migrated_db(tmp_path, "r7.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            created = await service.create(ConfigSnapshot.create())
            await _seed(
                database,
                notification_id="ntf_r7",
                session_id=created.session_id,
                kind=KIND_SUBAGENT,
                status=STATUS_BACKGROUNDED,
                payload={"childSessionID": "ses_child_r7", "description": "probe", "output": ""},
            )
            await service.replay_background_pending()
            assert _inbox_events(db_path) == [], "no fake terminal receipt"
            assert len(_rows(db_path, "ntf_r7")) == 1, "the row survives for the next boot"
        finally:
            await service.close()


async def test_r8_orphan_subagent_row_dropped_silently(tmp_path: Path) -> None:
    """A parent with no durable trace owes nobody: drop the row silently."""
    db_path = _migrated_db(tmp_path, "r8.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            await _seed(
                database,
                notification_id="ntf_r8",
                session_id="ses_orphan_r8",
                kind=KIND_SUBAGENT,
                payload={"childSessionID": "ses_child_r8", "description": "probe", "output": "done"},
            )
            await service.replay_background_pending()
            assert _inbox_events(db_path) == []
            assert _rows(db_path, "ntf_r8") == []
        finally:
            await service.close()


async def test_r9_empty_sweep_is_a_quiet_noop(tmp_path: Path) -> None:
    """No flag face: an empty table means the sweep touches nothing."""
    db_path = _migrated_db(tmp_path, "r9.db")
    async with open_database(db_path, mode="create") as database:
        service = _service(database)
        try:
            await service.replay_background_pending()
            assert _inbox_events(db_path) == []
        finally:
            await service.close()
