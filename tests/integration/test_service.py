"""Integration tests for the S7 SessionService + app composition (R1-S7).

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos over
CORE-CONTRACTS §7 L298-325 and the r1-slices R1-S7 acceptance list:

- the full cycle (r1-slices L143): create -> prompt -> messages (UserMessage)
  -> close -> reopen (new engine + new service instance, same db_path) ->
  identical messages;
- the §7 L318-323 four-step fixed order, one branch each: state gate rejects
  (NotFound / SessionReadOnly incl. same-request_id retries / Lifecycle
  CleanupFailed / SessionClosed on both the stored-row and the service-level
  gate), durable idempotency (same text -> the original RunRef with no new
  events/messages/notifications; different text -> IdempotencyConflict even
  while busy), the active_run_id gate (SessionBusy after an idempotency
  miss; concurrent prompts: one RunRef + one SessionBusy, or one shared
  RunRef), and step 4 (fixed IDs, expected_seq = pre-commit H, contiguous
  seq after reopen);
- §7 L325: the R1 accepted run holds the slot across close/reopen (a new
  request_id is SessionBusy; the same request_id returns the original
  RunRef);
- create validation (§6.1: extra keys / non-empty rules / non-integer
  version / missing field), prompt input validation (no implicit
  conversions), run_status (accepted RunStatus with the three identifiers /
  NotFound), session_status + messages read faces, ID prefix + ordering
  properties, and App.close ordering/idempotency.

Closed/cleanup_failed/read_only_diagnostic sessions are produced by direct
read-model SQL (the S6 test_eventlog pattern); no provider fake is needed --
R1 never calls a provider or tool.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.bus import VolatileBus
from opencode.contracts import PY_PROMPT_ACCEPTED, CoreErrorCode, UserMessage
from opencode.session import RunRef, RunStatusValue, SessionBusyError, SessionProjector, SessionService, SessionStatus
from opencode.session.service import REASON_ACTIVE_RUN_WITHOUT_PROMPT, AscendingIds, Clock, IdFactory, prompt_dedupe_key
from opencode.storage import (
    IdempotencyConflictError,
    InvalidCursorError,
    LifecycleCleanupFailedError,
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


def _event_count(db_path: Path, session_id: str) -> int:
    row = _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,))
    return int(row[0]) if row is not None else 0


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


class FixedClock:
    """Injectable clock returning one constant UTC datetime (deterministic tests)."""

    def __init__(self, now: datetime = _CREATED_AT) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now


class SeqIds:
    """Injectable deterministic IdFactory: ``<prefix> + zero-padded counter``.

    The counter order pins the service's step-4 generation order for
    assertions: create -> session/event/batch; prompt -> run/message/event/batch.
    """

    def __init__(self) -> None:
        self._counter = 0

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


@asynccontextmanager
async def _service(
    db_path: Path,
    *,
    bus: VolatileBus | None = None,
    ids: IdFactory | None = None,
    clock: Clock | None = None,
) -> AsyncIterator[SessionService]:
    """One service over its own engine on an already-migrated db_path.

    The context owns the Database handle: leaving it disposes the engine and
    releases the data-directory writer lock, so a later reopen on the same
    path works.
    """
    async with open_database(db_path, mode="create") as database:
        yield SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=bus if bus is not None else VolatileBus(),
            working_dir="C:/work",
            clock=clock,
            ids=ids,
        )


def _open_app(
    db_path: Path,
    *,
    bus: VolatileBus | None = None,
    ids: IdFactory | None = None,
    clock: Clock | None = None,
) -> App:
    return App(db_path, working_dir="C:/work", bus=bus, clock=clock, ids=ids)


async def _create_prompted(
    service: SessionService, *, text: str = "hello", request_id: str = "req_1"
) -> tuple[str, RunRef]:
    """create + prompt; returns (session_id, run_ref)."""
    session = await service.create(_SNAPSHOT)
    run_ref = await service.prompt(session.session_id, text, request_id)
    return session.session_id, run_ref


# -- the full cycle (r1-slices L143) ---------------------------------------------


async def test_full_cycle_persists_and_reopens_identically(tmp_path: Path) -> None:
    """create -> prompt -> messages -> close -> reopen (new engine + new
    service, same db_path) -> identical messages (acceptance C, first half)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _open_app(db_path) as app:
        session_ref = await app.service.create(_SNAPSHOT)
        run_ref = await app.service.prompt(session_ref.session_id, "hello, agent", "req_1")
        assert run_ref.session_id == session_ref.session_id
        assert run_ref.request_id == "req_1"
        assert run_ref.run_id.startswith("run_")

        page = await app.service.messages(session_ref.session_id)
        assert [message.text for message in page.items] == ["hello, agent"]
        message = page.items[0]
        assert isinstance(message, UserMessage)
        assert message.type == "user"
        assert message.id.startswith("msg_")

        status = await app.service.run_status(run_ref.run_id)
        assert status.session_id == run_ref.session_id
        assert status.run_id == run_ref.run_id
        assert status.request_id == run_ref.request_id
        assert status.status is RunStatusValue.ACCEPTED
        assert status.error is None

    # Reopen: a brand-new engine + service instance over the same db_path.
    async with _open_app(db_path) as app:
        assert await app.service.session_status(session_ref.session_id) is SessionStatus.ACTIVE
        reopened = await app.service.messages(session_ref.session_id)
        assert reopened.items == page.items
        assert reopened.has_more is False
        tail = await app.service.messages(session_ref.session_id, after_message_id=message.id)
        assert tail.items == []


