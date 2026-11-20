"""T-C3-003 evaluation harness: automation (prompt action) capability, before/after levels.

A measurement face, not an assertion face: every probe records a metric and
passes, whether or not the automation capability exists. Running this module
on pre-implementation code captures the BEFORE baseline; the identical inputs
on post-implementation code give the AFTER column. The delta is the ticket's
evidence against doc/GOAL.md's unattended-execution gate (定时巡检/变更窗口
的无人值守运行态):

* M1 trigger-latency wall -- an ``every`` automation past its slot must fire
  within the scheduler's wake granularity, not the next human turn;
* M2 restart replay -- an ``at`` automation whose slot passed while the
  process was down runs once (late), then is done: never lost, never twice;
* M3 phase-keep -- a repeating automation that missed slots skips them
  (``next_due`` advances from the ORIGINAL slot, no catch-up burst);
* M4 failure backoff -- a failed prompt delivery backs off by
  ``failed_retry_s`` (injected short), no hot retry loop;
* M5 coalesce -- while the previous prompt is still pending in the target's
  inbox the next slot is skipped, not stacked.

Capability probes are pinned to the banked T-C3-003 seam contract (see
doc/plans/port-232-increments.md): package ``opencode.automation`` with
``service.AutomationService(database=, sessions=, now_ms=, ...timing params)``
exposing ``create(dict) -> dict`` and ``tick() -> int``;
``scheduler.AutomationScheduler(service=, max_sleep_s=, leader_ttl_s=)`` as an
async context manager; sqlite tables ``automation`` + ``automation_lease`` at
alembic head; tool adapters under ``opencode.tools.automation`` registering
the five ``opencode__automation_*`` wire names. A post-implementation
``api-mismatch`` status means the harness and the implementation drifted: fix
the alignment, never the reading.

Timing parameters are injected short per the repo discipline (时序参数必须可
配置, 测试注短值防抖): min interval 1s, failed retry 0.5s, max sleep 50ms,
leader TTL 1s.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opencode.storage import open_database
from tests.integration.test_tool_bash import _FIXED_TIME, _FixedIds, _RecordingSink

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROJECT = "prj_autoeval"
_SESSION_ID = "ses_autoeval"
#: every-trigger period for M1 (s); the service face takes seconds (float).
_EVERY_S = 2.0
#: injected timing (repo discipline: short values, anti-flake).
_MIN_INTERVAL_S = 1.0
_FAILED_RETRY_S = 0.5
_LATE_AFTER_S = 60.0
_MAX_SLEEP_S = 0.05
_LEADER_TTL_S = 1.0
#: how long M1 waits for the first fire before reporting an honest timeout.
_FIRE_DEADLINE_S = 15.0

_EXPECTED_TOOLS = (
    "opencode__automation_create",
    "opencode__automation_list",
    "opencode__automation_pause",
    "opencode__automation_resume",
    "opencode__automation_delete",
)


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _service_module() -> Any | None:
    if importlib.util.find_spec("opencode.automation") is None:
        return None
    return importlib.import_module("opencode.automation.service")


def _migrate(db_path: Path) -> None:
    """Bring a fresh database to alembic head on the sync engine (compaction-test
    precedent); ``open_database`` does not create tables."""
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


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {str(row[0]) for row in conn.execute("select name from sqlite_master where type='table'").fetchall()}
    finally:
        conn.close()


def _row(db_path: Path, automation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute("select * from automation where id = ?", (automation_id,)).fetchone()
        return dict(found) if found is not None else {}
    finally:
        conn.close()


def _insert_row(db_path: Path, **overrides: Any) -> str:
    """A pre-seeded active row: the crash-restart shape each M2/M3 metric starts from."""
    now = int(time.time() * 1000)
    every = overrides.pop("every_s", 30.0)
    trigger = overrides.pop("trigger", {"type": "every", "every": every})
    values = {
        "id": overrides.pop("id", "aut_autoeval"),
        "project_id": _PROJECT,
        "session_id": None,
        "directory": str(overrides.pop("directory", REPO_ROOT)),
        "name": "seeded",
        "trigger": json.dumps(trigger),
        "tz": "UTC",
        "action": json.dumps({"type": "prompt", "prompt": "check", "to": {"session": _SESSION_ID}}),
        "status": "active",
        "next_due_at": overrides.pop("next_due_at", now - 90_000),
        "expires_at": None,
        "run_count": 0,
        "failures": 0,
        "last_run_at": None,
        "last_message_id": None,
        "last_outcome": None,
        "last_output": None,
        "retry_at": None,
        "state": None,
        "event_position": None,
        "time_created": now,
        "time_updated": now,
        **overrides,
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "insert into automation ({}) values ({})".format(", ".join(values), ", ".join("?" for _ in values)),
            tuple(json.dumps(v) if isinstance(v, (dict, list)) else v for v in values.values()),
        )
        conn.commit()
    finally:
        conn.close()
    return str(values["id"])


class _RecordingSessions:
    """Fake for the ``AutomationSessions`` port (structural: prompt delivery,
    child creation, project check, inbox-pending check) -- no LLM, no store."""

    def __init__(self, *, fail: bool = False, always_pending: bool = False) -> None:
        self.sends: list[tuple[str, str]] = []
        self.children: list[tuple[str, str, str]] = []
        self.fail = fail
        self.always_pending = always_pending

    def resolve_session(self, session_id: str) -> Any:
        if session_id != _SESSION_ID:
            return None
        return SimpleNamespace(id=_SESSION_ID, project_id=_PROJECT)

    def send_prompt(self, session_id: str, text: str, request_id: str) -> str:
        if self.fail:
            raise RuntimeError("delivery failed (injected)")
        self.sends.append((session_id, request_id))
        return f"msg_{len(self.sends)}"

    def create_child(self, parent_session_id: str, agent: str, child_session_id: str) -> None:
        self.children.append((parent_session_id, agent, child_session_id))

    def prompt_pending(self, session_id: str, message_id: str) -> bool:
        return self.always_pending


def _make_service(database: Any, sessions: _RecordingSessions) -> Any:
    service_module = _service_module()
    if service_module is None:
        raise ModuleNotFoundError("opencode.automation.service")
    return service_module.AutomationService(
        database=database,
        sessions=sessions,
        min_prompt_interval_s=_MIN_INTERVAL_S,
        late_after_s=_LATE_AFTER_S,
        failed_retry_s=_FAILED_RETRY_S,
    )


def _prompt_action() -> dict[str, Any]:
    return {"type": "prompt", "prompt": "run the check", "to": {"session": _SESSION_ID}}


def test_p_capability_probe(tmp_path: Path) -> None:
    """Module + tables + tool-registration probes; BEFORE reports capability-absent."""
    status: str
    tables: set[str] = set()
    tool_names: list[str] = []
    if _service_module() is None:
        status = "capability-absent"
    else:
        try:
            db_path = tmp_path / "probe.db"
            _migrate(db_path)
            tables = _table_names(db_path)
            if "automation" not in tables or "automation_lease" not in tables:
                status = "tables-missing"
            else:
                from typing import cast

                from opencode.tools.automation import AutomationAdapter  # type: ignore[import-untyped]

                from opencode.tools.registry import ToolRegistry

                async def _wired() -> list[str]:
                    async with open_database(db_path, mode="create") as database:
                        service = _make_service(database, _RecordingSessions())
                        registry = ToolRegistry(
                            sink=cast("Any", _RecordingSink()),
                            clock=lambda: _FIXED_TIME,
                            ids=cast("Any", _FixedIds()),
                            database=database,
                        )
                        for tool in ("create", "list", "pause", "resume", "delete"):
                            registry.register(AutomationAdapter(service, tool=tool))
                        catalog = registry.snapshot(_SESSION_ID)
                        return sorted(name for name in _EXPECTED_TOOLS if catalog.resolve(name) is not None)

                tool_names = asyncio.run(_wired())
                status = "probed" if len(tool_names) == len(_EXPECTED_TOOLS) else "tools-missing"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "P-capability",
            "status": status,
            "module": _service_module() is not None,
            "tables": sorted(t for t in tables if t.startswith("automation")),
            "tools_registered": tool_names,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch", "tables-missing", "tools-missing"))


def test_m1_every_fire_wall_clock(tmp_path: Path) -> None:
    """An every automation fires on its own within wake granularity after due."""
    status = "capability-absent"
    wall_s: float | None = None
    sends: int | None = None
    if _service_module() is not None:
        try:
            db_path = tmp_path / "m1.db"
            _migrate(db_path)

            async def _run() -> tuple[float, int]:
                sessions = _RecordingSessions()
                async with open_database(db_path, mode="create") as database:
                    service = _make_service(database, sessions)
                    await service.create(
                        {
                            "name": "eval-every",
                            "project_id": _PROJECT,
                            "directory": str(tmp_path),
                            "trigger": {"type": "every", "every": _EVERY_S},
                            "action": _prompt_action(),
                        }
                    )
                    scheduler_module = importlib.import_module("opencode.automation.scheduler")
                    started = time.monotonic()
                    async with scheduler_module.AutomationScheduler(
                        service=service, max_sleep_s=_MAX_SLEEP_S, leader_ttl_s=_LEADER_TTL_S
                    ):
                        deadline = started + _EVERY_S + _FIRE_DEADLINE_S
                        # Poll a sync fake (no Event to await): measurement face, bounded by deadline.
                        while not sessions.sends and time.monotonic() < deadline:  # noqa: ASYNC110
                            await asyncio.sleep(0.02)
                    return time.monotonic() - started, len(sessions.sends)

            wall_s, sends = asyncio.run(_run())
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M1-every-fire",
            "status": status,
            "every_s": _EVERY_S,
            "max_sleep_s": _MAX_SLEEP_S,
            "wall_s": None if wall_s is None else round(wall_s, 2),
            "deliveries": sends,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))


def test_m2_at_restart_replays_once(tmp_path: Path) -> None:
    """An at-automation whose slot passed while down: one late run, then done."""
    status = "capability-absent"
    sends: int | None = None
    outcome: str | None = None
    row_status: str | None = None
    second_tick_sends: int | None = None
    if _service_module() is not None:
        try:
            db_path = tmp_path / "m2.db"
            _migrate(db_path)
            now = int(time.time() * 1000)
            automation_id = _insert_row(
                db_path,
                id="aut_eval_at",
                trigger={"type": "at", "at": "2020-01-01T00:00:00Z"},
                next_due_at=now - 120_000,
            )

            async def _run() -> tuple[int, str, str, int]:
                sessions = _RecordingSessions()
                async with open_database(db_path, mode="create") as database:
                    service = _make_service(database, sessions)
                    await service.tick()
                    row = _row(db_path, automation_id)
                    after_first = len(sessions.sends)
                    await service.tick()
                    return after_first, str(row["last_outcome"]), str(row["status"]), len(sessions.sends)

            sends, outcome, row_status, second_tick_sends = asyncio.run(_run())
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M2-at-replay-once",
            "status": status,
            "deliveries": sends,
            "second_tick_deliveries": second_tick_sends,
            "last_outcome": outcome,
            "row_status": row_status,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))


def test_m3_every_phase_keep(tmp_path: Path) -> None:
    """Missed slots are skipped: next_due advances from the original slot."""
    status = "capability-absent"
    every_ms = 30_000
    phase_drift_ms: int | None = None
    run_count: int | None = None
    if _service_module() is not None:
        try:
            db_path = tmp_path / "m3.db"
            _migrate(db_path)
            now = int(time.time() * 1000)
            due = now - 3 * every_ms
            automation_id = _insert_row(db_path, id="aut_eval_phase", next_due_at=due, every_s=30.0)

            async def _run() -> None:
                async with open_database(db_path, mode="create") as database:
                    service = _make_service(database, _RecordingSessions())
                    await service.tick()

            asyncio.run(_run())
            row = _row(db_path, automation_id)
            expected_next = due + 4 * every_ms
            phase_drift_ms = int(row["next_due_at"]) - expected_next
            run_count = int(row["run_count"])
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M3-phase-keep",
            "status": status,
            "slots_missed": 3,
            "run_count": run_count,
            "phase_drift_ms": phase_drift_ms,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))


def test_m4_failure_backoff(tmp_path: Path) -> None:
    """A failed delivery retries only after failed_retry_s, not hot."""
    status = "capability-absent"
    attempts: int | None = None
    attempts_during_backoff: int | None = None
    attempts_after_backoff: int | None = None
    if _service_module() is not None:
        try:
            db_path = tmp_path / "m4.db"
            _migrate(db_path)

            async def _run() -> tuple[int, int, int]:
                sessions = _RecordingSessions(fail=True)
                async with open_database(db_path, mode="create") as database:
                    service = _make_service(database, sessions)
                    info = await service.create(
                        {
                            "name": "eval-backoff",
                            "project_id": _PROJECT,
                            "directory": str(tmp_path),
                            "trigger": {"type": "every", "every": _MIN_INTERVAL_S},
                            "action": _prompt_action(),
                        }
                    )
                    deadline = time.monotonic() + _FIRE_DEADLINE_S
                    while not sessions.sends and not _row(db_path, str(info["id"])).get("retry_at"):
                        if time.monotonic() > deadline:
                            break
                        await service.tick()
                        await asyncio.sleep(0.05)
                    after_failure = len(sessions.sends)
                    await service.tick()
                    during_backoff = len(sessions.sends)
                    await asyncio.sleep(_FAILED_RETRY_S + 0.3)
                    await service.tick()
                    await asyncio.sleep(0.1)
                    await service.tick()
                    return after_failure, during_backoff, len(sessions.sends)

            attempts, attempts_during_backoff, attempts_after_backoff = asyncio.run(_run())
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M4-backoff",
            "status": status,
            "failed_retry_s": _FAILED_RETRY_S,
            "attempts_first_due": attempts,
            "attempts_during_backoff": attempts_during_backoff,
            "attempts_after_backoff": attempts_after_backoff,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))


def test_m5_coalesce_pending_prompt(tmp_path: Path) -> None:
    """A still-pending previous prompt skips the next slot instead of stacking."""
    status = "capability-absent"
    deliveries: int | None = None
    outcome: str | None = None
    if _service_module() is not None:
        try:
            db_path = tmp_path / "m5.db"
            _migrate(db_path)

            async def _run() -> tuple[int, str]:
                sessions = _RecordingSessions(always_pending=True)
                async with open_database(db_path, mode="create") as database:
                    service = _make_service(database, sessions)
                    info = await service.create(
                        {
                            "name": "eval-coalesce",
                            "project_id": _PROJECT,
                            "directory": str(tmp_path),
                            "trigger": {"type": "every", "every": _MIN_INTERVAL_S},
                            "action": _prompt_action(),
                        }
                    )
                    automation_id = str(info["id"])
                    deadline = time.monotonic() + _FIRE_DEADLINE_S
                    while time.monotonic() < deadline:
                        await service.tick()
                        row = _row(db_path, automation_id)
                        if row.get("last_outcome") == "skipped":
                            break
                        await asyncio.sleep(0.05)
                    row = _row(db_path, automation_id)
                    return len(sessions.sends), str(row.get("last_outcome"))

            deliveries, outcome = asyncio.run(_run())
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}: {error}"
    _report(
        {
            "scenario": "M5-coalesce",
            "status": status,
            "deliveries": deliveries,
            "last_outcome": outcome,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))
