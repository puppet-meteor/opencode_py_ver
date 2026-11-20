"""T-C3-003 red contract set: automation prompt actions (at/every/cron).

Pins the banked seam contract (doc/plans/port-232-increments.md T-C3-003)
against upstream 2.3.2 ``core/src/automation.ts`` semantics:

* create validation -- relative ``at`` fixed to an ISO instant and must be
  future; ``every`` >= the (injectable) minimum; cron parses, runs at most
  once a minute and has at least one future slot; a prompt action needs a
  target when no owner session exists, the target session must resolve in
  the automation's project, and a subagent target needs an owner session;
  ``expires`` before the first run is rejected;
* scheduling -- ``following()`` semantics per trigger (at: one shot; every:
  phase-keep, missed slots skipped; cron: first slot STRICTLY after now,
  DST fall-back safe), tick eligibility (active + due<=now + retry gate)
  ordered by ``next_due_at``;
* single-flight -- a run claims the lease per run, so a second service over
  the same database never double-fires one slot;
* settlement -- optimistic concurrency (``run_count`` + status guard in the
  UPDATE): a completion settle never resurrects a removed row, a failure
  settle never overwrites a concurrent pause; a failed prompt delivery backs
  off by ``failed_retry_s`` WITHOUT counting the run and keeps its due slot;
  a coalesced (previous prompt still pending) slot skips AND advances
  without sending;
* lifecycle -- pause idempotent, resume recomputes the next slot and clears
  failure state, remove deletes, unknown ids raise;
* subagent targets -- the child session id is derived from the run
  (``ses_aut_<id>_<run>``) so a retried run adopts the child it already
  started instead of leaking another.

Timing is injected (short values + a mutable clock); no sleeps. Migrations
run on the sync engine (compaction-test precedent); row internals are read
back through sqlite directly so the settle face is pinned at the column
level, not through the Info projection.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opencode.storage import open_database

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROJECT = "prj_autotest"
_SESSION_ID = "ses_autotest"
#: a fixed epoch-ms base: 2026-10-25T00:00:00Z (the Berlin fall-back night).
_BASE_MS = 1_793_414_400_000
_EVERY_MS = 30_000
_LATE_AFTER_S = 60.0
_FAILED_RETRY_S = 5.0
_MIN_INTERVAL_S = 60.0


def _service_module() -> Any:
    if importlib.util.find_spec("opencode.automation") is None:
        raise ModuleNotFoundError("opencode.automation (T-C3-003 implementation)")
    return importlib.import_module("opencode.automation.service")


def _execute(coro: Any) -> Any:
    return asyncio.run(coro)


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


def _row(db_path: Path, automation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute("select * from automation where id = ?", (automation_id,)).fetchone()
        return dict(found) if found is not None else {}
    finally:
        conn.close()


def _insert_row(db_path: Path, **overrides: Any) -> str:
    now = overrides.pop("now_ms", _BASE_MS)
    trigger = overrides.pop("trigger", {"type": "every", "every": _EVERY_MS / 1000})
    values = {
        "id": overrides.pop("id", "aut_red"),
        "project_id": _PROJECT,
        "session_id": None,
        "directory": str(REPO_ROOT),
        "name": "seeded",
        "trigger": json.dumps(trigger),
        "tz": overrides.pop("tz", "UTC"),
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


class _Clock:
    """A mutable injected clock: tests advance it explicitly, no sleeps."""

    def __init__(self, now_ms: int = _BASE_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class _Sessions:
    """Fake ``AutomationSessions`` port with controllable failure/pending hooks."""

    def __init__(self, *, fail: bool = False, always_pending: bool = False) -> None:
        self.sends: list[tuple[str, str, str]] = []
        self.children: list[tuple[str, str, str]] = []
        self.fail = fail
        self.always_pending = always_pending
        self.on_send: Any = None

    def resolve_session(self, session_id: str) -> Any:
        if session_id != _SESSION_ID:
            return None
        return SimpleNamespace(id=_SESSION_ID, project_id=_PROJECT)

    async def send_prompt(self, session_id: str, text: str, request_id: str) -> str:
        if self.on_send is not None:
            await self.on_send()
        if self.fail:
            raise RuntimeError("delivery failed (injected)")
        self.sends.append((session_id, text, request_id))
        return f"msg_{len(self.sends)}"

    def create_child(self, parent_session_id: str, agent: str, child_session_id: str) -> None:
        self.children.append((parent_session_id, agent, child_session_id))

    def prompt_pending(self, session_id: str, message_id: str) -> bool:
        return self.always_pending


async def _make(db_path: Path, sessions: _Sessions, clock: _Clock, **overrides: Any) -> tuple[Any, Any]:
    module = _service_module()
    database = await open_database(db_path, mode="create").__aenter__()
    kwargs: dict[str, Any] = {
        "database": database,
        "sessions": sessions,
        "now_ms": clock,
        "late_after_s": _LATE_AFTER_S,
        "failed_retry_s": _FAILED_RETRY_S,
    }
    kwargs.update(overrides)
    return module.AutomationService(**kwargs), database


def _create_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "red-test",
        "project_id": _PROJECT,
        "directory": str(REPO_ROOT),
        "trigger": {"type": "every", "every": _MIN_INTERVAL_S},
        "action": {"type": "prompt", "prompt": "run the check", "to": {"session": _SESSION_ID}},
    }
    payload.update(overrides)
    return payload


async def _expect_invalid(service: Any, payload: dict[str, Any]) -> str:
    error = _service_module().InvalidAutomationError
    with pytest.raises(error) as caught:
        await service.create(payload)
    return str(getattr(caught.value, "reason", caught.value))


def test_create_validation_matrix(tmp_path: Path) -> None:
    """at must be a future instant; every >= min; cron must parse, stay
    minute-granular and actually run sometime."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> None:
        service, database = await _make(db_path, _Sessions(), _Clock())
        for trigger in (
            {"type": "at", "at": "2020-01-01T00:00:00Z"},
            {"type": "at", "at": "not a time"},
            {"type": "every", "every": 30.0},
            {"type": "cron", "cron": "* * * *"},
            {"type": "cron", "cron": "61 * * * *"},
            {"type": "cron", "cron": "0 0 31 2 *"},
        ):
            await _expect_invalid(service, _create_payload(trigger=trigger))
        await database.close()

    _execute(_scenario())


