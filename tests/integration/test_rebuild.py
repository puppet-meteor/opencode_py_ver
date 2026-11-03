"""Integration tests for the R1-S8 rebuild + read-only-diagnostic wiring.

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos/service
over CORE-CONTRACTS §3.2.1/§3.3 and the r1-slices R1-S8 acceptance list:

- acceptance C, second half: delete the read model (messages + sessions rows)
  -> rebuild -> identical messages and identical sessions fields
  (status/active_run_id/config_snapshot/snapshot_hash/applied_seq); rebuild is
  idempotent (a second rebuild changes nothing);
- failure rollback: an unknown event version, a deleted middle event (seq
  gap) and undecodable stored data each leave the original projection
  untouched (the clearing itself rolled back), isolate the session
  (read_only_diagnostic + the sanitized diagnostic: reason/first_bad_seq/
  applied_seq) and raise StorageCorrupt; writes then answer SessionReadOnly;
- a tampered projection row (bad messages JSON) fails closed on reads before
  the rebuild and is REPAIRED by it (the log is healthy -- no isolation);
- §3.2.1 semantics: the diagnostic mark survives a successful rebuild
  (status stays read_only_diagnostic; prompt -- same-request_id retries
  included -- keeps answering SessionReadOnly; healthy reads work), rebuild
  appends no business events and never notifies (live-subscription timeout
  with a positive control), a damaged first event over a deleted read model
  produces the §3.2.1 L193 minimal diagnostic record (applied_seq=0, empty
  config/active_run_id), and projection lag (A<H) is not corruption on the
  rebuild path (applied_seq := H);
- a snapshot-hash mismatch on the stored py.session.created isolates (§6.2
  "已存不符为 StorageCorrupt 并隔离"; §3.2.1 L191 names the rebuild path);
- the service face: never-created -> NotFound, malformed session_id ->
  ValidationError, the service-closed gate -> SessionClosed (open point E),
  and the B-review nit-2 microtest: the same request_id in two different
  sessions succeeds twice with different RunRefs (the dedupe key is
  session-scoped: unique(session_id, dedupe_key)).

Corrupted rows are produced by direct SQL (the S6/S7 pattern). The seq-gap
case needs a 3-event log (created + two accepted prompts); the second prompt
is committed through the real store directly because the §7 busy gate would
reject it -- the S3/S4-tested legal way to produce such logs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opencode.bus import VolatileBus
from opencode.contracts import PY_PROMPT_ACCEPTED, CoreErrorCode, EventDraft, PromptAcceptedData, UserMessage
from opencode.session import SessionBusyError, SessionProjector, SessionService, SessionStatus
from opencode.storage import (
    EventBatch,
    MessagePage,
    NotFoundError,
    ReadRepositories,
    SessionClosedError,
    SessionReadOnlyError,
    SessionStore,
    StorageCorruptError,
    StoreValidationError,
    open_database,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT: dict[str, Any] = {"schema_version": 1, "permission_rules": []}
_CREATED_AT = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

#: The full sessions read-model face the rebuild must restore exactly.
_SESSION_ROW = (
    "SELECT status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
    "FROM sessions WHERE session_id = ?"
)
_MESSAGE_ROWS = "SELECT message_id, seq, data FROM messages WHERE session_id = ?"


def _user_texts(page: MessagePage) -> list[str]:
    """User-text projection over a page; every item must be a UserMessage
    (T-C1-023b #95: static narrowing over the union face -- runtime-true pin)."""
    texts: list[str] = []
    for message in page.items:
        assert isinstance(message, UserMessage)
        texts.append(message.text)
    return texts


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


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _execute(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(statement, params)


def _event_count(db_path: Path, session_id: str) -> int:
    row = _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,))
    return int(row[0]) if row is not None else 0


def _session_row(db_path: Path, session_id: str) -> tuple[Any, ...]:
    row = _fetch_scalar(db_path, _SESSION_ROW, (session_id,))
    assert row is not None, f"sessions row for {session_id} missing"
    return row


def _diagnostic_payload(db_path: Path, session_id: str) -> dict[str, Any]:
    """The persisted sanitized diagnostic (§3.2.1) as a parsed object."""
    row = _fetch_scalar(db_path, "SELECT diagnostic FROM sessions WHERE session_id = ?", (session_id,))
    assert row is not None and row[0] is not None, f"diagnostic for {session_id} missing"
    payload = json.loads(row[0])
    assert isinstance(payload, dict)
    return payload


