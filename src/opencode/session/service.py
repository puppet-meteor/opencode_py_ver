"""Application command surface (R1-S7, CORE-CONTRACTS §7): SessionService.

:class:`SessionService` is the single entry point for the CLI, same-process
callers and a future HTTP face. R1 implements the §7 L298-312 subset:
``create``/``session_status``/``messages``/``prompt``/``run_status``/``close``
-- ``cancel``/``reply_permission``/``revoke_permission`` are R2+ and no stubs
are left behind (L325: R1 implements neither the run cancel nor the recovery
coordinator). ``prompt`` only persists one ``py.prompt.accepted(version=1)``
and returns the :class:`RunRef`; scheduling, run.started, assistant/idle
messages, provider and tool calls are all R2 (§7 L316).

The §7 L318-323 four-step fixed order (order IS semantics) runs under one
short-lived per-session commit lock (TS ``bus.ts`` KeyedMutex L579 mapping;
the lock is never held across external waits -- local SQLite reads + commit
are the fast path):

1. state gate incl. input validation -- NotFound / SessionReadOnly (always,
   including same-request_id retries: the gate precedes the idempotency
   query, §3.2.1) / LifecycleCleanupFailed / SessionClosed (stored row),
   then the service-level closing gate -> SessionClosed;
2. durable idempotency lookup by ``(session_id, dedupe_key)`` where the key
   is exactly ``"prompt:" + request_id`` -- a code-point-identical text
   returns the original RunRef (no new IDs, no re-projection, no re-notify);
   a different text is IdempotencyConflict even while busy;
3. the active_run_id gate -- an occupied slot is SessionBusy;
4. only then are message/run/event/batch IDs and timestamps generated and
   fixed, ``expected_seq`` read as the pre-commit H (§3.2) and the batch
   committed through :class:`opencode.storage.SessionStore`; after a
   successful commit the composition seam notifies the bus (§3.2 L159 step
   5) and the RunRef is returned.

R1 accepted runs hold the active-run slot across close/reopen (§7 L325): no
terminal state, no automatic release; ``close()`` only releases the service's
own resources and never appends cancel/terminal events.

TS mapping (behavioral, not isomorphic): ``Event.ID.create = "evt_" +
ascending()`` (``packages/core/src/event.ts`` L9-14) -- the default ID
factory is time-ordered and lexicographically sortable (no random UUID);
``publish/publishAll`` L511-672 unique-write-path + per-key serialization ->
the per-session asyncio lock plus :class:`SessionStore`; the
``session/projector.ts`` Layer composition (deps [Bus.node, Database.node])
lives in :mod:`opencode.app` (the non-Effect replacement of the
``sqlite.bun.ts`` L100-103 Layer structure).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from opencode.bus import VolatileBus
from opencode.contracts import (
    EVENT_VERSION,
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    CoreErrorCode,
    EventDraft,
    JsonObject,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.session.state import SessionStatus
from opencode.storage import (
    EventBatch,
    IdempotencyConflictError,
    LifecycleCleanupFailedError,
    MessagePage,
    NotFoundError,
    ReadRepositories,
    SessionClosedError,
    SessionReadOnlyError,
    SessionRecord,
    SessionStore,
    StorageCorruptError,
    StoreError,
    StoreValidationError,
)
from opencode.storage.txn import REASON_READ_MODEL_MISSING

#: §3.1 ID patterns enforced on the command surface (session/run identity).
_SES_PREFIX = "ses_"
_RUN_PREFIX = "run_"

#: §7 L321: the prompt dedupe key is exactly ``"prompt:" + request_id``
#: (string concatenation, case sensitive, reusable across sessions, stored
#: on the event row and protected by the unique(session_id, dedupe_key) index).
_DEDUPE_PREFIX = "prompt:"

#: Sanitized reason for a run_status read whose session holds an active-run
#: slot with no matching accepted event (read model / log inconsistency).
REASON_ACTIVE_RUN_WITHOUT_PROMPT = "active_run_without_prompt_event"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


def _utc_now() -> datetime:
    """The default clock: timezone-aware UTC (§1 timestamps are always aware)."""
    return datetime.now(UTC)


def _epoch_ms(value: datetime) -> int:
    """Exact integer epoch milliseconds (mirrors the storage time precision)."""
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


class SessionBusyError(StoreError):
    """The session already holds an active run (§7 L316/L323, code SessionBusy).

    Defined here because no storage module owns it: the busy gate is a §7
    service-protocol rule (the store's write gate covers the §3.2 session
    statuses only). Carries the standard §8 ``.core`` wire shape.
    """

    code = CoreErrorCode.SESSION_BUSY


@dataclass(frozen=True, slots=True)
class SessionRef:
    """§7 L314: ``{session_id}`` -- the created session reference."""

    session_id: str


@dataclass(frozen=True, slots=True)
class RunRef:
    """§7 L314: ``{session_id, run_id, request_id}`` -- no scheduling promise.

    ``session_id``/``run_id`` satisfy the §3.1 ses_/run_ patterns; ``request_id``
    is the caller-given non-empty string (no prefix requirement).
    """

    session_id: str
    run_id: str
    request_id: str


class RunStatusValue(StrEnum):
    """§7 L314 run statuses; R1 can only reach ``accepted``."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class RunStatus:
    """§7 L314: the run identifiers plus ``status`` and the optional error."""

    session_id: str
    run_id: str
    request_id: str
    status: RunStatusValue
    error: str | None = None