def test_create_relative_at_fixed_to_iso_and_future(tmp_path: Path) -> None:
    """'in 10 minutes' is stored as an absolute instant with the exact due."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> tuple[dict[str, Any], str]:
        service, database = await _make(db_path, _Sessions(), _Clock())
        info = await service.create(_create_payload(trigger={"type": "at", "at": "in 10 minutes"}))
        stored = _row(db_path, str(info["id"]))
        trigger = json.loads(str(stored["trigger"]))
        await database.close()
        return info, trigger["at"]

    info, stored_at = _execute(_scenario())
    assert str(info["id"]).startswith("aut_")
    assert info["status"] == "active"
    assert info["next_due_at"] == _BASE_MS + 600_000
    assert "in 10 minutes" not in stored_at


def test_create_target_rules(tmp_path: Path) -> None:
    """A prompt needs a reachable target; a subagent needs an owner session."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> None:
        service, database = await _make(db_path, _Sessions(), _Clock())
        no_target = _create_payload()
        del no_target["action"]["to"]
        await _expect_invalid(service, no_target)
        await _expect_invalid(
            service,
            _create_payload(action={"type": "prompt", "prompt": "x", "to": {"session": "ses_other"}}),
        )
        await _expect_invalid(
            service,
            _create_payload(action={"type": "prompt", "prompt": "x", "to": {"subagent": "worker"}}),
        )
        await database.close()

    _execute(_scenario())


def test_create_expires_before_first_run_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> None:
        service, database = await _make(db_path, _Sessions(), _Clock())
        await _expect_invalid(
            service, _create_payload(trigger={"type": "every", "every": 60.0}, expires="in 30 seconds")
        )
        await database.close()

    _execute(_scenario())


