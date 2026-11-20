"""T-C3-008 contract set: storage-failure classification + bound-parameter batching.

RED against main @ 8ad5b4c (the modules ``opencode.storage.failures`` and
``opencode.storage.batch`` do not exist); GREEN after the implementation wires
``failures.py`` + ``batch.py`` and the four transaction seams. Upstream:
``packages/core/src/database/storage-failure.ts`` (58 lines) + ``batch.ts``
(21 lines) + ``storage-failure.test.ts`` (4 families). The five banked
decisions live in doc/plans/port-232-increments.md T-C3-008.

Empirical pins (wal_probe.py, 2026-11-21):

- ``PRAGMA max_page_count = 1`` fails the 60 KiB INSERT with SQLITE_FULL (13)
  under BOTH journal modes WAL and DELETE -- WAL does not buffer past the cap,
  so the live probes cap storage without touching journal mode (upstream form).
- A hand-constructed ``sqlite3.OperationalError`` has no ``sqlite_errorcode``
  until one is assigned, and the assignment sticks (the fixture helper below
  and the classification "not a storage failure" case both rely on this).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from opencode.storage import EventBatch, SessionStore, open_database
from opencode.storage.batch import MAX_BOUND_PARAMETERS, batches
from opencode.storage.failures import (
    AUTO_ROLLBACK_CODES,
    STORAGE_MESSAGES,
    finalize_transaction,
    rolled_back,
    storage_failure_message,
)
from tests.integration.test_store_commit import (
    RecordingProjector,
    _prompt_accepted_draft,
    _require_success,
    _session_created_draft,
    _upgrade_head,
)

#: The upstream live-probe payload: 60 KiB into a one-page-capped database.
_LARGE = "x" * 60_000


def _sql_error(code: int, message: str = "fixture") -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


# ---------------------------------------------------------------------------
# the classification tables (upstream verbatim)
# ---------------------------------------------------------------------------


def test_messages_table_is_upstream_verbatim() -> None:
    assert STORAGE_MESSAGES == {
        5: "Storage is in use by another process",  # SQLITE_BUSY
        6: "Storage is in use by another process",  # SQLITE_LOCKED
        8: "Storage is read-only",  # SQLITE_READONLY
        10: "Storage couldn't be read or written",  # SQLITE_IOERR
        11: "Storage is damaged",  # SQLITE_CORRUPT
        13: "Storage is full",  # SQLITE_FULL
        14: "Storage couldn't be opened",  # SQLITE_CANTOPEN
        26: "Storage is damaged",  # SQLITE_NOTADB
    }


def test_auto_rollback_codes_set() -> None:
    # sqlite.org transaction error responses: after these the transaction may
    # already be rolled back by SQLite itself (upstream autoRollback).
    assert AUTO_ROLLBACK_CODES == frozenset({5, 7, 9, 10, 13})


def test_classification_matrix() -> None:
    for code, expected in (
        (5, "Storage is in use by another process"),
        (6, "Storage is in use by another process"),
        (8, "Storage is read-only"),
        (10, "Storage couldn't be read or written"),
        (11, "Storage is damaged"),
        (13, "Storage is full"),
        (14, "Storage couldn't be opened"),
        (26, "Storage is damaged"),
    ):
        assert storage_failure_message(_sql_error(code)) == expected
    # A constraint, a generic sqlite error, a plain exception, and an
    # un-coded sqlite error are NOT storage failures.
    assert storage_failure_message(_sql_error(19)) is None
    assert storage_failure_message(_sql_error(1)) is None
    assert storage_failure_message(ValueError("plain")) is None
    assert storage_failure_message(sqlite3.OperationalError("no code attached")) is None
    assert storage_failure_message(None) is None


def test_extended_codes_classify_by_primary_byte() -> None:
    assert storage_failure_message(_sql_error(517)) == "Storage is in use by another process"  # BUSY_SNAPSHOT
    assert storage_failure_message(_sql_error(262)) == "Storage is in use by another process"  # LOCKED_SHAREDCACHE
    assert storage_failure_message(_sql_error(266)) == "Storage couldn't be read or written"  # IOERR_READ
    assert storage_failure_message(_sql_error(522)) == "Storage couldn't be read or written"  # IOERR_SHORT_READ
    assert storage_failure_message(_sql_error(267)) == "Storage is damaged"  # CORRUPT_VTAB


def test_wrapped_errors_walk_orig_and_cause() -> None:
    # SQLAlchemy wraps the DBAPI error as `.orig`.
    wrapped = OperationalError("INSERT ...", None, _sql_error(13, "disk full"))
    assert storage_failure_message(wrapped) == "Storage is full"
    # Arbitrary re-wraps keep the original as `__cause__` (the py stand-in
    # for the upstream Effect cause walk), transitively.
    inner = _sql_error(5, "busy")
    middle: BaseException = RuntimeError("middle wrap")
    middle.__cause__ = inner
    outer: BaseException = ValueError("outer wrap")
    outer.__cause__ = middle
    assert storage_failure_message(outer) == "Storage is in use by another process"
    # A wrapper around a non-sqlite error classifies as nothing.
    assert storage_failure_message(OperationalError("INSERT", None, ValueError("not sqlite"))) is None
    # A cyclic chain terminates instead of hanging.
    cyclic: BaseException = RuntimeError("cycle")
    cyclic.__cause__ = cyclic
    assert storage_failure_message(cyclic) is None


def test_rolled_back_membership() -> None:
    for code in (5, 7, 9, 10, 13, 266):  # the auto-rollback set + an extended IOERR
        assert rolled_back(_sql_error(code)) is True
    for code in (6, 8, 11, 14, 19, 262):  # everything else (and non-members) says not
        assert rolled_back(_sql_error(code)) is False
    assert rolled_back(ValueError("plain")) is False
    assert rolled_back(None) is False


# ---------------------------------------------------------------------------
# batching (upstream batch.ts)
# ---------------------------------------------------------------------------


def test_max_bound_parameters_is_100() -> None:
    # Durable Object SQLite rejects statements binding more than 100
    # parameters; every runtime stays under it.
    assert MAX_BOUND_PARAMETERS == 100


def test_batches_arithmetic_and_injectable_cap() -> None:
    # size = max(1, (cap - fixed) // per_item), exact ceiling slices.
    assert batches(list(range(10)), 30) == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]
    assert batches([], 1) == []
    assert batches([1, 2], 250) == [[1], [2]]  # per_item above the cap degrades to singletons
    assert batches(list(range(10)), 30, 13) == [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]]  # (100-13)//30 = 2
    assert batches(list(range(3)), 10, 90) == [[0], [1], [2]]  # fixed leaves exactly one slot
    chunked = batches(list(range(250)), 25)  # size 4
    assert len(chunked) == 63
    assert chunked[0] == [0, 1, 2, 3]
    assert chunked[-1] == [248, 249]
    # The cap is injectable for runtimes with a different real limit.
    assert batches(list(range(10)), 1, max_bound_parameters=3) == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]


# ---------------------------------------------------------------------------
# the finalize seam (upstream session.ts L198-205)
# ---------------------------------------------------------------------------


class _ExplodingRollback:
    """Structural fake: the rollback itself fails."""

    async def rollback(self) -> None:
        raise RuntimeError("rollback exploded")


class _RecordingRollback:
    """Structural fake: the rollback succeeds."""

    def __init__(self) -> None:
        self.calls = 0

    async def rollback(self) -> None:
        self.calls += 1


async def test_finalize_swallows_rollback_failure_for_auto_rollback_bodies() -> None:
    connection = _ExplodingRollback()
    for code in (5, 7, 9, 10, 13):
        # SQLite may already have rolled the transaction back; the rollback
        # failure is noise, so the caller's body error is what surfaces.
        await finalize_transaction(connection, _sql_error(code))


async def test_finalize_replaces_body_when_rollback_fails_otherwise() -> None:
    connection = _ExplodingRollback()
    with pytest.raises(RuntimeError, match="rollback exploded"):
        await finalize_transaction(connection, _sql_error(19, "constraint"))
    with pytest.raises(RuntimeError, match="rollback exploded"):
        await finalize_transaction(connection, ValueError("plain"))


async def test_finalize_returns_none_when_rollback_succeeds() -> None:
    connection = _RecordingRollback()
    await finalize_transaction(connection, _sql_error(13, "full"))
    assert connection.calls == 1


# ---------------------------------------------------------------------------
# the live full disk (upstream storage-failure.test.ts families 1-2)
# ---------------------------------------------------------------------------


async def test_live_full_disk_classifies_and_recovers(tmp_path: Path) -> None:
    async with open_database(tmp_path / "full.db", mode="create") as database:
        async with database.engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE probe (value TEXT)"))
        async with database.engine.connect() as connection:
            await connection.execute(sa.text("PRAGMA max_page_count = 1"))
        caught: BaseException | None = None
        try:
            async with database.engine.begin() as connection:
                await connection.execute(sa.text("INSERT INTO probe (value) VALUES (:v)"), {"v": _LARGE})
        except BaseException as error:  # the probe measures the failure itself
            caught = error
        assert caught is not None
        assert storage_failure_message(caught) == "Storage is full"
        assert rolled_back(caught) is True
        async with database.engine.connect() as connection:
            await connection.execute(sa.text("PRAGMA max_page_count = 1073741823"))
        async with database.engine.begin() as connection:
            await connection.execute(sa.text("INSERT INTO probe (value) VALUES ('ok')"))
        async with database.engine.connect() as connection:
            count = (await connection.execute(sa.text("SELECT count(*) FROM probe"))).scalar_one()
        assert count == 1  # the capped insert never landed; nothing dangles


async def test_session_commit_surfaces_storage_failure_and_recovers(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _require_success(_upgrade_head(db_path))
    projector = RecordingProjector()
    async with open_database(db_path, mode="create") as database:
        # Cap growth on the pool's connection: SessionStore shares this
        # engine, so the commit transaction checks out the capped connection.
        async with database.engine.connect() as connection:
            await connection.execute(sa.text("PRAGMA max_page_count = 1"))
        store = SessionStore(database.engine, projector=projector)
        # The 60 KiB payload forces page growth: a capped database rejects the
        # events INSERT inside the commit transaction (small rows can land in
        # existing free pages and never trip the cap).
        batch = EventBatch(
            "batch_1",
            (
                _session_created_draft("ses_a", "evt_a1"),
                _prompt_accepted_draft("ses_a", "evt_a2", text=_LARGE),
            ),
        )
        with pytest.raises(OperationalError) as excinfo:
            await store.commit("ses_a", batch, 0)
        assert storage_failure_message(excinfo.value) == "Storage is full"
        assert rolled_back(excinfo.value) is True
        assert projector.calls == []  # the body failed before projection ran
        async with database.engine.connect() as connection:
            await connection.execute(sa.text("PRAGMA max_page_count = 1073741823"))
        committed = await store.commit("ses_a", batch, 0)
        assert [event.seq for event in committed] == [1, 2]
        assert [event.id for event in projector.calls] == ["evt_a1", "evt_a2"]


# ---------------------------------------------------------------------------
# the four seam wirings (structural source contract)
# ---------------------------------------------------------------------------


def test_seam_wiring_source_contract() -> None:
    storage_dir = Path(__file__).resolve().parents[2] / "src" / "opencode" / "storage"
    sources: dict[str, str] = {
        name: (storage_dir / name).read_text(encoding="utf-8") for name in ("txn.py", "jobs_store.py")
    }
    for name, source in sources.items():
        # _commit_transaction/_rebuild_transaction (txn.py) and
        # _commit/rebuild (jobs_store.py) each finalize exactly once per seam.
        assert source.count("finalize_transaction(") == 2, name
        # The bare rollback is gone from the seams: it now lives inside
        # finalize_transaction with the auto-rollback-aware replacement.
        assert "await connection.rollback()" not in source, name
