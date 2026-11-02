"""Integration test: the §3.2 commit -> notify composition over a real store (R1-S5).

CORE-CONTRACTS §3.2 L159 step 5 (S3-established Python reading): storage stays
bus-free -- the caller composes "commit succeeds first, then notify the
committed durable events by seq". This is the S7 SessionService pattern in
miniature (状态门禁 -> 生成事件 -> commit -> 本地通知) and pins acceptance
A's latter half: the bus never sees uncommitted events. A commit that fails
inside the injected throwing projector (the S4 FailingProjector pattern)
leaves the subscriber with zero events and the database with zero residue;
the healthy retry of the same batch (seq allocation restarts at H+1) then
reaches the live subscriber in seq order, and nothing uncommitted ever
arrives.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.bus import VolatileBus
from opencode.contracts import (
    ConfigSnapshot,
    DurableEvent,
    EventDraft,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
    default_event_registry,
)
from opencode.session import SessionProjector
from opencode.storage import EventBatch, SessionStore, open_database

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _session_created_draft(session_id: str, event_id: str) -> EventDraft[SessionCreatedData]:
    snapshot = ConfigSnapshot.create()
    return EventDraft[SessionCreatedData](
        id=event_id,
        type="py.session.created",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ),
    )


def _prompt_accepted_draft(session_id: str, event_id: str) -> EventDraft[PromptAcceptedData]:
    return EventDraft[PromptAcceptedData](
        id=event_id,
        type="py.prompt.accepted",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id="req_1",
            run_id="run_1",
            message=UserMessage(id="msg_1", type="user", created_at=_CREATED_AT, metadata={}, text="hello"),
        ),
        metadata={"run_id": "run_1"},
    )


class FailingProjector(SessionProjector):
    """Real projector that fails on one event id (fault injection, S4 pattern)."""

    def __init__(self, *, fail_on_event_id: str) -> None:
        self.fail_on_event_id = fail_on_event_id

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        if event.id == self.fail_on_event_id:
            raise RuntimeError(f"injected projector failure at {event.id}")
        await super().apply(connection, event)


async def test_bus_receives_only_committed_events(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"
    batch = EventBatch(
        "batch_1",
        (_session_created_draft("ses_a", "evt_a1"), _prompt_accepted_draft("ses_a", "evt_a2")),
    )
    # The bus validates types against the same registry the store commits by;
    # it receives no storage object at all (zero DB dependencies).
    bus = VolatileBus(registry=default_event_registry())

    async with open_database(db_path, mode="create") as database:
        failing = SessionStore(database.engine, projector=FailingProjector(fail_on_event_id="evt_a2"))
        healthy = SessionStore(database.engine, projector=SessionProjector())

        async with bus.subscribe("ses_a") as events:
            # Phase 1 -- the commit dies inside the projector: §3.2 rolls the
            # whole transaction back, and a commit that never happened must
            # notify nothing (the bus receives no uncommitted events).
            with pytest.raises(RuntimeError, match="injected projector failure"):
                await failing.commit("ses_a", batch, 0)
            assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM events") == (0,)
            assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM sessions") == (0,)
            assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages") == (0,)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)
            assert bus.subscription_count == 1  # the failure is not the subscriber's

            # Phase 2 -- healthy projector, same batch: seq restarts at H+1 and
            # the composition notifies only after the commit returned (§3.2
            # L159 step 5).
            committed = await healthy.commit("ses_a", batch, 0)
            assert [event.seq for event in committed] == [1, 2]
            bus.notify("ses_a", committed)
            received = [await asyncio.wait_for(anext(events), timeout=1.0) for _ in range(2)]
            assert received == committed
            first, second = received
            assert isinstance(first, DurableEvent)
            assert isinstance(second, DurableEvent)
            assert [first.seq, second.seq] == [1, 2]
            assert [first.id, second.id] == ["evt_a1", "evt_a2"]

            # Nothing else arrives: the bus saw exactly the committed batch.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(events), timeout=0.05)

    # The composed end state equals a plain S4 commit: A = H = 2, one message.
    assert _fetch_scalar(db_path, "SELECT status, applied_seq FROM sessions WHERE session_id = 'ses_a'") == (
        "active",
        2,
    )
    assert _fetch_scalar(db_path, "SELECT COUNT(*) FROM messages WHERE session_id = 'ses_a'") == (1,)
