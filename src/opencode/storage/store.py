"""Unique commit interface over the durable event log (R1-S3, CORE-CONTRACTS §3.2).

:class:`SessionStore` is the single commit path: every durable event of a
session enters storage only through :meth:`SessionStore.commit`, which runs the
full §3.2 sequence in one SQLite write transaction (read H -> write gate ->
idempotency -> H/A invariants -> expected_seq -> ownership/version ->
consecutive seq -> insert -> projector -> verify A=H+n -> commit). The heavy
lifting lives in the S4-private :class:`~opencode.storage.txn.TransactionWriter`
(§3.2: "S4 的事务 writer 仅供此接口调用"); this module is the only public
dependency surface for callers.

The projector is an injected seam (:class:`Projector`, Protocol): the real
read-model projector lands in R1-S4; tests inject a recording fake. The
notification side of §3.2 (post-commit wake) is deliberately absent -- it is
R1-S5 and nothing in this module may depend on a bus (asserted structurally by
tests/integration/test_store_commit.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from opencode.contracts import DurableEvent, EventDraft, EventRegistry, default_event_registry
from opencode.storage.txn import TransactionWriter


@dataclass(frozen=True, slots=True)
class EventBatch:
    """One atomic commit unit: batch id + ordered drafts (+ optional dedupe key).

    The batch id is the idempotency-group key (§3.2: batch id + event ids +
    content); drafts keep their ids across retries. ``dedupe_key``, when set,
    is stored on every event row of the batch and enforced by the
    unique(session_id, dedupe_key) index across batches.
    """

    id: str
    events: tuple[EventDraft[Any], ...]
    dedupe_key: str | None = None


class Projector(Protocol):
    """Read-model projection seam (§3.2): applied inside the commit transaction.

    ``apply`` is called once per inserted event, in batch order, inside the
    same write transaction as the event inserts; it must keep the projection
    consistent with the log, including advancing ``sessions.applied_seq`` to
    the high watermark (the store verifies A=H+n before committing and rolls
    the whole transaction back otherwise). Raising aborts the commit with a
    full rollback and no persisted events. The real projector is R1-S4; R1-S3
    tests inject recording fakes.
    """

    async def apply(self, connection: AsyncConnection, event: DurableEvent[Any]) -> None:
        """Project one durable event onto the read model."""
        ...


class SessionStore:
    """The unique commit interface (§3.2) over one async storage engine.

    One store instance may serve any session: serialization is per session
    (per-session asyncio lock in the writer) plus one process-wide write lock,
    with the OS file lock from :mod:`opencode.storage.db` excluding other
    writer processes.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        projector: Projector,
        registry: EventRegistry | None = None,
    ) -> None:
        self._writer = TransactionWriter(
            engine,
            projector=projector,
            registry=registry if registry is not None else default_event_registry(),
        )

    async def commit(self, session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]:
        """Commit one event batch for a session; returns the durable events.

        ``expected_seq`` is the caller's expected pre-commit high watermark H
        (equality only, never "catch up"): on a healthy log a mismatch raises
        :class:`~opencode.storage.txn.ConflictError`. An exact whole-batch
        retry returns the original events with no re-projection and no
        watermark advance, even with a stale ``expected_seq``. Corruption
        detections (A≠H, seq gaps/duplicates/illegal seq, missing read-model
        row, undecodable stored events) raise
        :class:`~opencode.storage.txn.StorageCorruptError` after the business
        transaction rolled back and the session was isolated
        (§3.2.1: ``sessions.status = read_only_diagnostic`` + sanitized
        diagnostic; subsequent commits fail with
        :class:`~opencode.storage.txn.SessionReadOnlyError` before any
        idempotency replay).
        """
        return await self._writer.commit(session_id, batch, expected_seq)

    async def rebuild(self, session_id: str) -> None:
        """§3.3 management rebuild: restore one session's read model from the log.

        A controlled administrative write, not a §3.2 business commit: one
        transaction under the writer's per-session lock replays the session's
        full log through the S4 fold and replaces the read model only after
        the whole log validates and projects (§3.3: full validation before the
        replacement commits). Any failure rolls the whole transaction back --
        the clearing included, the original projection preserved -- and the
        session is isolated (§3.2.1), with the discovering call raising
        :class:`~opencode.storage.txn.StorageCorruptError`. Rebuild appends no
        events, never advances seq, never notifies, is idempotent (same log ->
        same final state), and carries a surviving row's administrative
        status/diagnostic over verbatim (a read_only_diagnostic session
        rebuilds to a restored but still read-only state, §3.2.1 L196).
        """
        await self._writer.rebuild(session_id)

    async def cleanup_failed(self, session_id: str, run_id: str, reason: str) -> None:
        """S7 management mark: ``sessions.status = cleanup_failed`` (T-C1-022 ④-3).

        Thin delegate to :meth:`TransactionWriter.cleanup_failed` -- one short
        independent transaction scoped to still-active rows, no business
        events, no seq advance, no notify, no asyncio locks (the
        close-timeout call sites never held any). The guard makes repeats and
        already-marked rows no-ops; the store's write gate afterwards answers
        every later commit on this session with
        :class:`~opencode.storage.txn.LifecycleCleanupFailedError`.
        """
        await self._writer.cleanup_failed(session_id, run_id, reason)
