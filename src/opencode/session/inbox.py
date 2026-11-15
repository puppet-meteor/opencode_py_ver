"""The durable steering-inbox event face (T-C1-100, P7-S1).

The §3-2 contract-event face behind runtime-session steerability: admission
(``py.inbox.enqueued``), withdrawal (``py.inbox.cancelled``), re-arm
(``py.inbox.delivery_changed``) and consumption (``py.inbox.delivered``)
all commit through this module over the same store/repos/bus/id/clock the
service owns. The split is deliberate: :class:`opencode.session.service.
SessionService` owns the gates, the scheduling and the per-session commit
lock; this module owns the event shapes and batch constructions, so every
committing method here runs with the CALLER holding that lock (the
runner-facing and HTTP-facing wrappers in the service acquire it). The
runner's :class:`~opencode.session.runner.EventCommitter` commits share no
service lock, so every commit re-reads ``log_high`` and retries a bounded
number of times on :class:`ConflictError` -- the same discipline as the
service's cancel-intent commit.

Dedupe note: no batch here carries a logical dedupe key. The store's
unique(session_id, dedupe_key) makes a keyed batch single-row, while the
accept-carrying inbox commits are deliberately multi-event (atomic
admission/promotion); their idempotent retry recognition therefore rides
the service's §3-1 step-2.5 face (the parked-row lookup plus the runs
read model's unique(session_id, request_id) mapping), not a key.

Id note: :class:`opencode.session.service.IdFactory` has no inbox face and
gaining one would break its existing test doubles, so ``inbox_`` ids mint
from the shared ascending factory's batch core (unique, ascending, per
process -- the same guarantees the four §3.1 prefixes carry).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from opencode.bus import VolatileBus
from opencode.contracts import (
    EVENT_VERSION,
    PY_PROMPT_ACCEPTED,
    EventDraft,
    PromptAcceptedData,
    UserMessage,
)
from opencode.contracts.events import (
    PY_INBOX_CANCELLED,
    PY_INBOX_DELIVERED,
    PY_INBOX_DELIVERY_CHANGED,
    PY_INBOX_ENQUEUED,
    InboxCancelledData,
    InboxDeliveredData,
    InboxDeliveryChangedData,
    InboxEnqueuedData,
)
from opencode.storage import EventBatch, ReadRepositories, SessionStore
from opencode.storage.repos import InboxItemRow
from opencode.storage.txn import ConflictError

if TYPE_CHECKING:
    # Runtime cycle guard: the service imports this module at runtime; these
    # two annotation-only names flow back lazily (no import cycle).
    from opencode.session.service import Clock, IdFactory

#: Mirrors the service's commit-attempt bound: the EventCommitter's own
#: commits can advance ``log_high`` between the read and the commit attempt.
_COMMIT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class InboxRef:
    """§4-1 (T-C1-100): the parked-admission wire ref.

    Returned by :meth:`SessionService.prompt` only when the item actually
    parked (a busy session with explicit delivery); an idle session never
    sees this ref -- its item is admitted and started atomically.
    """

    session_id: str
    inbox_id: str
    kind: str
    delivery: str
    request_id: str | None


class SessionInbox:
    """The §3-2 durable inbox committer (T-C1-100, P7-S1).

    Every committing method assumes the caller holds the service's
    per-session lock (ordering: gates -> admission -> consumption); the
    read faces are plain read-model queries. No batch carries a logical
    dedupe key (see the module note); admission/withdrawal/re-arm/
    consumption are single-flight under the lock, and the accept-carrying
    commits' retry recognition is the service's step-2.5 face.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        repos: ReadRepositories,
        bus: VolatileBus,
        ids: IdFactory,
        clock: Clock,
    ) -> None:
        self._store = store
        self._repos = repos
        self._bus = bus
        self._ids = ids
        self._clock = clock

    # -- read faces -------------------------------------------------------------

    async def pending(self, session_id: str) -> list[InboxItemRow]:
        """Every pending inbox row of one session, in enqueue order."""
        return await self._repos.inbox_items(session_id)

    async def pending_by_request(self, session_id: str, request_id: str) -> InboxItemRow | None:
        """The oldest still-pending user item admitted under ``request_id``.

        Synthetic rows carry ``request_id`` ``None`` in the read model, so
        a match here implies a ``user`` item (the prompt step-2.5 face).
        """
        for row in await self._repos.inbox_items(session_id):
            if row.request_id == request_id:
                return row
        return None

    # -- admission (§4-1) ------------------------------------------------------

    def _next_inbox_id(self) -> str:
        """Mint one ``inbox_`` id from the shared ascending factory core."""
        return self._ids.batch_id().replace("batch_", "inbox_", 1)

    async def admit(
        self,
        session_id: str,
        *,
        text: str,
        request_id: str,
        delivery: Literal["steer", "queue"],
        kind: Literal["user", "synthetic"] = "user",
    ) -> InboxRef:
        """Park one item (``py.inbox.enqueued``); the busy-session face.

        The full user turn parks verbatim so the delivered fold is pure
        event replay; ``kind="user"`` records the originating request_id
        (the promotion re-uses it for the paired accepted batch), while
        the ``kind="synthetic"`` seam (the S2 import-delivery entry point)
        parks service-authored text with no request id. compaction/move
        kinds are NOT ported (ticket §3-2, DIFFERENT-BY-DESIGN).
        """
        inbox_id = self._next_inbox_id()
        created_at = self._clock()
        message = UserMessage(
            id=self._ids.message_id(),
            type="user",
            created_at=created_at,
            metadata={},
            text=text,
        )
        data = InboxEnqueuedData(
            inbox_id=inbox_id,
            kind=kind,
            delivery=delivery,
            request_id=request_id if kind == "user" else None,
            message=message,
        )
        draft = EventDraft[InboxEnqueuedData](
            id=self._ids.event_id(),
            type=PY_INBOX_ENQUEUED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=data,
            metadata={"request_id": request_id} if kind == "user" else {},
        )
        await self._commit(session_id, EventBatch(self._ids.batch_id(), (draft,), None))
        return InboxRef(
            session_id=session_id,
            inbox_id=inbox_id,
            kind=kind,
            delivery=delivery,
            request_id=request_id if kind == "user" else None,
        )

    async def admit_and_start(
        self,
        session_id: str,
        *,
        text: str,
        request_id: str,
        delivery: Literal["steer", "queue"],
    ) -> tuple[str, str]:
        """Idle admission: park + accept + deliver in ONE atomic batch (§4-1).

        The session was idle when the caller checked, so the item never
        rests: one commit records the enqueue provenance, the accepted run
        over the parked message (the §7 step-4 shape, same message object)
        and its delivery. No dedupe key (the module note): the batch is
        three rows, and the retry recognition is the service's step-2.5
        face over the runs read model. Returns ``(run_id, request_id)``.
        """
        inbox_id = self._next_inbox_id()
        run_id = self._ids.run_id()
        created_at = self._clock()
        message = UserMessage(
            id=self._ids.message_id(),
            type="user",
            created_at=created_at,
            metadata={},
            text=text,
        )
        enqueued = EventDraft[InboxEnqueuedData](
            id=self._ids.event_id(),
            type=PY_INBOX_ENQUEUED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=InboxEnqueuedData(
                inbox_id=inbox_id,
                kind="user",
                delivery=delivery,
                request_id=request_id,
                message=message,
            ),
            metadata={"request_id": request_id},
        )
        accepted = EventDraft[PromptAcceptedData](
            id=self._ids.event_id(),
            type=PY_PROMPT_ACCEPTED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=PromptAcceptedData(request_id=request_id, run_id=run_id, message=message),
            metadata={"run_id": run_id, "request_id": request_id},
        )
        delivered = EventDraft[InboxDeliveredData](
            id=self._ids.event_id(),
            type=PY_INBOX_DELIVERED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=InboxDeliveredData(inbox_id=inbox_id),
            metadata={"run_id": run_id},
        )
        await self._commit(session_id, EventBatch(self._ids.batch_id(), (enqueued, accepted, delivered), None))
        return run_id, request_id

    async def promote(self, session_id: str, item: InboxItemRow) -> tuple[str, str]:
        """Promote one parked item into a fresh run: accept + deliver, ONE batch.

        The parked message re-enters history verbatim under its own id: the
        accepted draft is §7's step-4 shape over the parked ``UserMessage``,
        and the delivered fold's paired-accepted short-circuit sees the
        identical message id already in history, so the pending row drops
        without a second insert. Synthetic items mint the deterministic
        ``synthetic:{inbox_id}`` request id (§5-4); no dedupe key rides the
        two-row batch (the module note) -- a stored same-batch retry
        replays by batch id, and the user-facing retry recognition is the
        service's step-2.5 face. Returns ``(run_id, request_id)``.
        """
        request_id = item.request_id if item.request_id is not None else f"synthetic:{item.inbox_id}"
        run_id = self._ids.run_id()
        created_at = self._clock()
        accepted = EventDraft[PromptAcceptedData](
            id=self._ids.event_id(),
            type=PY_PROMPT_ACCEPTED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=PromptAcceptedData(request_id=request_id, run_id=run_id, message=item.message),
            metadata={"run_id": run_id, "request_id": request_id},
        )
        delivered = EventDraft[InboxDeliveredData](
            id=self._ids.event_id(),
            type=PY_INBOX_DELIVERED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=InboxDeliveredData(inbox_id=item.inbox_id),
            metadata={"run_id": run_id},
        )
        await self._commit(session_id, EventBatch(self._ids.batch_id(), (accepted, delivered), None))
        return run_id, request_id

    # -- consumption (§3-3) ----------------------------------------------------

    async def deliver_steer(self, session_id: str, run_id: str) -> str | None:
        """Consume the oldest pending steer into the active run (§3-3).

        One ``py.inbox.delivered`` commit (``run_id`` metadata records the
        consuming run for provenance); the delivered fold appends the
        parked user turn to history, so the run's next-step context
        carries it. At most one per call -- the runner calls this once per
        step boundary -- and queue-delivery rows are never touched here.
        Returns the consumed ``inbox_id``, or ``None`` when no steer is
        pending (the common case: one read-model query per step).
        """
        for item in await self._repos.inbox_items(session_id):
            if item.delivery != "steer":
                continue
            created_at = self._clock()
            draft = EventDraft[InboxDeliveredData](
                id=self._ids.event_id(),
                type=PY_INBOX_DELIVERED,
                version=EVENT_VERSION,
                session_id=session_id,
                created_at=created_at,
                data=InboxDeliveredData(inbox_id=item.inbox_id),
                metadata={"run_id": run_id},
            )
            await self._commit(session_id, EventBatch(self._ids.batch_id(), (draft,), None))
            return item.inbox_id
        return None

    # -- §3-6 endpoint faces ---------------------------------------------------

    async def cancel(self, session_id: str, inbox_id: str) -> bool:
        """Withdraw one pending item; ``False`` = no pending row (no-op 200).

        Cancelling an already-delivered or already-cancelled item is the
        endpoint's no-op: no event, no state change, an honest answer.
        """
        if await self._repos.inbox_item(session_id, inbox_id) is None:
            return False
        created_at = self._clock()
        draft = EventDraft[InboxCancelledData](
            id=self._ids.event_id(),
            type=PY_INBOX_CANCELLED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=InboxCancelledData(inbox_id=inbox_id),
            metadata={},
        )
        await self._commit(session_id, EventBatch(self._ids.batch_id(), (draft,), None))
        return True

    async def update_delivery(
        self, session_id: str, inbox_id: str, delivery: Literal["steer", "queue"]
    ) -> InboxItemRow:
        """Flip one pending item's delivery; the updated row is returned.

        An unknown target raises :class:`ConflictError` -- the §3-6 update
        face is NOT a no-op (unlike cancel): a delivered or withdrawn item
        no longer exists to re-arm. A same-value flip commits nothing and
        answers the unchanged row (disclosed: the durable log records only
        real changes).
        """
        row = await self._repos.inbox_item(session_id, inbox_id)
        if row is None:
            raise ConflictError(
                f"inbox item {inbox_id!r} is not pending in session {session_id!r}",
                details={"session_id": session_id, "inbox_id": inbox_id},
            )
        if row.delivery == delivery:
            return row
        created_at = self._clock()
        draft = EventDraft[InboxDeliveryChangedData](
            id=self._ids.event_id(),
            type=PY_INBOX_DELIVERY_CHANGED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=InboxDeliveryChangedData(inbox_id=inbox_id, delivery=delivery),
            metadata={},
        )
        await self._commit(session_id, EventBatch(self._ids.batch_id(), (draft,), None))
        updated = await self._repos.inbox_item(session_id, inbox_id)
        assert updated is not None  # the commit above just re-armed this row
        return updated

    # -- commit plumbing -------------------------------------------------------

    async def _commit(self, session_id: str, batch: EventBatch) -> None:
        """One bounded ConflictError-retry commit + bus notify (§7 L323 parity)."""
        conflict: ConflictError | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            expected_seq = await self._repos.log_high(session_id)
            try:
                committed = await self._store.commit(session_id, batch, expected_seq)
            except ConflictError as error:
                conflict = error
                continue
            self._bus.notify(session_id, committed)
            return
        assert conflict is not None
        raise conflict
