"""Application command surface (R1-S7 + R2-S7, CORE-CONTRACTS §7): SessionService.

:class:`SessionService` is the single entry point for the CLI, same-process
callers and a future HTTP face. R2-S7 implements ``create``/``session_status``
/``messages``/``prompt``/``run_status``/``cancel``/``close`` --
``reply_permission``/``revoke_permission`` are R3+ and no stubs are left
behind. ``prompt`` persists one ``py.prompt.accepted(version=1)`` and, when a
scheduler is wired, creates the runner task with a strong reference (§7
L331); ``cancel`` lands the cooperative cancel intent (§7 L335);
``run_status`` resolves runs through the runs read model (§3.2 L187, OP-2);
``close`` runs the R2 shutdown lifecycle (§7 L337). The R4 recovery
coordinator (storage-only accepted runs, crash recovery) is deliberately not
implemented here.

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
terminal state, no automatic release. R2 close releases *live scheduled*
runs (cancel_requested + interrupted terminals, §7 L337); a storage-only
accepted run (no live task -- pre-restart leftover or an accept that raced
the closing gate) keeps its records untouched for the R4 recovery coordinator
and is never auto-cancelled here (the L325 R1 rule survives the R2 lifecycle).

TS mapping (behavioral, not isomorphic): ``Event.ID.create = "evt_" +
ascending()`` (``packages/core/src/event.ts`` L9-14) -- the default ID
factory is time-ordered and lexicographically sortable (no random UUID);
``publish/publishAll`` L511-672 unique-write-path + per-key serialization ->
the per-session asyncio lock plus :class:`SessionStore`; the
``session/projector.ts`` Layer composition (deps [Bus.node, Database.node])
lives in :mod:`opencode.app` (the non-Effect replacement of the
``sqlite.bun.ts`` L100-103 Layer structure); ``run-coordinator.ts`` (run task
coordination) maps onto the service-owned ``self._tasks`` strong references
plus the §7 L337 close lifecycle (ADR-0001 decision 1: one coroutine state
machine per run instead of an Effect services graph).
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.bus import VolatileBus
from opencode.contracts import (
    EVENT_VERSION,
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    ConfigSnapshotV2,
    CoreErrorCode,
    EventDraft,
    JsonObject,
    PromptAcceptedData,
    SessionCreatedData,
    SessionCreatedDataV2,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.events import (
    PY_PERMISSION_REVOKED,
    PY_RUN_CANCEL_REQUESTED,
    ConfigSnapshotV3,
    PermissionRevokedData,
    RunCancelRequestedData,
    SessionCreatedDataV3,
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
from opencode.storage.txn import REASON_READ_MODEL_MISSING, ConflictError

if TYPE_CHECKING:
    # Runtime cycle guard (①): the tools package imports this module at
    # runtime (AscendingIds/Clock/IdFactory), so the policy types are
    # annotation-only here; the wired instances flow in through the
    # constructor and are used structurally.
    from opencode.tools.policy import ApprovalCoordinator, PermissionPolicy

#: §3.1 ID patterns enforced on the command surface (session/run identity).
_SES_PREFIX = "ses_"
_RUN_PREFIX = "run_"

#: §7 L321: the prompt dedupe key is exactly ``"prompt:" + request_id``
#: (string concatenation, case sensitive, reusable across sessions, stored
#: on the event row and protected by the unique(session_id, dedupe_key) index).
_DEDUPE_PREFIX = "prompt:"

#: §7 L335: the cancel dedupe key is exactly ``"cancel:" + run_id``.
_CANCEL_DEDUPE_PREFIX = "cancel:"

#: Sanitized reason for a run_status read whose session holds an active-run
#: slot with no matching accepted event (read model / log inconsistency).
REASON_ACTIVE_RUN_WITHOUT_PROMPT = "active_run_without_prompt_event"

#: §3.2 L187/§3.2.1 sanitized reasons for the runs-read-model faces.
REASON_RUN_PROJECTION_MISSING = "run_projection_missing"
REASON_RUN_STATUS_UNKNOWN = "run_status_unknown"
REASON_RUN_OWNER_MISSING = "run_owner_row_missing"
REASON_RUN_ERROR_UNDECODABLE = "run_error_undecodable"
REASON_CANCEL_INTENT_ROW_UNEXPECTED = "cancel_intent_row_unexpected"
REASON_CLEANUP_UNCONFIRMED = "cleanup_unconfirmed"
REASON_CLOSE_TIMEOUT = "close_timeout"

#: §7 L337: default wait for external shutdown (runner tasks, provider
#: stream/adapter) before the timeout branch. The contract fixes the order,
#: not the number (④ open question: the CLI face owns the tunable in R2-S8).
DEFAULT_CLOSE_TIMEOUT_S = 30.0

#: §5.2 L268/L270 sanitized reasons for the service approval faces. These
#: duplicate the canonical constants in ``tools/policy.py`` (same strings):
#: importing that module at runtime here would close the import cycle
#: (tools.invoker imports this module), so the three literals stay local.
REASON_APPROVAL_RUN_CANCELLED = "run cancelled while awaiting approval"
REASON_APPROVAL_SHUTDOWN = "service close while awaiting approval"
REASON_DECISION_REVOKED = "revoked by host"

#: Grace period after a hard task cancel at close-timeout: the runner's
#: CancelledError handler still commits the honest unknown/interrupted
#: terminals through the same store (§7 L337), so the cleanup_failed mark
#: must wait for it before it closes the write gate.
_CLOSE_GRACE_S = 1.0

#: §3.2: bounded ConflictError retries for the cancel-intent commit (the
#: scheduled runner commits concurrently through its own EventCommitter locks,
#: so a stale expected_seq is possible and is retried with the same batch).
_COMMIT_ATTEMPTS = 3

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


class ApprovalExpiredError(StoreError):
    """A reply arrived after the approval expired (§5.2 L268/§7 L173, code ApprovalExpired).

    Defined beside :class:`SessionBusyError` for the same reason: the §7
    approval-protocol race is a service rule no storage module owns. The
    companion late-reply case (over a replied/closed permission) reuses the
    storage-exported :class:`ConflictError` and its registered Conflict code
    (§8 L348); both codes are pre-registered, none is added here.
    """

    code = CoreErrorCode.APPROVAL_EXPIRED


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    """§7 L322: ``{permission_id, decision, execution_id}`` -- an ack, not a token.

    Returned by :meth:`SessionService.reply_permission` before the Invoker
    makes the reply durable (it commits immediately after): the receipt
    confirms the host's input was accepted by the §7 L173 CAS and carries no
    reusable execution permission -- the durable reply belongs to exactly
    one pinned execution_id (① disclosed).
    """

    permission_id: str
    decision: Literal["allow", "deny"]
    execution_id: str


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

    @property
    def is_terminal(self) -> bool:
        return self not in _RUN_STATUS_NON_TERMINAL


#: The statuses a scheduled task may still be occupying (§7 L314; state.py mirrors).
_RUN_STATUS_NON_TERMINAL = frozenset({RunStatusValue.ACCEPTED, RunStatusValue.RUNNING, RunStatusValue.CANCELLING})


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


class RunScheduler(Protocol):
    """The S6 generation-loop face the service schedules onto (§7 L331).

    Structural on purpose: :mod:`opencode.session.runner` imports
    :class:`AscendingIds`/:class:`Clock`/:class:`IdFactory` from this module,
    so the concrete runner can only be referenced here through a protocol
    (the composition root in :mod:`opencode.app` passes the real object).
    ``run`` drives one accepted run to its terminal (an opaque awaitable
    result); ``cancel_event`` is the cooperative in-memory signal the host
    sets only after the durable ``py.run.cancel_requested`` commit lands.
    """

    def run(self, session_id: str, run_id: str) -> Awaitable[object]: ...

    def cancel_event(self, run_id: str) -> asyncio.Event: ...


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


def cancel_dedupe_key(run_id: str) -> str:
    """§7 L335: the cancel dedupe key is exactly ``"cancel:" + run_id``."""
    return _CANCEL_DEDUPE_PREFIX + run_id


def _coerce_config_snapshot(
    config_snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3 | JsonObject,
) -> ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3:
    """Validate the §6.1 snapshot (v1/v2/v3); an illegal one is ValidationError (§7 create).

    A JSON object is routed by its ``schema_version``: 3 decodes through the
    v3 model (R4-S3), 2 through the v2 model, anything else through the
    frozen v1 model (whose error messages are unchanged); a non-dict is
    rejected before all three.
    """
    if isinstance(config_snapshot, ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3):
        return config_snapshot
    if not isinstance(config_snapshot, dict):
        raise StoreValidationError("config_snapshot must be a ConfigSnapshot or a JSON object")
    model: type[ConfigSnapshot] | type[ConfigSnapshotV2] | type[ConfigSnapshotV3]
    if config_snapshot.get("schema_version") == 3:
        model = ConfigSnapshotV3
    elif config_snapshot.get("schema_version") == 2:
        model = ConfigSnapshotV2
    else:
        model = ConfigSnapshot
    try:
        return model.model_validate(config_snapshot)
    except PydanticValidationError as error:
        raise StoreValidationError(f"invalid config_snapshot: {error}") from error


def _decode_run_error(run_id: str, raw: Any) -> str | None:
    """Decode the ``runs.error`` column into the RunStatus error message.

    The projector stores ``canonical_json(CoreError.model_dump())``; the §7
    RunStatus carries only the human-readable ``message``. Anything that does
    not decode into a non-empty message is status corruption, fail closed
    (§3.2.1), never a fabricated error text.
    """
    if raw is None:
        return None
    message: Any = None
    try:
        payload: Any = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
        if isinstance(payload, dict):
            message = payload.get("message")
    except ValueError:
        message = None
    if not isinstance(message, str) or not message:
        raise StorageCorruptError(
            f"run {run_id} has an undecodable error payload",
            isolate=True,
            details={"reason": REASON_RUN_ERROR_UNDECODABLE, "run_id": run_id},
        )
    return message


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
        engine: AsyncEngine | None = None,
        runner: RunScheduler | None = None,
        provider_close: Callable[[], Awaitable[None]] | None = None,
        mcp_close: Callable[[], Awaitable[None]] | None = None,
        approvals: ApprovalCoordinator | None = None,
        policy: PermissionPolicy | None = None,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
    ) -> None:
        if not isinstance(working_dir, str) or not working_dir:
            raise StoreValidationError("working_dir must be a non-empty string")
        if (
            isinstance(close_timeout_s, bool)
            or not isinstance(close_timeout_s, int | float)
            or not close_timeout_s > 0
            or not math.isfinite(close_timeout_s)
        ):
            raise StoreValidationError("close_timeout_s must be a positive finite number of seconds")
        self._store = store
        self._repos = repos
        self._bus = bus
        self._working_dir = working_dir
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        # R2-S7 wiring: the engine is the wiring discriminant only -- it picks
        # the run_status/cancel face, gates cancel, and keeps the R1
        # cleanup-mark no-op; the ④-3 data paths themselves go through the
        # repos/store faces. The runner is the scheduled S6 face (None keeps
        # the R1 behavior); provider_close is the §7 L337 provider shutdown
        # seam.
        self._engine = engine
        self._runner = runner
        self._provider_close = provider_close
        # R4-S2 wiring: the §7 L339 MCP shutdown seam. Closed in Phase 3
        # BEFORE the provider ("依次关闭 MCP、provider、DB"); None keeps the
        # pre-R4 face (no MCP manager in the composition).
        self._mcp_close = mcp_close
        # R3-S2 wiring: the approval channel (§7 L316) and the policy engine
        # holding the current-boot saved decisions (revoke face, §5.2 L270).
        # Both stay optional so the R1/R2 direct-construction faces are
        # unchanged; the App composition passes the same pair it injected
        # into the ToolRegistry.
        self._approvals = approvals
        self._policy = policy
        self._close_timeout_s = float(close_timeout_s)
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        # §7 L331 end: strong references to the scheduled run tasks
        # (run_id -> owning session + task) -- a task the registry drops would
        # be garbage-collected mid-flight.
        self._tasks: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._schedule_errors: dict[str, BaseException] = {}

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has finished its lifecycle (the closed gate).

        The closing gate (``_closing``) is entered synchronously by the first
        close call; write commands consult both, so an in-flight shutdown
        already answers SessionClosed.
        """
        return self._closed

    # -- §7 create ---------------------------------------------------------

    async def create(
        self, config_snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3 | JsonObject
    ) -> SessionRef:
        """Create and persist a session (§7 L304); every call is a new ses_ id.

        The §6.1 snapshot is validated through the contracts model (an illegal
        one is ValidationError); a v1 snapshot commits ``py.session.created``
        version 1, a v2 snapshot version 2 (R3-S6), a v3 snapshot version 3
        (R4-S3; the wire version follows the data shape, §8 L358 — the v3
        branch checks first because a v3 snapshot is a standalone model,
        never a v2 subclass). ``working_dir`` is the constructor-injected
        value. The single ``py.session.created`` batch commits with
        ``expected_seq=0`` (§3.2: a not-yet-created session's logical H=A=0);
        the projector inserts the sessions row in the same transaction. The
        bus is notified after the commit like every commit path (§3.2 L159
        step 5; open point E -- a session without subscribers is a no-op).
        """
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        snapshot = _coerce_config_snapshot(config_snapshot)
        session_id = self._ids.session_id()
        created_at = self._clock()
        draft: EventDraft[SessionCreatedData] | EventDraft[SessionCreatedDataV2] | EventDraft[SessionCreatedDataV3]
        if isinstance(snapshot, ConfigSnapshotV3):
            draft = EventDraft[SessionCreatedDataV3](
                id=self._ids.event_id(),
                type=PY_SESSION_CREATED,
                version=3,
                session_id=session_id,
                created_at=created_at,
                data=SessionCreatedDataV3(
                    working_dir=self._working_dir,
                    config_snapshot=snapshot,
                    snapshot_hash=compute_snapshot_hash(snapshot),
                ),
            )
        elif isinstance(snapshot, ConfigSnapshotV2):
            draft = EventDraft[SessionCreatedDataV2](
                id=self._ids.event_id(),
                type=PY_SESSION_CREATED,
                version=2,
                session_id=session_id,
                created_at=created_at,
                data=SessionCreatedDataV2(
                    working_dir=self._working_dir,
                    config_snapshot=snapshot,
                    snapshot_hash=compute_snapshot_hash(snapshot),
                ),
            )
        else:
            draft = EventDraft[SessionCreatedData](
                id=self._ids.event_id(),
                type=PY_SESSION_CREATED,
                version=EVENT_VERSION,
                session_id=session_id,
                created_at=created_at,
                data=SessionCreatedData(
                    working_dir=self._working_dir,
                    config_snapshot=snapshot,
                    snapshot_hash=compute_snapshot_hash(snapshot),
                ),
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
            if self._closed or self._closing:
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
            # §7 L331: only a freshly accepted request is scheduled (the
            # idempotent return above never reaches here); the strong task
            # reference lives in self._tasks. The closing re-check shares this
            # synchronous block with create_task, so a concurrent close either
            # sees the task in the registry or makes this a no-op.
            self._schedule_run(session_id, run_id)
            return RunRef(session_id=session_id, run_id=run_id, request_id=request_id)

    # -- §7 run_status -------------------------------------------------------

    async def run_status(self, run_id: str) -> RunStatus:
        """Look up one run (§7 L311) through the runs read model (§3.2 L187).

        With the engine wired (the App composition), the run resolves by a
        single-row ``runs`` lookup -- historical terminal runs included -- and
        the normal query never scans the events log (OP-2, T-A-005). A missing
        runs row over a surviving trace (the active-run slot or the idle
        message) is a missing projection; a runs row over a missing sessions
        row is ownership corruption; an unknown status value or an undecodable
        error payload is status corruption -- all StorageCorrupt (§3.2.1),
        never a fabricated RunStatus. Without the engine (R1 direct
        construction) the R1 active-slot face is kept unchanged.
        """
        _require_run_id_arg(run_id)
        if self._engine is None:
            return await self._run_status_by_active_slot(run_id)
        return await self._run_status_by_runs_table(run_id)

    async def _run_status_by_active_slot(self, run_id: str) -> RunStatus:
        """The R1 face (open point C): resolve the run by the active-run slot.

        The sessions table's ``active_run_id`` is R1's only run face;
        ``request_id`` comes from the matching accepted prompt's typed data. A
        held slot with no matching event is read-model/log inconsistency ->
        StorageCorrupt (fail closed). A run that is not (or no longer) the
        active run is NotFound on this face (the runs read model is not
        wired).
        """
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

    async def _run_status_by_runs_table(self, run_id: str) -> RunStatus:
        """§3.2 L187: one PK lookup on ``runs`` + one ownership row check.

        T-C1-022 ④-3: the single-row probes go through the repos faces
        (``run_by_id`` / ``session_id_by_active_run`` /
        ``session_id_by_message_id`` / ``session_owner``); the classification
        (NotFound vs corruption, owner/status decoding, error payload
        decoding) stays here exactly as before the consolidation.
        """
        run = await self._repos.run_by_id(run_id)
        if run is None:
            # Distinguish a never-existed run (NotFound) from a missing
            # projection (StorageCorrupt) by the two surviving traces:
            # the active-run slot and the idle message (msg_idle_{run_id}).
            slot = await self._repos.session_id_by_active_run(run_id)
            idle = await self._repos.session_id_by_message_id(f"msg_idle_{run_id}")
            if slot is not None or idle is not None:
                raise StorageCorruptError(
                    f"run {run_id} has surviving traces but no runs row",
                    isolate=True,
                    details={"reason": REASON_RUN_PROJECTION_MISSING, "run_id": run_id},
                )
            raise NotFoundError(f"run {run_id} does not exist", details={"run_id": run_id})
        session_id = run.session_id
        owner = await self._repos.session_owner(session_id)
        if owner is None:
            raise StorageCorruptError(
                f"run {run_id} belongs to session {session_id} which has no sessions row",
                isolate=True,
                details={"reason": REASON_RUN_OWNER_MISSING, "run_id": run_id, "session_id": session_id},
            )
        try:
            SessionStatus(owner.status)
        except ValueError as error:
            raise StorageCorruptError(
                f"session {session_id} has unknown status {owner.status!r}",
                isolate=True,
                details={"reason": REASON_RUN_OWNER_MISSING, "run_id": run_id, "session_id": session_id},
            ) from error
        try:
            status = RunStatusValue(run.status)
        except ValueError as error:
            raise StorageCorruptError(
                f"run {run_id} has unknown status {run.status!r}",
                isolate=True,
                details={"reason": REASON_RUN_STATUS_UNKNOWN, "run_id": run_id, "status": run.status},
            ) from error
        return RunStatus(
            session_id=session_id,
            run_id=run_id,
            request_id=run.request_id,
            status=status,
            error=_decode_run_error(run_id, run.error),
        )

    # -- §7 cancel (R2-S7) ----------------------------------------------------

    async def cancel(self, run_id: str) -> RunStatus:
        """Cancel one run (§7 L335): one intent, one cleanup wait, honest terminals.

        An already-terminal run returns its original RunStatus unchanged (a
        late cancel never overwrites a committed result). A non-terminal run
        commits exactly one ``py.run.cancel_requested`` under the session
        commit lock (dedupe key ``cancel:run_id``, check-first, so repeated
        cancels never re-commit), then -- outside the lock, after the
        cooperative signal -- awaits the same scheduled task every repeated
        cancel awaits. A task that exits through its own hard cancel is not
        an error here: its CancelledError handler committed the interrupted
        terminal and the re-read reports it; only a cancellation of this
        call itself propagates. A run without a live task (a storage-only
        accept or a pre-restart leftover) keeps its cancelling state for the
        R4 recovery coordinator; a task that already crashed is a cleanup
        that cannot be confirmed: the session is marked ``cleanup_failed``
        and :class:`LifecycleCleanupFailedError` is raised. The runner
        consumes the intent at its step/tool boundaries (an in-flight
        provider stream is not aborted mid-chunk); late completion callbacks
        are deduped by execution id on the S5 invoker face.
        """
        _require_run_id_arg(run_id)
        if self._engine is None:
            # The R1 fallback face cannot observe cancelling honestly; a
            # cancel that returned a fabricated status would lie (④ float).
            raise StoreValidationError("cancel requires the runs read model (engine not wired)")
        current = await self._run_status_by_runs_table(run_id)
        if current.status.is_terminal:
            return current
        session_id = current.session_id
        # R3-S2 §7 L173/L337: set the cooperative signal FIRST -- any Invoker
        # that wakes from here on (via the coordinator close below or a
        # racing host reply) observes it and folds an in-flight allow into
        # the cancelled/not_started pair instead of committing a started
        # the fold rejects on a cancelling run. The durable intent commits
        # while the parked Invoker is still quiet (resolving the approval
        # first would wake its commit burst against this intent's retries);
        # only then does close_run resolve the pending approval -- the
        # Invoker, not this method, commits the paired py.permission.closed
        # + terminal through its own callback (T-B-027 ④-1). close_run is a
        # no-op when nothing is pending, and a pending approval pins the run
        # non-terminal, so the pair is always fold-legal.
        if self._runner is not None:
            self._runner.cancel_event(run_id).set()
        async with self._session_lock(session_id):
            # Re-read under the lock: the scheduled task may have committed
            # its terminal while we waited for the lock (late cancel).
            current = await self._run_status_by_runs_table(run_id)
            if current.status.is_terminal:
                return current
            await self._commit_cancel_intent(session_id, run_id, "user")
        if self._approvals is not None:
            await self._approvals.close_run(run_id, REASON_APPROVAL_RUN_CANCELLED)
        # §7 L313: the lock is released before waiting for the cleanup.
        if self._runner is not None:
            self._runner.cancel_event(run_id).set()
        entry = self._tasks.get(run_id)
        if entry is not None:
            _, task = entry
            try:
                await task
            except asyncio.CancelledError:
                # m-1: a CancelledError surfacing at this await is either the
                # awaited task's own hard cancel (its handler committed the
                # honest interrupted terminal -- fall through and re-read) or
                # this caller's cancellation (the task was NOT cancelled --
                # propagate, never swallow the caller's cancel).
                if not task.cancelled():
                    raise
            final = await self._run_status_by_runs_table(run_id)
            if final.status.is_terminal:
                return final
            await self._fail_cleanup(
                session_id, run_id, REASON_CLEANUP_UNCONFIRMED, "the scheduled task exited without a terminal run"
            )
        crash = self._schedule_errors.get(run_id)
        if crash is not None:
            await self._fail_cleanup(
                session_id,
                run_id,
                REASON_CLEANUP_UNCONFIRMED,
                f"the scheduled task crashed: {type(crash).__name__}",
                cause=crash,
            )
        # No live task: the intent is durable, there are no in-process
        # resources to reclaim, and R4 owns the terminal.
        return await self._run_status_by_runs_table(run_id)

    # -- §7 reply_permission / revoke_permission (R3-S2) ----------------------

    async def reply_permission(self, permission_id: str, reply: str) -> DecisionReceipt:
        """The host approval input channel (§7 L316): CAS-resolve one pending reply.

        Independent of the run/session commit locks by design -- the Invoker
        parks on the approval future without holding them, so a host reply
        can never deadlock against a waiting run (§7 L316 "与运行锁分离").
        The reply vocabulary is once/always/reject (§3.1 L136). The §7 L173
        conditional update runs in the coordinator: the first legal
        transition wins, a late reply maps to
        :class:`ApprovalExpiredError` (the earlier terminal was expiry) or
        :class:`ConflictError` (replied/closed), an unknown permission_id is
        NotFound. A ``read_only_diagnostic`` session's approvals are refused
        here (§3.2.1 L203: the Invoker's paired commit could never land on a
        quarantined session). The returned receipt is an ack of the accepted
        CAS, not a reusable permission: the Invoker commits the durable
        ``py.permission.replied`` (plus ``saved`` for an always) immediately
        after (①).
        """
        if not isinstance(permission_id, str) or not permission_id:
            raise StoreValidationError("permission_id must be a non-empty string")
        if reply not in ("once", "always", "reject"):
            raise StoreValidationError("reply must be one of 'once', 'always', 'reject' (§3.1 L136)")
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        if self._approvals is None:
            raise StoreValidationError("reply_permission requires an approval channel (approvals=)")
        # the membership validation above pins the vocabulary; the str type
        # carries no narrowing, so the literal is restored explicitly
        reply_value = cast(Literal["once", "always", "reject"], reply)
        request = await self._approvals.pending_request(permission_id)
        if request is not None:
            record = await self._read_record(request.session_id)
            if record.status == SessionStatus.READ_ONLY_DIAGNOSTIC:
                raise SessionReadOnlyError(
                    f"session {request.session_id} is read_only_diagnostic",
                    details={"session_id": request.session_id, "permission_id": permission_id},
                )
        resolution = await self._approvals.resolve_reply(permission_id, reply_value)
        if resolution.status == "unknown":
            raise NotFoundError(
                f"permission {permission_id!r} does not exist", details={"permission_id": permission_id}
            )
        if resolution.status == "late":
            assert resolution.outcome is not None
            if resolution.outcome.outcome == "expired":
                raise ApprovalExpiredError(
                    f"permission {permission_id!r} already expired",
                    details={"permission_id": permission_id},
                )
            raise ConflictError(
                f"permission {permission_id!r} is already {resolution.outcome.outcome}",
                details={"permission_id": permission_id, "state": resolution.outcome.outcome},
            )
        # An accepted CAS implies the permission was pending above (register
        # happens exactly once, before the Invoker awaits).
        assert request is not None and resolution.outcome is not None
        decision: Literal["allow", "deny"] = "allow" if reply_value != "reject" else "deny"
        return DecisionReceipt(
            permission_id=permission_id,
            decision=decision,
            execution_id=request.execution_id,
        )

    async def revoke_permission(self, decision_id: str) -> None:
        """Revoke one saved always-decision (§7 L317): deactivate, then append the event.

        Only decisions the current boot registered are revocable through
        this face (the in-memory policy registry is the activation set, §5.2
        L270); a previous boot's audit rows answer NotFound (① disclosed).
        The in-memory deactivation precedes the durable commit (fail-closed
        direction: a failed commit leaves the rule deactivated, never a
        revoked row that still activates). Revocation never changes already
        executed results (§5.2 L270) -- the Invoker's pre-start gate simply
        stops matching the rule. The ``py.permission.revoked`` event commits
        under the session lock with bounded ConflictError retries, like
        every service write.
        """
        if not isinstance(decision_id, str) or not decision_id:
            raise StoreValidationError("decision_id must be a non-empty string")
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        if self._policy is None:
            raise StoreValidationError("revoke_permission requires the permission policy (policy=)")
        decision = self._policy.decision(decision_id)
        if decision is None:
            raise NotFoundError(f"decision {decision_id!r} does not exist", details={"decision_id": decision_id})
        if decision.state != "saved":
            raise ConflictError(
                f"decision {decision_id!r} is already {decision.state}",
                details={"decision_id": decision_id, "state": decision.state},
            )
        record = await self._read_record(decision.session_id)
        if record.status == SessionStatus.READ_ONLY_DIAGNOSTIC:
            raise SessionReadOnlyError(
                f"session {decision.session_id} is read_only_diagnostic",
                details={"session_id": decision.session_id, "decision_id": decision_id},
            )
        deactivated = self._policy.deactivate(decision_id)
        assert deactivated  # the state check above pins saved; deactivate only fails otherwise
        async with self._session_lock(decision.session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._commit_revoked(decision.session_id, decision_id)

    async def _commit_revoked(self, session_id: str, decision_id: str) -> None:
        """Commit one ``py.permission.revoked`` under the session lock (§5.2 L270).

        Bounded ConflictError retries with the same batch (the scheduled
        runner commits concurrently, so a stale expected_seq is retried);
        there is no dedupe key -- repeated revocations are fenced by the
        state gate in :meth:`revoke_permission` and the fold's
        terminal-once rule.
        """
        draft = EventDraft[PermissionRevokedData](
            id=self._ids.event_id(),
            type=PY_PERMISSION_REVOKED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=PermissionRevokedData(decision_id=decision_id, reason=REASON_DECISION_REVOKED),
            metadata={},
        )
        batch = EventBatch(self._ids.batch_id(), (draft,))
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

    # -- §7 close -------------------------------------------------------------

    async def close(self) -> None:
        """The §7 L337 R2 close lifecycle (R1 semantics without a scheduler).

        The first call atomically enters the closing gate (``_closing`` is
        set synchronously; afterwards ``prompt``/``create``/``rebuild``
        answer SessionClosed) and starts the one cleanup task. Concurrent or
        repeated calls await that same task -- the same result or error, never
        a duplicate shutdown event. The lifecycle: commit
        ``py.run.cancel_requested(reason=shutdown)`` for every live scheduled
        run and set its cooperative signal -- a quarantined session is
        skipped, not fatal (A-010: its intent commit raises
        SessionReadOnlyError and close must still drain the other sessions) --
        then resolve every pending approval "closed" (OP-9, T-C1-026: each
        waiting Invoker, its run already cancelling and its signal already
        set, commits the closed-first ``py.permission.closed`` +
        ``tool.finished`` pair, T-B-027 ④-1, and folds the run to
        interrupted), then release the commit locks, wait
        for the scheduled tasks while their terminals still flow through the
        same :meth:`SessionStore.commit`; on timeout hard-cancel the
        stragglers, let their CancelledError handlers commit the honest
        ``tool.finished(effect_status=unknown)`` + interrupted terminals, and
        mark only the sessions still stuck after the grace period
        ``cleanup_failed`` (a grace exit with committed terminals is a
        confirmed cleanup, #90); finally close MCP and the provider (§7
        L339 order) and set the service closed. Storage-only accepted
        runs (no live task) keep their records for R4 and are never
        claimed cleaned.
        The engine and writer lock stay owned by the composition root.
        """
        if self._close_task is not None:
            await self._close_task
            return
        self._closing = True
        self._close_task = asyncio.create_task(self._close_lifecycle())
        await self._close_task

    async def _close_lifecycle(self) -> None:
        """The single cleanup-task body (§7 L337 full order)."""
        # Phase 1: one shutdown intent per live scheduled run through the
        # check-first dedup + ConflictError retry loop (no session lock held:
        # the intent is idempotent by its dedupe key, so "随后释放提交锁"
        # needs no release because none was taken). The task registry
        # snapshot is safe: prompt's scheduling decision is a synchronous
        # block that re-checks _closing, so every task here was registered
        # before the gate and no later one can appear. The intents also land
        # while every approval-waiting Invoker is still parked: resolving
        # those approvals first would wake their [closed, finished] commit
        # bursts against these very retries.
        scheduled = list(self._tasks.items())
        for _run_id, (session_id, _task) in scheduled:
            try:
                await self._commit_cancel_intent(session_id, _run_id, "shutdown")
            except SessionReadOnlyError:
                # ④-4 (A-010): a session quarantined mid-run must not break
                # the close of every other session -- the quarantine already
                # fenced its writes, so its intent is skipped and the signal
                # still fires (the runner drains on its own terminal path).
                pass
            if self._runner is not None:
                self._runner.cancel_event(_run_id).set()
        # Phase 1.5 (OP-9, T-C1-026): NOW resolve every pending approval
        # "closed" -- after every intent, with every cooperative signal
        # already set. Each registered Invoker observes the closed status
        # and commits the closed-first ``py.permission.closed`` +
        # ``tool.finished`` pair (T-B-027 ④-1), so the run folds to
        # interrupted/cancelled through the normal phase-2 wait; the
        # post-allow re-check can never commit a started over the already
        # cancelling run.
        if self._approvals is not None:
            await self._approvals.close_all(REASON_APPROVAL_SHUTDOWN)
        # Phase 2: wait for the tasks (their determined tool/step/run
        # terminals still commit through the same store); on timeout
        # hard-cancel the stragglers and let the grace period carry their
        # CancelledError handlers' final commits. Only the tasks still
        # pending after the grace are marked cleanup_failed (§7 L337: an
        # unconfirmed shutdown is never reported as cleaned -- conversely a
        # grace exit with committed terminals is a confirmed cleanup, #90:
        # marking it failed would lie and fence the reopen face). Tasks still
        # stuck after the grace are abandoned -- records kept, no further
        # in-process claim (③ float); that pending2 marking branch is
        # unreachable under the test discipline (it would need a tool that
        # survives task.cancel(), T-C1-022 ③-4) -- disclosed, not tested.
        if scheduled:
            by_task = {task: (run_id, session_id) for run_id, (session_id, task) in scheduled}
            _done, pending = await asyncio.wait(set(by_task), timeout=self._close_timeout_s)
            if pending:
                for task in pending:
                    task.cancel()
                _done2, pending2 = await asyncio.wait(pending, timeout=_CLOSE_GRACE_S)
                for task in pending2:
                    run_id, session_id = by_task[task]
                    await self._mark_cleanup_failed(session_id, run_id, REASON_CLOSE_TIMEOUT)
        # Phase 3: every own task has exited or is durably marked -- close in
        # the §7 L339 order: MCP first, then the provider, then the service
        # gate. The DB/engine stays with the App.
        if self._mcp_close is not None:
            await self._mcp_close()
        if self._provider_close is not None:
            await self._provider_close()
        self._closed = True

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
            if self._closed or self._closing:
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

    # -- R2-S7 internals: scheduling, cancel, cleanup ---------------------------

    def _schedule_run(self, session_id: str, run_id: str) -> None:
        """Create the runner task holding a strong reference (§7 L331 end).

        Called inside the prompt commit lock right after the accepted batch
        commits; the idempotent retry path never reaches here (only this
        freshly accepted request is scheduled). The ``_closing`` re-check
        shares the synchronous block with ``create_task``: a concurrent close
        either sees this task in the registry or makes this call a no-op --
        never a scheduled run the close lifecycle would miss.
        """
        if self._runner is None or self._closing or self._closed:
            return
        task = asyncio.create_task(self._drive_run(session_id, run_id))
        self._tasks[run_id] = (session_id, task)

    async def _drive_run(self, session_id: str, run_id: str) -> None:
        """The scheduled task body: drive the runner, record its failure.

        A crashed task is swallowed here (never re-raised into the loop as an
        unretrieved-exception warning) and recorded so :meth:`cancel` reports
        the unconfirmable cleanup as LifecycleCleanupFailed. CancelledError is
        the close-timeout path's own signal -- the runner's handler has
        finalized the run (or left the open step to recovery) -- and is not
        an error.
        """
        assert self._runner is not None
        try:
            await self._runner.run(session_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # recorded here; surfaced by cancel/close
            self._schedule_errors[run_id] = error
        finally:
            self._tasks.pop(run_id, None)

    async def _commit_cancel_intent(self, session_id: str, run_id: str, reason: Literal["user", "shutdown"]) -> None:
        """Commit one ``py.run.cancel_requested`` under the session lock.

        Check-first idempotency: the ``(session_id, "cancel:"+run_id)`` row is
        queried before committing (the store replays only same-batch retries),
        so repeated cancels and a cancel/close overlap never re-commit the
        intent. Each ConflictError retry re-checks the run state first -- the
        scheduled runner commits concurrently and may have committed the
        terminal while we retried, and an intent must never land on a
        terminal run (late cancel never overwrites, §7 L335).
        """
        if await self._cancel_intent_row(session_id, run_id):
            return
        batch = EventBatch(
            self._ids.batch_id(),
            (self._cancel_intent_draft(session_id, run_id, reason),),
            cancel_dedupe_key(run_id),
        )
        conflict: ConflictError | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            status = await self._runs_status_value(run_id)
            if status is not None and status.is_terminal:
                # The runner finished first; the caller re-reads and returns
                # the original terminal status (no intent, no overwrite).
                return
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

    async def _cancel_intent_row(self, session_id: str, run_id: str) -> bool:
        """True when a row already sits under the cancel dedupe key.

        A row under the key that is not ``py.run.cancel_requested`` v1 is
        stored corruption on this read path, fail closed (mirrors the prompt
        dedupe face's classification).
        """
        row = await self._repos.event_by_dedupe_key(session_id, cancel_dedupe_key(run_id))
        if row is None:
            return False
        if row.type != PY_RUN_CANCEL_REQUESTED or row.version != EVENT_VERSION:
            raise StorageCorruptError(
                f"session {session_id} dedupe row for run {run_id} is not {PY_RUN_CANCEL_REQUESTED} v{EVENT_VERSION}",
                isolate=True,
                details={
                    "reason": REASON_CANCEL_INTENT_ROW_UNEXPECTED,
                    "session_id": session_id,
                    "run_id": run_id,
                    "event_id": str(row.id),
                },
            )
        return True

    def _cancel_intent_draft(
        self, session_id: str, run_id: str, reason: Literal["user", "shutdown"]
    ) -> EventDraft[RunCancelRequestedData]:
        """One ``py.run.cancel_requested`` v1 draft (§3.1 L125, run metadata)."""
        return EventDraft[RunCancelRequestedData](
            id=self._ids.event_id(),
            type=PY_RUN_CANCEL_REQUESTED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=RunCancelRequestedData(run_id=run_id, reason=reason),
            metadata={"run_id": run_id},
        )

    async def _runs_status_value(self, run_id: str) -> RunStatusValue | None:
        """The current ``runs.status`` of one run by PK (None = no row)."""
        status = await self._repos.run_status_by_id(run_id)
        if status is None:
            return None
        try:
            return RunStatusValue(status)
        except ValueError as error:
            raise StorageCorruptError(
                f"run {run_id} has unknown status {status!r}",
                isolate=True,
                details={"reason": REASON_RUN_STATUS_UNKNOWN, "run_id": run_id, "status": status},
            ) from error

    async def _fail_cleanup(
        self,
        session_id: str,
        run_id: str,
        reason: str,
        detail: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        """Mark the session cleanup_failed and report LifecycleCleanupFailed (§7 L335)."""
        await self._mark_cleanup_failed(session_id, run_id, reason)
        error = LifecycleCleanupFailedError(
            f"run {run_id} cleanup could not be confirmed: {detail}",
            details={"session_id": session_id, "run_id": run_id, "reason": reason},
        )
        if cause is not None:
            raise error from cause
        raise error

    async def _mark_cleanup_failed(self, session_id: str, run_id: str, reason: str) -> None:
        """Persist ``sessions.status = cleanup_failed`` (management write).

        T-C1-022 ④-3: the direct engine UPDATE moved into
        :meth:`opencode.storage.SessionStore.cleanup_failed` (same statement,
        same independent short transaction, still no asyncio locks); this
        keeps only the R1 wiring gate -- without an engine there is no
        runs-read-model lifecycle, so the mark stays the historical no-op.
        The store's write gate then answers every later commit on this
        session with LifecycleCleanupFailed (该 session 不可再执行). The
        ``status = 'active'`` guard makes repeats and already-marked rows
        no-ops.
        """
        if self._engine is None:
            return
        await self._store.cleanup_failed(session_id, run_id, reason)