def test_at_runs_once_late_then_done(tmp_path: Path) -> None:
    """A slot missed while down runs once (late past late_after) and is done."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> dict[str, Any]:
        sessions = _Sessions()
        automation_id = _insert_row(
            db_path,
            id="aut_red_at",
            trigger={"type": "at", "at": "2026-10-25T00:00:00Z"},
            next_due_at=_BASE_MS - 120_000,
        )
        service, database = await _make(db_path, sessions, _Clock())
        assert await service.tick() == 1
        assert await service.tick() == 0  # done rows are not eligible
        row = _row(db_path, automation_id)
        await database.close()
        return row

    row = _execute(_scenario())
    assert row["status"] == "done"
    assert row["next_due_at"] is None
    assert row["last_outcome"] == "late"
    assert row["run_count"] == 1


def test_every_phase_keep_exact(tmp_path: Path) -> None:
    """Three missed 30s slots: one run, next_due = due + 4*every (no burst)."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    due = _BASE_MS - 3 * _EVERY_MS

    async def _scenario() -> dict[str, Any]:
        automation_id = _insert_row(db_path, id="aut_red_phase", next_due_at=due)
        service, database = await _make(db_path, _Sessions(), _Clock())
        await service.tick()
        row = _row(db_path, automation_id)
        await database.close()
        return row

    row = _execute(_scenario())
    assert row["run_count"] == 1
    assert row["next_due_at"] == due + 4 * _EVERY_MS
    assert row["last_outcome"] == "fired"


def test_cron_strict_next_slot(tmp_path: Path) -> None:
    """/5 cron: the next slot is the boundary STRICTLY after the run."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> dict[str, Any]:
        automation_id = _insert_row(
            db_path,
            id="aut_red_cron",
            trigger={"type": "cron", "cron": "*/5 * * * *"},
            next_due_at=_BASE_MS,
        )
        service, database = await _make(db_path, _Sessions(), _Clock(now_ms=_BASE_MS + 3 * 60_000))
        await service.tick()
        row = _row(db_path, automation_id)
        await database.close()
        return row

    row = _execute(_scenario())
    assert row["run_count"] == 1
    # run at 00:03:00Z -> strictly next /5 slot = 00:05:00Z
    assert row["next_due_at"] == _BASE_MS + 5 * 60_000


def test_cron_dst_fallback_safety(tmp_path: Path) -> None:
    """Berlin 2026-10-25 fall-back: during the repeated 02:30 the next slot is
    TOMORROW's 02:30 CET, never the already-past first occurrence."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    # 2026-10-25T01:30Z = 02:30 CET, the SECOND occurrence of that wall clock.
    run_ms = _BASE_MS + 90 * 60_000

    async def _scenario() -> dict[str, Any]:
        automation_id = _insert_row(
            db_path,
            id="aut_red_dst",
            trigger={"type": "cron", "cron": "30 2 * * *"},
            tz="Europe/Berlin",
            next_due_at=_BASE_MS,
        )
        service, database = await _make(db_path, _Sessions(), _Clock(now_ms=run_ms))
        await service.tick()
        row = _row(db_path, automation_id)
        await database.close()
        return row

    row = _execute(_scenario())
    assert row["run_count"] == 1
    # next = 2026-10-26T02:30 CET = 01:30Z, +24h from the run instant
    assert row["next_due_at"] == run_ms + 24 * 60 * 60_000


def test_tick_eligibility_and_order(tmp_path: Path) -> None:
    """Only active+due+retry-free rows run, earliest first."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> tuple[Any, dict[str, Any]]:
        sessions = _Sessions()
        _insert_row(db_path, id="aut_red_paused", status="paused", next_due_at=_BASE_MS - 60_000)
        _insert_row(db_path, id="aut_red_retry", next_due_at=_BASE_MS - 60_000, retry_at=_BASE_MS + 60_000)
        _insert_row(db_path, id="aut_red_future", next_due_at=_BASE_MS + 60_000)
        _insert_row(db_path, id="aut_red_early", next_due_at=_BASE_MS - 90_000)
        _insert_row(db_path, id="aut_red_late", next_due_at=_BASE_MS - 30_000)
        service, database = await _make(db_path, sessions, _Clock(), tick_concurrency=1)
        await service.tick()
        row_paused = _row(db_path, "aut_red_paused")
        await database.close()
        return sessions, row_paused

    sessions, row_paused = _execute(_scenario())
    assert len(sessions.sends) == 2  # earliest first, both delivered
    assert row_paused["last_outcome"] is None  # never touched


def test_lease_single_flight_across_services(tmp_path: Path) -> None:
    """A per-run lease claim: the second service over the same database does
    not double-fire one slot."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    _insert_row(db_path, id="aut_red_lease", next_due_at=_BASE_MS - 60_000)

    async def _scenario() -> int:
        sessions_a = _Sessions()
        sessions_b = _Sessions()
        service_a, database_a = await _make(db_path, sessions_a, _Clock())
        service_b, database_b = await _make(db_path, sessions_b, _Clock())
        await service_a.tick()
        await service_b.tick()
        await database_a.close()
        await database_b.close()
        return len(sessions_a.sends) + len(sessions_b.sends)

    assert _execute(_scenario()) == 1


