"""Integration tests for migration 0001 and the storage engine open/close (R1-S2).

Covers the R1-S2 acceptance criteria (r1-slices.md §2):
- ``python -m alembic upgrade head`` builds the 0001 minimal schema from an
  empty database; re-running against an already-migrated database is a no-op;
- the CORE-CONTRACTS §3.2 column lists, unique constraints and foreign keys,
  including "no separate sequence table" and the events-log independence from
  read-model rows;
- WAL / foreign_keys / busy_timeout asserted via PRAGMA queries on the
  runtime async engine, plus the sync-migration + async-runtime combination
  on Windows paths (r1-slices.md §4 #6);
- the three open modes (readonly/create/readwrite) and the close finalizer.

Platform-specific writer-lock contention is NOT retested here: R0-B already
verified it (doc/evidence/r0/platform.json: a second writer conflicts before
any database access; a crash releases the lock).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from opencode.contracts import (
    ConfigSnapshot,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    canonical_json,
    compute_snapshot_hash,
)
from opencode.session import SessionProjector
from opencode.storage import (
    BUSY_TIMEOUT_MS,
    DatabaseNotFoundError,
    SessionStore,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

EVENTS_COLUMNS = (
    "id",
    "session_id",
    "seq",
    "type",
    "version",
    "created",
    "data",
    "metadata",
    "batch_id",
    "batch_ordinal",
    "dedupe_key",
)
SESSIONS_COLUMNS = (
    "session_id",
    "status",
    "active_run_id",
    "config_snapshot",
    "snapshot_hash",
    "applied_seq",
    "diagnostic",
)
MESSAGES_COLUMNS = ("session_id", "message_id", "seq", "data")
TOOL_EXECUTIONS_COLUMNS = (
    "session_id",
    "execution_id",
    "run_id",
    "step_id",
    "message_id",
    "tool_call_id",
    "input_hash",
    "state",
)
PERMISSIONS_COLUMNS = (
    "session_id",
    "permission_id",
    "decision_id",
    "execution_id",
    "action",
    "resources",
    "input_hash",
    "scope",
    "expires_at",
    "state",
)
RUNS_COLUMNS = ("run_id", "session_id", "request_id", "status", "error")

_SESSIONS_CASCADE = [("sessions", "session_id", "session_id", "CASCADE")]


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout: {result.stdout}\nstderr: {result.stderr}"


def _fetch_all(db_path: Path, statement: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement).fetchall()


def _table_names(db_path: Path) -> set[str]:
    rows = _fetch_all(db_path, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {str(row[0]) for row in rows}


def _column_names(db_path: Path, table: str) -> set[str]:
    return {str(row[1]) for row in _fetch_all(db_path, f"PRAGMA table_info({table})")}


def _column_info(db_path: Path, table: str, column: str) -> tuple[int, int]:
    """Return the (notnull, pk) flags for one column."""
    for row in _fetch_all(db_path, f"PRAGMA table_info({table})"):
        if row[1] == column:
            return int(row[3]), int(row[5])
    raise AssertionError(f"column {table}.{column} does not exist")


def _unique_index_names(db_path: Path, table: str) -> set[str]:
    return {str(row[1]) for row in _fetch_all(db_path, f"PRAGMA index_list({table})") if row[2]}


def _index_columns(db_path: Path, index: str) -> list[str]:
    return [str(row[2]) for row in _fetch_all(db_path, f"PRAGMA index_info({index})")]


def _foreign_keys(db_path: Path, table: str) -> list[tuple[str, str, str, str]]:
    """Return (referenced_table, from_column, to_column, on_delete) per FK."""
    return [
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
        for row in _fetch_all(db_path, f"PRAGMA foreign_key_list({table})")
    ]


def _schema_fingerprint(db_path: Path) -> list[tuple[Any, ...]]:
    return sorted(_fetch_all(db_path, "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def _alembic_version(db_path: Path) -> str:
    rows = _fetch_all(db_path, "SELECT version_num FROM alembic_version")
    assert len(rows) == 1
    return str(rows[0][0])


def test_upgrade_head_builds_0001_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    assert _alembic_version(db_path) == "0002"
    # No separate sequence table (CORE-CONTRACTS §3.2 Python difference);
    # 0002 adds the fifth read model (runs, §3.2 L183-188).
    assert _table_names(db_path) == {
        "alembic_version",
        "events",
        "sessions",
        "messages",
        "tool_executions",
        "permissions",
        "runs",
    }
    assert _column_names(db_path, "events") == set(EVENTS_COLUMNS)
    assert _column_names(db_path, "sessions") == set(SESSIONS_COLUMNS)
    assert _column_names(db_path, "messages") == set(MESSAGES_COLUMNS)
    assert _column_names(db_path, "tool_executions") == set(TOOL_EXECUTIONS_COLUMNS)
    assert _column_names(db_path, "permissions") == set(PERMISSIONS_COLUMNS)
    assert _column_names(db_path, "runs") == set(RUNS_COLUMNS)

    # Key nullability/pk flags: events.id PK, dedupe_key optional; the
    # sessions minimal diagnostic record (§3.2.1) needs nullable
    # config/active_run_id and NOT NULL applied_seq.
    assert _column_info(db_path, "events", "id") == (1, 1)
    assert _column_info(db_path, "events", "dedupe_key") == (0, 0)
    assert _column_info(db_path, "events", "seq") == (1, 0)
    assert _column_info(db_path, "sessions", "session_id") == (1, 1)
    assert _column_info(db_path, "sessions", "status") == (1, 0)
    assert _column_info(db_path, "sessions", "applied_seq") == (1, 0)
    assert _column_info(db_path, "sessions", "active_run_id") == (0, 0)
    assert _column_info(db_path, "sessions", "config_snapshot") == (0, 0)
    assert _column_info(db_path, "sessions", "snapshot_hash") == (0, 0)
    assert _column_info(db_path, "sessions", "diagnostic") == (0, 0)

    # Unique constraints (§3.2): events x3, messages, tool_executions x2.
    unique_indexes = _unique_index_names(db_path, "events")
    assert "events_session_seq_uq" in unique_indexes
    assert _index_columns(db_path, "events_session_seq_uq") == ["session_id", "seq"]
    assert "events_session_batch_uq" in unique_indexes
    assert _index_columns(db_path, "events_session_batch_uq") == ["session_id", "batch_id", "batch_ordinal"]
    assert "events_session_dedupe_uq" in unique_indexes
    assert _index_columns(db_path, "events_session_dedupe_uq") == ["session_id", "dedupe_key"]
    assert "messages_session_message_uq" in _unique_index_names(db_path, "messages")
    assert _index_columns(db_path, "messages_session_message_uq") == ["session_id", "message_id"]
    tool_unique = _unique_index_names(db_path, "tool_executions")
    assert "tool_executions_session_execution_uq" in tool_unique
    assert _index_columns(db_path, "tool_executions_session_execution_uq") == ["session_id", "execution_id"]
    assert "tool_executions_session_step_tool_call_uq" in tool_unique
    assert _index_columns(db_path, "tool_executions_session_step_tool_call_uq") == [
        "session_id",
        "step_id",
        "tool_call_id",
    ]

    # 0002 runs read model (§3.2 L183-188): run_id PK, nullable error JSON,
    # unique(session_id, request_id), FK cascade from sessions.
    assert _column_info(db_path, "runs", "run_id") == (1, 1)
    assert _column_info(db_path, "runs", "session_id") == (1, 0)
    assert _column_info(db_path, "runs", "request_id") == (1, 0)
    assert _column_info(db_path, "runs", "status") == (1, 0)
    assert _column_info(db_path, "runs", "error") == (0, 0)
    assert "runs_session_request_uq" in _unique_index_names(db_path, "runs")
    assert _index_columns(db_path, "runs_session_request_uq") == ["session_id", "request_id"]
    assert _foreign_keys(db_path, "runs") == _SESSIONS_CASCADE

    # FKs: the events log carries none (survives read-model deletion, §3.3
    # acceptance C); read-model tables cascade from sessions.
    assert _foreign_keys(db_path, "events") == []
    assert _foreign_keys(db_path, "messages") == _SESSIONS_CASCADE
    assert _foreign_keys(db_path, "tool_executions") == _SESSIONS_CASCADE
    assert _foreign_keys(db_path, "permissions") == _SESSIONS_CASCADE


def test_upgrade_head_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fingerprint = _schema_fingerprint(db_path)
    assert _alembic_version(db_path) == "0002"

    # Already at head: the second upgrade is a no-op.
    _require_success(_upgrade_head(db_path))
    assert _schema_fingerprint(db_path) == fingerprint
    assert _alembic_version(db_path) == "0002"


async def test_runtime_engine_pragmas_and_wal_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        async with database.engine.connect() as connection:
            journal_mode = (await connection.execute(sa.text("PRAGMA journal_mode"))).scalar()
            foreign_keys = (await connection.execute(sa.text("PRAGMA foreign_keys"))).scalar()
            busy_timeout = (await connection.execute(sa.text("PRAGMA busy_timeout"))).scalar()
            assert journal_mode == "wal"
            assert foreign_keys == 1
            assert busy_timeout == BUSY_TIMEOUT_MS

            # Async runtime writes on the sync-migrated schema (§4 #6).
            await connection.execute(
                sa.text(
                    "INSERT INTO events "
                    "(id, session_id, seq, type, version, created, data, metadata, batch_id, batch_ordinal) "
                    "VALUES ('evt_probe_1', 'ses_probe', 1, 'py.session.created', 1, 0, '{}', '{}', 'batch_probe', 0)"
                )
            )
            await connection.commit()
            count = (await connection.execute(sa.text("SELECT count(*) FROM events"))).scalar()
            assert count == 1

            # foreign_keys is enforced, not merely declared (R0-B style backstop).
            with pytest.raises(IntegrityError):
                await connection.execute(
                    sa.text(
                        "INSERT INTO messages (session_id, message_id, seq, data) "
                        "VALUES ('ses_missing', 'msg_probe', 1, '{}')"
                    )
                )
            await connection.rollback()

    # WAL persists in the database file after the async engine disposed.
    assert _fetch_all(db_path, "PRAGMA journal_mode") == [("wal",)]
    # Writer lock was released on close; a new writer can open (R0-B
    # contention itself is not retested).
    async with open_database(db_path, mode="readwrite"):
        pass


async def test_open_modes_and_close_finalizer(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "app.db"
    with pytest.raises(DatabaseNotFoundError):
        open_database(missing, mode="readonly")
    with pytest.raises(DatabaseNotFoundError):
        open_database(missing, mode="readwrite")

    # create: makes the data directory, the database file and the writer lock.
    created = tmp_path / "fresh" / "app.db"
    database = open_database(created, mode="create")
    async with database:
        async with database.engine.connect() as connection:
            assert (await connection.execute(sa.text("PRAGMA journal_mode"))).scalar() == "wal"
    assert created.exists()
    assert (created.parent / "writer.lock").exists()
    assert database.closed
    await database.close()  # close is idempotent (TS close finalizer semantics)

    # readwrite: opens the existing file with the same PRAGMA guarantees.
    async with open_database(created, mode="readwrite") as rw:
        async with rw.engine.connect() as connection:
            assert (await connection.execute(sa.text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await connection.execute(sa.text("PRAGMA busy_timeout"))).scalar() == BUSY_TIMEOUT_MS

    # readonly: opens read-only over the existing (WAL) file; writes rejected.
    async with open_database(created, mode="readonly") as ro:
        assert (tmp_path / "fresh" / "writer.lock").exists()
        async with ro.engine.connect() as connection:
            assert (await connection.execute(sa.text("PRAGMA journal_mode"))).scalar() == "wal"
            assert (await connection.execute(sa.text("PRAGMA foreign_keys"))).scalar() == 1
            with pytest.raises(OperationalError):
                await connection.execute(sa.text("CREATE TABLE probe (id TEXT)"))
                await connection.commit()


# -- 0002: the runs read model (R2-S2; CORE-CONTRACTS §3.2 L183-188) ------------


def _upgrade_to(db_path: Path, revision: str) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI up to one revision (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", revision],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


_LEGACY_SESSION = "ses_legacy_0000000000000000001"
_LEGACY_CREATED_AT = datetime(2026, 11, 2, 8, 0, 1, tzinfo=UTC)


def _seed_legacy_r1_rows(db_path: Path) -> None:
    """Seed an R1-era 0001 database: created + three accepted prompts.

    R1-era rows exactly as the 0001-era commit path wrote them (three
    accepted runs per session is the R1 baseline, plan §0.2); no runs table
    exists at 0001.
    """
    snapshot = ConfigSnapshot.create()
    created_data = canonical_json(
        SessionCreatedData(
            working_dir="D:/work/demo",
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ).model_dump(mode="json")
    )
    created_ms = int(_LEGACY_CREATED_AT.timestamp() * 1000)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, batch_id, "
            "batch_ordinal) VALUES ('evt_legacy_c', ?, 1, 'py.session.created', 1, ?, ?, '{}', "
            "'batch_legacy_c', 0)",
            (_LEGACY_SESSION, created_ms, created_data),
        )
        connection.execute(
            "INSERT INTO sessions (session_id, status, active_run_id, config_snapshot, snapshot_hash, "
            "applied_seq) VALUES (?, 'active', 'run_3', ?, ?, 4)",
            (
                _LEGACY_SESSION,
                canonical_json(snapshot.model_dump(mode="json")),
                compute_snapshot_hash(snapshot),
            ),
        )
        for index in range(3):
            run_id = f"run_{index + 1}"
            request_id = f"req_{index + 1}"
            message = UserMessage(
                id=f"msg_{index + 1}",
                type="user",
                created_at=_LEGACY_CREATED_AT,
                metadata={},
                text=f"text-{index}",
            )
            data = canonical_json(
                PromptAcceptedData(
                    request_id=request_id,
                    run_id=run_id,
                    message=message,
                ).model_dump(mode="json")
            )
            metadata = canonical_json({"run_id": run_id, "request_id": request_id})
            connection.execute(
                "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
                "batch_id, batch_ordinal) VALUES (?, ?, ?, 'py.prompt.accepted', 1, ?, ?, ?, ?, 0)",
                (
                    f"evt_legacy_p{index}",
                    _LEGACY_SESSION,
                    index + 2,
                    created_ms,
                    data,
                    metadata,
                    f"batch_legacy_p{index}",
                ),
            )
            connection.execute(
                "INSERT INTO messages (session_id, message_id, seq, data) VALUES (?, ?, ?, ?)",
                (_LEGACY_SESSION, message.id, index + 2, canonical_json(message.model_dump(mode="json"))),
            )


def _runs_rows(db_path: Path) -> list[tuple[Any, ...]]:
    return _fetch_all(db_path, "SELECT run_id, session_id, request_id, status, error FROM runs ORDER BY run_id")


def test_upgrade_0001_to_0002_preserves_legacy_data(tmp_path: Path) -> None:
    """§3.3 upgrade discipline: the 0001->0002 step is additive -- the old
    data survives verbatim and the new table starts empty (the backfill runs
    through the rebuild before runs queries are served, §3.2 L187)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_to(db_path, "0001"))
    assert _alembic_version(db_path) == "0001"
    assert "runs" not in _table_names(db_path)
    _seed_legacy_r1_rows(db_path)

    _require_success(_upgrade_head(db_path))
    assert _alembic_version(db_path) == "0002"
    assert _column_names(db_path, "runs") == set(RUNS_COLUMNS)
    # Old-data verification: events/sessions/messages rows are untouched.
    assert _fetch_all(db_path, "SELECT COUNT(*) FROM events") == [(4,)]
    assert _fetch_all(
        db_path,
        f"SELECT status, active_run_id, applied_seq, diagnostic FROM sessions WHERE session_id = '{_LEGACY_SESSION}'",
    ) == [("active", "run_3", 4, None)]
    assert _fetch_all(db_path, "SELECT message_id, seq FROM messages ORDER BY seq") == [
        ("msg_1", 2),
        ("msg_2", 3),
        ("msg_3", 4),
    ]
    # No runs rows yet: the migration creates the model, the rebuild backfills.
    assert _runs_rows(db_path) == []


async def test_0002_backfill_via_rebuild_keeps_every_accepted_run(tmp_path: Path) -> None:
    """§3.2 L187: the backfill projects every accepted run (R1 multi-accepted
    logs keep all accepted rows), adds no terminal gate, and leaves the last
    active_run_id and the messages untouched."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_to(db_path, "0001"))
    _seed_legacy_r1_rows(db_path)
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="readwrite") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await store.rebuild(_LEGACY_SESSION)

    assert _runs_rows(db_path) == [
        ("run_1", _LEGACY_SESSION, "req_1", "accepted", None),
        ("run_2", _LEGACY_SESSION, "req_2", "accepted", None),
        ("run_3", _LEGACY_SESSION, "req_3", "accepted", None),
    ]
    # The last accepted run keeps the active slot; no terminal statuses appear.
    assert _fetch_all(
        db_path,
        f"SELECT status, active_run_id, applied_seq FROM sessions WHERE session_id = '{_LEGACY_SESSION}'",
    ) == [("active", "run_3", 4)]
    assert _fetch_all(db_path, "SELECT message_id, seq FROM messages ORDER BY seq") == [
        ("msg_1", 2),
        ("msg_2", 3),
        ("msg_3", 4),
    ]