@asynccontextmanager
async def _service(db_path: Path, *, bus: VolatileBus | None = None) -> AsyncIterator[SessionService]:
    """One service over its own engine on an already-migrated db_path."""
    async with open_database(db_path, mode="create") as database:
        yield SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=bus if bus is not None else VolatileBus(),
            working_dir="C:/work",
        )


async def _create_prompted(
    service: SessionService, *, text: str = "hello", request_id: str = "req_1"
) -> tuple[str, Any]:
    """create + prompt; returns (session_id, run_ref)."""
    session = await service.create(_SNAPSHOT)
    run_ref = await service.prompt(session.session_id, text, request_id)
    return session.session_id, run_ref


# -- acceptance C, second half: rebuild the deleted read model ----------------


async def test_rebuild_restores_deleted_read_model_identically(tmp_path: Path) -> None:
    """Delete the read model (messages + sessions rows) -> rebuild -> the
    messages and every sessions field come back identical
    (status/active_run_id/config_snapshot/snapshot_hash/applied_seq), and a
    second rebuild changes nothing: the replay is a deterministic function of
    the log (idempotence)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service, text="hello, agent", request_id="req_1")
        original_messages = (await service.messages(session_id)).items
        original_row = _session_row(db_path, session_id)
        assert original_row[0] == "active"

        # Delete the read model; the surviving log makes reads fail closed
        # (a missing projection is never faked as empty history, §3.2.1).
        _execute(db_path, "DELETE FROM messages WHERE session_id = ?", (session_id,))
        _execute(db_path, "DELETE FROM sessions WHERE session_id = ?", (session_id,))
        with pytest.raises(StorageCorruptError) as missing:
            await service.messages(session_id)
        assert missing.value.core.details["reason"] == "sessions_row_missing_with_log"

        # Rebuild restores the read model from the log alone (acceptance C).
        await service.rebuild(session_id)
        assert (await service.messages(session_id)).items == original_messages
        assert _session_row(db_path, session_id) == original_row

        # Idempotent: replaying the same log again changes nothing.
        await service.rebuild(session_id)
        assert (await service.messages(session_id)).items == original_messages
        assert _session_row(db_path, session_id) == original_row


# -- failure rollback: original projection preserved, then isolation ---------


async def test_rebuild_unknown_version_rolls_back_clearing_and_isolates(tmp_path: Path) -> None:
    """A stored event version the registry does not know fails the replay:
    StorageCorrupt with the sanitized details, the original projection
    untouched (the DELETEs rolled back with everything else), the session
    isolated read_only_diagnostic, and every later write -- same-request_id
    retries included -- answered with SessionReadOnly (§3.2.1 L195)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_row = _session_row(db_path, session_id)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))
        event_id_row = _fetch_scalar(
            db_path, "SELECT id FROM events WHERE session_id = ? AND type = 'py.prompt.accepted'", (session_id,)
        )
        assert event_id_row is not None
        assert _event_count(db_path, session_id) == 2

        _execute(db_path, "UPDATE events SET version = 99 WHERE session_id = ? AND seq = 2", (session_id,))

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert error.value.core.retryable is False
        details = error.value.core.details
        assert details["reason"] == "stored_event_decode_failed"
        assert details["first_bad_seq"] == 2
        assert details["first_bad_event_id"] == event_id_row[0]
        assert details["applied_seq"] == 2  # A at discovery time

        # The whole transaction rolled back: the original projection survived
        # (the clearing included) except for the administrative mark.
        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert after[1:5] == original_row[1:5]  # active_run_id..applied_seq untouched
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages
        assert _diagnostic_payload(db_path, session_id) == {
            "reason": "stored_event_decode_failed",
            "first_bad_seq": 2,
            "first_bad_event_id": event_id_row[0],
            "applied_seq": 2,
        }

        # §3.2.1: reads stay available; writes (retries included) are refused.
        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC
        with pytest.raises(SessionReadOnlyError) as retry:
            await service.prompt(session_id, "hello", "req_1")  # same request_id, same text
        assert retry.value.core.code == CoreErrorCode.SESSION_READ_ONLY
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "another", "req_2")
        assert _event_count(db_path, session_id) == 2  # no business events appended


