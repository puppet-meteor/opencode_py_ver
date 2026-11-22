"""T-C3-015 evaluation harness: boot background-row replay, before/after levels.

A measurement face, not an assertion face (the ``test_bg_jobs_eval.py`` shape):
every scenario probes the T-C3-015 seam contract first and records a metric,
passing whether or not the capability exists. BEFORE = pre-implementation main
(the sweep method absent, the seeded row never touched); AFTER =
post-implementation (the sweep delivers each row's notice and consumes its
row). The delta is the ticket's evidence against doc/GOAL.md's unattended
gate: a settlement whose delivery failed (or whose host died before the
in-process port ran) survives on disk and reaches its session on the next
boot, instead of accumulating forever.

Seam contract (banked in doc/plans/port-232-increments.md T-C3-015):

* ``SessionService.replay_background_pending()`` -- the per-row boot sweep
  (per-row deliver-then-delete; a missing session acknowledges the row by
  deleting it; any other delivery failure keeps the row -- at-least-once);
* the App boot follow-up ``_background_replay_task`` (the third post-scan
  task, the ``_inbox_wake_task``/``_subagent_rearm_task`` house style).

An ``api-mismatch`` status means the harness and the implementation drifted:
fix the alignment, never the reading.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from opencode.bus import VolatileBus
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import PY_INBOX_ENQUEUED
from opencode.contracts.messages import JsonValue
from opencode.session import SessionProjector, SessionService
from opencode.storage import ReadRepositories, SessionStore, open_database
from opencode.tools.background import KIND_SHELL, BackgroundJobRow, upsert_row

REPO_ROOT = Path(__file__).resolve().parents[2]

_SETTLED_TEXT = "BG-REPLAY-SETTLED-MARKER"
_RESTART_MARKER = "restart"
_TITLE = "echo bg-replay"


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


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


def _row_statuses(db_path: Path, notification_id: str | None = None) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        if notification_id is None:
            return connection.execute("select notification_id, status from background_job").fetchall()
        return connection.execute(
            "select notification_id, status from background_job where notification_id = ?",
            (notification_id,),
        ).fetchall()


def _inbox_rows(db_path: Path, session_id: str, inbox_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT session_id, data FROM events WHERE type = ?", (PY_INBOX_ENQUEUED,)).fetchall()
    matched: list[dict[str, Any]] = []
    for row_session, data in rows:
        if row_session != session_id:
            continue
        try:
            decoded = json.loads(str(data))
        except ValueError:
            continue
        if isinstance(decoded, dict) and decoded.get("inbox_id") == inbox_id:
            matched.append(decoded)
    return matched


def _service(database: Any) -> SessionService:
    return SessionService(
        store=SessionStore(database.engine, projector=SessionProjector()),
        repos=ReadRepositories(database.engine),
        bus=VolatileBus(),
        working_dir="C:/work",
        database=database,
    )


async def _seed(database: Any, *, notification_id: str, session_id: str, status: str) -> None:
    payload: dict[str, JsonValue] = (
        {"text": _SETTLED_TEXT, "command": _TITLE} if status == "succeeded" else {"command": _TITLE}
    )
    await upsert_row(
        database,
        BackgroundJobRow(
            notification_id=notification_id,
            session_id=session_id,
            tool_call_id=f"call_{notification_id}",
            kind=KIND_SHELL,
            status=status,
            title=_TITLE,
            payload=payload,
        ),
    )


def _sweep_face(service: SessionService) -> Any:
    return getattr(service, "replay_background_pending", None)


async def _run_sweep(service: SessionService) -> str:
    """Await the sweep; ``absent`` / ``api-mismatch:<Error>`` / ``ran``."""
    face = _sweep_face(service)
    if face is None:
        return "absent"
    try:
        await face()
    except Exception as error:  # the harness reports, never fails
        return f"api-mismatch:{type(error).__name__}"
    return "ran"


def _app_task_wired() -> bool:
    from opencode.app import App

    return "_background_replay_task" in inspect.getsource(App.__init__)


def test_p0_capability_probe() -> None:
    """The two seam faces: the service sweep method + the App boot task."""
    probe_service = getattr(SessionService, "replay_background_pending", None)
    _report({"scenario": "P0-capability", "service_face": probe_service is not None, "app_task": _app_task_wired()})
    # The always-true invariant on both sides: the probe itself never raises.
    assert isinstance(probe_service is not None, bool)


def test_m1_settled_row_replayed_and_consumed(tmp_path: Path) -> None:
    """A settled row reaches its session's inbox and its row is consumed."""
    db_path = _migrated_db(tmp_path, "m1.db")

    import asyncio

    async def _scenario() -> dict[str, Any]:
        async with open_database(db_path, mode="create") as database:
            service = _service(database)
            try:
                created = await service.create(ConfigSnapshot.create())
                await _seed(database, notification_id="ntf_m1", session_id=created.session_id, status="succeeded")
                sweep = await _run_sweep(service)
                inbox = _inbox_rows(db_path, created.session_id, "ntf_m1")
                rows = _row_statuses(db_path, "ntf_m1")
                return {
                    "sweep": sweep,
                    "delivered": bool(inbox),
                    "delivered_text": _SETTLED_TEXT in json.dumps(inbox),
                    "rows_left": len(rows),
                }
            finally:
                await service.close()

    metrics = asyncio.run(_scenario())
    _report({"scenario": "M1-settled-replay", **metrics})
    # Always-true invariants: the seeded row existed before the sweep ran.
    assert metrics["rows_left"] >= 0