# -- §7 step 1: the state gate --------------------------------------------------


async def test_prompt_rejects_missing_session_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        with pytest.raises(NotFoundError) as prompt_error:
            await service.prompt("ses_missing", "hello", "req_1")
        assert prompt_error.value.core.code == CoreErrorCode.NOT_FOUND

        with pytest.raises(NotFoundError) as status_error:
            await service.session_status("ses_missing")
        assert status_error.value.core.code == CoreErrorCode.NOT_FOUND

        with pytest.raises(NotFoundError) as run_error:
            await service.run_status("run_missing")
        assert run_error.value.core.code == CoreErrorCode.NOT_FOUND


async def test_prompt_read_only_gate_precedes_idempotent_retry(tmp_path: Path) -> None:
    """§3.2.1/§7 L320: read_only_diagnostic rejects ALWAYS, including the
    same-request_id retry (the gate precedes the idempotency query), while
    session_status stays queryable ("读取仍可用")."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, _ = await _create_prompted(service)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE sessions SET status = 'read_only_diagnostic' WHERE session_id = ?", (session_id,)
            )

        with pytest.raises(SessionReadOnlyError) as error:
            await service.prompt(session_id, "hello", "req_1")  # same request_id, same text
        assert error.value.core.code == CoreErrorCode.SESSION_READ_ONLY
        assert error.value.core.retryable is False

        assert await service.session_status(session_id) is SessionStatus.READ_ONLY_DIAGNOSTIC


async def test_prompt_rejects_cleanup_failed_and_closed_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_a, _ = await _create_prompted(service)
        session_b, _ = await _create_prompted(service)
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE sessions SET status = 'cleanup_failed' WHERE session_id = ?", (session_a,))
            connection.execute("UPDATE sessions SET status = 'closed' WHERE session_id = ?", (session_b,))

        with pytest.raises(LifecycleCleanupFailedError) as cleanup:
            await service.prompt(session_a, "hello", "req_1")
        assert cleanup.value.core.code == CoreErrorCode.LIFECYCLE_CLEANUP_FAILED

        with pytest.raises(SessionClosedError) as closed:
            await service.prompt(session_b, "hello", "req_1")
        assert closed.value.core.code == CoreErrorCode.SESSION_CLOSED


async def test_service_close_gate_rejects_writes_reads_keep_working(tmp_path: Path) -> None:
    """The service-level closing gate: after close(), prompt/create answer
    SessionClosed (the gate is checked after the stored-status gates), reads
    keep working while the composition root keeps the engine open, and
    repeated close() is a safe no-op."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, run_ref = await _create_prompted(service)
        await service.close()
        await service.close()  # repeated close is safe

        with pytest.raises(SessionClosedError) as prompt_error:
            await service.prompt(session_id, "another", "req_2")
        assert prompt_error.value.core.code == CoreErrorCode.SESSION_CLOSED

        with pytest.raises(SessionClosedError) as create_error:
            await service.create(_SNAPSHOT)
        assert create_error.value.core.code == CoreErrorCode.SESSION_CLOSED

        # Reads still work: the service released only its own resources.
        assert await service.session_status(session_id) is SessionStatus.ACTIVE
        assert (await service.messages(session_id)).items != []
        status = await service.run_status(run_ref.run_id)
        assert status.status is RunStatusValue.ACCEPTED