#: Injectable clock: returns timezone-aware UTC datetimes (§1).
type Clock = Callable[[], datetime]


class IdFactory(Protocol):
    """Injectable ID factory (§7 step 4 / open point B).

    The four identity prefixes are mandatory (§3.1: evt_/msg_/ses_/run_);
    batch ids are the idempotency-group key and carry no §3.1 prefix
    constraint (open point D). A fixed factory makes service behavior
    deterministic in tests.
    """

    def session_id(self) -> str: ...

    def event_id(self) -> str: ...

    def message_id(self) -> str: ...

    def run_id(self) -> str: ...

    def batch_id(self) -> str: ...


class AscendingIds:
    """Default ID factory: ``<prefix> + <13-digit ms><10-digit counter>``.

    TS ``Event.ID.create = "evt_" + ascending()`` (event.ts L9-14): IDs are
    time-ordered and lexicographically sortable, never random UUIDs -- logs
    sort and tests can assert prefixes. stdlib-only; the counter advances on
    every call so a constant injected clock still yields a strictly
    increasing, deterministic sequence. Uniqueness is per process (Phase 1
    has one writer process per data directory, §3.2); the same-millisecond
    capacity is 10^10 calls.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else _utc_now
        self._last_ms: int = -1
        self._counter: int = 0

    def _next(self) -> str:
        ms = _epoch_ms(self._clock())
        if ms > self._last_ms:
            self._last_ms = ms
            self._counter = 0
        else:
            # Same millisecond: keep counting so IDs stay unique and ascending
            # within the process. That guarantee holds only while the clock
            # never steps backwards: a backwards step can emit a smaller ID
            # than an earlier one (breaking ascending), and oscillation back
            # to an already-seen millisecond can repeat an old (ms, counter)
            # pair (breaking unique). Clamping a backwards clock is R2 work,
            # deliberately not done here.
            self._counter += 1
        return f"{ms:013d}{self._counter:010d}"

    def session_id(self) -> str:
        return _SES_PREFIX + self._next()

    def event_id(self) -> str:
        return "evt_" + self._next()

    def message_id(self) -> str:
        return "msg_" + self._next()

    def run_id(self) -> str:
        return _RUN_PREFIX + self._next()

    def batch_id(self) -> str:
        return "batch_" + self._next()


def _require_session_id_arg(session_id: Any) -> None:
    """§3.1/§7 step 1: session_id must be a non-empty string matching ^ses_."""
    if not isinstance(session_id, str) or not session_id:
        raise StoreValidationError("session_id must be a non-empty string")
    if not session_id.startswith(_SES_PREFIX):
        raise StoreValidationError("session_id must match ^ses_")


def _require_request_id_arg(request_id: Any) -> None:
    """§7 step 1: request_id must be a non-empty string (no prefix required)."""
    if not isinstance(request_id, str) or not request_id:
        raise StoreValidationError("request_id must be a non-empty string")


def _require_run_id_arg(run_id: Any) -> None:
    """§3.1: run ids on the command surface must be non-empty ^run_ strings."""
    if not isinstance(run_id, str) or not run_id:
        raise StoreValidationError("run_id must be a non-empty string")
    if not run_id.startswith(_RUN_PREFIX):
        raise StoreValidationError("run_id must match ^run_")


def prompt_dedupe_key(request_id: str) -> str:
    """§7 L321: the dedupe key is exactly ``"prompt:" + request_id``."""
    return _DEDUPE_PREFIX + request_id


def _coerce_config_snapshot(config_snapshot: ConfigSnapshot | JsonObject) -> ConfigSnapshot:
    """Validate the §6.1 snapshot; an illegal one is ValidationError (§7 create)."""
    if isinstance(config_snapshot, ConfigSnapshot):
        return config_snapshot
    if not isinstance(config_snapshot, dict):
        raise StoreValidationError("config_snapshot must be a ConfigSnapshot or a JSON object")
    try:
        return ConfigSnapshot.model_validate(config_snapshot)
    except PydanticValidationError as error:
        raise StoreValidationError(f"invalid config_snapshot: {error}") from error


class SessionService:
    """The §7 application command entry point (R1 subset) over storage + bus.

    Constructed with the storage components (``SessionStore`` +
    ``ReadRepositories`` over one engine), the ``VolatileBus``, the injected
    ``working_dir`` (open point A: R1 is one service = one process = one CWD;
    the §7 ``create(config_snapshot)`` signature is kept) and optional
    clock/ID factory (open point B). Errors are the storage-exported §8 wire
    classes (open point F); the only locally defined one is
    :class:`SessionBusyError`, which no storage module owns.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        repos: ReadRepositories,
        bus: VolatileBus,
        working_dir: str,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        if not isinstance(working_dir, str) or not working_dir:
            raise StoreValidationError("working_dir must be a non-empty string")
        self._store = store
        self._repos = repos
        self._bus = bus
        self._working_dir = working_dir
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run (the service-level closing gate)."""
        return self._closed

    # -- §7 create ---------------------------------------------------------

    async def create(self, config_snapshot: ConfigSnapshot | JsonObject) -> SessionRef:
        """Create and persist a session (§7 L304); every call is a new ses_ id.

        The §6.1 snapshot is validated through the contracts model (an illegal
        one is ValidationError); ``working_dir`` is the constructor-injected
        value. The single ``py.session.created`` batch commits with
        ``expected_seq=0`` (§3.2: a not-yet-created session's logical H=A=0);
        the projector inserts the sessions row in the same transaction. The
        bus is notified after the commit like every commit path (§3.2 L159
        step 5; open point E -- a session without subscribers is a no-op).
        """
        if self._closed:
            raise SessionClosedError("service is closed")
        snapshot = _coerce_config_snapshot(config_snapshot)
        session_id = self._ids.session_id()
        created_at = self._clock()
        data = SessionCreatedData(
            working_dir=self._working_dir,
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        )
        draft = EventDraft[SessionCreatedData](
            id=self._ids.event_id(),
            type=PY_SESSION_CREATED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=data,
        )
        committed = await self._store.commit(session_id, EventBatch(self._ids.batch_id(), (draft,)), 0)
        self._bus.notify(session_id, committed)
        return SessionRef(session_id=session_id)

    # -- §7 session_status -------------------------------------------------

    async def session_status(self, session_id: str) -> SessionStatus:
        """Read one session's status (§7 L305): no recovery, no external calls.

        Every stored status stays queryable (§3.2.1 "session_status 仍可查",
        ``read_only_diagnostic`` included); an unknown stored status is
        read-model corruption -> StorageCorrupt (fail closed).
        """
        _require_session_id_arg(session_id)
        record = await self._read_record(session_id)
        return SessionStatus(record.status)

    # -- §7 messages ---------------------------------------------------------

    async def messages(
        self,
        session_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 100,
    ) -> MessagePage:
        """Forward to the S6 read repository (§7 L310, ordered cursor pages).

        The cursor must belong to this session (S6 InvalidCursor semantics);
        reads never trigger recovery or external calls.
        """
        _require_session_id_arg(session_id)
        if after_message_id is not None and (not isinstance(after_message_id, str) or not after_message_id):
            raise StoreValidationError("after_message_id must be None or a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise StoreValidationError("limit must be an integer (bools are rejected)")
        return await self._repos.messages(session_id, after_message_id=after_message_id, limit=limit)

    # -- §7 prompt -----------------------------------------------------------

    async def prompt(self, session_id: str, text: str, request_id: str) -> RunRef:
        """Accept one user prompt: persist ``py.prompt.accepted``, return RunRef.

        §7 L318-323 four-step fixed order, the whole sequence under the
        per-session commit lock. State-independent input validation runs
        before the lock is taken (mirroring the store's own
        ``_validate_arguments``-before-lock pattern); every DB-touching step
        (gates, idempotency, H read, commit) runs inside it. Step 4 IDs and
        timestamps are generated and fixed only after steps 1-3 pass; a
        failed/rolled-back commit leaves no request_id, message or active run
        behind (the whole batch rolls back), and a fresh call re-enters from
        step 1 (a stored-batch retry inside one store commit reuses the fixed
        values by construction -- the same drafts go in).
        """
        _require_session_id_arg(session_id)
        _require_request_id_arg(request_id)
        if not isinstance(text, str):
            raise StoreValidationError("text must be a string (no implicit conversion)")
        dedupe_key = prompt_dedupe_key(request_id)

        async with self._session_lock(session_id):
            # -- step 1: state gate (§7 L320) --------------------------------
            record = await self._read_record(session_id)
            status = record.status
            if status == SessionStatus.READ_ONLY_DIAGNOSTIC:
                # Always SessionReadOnly, including same-request_id retries:
                # the gate precedes the idempotency query (§3.2.1).
                raise SessionReadOnlyError(
                    f"session {session_id} is read_only_diagnostic", details={"session_id": session_id}
                )
            if status == SessionStatus.CLEANUP_FAILED:
                raise LifecycleCleanupFailedError(
                    f"session {session_id} failed resource cleanup", details={"session_id": session_id}
                )
            if status == SessionStatus.CLOSED:
                raise SessionClosedError(f"session {session_id} is closed", details={"session_id": session_id})
            # The session is active; only now the service-level closing gate.
            if self._closed:
                raise SessionClosedError("service is closed")

            # -- step 2: durable idempotency (§7 L321-322) ------------------
            accepted = await self._repos.prompt_by_dedupe_key(session_id, dedupe_key)
            if accepted is not None:
                if accepted.message_text == text:
                    # Code-point identical: the original RunRef, no new IDs,
                    # no re-projection, no re-notify, no trim/normalization.
                    return RunRef(session_id=session_id, run_id=accepted.run_id, request_id=accepted.request_id)
                raise IdempotencyConflictError(
                    f"request {request_id!r} was already accepted with a different text",
                    details={"session_id": session_id, "request_id": request_id},
                )

            # -- step 3: active_run_id gate (§7 L323) ------------------------
            if record.active_run_id is not None:
                raise SessionBusyError(
                    f"session {session_id} already holds an active run",
                    details={"session_id": session_id, "active_run_id": record.active_run_id},
                )

            # -- step 4: generate + fix IDs, expected_seq = H, commit, notify -
            run_id = self._ids.run_id()
            created_at = self._clock()
            data = PromptAcceptedData(
                request_id=request_id,
                run_id=run_id,
                message=UserMessage(
                    id=self._ids.message_id(),
                    type="user",
                    created_at=created_at,
                    metadata={},
                    text=text,
                ),
            )
            draft = EventDraft[PromptAcceptedData](
                id=self._ids.event_id(),
                type=PY_PROMPT_ACCEPTED,
                version=EVENT_VERSION,
                session_id=session_id,
                created_at=created_at,
                data=data,
                metadata={"run_id": run_id, "request_id": request_id},
            )
            expected_seq = await self._repos.log_high(session_id)
            committed = await self._store.commit(
                session_id, EventBatch(self._ids.batch_id(), (draft,), dedupe_key), expected_seq
            )
            self._bus.notify(session_id, committed)
            return RunRef(session_id=session_id, run_id=run_id, request_id=request_id)

    # -- §7 run_status -------------------------------------------------------

    async def run_status(self, run_id: str) -> RunStatus:
        """Look up one run (§7 L311): R1 finds it by the active-run slot.

        Open point C: the sessions table's ``active_run_id`` is R1's only run
        face; ``request_id`` comes from the matching accepted prompt's typed
        data. A held slot with no matching event is read-model/log
        inconsistency -> StorageCorrupt (fail closed). A run that is not (or
        no longer) the active run is NotFound in R1 (no run history table
        exists yet).
        """
        _require_run_id_arg(run_id)
        record = await self._repos.session_by_active_run(run_id)
        if record is None:
            raise NotFoundError(f"run {run_id} does not exist", details={"run_id": run_id})
        accepted = await self._repos.prompt_by_run_id(record.session_id, run_id)
        if accepted is None:
            raise StorageCorruptError(
                f"session {record.session_id} holds active run {run_id} without its accepted prompt",
                isolate=True,
                details={
                    "reason": REASON_ACTIVE_RUN_WITHOUT_PROMPT,
                    "session_id": record.session_id,
                    "run_id": run_id,
                },
            )
        return RunStatus(
            session_id=record.session_id,
            run_id=run_id,
            request_id=accepted.request_id,
            status=RunStatusValue.ACCEPTED,
        )

    # -- §7 close -------------------------------------------------------------

    async def close(self) -> None:
        """Release the service's own resources (§7 L312, R1 semantics).

        Sets the service-level closing/closed gate first (afterwards
        ``prompt``/``create`` answer SessionClosed; reads keep working while
        the composition root keeps the engine open). R1 has no scheduler
        tasks, no run recovery (R4) and no service-owned bus subscriptions
        (subscriptions are caller-managed contexts), so the gate plus the
        lock-registry cleanup is the whole resource release; the engine and
        writer lock are owned by :class:`opencode.app.App`, which disposes
        them after this (task ②: service.close -> engine dispose). Repeat
        calls are safe no-ops.
        """
        self._closed = True
        self._locks.clear()

    # -- §3.3 rebuild (management command, not a §7 business method) ------------

    async def rebuild(self, session_id: str) -> None:
        """Rebuild one session's read model from its event log (§3.3, R1-S8).

        A controlled management command, not one of the §7 L298-312 business
        methods (that table has no rebuild row): the single entry point for
        CLI/same-process callers, forwarding to
        :meth:`opencode.storage.SessionStore.rebuild` under this service's
        per-session lock and closing gate. A never-created session (no
        sessions row, no log) is NotFound; a missing sessions row over a
        surviving log is the rebuild target, not corruption -- the read model
        is exactly what rebuild restores, so the ``_read_record`` corruption
        mapping does not apply on this face. A read_only_diagnostic session
        MAY rebuild (data-plane recovery: messages/config/applied_seq come
        back), but the administrative mark survives the rebuild (§3.2.1
        L196) and write commands keep answering SessionReadOnly, including
        same-request_id retries. Rebuild never notifies the bus and never
        appends events (§3.3).
        """
        _require_session_id_arg(session_id)
        async with self._session_lock(session_id):
            if self._closed:
                # Open point E, conservative reading: the service-level gate
                # is uniform with prompt/create -- a closed service answers
                # SessionClosed on every management face too.
                raise SessionClosedError("service is closed")
            record = await self._repos.session(session_id)
            if record is None and await self._repos.log_high(session_id) == 0:
                raise NotFoundError(f"session {session_id} does not exist", details={"session_id": session_id})
            await self._store.rebuild(session_id)

    # -- internals -------------------------------------------------------------

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """Get-or-create the per-session commit lock (sync: no await, no race)."""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def _read_record(self, session_id: str) -> SessionRecord:
        """The step-1 existence + status read: NotFound / corruption mapping.

        A missing sessions row over a surviving log is read-model corruption
        (§3.2: "已有日志却缺失 sessions 行属于读模型缺失"), never a NotFound
        and never a new-session opportunity; a missing row with no log is the
        §7 NotFound. An unknown stored status is corruption on this read path
        (fail closed), matching the write gate's classification.
        """
        record = await self._repos.session(session_id)
        if record is None:
            high = await self._repos.log_high(session_id)
            if high > 0:
                raise StorageCorruptError(
                    f"session {session_id} has an event log but no sessions row",
                    isolate=True,
                    details={"reason": REASON_READ_MODEL_MISSING, "session_id": session_id},
                )
            raise NotFoundError(f"session {session_id} does not exist", details={"session_id": session_id})
        try:
            SessionStatus(record.status)
        except ValueError as error:
            raise StorageCorruptError(
                f"session {session_id} has unknown status {record.status!r}",
                isolate=True,
                details={"session_id": session_id, "status": record.status},
            ) from error
        return record