async def test_rebuild_seq_gap_rolls_back_and_isolates(tmp_path: Path) -> None:
    """A deleted middle event (seq gap) fails the replay the same way: the
    first missing seq locates the gap, the original projection survives the
    rollback, and the session is isolated. The 3-event log (created + two
    accepted prompts) is committed through the real store directly -- the §7
    busy gate would reject the second prompt, and direct store commits are
    the S3/S4-tested legal way to produce such logs."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        repos = ReadRepositories(database.engine)
        service = SessionService(store=store, repos=repos, bus=VolatileBus(), working_dir="C:/work")
        session_id = (await service.create(_SNAPSHOT)).session_id
        await service.prompt(session_id, "first", "req_1")
        draft = EventDraft[PromptAcceptedData](
            id="evt_direct_3",
            type=PY_PROMPT_ACCEPTED,
            version=1,
            session_id=session_id,
            created_at=_CREATED_AT,
            data=PromptAcceptedData(
                request_id="req_2",
                run_id="run_direct",
                message=UserMessage(id="msg_direct", type="user", created_at=_CREATED_AT, metadata={}, text="second"),
            ),
            metadata={"run_id": "run_direct"},
        )
        await store.commit(session_id, EventBatch("batch_direct", (draft,)), await repos.log_high(session_id))

        original_row = _session_row(db_path, session_id)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))
        assert len(original_messages) == 2
        assert _event_count(db_path, session_id) == 3

        _execute(db_path, "DELETE FROM events WHERE session_id = ? AND seq = 2", (session_id,))

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "seq_gap"
        assert details["first_bad_seq"] == 2  # the first missing seq
        assert details["applied_seq"] == 3  # A at discovery time

        # Rollback preserved the projection; isolation flipped the status only.
        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert after[1:5] == original_row[1:5]
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages
        assert _diagnostic_payload(db_path, session_id)["first_bad_seq"] == 2
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "third", "req_3")


async def test_rebuild_undecodable_stored_data_rolls_back_and_isolates(tmp_path: Path) -> None:
    """Invalid JSON in a stored events row fails the replay decode (§3.2.1
    "无效 JSON"): same StorageCorrupt + rollback + isolation path as the
    unknown version, with the offending row located."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_row = _session_row(db_path, session_id)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))

        _execute(db_path, "UPDATE events SET data = '{\"broken\": ' WHERE session_id = ? AND seq = 2", (session_id,))

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "stored_event_decode_failed"
        assert details["first_bad_seq"] == 2
        assert details["applied_seq"] == 2

        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert after[1:5] == original_row[1:5]
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages


# -- damaged projection vs damaged log: rebuild repairs, never fakes ---------


async def test_rebuild_repairs_tampered_projection_row(tmp_path: Path) -> None:
    """A tampered messages row fails closed on reads before the rebuild (no
    crash, StorageCorrupt locating the row); the rebuild itself SUCCEEDS --
    the healthy log replaces the damaged projection row -- and the session
    stays active (no isolation: the log, not the projection, decides)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service, text="healthy")
        original_messages = (await service.messages(session_id)).items
        original_row = _session_row(db_path, session_id)

        _execute(db_path, "UPDATE messages SET data = '{\"broken\"' WHERE session_id = ?", (session_id,))
        with pytest.raises(StorageCorruptError) as damaged:
            await service.messages(session_id)
        assert damaged.value.core.details["reason"] == "message_row_decode_failed"

        # Rebuild replaces the read model from the healthy log: no isolation.
        await service.rebuild(session_id)
        assert (await service.messages(session_id)).items == original_messages
        assert _session_row(db_path, session_id) == original_row
        assert _event_count(db_path, session_id) == 2

        # Writes are unaffected: the active-run slot still answers busy.
        assert await service.session_status(session_id) is SessionStatus.ACTIVE
        with pytest.raises(SessionBusyError):
            await service.prompt(session_id, "next", "req_2")


async def test_rebuild_projection_lag_is_not_corruption(tmp_path: Path) -> None:
    """A<H is not corruption on the rebuild path: applied_seq := H is exactly
    the rebuild's job (the commit gate classifies A!=H; the rebuild repairs
    it). The status stays active, the messages stay identical."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_messages = (await service.messages(session_id)).items
        lagged = _session_row(db_path, session_id)
        assert lagged[4] == 2  # healthy A=H=2

        _execute(db_path, "UPDATE sessions SET applied_seq = 1 WHERE session_id = ?", (session_id,))

        await service.rebuild(session_id)  # not StorageCorrupt
        restored = _session_row(db_path, session_id)
        assert restored[0] == "active"
        assert restored[4] == 2  # applied_seq := H
        assert (await service.messages(session_id)).items == original_messages