# -- §7 step 2: durable idempotency ----------------------------------------------


async def test_prompt_idempotent_retry_returns_original_run_without_side_effects(tmp_path: Path) -> None:
    """§7 L321-322: the same request_id with a code-point-identical text returns
    the original RunRef -- no new IDs, no re-projection, no re-notification."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    bus = VolatileBus()

    async with _service(db_path, bus=bus) as service:
        session_id, run_ref = await _create_prompted(service, text="hello", request_id="req_1")
        assert _event_count(db_path, session_id) == 2  # session.created + prompt.accepted

        # The stored dedupe key is exactly "prompt:" + request_id (§7 L321).
        row = _fetch_scalar(
            db_path,
            "SELECT dedupe_key FROM events WHERE session_id = ? AND type = 'py.prompt.accepted'",
            (session_id,),
        )
        assert row == ("prompt:req_1",)
        assert row == (prompt_dedupe_key("req_1"),)

        # The retry under a live subscription: nothing may be re-notified.
        async with bus.subscribe(session_id) as events:
            retry = await service.prompt(session_id, "hello", "req_1")
            assert retry == run_ref
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)
        assert _event_count(db_path, session_id) == 2
        assert len((await service.messages(session_id)).items) == 1

        # Positive control: the same subscription mechanism does deliver a
        # fresh commit notification, so the timeout above is meaningful.
        session_b = await service.create(_SNAPSHOT)
        async with bus.subscribe(session_b.session_id) as events_b:
            await service.prompt(session_b.session_id, "hello", "req_b")
            notified = await asyncio.wait_for(anext(events_b), timeout=1.0)
        assert notified.type == PY_PROMPT_ACCEPTED


async def test_prompt_same_request_id_different_text_conflicts_even_while_busy(tmp_path: Path) -> None:
    """§7 L322: a different text under the same request_id is IdempotencyConflict
    -- the idempotency lookup precedes the active_run_id gate, so the conflict
    is reported even while the session is busy with the original run."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, run_ref = await _create_prompted(service, text="hello", request_id="req_1")

        with pytest.raises(IdempotencyConflictError) as error:
            await service.prompt(session_id, "hello again", "req_1")
        assert error.value.core.code == CoreErrorCode.IDEMPOTENCY_CONFLICT

        # No implicit trimming or case folding: near-miss texts are conflicts.
        for near_miss in (" hello", "Hello", "hello "):
            with pytest.raises(IdempotencyConflictError):
                await service.prompt(session_id, near_miss, "req_1")

        # The conflict disturbed nothing: the original run still holds the slot.
        assert _event_count(db_path, session_id) == 2
        assert (await service.run_status(run_ref.run_id)).status is RunStatusValue.ACCEPTED


# -- §7 step 3: the active_run_id gate ---------------------------------------------


