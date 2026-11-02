"""Integration tests for SessionStore.commit and the §3.2.1 isolation entry (R1-S3).

Covers the T-C1-004 acceptance criteria over CORE-CONTRACTS §3.2/§3.2.1:
- normal path: A=H, consecutive per-session seq H+1..H+n, batch columns stored;
- idempotency: full-batch retry (stale expected_seq included) returns the
  original events with no re-projection / watermark advance; mixed
  committed+new ids, same-id-different-content and cross-session id reuse
  raise IdempotencyConflict;
- expected_seq != H on a healthy log raises Conflict;
- injected Protocol projector: calls recorded in order; a raising fake rolls
  the whole transaction back (zero event rows) and a retry succeeds cleanly;
- hand-corrupted logs (A != H, seq gap, duplicate seq, illegal seq, missing
  read-model row) raise StorageCorrupt (not Conflict) and route into §3.2.1
  isolation: status read_only_diagnostic + sanitized diagnostic, no business
  events, applied_seq preserved (minimal record when the row is missing);
  subsequent commits -- including committed-batch retries -- fail with
  SessionReadOnly BEFORE any idempotency replay;
- the store/txn modules never import a bus (notification is R1-S5);
- N-1: bare ``alembic upgrade head`` without ``-x db_path`` fails fast;
- N-2: open_database rejects an unknown open mode with ValueError.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.contracts import (
    ConfigSnapshot,
    CoreErrorCode,
    DurableEvent,
    EventDraft,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    canonical_json,
    compute_snapshot_hash,
)
from opencode.storage import (
    ConflictError,
    EventBatch,
    IdempotencyConflictError,
    SessionReadOnlyError,
    SessionStore,
    StorageCorruptError,
    StoreValidationError,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_CREATED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
_CREATED_AT_MS = int(_CREATED_AT.timestamp()) * 1000


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


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _session_created_draft(
    session_id: str, event_id: str, *, working_dir: str = "C:/work"
) -> EventDraft[SessionCreatedData]:
    snapshot = ConfigSnapshot.create()
    return EventDraft[SessionCreatedData](
        id=event_id,
        type="py.session.created",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=SessionCreatedData(
            working_dir=working_dir,
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ),
    )


def _prompt_accepted_draft(
    session_id: str, event_id: str, *, run_id: str = "run_1", text: str = "hello"
) -> EventDraft[PromptAcceptedData]:
    return EventDraft[PromptAcceptedData](
        id=event_id,
        type="py.prompt.accepted",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id="req_1",
            run_id=run_id,
            message=UserMessage(id="msg_1", type="user", created_at=_CREATED_AT, metadata={}, text=text),
        ),
        metadata={"run_id": run_id},
    )


class RecordingProjector:
    """Injected §3.2 projector fake: records calls and advances applied_seq minimally."""

    def __init__(self, *, fail_on_event_id: str | None = None) -> None:
        self.calls: list[DurableEvent[Any]] = []
        self.fail_on_event_id = fail_on_event_id

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        self.calls.append(event)
        if self.fail_on_event_id is not None and event.id == self.fail_on_event_id:
            raise RuntimeError(f"injected projector failure at {event.id}")
        await connection.execute(
            sa.text(
                "INSERT INTO sessions (session_id, status, applied_seq) VALUES (:sid, 'active', :applied) "
                "ON CONFLICT(session_id) DO UPDATE SET applied_seq = excluded.applied_seq"
            ),
            {"sid": event.session_id, "applied": event.seq},
        )


async def test_commit_normal_path_advances_watermark_and_consecutive_seq(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)

        first = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        committed_first = await store.commit("ses_a", first, 0)
        assert [event.seq for event in committed_first] == [1, 2]
        assert [event.id for event in committed_first] == ["evt_a1", "evt_a2"]
        assert isinstance(committed_first[0].data, SessionCreatedData)
        assert committed_first[0].data.working_dir == "C:/work"
        assert committed_first[1].metadata == {"run_id": "run_1"}

        second = EventBatch("batch_2", (_prompt_accepted_draft("ses_a", "evt_a3", text="again"),))
        committed_second = await store.commit("ses_a", second, 2)
        assert [event.seq for event in committed_second] == [3]

        # The projector ran inside each commit transaction, in batch order.
        assert [event.id for event in projector.calls] == ["evt_a1", "evt_a2", "evt_a3"]
        assert [event.seq for event in projector.calls] == [1, 2, 3]

    rows = _fetch_all(
        db_path,
        "SELECT seq, type, created, data, metadata, batch_id, batch_ordinal, dedupe_key "
        "FROM events WHERE session_id = 'ses_a' ORDER BY seq",
    )
    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[0][1] == "py.session.created"
    assert rows[0][2] == _CREATED_AT_MS
    assert rows[0][3] == canonical_json(committed_first[0].data.model_dump(mode="json"))
    assert rows[0][4] == "{}"
    assert rows[0][5] == "batch_1"
    assert rows[0][6] == 0
    assert rows[0][7] is None
    assert _fetch_all(db_path, "SELECT status, applied_seq FROM sessions WHERE session_id = 'ses_a'") == [("active", 3)]


async def test_commit_full_batch_retry_replays_without_reprojection(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        first = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        original = await store.commit("ses_a", first, 0)
        assert len(projector.calls) == 2

        # Stale expected_seq (H is now 2): the replay still returns the original events.
        stale_retry = await store.commit("ses_a", first, 0)
        assert stale_retry == original
        assert [event.seq for event in stale_retry] == [1, 2]
        assert stale_retry[1].data.message.text == "hello"

        # A retry with the correct expected_seq is equally idempotent.
        fresh_retry = await store.commit("ses_a", first, 2)
        assert fresh_retry == original

        # No re-projection, no new rows, no watermark advance.
        assert len(projector.calls) == 2
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (2,)
        assert _fetch_scalar(db_path, "SELECT applied_seq FROM sessions WHERE session_id = 'ses_a'") == (2,)


async def test_commit_idempotency_conflicts(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        first = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        await store.commit("ses_a", first, 0)

        # Mixed committed + new event ids.
        mixed = EventBatch(
            "batch_mixed",
            (_prompt_accepted_draft("ses_a", "evt_a2"), _prompt_accepted_draft("ses_a", "evt_new")),
        )
        with pytest.raises(IdempotencyConflictError) as mixed_error:
            await store.commit("ses_a", mixed, 2)
        assert mixed_error.value.core.code == CoreErrorCode.IDEMPOTENCY_CONFLICT

        # Same ids under the same batch id, but diverged content.
        diverged = EventBatch(
            "batch_1",
            (
                _session_created_draft("ses_a", "evt_a1", working_dir="C:/other"),
                _prompt_accepted_draft("ses_a", "evt_a2", text="changed"),
            ),
        )
        with pytest.raises(IdempotencyConflictError):
            await store.commit("ses_a", diverged, 2)

        # An id already committed in another session.
        foreign = EventBatch("batch_b", (_session_created_draft("ses_b", "evt_a1"),))
        with pytest.raises(IdempotencyConflictError) as foreign_error:
            await store.commit("ses_b", foreign, 0)
        assert foreign_error.value.core.code == CoreErrorCode.IDEMPOTENCY_CONFLICT
        assert foreign_error.value.core.details["event_id"] == "evt_a1"

        # No rejected batch persisted anything.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (2,)


async def test_commit_expected_seq_mismatch_raises_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        first = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        await store.commit("ses_a", first, 0)

        with pytest.raises(ConflictError) as error:
            await store.commit("ses_a", EventBatch("batch_2", (_prompt_accepted_draft("ses_a", "evt_a3"),)), 5)
        assert error.value.core.code == CoreErrorCode.CONFLICT
        assert error.value.core.details == {
            "session_id": "ses_a",
            "expected_seq": 5,
            "high_watermark": 2,
        }

        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (2,)
        assert _fetch_scalar(db_path, "SELECT applied_seq FROM sessions WHERE session_id = 'ses_a'") == (2,)
        assert len(projector.calls) == 2


async def test_commit_projector_failure_rolls_back_entirely(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector(fail_on_event_id="evt_a2")

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        batch = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        with pytest.raises(RuntimeError, match="injected projector failure"):
            await store.commit("ses_a", batch, 0)

        # Full rollback: zero event rows, no read-model row, no isolation state.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)
        assert [event.id for event in projector.calls] == ["evt_a1", "evt_a2"]

        # Retry with a healthy projector over the same engine: no partial state.
        recovered = await SessionStore(database.engine, projector=RecordingProjector()).commit("ses_a", batch, 0)
        assert [event.seq for event in recovered] == [1, 2]

    assert _fetch_scalar(db_path, "SELECT status, applied_seq FROM sessions WHERE session_id = 'ses_a'") == (
        "active",
        2,
    )


async def test_commit_applied_seq_mismatch_isolates_and_gates(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        first = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        await store.commit("ses_a", first, 0)

        # Hand-corrupt the read model: A != H (2).
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE sessions SET applied_seq = 1 WHERE session_id = 'ses_a'")

        second = EventBatch("batch_2", (_prompt_accepted_draft("ses_a", "evt_a3"),))
        with pytest.raises(StorageCorruptError) as corrupt:
            await store.commit("ses_a", second, 2)
        assert corrupt.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert corrupt.value.core.details["reason"] == "applied_seq_mismatch"

        # The business transaction rolled back: no new events.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (2,)

        # §3.2.1 isolation persisted: status + sanitized diagnostic, A preserved.
        status, applied, diagnostic = _fetch_all(
            db_path, "SELECT status, applied_seq, diagnostic FROM sessions WHERE session_id = 'ses_a'"
        )[0]
        assert status == "read_only_diagnostic"
        assert applied == 1
        assert json.loads(diagnostic) == {"reason": "applied_seq_mismatch", "applied_seq": 1}

        # Gate BEFORE idempotency: even the committed-batch retry is rejected.
        with pytest.raises(SessionReadOnlyError) as retry_error:
            await store.commit("ses_a", first, 2)
        assert retry_error.value.core.code == CoreErrorCode.SESSION_READ_ONLY

        # The in-memory gate holds on this writer, and the DB status gates a
        # fresh store instance (new TransactionWriter) too.
        with pytest.raises(SessionReadOnlyError):
            await store.commit("ses_a", second, 2)
        fresh = SessionStore(database.engine, projector=RecordingProjector())
        with pytest.raises(SessionReadOnlyError):
            await fresh.commit("ses_a", EventBatch("batch_3", (_prompt_accepted_draft("ses_a", "evt_a4"),)), 2)

        # The diagnostic write produced no business events and no seq/A advance.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (2,)
        assert _fetch_scalar(db_path, "SELECT applied_seq FROM sessions WHERE session_id = 'ses_a'") == (1,)


async def test_commit_seq_corruption_isolates_per_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        for session_id, suffix in (("ses_gap", "g"), ("ses_dup", "d"), ("ses_illegal", "i")):
            batch = EventBatch(
                f"batch_{suffix}",
                (
                    _session_created_draft(session_id, f"evt_{suffix}1"),
                    _prompt_accepted_draft(session_id, f"evt_{suffix}2"),
                ),
            )
            await store.commit(session_id, batch, 0)

        with sqlite3.connect(db_path) as connection:
            # Gap: {1, 4}.
            connection.execute("UPDATE events SET seq = 4 WHERE id = 'evt_g2'")
            # Duplicate seq 2 (needs the unique index out of the way: the
            # index is the schema-level guard, the store check is defense in
            # depth for pre-index or index-damaged databases).
            connection.execute("DROP INDEX events_session_seq_uq")
            connection.execute(
                "INSERT INTO events (id, session_id, seq, type, version, created, data, metadata, "
                "batch_id, batch_ordinal) VALUES ('evt_d3', 'ses_dup', 2, 'py.prompt.accepted', 1, 0, "
                "'{}', '{}', 'hand', 0)"
            )
            # Illegal seq: {0, 2}.
            connection.execute("UPDATE events SET seq = 0 WHERE id = 'evt_i1'")

        for session_id, reason, first_bad_seq in (
            ("ses_gap", "seq_gap", 2),
            ("ses_dup", "duplicate_seq", 2),
            ("ses_illegal", "illegal_seq", 0),
        ):
            batch = EventBatch(f"next_{session_id}", (_prompt_accepted_draft(session_id, f"evt_{session_id}_x"),))
            with pytest.raises(StorageCorruptError) as error:
                await store.commit(session_id, batch, 2)
            assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
            assert error.value.core.details["reason"] == reason
            assert error.value.core.details["first_bad_seq"] == first_bad_seq
            assert _fetch_all(db_path, "SELECT status FROM sessions WHERE session_id = ?", (session_id,)) == [
                ("read_only_diagnostic",)
            ]


async def test_commit_missing_read_model_isolates_and_gates(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=projector)
        first = EventBatch("batch_1", (_session_created_draft("ses_rm", "evt_rm1"),))
        await store.commit("ses_rm", first, 0)

        # Delete the read-model row; the event log survives (events has no FK).
        with sqlite3.connect(db_path) as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = 'ses_rm'")
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_rm'") == (1,)

        with pytest.raises(StorageCorruptError) as error:
            await store.commit("ses_rm", EventBatch("batch_2", (_prompt_accepted_draft("ses_rm", "evt_rm2"),)), 1)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.details["reason"] == "sessions_row_missing_with_log"

        # §3.2.1 minimal diagnostic record: empty config/run, A = 0.
        row = _fetch_all(
            db_path,
            "SELECT status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
            "FROM sessions WHERE session_id = 'ses_rm'",
        )[0]
        assert row[0] == "read_only_diagnostic"
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        assert row[4] == 0
        assert json.loads(row[5]) == {"reason": "sessions_row_missing_with_log", "applied_seq": 0}

        # Gate before idempotency: the committed-batch retry is rejected too.
        with pytest.raises(SessionReadOnlyError):
            await store.commit("ses_rm", first, 1)

        # The diagnostic write added no business events.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_rm'") == (1,)


async def test_commit_validates_caller_input(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=RecordingProjector())
        good = _session_created_draft("ses_v", "evt_v1")

        with pytest.raises(StoreValidationError):
            await store.commit("", EventBatch("batch", (good,)), 0)
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("", (good,)), 0)
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("batch", ()), 0)
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("batch", (good, good)), 0)
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("batch", (good,)), -1)
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("batch", (good,)), True)

        # Ownership: the draft belongs to another session.
        foreign = _session_created_draft("ses_other", "evt_v2")
        with pytest.raises(StoreValidationError) as ownership:
            await store.commit("ses_v", EventBatch("batch", (foreign,)), 0)
        assert ownership.value.core.code == CoreErrorCode.VALIDATION_ERROR
        assert ownership.value.core.details["event_id"] == "evt_v2"

        # Version: no durable definition registered for the type.
        unknown = EventDraft[SessionCreatedData](
            id="evt_v3",
            type="py.unknown.event",
            version=1,
            session_id="ses_v",
            created_at=_CREATED_AT,
            data=good.data,
        )
        with pytest.raises(StoreValidationError):
            await store.commit("ses_v", EventBatch("batch", (unknown,)), 0)

        # Nothing was persisted by any rejected commit.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)


def test_alembic_without_db_path_fails_fast() -> None:
    """R1-S2 review N-1: no sqlalchemy.url default; a bare run must fail fast."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "no database URL" in result.stderr
    # It failed before creating any database file in the CWD.
    assert not (REPO_ROOT / "opencode.db").exists()


def test_open_database_rejects_unknown_mode(tmp_path: Path) -> None:
    """R1-S2 review N-2: an unknown open mode is rejected with ValueError."""
    with pytest.raises(ValueError, match="unknown open mode"):
        open_database(tmp_path / "app.db", mode="banana")  # type: ignore[arg-type]


def _imported_module_names(module: ModuleType) -> list[str]:
    """All module names imported by ``module`` (imports only, not docstrings)."""
    tree = ast.parse(inspect.getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_store_and_txn_never_import_a_bus() -> None:
    """Notification is R1-S5: the store seam must be structurally bus-free."""
    import opencode.storage.store as store_module
    import opencode.storage.txn as txn_module

    for module in (store_module, txn_module):
        for name in _imported_module_names(module):
            assert "bus" not in name.lower(), f"{module.__name__} must not import {name}"
