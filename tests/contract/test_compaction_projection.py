"""M6-compaction slice-two projection/fold tests (CORE-CONTRACTS §2.4 L126; T-C1-062).

Face 1 verdict pinned first (the ticket's gating task): the fold does NOT
default-advance ``applied_seq`` for unknown event types -- ``state.apply``
raised ``"event type ... is not foldable"`` and ``SessionProjector.apply``
raised ``"event type ... is not projectable"`` for ``py.context.compacted``
before this slice -- so the event needed an explicit fold branch (the
skills-precedent shape: state carries the record, projector advances A). No
architectural gap: the branch landed in the two authorized faces. The tests:

* A=H over the compacted stream: the REAL ``SessionProjector`` (inside the
  REAL ``SessionStore.commit`` transaction) folds a stream containing
  ``py.context.compacted`` and leaves ``sessions.applied_seq`` == the event
  log's high watermark H (§3.2 L201; the store re-verifies A=H+n).
* The effective view (§2.4 L126): the fold replaces the covered prefix with
  exactly one summary user message and keeps the retained tail (settling seq
  >= ``retained_from``); a second compaction replaces the view again and
  drops the first summary (the None-link keeps the settled read face's
  strictly-ascending seqs intact across passes).
* The ContextBuilder face: ``build(history=folded_messages)`` assembles the
  effective request (summary + retained tail) unchanged -- the replacement
  materializes in the fold, so the builder needs zero modification; a
  post-compaction overflow still raises :class:`ContextLimitExceeded`
  honestly (§7 L339: never silently trimmed).
* 存量兼容: a stream without any compaction event folds to the exact
  pre-M6 shape (the seq linkage rides along, ``compaction`` stays None).
* Negatives: a compaction event over a non-created session is a fold
  ``IllegalTransitionError`` and, through the real projector, a
  ``StorageCorruptError`` (fail closed, no isolation -- projection-time
  rejection against a healthy pre-state).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text as sa_text

from opencode.contracts import (
    ConfigSnapshot,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.events import (
    PY_CONTEXT_COMPACTED,
    ContextCompactedData,
    DurableEvent,
    EventDraft,
)
from opencode.contracts.llm import LLMTextPart, ModelRef, Usage
from opencode.session.context import ContextBuilder, ContextLimitExceeded, RuntimeSettings
from opencode.session.projector import SessionProjector
from opencode.session.state import CompactionInfo, IllegalTransitionError, SessionState, apply
from opencode.storage import EventBatch, SessionStore, StorageCorruptError, open_database

REPO_ROOT = Path(__file__).resolve().parents[2]

_SESSION_ID = "ses_00000000000000000000000000c2"
_T0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="glm-4.6")
_USAGE = Usage(input=2048, output=256)
_SUMMARY_TEXT = "Covered: setup, a file read, and two tool-call turns."


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


def _created(seq: int, *, event_id: str = "evt_c001") -> DurableEvent[SessionCreatedData]:
    return DurableEvent[SessionCreatedData](
        id=event_id,
        type="py.session.created",
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=_SNAPSHOT,
            snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
        ),
        seq=seq,
    )


def _prompt(seq: int, ordinal: int) -> DurableEvent[PromptAcceptedData]:
    """One settled user message (multi-accepted R1-baseline log shape, §0.2)."""
    run_id = f"run_seed_{ordinal:03d}"
    return DurableEvent[PromptAcceptedData](
        id=f"evt_p{ordinal:03d}",
        type="py.prompt.accepted",
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=PromptAcceptedData(
            request_id=f"req_seed_{ordinal:03d}",
            run_id=run_id,
            message=UserMessage(
                id=f"msg_seed_{ordinal:03d}", type="user", created_at=_T0, metadata={}, text=f"turn {ordinal}"
            ),
        ),
        metadata={"run_id": run_id},
        seq=seq,
    )


def _compacted(
    seq: int,
    *,
    covered_seq: int,
    retained_from: int,
    message_count: int,
    event_id: str = "evt_cmp_1",
    summary_text: str = _SUMMARY_TEXT,
) -> DurableEvent[ContextCompactedData]:
    return DurableEvent[ContextCompactedData](
        id=event_id,
        type=PY_CONTEXT_COMPACTED,
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=ContextCompactedData(
            covered_seq=covered_seq,
            retained_from=retained_from,
            summary_text=summary_text,
            summary_hash="b" * 64,
            model=_MODEL,
            usage=_USAGE,
            message_count=message_count,
        ),
        seq=seq,
    )


def _seeded(count: int) -> list[DurableEvent[Any]]:
    """created + ``count`` settled user messages, settling seqs 2..count+1."""
    events: list[DurableEvent[Any]] = [_created(1)]
    for ordinal in range(1, count + 1):
        events.append(_prompt(1 + ordinal, ordinal))
    return events


def _fold(events: list[DurableEvent[Any]]) -> SessionState:
    state = SessionState()
    for event in events:
        state = apply(state, event)
    return state


# ---------------------------------------------------------------------------
# the effective view: summary message + retained tail (§2.4 L126)
# ---------------------------------------------------------------------------


def test_fold_replaces_covered_prefix_with_summary_and_tail() -> None:
    """8 settled messages (seqs 2..9); pass covers seqs 2..4, retained_from=5
    keeps seqs 5..9 -- the view is the summary plus the five-message tail."""
    state = _fold([*_seeded(8), _compacted(10, covered_seq=4, retained_from=5, message_count=3)])

    assert len(state.messages) == 6
    summary = state.messages[0]
    assert isinstance(summary, UserMessage)
    assert summary.id == "msg_summary_evt_cmp_1"
    assert summary.created_at == _T0
    assert summary.text == _SUMMARY_TEXT
    assert summary.metadata == {
        "compaction": {"covered_seq": 4, "retained_from": 5, "summary_hash": "b" * 64, "message_count": 3}
    }
    assert [message.id for message in state.messages[1:]] == [f"msg_seed_{ordinal:03d}" for ordinal in range(4, 9)]
    assert state.message_seqs == (None, 5, 6, 7, 8, 9)
    assert state.compaction == CompactionInfo(covered_seq=4, retained_from=5, summary_hash="b" * 64, message_count=3)
    assert state.applied_seq == 10


def test_second_compaction_replaces_the_view_and_drops_the_first_summary() -> None:
    """Pass two: 8 more settled after pass one (seqs 11..18 over the pass-one
    view's tail 5..9). The new boundary covers the 8 oldest (seqs 5..13),
    retains 14..18 -- the first summary is itself replaced (never retained)."""
    events = [*_seeded(8), _compacted(10, covered_seq=4, retained_from=5, message_count=3)]
    for ordinal in range(9, 17):
        events.append(_prompt(10 + ordinal - 8, ordinal))
    events.append(_compacted(19, covered_seq=13, retained_from=14, message_count=8, event_id="evt_cmp_2"))

    state = _fold(events)

    assert state.messages[0].id == "msg_summary_evt_cmp_2"
    assert [message.id for message in state.messages[1:]] == [f"msg_seed_{ordinal:03d}" for ordinal in range(12, 17)]
    assert state.message_seqs == (None, 14, 15, 16, 17, 18)
    assert state.compaction == CompactionInfo(covered_seq=13, retained_from=14, summary_hash="b" * 64, message_count=8)


def test_fold_without_compaction_is_the_legacy_shape() -> None:
    """存量兼容: no compaction event -> the pre-M6 messages shape rides along."""
    state = _fold(_seeded(8))

    assert [message.id for message in state.messages] == [f"msg_seed_{ordinal:03d}" for ordinal in range(1, 9)]
    assert state.message_seqs == (2, 3, 4, 5, 6, 7, 8, 9)
    assert state.compaction is None


def test_fold_replay_is_deterministic() -> None:
    """The summary message derives from event fields only: two folds agree."""
    events = [*_seeded(8), _compacted(10, covered_seq=4, retained_from=5, message_count=3)]
    assert _fold(events) == _fold(events)


def test_compaction_requires_an_active_session() -> None:
    """Fail closed: a compaction event over a non-created session is illegal."""
    with pytest.raises(IllegalTransitionError) as error:
        _fold([_compacted(1, covered_seq=0, retained_from=1, message_count=0)])
    assert "requires an active session" in str(error.value)


# ---------------------------------------------------------------------------
# the ContextBuilder face: build assembles the effective view unchanged
# ---------------------------------------------------------------------------


def _builder() -> ContextBuilder:
    return ContextBuilder(RuntimeSettings(model=_MODEL, context_limit=100_000, output_reserve=4096, timeout_s=30.0))


def test_build_assembles_summary_plus_retained_tail() -> None:
    """§2.4 L126/L133: from the saved replacement + retained history; the
    summary rides as one user message, the tail verbatim, nothing else."""
    state = _fold([*_seeded(8), _compacted(10, covered_seq=4, retained_from=5, message_count=3)])

    request = _builder().build(request_id="req_build_1", history=state.messages)

    assert [message.role for message in request.messages] == ["user"] * 6
    texts = [part.text for message in request.messages for part in message.content if isinstance(part, LLMTextPart)]
    assert texts == [_SUMMARY_TEXT, *(f"turn {ordinal}" for ordinal in range(4, 9))]


def test_build_still_raises_context_limit_exceeded_after_compaction() -> None:
    """§7 L339 + §2.4: compaction relieves pressure but never silently trims --
    a still-overflowing effective context raises honestly."""
    state = _fold([*_seeded(8), _compacted(10, covered_seq=4, retained_from=5, message_count=3)])

    # The effective view still estimates at ~21 tokens (82 chars / 4); a
    # 16-token input budget cannot hold it -> the honest overflow.
    tight = ContextBuilder(RuntimeSettings(model=_MODEL, context_limit=80, output_reserve=64, timeout_s=30.0))
    with pytest.raises(ContextLimitExceeded):
        tight.build(request_id="req_build_2", history=state.messages)


# ---------------------------------------------------------------------------
# A=H through the real projector (§3.2 L201) -- the gating verdict's test pin
# ---------------------------------------------------------------------------


async def test_projector_advances_applied_seq_over_compacted_stream(tmp_path: Path) -> None:
    """Real SessionStore + real SessionProjector: a stream containing
    ``py.context.compacted`` ends with sessions.applied_seq == log high (A=H),
    the messages read model keeps the ORIGINAL rows (never rewritten,
    §2.4 L133/L134), and the folded effective view replaces the prefix."""
    db_path = tmp_path / "compaction_projection.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        committed: list[DurableEvent[Any]] = []
        drafts: tuple[EventDraft[Any], ...] = (
            EventDraft[SessionCreatedData](
                id="evt_c001",
                type="py.session.created",
                version=1,
                session_id=_SESSION_ID,
                created_at=_T0,
                data=SessionCreatedData(
                    working_dir="C:/work",
                    config_snapshot=_SNAPSHOT,
                    snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
                ),
            ),
            EventDraft[PromptAcceptedData](
                id="evt_p001",
                type="py.prompt.accepted",
                version=1,
                session_id=_SESSION_ID,
                created_at=_T0,
                data=PromptAcceptedData(
                    request_id="req_seed_001",
                    run_id="run_seed_001",
                    message=UserMessage(id="msg_seed_001", type="user", created_at=_T0, metadata={}, text="turn 1"),
                ),
                metadata={"run_id": "run_seed_001"},
            ),
            EventDraft[ContextCompactedData](
                id="evt_cmp_1",
                type=PY_CONTEXT_COMPACTED,
                version=1,
                session_id=_SESSION_ID,
                created_at=_T0,
                data=ContextCompactedData(
                    covered_seq=1,
                    retained_from=2,
                    summary_text=_SUMMARY_TEXT,
                    summary_hash="b" * 64,
                    model=_MODEL,
                    usage=_USAGE,
                    message_count=0,
                ),
            ),
        )
        for draft in drafts:
            committed.extend(
                await store.commit(_SESSION_ID, EventBatch(f"batch_{len(committed):03d}", (draft,)), len(committed))
            )

        # A=H: the projector advanced applied_seq through the compaction event.
        high = committed[-1].seq
        async with database.engine.connect() as connection:
            applied = (
                await connection.execute(
                    sa_text("SELECT applied_seq FROM sessions WHERE session_id = :sid"), {"sid": _SESSION_ID}
                )
            ).scalar_one()
        assert applied == high == 3

        # The original row is never rewritten and the summary never lands in
        # the read model: the effective view lives in the fold only.
        async with database.engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa_text("SELECT message_id FROM messages WHERE session_id = :sid"), {"sid": _SESSION_ID}
                )
            ).fetchall()
        assert [row[0] for row in rows] == ["msg_seed_001"]

        state = _fold(committed)
        assert [message.id for message in state.messages] == ["msg_summary_evt_cmp_1", "msg_seed_001"]
        assert state.message_seqs == (None, 2)
        assert state.compaction is not None
        assert state.compaction.retained_from == 2


async def test_projector_rejects_compaction_without_session_row(tmp_path: Path) -> None:
    """Fail closed: a compaction batch over a non-created session cannot
    project (the fold requires an active session) -> StorageCorruptError."""
    db_path = tmp_path / "compaction_reject.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        draft = EventDraft[ContextCompactedData](
            id="evt_cmp_bad",
            type=PY_CONTEXT_COMPACTED,
            version=1,
            session_id=_SESSION_ID,
            created_at=_T0,
            data=ContextCompactedData(
                covered_seq=0,
                retained_from=1,
                summary_text=_SUMMARY_TEXT,
                summary_hash="b" * 64,
                model=_MODEL,
                usage=_USAGE,
                message_count=0,
            ),
        )
        with pytest.raises(StorageCorruptError):
            await store.commit(_SESSION_ID, EventBatch("batch_bad", (draft,)), 0)
