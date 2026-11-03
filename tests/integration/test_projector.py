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
    assert session_row[1] == "run_1"  # §7 L316 wiring (T-C1-010): the projector sets the active-run slot
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
    assert len(first.messages) == 1
    user_message = first.messages[0]
    assert isinstance(user_message, UserMessage)
    assert user_message.text == "hello, agent"
    assert user_message.id == "msg_0000000000000000000000000001"

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

    # Event types outside the folded subset are not foldable.
    future = DurableEvent[PromptAcceptedData](
        id="evt_future",
        type="py.permission.requested",
        version=1,
        session_id="ses_0000000000000000000000000001",
        created_at=prompt.created_at,
        data=prompt.data,
        seq=3,
    )
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), future)

    # A data model that does not match the event type is rejected (run.started
    # is foldable in R2, but not with prompt.accepted data).
    mismatched_run = DurableEvent[PromptAcceptedData](
        id="evt_mismatch_run",
        type="py.run.started",
        version=1,
        session_id="ses_0000000000000000000000000001",
        created_at=prompt.created_at,
        data=prompt.data,
        metadata={"run_id": "run_0000000000000000000000000002"},
        seq=3,
    )
    with pytest.raises(IllegalTransitionError):
        apply(apply(SessionState(), created), mismatched_run)

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


# -- R2-S2: run/step/tool/text projection + runs read model (plan §2) -----------


_R2_SESSION = "ses_0000000000000000000000000001"
_R2_RUN = "run_0000000000000000000000000002"
_R2_FIXTURES: tuple[tuple[str, str], ...] = (
    ("run_started", "events_run_started_valid.json"),
    ("step_started", "events_step_started_valid.json"),
    ("text_finalized", "events_text_finalized_valid.json"),
    ("reasoning_finalized", "events_reasoning_finalized_valid.json"),
    ("step_generated", "events_step_generated_valid.json"),
    ("tool_requested", "events_tool_requested_valid.json"),
    ("tool_started", "events_tool_started_valid.json"),
    ("tool_finished", "events_tool_finished_valid.json"),
    ("step_finished", "events_step_finished_valid.json"),
    ("cancel_requested", "events_run_cancel_requested_valid.json"),
    ("run_finished", "events_run_finished_valid.json"),
)