# -- §3.2.1: the diagnostic mark survives a successful rebuild ----------------


async def test_diagnostic_mark_survives_successful_rebuild(tmp_path: Path) -> None:
    """§3.2.1 L196: a successful rebuild never auto-clears the diagnostic
    mark. After the log is repaired, rebuild restores the data plane
    (messages/config/applied_seq) while the status stays
    read_only_diagnostic and the persisted diagnostic is carried over
    verbatim; write commands keep answering SessionReadOnly (same-request_id
    retries included) and the healthy reads keep working."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_messages = (await service.messages(session_id)).items

        # Isolate the session through the rebuild failure path, then repair
        # the log row that caused it.
        _execute(db_path, "UPDATE events SET version = 99 WHERE session_id = ? AND seq = 2", (session_id,))
        with pytest.raises(StorageCorruptError):
            await service.rebuild(session_id)
        assert _session_row(db_path, session_id)[0] == "read_only_diagnostic"
        isolated_diagnostic = _diagnostic_payload(db_path, session_id)

        _execute(db_path, "UPDATE events SET version = 1 WHERE session_id = ? AND seq = 2", (session_id,))

        # The successful rebuild: data plane restored, management mark kept.
        await service.rebuild(session_id)
        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"  # the mark survives (L196)
        assert after[4] == 2  # applied_seq restored to H
        assert _diagnostic_payload(db_path, session_id) == isolated_diagnostic  # carried verbatim
        assert (await service.messages(session_id)).items == original_messages  # data plane restored
        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC

        # Writes keep answering SessionReadOnly, retries included (§3.2.1 L195).
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "hello", "req_1")  # same request_id, same text
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "hello again", "req_1")  # same request_id, different text
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "fresh", "req_9")  # a brand-new request

        # A second rebuild keeps the mark too (rebuild is not a repair command).
        await service.rebuild(session_id)
        assert _session_row(db_path, session_id)[0] == "read_only_diagnostic"
        assert _diagnostic_payload(db_path, session_id) == isolated_diagnostic


# -- §3.2.1: no new events, no notifications, minimal record ------------------


async def test_rebuild_appends_no_events_and_never_notifies(tmp_path: Path) -> None:
    """§3.3: rebuild replays the committed log -- it appends no business
    events (event count unchanged) and never notifies the Bus (live
    subscription stays silent; positive control: a fresh commit notifies)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with _service(db_path, bus=bus) as service:
        session_id, _ = await _create_prompted(service)
        assert _event_count(db_path, session_id) == 2

        async with bus.subscribe(session_id) as events:
            await service.rebuild(session_id)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)
        assert _event_count(db_path, session_id) == 2  # no business events appended

        # Positive control: the same subscription mechanism does deliver a
        # fresh commit notification, so the timeout above is meaningful.
        session_b = await service.create(_SNAPSHOT)
        async with bus.subscribe(session_b.session_id) as events_b:
            await service.prompt(session_b.session_id, "hello", "req_b")
            notified = await asyncio.wait_for(anext(events_b), timeout=1.0)
        assert notified.type == PY_PROMPT_ACCEPTED