async def test_r1_accepted_run_holds_the_slot_across_close_and_reopen(tmp_path: Path) -> None:
    """§7 L325: the R1 accepted run has no terminal state, so the slot survives a
    service close/reopen -- a new request_id is SessionBusy, the same request_id
    still idempotently returns the original RunRef, and run_status keeps
    answering accepted."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, run_ref = await _create_prompted(service, text="hello", request_id="req_1")

    async with _service(db_path) as service:  # a brand-new service over the same db
        with pytest.raises(SessionBusyError) as busy:
            await service.prompt(session_id, "another prompt", "req_2")
        assert busy.value.core.code == CoreErrorCode.SESSION_BUSY

        retry = await service.prompt(session_id, "hello", "req_1")
        assert retry == run_ref

        assert (await service.run_status(run_ref.run_id)).status is RunStatusValue.ACCEPTED
        assert len((await service.messages(session_id)).items) == 1


async def test_concurrent_prompts_one_run_one_busy(tmp_path: Path) -> None:
    """§7 L323: two different request_ids racing on one session serialize under
    the per-session commit lock -- exactly one RunRef, one SessionBusy, and only
    one accepted event lands in the log."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session = await service.create(_SNAPSHOT)
        results = await asyncio.gather(
            service.prompt(session.session_id, "first", "req_1"),
            service.prompt(session.session_id, "second", "req_2"),
            return_exceptions=True,
        )

        winners: list[RunRef] = []
        busy: list[SessionBusyError] = []
        for result in results:
            if isinstance(result, RunRef):
                winners.append(result)
            elif isinstance(result, SessionBusyError):
                busy.append(result)
        assert len(winners) == 1
        assert len(busy) == 1

        assert _event_count(db_path, session.session_id) == 2  # created + one accepted
        expected_text = "first" if winners[0].request_id == "req_1" else "second"
        page = await service.messages(session.session_id)
        assert [message.text for message in page.items] == [expected_text]
        assert (await service.run_status(winners[0].run_id)).status is RunStatusValue.ACCEPTED


async def test_concurrent_identical_prompts_share_one_run(tmp_path: Path) -> None:
    """§7 L323: the same request_id racing twice -> one commit plus one dedupe
    hit: both callers receive the SAME RunRef and only one event lands."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session = await service.create(_SNAPSHOT)
        first, second = await asyncio.gather(
            service.prompt(session.session_id, "hello", "req_1"),
            service.prompt(session.session_id, "hello", "req_1"),
        )
        assert first == second
        assert _event_count(db_path, session.session_id) == 2


# -- §7 step 4: fixed IDs + expected_seq = the pre-commit H ------------------------


async def test_prompt_expected_seq_is_the_pre_commit_high_watermark(tmp_path: Path) -> None:
    """§3.2/§7 step 4: expected_seq is the per-session high-water mark H read
    before the commit; the per-session seq numbering stays contiguous across a
    reopen, and the step-4 ID generation order is fixed (create: session/event/
    batch; prompt: run/message/event/batch)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    ids = SeqIds()

    seqs_sql = "SELECT seq FROM events WHERE session_id = ? ORDER BY seq"
    session_sql = "SELECT applied_seq, active_run_id FROM sessions WHERE session_id = ?"
    messages_sql = "SELECT message_id FROM messages WHERE session_id = ?"

    async with _service(db_path, ids=ids) as service:
        session_a = await service.create(_SNAPSHOT)  # ses_0001, evt_0002, batch_0003
        assert session_a.session_id == "ses_0001"
        run_a = await service.prompt(session_a.session_id, "hello", "req_1")  # run_0004..batch_0007
        assert run_a.run_id == "run_0004"
        assert _fetch_all(db_path, seqs_sql, (session_a.session_id,)) == [(1,), (2,)]
        assert _fetch_scalar(db_path, session_sql, (session_a.session_id,)) == (2, "run_0004")
        assert _fetch_all(db_path, messages_sql, (session_a.session_id,)) == [("msg_0005",)]

    async with _service(db_path, ids=ids) as service:  # reopen: new engine + service
        # Reads never advance H or applied_seq (no replay side effects).
        max_seq_sql = "SELECT MAX(seq) FROM events WHERE session_id = ?"
        assert _fetch_scalar(db_path, max_seq_sql, (session_a.session_id,)) == (2,)
        assert (await service.session_status(session_a.session_id)) is SessionStatus.ACTIVE
        assert _fetch_scalar(db_path, session_sql, (session_a.session_id,)) == (2, "run_0004")

        # A fresh session starts its own per-session numbering from H = 0.
        session_b = await service.create(_SNAPSHOT)  # ses_0008, evt_0009, batch_0010
        assert session_b.session_id == "ses_0008"
        assert _fetch_all(db_path, seqs_sql, (session_b.session_id,)) == [(1,)]
        run_b = await service.prompt(session_b.session_id, "again", "req_b")  # run_0011..batch_0014
        assert run_b.run_id == "run_0011"
        assert _fetch_all(db_path, seqs_sql, (session_b.session_id,)) == [(1,), (2,)]