def test_prompt_failure_backoff_keeps_run_count(tmp_path: Path) -> None:
    """A failed delivery: outcome failed, retry_at = now + failed_retry_s,
    run NOT counted, next_due KEPT; inside the window no new attempt."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    due = _BASE_MS - 60_000
    _insert_row(db_path, id="aut_red_backoff", next_due_at=due)

    async def _scenario() -> list[dict[str, Any]]:
        service, database = await _make(db_path, _Sessions(fail=True), _Clock())
        await service.tick()
        rows = [_row(db_path, "aut_red_backoff")]
        await service.tick()  # inside the backoff window
        rows.append(_row(db_path, "aut_red_backoff"))
        await database.close()
        return rows

    after_failure, during_window = _execute(_scenario())
    assert after_failure["last_outcome"] == "failed"
    assert after_failure["retry_at"] == _BASE_MS + int(_FAILED_RETRY_S * 1000)
    assert after_failure["run_count"] == 0
    assert after_failure["next_due_at"] == due
    assert during_window["run_count"] == 0


def test_invalid_target_pauses(tmp_path: Path) -> None:
    """A target that no longer resolves pauses with the reason, no retry."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> dict[str, Any]:
        automation_id = _insert_row(
            db_path,
            id="aut_red_target",
            next_due_at=_BASE_MS - 60_000,
            action=json.dumps({"type": "prompt", "prompt": "x", "to": {"session": "ses_gone"}}),
        )
        service, database = await _make(db_path, _Sessions(), _Clock())
        await service.tick()
        row = _row(db_path, automation_id)
        await database.close()
        return row

    row = _execute(_scenario())
    assert row["status"] == "paused"
    assert row["last_outcome"] == "failed"
    assert row["retry_at"] is None
    assert "ses_gone" in str(row["last_output"])


def test_coalesce_skips_and_advances(tmp_path: Path) -> None:
    """A still-pending previous prompt: outcome skipped, no send, no run
    counted, and the timer still advances."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    _insert_row(db_path, id="aut_red_coalesce", next_due_at=_BASE_MS - 60_000)

    async def _scenario() -> tuple[int, dict[str, Any]]:
        sessions = _Sessions(always_pending=True)
        clock = _Clock()
        service, database = await _make(db_path, sessions, clock)
        await service.tick()  # first run: fires (no last_message_id yet)
        clock.now_ms = _BASE_MS + _EVERY_MS + 1  # past the advanced slot
        await service.tick()  # previous prompt still pending -> skipped
        row = _row(db_path, "aut_red_coalesce")
        await database.close()
        return len(sessions.sends), row

    sends, row = _execute(_scenario())
    assert sends == 1
    assert row["run_count"] == 1
    assert row["last_outcome"] == "skipped"
    assert row["next_due_at"] == _BASE_MS + 2 * _EVERY_MS


def test_settle_never_resurrects_or_overwrites(tmp_path: Path) -> None:
    """Optimistic settle guards: a run completing after remove stays removed;
    a failure after a concurrent pause leaves the pause intact."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    _insert_row(db_path, id="aut_red_rm", next_due_at=_BASE_MS - 60_000)
    # due later in the first tick's clock so only the rm row runs first
    _insert_row(db_path, id="aut_red_pause", next_due_at=_BASE_MS + 30_000)

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        sessions = _Sessions()
        clock = _Clock()
        service, database = await _make(db_path, sessions, clock)

        async def _remove_mid_send() -> None:
            await service.remove("aut_red_rm")

        sessions.on_send = _remove_mid_send
        await service.tick()
        removed = _row(db_path, "aut_red_rm")

        async def _pause_mid_send() -> None:
            await service.pause("aut_red_pause")

        sessions.on_send = _pause_mid_send
        sessions.fail = True
        clock.now_ms = _BASE_MS + 30_001
        await service.tick()
        paused = _row(db_path, "aut_red_pause")
        await database.close()
        return removed, paused

    removed, paused = _execute(_scenario())
    assert removed == {}
    assert paused["status"] == "paused"
    assert paused["retry_at"] is None