async def test_first_event_damage_over_deleted_read_model_writes_minimal_record(tmp_path: Path) -> None:
    """§3.2.1 L193: with the sessions row gone and the log's first event
    damaged (the created event deleted -> the log starts at seq 2), the
    failed rebuild isolates into the minimal diagnostic record:
    applied_seq=0, empty config/active_run_id, diagnostic payload with the
    first bad seq -- not a py.session.created and not an executable session."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        _execute(db_path, "DELETE FROM messages WHERE session_id = ?", (session_id,))
        _execute(db_path, "DELETE FROM sessions WHERE session_id = ?", (session_id,))
        _execute(db_path, "DELETE FROM events WHERE session_id = ? AND type = 'py.session.created'", (session_id,))

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "seq_gap"
        assert details["first_bad_seq"] == 1  # the log must start at seq 1
        assert details["applied_seq"] == 0  # no read model existed at discovery

        # The §3.2.1 L193 minimal diagnostic record (the isolation INSERT).
        record = _fetch_scalar(
            db_path,
            "SELECT status, active_run_id, config_snapshot, applied_seq FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        assert record == ("read_only_diagnostic", None, None, 0)
        assert _diagnostic_payload(db_path, session_id) == {
            "reason": "seq_gap",
            "first_bad_seq": 1,
            "applied_seq": 0,
        }
        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC


# -- §6.2 snapshot-hash re-verification surfaces on the rebuild path ---------


async def test_rebuild_snapshot_hash_mismatch_isolates(tmp_path: Path) -> None:
    """§6.2/§3.2.1 L191 ("快照哈希不符"): a stored py.session.created whose
    snapshot_hash no longer matches its config_snapshot is stored corruption
    on the rebuild path -- StorageCorrupt, full rollback, isolation -- even
    though the tampered JSON itself is well-formed. The re-verification is
    enforced by the DurableEvent[SessionCreatedData] decode (contracts layer,
    the same check every decode path runs), so it surfaces with the
    stored-decode-failed reason."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))

        data_row = _fetch_scalar(
            db_path, "SELECT data FROM events WHERE session_id = ? AND type = 'py.session.created'", (session_id,)
        )
        assert data_row is not None
        payload = json.loads(data_row[0])
        assert isinstance(payload, dict)
        payload["snapshot_hash"] = "0" * 64  # decodes fine, no longer matches
        _execute(
            db_path,
            "UPDATE events SET data = ? WHERE session_id = ? AND type = 'py.session.created'",
            (json.dumps(payload), session_id),
        )

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "stored_event_decode_failed"
        assert details["first_bad_seq"] == 1
        assert details["applied_seq"] == 2

        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "hello", "req_1")


# -- the service face: gates + the B-review nit-2 microtest ------------------


async def test_rebuild_service_face_gates(tmp_path: Path) -> None:
    """The management entry's own gates: a never-created session is NotFound
    (no row, no log); a malformed session_id is ValidationError; after
    service.close() the answer is SessionClosed (open point E, conservative:
    uniform with prompt/create)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        with pytest.raises(NotFoundError) as missing:
            await service.rebuild("ses_missing")
        assert missing.value.core.code == CoreErrorCode.NOT_FOUND

        for bad_id in ("", "run_123", 123, None):
            with pytest.raises(StoreValidationError):
                await service.rebuild(bad_id)  # type: ignore[arg-type]

        session_id, _ = await _create_prompted(service)
        await service.close()
        with pytest.raises(SessionClosedError) as closed:
            await service.rebuild(session_id)
        assert closed.value.core.code == CoreErrorCode.SESSION_CLOSED


async def test_same_request_id_across_sessions_yields_distinct_runs(tmp_path: Path) -> None:
    """B-review nit-2 microtest: the prompt dedupe key is session-scoped
    (unique(session_id, dedupe_key)) -- the same request_id prompted in two
    different sessions succeeds twice, with two DIFFERENT RunRefs, each
    session keeping its own message and its own active run."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_a = (await service.create(_SNAPSHOT)).session_id
        session_b = (await service.create(_SNAPSHOT)).session_id

        run_a = await service.prompt(session_a, "hello", "req_shared")
        run_b = await service.prompt(session_b, "hello", "req_shared")

        assert run_a.request_id == run_b.request_id == "req_shared"
        assert run_a.run_id != run_b.run_id
        assert run_a.session_id == session_a
        assert run_b.session_id == session_b

        # Each session sees its own message and its own active run.
        assert _user_texts(await service.messages(session_a)) == ["hello"]
        assert _user_texts(await service.messages(session_b)) == ["hello"]
        status_a = await service.run_status(run_a.run_id)
        status_b = await service.run_status(run_b.run_id)
        assert status_a.session_id == session_a
        assert status_b.session_id == session_b
        assert _event_count(db_path, session_a) == 2
        assert _event_count(db_path, session_b) == 2


# -- T-C1-012: the empty log branches and the A > H invariant (fail closed) --