def test_default_ids_are_prefixed_and_lexicographically_ascending() -> None:
    """The default factory produces no random UUIDs: every id is a mandatory §3.1
    prefix plus a zero-padded time-ordered body -- unique and lexicographically
    sortable within its prefix even under a constant injected clock (the
    same-millisecond counter keeps strict order), and a later millisecond
    always sorts after an earlier one."""
    ids = AscendingIds(FixedClock())  # the same millisecond on every call
    groups: list[tuple[list[str], str]] = [
        ([ids.session_id() for _ in range(3)], "ses_"),
        ([ids.event_id() for _ in range(3)], "evt_"),
        ([ids.message_id() for _ in range(3)], "msg_"),
        ([ids.run_id() for _ in range(3)], "run_"),
        ([ids.batch_id() for _ in range(3)], "batch_"),
    ]
    for group, prefix in groups:
        assert all(value.startswith(prefix) and len(value) == len(prefix) + 23 for value in group)
        assert group == sorted(group)
        assert len(set(group)) == 3

    later = AscendingIds(FixedClock(_CREATED_AT.replace(microsecond=3_000)))  # +3 ms
    assert ids.event_id() < later.event_id()


# -- §7 run_status (open point C) ---------------------------------------------------


async def test_run_status_finds_the_active_run_and_fails_closed(tmp_path: Path) -> None:
    """run_status finds the R1 run by the active-run slot: an accepted RunStatus
    with the three identifiers; an unknown run is NotFound; a held slot without
    its accepted event is read-model/log inconsistency -> StorageCorrupt."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, run_ref = await _create_prompted(service, request_id="req_1")

        status = await service.run_status(run_ref.run_id)
        assert (status.session_id, status.run_id, status.request_id) == (session_id, run_ref.run_id, "req_1")
        assert status.status is RunStatusValue.ACCEPTED
        assert status.error is None

        with pytest.raises(NotFoundError) as missing:
            await service.run_status("run_missing")
        assert missing.value.core.code == CoreErrorCode.NOT_FOUND

        with pytest.raises(StoreValidationError):  # a ses_ id is not a run id
            await service.run_status(session_id)

        # A held slot without its accepted event is inconsistency, fail closed.
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE sessions SET active_run_id = 'run_orphan' WHERE session_id = ?", (session_id,))
        with pytest.raises(StorageCorruptError) as corrupt:
            await service.run_status("run_orphan")
        assert corrupt.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert corrupt.value.core.details["reason"] == REASON_ACTIVE_RUN_WITHOUT_PROMPT


# -- create/prompt input validation (§6.1 / §7 step 1) --------------------------------


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param({"schema_version": 1, "permission_rules": [], "extra": True}, id="extra-key"),
        pytest.param({"schema_version": 1, "permission_rules": [{"allow": "*"}]}, id="non-empty-rules"),
        pytest.param({"schema_version": "1", "permission_rules": []}, id="string-schema-version"),
        pytest.param({"schema_version": True, "permission_rules": []}, id="bool-schema-version"),
        pytest.param({"permission_rules": []}, id="missing-schema-version"),
        pytest.param("not-an-object", id="not-an-object"),
    ],
)
async def test_create_rejects_invalid_snapshots(tmp_path: Path, snapshot: object) -> None:
    """§6.1: exactly {schema_version: 1, permission_rules: []} is legal -- an
    illegal snapshot is ValidationError before any storage write."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        with pytest.raises(StoreValidationError) as error:
            await service.create(snapshot)  # type: ignore[arg-type]
        assert error.value.core.code == CoreErrorCode.VALIDATION_ERROR
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)