def _fixture_raw(name: str) -> dict[str, Any]:
    """Load one contract fixture's envelope as a plain dict."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_draft(name: str) -> EventDraft[Any]:
    """One contract fixture as a commit-ready draft (seq stripped)."""
    event = _fixture_event(name)
    raw = event.model_dump(mode="json")
    raw.pop("seq")
    draft_model: Any = EventDraft  # runtime-parametrized generic; mypy stays out
    return draft_model[type(event.data)].model_validate(raw)


def _draft_from_raw(raw: dict[str, Any]) -> EventDraft[Any]:
    """Validate a (possibly modified) raw envelope into a typed draft."""
    raw.pop("seq", None)
    definition = default_event_registry().resolve_durable(str(raw["type"]), int(raw["version"]))
    draft_model: Any = EventDraft  # runtime-parametrized generic; mypy stays out
    return draft_model[definition.data_model].model_validate(raw)


def _r2_prompt_draft() -> EventDraft[Any]:
    """The R1 prompt fixture re-aimed at the R2 fixture run (run ...0002)."""
    raw = _fixture_raw("events_prompt_accepted_valid.json")
    raw["id"] = "evt_0000000000000000000000000009"
    raw["data"]["request_id"] = "req_0000000000000000000000000002"
    raw["data"]["run_id"] = _R2_RUN
    raw["metadata"] = {"run_id": _R2_RUN, "request_id": "req_0000000000000000000000000002"}
    return _draft_from_raw(raw)


def _r2_draft(key: str, *, event_id: str | None = None) -> EventDraft[Any]:
    """The fixture draft for one R2 stage, optionally under a fresh event id."""
    draft = _fixture_draft(dict(_R2_FIXTURES)[key])
    if event_id is not None:
        draft = draft.model_copy(update={"id": event_id})
    return draft


def _rows(db_path: Path, statement: str) -> list[tuple[Any, ...]]:
    return _fetch_all(db_path, statement)


async def _seed_created_and_prompt(store: SessionStore) -> int:
    """Commit created + the R2 prompt; returns H (the prompt event's seq)."""
    created = _fixture_draft("events_session_created_valid.json")
    committed = await store.commit(_R2_SESSION, EventBatch("batch_r2_created", (created,)), 0)
    prompt = _r2_prompt_draft()
    committed = await store.commit(_R2_SESSION, EventBatch("batch_r2_prompt", (prompt,)), 1)
    return committed[-1].seq


async def test_r2_full_lifecycle_projects_all_four_faces(tmp_path: Path) -> None:
    """The legal 13-event fixture log: staged commits walk the run/step/tool
    tables row by row; idle lands, the active slot releases, the terminal
    assistant message replaces the generated one, the tool execution row
    advances pending -> running -> succeeded."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)
        assert _rows(db_path, f"SELECT status, request_id, error FROM runs WHERE run_id = '{_R2_RUN}'") == [
            ("accepted", "req_0000000000000000000000000002", None)
        ]

        # run.started: accepted -> running.
        high = (await store.commit(_R2_SESSION, EventBatch("b_run_started", (_r2_draft("run_started"),)), high))[-1].seq
        assert _rows(db_path, f"SELECT status FROM runs WHERE run_id = '{_R2_RUN}'") == [("running",)]

        # step.started + generation batch: the assistant message row appears
        # at step.generated with the pending tool plan registered.
        for key, batch_id in (
            ("step_started", "b_step_started"),
            ("text_finalized", "b_text_finalized"),
            ("reasoning_finalized", "b_reasoning_finalized"),
            ("step_generated", "b_step_generated"),
        ):
            high = (await store.commit(_R2_SESSION, EventBatch(batch_id, (_r2_draft(key),)), high))[-1].seq
        generated = _fixture_event("events_step_generated_valid.json")
        expected_generated_seq = high  # the loop ended on step.generated (seq 7)
        assert _rows(
            db_path,
            "SELECT message_id, seq, data FROM messages WHERE message_id = 'msg_0000000000000000000000000012'",
        ) == [
            (
                "msg_0000000000000000000000000012",
                expected_generated_seq,
                canonical_json(generated.data.message.model_dump(mode="json")),
            )
        ]
        assert _rows(
            db_path,
            "SELECT execution_id, run_id, step_id, message_id, tool_call_id, input_hash, state FROM tool_executions",
        ) == [
            (
                "exec_0000000000000000000000000001",
                _R2_RUN,
                "step_0000000000000000000000000002",
                "msg_0000000000000000000000000012",
                "call_0000000000000000000000000001",
                "5b8160c3ef7a2aa5fc1207a2740b1d783068c96fd850a1bbb969107e32c2d625",
                "pending",
            )
        ]

        # tool.requested (pending -> pending), tool.started (-> running),
        # tool.finished (-> succeeded): only the state column may move.
        high = (await store.commit(_R2_SESSION, EventBatch("b_tool_requested", (_r2_draft("tool_requested"),)), high))[
            -1
        ].seq
        assert _rows(db_path, "SELECT state FROM tool_executions") == [("pending",)]
        high = (await store.commit(_R2_SESSION, EventBatch("b_tool_started", (_r2_draft("tool_started"),)), high))[
            -1
        ].seq
        assert _rows(db_path, "SELECT state FROM tool_executions") == [("running",)]
        high = (await store.commit(_R2_SESSION, EventBatch("b_tool_finished", (_r2_draft("tool_finished"),)), high))[
            -1
        ].seq
        assert _rows(db_path, "SELECT state FROM tool_executions") == [("succeeded",)]

        # step.finished: the assistant row keeps its first-appearance seq and
        # its data becomes the self-contained terminal message.
        finished = _fixture_event("events_step_finished_valid.json")
        expected_seq = expected_generated_seq
        high = (await store.commit(_R2_SESSION, EventBatch("b_step_finished", (_r2_draft("step_finished"),)), high))[
            -1
        ].seq
        assert _rows(
            db_path,
            "SELECT message_id, seq, data FROM messages WHERE message_id = 'msg_0000000000000000000000000012'",
        ) == [
            (
                "msg_0000000000000000000000000012",
                expected_seq,
                canonical_json(finished.data.message.model_dump(mode="json")),
            )
        ]

        # cancel_requested: running -> cancelling; run.finished: cancelling ->
        # interrupted, the idle row lands, the active slot releases.
        high = (await store.commit(_R2_SESSION, EventBatch("b_cancel", (_r2_draft("cancel_requested"),)), high))[-1].seq
        assert _rows(db_path, f"SELECT status FROM runs WHERE run_id = '{_R2_RUN}'") == [("cancelling",)]
        assert _rows(db_path, f"SELECT active_run_id FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(_R2_RUN,)]
        high = (await store.commit(_R2_SESSION, EventBatch("b_run_finished", (_r2_draft("run_finished"),)), high))[
            -1
        ].seq
        assert _rows(db_path, f"SELECT status, error FROM runs WHERE run_id = '{_R2_RUN}'") == [("interrupted", None)]
        assert _rows(db_path, f"SELECT active_run_id FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(None,)]
        idle_run_finished = _fixture_event("events_run_finished_valid.json")
        idle = idle_run_finished.data
        expected_idle = canonical_json(
            {
                "id": f"msg_idle_{idle.run_id}",
                "type": "idle",
                "created_at": idle_run_finished.model_dump(mode="json")["created_at"],
                "metadata": {},
                "run_id": idle.run_id,
                "outcome": idle.outcome,
                "error": None,
            }
        )
        assert _rows(db_path, "SELECT message_id, data FROM messages WHERE message_id LIKE 'msg_idle_%'") == [
            (f"msg_idle_{idle.run_id}", expected_idle)
        ]
        assert _rows(db_path, f"SELECT applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(13,)]

    assert _rows(db_path, "SELECT COUNT(*) FROM events") == [(13,)]


async def test_r2_illegal_transitions_are_rejected_with_zero_residue(tmp_path: Path) -> None:
    """§2.1 L39-63 table negatives: every illegal transition is StorageCorrupt
    (reason=illegal_projection_transition), the batch leaves zero residue,
    and the healthy session stays writable after each rejection."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)

        async def commit_legal(key: str) -> None:
            nonlocal high
            high = (await store.commit(_R2_SESSION, EventBatch(f"b_{key}", (_r2_draft(key),)), high))[-1].seq

        async def probe_illegal(raw: dict[str, Any], note: str) -> None:
            before = _rows(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
            with pytest.raises(StorageCorruptError) as error:
                await store.commit(
                    _R2_SESSION, EventBatch(f"n_{abs(hash(note)) % 10**8}", (_draft_from_raw(raw),)), high
                )
            assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
            assert error.value.core.details["reason"] == "illegal_projection_transition", note
            after = _rows(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
            assert after == before, note

        def staged(key: str, *, event_id: str | None = None) -> dict[str, Any]:
            raw = _fixture_raw(dict(_R2_FIXTURES)[key])
            if event_id is not None:
                raw["id"] = event_id
            return raw

        # Anchor 1 (H=2, run accepted): a step cannot start, and an accepted
        # run can only finish interrupted.
        await probe_illegal(
            {**staged("step_started"), "id": "evt_9000000000000000000000000007"}, "step.started from accepted"
        )
        accepted_conflict = staged("run_finished", event_id="evt_9000000000000000000000000006")
        accepted_conflict["data"]["outcome"] = "succeeded"
        await probe_illegal(accepted_conflict, "run.finished succeeded from accepted")

        # Anchor 2 (H=3, run running): run.started twice.
        await commit_legal("run_started")
        await probe_illegal(staged("run_started", event_id="evt_9000000000000000000000000001"), "run.started twice")

        # Anchor 3 (H=4, step running): finalized identity rules.
        await commit_legal("step_started")
        unknown_message = staged("text_finalized", event_id="evt_9000000000000000000000000011")
        unknown_message["data"]["message_id"] = "msg_0000000000000000000000000099"
        await probe_illegal(unknown_message, "finalized for an unknown message")
        await commit_legal("text_finalized")
        ordinal_clash = staged("text_finalized", event_id="evt_9000000000000000000000000012")
        ordinal_clash["data"]["part_id"] = "part_0000000000000000000000000099"
        await probe_illegal(ordinal_clash, "finalized ordinal clash")
        part_clash = staged("text_finalized", event_id="evt_9000000000000000000000000013")
        part_clash["data"]["text"] = "Different text."
        await probe_illegal(part_clash, "finalized part content clash")
        await commit_legal("reasoning_finalized")

        # Anchor 3b: the generation batch is atomic -- a drifted generated
        # event rolls its in-batch finalized back with it (no half batch).
        before = _rows(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
        extra_finalized = staged("text_finalized", event_id="evt_9000000000000000000000000014")
        extra_finalized["data"]["part_id"] = "part_0000000000000000000000000003"
        extra_finalized["data"]["ordinal"] = 2
        extra_finalized["data"]["text"] = "Third part."
        drifted = staged("step_generated", event_id="evt_9000000000000000000000000015")
        drifted["data"]["message"]["parts"][0]["text"] = "Drifted text."
        with pytest.raises(StorageCorruptError) as batch_error:
            await store.commit(
                _R2_SESSION,
                EventBatch("n_half_batch", (_draft_from_raw(extra_finalized), _draft_from_raw(drifted))),
                high,
            )
        assert batch_error.value.core.details["reason"] == "illegal_projection_transition"
        assert _rows(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'") == before

        # Anchor 3c: generated identity rules (text drift, tool_call_id
        # duplication, execution_id reuse).
        drifted_only = staged("step_generated", event_id="evt_9000000000000000000000000016")
        drifted_only["data"]["message"]["parts"][0]["text"] = "Drifted text."
        await probe_illegal(drifted_only, "generated text drift")
        dup_call = staged("step_generated", event_id="evt_9000000000000000000000000017")
        dup_call["data"]["message"]["parts"].append(
            {**dup_call["data"]["message"]["parts"][2], "execution_id": "exec_0000000000000000000000000002"}
        )
        await probe_illegal(dup_call, "tool_call_id duplicated in the plan")
        dup_exec = staged("step_generated", event_id="evt_9000000000000000000000000018")
        dup_exec["data"]["message"]["parts"].append(
            {**dup_exec["data"]["message"]["parts"][2], "tool_call_id": "call_0000000000000000000000000002"}
        )
        await probe_illegal(dup_exec, "execution_id reused in the plan")

        # Anchor 4 (H=7, tool pending): request/finish identity rules and
        # step.finished before the plan terminates.
        await commit_legal("step_generated")
        await probe_illegal(
            staged("run_finished", event_id="evt_9000000000000000000000000031"),
            "run.finished before the step terminated",
        )
        bad_hash = staged("tool_requested", event_id="evt_9000000000000000000000000019")
        bad_hash["data"]["input_hash"] = "0" * 64
        await probe_illegal(bad_hash, "tool.requested input_hash mismatch")
        foreign_run = staged("tool_requested", event_id="evt_9000000000000000000000000020")
        foreign_run["metadata"]["run_id"] = "run_0000000000000000000000000099"
        await probe_illegal(foreign_run, "tool.requested foreign run")
        await probe_illegal(
            staged("tool_finished", event_id="evt_9000000000000000000000000021"),
            "tool.finished succeeded from pending",
        )
        await probe_illegal(
            staged("step_finished", event_id="evt_9000000000000000000000000022"), "step.finished before tool terminal"
        )
        model_drift = staged("step_finished", event_id="evt_9000000000000000000000000023")
        model_drift["data"]["message"]["model"]["model"] = "other-model"
        await probe_illegal(model_drift, "step.finished model drift")

        # Anchor 5 (H=9, tool running): double start, request after start,
        # running -> succeeded/unknown.
        await commit_legal("tool_requested")
        await commit_legal("tool_started")
        await probe_illegal(staged("tool_started", event_id="evt_9000000000000000000000000024"), "tool.started twice")
        await probe_illegal(
            staged("tool_requested", event_id="evt_9000000000000000000000000025"), "tool.requested after start"
        )
        running_unknown = staged("tool_finished", event_id="evt_9000000000000000000000000026")
        running_unknown["data"]["effect_status"] = "unknown"
        await probe_illegal(running_unknown, "tool.finished succeeded/unknown from running")

        # Anchor 6 (H=10, tool terminal): terminals are irreversible.
        await commit_legal("tool_finished")
        other_terminal = staged("tool_finished", event_id="evt_9000000000000000000000000027")
        other_terminal["data"]["state"] = "failed"
        await probe_illegal(other_terminal, "tool terminal conflict")

        # Anchor 7 (H=12, cancelling): cancel once; a terminal run is never
        # overwritten and cannot cancel again.
        await commit_legal("step_finished")
        await commit_legal("cancel_requested")
        await probe_illegal(
            staged("cancel_requested", event_id="evt_9000000000000000000000000028"), "cancel_requested twice"
        )
        await commit_legal("run_finished")
        outcome_conflict = staged("run_finished", event_id="evt_9000000000000000000000000029")
        outcome_conflict["data"]["outcome"] = "succeeded"
        await probe_illegal(outcome_conflict, "run.finished outcome conflict")
        await probe_illegal(
            staged("cancel_requested", event_id="evt_9000000000000000000000000030"), "cancel after run.finished"
        )

        # Every rejection left the log prefix legal: the walk reached 13
        # committed events exactly (the fixture chain, no probe residue).
        assert _rows(db_path, f"SELECT applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(13,)]


async def test_r2_step_finished_without_generated_uses_finalized_text_only(tmp_path: Path) -> None:
    """§3.3 L212: without a generated plan, only the committed finalized text
    composes an interrupted terminal message (tool parts are impossible and
    completed is unreachable)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)
        for key in ("run_started", "step_started", "text_finalized"):
            high = (await store.commit(_R2_SESSION, EventBatch(f"b_{key}", (_r2_draft(key),)), high))[-1].seq

        # A completed message without a generated plan is impossible; a tool
        # part without a generated plan is impossible too (the step is still
        # running, so these hit the no-plan rules, not the terminal replay).
        completed = _fixture_raw("events_step_finished_valid.json")
        completed["id"] = "evt_0000000000000000000000000042"
        completed["data"]["message"]["parts"] = [completed["data"]["message"]["parts"][0]]
        completed["data"]["message"]["status"] = "completed"
        with pytest.raises(StorageCorruptError) as error:
            await store.commit(_R2_SESSION, EventBatch("n_completed_no_plan", (_draft_from_raw(completed),)), high)
        assert error.value.core.details["reason"] == "illegal_projection_transition"
        with_tool = _fixture_raw("events_step_finished_valid.json")
        with_tool["id"] = "evt_0000000000000000000000000043"
        with_tool["data"]["message"]["status"] = "interrupted"
        with pytest.raises(StorageCorruptError) as tool_error:
            await store.commit(_R2_SESSION, EventBatch("n_tool_no_plan", (_draft_from_raw(with_tool),)), high)
        assert tool_error.value.core.details["reason"] == "illegal_projection_transition"

        raw = _fixture_raw("events_step_finished_valid.json")
        raw["id"] = "evt_0000000000000000000000000041"
        raw["data"]["message"] = {
            "id": "msg_0000000000000000000000000012",
            "type": "assistant",
            "created_at": raw["data"]["message"]["created_at"],
            "metadata": {},
            "model": raw["data"]["message"]["model"],
            "parts": [raw["data"]["message"]["parts"][0]],
            "status": "interrupted",
        }
        draft = _draft_from_raw(raw)
        high = (await store.commit(_R2_SESSION, EventBatch("b_interrupted_finish", (draft,)), high))[-1].seq

        message = _fixture_event("events_step_finished_valid.json").data.message.model_copy(
            update={
                "parts": [_fixture_event("events_step_finished_valid.json").data.message.parts[0]],
                "status": "interrupted",
                "finish": None,
                "raw_finish": None,
                "usage": None,
                "cost_usd": None,
                "completed_at": None,
            }
        )
        assert _rows(
            db_path,
            "SELECT message_id, seq, data FROM messages WHERE message_id = 'msg_0000000000000000000000000012'",
        ) == [
            (
                "msg_0000000000000000000000000012",
                high,
                canonical_json(message.model_dump(mode="json")),
            )
        ]


async def test_r2_runs_rows_share_the_event_transaction(tmp_path: Path) -> None:
    """Acceptance A for the runs read model: a projection failure in the same
    batch rolls the runs writes back with the events (no partial rows)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)
        second = _fixture_raw("events_run_started_valid.json")
        second["id"] = "evt_9000000000000000000000000031"
        with pytest.raises(StorageCorruptError) as error:
            await store.commit(
                _R2_SESSION,
                EventBatch("n_runs_same_tx", (_r2_draft("run_started"), _draft_from_raw(second))),
                high,
            )
        assert error.value.core.details["reason"] == "illegal_projection_transition"
        assert _rows(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'") == [(2,)]
        assert _rows(db_path, f"SELECT status FROM runs WHERE run_id = '{_R2_RUN}'") == [("accepted",)]

        # The same request id under a second run id is an identity conflict
        # (unique(session_id, request_id), §2.1 L37 spirit).
        with pytest.raises(StorageCorruptError) as request_error:
            await store.commit(
                _R2_SESSION, EventBatch("n_request_reuse", (_draft_from_raw(_foreign_request_prompt()),)), high
            )
        assert request_error.value.core.details["reason"] == "duplicate_run_projection"


def _foreign_request_prompt() -> dict[str, Any]:
    """The seeded request id re-accepted under a fresh run id (fresh message id)."""
    raw = _fixture_raw("events_prompt_accepted_valid.json")
    raw["id"] = "evt_9000000000000000000000000033"
    raw["data"]["message"]["id"] = "msg_0000000000000000000000000002"
    raw["data"]["request_id"] = "req_0000000000000000000000000002"
    raw["data"]["run_id"] = "run_0000000000000000000000000003"
    raw["metadata"] = {"run_id": "run_0000000000000000000000000003", "request_id": "req_0000000000000000000000000002"}
    return raw


async def test_r1_legacy_log_replays_and_rebuilds_identically(tmp_path: Path) -> None:
    """§0.2 legacy compatibility: an R1 multi-accepted log (created + two
    accepted prompts, distinct runs) folds to the R1 semantics verbatim and
    rebuilds to identical rows; both accepted runs keep accepted rows."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        created = _fixture_draft("events_session_created_valid.json")
        await store.commit(_R2_SESSION, EventBatch("b_c", (created,)), 0)
        first = _fixture_draft("events_prompt_accepted_valid.json")
        high = (await store.commit(_R2_SESSION, EventBatch("b_p1", (first,)), 1))[-1].seq

        second_raw = _fixture_raw("events_prompt_accepted_valid.json")
        second_raw["id"] = "evt_0000000000000000000000000003"
        second_raw["data"]["request_id"] = "req_0000000000000000000000000002"
        second_raw["data"]["run_id"] = "run_0000000000000000000000000003"
        second_raw["data"]["message"]["id"] = "msg_0000000000000000000000000002"
        second_raw["metadata"] = {"run_id": "run_0000000000000000000000000003"}
        high = (await store.commit(_R2_SESSION, EventBatch("b_p2", (_draft_from_raw(second_raw),)), high))[-1].seq
        assert high == 3

        # R1 fold semantics are unchanged: active session, last accepted run
        # in the slot, both user messages in order.
        assert _rows(
            db_path, f"SELECT status, active_run_id, applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'"
        ) == [("active", "run_0000000000000000000000000003", 3)]
        assert _rows(db_path, "SELECT message_id, seq FROM messages ORDER BY seq") == [
            ("msg_0000000000000000000000000001", 2),
            ("msg_0000000000000000000000000002", 3),
        ]
        incremental = _rows(db_path, "SELECT run_id, request_id, status, error FROM runs ORDER BY run_id")

        await store.rebuild(_R2_SESSION)
        assert _rows(
            db_path, f"SELECT status, active_run_id, applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'"
        ) == [("active", "run_0000000000000000000000000003", 3)]
        assert _rows(db_path, "SELECT message_id, seq FROM messages ORDER BY seq") == [
            ("msg_0000000000000000000000000001", 2),
            ("msg_0000000000000000000000000002", 3),
        ]
        assert (
            _rows(db_path, "SELECT run_id, request_id, status, error FROM runs ORDER BY run_id")
            == incremental
            == [
                (
                    "run_0000000000000000000000000001",
                    "req_0000000000000000000000000001",
                    "accepted",
                    None,
                ),
                (
                    "run_0000000000000000000000000003",
                    "req_0000000000000000000000000002",
                    "accepted",
                    None,
                ),
            ]
        )

        # The fold-level legacy replay: same state through the pure fold.
        created_event = _fixture_event("events_session_created_valid.json")
        prompt_event = _fixture_event("events_prompt_accepted_valid.json")
        state = apply(apply(SessionState(), created_event), prompt_event)
        assert state.status is SessionStatus.ACTIVE
        assert state.active_run_id == "run_0000000000000000000000000001"
        assert [message.id for message in state.messages] == ["msg_0000000000000000000000000001"]
        assert set(state.runs) == {"run_0000000000000000000000000001"}


def _r2_prompt_event() -> DurableEvent[Any]:
    """The R1 prompt fixture re-aimed at the R2 fixture run, as a durable event (seq 2)."""
    raw = _fixture_raw("events_prompt_accepted_valid.json")
    raw["id"] = "evt_0000000000000000000000000009"
    raw["data"]["request_id"] = "req_0000000000000000000000000002"
    raw["data"]["run_id"] = _R2_RUN
    raw["metadata"] = {"run_id": _R2_RUN, "request_id": "req_0000000000000000000000000002"}
    return default_event_registry().decode_durable(raw)


def test_r2_fold_idempotent_replays_change_nothing() -> None:
    """Pure fold idempotency (§2.1 L63/L53): identical terminal replays of a
    finalized part, a tool terminal and a run terminal fold to no state
    change beyond applied_seq."""
    created = _fixture_event("events_session_created_valid.json")
    prompt = _r2_prompt_event()
    events = [_fixture_event(dict(_R2_FIXTURES)[key]) for key, _ in _R2_FIXTURES]
    seeded = apply(apply(SessionState(), created), prompt)

    # A finalized part replays idempotently only before py.step.generated
    # closes the batch: walk a pre-generation prefix and replay there.
    pre_generated = seeded
    for event in events[:4]:  # run_started .. reasoning_finalized
        pre_generated = apply(pre_generated, event)
    replay_text = events[2].model_copy(update={"id": "evt_9000000000000000000000000041", "seq": 7})
    folded = apply(pre_generated, replay_text)
    assert folded.finalized == pre_generated.finalized
    assert folded.applied_seq == 7

    # A tool terminal replays identically (idempotent, §2.1 L53).
    state = seeded
    for event in events[:-1]:  # through cancel_requested (H=12)
        state = apply(state, event)
    replay_tool = events[7].model_copy(update={"id": "evt_9000000000000000000000000042", "seq": 14})
    replayed_tool = apply(state, replay_tool)
    assert replayed_tool.tools == state.tools
    assert replayed_tool.applied_seq == 14

    # A terminal step message replays identically (idempotent, §2.1 L63).
    replay_step = events[8].model_copy(update={"id": "evt_9000000000000000000000000044", "seq": 15})
    replayed_step = apply(state, replay_step)
    assert replayed_step.messages == state.messages
    assert replayed_step.steps == state.steps
    assert replayed_step.applied_seq == 15

    # A run terminal replays identically (§2.1 L63).
    state = apply(state, events[10])  # the legal run.finished
    replay_finish = events[10].model_copy(update={"id": "evt_9000000000000000000000000043", "seq": 16})
    replayed = apply(state, replay_finish)
    assert replayed.runs == state.runs
    assert replayed.messages == state.messages
    assert replayed.active_run_id == state.active_run_id
    assert replayed.applied_seq == 16