def test_m2_running_row_replayed_as_interruption(tmp_path: Path) -> None:
    """A still-running row replays as the honest restart interruption."""
    db_path = _migrated_db(tmp_path, "m2.db")

    import asyncio

    async def _scenario() -> dict[str, Any]:
        async with open_database(db_path, mode="create") as database:
            service = _service(database)
            try:
                created = await service.create(ConfigSnapshot.create())
                await _seed(database, notification_id="ntf_m2", session_id=created.session_id, status="running")
                sweep = await _run_sweep(service)
                inbox = _inbox_rows(db_path, created.session_id, "ntf_m2")
                blob = json.dumps(inbox)
                rows = _row_statuses(db_path, "ntf_m2")
                return {
                    "sweep": sweep,
                    "delivered": bool(inbox),
                    "interruption_worded": _RESTART_MARKER in blob.lower(),
                    "rows_left": len(rows),
                }
            finally:
                await service.close()

    metrics = asyncio.run(_scenario())
    _report({"scenario": "M2-running-replay", **metrics})
    assert metrics["rows_left"] >= 0


def test_m3_missing_session_row_acknowledged(tmp_path: Path) -> None:
    """A row whose session is gone is acknowledged (deleted), delivered nowhere."""
    db_path = _migrated_db(tmp_path, "m3.db")

    import asyncio

    async def _scenario() -> dict[str, Any]:
        async with open_database(db_path, mode="create") as database:
            service = _service(database)
            try:
                await _seed(database, notification_id="ntf_m3", session_id="ses_gone_replay", status="succeeded")
                sweep = await _run_sweep(service)
                rows = _row_statuses(db_path, "ntf_m3")
                with sqlite3.connect(db_path) as connection:
                    enqueued = connection.execute(
                        "SELECT COUNT(*) FROM events WHERE type = ?", (PY_INBOX_ENQUEUED,)
                    ).fetchone()[0]
                return {"sweep": sweep, "rows_left": len(rows), "inbox_events": int(enqueued)}
            finally:
                await service.close()

    metrics = asyncio.run(_scenario())
    _report({"scenario": "M3-missing-session", **metrics})
    assert metrics["rows_left"] >= 0


def test_m4_delivery_failure_keeps_the_row(tmp_path: Path, monkeypatch: Any) -> None:
    """A raising delivery keeps the row standing for the next boot."""
    db_path = _migrated_db(tmp_path, "m4.db")

    import asyncio

    async def _scenario() -> dict[str, Any]:
        async with open_database(db_path, mode="create") as database:
            service = _service(database)
            try:
                created = await service.create(ConfigSnapshot.create())
                await _seed(database, notification_id="ntf_m4", session_id=created.session_id, status="succeeded")

                async def _boom(*args: Any, **kwargs: Any) -> None:
                    raise RuntimeError("wake destination gone")

                monkeypatch.setattr(service, "deliver_event_wake", _boom)
                sweep = await _run_sweep(service)
                rows = _row_statuses(db_path, "ntf_m4")
                return {"sweep": sweep, "rows_left": len(rows)}
            finally:
                await service.close()

    metrics = asyncio.run(_scenario())
    _report({"scenario": "M4-failure-keeps", **metrics})
    assert metrics["rows_left"] >= 0
