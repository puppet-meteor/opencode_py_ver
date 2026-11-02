"""Integration tests for the real read-model projector (R1-S4).

Covers the T-C1-005 acceptance criteria over CORE-CONTRACTS §3.2/§3.3:
- create -> accepted -> sessions/messages rows with the exact projected
  content (status/snapshot_hash/applied_seq/message_id/seq/data);
- acceptance A: events/projection/applied_seq share one transaction -- a
  projector failure rolls everything back (zero residue) and a retry over a
  healthy projector restarts seq allocation;
- duplicate py.session.created projection (a second event id, and in-batch)
  and duplicate embedded message ids are StorageCorrupt without isolating
  the session (isolate=False: healthy pre-state, bad batch);
- py.prompt.accepted without a sessions row is StorageCorrupt (most
  conservative reading; progress-file open question);
- a projector that skips the applied_seq advance is caught by the store's
  A=H+n re-verification (projector advances, store re-verifies);
- commits and read-model queries make no external calls (socket/subprocess
  banned under monkeypatch);
- the state fold is a pure deterministic function (the R1-S1 contract
  fixtures as projector input) and the fold module is structurally IO-free.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import socket
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
    default_event_registry,
)
from opencode.session import (
    IllegalTransitionError,
    SessionProjector,
    SessionState,
    SessionStatus,
    apply,
)
from opencode.storage import (
    EventBatch,
    SessionStore,
    StorageCorruptError,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "contract" / "fixtures"
SNAPSHOT_HASH = "77c932c31585517c128479af02c40cd377e26d4d25ce57df197fc8509e14f802"

_CREATED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


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
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"


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
    session_id: str,
    event_id: str,
    *,
    run_id: str = "run_1",
    text: str = "hello",
    message_id: str = "msg_1",
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
            message=UserMessage(id=message_id, type="user", created_at=_CREATED_AT, metadata={}, text=text),
        ),
        metadata={"run_id": run_id},
    )


class FailingProjector(SessionProjector):
    """Real projector that fails on one event id (fault injection)."""

    def __init__(self, *, fail_on_event_id: str) -> None:
        self.fail_on_event_id = fail_on_event_id

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        if event.id == self.fail_on_event_id:
            raise RuntimeError(f"injected projector failure at {event.id}")
        await super().apply(connection, event)


class NoAdvanceProjector(SessionProjector):
    """Real projector that skips the applied_seq advance (fault injection)."""

    async def _advance_applied_seq(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        pass  # deliberately forget the watermark advance


async def test_projector_writes_session_and_message_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        created = _session_created_draft("ses_a", "evt_a1")
        prompt = _prompt_accepted_draft("ses_a", "evt_a2")
        committed = await store.commit("ses_a", EventBatch("batch_1", (created, prompt)), 0)
        assert [event.seq for event in committed] == [1, 2]

    session_row = _fetch_all(
        db_path,
        "SELECT status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
        "FROM sessions WHERE session_id = 'ses_a'",
    )[0]
    assert session_row[0] == "active"
    assert session_row[1] is None  # R1-S4 does not occupy the run slot (progress file)
    assert session_row[2] == '{"permission_rules":[],"schema_version":1}'
    assert session_row[3] == SNAPSHOT_HASH
    assert session_row[4] == 2  # A = H: the projector advanced per event
    assert session_row[5] is None

    message_rows = _fetch_all(db_path, "SELECT session_id, message_id, seq, data FROM messages")
    assert message_rows == [
        (
            "ses_a",
            "msg_1",
            2,  # message order = event seq (TS insertMessage L481-504)
            canonical_json(prompt.data.message.model_dump(mode="json")),
        )
    ]
    assert _fetch_scalar(db_path, "SELECT MAX(seq) FROM events WHERE session_id = 'ses_a'") == (2,)


async def test_projector_failure_rolls_back_and_retry_restarts_seq(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    batch = EventBatch(
        "batch_1",
        (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
    )

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=FailingProjector(fail_on_event_id="evt_a2"))
        with pytest.raises(RuntimeError, match="injected projector failure"):
            await store.commit("ses_a", batch, 0)

        # Acceptance A: full rollback -- the created projection (already
        # written when the failure hit) left zero residue together with the
        # event rows and the messages row.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages") == (0,)

        # Swap in the healthy real projector and retry the same batch: seq
        # allocation restarts at H+1 (nothing survived the rollback).
        healthy = SessionStore(database.engine, projector=SessionProjector())
        committed = await healthy.commit("ses_a", batch, 0)
        assert [event.seq for event in committed] == [1, 2]

    assert _fetch_all(db_path, "SELECT status, applied_seq FROM sessions WHERE session_id = 'ses_a'") == [("active", 2)]
    assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages") == (1,)


async def test_projector_duplicate_created_projection_is_storage_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await store.commit("ses_a", EventBatch("batch_1", (_session_created_draft("ses_a", "evt_a1"),)), 0)

        # A second py.session.created under a fresh event id: TS dies with
        # SessionAlreadyProjected; Python raises StorageCorrupt.
        with pytest.raises(StorageCorruptError) as error:
            await store.commit("ses_a", EventBatch("batch_2", (_session_created_draft("ses_a", "evt_a2"),)), 1)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.details["reason"] == "duplicate_session_projection"
        assert error.value.core.details["event_id"] == "evt_a2"

        # isolate=False: the bad batch is rejected and rolled back; the
        # healthy session is NOT quarantined and stays writable.
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (1,)
        assert _fetch_all(
            db_path, "SELECT status, applied_seq, diagnostic FROM sessions WHERE session_id = 'ses_a'"
        ) == [("active", 1, None)]
        committed = await store.commit("ses_a", EventBatch("batch_3", (_prompt_accepted_draft("ses_a", "evt_a3"),)), 1)
        assert [event.seq for event in committed] == [2]

        # In-batch double create is the same defect (the row insert conflicts).
        with pytest.raises(StorageCorruptError) as in_batch:
            await store.commit(
                "ses_b",
                EventBatch(
                    "batch_b",
                    (_session_created_draft("ses_b", "evt_b1"), _session_created_draft("ses_b", "evt_b2")),
                ),
                0,
            )
        assert in_batch.value.core.details["reason"] == "duplicate_session_projection"
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_b'") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions WHERE session_id = 'ses_b'") == (0,)


async def test_projector_duplicate_message_id_is_storage_corrupt(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await store.commit(
            "ses_a",
            EventBatch(
                "batch_1",
                (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
            ),
            0,
        )

        # Same embedded message id under a fresh event id: the read-model
        # unique(session_id, message_id) rejects the projection.
        with pytest.raises(StorageCorruptError) as error:
            await store.commit("ses_a", EventBatch("batch_2", (_prompt_accepted_draft("ses_a", "evt_a3"),)), 2)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.details["reason"] == "duplicate_message_id"

        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = 'ses_a'") == (2,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages WHERE session_id = 'ses_a'") == (1,)
        assert _fetch_all(db_path, "SELECT status, applied_seq FROM sessions WHERE session_id = 'ses_a'") == [
            ("active", 2)
        ]


async def test_projector_prompt_without_session_is_storage_corrupt(tmp_path: Path) -> None:
    """Most conservative reading (progress-file open question): a batch whose
    first event is not py.session.created is rejected as corruption of the
    would-be log, rolled back with zero residue, and does not quarantine
    (there is no session to quarantine; no minimal diagnostic row appears)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        with pytest.raises(StorageCorruptError) as error:
            await store.commit("ses_a", EventBatch("batch_1", (_prompt_accepted_draft("ses_a", "evt_a1"),)), 0)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.details["reason"] == "prompt_accepted_without_session"

        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages") == (0,)

        # The same session id is still creatable afterwards.
        committed = await store.commit("ses_a", EventBatch("batch_2", (_session_created_draft("ses_a", "evt_a2"),)), 0)
        assert [event.seq for event in committed] == [1]