async def test_minimal_record_over_empty_log_is_preserved_verbatim(tmp_path: Path) -> None:
    """The §3.2.1 L193 minimal diagnostic record over an empty log is the
    one surviving row a rebuild leaves untouched: a true no-op (no DELETE
    executes; the empty transaction commits), so every field keeps its value
    verbatim (status/diagnostic/applied_seq/active_run_id/config), the mark
    is never auto-cleared (L196), and writes keep answering SessionReadOnly.
    Reached only through SQL tampering of events (protocol-unreachable)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)

        # Build the minimal record through the real isolation path (the
        # first-event-damage construction), then empty the log entirely.
        _execute(db_path, "DELETE FROM messages WHERE session_id = ?", (session_id,))
        _execute(db_path, "DELETE FROM sessions WHERE session_id = ?", (session_id,))
        _execute(db_path, "DELETE FROM events WHERE session_id = ? AND type = 'py.session.created'", (session_id,))
        with pytest.raises(StorageCorruptError):
            await service.rebuild(session_id)
        record = _session_row(db_path, session_id)
        assert record[:5] == ("read_only_diagnostic", None, None, None, 0)
        diagnostic = _diagnostic_payload(db_path, session_id)

        _execute(db_path, "DELETE FROM events WHERE session_id = ?", (session_id,))
        assert _event_count(db_path, session_id) == 0

        # Rebuild over the record + the empty log: a true no-op.
        await service.rebuild(session_id)
        assert _session_row(db_path, session_id) == record  # all six fields verbatim
        assert _diagnostic_payload(db_path, session_id) == diagnostic
        assert _event_count(db_path, session_id) == 0  # no business events appended

        # The mark survives; reads keep working; writes keep being refused.
        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "hello", "req_1")


async def test_active_row_over_cleared_log_isolates_projection_ahead(tmp_path: Path) -> None:
    """An active row whose log has been cleared entirely is A > H = 0: the
    log cannot prove the stored applied_seq, so rebuild fails closed --
    StorageCorrupt (reason=projection_ahead_of_log, first_bad_seq=1=H+1,
    applied_seq=the discovery-time A), the whole transaction rolled back
    (the row and its messages survive untouched apart from the
    administrative mark), the session isolated, and later writes (same
    request_id retries included) refused."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_row = _session_row(db_path, session_id)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))
        assert original_row[4] == 2  # A = H = 2 before the damage

        _execute(db_path, "DELETE FROM events WHERE session_id = ?", (session_id,))

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "projection_ahead_of_log"
        assert details["first_bad_seq"] == 1  # H + 1 = 0 + 1
        assert details["applied_seq"] == 2  # A at discovery

        # The rollback preserved the projection; isolation flipped the status only.
        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert after[1:5] == original_row[1:5]
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages
        assert _diagnostic_payload(db_path, session_id) == {
            "reason": "projection_ahead_of_log",
            "first_bad_seq": 1,
            "applied_seq": 2,
        }

        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "hello", "req_1")  # same request_id, same text
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "another", "req_2")
        assert _event_count(db_path, session_id) == 0  # no business events appended


