"""R3-S1 integration tests: py.permission.* projection + the single-sourced registry.

Covers the T-C1-025 acceptance criteria over CORE-CONTRACTS §3.2/§3.3 and
plan §2 (r3-slices) groups A/C:
- the permissions read model (§3.2 L183: input_hash/scope/expiry/terminal
  state) projects live inside the commit transaction: requested/saved
  INSERT approval/decision rows, replied/closed/revoked/expired UPDATE the
  state column (group C);
- the §2.1 L35 pending -> denied/cancelled faces pair with the approval
  terminal (§5.2 L268, §7 L337/L173): terminals are written once, identical
  replays are idempotent, late replies never override a closed permission;
- replay is a deterministic function of the log: warm incremental batches,
  a cold full replay (fresh projector per commit) and two consecutive
  rebuilds land identical rows (§3.3 连续 rebuild 全等);
- registry single-sourcing (group A): an application-registered durable
  type committed through the writer's registry replays through the SAME
  bound registry -- the R2-S2-review n-1 double-registry drift
  (txn insert passes, projector replay raises stored_event_decode_failed)
  is pinned shut;
- a read_only_diagnostic session rejects permission writes (§3.2.1 L195).

S1 has no protocol-side producer (Policy/approval land in R3-S2), so events
are driven by hand injection: the frozen R2 fixture saga (same
session/run/execution/input_hash) extended with the R3 contract fixtures
and raw-envelope surgery for the producer shapes the fixtures do not carry.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from opencode.contracts import (
    CoreErrorCode,
    DurableEvent,
    EventDefinition,
    EventDraft,
    EventRegistry,
    default_event_registry,
)
from opencode.contracts.events import PY_PERMISSION_REQUESTED
from opencode.session import IllegalTransitionError, SessionProjector, SessionState, apply
from opencode.storage import (
    EventBatch,
    SessionReadOnlyError,
    SessionStore,
    StorageCorruptError,
    open_database,
)
from opencode.storage.txn import epoch_ms
from tests.integration.test_projector import (
    _R2_FIXTURES,
    _R2_SESSION,
    _draft_from_raw,
    _fetch_all,
    _fixture_draft,
    _fixture_event,
    _fixture_raw,
    _r2_prompt_event,
    _require_success,
    _seed_created_and_prompt,
    _upgrade_head,
)

_FIXTURES = {
    "requested": "events_permission_requested_valid.json",
    "replied": "events_permission_replied_valid.json",
    "closed": "events_permission_closed_valid.json",
    "saved": "events_permission_saved_valid.json",
    "revoked": "events_permission_revoked_valid.json",
    "expired": "events_permission_expired_valid.json",
}

_PERMISSION_HASH = "5b8160c3ef7a2aa5fc1207a2740b1d783068c96fd850a1bbb969107e32c2d625"
_PERMISSION_ID = "per_0000000000000000000000000001"
_DECISION_ID = "psv_0000000000000000000000000001"
_DECISION_ID_2 = "psv_0000000000000000000000000002"
_REQUESTED_EXPIRY = epoch_ms(datetime(2026, 11, 3, 8, 5, 20, tzinfo=UTC))
_SAVED_EXPIRY = epoch_ms(datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC))
_RESOURCES_JSON = '["file://D:/work/demo/notes.md"]'
_SCOPE_JSON = '{"action":"write","resources":["file://D:/work/demo/notes.md"]}'

_PERMISSION_ROWS_SQL = (
    "SELECT session_id, permission_id, decision_id, execution_id, action, resources, input_hash, "
    "scope, expires_at, state FROM permissions ORDER BY COALESCE(permission_id, decision_id)"
)


def _permission_rows(db_path: Path) -> list[tuple[Any, ...]]:
    return _fetch_all(db_path, _PERMISSION_ROWS_SQL)


def _permission_raw(kind: str, *, event_id: str | None = None, **data_overrides: Any) -> dict[str, Any]:
    """One permission fixture envelope, optionally under a fresh id / data surgery."""
    raw = _fixture_raw(_FIXTURES[kind])
    if event_id is not None:
        raw["id"] = event_id
    raw["data"].update(data_overrides)
    return raw


def _permission_draft(kind: str, *, event_id: str | None = None, **data_overrides: Any) -> EventDraft[Any]:
    return _draft_from_raw(_permission_raw(kind, event_id=event_id, **data_overrides))


def _permission_event(kind: str, *, event_id: str, seq: int = 90, **data_overrides: Any) -> DurableEvent[Any]:
    """One permission fixture as a foldable durable event under a fresh id."""
    event = _fixture_event(_FIXTURES[kind])
    return event.model_copy(update={"id": event_id, "seq": seq, "data": event.data.model_copy(update=data_overrides)})


def _tool_finished_terminal_raw(state: str, *, event_id: str) -> dict[str, Any]:
    """pending -> denied/cancelled tool.finished, effect_status=not_started (§2.1 L35).

    Only ``state``/``effect_status`` are overridden: the fixture content stays
    (``ToolFinishedData.content`` is min_length=1; the R2 pattern at
    ``test_projector.py:1328`` keeps it too)."""
    raw = _fixture_raw("events_tool_finished_valid.json")
    raw["id"] = event_id
    raw["data"]["state"] = state
    raw["data"]["effect_status"] = "not_started"
    return raw


def _step_finished_terminal_raw(state: str, *, event_id: str) -> dict[str, Any]:
    """The terminal assistant message matching a not_started tool terminal."""
    raw = _fixture_raw("events_step_finished_valid.json")
    raw["id"] = event_id
    part = raw["data"]["message"]["parts"][1]
    assert isinstance(part, dict) and part["type"] == "tool"
    part["state"] = state
    part["effect_status"] = "not_started"
    return raw


def _run_finished_raw(outcome: str, *, event_id: str) -> dict[str, Any]:
    raw = _fixture_raw("events_run_finished_valid.json")
    raw["id"] = event_id
    raw["data"]["outcome"] = outcome
    return raw


async def _seed_pending_tool(store: SessionStore, db_path: Path) -> int:
    """created..step_generated (the tool plan pending); returns H (seq 7)."""
    high = await _seed_created_and_prompt(store)
    high = (
        await store.commit(
            _R2_SESSION, EventBatch("p_run_started", (_fixture_draft(dict(_R2_FIXTURES)["run_started"]),)), high
        )
    )[-1].seq
    batch = EventBatch(
        "p_generation",
        tuple(
            _fixture_draft(dict(_R2_FIXTURES)[key])
            for key in ("step_started", "text_finalized", "reasoning_finalized", "step_generated")
        ),
    )
    high = (await store.commit(_R2_SESSION, batch, high))[-1].seq
    assert _fetch_all(db_path, "SELECT state FROM tool_executions") == [("pending",)]
    return high


def _seeded_approval_state() -> tuple[SessionState, DurableEvent[Any]]:
    """The pure fold through step.generated + permission.requested (all pending)."""
    state = apply(apply(SessionState(), _fixture_event("events_session_created_valid.json")), _r2_prompt_event())
    for key in ("run_started", "step_started", "text_finalized", "reasoning_finalized", "step_generated"):
        state = apply(state, _fixture_event(dict(_R2_FIXTURES)[key]))
    requested = _fixture_event(_FIXTURES["requested"])
    return apply(state, requested), requested


# -- group C: the permissions read model projects inside the commit transaction --


async def test_approval_reject_flow_projects_the_permissions_read_model(tmp_path: Path) -> None:
    """requested -> replied(reject) -> tool denied, then the decision pair:
    the permissions rows carry the §3.2 L183 four fields; the tool takes the
    §2.1 L35 pending -> denied face with effect_status=not_started; the
    denied terminal is written once (identical callback replay is a
    dedupe, a different terminal is refused)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)

        # py.permission.requested: the approval row lands pending.
        high = (await store.commit(_R2_SESSION, EventBatch("p_requested", (_permission_draft("requested"),)), high))[
            -1
        ].seq
        assert _permission_rows(db_path) == [
            (
                _R2_SESSION,
                _PERMISSION_ID,
                None,
                "exec_0000000000000000000000000001",
                "write",
                _RESOURCES_JSON,
                _PERMISSION_HASH,
                None,
                _REQUESTED_EXPIRY,
                "pending",
            )
        ]

        # One batch: the host reject (§5.2 L268 实际 reject 才写 replied) and the
        # denied terminal it produces -- the writer inserts both before
        # projecting, so the second event's pre-state already replays the first.
        reject_batch = EventBatch(
            "p_reject",
            (
                _permission_draft("replied", event_id="evt_0000000000000000000000000801", reply="reject"),
                _draft_from_raw(_tool_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000802")),
            ),
        )
        high = (await store.commit(_R2_SESSION, reject_batch, high))[-1].seq
        assert _fetch_all(db_path, "SELECT state FROM tool_executions") == [("denied",)]
        assert _permission_rows(db_path)[-1][-1] == "replied"

        # Terminal once (§2.1 L35 终态只有一次): the identical callback under a
        # fresh event id is a dedupe (idempotent fold replay, row untouched);
        # a different terminal is StorageCorrupt with zero residue.
        high = (
            await store.commit(
                _R2_SESSION,
                EventBatch(
                    "p_denied_replay",
                    (
                        _draft_from_raw(
                            _tool_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000803")
                        ),
                    ),
                ),
                high,
            )
        )[-1].seq
        assert _fetch_all(db_path, "SELECT state FROM tool_executions") == [("denied",)]
        with pytest.raises(StorageCorruptError) as conflict:
            await store.commit(
                _R2_SESSION,
                EventBatch(
                    "p_denied_conflict",
                    (
                        _draft_from_raw(
                            _tool_finished_terminal_raw("cancelled", event_id="evt_0000000000000000000000000804")
                        ),
                    ),
                ),
                high,
            )
        assert conflict.value.core.details["reason"] == "illegal_projection_transition"

        # The run closes over the denied plan (no cross-entity gate is needed:
        # the fold already holds the tool terminal).
        end_batch = EventBatch(
            "p_step_end",
            (
                _draft_from_raw(_step_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000805")),
                _draft_from_raw(_run_finished_raw("succeeded", event_id="evt_0000000000000000000000000806")),
            ),
        )
        high = (await store.commit(_R2_SESSION, end_batch, high))[-1].seq
        assert _fetch_all(db_path, "SELECT status FROM runs") == [("succeeded",)]
        assert _fetch_all(db_path, f"SELECT active_run_id FROM sessions WHERE session_id = '{_R2_SESSION}'") == [
            (None,)
        ]

        # The decision pair: saved -> revoked, saved (no explicit expiry) -> expired.
        high = (await store.commit(_R2_SESSION, EventBatch("p_saved_1", (_permission_draft("saved"),)), high))[-1].seq
        high = (await store.commit(_R2_SESSION, EventBatch("p_revoked_1", (_permission_draft("revoked"),)), high))[
            -1
        ].seq
        saved_2 = _permission_draft(
            "saved", event_id="evt_0000000000000000000000000807", decision_id=_DECISION_ID_2, expires_at=None
        )
        high = (await store.commit(_R2_SESSION, EventBatch("p_saved_2", (saved_2,)), high))[-1].seq
        expired_2 = _permission_draft(
            "expired", event_id="evt_0000000000000000000000000808", decision_id=_DECISION_ID_2
        )
        high = (await store.commit(_R2_SESSION, EventBatch("p_expired_2", (expired_2,)), high))[-1].seq

        assert _permission_rows(db_path) == [
            (
                _R2_SESSION,
                _PERMISSION_ID,
                None,
                "exec_0000000000000000000000000001",
                "write",
                _RESOURCES_JSON,
                _PERMISSION_HASH,
                None,
                _REQUESTED_EXPIRY,
                "replied",
            ),
            (_R2_SESSION, None, _DECISION_ID, None, None, None, None, _SCOPE_JSON, _SAVED_EXPIRY, "revoked"),
            (_R2_SESSION, None, _DECISION_ID_2, None, None, None, None, _SCOPE_JSON, None, "expired"),
        ]
        assert _fetch_all(db_path, f"SELECT applied_seq FROM sessions WHERE session_id = '{_R2_SESSION}'") == [(high,)]


async def test_closed_flow_is_terminal_once_and_rejects_the_late_reply(tmp_path: Path) -> None:
    """The §7 L337 close shape: closed(cancelled) pairs the tool terminal in
    the same batch, closed-first (§3.3 L212 producer order). A late reply
    after close is refused (§5.2 L268 迟到回复拒绝, 不能覆盖 closed); the
    byte-identical close replays idempotently and any drift is refused."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)
        high = (await store.commit(_R2_SESSION, EventBatch("p_requested", (_permission_draft("requested"),)), high))[
            -1
        ].seq

        close_batch = EventBatch(
            "p_close",
            (
                _permission_draft("closed", event_id="evt_0000000000000000000000000811", outcome="cancelled"),
                _draft_from_raw(_tool_finished_terminal_raw("cancelled", event_id="evt_0000000000000000000000000812")),
            ),
        )
        high = (await store.commit(_R2_SESSION, close_batch, high))[-1].seq
        assert _permission_rows(db_path) == [
            (
                _R2_SESSION,
                _PERMISSION_ID,
                None,
                "exec_0000000000000000000000000001",
                "write",
                _RESOURCES_JSON,
                _PERMISSION_HASH,
                None,
                _REQUESTED_EXPIRY,
                "closed",
            )
        ]
        assert _fetch_all(db_path, "SELECT state FROM tool_executions") == [("cancelled",)]

        # Late reply: refused with zero residue, the session stays writable.
        events_before = _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
        with pytest.raises(StorageCorruptError) as late:
            await store.commit(
                _R2_SESSION,
                EventBatch(
                    "p_late_reply",
                    (_permission_draft("replied", event_id="evt_0000000000000000000000000813", reply="once"),),
                ),
                high,
            )
        assert late.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        assert late.value.core.details["reason"] == "illegal_projection_transition"
        assert _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'") == events_before

        # The identical close under a fresh event id is the §7 L173 dedupe: it
        # commits, the row does not move, applied_seq advances.
        high = (
            await store.commit(
                _R2_SESSION,
                EventBatch(
                    "p_close_replay",
                    (_permission_draft("closed", event_id="evt_0000000000000000000000000814", outcome="cancelled"),),
                ),
                high,
            )
        )[-1].seq
        assert _permission_rows(db_path)[-1][-1] == "closed"

        # Any drift on the closed terminal is a conflict (terminal once).
        for note, overrides in (
            ("different reason", {"reason": "other reason"}),
            ("different outcome", {"outcome": "expired"}),
        ):
            with pytest.raises(StorageCorruptError) as drift:
                await store.commit(
                    _R2_SESSION,
                    EventBatch(
                        f"p_close_drift_{abs(hash(note)) % 10**8}",
                        (_permission_draft("closed", event_id="evt_0000000000000000000000000815", **overrides),),
                    ),
                    high,
                )
            assert drift.value.core.details["reason"] == "illegal_projection_transition"


async def test_decision_lifecycle_transitions_are_terminal_once(tmp_path: Path) -> None:
    """saved registers once; revoked/expired terminate a SAVED decision once;
    expired-after-revoked and the duplicate registration are refused with
    zero residue (session-scoped: no run linkage needed at all)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)

        high = (await store.commit(_R2_SESSION, EventBatch("d_saved", (_permission_draft("saved"),)), high))[-1].seq
        assert _permission_rows(db_path) == [
            (_R2_SESSION, None, _DECISION_ID, None, None, None, None, _SCOPE_JSON, _SAVED_EXPIRY, "saved")
        ]

        async def probe_illegal(kind: str, note: str, **data_overrides: Any) -> None:
            nonlocal high
            before = _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
            with pytest.raises(StorageCorruptError) as error:
                await store.commit(
                    _R2_SESSION,
                    EventBatch(
                        f"d_{abs(hash(note)) % 10**8}",
                        (
                            _permission_draft(
                                kind,
                                event_id=f"evt_0000000000000000000000{900 + len(note) % 100:04d}",
                                **data_overrides,
                            ),
                        ),
                    ),
                    high,
                )
            assert error.value.core.details["reason"] == "illegal_projection_transition", note
            assert _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'") == before

        await probe_illegal("saved", "duplicate registration")
        high = (await store.commit(_R2_SESSION, EventBatch("d_revoked", (_permission_draft("revoked"),)), high))[-1].seq
        assert _permission_rows(db_path)[-1][-1] == "revoked"
        await probe_illegal("revoked", "second revoke different reason", reason="changed my mind")
        await probe_illegal("expired", "expired after revoked")

        # The byte-identical revoke under a fresh event id is the dedupe.
        high = (
            await store.commit(
                _R2_SESSION,
                EventBatch(
                    "d_revoked_replay", (_permission_draft("revoked", event_id="evt_0000000000000000000000000821"),)
                ),
                high,
            )
        )[-1].seq
        assert _permission_rows(db_path)[-1][-1] == "revoked"

        # A second decision with no explicit expiry, expired by the check.
        saved_2 = _permission_draft(
            "saved", event_id="evt_0000000000000000000000000822", decision_id=_DECISION_ID_2, expires_at=None
        )
        high = (await store.commit(_R2_SESSION, EventBatch("d_saved_2", (saved_2,)), high))[-1].seq
        expired_2 = _permission_draft(
            "expired", event_id="evt_0000000000000000000000000823", decision_id=_DECISION_ID_2
        )
        high = (await store.commit(_R2_SESSION, EventBatch("d_expired_2", (expired_2,)), high))[-1].seq
        assert _permission_rows(db_path) == [
            (_R2_SESSION, None, _DECISION_ID, None, None, None, None, _SCOPE_JSON, _SAVED_EXPIRY, "revoked"),
            (_R2_SESSION, None, _DECISION_ID_2, None, None, None, None, _SCOPE_JSON, None, "expired"),
        ]


# -- group C acceptance: replay is a deterministic function of the log ----------


async def test_permission_projection_is_identical_warm_cold_and_across_rebuilds(tmp_path: Path) -> None:
    """Warm incremental batches, a cold full replay (fresh projector per
    commit, so every event re-decodes and re-folds the whole stored prefix)
    and two consecutive rebuilds land identical permissions rows and an
    identical durable log (§3.3 重放=日志确定性函数; 连续 rebuild 全等)."""
    warm_path = tmp_path / "warm.db"
    cold_path = tmp_path / "cold.db"
    _require_success(_upgrade_head(warm_path))
    _require_success(_upgrade_head(cold_path))

    def saga_batches() -> list[tuple[str, tuple[EventDraft[Any], ...]]]:
        return [
            ("s_requested", (_permission_draft("requested"),)),
            (
                "s_reject",
                (
                    _permission_draft("replied", event_id="evt_0000000000000000000000000831", reply="reject"),
                    _draft_from_raw(_tool_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000832")),
                ),
            ),
            (
                "s_decisions",
                (
                    _permission_draft("saved"),
                    _permission_draft("revoked", event_id="evt_0000000000000000000000000833"),
                    _permission_draft(
                        "saved",
                        event_id="evt_0000000000000000000000000834",
                        decision_id=_DECISION_ID_2,
                        expires_at=None,
                    ),
                    _permission_draft(
                        "expired", event_id="evt_0000000000000000000000000000835", decision_id=_DECISION_ID_2
                    ),
                ),
            ),
        ]

    async def commit_pending_tool(store: SessionStore, db_path: Path) -> int:
        return await _seed_pending_tool(store, db_path)

    warm_batches: list[tuple[str, tuple[EventDraft[Any], ...]]] = []
    async with open_database(warm_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await commit_pending_tool(store, warm_path)
        warm_batches = saga_batches()
        for batch_id, drafts in warm_batches:
            batch = EventBatch(batch_id, drafts)
            high = (await store.commit(_R2_SESSION, batch, high))[-1].seq
        warm = _permission_rows(warm_path)
        assert len(warm) == 3

        # Consecutive rebuilds over the same log are byte-identical (and the
        # rebuild re-projects the permissions rows it just cascade-deleted).
        await store.rebuild(_R2_SESSION)
        assert _permission_rows(warm_path) == warm
        await store.rebuild(_R2_SESSION)
        assert _permission_rows(warm_path) == warm

    async with open_database(cold_path, mode="create") as database:
        high = 0
        # A fresh projector per commit: every event takes the full decode+fold
        # replay of the whole stored prefix (the pre-cache behaviour).
        cold_seed = SessionStore(database.engine, projector=SessionProjector())
        high = await commit_pending_tool(cold_seed, cold_path)
        for batch_id, drafts in warm_batches:
            cold = SessionStore(database.engine, projector=SessionProjector())
            batch = EventBatch(batch_id, drafts)
            high = (await cold.commit(_R2_SESSION, batch, high))[-1].seq
    assert _permission_rows(cold_path) == warm

    # The durable log itself is partition-independent (batch bookkeeping excluded).
    columns = "id, seq, type, version, created, data, metadata"
    assert _fetch_all(warm_path, f"SELECT {columns} FROM events ORDER BY seq") == _fetch_all(
        cold_path, f"SELECT {columns} FROM events ORDER BY seq"
    )


# -- group A: registry single-sourcing (R2-S2-review n-1) ----------------------


class _HeartbeatData(BaseModel):
    """Test-local durable data model (an application-registered type)."""

    model_config = ConfigDict(extra="forbid")

    beat: int = Field(ge=0)


def _heartbeat_registry() -> EventRegistry:
    registry = default_event_registry()
    registry.register(EventDefinition(event_type="py.test.heartbeat", durability="durable", data_model=_HeartbeatData))
    return registry


def _heartbeat_raw() -> dict[str, Any]:
    return {
        "id": "evt_0000000000000000000000000061",
        "type": "py.test.heartbeat",
        "version": 1,
        "session_id": _R2_SESSION,
        "created_at": "2026-11-03T08:00:30Z",
        "data": {"beat": 1},
        "metadata": {},
    }


def _draft_via_registry(raw: dict[str, Any], registry: EventRegistry) -> EventDraft[Any]:
    raw.pop("seq", None)
    definition = registry.resolve_durable(str(raw["type"]), int(raw["version"]))
    draft_model: Any = EventDraft  # runtime-parametrized generic; mypy stays out
    return draft_model[definition.data_model].model_validate(raw)


class HeartbeatProjector(SessionProjector):
    """Real projector extended by the application: its private durable type
    projects as a no-op row diff (only the watermark advance)."""

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        if event.type == "py.test.heartbeat":
            await self._advance_applied_seq(connection, event)
            return
        await super().apply(connection, event)


async def test_registry_single_sourcing_replay_decodes_through_the_bound_registry(tmp_path: Path) -> None:
    """The n-1 pin (R2-S2-review; plan §2 group A): an application-registered
    durable type commits through the writer's registry AND replays through
    the same registry bound onto the projector. The drift this removes:
    the module-frozen default registry on the replay side raising
    stored_event_decode_failed over a row the writer just accepted. The
    fold stays closed by design, so the fresh replay still fails on the
    custom type -- but as illegal_projection_transition, proving the
    decode went through the bound registry."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    registry = _heartbeat_registry()

    async with open_database(db_path, mode="create") as database:
        projector = HeartbeatProjector()
        store = SessionStore(database.engine, projector=projector, registry=registry)
        # The writer bound its registry onto the projector at construction.
        assert projector._replay_registry() is registry
        high = (
            await store.commit(
                _R2_SESSION, EventBatch("h_created", (_fixture_draft("events_session_created_valid.json"),)), 0
            )
        )[-1].seq
        high = (
            await store.commit(
                _R2_SESSION, EventBatch("h_beat", (_draft_via_registry(_heartbeat_raw(), registry),)), high
            )
        )[-1].seq
        assert high == 2
        assert _fetch_all(db_path, "SELECT type FROM events ORDER BY seq") == [
            ("py.session.created",),
            ("py.test.heartbeat",),
        ]

    async with open_database(db_path, mode="create") as database:
        plain = SessionProjector()
        store = SessionStore(database.engine, projector=plain, registry=registry)
        assert plain._replay_registry() is registry
        with pytest.raises(StorageCorruptError) as error:
            await store.commit(_R2_SESSION, EventBatch("h_saved", (_permission_draft("saved"),)), high)
        assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
        # Not stored_event_decode_failed: the stored heartbeat decoded through
        # the bound registry; the closed fold is what refuses it.
        assert error.value.core.details["reason"] == "illegal_projection_transition"
        assert error.value.core.details["event_id"] == "evt_0000000000000000000000000061"
        assert error.value.core.details["seq"] == 2
        assert _fetch_all(db_path, "SELECT COUNT(*) FROM events") == [(2,)]


def test_bind_registry_guards() -> None:
    """The bind hook's defensive branches: rebinding a DIFFERENT registry is
    a wiring defect (two stores diverging one projector), binding the same
    registry twice is a no-op, and a never-bound projector lazily falls back
    to the default wire registry."""
    projector = SessionProjector()
    registry = default_event_registry()
    projector.bind_registry(registry)
    projector.bind_registry(registry)  # idempotent rebind of the same registry
    with pytest.raises(RuntimeError, match="already bound"):
        projector.bind_registry(default_event_registry())

    unbound = SessionProjector()
    lazy = unbound._replay_registry()
    assert lazy.resolve_durable(PY_PERMISSION_REQUESTED, 1).event_type == PY_PERMISSION_REQUESTED
    assert unbound._replay_registry() is lazy  # memoized, not rebuilt per call


# -- §3.2.1 read-only discipline over permission writes -------------------------


async def test_diagnostic_session_rejects_permission_writes(tmp_path: Path) -> None:
    """Stored-log damage isolates the session (§3.2.1); the read-only gate
    then refuses a py.permission.saved commit with SessionReadOnly while
    the committed permissions row survives the rolled-back rebuild."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_pending_tool(store, db_path)
        high = (await store.commit(_R2_SESSION, EventBatch("p_requested", (_permission_draft("requested"),)), high))[
            -1
        ].seq
        approval_row = _permission_rows(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE events SET data = '{\"broken\": ' WHERE session_id = ? AND seq = ?",
            (_R2_SESSION, high),
        )

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        with pytest.raises(StorageCorruptError) as error:
            await store.rebuild(_R2_SESSION)
        assert error.value.core.details["reason"] == "stored_event_decode_failed"
        assert _fetch_all(db_path, f"SELECT status FROM sessions WHERE session_id = '{_R2_SESSION}'") == [
            ("read_only_diagnostic",)
        ]
        # The failed rebuild rolled its clearing back: the committed
        # permissions row is untouched (§3.3 no half read model).
        assert _permission_rows(db_path) == approval_row

        with pytest.raises(SessionReadOnlyError) as rejected:
            await store.commit(_R2_SESSION, EventBatch("p_saved", (_permission_draft("saved"),)), high)
        assert rejected.value.core.details["session_id"] == _R2_SESSION


# -- fold-level negatives (direct state.apply probes, the T-C1-020 pattern) ----


def test_fold_approval_terminals_are_first_commit_wins() -> None:
    """§7 L173: for a pending permission only the first committed
    reply/close transition is legal; the identical replay is a dedupe and
    everything else -- the other kind of terminal, a drifted terminal, a
    late reply -- is refused."""
    state, _ = _seeded_approval_state()

    closed = apply(state, _permission_event("closed", event_id="evt_0000000000000000000000000841"))
    assert closed.permissions[_PERMISSION_ID].state.value == "closed"
    assert closed.permissions[_PERMISSION_ID].outcome == "expired"

    # The byte-identical close replays idempotently (only applied_seq moves).
    replay = apply(closed, _permission_event("closed", event_id="evt_0000000000000000000000000842", seq=91))
    assert replay.applied_seq == 91
    assert replay.permissions[_PERMISSION_ID] == closed.permissions[_PERMISSION_ID]

    # A late reply after close (§5.2 L268) and any drifted close are refused.
    with pytest.raises(IllegalTransitionError, match="late reply cannot"):
        apply(closed, _permission_event("replied", event_id="evt_0000000000000000000000000843"))
    with pytest.raises(IllegalTransitionError, match="already closed"):
        apply(closed, _permission_event("closed", event_id="evt_0000000000000000000000000844", reason="drift"))
    with pytest.raises(IllegalTransitionError, match="already closed"):
        apply(closed, _permission_event("closed", event_id="evt_0000000000000000000000000845", outcome="cancelled"))

    # replied is terminal too: close-after-reply and the drifted reply are refused.
    replied = apply(state, _permission_event("replied", event_id="evt_0000000000000000000000000846"))
    assert replied.permissions[_PERMISSION_ID].reply == "once"
    idempotent = apply(replied, _permission_event("replied", event_id="evt_0000000000000000000000000847", seq=91))
    assert idempotent.permissions[_PERMISSION_ID] == replied.permissions[_PERMISSION_ID]
    with pytest.raises(IllegalTransitionError, match="late reply cannot"):
        apply(replied, _permission_event("replied", event_id="evt_0000000000000000000000000848", reply="reject"))
    with pytest.raises(IllegalTransitionError, match="already replied"):
        apply(replied, _permission_event("closed", event_id="evt_0000000000000000000000000000849", outcome="cancelled"))


def test_fold_decision_terminals_and_registration_gates() -> None:
    """Decision registration and terminals: saved registers a fresh id once;
    revoked/expired are written once, cross-terminal writes and drifted
    reasons are refused, unknown ids never resolve."""
    state, _ = _seeded_approval_state()

    saved = apply(state, _permission_event("saved", event_id="evt_0000000000000000000000000851"))
    assert saved.decisions[_DECISION_ID].state.value == "saved"
    assert saved.decisions[_DECISION_ID].expires_at is not None

    with pytest.raises(IllegalTransitionError, match="already registered"):
        apply(saved, _permission_event("saved", event_id="evt_0000000000000000000000000852"))
    with pytest.raises(IllegalTransitionError, match="unknown decision"):
        apply(saved, _permission_event("revoked", event_id="evt_0000000000000000000000000853", decision_id="psv_9999"))

    revoked = apply(saved, _permission_event("revoked", event_id="evt_0000000000000000000000000854"))
    assert revoked.decisions[_DECISION_ID].reason == "host revoked the saved rule"
    idempotent = apply(revoked, _permission_event("revoked", event_id="evt_0000000000000000000000000855", seq=91))
    assert idempotent.decisions[_DECISION_ID] == revoked.decisions[_DECISION_ID]
    with pytest.raises(IllegalTransitionError, match="already revoked"):
        apply(revoked, _permission_event("expired", event_id="evt_0000000000000000000000000856"))
    with pytest.raises(IllegalTransitionError, match="already revoked"):
        apply(revoked, _permission_event("revoked", event_id="evt_0000000000000000000000000857", reason="drift"))

    expired = apply(saved, _permission_event("expired", event_id="evt_0000000000000000000000000858"))
    with pytest.raises(IllegalTransitionError, match="already expired"):
        apply(expired, _permission_event("revoked", event_id="evt_0000000000000000000000000859"))


def test_fold_permission_ownership_gates() -> None:
    """requested pins its execution (PENDING tool + input_hash + one approval
    per execution + run RUNNING + envelope run_id); replied/closed enforce
    the envelope run_id against the pinned run."""
    state, _ = _seeded_approval_state()
    pre_requested = apply(  # undo: rebuild the state before the approval
        apply(SessionState(), _fixture_event("events_session_created_valid.json")), _r2_prompt_event()
    )
    for key in ("run_started", "step_started", "text_finalized", "reasoning_finalized", "step_generated"):
        pre_requested = apply(pre_requested, _fixture_event(dict(_R2_FIXTURES)[key]))

    requested = _permission_event("requested", event_id="evt_0000000000000000000000000861")
    with pytest.raises(IllegalTransitionError, match="already registered"):
        apply(state, requested)
    with pytest.raises(IllegalTransitionError, match="input_hash mismatch"):
        apply(
            pre_requested,
            requested.model_copy(update={"data": requested.data.model_copy(update={"input_hash": "0" * 64})}),
        )
    with pytest.raises(IllegalTransitionError, match="already has permission"):
        apply(
            state,
            requested.model_copy(
                update={
                    "id": "evt_0000000000000000000000000862",
                    "data": requested.data.model_copy(update={"permission_id": "per_0000000000000000000000000002"}),
                }
            ),
        )
    with pytest.raises(IllegalTransitionError, match="requires an active session"):
        apply(SessionState(), requested)

    # The envelope run_id must match the pinned run (the approval trio is
    # fold-scoped even though it is not in RUN_SCOPED_EVENT_TYPES).
    replied = _permission_event("replied", event_id="evt_0000000000000000000000000863")
    assert replied.metadata["run_id"]
    wrong_run = replied.model_copy(update={"metadata": {"run_id": "run_0000000000000000000000000099"}})
    with pytest.raises(IllegalTransitionError, match="does not match run"):
        apply(state, wrong_run)
    no_run = replied.model_copy(update={"metadata": {}})
    with pytest.raises(IllegalTransitionError, match="does not match run"):
        apply(state, no_run)


async def test_store_level_permission_transition_negatives(tmp_path: Path) -> None:
    """The store-level anchor matrix: every illegal permission transition is
    StorageCorrupt (illegal_projection_transition) with zero residue and a
    still-writable session -- including the policy-deny shape (tool denied
    with the approval still pending, §5.2 L266 策略直接 deny 不伪造宿主回
    复) followed by the late reply after the run ends."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        high = await _seed_created_and_prompt(store)

        async def probe_illegal(draft: EventDraft[Any], note: str) -> None:
            before = _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
            with pytest.raises(StorageCorruptError) as error:
                await store.commit(_R2_SESSION, EventBatch(f"n_{abs(hash(note)) % 10**8}", (draft,)), high)
            assert error.value.core.code == CoreErrorCode.STORAGE_CORRUPT
            assert error.value.core.details["reason"] == "illegal_projection_transition", note
            after = _fetch_all(db_path, f"SELECT COUNT(*) FROM events WHERE session_id = '{_R2_SESSION}'")
            assert after == before, note

        # Anchor A (run accepted, no plan): the approval cannot be asked yet.
        await probe_illegal(_permission_draft("requested"), "requested before the plan exists")

        high = await _seed_pending_tool(store, db_path)

        # Anchor B (tool pending): requested identity rules.
        await probe_illegal(
            _permission_draft("requested", event_id="evt_0000000000000000000000000871", input_hash="0" * 64),
            "requested input_hash mismatch",
        )
        await probe_illegal(
            _permission_draft(
                "requested",
                event_id="evt_0000000000000000000000000872",
                execution_id="exec_0000000000000000000000000002",
            ),
            "requested for an unknown execution",
        )
        high = (await store.commit(_R2_SESSION, EventBatch("n_requested", (_permission_draft("requested"),)), high))[
            -1
        ].seq
        await probe_illegal(
            _permission_draft("requested", event_id="evt_0000000000000000000000000873"),
            "requested twice",
        )
        await probe_illegal(
            _permission_draft(
                "requested",
                event_id="evt_0000000000000000000000000874",
                permission_id="per_0000000000000000000000000002",
            ),
            "second approval for one execution",
        )
        await probe_illegal(
            _permission_draft("replied", event_id="evt_0000000000000000000000000875", permission_id="per_9999"),
            "reply for an unknown permission",
        )
        await probe_illegal(
            _permission_draft("closed", event_id="evt_0000000000000000000000000876", permission_id="per_9999"),
            "close for an unknown permission",
        )
        await probe_illegal(
            _permission_draft("revoked", event_id="evt_0000000000000000000000000877"),
            "revoke without a saved decision",
        )

        # The policy-deny shape: the tool takes denied while the approval is
        # still pending (no cross-entity gate -- §2.1 L53 pairing is producer
        # discipline); the run then closes; the late reply is refused because
        # its run already ended.
        policy_deny = EventBatch(
            "n_policy_deny",
            (
                _draft_from_raw(_tool_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000878")),
                _draft_from_raw(_step_finished_terminal_raw("denied", event_id="evt_0000000000000000000000000879")),
                _draft_from_raw(_run_finished_raw("succeeded", event_id="evt_0000000000000000000000000880")),
            ),
        )
        high = (await store.commit(_R2_SESSION, policy_deny, high))[-1].seq
        assert _fetch_all(db_path, "SELECT state FROM tool_executions") == [("denied",)]
        assert _fetch_all(db_path, "SELECT state FROM permissions WHERE permission_id IS NOT NULL") == [("pending",)]
        await probe_illegal(
            _permission_draft("replied", event_id="evt_0000000000000000000000000881", reply="once"),
            "reply after the run ended",
        )

        # The healthy session stays writable after every rejection.
        saved = _permission_draft("saved", event_id="evt_0000000000000000000000000882")
        high = (await store.commit(_R2_SESSION, EventBatch("n_saved", (saved,)), high))[-1].seq
        assert _permission_rows(db_path)[-1][-1] == "saved"