async def test_projector_missing_advance_is_caught_by_store_recheck(tmp_path: Path) -> None:
    """Projector advances applied_seq, store re-verifies A=H+n (T-B-008 split):
    a projector that forgets the advance is rejected with the projection
    invariant reason and nothing is committed."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=NoAdvanceProjector())
        batch = EventBatch(
            "batch_1",
            (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
        )
        with pytest.raises(StorageCorruptError) as error:
            await store.commit("ses_a", batch, 0)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.details["reason"] == "applied_seq_not_advanced_after_projection"

        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages") == (0,)


async def test_commit_and_queries_make_no_external_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1 verification: commits, projection and read-model queries are
    local-only -- the whole pipeline passes with outbound sockets and
    subprocesses banned under monkeypatch.

    The socket class itself must stay intact: the Windows proactor event
    loop runs isinstance(conn, socket.socket) inside its self-pipe reader,
    so the outbound ban targets create_connection/getaddrinfo and the
    connect method instead of replacing the class (which kills the loop's
    wakeup pipe and deadlocks every await)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    def banned(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("external call attempted under the ban")

    monkeypatch.setattr(socket, "create_connection", banned)
    monkeypatch.setattr(socket, "getaddrinfo", banned)
    monkeypatch.setattr(socket.socket, "connect", banned)
    monkeypatch.setattr(subprocess, "Popen", banned)
    monkeypatch.setattr(subprocess, "run", banned)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", banned)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", banned)

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        committed = await store.commit(
            "ses_a",
            EventBatch(
                "batch_1",
                (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
            ),
            0,
        )
        assert [event.seq for event in committed] == [1, 2]

        # A read-model query over the same engine stays local too.
        async with database.engine.connect() as connection:
            rows = (
                await connection.execute(sa.text("SELECT message_id, seq FROM messages WHERE session_id = 'ses_a'"))
            ).fetchall()
        assert rows == [("msg_1", 2)]


def _fixture_event(name: str) -> DurableEvent[Any]:
    """Decode one R1-S1 contract fixture into a typed durable event."""
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return default_event_registry().decode_durable(raw)


def test_state_fold_is_deterministic_over_contract_fixtures() -> None:
    """Pure fold over the S1 fixtures: same input -> same output, R1 fields."""
    created = _fixture_event("events_session_created_valid.json")
    prompt = _fixture_event("events_prompt_accepted_valid.json")

    first = apply(apply(SessionState(), created), prompt)
    second = apply(apply(SessionState(), created), prompt)
    assert first == second  # determinism: no clock/randomness involvement

    assert first.session_id == "ses_0000000000000000000000000001"
    assert first.status is SessionStatus.ACTIVE
    assert first.applied_seq == 2
    assert first.working_dir == "D:/work/demo"
    assert first.config_snapshot == ConfigSnapshot.create()
    assert first.snapshot_hash == SNAPSHOT_HASH
    assert [message.text for message in first.messages] == ["hello, agent"]
    assert first.messages[0].id == "msg_0000000000000000000000000001"

    # The create fold alone lands the §6.1 state with no messages yet.
    only_created = apply(SessionState(), created)
    assert only_created.applied_seq == 1
    assert only_created.messages == ()


def test_state_fold_rejects_illegal_transitions() -> None:
    """Fail closed: ownership mismatch, double create, prompt without an
    active session, non-active statuses, non-R1 event types, and data/type
    mismatches all raise instead of skipping."""
    created = _fixture_event("events_session_created_valid.json")
    prompt = _fixture_event("events_prompt_accepted_valid.json")

    with pytest.raises(IllegalTransitionError):
        apply(SessionState(), prompt)  # no session yet
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), created)  # second create
    with pytest.raises(IllegalTransitionError):
        apply(SessionState(session_id="ses_0000000000000000000000000001", status=SessionStatus.CLOSED), prompt)
    with pytest.raises(IllegalTransitionError):
        apply(SessionState(session_id="ses_0000000000000000000000000002", status=SessionStatus.ACTIVE), prompt)

    # Ownership: the event belongs to another session than the folded state.
    foreign = prompt.model_copy(update={"session_id": "ses_0000000000000000000000000002"})
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), foreign)

    # Event types outside the R1 subset are not foldable.
    future = DurableEvent[PromptAcceptedData](
        id="evt_future",
        type="py.run.started",
        version=1,
        session_id="ses_0000000000000000000000000001",
        created_at=prompt.created_at,
        data=prompt.data,
        seq=3,
    )
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), future)

    # A data model that does not match the event type is rejected.
    mismatched = DurableEvent[PromptAcceptedData](
        id="evt_mismatch",
        type="py.session.created",
        version=1,
        session_id="ses_0000000000000000000000000001",
        created_at=prompt.created_at,
        data=prompt.data,
        seq=3,
    )
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), mismatched)


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


def test_state_module_is_structurally_pure() -> None:
    """§3.3: the fold module imports no IO/clock/randomness machinery and
    makes no clock/randomness calls."""
    import opencode.session.state as state_module

    for name in _imported_module_names(state_module):
        root = name.split(".")[0]
        assert root not in {"asyncio", "http", "os", "pathlib", "random", "socket", "subprocess", "time", "urllib"}, (
            f"{state_module.__name__} must not import {name}"
        )

    forbidden_calls = {"now", "today", "utcnow", "random", "randint", "open"}
    tree = ast.parse(inspect.getsource(state_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
            assert name not in forbidden_calls, f"the fold must not call {name}()"


def test_projector_delegates_transition_rules_to_the_fold() -> None:
    """The projector must not inline fold rules: it imports the shared fold."""
    import opencode.session.projector as projector_module

    names = _imported_module_names(projector_module)
    assert "opencode.session.state" in names