def test_pause_resume_semantics(tmp_path: Path) -> None:
    """Pause idempotent; resume recomputes the slot (a missed at runs once,
    late), clears failure state and re-enables; a repeating trigger resumes
    at its next slot and runs then."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)
    _insert_row(
        db_path,
        id="aut_red_at",
        trigger={"type": "at", "at": "2026-10-25T00:00:00Z"},
        next_due_at=_BASE_MS - 120_000,
        retry_at=_BASE_MS + 60_000,
        last_outcome="failed",
        status="paused",
    )
    _insert_row(db_path, id="aut_red_every", status="paused", next_due_at=_BASE_MS - 90_000)

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        service, database = await _make(db_path, _Sessions(), _Clock())
        await service.pause("aut_red_at")  # already paused: idempotent
        resumed_at = await service.resume("aut_red_at")
        resumed_every = await service.resume("aut_red_every")
        await service.tick()  # the missed at fires late once
        row_at = _row(db_path, "aut_red_at")
        row_every = _row(db_path, "aut_red_every")
        await database.close()
        return resumed_at, row_at, dict(resumed_every, row=row_every)

    resumed_at, row_at, resumed_every = _execute(_scenario())
    assert resumed_at["status"] == "active"
    assert resumed_at["retry_at"] is None
    assert row_at["status"] == "done"
    assert row_at["last_outcome"] == "late"
    # every resumed at its next slot (BASE+30s), not yet due at BASE: idle
    assert resumed_every["status"] == "active"
    assert resumed_every["row"]["run_count"] == 0
    assert resumed_every["row"]["next_due_at"] == _BASE_MS + 30_000


def test_expired_never_runs(tmp_path: Path) -> None:
    """A slot that only comes up after expires_at (and past late_after):
    done/expired without a send."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> tuple[dict[str, Any], int]:
        sessions = _Sessions()
        automation_id = _insert_row(
            db_path,
            id="aut_red_expired",
            next_due_at=_BASE_MS - 120_000,
            expires_at=_BASE_MS - 60_000,
        )
        service, database = await _make(db_path, sessions, _Clock())
        await service.tick()
        row = _row(db_path, automation_id)
        await database.close()
        return row, len(sessions.sends)

    row, sends = _execute(_scenario())
    assert sends == 0
    assert row["status"] == "done"
    assert row["last_outcome"] == "expired"


def test_subagent_derived_child_adoption(tmp_path: Path) -> None:
    """A subagent target: child id derived from the run; a failed run retries
    the SAME child (adoption), the next run derives the next id."""
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
        sessions = _Sessions()
        clock = _Clock()
        service, database = await _make(db_path, sessions, clock)
        info = await service.create(
            _create_payload(
                session_id=_SESSION_ID,
                trigger={"type": "every", "every": _MIN_INTERVAL_S},
                action={"type": "prompt", "prompt": "delegate", "to": {"subagent": "worker"}},
            )
        )
        automation_id = str(info["id"])
        clock.now_ms = _BASE_MS + int(_MIN_INTERVAL_S * 1000)
        sessions.fail = True
        await service.tick()
        assert _row(db_path, automation_id)["run_count"] == 0
        clock.now_ms += int(_FAILED_RETRY_S * 1000) + 1
        sessions.fail = False
        await service.tick()
        after_retry = _row(db_path, automation_id)
        clock.now_ms = int(after_retry["next_due_at"]) + 1
        await service.tick()
        await database.close()
        return sessions.children, after_retry

    children, after_retry = _execute(_scenario())
    assert after_retry["run_count"] == 1  # the failed run was not counted
    first_child = children[0][2]
    assert first_child == f"ses_aut_{after_retry['id']}_1"
    # the retried run adopted the same child; the NEXT run derives a new id
    assert [child[2] for child in children[:2]] == [first_child, first_child]
    assert len({child[2] for child in children}) == 2
    assert children[0][0] == _SESSION_ID
    assert children[0][1] == "worker"


def test_lifecycle_remove_and_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "red.db"
    _migrate(db_path)

    async def _scenario() -> None:
        service, database = await _make(db_path, _Sessions(), _Clock())
        info = await service.create(_create_payload())
        listed = await service.list(project_id=_PROJECT)
        assert any(item["id"] == info["id"] for item in listed)
        await service.remove(str(info["id"]))
        assert _row(db_path, str(info["id"])) == {}
        module = _service_module()
        with pytest.raises(module.AutomationNotFoundError):
            await service.get("aut_missing")
        with pytest.raises(module.AutomationNotFoundError):
            await service.remove("aut_missing")
        await database.close()

    _execute(_scenario())