async def test_projection_ahead_of_a_self_consistent_log_isolates(tmp_path: Path) -> None:
    """A > H over a NON-empty, self-consistent log (the tail events
    deleted, the log still running 1..k): the stored applied_seq claims
    progress the log cannot prove -- rebuild isolates
    (reason=projection_ahead_of_log, first_bad_seq=H+1 locating the first
    missing tail event) instead of silently normalizing A := H; the row
    survives the rollback and writes are refused. The 3-event log is
    committed through the real store directly (the §7 busy gate would
    reject the second prompt) -- the S3/S4-tested legal way."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        repos = ReadRepositories(database.engine)
        service = SessionService(store=store, repos=repos, bus=VolatileBus(), working_dir="C:/work")
        session_id = (await service.create(_SNAPSHOT)).session_id
        await service.prompt(session_id, "first", "req_1")
        draft = EventDraft[PromptAcceptedData](
            id="evt_direct_3",
            type=PY_PROMPT_ACCEPTED,
            version=1,
            session_id=session_id,
            created_at=_CREATED_AT,
            data=PromptAcceptedData(
                request_id="req_2",
                run_id="run_direct",
                message=UserMessage(id="msg_direct", type="user", created_at=_CREATED_AT, metadata={}, text="second"),
            ),
            metadata={"run_id": "run_direct"},
        )
        await store.commit(session_id, EventBatch("batch_direct", (draft,)), await repos.log_high(session_id))

        # Truncate the tail (the log stays self-consistent at 1..2) and push
        # the stored applied_seq beyond it: A = 4 > H = 2.
        _execute(db_path, "DELETE FROM events WHERE session_id = ? AND seq = 3", (session_id,))
        _execute(db_path, "UPDATE sessions SET applied_seq = 4 WHERE session_id = ?", (session_id,))
        original_row = _session_row(db_path, session_id)
        original_messages = _fetch_all(db_path, _MESSAGE_ROWS, (session_id,))
        assert len(original_messages) == 2
        assert _event_count(db_path, session_id) == 2  # H = 2

        with pytest.raises(StorageCorruptError) as error:
            await service.rebuild(session_id)
        details = error.value.core.details
        assert details["reason"] == "projection_ahead_of_log"
        assert details["first_bad_seq"] == 3  # H + 1: the first missing tail event
        assert details["applied_seq"] == 4  # A at discovery

        # The rollback preserved the projection; isolation flipped the status only.
        after = _session_row(db_path, session_id)
        assert after[0] == "read_only_diagnostic"
        assert after[1:5] == original_row[1:5]
        assert _fetch_all(db_path, _MESSAGE_ROWS, (session_id,)) == original_messages
        assert _diagnostic_payload(db_path, session_id) == {
            "reason": "projection_ahead_of_log",
            "first_bad_seq": 3,
            "applied_seq": 4,
        }
        with pytest.raises(SessionReadOnlyError):
            await service.prompt(session_id, "third", "req_3")
        assert _event_count(db_path, session_id) == 2  # no business events appended


async def test_closed_status_is_carried_over_verbatim_by_rebuild(tmp_path: Path) -> None:
    """T-B-013 nit-1 microtest: a successful rebuild carries a surviving
    row's administrative status over verbatim -- a 'closed' row (R1 has no
    protocol command that produces one; SQL-set here) rebuilds with the
    data plane restored and every sessions field identical."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        original_messages = (await service.messages(session_id)).items

        _execute(db_path, "UPDATE sessions SET status = 'closed' WHERE session_id = ?", (session_id,))
        closed_row = _session_row(db_path, session_id)
        assert closed_row[0] == "closed"

        await service.rebuild(session_id)

        assert _session_row(db_path, session_id) == closed_row  # status verbatim + data plane rebuilt
        assert (await service.messages(session_id)).items == original_messages
        assert await service.session_status(session_id) is SessionStatus.CLOSED


# -- R2-S2: the full R2 run lifecycle rebuilds byte-identically (plan §2/§0.2) --

_R2_SESSION = "ses_0000000000000000000000000001"
_R2_RUN = "run_0000000000000000000000000002"

#: The 11-event legal R2 chain; created + the re-aimed prompt + these = 13.
_R2_CHAIN: tuple[str, ...] = (
    "events_run_started_valid.json",
    "events_step_started_valid.json",
    "events_text_finalized_valid.json",
    "events_reasoning_finalized_valid.json",
    "events_step_generated_valid.json",
    "events_tool_requested_valid.json",
    "events_tool_started_valid.json",
    "events_tool_finished_valid.json",
    "events_step_finished_valid.json",
    "events_run_cancel_requested_valid.json",
    "events_run_finished_valid.json",
)

_SESSIONS_FACE = (
    "SELECT status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
    "FROM sessions WHERE session_id = ?"
)
_MESSAGES_FACE = "SELECT message_id, seq, data FROM messages WHERE session_id = ? ORDER BY seq"
_TOOLS_FACE = (
    "SELECT execution_id, run_id, step_id, message_id, tool_call_id, input_hash, state "
    "FROM tool_executions WHERE session_id = ? ORDER BY execution_id"
)
_RUNS_FACE = "SELECT run_id, session_id, request_id, status, error FROM runs WHERE session_id = ? ORDER BY run_id"


def _fixture_draft(name: str) -> EventDraft[Any]:
    """One contract fixture as a commit-ready draft (seq stripped).

    Local import: this section is appended after the frozen import block
    (append-only extension), so the registry import stays function-local.
    """
    from opencode.contracts import default_event_registry

    raw = json.loads((Path(__file__).resolve().parents[1] / "contract" / "fixtures" / name).read_text(encoding="utf-8"))
    raw.pop("seq", None)
    definition = default_event_registry().resolve_durable(str(raw["type"]), int(raw["version"]))
    draft_model: Any = EventDraft  # runtime-parametrized generic; mypy stays out
    return draft_model[definition.data_model].model_validate(raw)