@pytest.mark.parametrize(
    ("session_id", "request_id", "text"),
    [
        pytest.param(42, "req_1", "hello", id="session-id-not-a-string"),
        pytest.param("", "req_1", "hello", id="session-id-empty"),
        pytest.param("evt_0001", "req_1", "hello", id="session-id-wrong-prefix"),
        pytest.param("ses_0001", 42, "hello", id="request-id-not-a-string"),
        pytest.param("ses_0001", "", "hello", id="request-id-empty"),
        pytest.param("ses_0001", "req_1", 42, id="text-not-a-string"),
    ],
)
async def test_prompt_rejects_invalid_inputs_before_any_read(
    tmp_path: Path, session_id: object, request_id: object, text: object
) -> None:
    """§7 step 1: input validation precedes every gate and read -- a bad argument
    is ValidationError even for a session that does not exist (no implicit
    conversions anywhere on the command surface)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        with pytest.raises(StoreValidationError) as error:
            await service.prompt(session_id, text, request_id)  # type: ignore[arg-type]
        assert error.value.core.code == CoreErrorCode.VALIDATION_ERROR
        assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)


async def test_prompt_accepts_empty_string_text(tmp_path: Path) -> None:
    """Only the text TYPE is validated: a zero-length text is a legal prompt (an
    empty string is not falsy-coerced into a rejection)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_id, run_ref = await _create_prompted(service, text="", request_id="req_1")
        page = await service.messages(session_id)
        assert [message.text for message in page.items] == [""]
        assert run_ref.request_id == "req_1"


# -- session_status / messages read faces (§7 L305/L310) --------------------------------


async def test_session_status_and_messages_read_faces(tmp_path: Path) -> None:
    """Reads never trigger recovery or writes: session_status is the enum face;
    message pages are ordered with an exclusive cursor; another session's cursor
    is InvalidCursor; the limit clamps; bad arguments are ValidationError.

    Through the R1 command surface one session reaches at most one user message
    (the busy gate holds until R2 adds provider turns), so deep pagination stays
    covered by the S6 tests."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with _service(db_path) as service:
        session_a, _ = await _create_prompted(service, text="hello", request_id="req_a")
        session_b, _ = await _create_prompted(service, text="hi", request_id="req_b")

        assert (await service.session_status(session_a)) is SessionStatus.ACTIVE

        page = await service.messages(session_a)
        assert [message.text for message in page.items] == ["hello"]
        assert page.has_more is False
        assert page.next_after_message_id == page.items[0].id

        tail = await service.messages(session_a, after_message_id=page.next_after_message_id)
        assert tail.items == []
        assert tail.has_more is False

        foreign = (await service.messages(session_b)).items[0].id
        with pytest.raises(InvalidCursorError) as cursor_error:
            await service.messages(session_a, after_message_id=foreign)
        assert cursor_error.value.core.code == CoreErrorCode.INVALID_CURSOR

        clamped = await service.messages(session_a, limit=0)  # 0 clamps to 1
        assert len(clamped.items) == 1

        with pytest.raises(StoreValidationError):
            await service.messages(session_a, after_message_id="")
        with pytest.raises(StoreValidationError):
            await service.messages(session_a, limit=True)

        with pytest.raises(NotFoundError):
            await service.messages("ses_missing")


# -- App composition: close ordering (task ②) ------------------------------------------


async def test_app_close_releases_engine_and_writer_lock_idempotently(tmp_path: Path) -> None:
    """App.close releases in the fixed order -- the service gate first, then the
    engine and the data-directory writer lock -- and is safe to repeat. The
    service owns no bus subscriptions; a fresh App can open the released data
    directory and keeps reading the persisted session."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    app = _open_app(db_path)
    session_id, run_ref = await _create_prompted(app.service)
    assert app.bus.subscription_count == 0

    await app.close()
    await app.close()  # repeated close is a no-op
    assert app.service.closed is True
    assert app.database.closed is True

    with pytest.raises(SessionClosedError):  # the service gate answers first
        await app.service.create(_SNAPSHOT)
    with pytest.raises(SessionClosedError):
        await app.service.prompt(session_id, "more", "req_2")

    async with _open_app(db_path) as reopened:  # the writer lock was released
        assert (await reopened.service.session_status(session_id)) is SessionStatus.ACTIVE
        assert (await reopened.service.run_status(run_ref.run_id)).status is RunStatusValue.ACCEPTED