def _r2_prompt_draft() -> EventDraft[Any]:
    """The R1 prompt fixture re-aimed at the R2 fixture run (run ...0002)."""
    from opencode.contracts import default_event_registry

    raw = json.loads(
        (Path(__file__).resolve().parents[1] / "contract" / "fixtures" / "events_prompt_accepted_valid.json").read_text(
            encoding="utf-8"
        )
    )
    raw.pop("seq", None)
    raw["id"] = "evt_0000000000000000000000000009"
    raw["data"]["request_id"] = "req_0000000000000000000000000002"
    raw["data"]["run_id"] = _R2_RUN
    raw["metadata"] = {"run_id": _R2_RUN, "request_id": "req_0000000000000000000000000002"}
    definition = default_event_registry().resolve_durable(str(raw["type"]), int(raw["version"]))
    draft_model: Any = EventDraft  # runtime-parametrized generic; mypy stays out
    return draft_model[definition.data_model].model_validate(raw)


async def test_r2_log_rebuilds_every_face_byte_identically(tmp_path: Path) -> None:
    """The legal 13-event R2 run lifecycle (accepted -> running -> cancelling
    -> interrupted, one tool execution pending -> succeeded, an assistant
    message replaced at step.finished, an idle row at run.finished) rebuilds
    from the log alone with every read-model face byte-identical: the
    sessions/messages/tool_executions/runs rows, the first-appearance message
    seq through the terminal replacement, and the terminal facts (interrupted
    run, released slot, applied_seq 13)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        created = _fixture_draft("events_session_created_valid.json")
        chain = tuple(_fixture_draft(name) for name in _R2_CHAIN)
        committed = await store.commit(
            _R2_SESSION, EventBatch("batch_r2_lifecycle", (created, _r2_prompt_draft(), *chain)), 0
        )
        assert [event.seq for event in committed] == list(range(1, 14))

        sessions = _fetch_all(db_path, _SESSIONS_FACE, (_R2_SESSION,))
        messages = _fetch_all(db_path, _MESSAGES_FACE, (_R2_SESSION,))
        tools = _fetch_all(db_path, _TOOLS_FACE, (_R2_SESSION,))
        runs = _fetch_all(db_path, _RUNS_FACE, (_R2_SESSION,))

        # The incremental projection's terminal facts before the rebuild.
        assert sessions[0][0] == "active"
        assert sessions[0][1] is None  # the slot released at py.run.finished
        assert sessions[0][4] == 13
        assert [row[0] for row in messages] == [
            "msg_0000000000000000000000000001",
            "msg_0000000000000000000000000012",
            f"msg_idle_{_R2_RUN}",
        ]
        assert messages[1][1] == 7  # first appearance (step.generated) kept through the replacement
        assert tools == [
            (
                "exec_0000000000000000000000000001",
                _R2_RUN,
                "step_0000000000000000000000000002",
                "msg_0000000000000000000000000012",
                "call_0000000000000000000000000001",
                "5b8160c3ef7a2aa5fc1207a2740b1d783068c96fd850a1bbb969107e32c2d625",
                "succeeded",
            )
        ]
        assert runs == [
            ("run_0000000000000000000000000002", _R2_SESSION, "req_0000000000000000000000000002", "interrupted", None)
        ]

        await store.rebuild(_R2_SESSION)

        assert _fetch_all(db_path, _SESSIONS_FACE, (_R2_SESSION,)) == sessions
        assert _fetch_all(db_path, _MESSAGES_FACE, (_R2_SESSION,)) == messages
        assert _fetch_all(db_path, _TOOLS_FACE, (_R2_SESSION,)) == tools
        assert _fetch_all(db_path, _RUNS_FACE, (_R2_SESSION,)) == runs

        # Idempotent: a second rebuild over the same log changes nothing.
        await store.rebuild(_R2_SESSION)
        assert _fetch_all(db_path, _MESSAGES_FACE, (_R2_SESSION,)) == messages
        assert _fetch_all(db_path, _TOOLS_FACE, (_R2_SESSION,)) == tools
        assert _fetch_all(db_path, _RUNS_FACE, (_R2_SESSION,)) == runs
