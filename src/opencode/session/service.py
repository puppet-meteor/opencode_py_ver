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
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, overload

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.bus import VolatileBus
from opencode.bus.eventlog import MAX_READ_LIMIT, EventCursor, EventLog
from opencode.contracts import (
    EVENT_VERSION,
    PY_MESSAGE_IMPORTED,
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    PY_SESSION_DELETED,
    ConfigSnapshot,
    ConfigSnapshotV2,
    CoreErrorCode,
    EventDraft,
    JsonObject,
    MessageImportedData,
    PromptAcceptedData,
    SessionCreatedData,
    SessionCreatedDataV2,
    SessionDeletedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.events import (
    PY_PERMISSION_REVOKED,
    PY_RUN_CANCEL_REQUESTED,
    PY_SKILL_ACTIVATED,
    PY_SUBAGENT_BACKGROUNDED,
    ConfigSnapshotV3,
    PermissionRevokedData,
    RunCancelRequestedData,
    SessionCreatedDataV3,
    SkillActivatedData,
    SubagentBackgroundedData,
)
from opencode.contracts.messages import AssistantMessage, AssistantTextPart, IdleMessage, SessionMessage
from opencode.session.compaction import (
    CompactionEngine,
    CompactionOutcome,
    SettledHistoryView,
    SettledMessage,
    SummaryGenerator,
)
from opencode.session.inbox import InboxRef, SessionInbox
from opencode.session.state import SessionState, SessionStatus, SubagentObservation
from opencode.session.state import apply as apply_event
from opencode.skills import SkillActivationReceipt, SkillCatalog
from opencode.storage import (
    EventBatch,
    IdempotencyConflictError,
    LifecycleCleanupFailedError,
    MessagePage,
    NotFoundError,
    ReadRepositories,
    SessionClosedError,
    SessionCreatedRow,
    SessionListRow,
    SessionReadOnlyError,
    SessionRecord,
    SessionStore,
    StorageCorruptError,
    StoreError,
    StoreValidationError,
)
from opencode.storage.db import Database
from opencode.storage.repos import MAX_PAGE_LIMIT, InboxItemRow
from opencode.storage.txn import REASON_READ_MODEL_MISSING, ConflictError, from_epoch_ms

if TYPE_CHECKING:
    # T-C3-014: annotation-only for the same runtime-cycle reason as the
    # policy types below (``opencode.tools.__init__`` pulls the invoker,
    # which imports this module); the delivery face imports its runtime
    # helpers locally, the ``subagent_report_text`` precedent.
    from opencode.tools.background import BackgroundJobRow

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

#: §6.3 L347-350 sanitized reason for the re-activation refusal: a committed
#: ``py.skill.activated`` carries a different body hash for the same skill id
#: or the same declared path, and the host did not pass
#: ``confirm_hash_change=True``.
REASON_SKILL_HASH_CHANGED = "skill body hash changed since the committed activation"

#: Grace period after a hard task cancel at close-timeout: the runner's
#: CancelledError handler still commits the honest unknown/interrupted
#: terminals through the same store (§7 L337), so the cleanup_failed mark
#: must wait for it before it closes the write gate.
_CLOSE_GRACE_S = 1.0

#: §3.2: bounded ConflictError retries for the cancel-intent commit (the
#: scheduled runner commits concurrently through its own EventCommitter locks,
#: so a stale expected_seq is possible and is retried with the same batch).
_COMMIT_ATTEMPTS = 3

#: T-C1-101 (P7-S2): the background observer's poll cadence over the durable
#: ``run_status`` face (the same throttle as the tool's foreground poll -- a
#: scripted settle answers within one tick in tests, and a real child holds a
#: provider stream so 20 durable reads/second is noise).
SUBAGENT_OBSERVER_POLL_S = 0.05

#: T-C1-101 (P7-S2): the delivery re-entry bound. A settle can land inside a
#: live parent run's commit burst (the parked-item wake run, a successor
#: prompt), and the runner's EventCommitter shares no lock with the service's
#: session lock -- the inbox commit's tight same-batch CAS bound exhausts
#: there (each attempt's await gap hands the runner one more batch), so a
#: ConflictError re-enters the whole delivery after one poll tick: the burst
#: drains, the parent's log settles, the next attempt commits clean. The
#: bound's last ConflictError records like any storage error -- the marker
#: stays outstanding and a restart's re-arm retries.
_SUBAGENT_DELIVERY_ATTEMPTS = 3

#: T-C1-101 (P7-S2): the TS subagent-completion.ts delivery texts, verbatim.
#: The no-text sentence is canonical HERE (the delivery face lives on this
#: service); the tool module's S5 ``NO_TEXT_RESPONSE`` name aliases this
#: constant so both faces can never drift.
SUBAGENT_NO_TEXT_RESPONSE = "Subagent completed without a text response."
SUBAGENT_ERROR_RESPONSE = "Subagent failed"
SUBAGENT_CANCELLED_RESPONSE = "Subagent cancelled"

#: T-C1-101 (P7-S2): the notification-id form -- ``subagent:{run_id}`` over
#: the CHILD's run id. Unique per generation (one accepted child prompt = one
#: child run), deterministic and restart-stable (the re-arm scan and the
#: admission's completeBackground-pop key on the same text).
SUBAGENT_NOTIFICATION_PREFIX = "subagent:"

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
class SessionInfo:
    """T-C1-094: the ``GET /session/{id}`` face — the four-field get wire.

    Exactly the serialization whitelist of the session resource get face:
    ``config_snapshot``/``snapshot_hash``/``diagnostic`` never leave the
    service through this face (the export face carries the snapshot
    explicitly instead). ``status`` is the raw stored wire text.
    """

    session_id: str
    status: str
    applied_seq: int
    active_run_id: str | None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """T-C1-094: one ``GET /session`` list element — the get face plus the created stamp.

    ``created_at_ms`` is the ``py.session.created`` event's epoch-ms
    timestamp (the created-descending list ordering key); ``status`` is the
    raw stored wire text.
    """

    session_id: str
    status: str
    applied_seq: int
    active_run_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class SessionExport:
    """T-C1-094: the ``GET /session/{id}/export`` face — a self-describing transfer.

    The exact shape :meth:`SessionService.import_session` restores: identity
    (``session_id``), the created-event fields (``working_dir`` /
    ``config_snapshot`` / ``snapshot_hash`` — re-validated on import through
    the §6.2 canonical hash — / ``created_at_ms``), the tombstone-relevant
    ``status``, and the full projected message history in order. Messages
    ride as the §2.1 wire JSONs (``model_dump(mode="json")``), so the round
    trip never re-shapes them.
    """

    session_id: str
    status: str
    working_dir: str
    config_snapshot: JsonObject
    snapshot_hash: str
    created_at_ms: int
    messages: tuple[SessionMessage, ...]

    def to_payload(self) -> JsonObject:
        """The wire JSON object — the exact import face, shared by HTTP and SDK."""
        return {
            "session_id": self.session_id,
            "status": self.status,
            "working_dir": self.working_dir,
            "config_snapshot": self.config_snapshot,
            "snapshot_hash": self.snapshot_hash,
            "created_at_ms": self.created_at_ms,
            "messages": [message.model_dump(mode="json") for message in self.messages],
        }


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


@dataclass(frozen=True, slots=True)
class InterruptReceipt:
    """§3-5 (T-C1-100): the interrupt face's honest receipt.

    ``interrupted`` is True only when the run this call targeted settled on
    the interrupted terminal; a run that finished on its own first answers
    its original outcome (``interrupted=False``, the late-interrupt parity
    of cancel). ``run_id``/``status`` carry the target run; a session with
    no active run answers the idle no-op (both ``None``).
    ``resumed_run_id`` is the resume drive's fresh run over the oldest
    pending steer (None = no resume asked, no pending steer, or the idle
    no-op).
    """

    interrupted: bool
    session_id: str
    run_id: str | None
    status: RunStatusValue | None
    resumed_run_id: str | None = None


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


#: T-C1-094 (P6-S1): the exact import-payload key set — the export wire
#: (:meth:`SessionService.export_session`) accepted back verbatim; a missing
#: or extra key refuses the whole import (strict shape, no defaults guessed).
_IMPORT_PAYLOAD_KEYS = frozenset(
    {"session_id", "status", "working_dir", "config_snapshot", "snapshot_hash", "created_at_ms", "messages"}
)

#: T-C1-094 (P6-S1): the §2.1 message-union dispatch for the import payload
#: (the same tag->model table the projected-row decoder uses; a local copy
#: keeps the repos table private).
_IMPORT_MESSAGE_MODELS: dict[str, type[SessionMessage]] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "idle": IdleMessage,
}


def _import_identity(raw: JsonObject) -> tuple[str, str, str]:
    """Validate the import payload's exact key set + the string faces, fail closed.

    Returns ``(session_id, status, working_dir)``. ``status`` is validated
    for round-trip fidelity only — the restored session is ACTIVE whatever
    the exported status said (boot-local marks are not transferable state;
    disclosed in the ticket progress record).
    """
    if not isinstance(raw, dict):
        raise StoreValidationError("import payload must be a JSON object")
    missing = sorted(_IMPORT_PAYLOAD_KEYS - set(raw))
    unknown = sorted(set(raw) - _IMPORT_PAYLOAD_KEYS)
    if missing or unknown:
        raise StoreValidationError(f"import payload key set mismatch (missing: {missing}, unknown: {unknown})")
    session_id = raw["session_id"]
    if not isinstance(session_id, str) or not session_id.startswith(_SES_PREFIX):
        raise StoreValidationError("import payload field 'session_id' must be a ses_-prefixed string")
    status = raw["status"]
    if not isinstance(status, str):
        raise StoreValidationError(f"import payload field 'status' must be a string, got {status!r}")
    try:
        SessionStatus(status)
    except ValueError as error:
        raise StoreValidationError(
            f"import payload field 'status' is not a known session status: {status!r}"
        ) from error
    working_dir = raw["working_dir"]
    if not isinstance(working_dir, str) or not working_dir:
        raise StoreValidationError("import payload field 'working_dir' must be a non-empty string")
    return session_id, status, working_dir


def _verify_snapshot_hash(raw_hash: Any, snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3) -> str:
    """Re-verify the §6.2 canonical snapshot digest of the import payload.

    The same digest the created-event validator enforces on decode, checked
    before any commit: a tampered or drifted snapshot never lands in the log.
    """
    if not isinstance(raw_hash, str) or not raw_hash:
        raise StoreValidationError("import payload field 'snapshot_hash' must be a non-empty string")
    computed = compute_snapshot_hash(snapshot)
    if raw_hash != computed:
        raise StoreValidationError(
            f"import payload snapshot_hash does not match the canonical sha256 of config_snapshot "
            f"({raw_hash} != {computed})"
        )
    return raw_hash


def _require_epoch_ms(value: Any) -> int:
    """The import payload's ``created_at_ms`` face: an integer, bools rejected."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreValidationError("import payload field 'created_at_ms' must be an integer")
    return value


def _import_messages(session_id: str, raw_messages: Any) -> list[SessionMessage]:
    """Decode the import payload's message list through the §2.1 wire union.

    Fail closed (ticket §8): an entry that is not a JSON object, carries an
    unknown type tag, or fails model validation refuses the whole import —
    nothing is silently dropped, so an accepted import restores the message
    count exactly. Duplicate message ids refuse here rather than at the
    fold, so a malformed payload answers ValidationError, never a 500.
    """
    if not isinstance(raw_messages, list):
        raise StoreValidationError("import payload field 'messages' must be a list")
    messages: list[SessionMessage] = []
    seen: set[str] = set()
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise StoreValidationError(
                f"import payload for session {session_id} has a message entry that is not a JSON object"
            )
        model = _IMPORT_MESSAGE_MODELS.get(str(raw.get("type")))
        if model is None:
            raise StoreValidationError(
                f"import payload for session {session_id} has a message with unknown type tag {raw.get('type')!r}"
            )
        try:
            message = model.model_validate(raw)
        except PydanticValidationError as error:
            raise StoreValidationError(
                f"import payload for session {session_id} has a message that did not decode: {error}"
            ) from error
        if message.id in seen:
            raise StoreValidationError(f"import payload for session {session_id} has duplicate message id {message.id}")
        seen.add(message.id)
        messages.append(message)
    return messages


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
        catalog: SkillCatalog | None = None,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
        compaction_summarizer: SummaryGenerator | None = None,
        database: Database | None = None,
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
        # P2-S2 wiring: the §6.3 skill catalog (the activation check-order
        # gate). Optional like approvals/policy -- the direct-construction
        # faces stay unchanged; the App composition passes the
        # discovery-backed instance.
        self._catalog = catalog
        # Lazily-built event-log reader for the re-activation confirmation
        # gate (committed py.skill.activated events, §6.3 L347-350).
        self._skill_log: EventLog | None = None
        # M6 compaction wiring (§2.4, T-C1-062): the summary-generation seam.
        # Everything else the engine needs is real and local -- this service's
        # store (the §3.2 commit face), the settled read face below (the
        # EventLog fold + log_high), the shared id factory/clock, and this
        # service's own per-session commit lock -- so only the model-call face
        # is injected. None keeps the pre-M6 face exactly (no trigger, zero
        # behavior change); the engine is built lazily on the first trigger.
        self._compaction_summarizer = compaction_summarizer
        self._compaction_engine: CompactionEngine | None = None
        self._compaction_log: EventLog | None = None
        # The last compaction failure per session (§2.4 L132: the CONTEXT
        # guarantee is "no replacement"; the error itself is kept here --
        # recorded, inspectable, never silently dropped -- and the stateless
        # engine retries on the next trigger. Not surfaced on any §7 command
        # face yet: compaction has no host command (§2.4 L110).
        self._compaction_errors: dict[str, BaseException] = {}
        self._close_timeout_s = float(close_timeout_s)
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        # §7 L331 end: strong references to the scheduled run tasks
        # (run_id -> owning session + task) -- a task the registry drops would
        # be garbage-collected mid-flight.
        self._tasks: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._schedule_errors: dict[str, BaseException] = {}
        # T-C1-100 (P7-S1): the durable steering-inbox face. Built eagerly
        # over the same store/repos/bus/id/clock -- every committing method
        # there runs with the CALLER holding this service's per-session
        # commit lock (the ordering contract), so no separate lock state
        # lives here.
        self._inbox = SessionInbox(store=store, repos=repos, bus=bus, ids=self._ids, clock=self._clock)
        # T-C1-101 (P7-S2): the background subagent observation faces.
        # ``_subagent_observers``: notification_id -> (parent session id,
        # task) -- the process-local arm of the durable markers (strong
        # references, the _tasks precedent; the observer life outlives the
        # tool call that armed it, the TS global-scope fork). ``_subagent_
        # delivery_failures``: the last delivery error per notification id --
        # recorded, inspectable, never silently dropped (the
        # _compaction_errors precedent); the marker stays outstanding, so a
        # restart's re-arm retries.
        # T-C3-005 wiring: the optional database handle for the subagent
        # outbox rows -- settle upserts, successful delivery deletes, the
        # keep-alive reads run live. None keeps the pre-T-C3-005 face
        # (in-process delivery only, no replay coverage); the App
        # composition passes its owned handle. The row helpers are imported
        # LAZILY at their call sites: ``opencode.tools.__init__`` pulls the
        # registry, which imports THIS module -- a top-level import would
        # close a cycle.
        self._database = database
        self._subagent_observers: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._subagent_delivery_failures: dict[str, BaseException] = {}
        # Lazily-built event-log reader for the re-arm scan's full fold (the
        # _compaction_log precedent).
        self._subagent_log: EventLog | None = None

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
        self,
        config_snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3 | JsonObject,
        *,
        parent_id: str | None = None,
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

        P6-S5 (T-C1-098, ticket §3-3 ①): the optional ``parent_id`` records
        the subagent parent link on the durable created event (``None`` =
        a root session, the pre-P6 face unchanged). The link carries no
        lifecycle: no delete cascade, no status coupling -- the child is a
        fully independent session that merely remembers its origin for the
        subagent depth guard.
        """
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.startswith("ses_")):
            raise StoreValidationError("parent_id must be None or a string matching ^ses_")
        snapshot = _coerce_config_snapshot(config_snapshot)
        session_id = self._ids.session_id()
        draft = self._created_draft(session_id, self._working_dir, snapshot, self._clock(), parent_id=parent_id)
        committed = await self._store.commit(session_id, EventBatch(self._ids.batch_id(), (draft,)), 0)
        self._bus.notify(session_id, committed)
        return SessionRef(session_id=session_id)

    def _created_draft(
        self,
        session_id: str,
        working_dir: str,
        snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3,
        created_at: datetime,
        *,
        parent_id: str | None = None,
    ) -> EventDraft[SessionCreatedData] | EventDraft[SessionCreatedDataV2] | EventDraft[SessionCreatedDataV3]:
        """One ``py.session.created`` draft with the wire version routed by the data shape.

        The version path shared by create/fork/import (§8 L358: the wire
        version follows the data shape): a v3 snapshot commits version 3
        (checked first — a v3 snapshot is a standalone model, never a v2
        subclass), a v2 snapshot version 2, a v1 snapshot version 1. The
        snapshot hash is computed from the same model the draft carries, so
        the §6.2 re-validation on decode can never disagree. The P6-S5
        ``parent_id`` (T-C1-098) threads through every wire version with a
        ``None`` default, so fork/import keep the root-session face
        unchanged without touching their call sites.
        """
        if isinstance(snapshot, ConfigSnapshotV3):
            return EventDraft[SessionCreatedDataV3](
                id=self._ids.event_id(),
                type=PY_SESSION_CREATED,
                version=3,
                session_id=session_id,
                created_at=created_at,
                data=SessionCreatedDataV3(
                    working_dir=working_dir,
                    config_snapshot=snapshot,
                    snapshot_hash=compute_snapshot_hash(snapshot),
                    parent_id=parent_id,
                ),
            )
        if isinstance(snapshot, ConfigSnapshotV2):
            return EventDraft[SessionCreatedDataV2](
                id=self._ids.event_id(),
                type=PY_SESSION_CREATED,
                version=2,
                session_id=session_id,
                created_at=created_at,
                data=SessionCreatedDataV2(
                    working_dir=working_dir,
                    config_snapshot=snapshot,
                    snapshot_hash=compute_snapshot_hash(snapshot),
                    parent_id=parent_id,
                ),
            )
        return EventDraft[SessionCreatedData](
            id=self._ids.event_id(),
            type=PY_SESSION_CREATED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=created_at,
            data=SessionCreatedData(
                working_dir=working_dir,
                config_snapshot=snapshot,
                snapshot_hash=compute_snapshot_hash(snapshot),
                parent_id=parent_id,
            ),
        )

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

    # T-C1-100: the overloads pin the default face's static contract --
    # ``delivery=None`` (or omitted) can never answer InboxRef, so every
    # pre-P7 caller keeps the plain RunRef type; an explicit delivery opts
    # into the union (busy parks, idle admits-and-starts).
    @overload
    async def prompt(self, session_id: str, text: str, request_id: str, *, delivery: None = ...) -> RunRef: ...

    @overload
    async def prompt(
        self, session_id: str, text: str, request_id: str, *, delivery: Literal["steer", "queue"]
    ) -> RunRef | InboxRef: ...

    async def prompt(
        self, session_id: str, text: str, request_id: str, *, delivery: Literal["steer", "queue"] | None = None
    ) -> RunRef | InboxRef:
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

        T-C1-100 (P7-S1) adds the opt-in ``delivery`` face on top; the
        default stays byte-identical (``delivery=None`` keeps this §7
        four-step contract exactly -- a busy session still answers
        :class:`SessionBusyError`, and every pre-P7 test of this face runs
        against the same path). An explicit ``"steer"``/``"queue"`` parks
        the prompt in the durable inbox when the session is busy
        (:class:`InboxRef`, the admission face) or admits-and-starts it in
        one atomic batch when idle (RunRef, the §4-1 matrix). Step 2.5
        recognizes the retries: the same request_id with a different text
        is the §7 idempotency conflict, the same text while the item stays
        parked and the session busy keeps the busy face (never a second
        item), the same text on an idle session promotes the parked item
        now, and a request_id whose parked item was already promoted or
        admitted-and-started (an unkeyed accept -- a keyed batch is
        single-row) answers the ORIGINAL RunRef through the runs read
        model, exactly like step 2's keyed face.
        """
        _require_session_id_arg(session_id)
        _require_request_id_arg(request_id)
        if not isinstance(text, str):
            raise StoreValidationError("text must be a string (no implicit conversion)")
        if delivery is not None and delivery not in ("steer", "queue"):
            raise StoreValidationError('delivery must be None, "steer" or "queue"')
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

            # -- step 2.5: parked-retry recognition (T-C1-100, P7-S1) ----------
            parked = await self._inbox.pending_by_request(session_id, request_id)
            if parked is not None:
                if parked.message.text != text:
                    raise IdempotencyConflictError(
                        f"request {request_id!r} was already accepted with a different text",
                        details={"session_id": session_id, "request_id": request_id},
                    )
                if record.active_run_id is not None:
                    # Same item, still busy: it stays parked; the honest
                    # answer is the busy face, never a second admission.
                    raise SessionBusyError(
                        f"session {session_id} already holds an active run",
                        details={"session_id": session_id, "active_run_id": record.active_run_id},
                    )
                run_id, accepted_request_id = await self._inbox.promote(session_id, parked)
                self._schedule_run(session_id, run_id)
                return RunRef(session_id=session_id, run_id=run_id, request_id=accepted_request_id)
            promoted_run_id = await self._repos.run_id_by_request(session_id, request_id)
            if promoted_run_id is not None:
                # §3-1 step 2.5: the request was already accepted through an
                # inbox promotion or an idle admission -- an unkeyed accept
                # (a keyed batch is single-row, unique(session_id,
                # dedupe_key)), invisible to step 2. The runs read model's
                # unique(session_id, request_id) is the retry face, with
                # step 2's exact semantics: same text -> the original
                # RunRef (never a second run); different text -> the §7
                # idempotency conflict.
                promoted = await self._repos.prompt_by_run_id(session_id, promoted_run_id)
                assert promoted is not None  # a runs row and its accepted event commit together
                if promoted.message_text != text:
                    raise IdempotencyConflictError(
                        f"request {request_id!r} was already accepted with a different text",
                        details={"session_id": session_id, "request_id": request_id},
                    )
                return RunRef(session_id=session_id, run_id=promoted_run_id, request_id=promoted.request_id)

            # -- step 3: active_run_id gate (§7 L323) ------------------------
            if record.active_run_id is not None:
                if delivery is None:
                    raise SessionBusyError(
                        f"session {session_id} already holds an active run",
                        details={"session_id": session_id, "active_run_id": record.active_run_id},
                    )
                # §4-1: explicit delivery parks the prompt (the busy face
                # becomes an admission); the run's step boundaries and its
                # settle recheck own every later transition.
                return await self._inbox.admit(session_id, text=text, request_id=request_id, delivery=delivery)

            # -- step 4: generate + fix IDs, expected_seq = H, commit, notify -
            if delivery is not None:
                # §4-1 idle + explicit delivery: the item never rests -- one
                # atomic batch parks the provenance, accepts the run over
                # the parked message and records the delivery (a stored
                # same-batch retry replays under the same prompt dedupe key).
                run_id, accepted_request_id = await self._inbox.admit_and_start(
                    session_id, text=text, request_id=request_id, delivery=delivery
                )
                self._schedule_run(session_id, run_id)
                return RunRef(session_id=session_id, run_id=run_id, request_id=accepted_request_id)
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

    # -- §3 inbox + interrupt (T-C1-100, P7-S1) --------------------------------

    async def inbox_list(self, session_id: str) -> list[InboxItemRow]:
        """§3-6 list: every pending inbox row of one session, in enqueue order.

        A pure read-model query (no lock, no recovery trigger); the
        deleted-tombstone face answers NotFound exactly like every other
        resource face.
        """
        _require_session_id_arg(session_id)
        await self._read_live_record(session_id)
        return await self._inbox.pending(session_id)

    async def inbox_cancel(self, session_id: str, inbox_id: str) -> bool:
        """§3-6 cancel: withdraw one pending item; ``False`` is the no-op.

        Withdrawing an item that is no longer pending (already delivered or
        already cancelled) commits nothing and answers ``False`` -- the
        endpoint's honest 200, never an error and never a second event.
        """
        _require_session_id_arg(session_id)
        if not isinstance(inbox_id, str) or not inbox_id:
            raise StoreValidationError("inbox_id must be a non-empty string")
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._read_live_record(session_id)
            return await self._inbox.cancel(session_id, inbox_id)

    async def inbox_update(self, session_id: str, inbox_id: str, delivery: Literal["steer", "queue"]) -> InboxItemRow:
        """§3-6 update: re-arm one pending item's delivery; returns the row.

        A flip to ``"steer"`` rings the doorbell implicitly: the runner's
        step-boundary recheck consumes the re-armed steer within one step
        (no in-memory signal exists to set -- the durable log is the only
        truth). An update on an item that is no longer pending raises
        :class:`ConflictError` (the §3-6 face is NOT a no-op, unlike
        cancel); unknown delivery values fail closed before any lock.
        """
        _require_session_id_arg(session_id)
        if not isinstance(inbox_id, str) or not inbox_id:
            raise StoreValidationError("inbox_id must be a non-empty string")
        if delivery not in ("steer", "queue"):
            raise StoreValidationError('delivery must be "steer" or "queue"')
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._read_live_record(session_id)
            return await self._inbox.update_delivery(session_id, inbox_id, delivery)

    # -- §3 T-C1-101: subagent background observation (P7-S2) ------------------

    async def subagent_final_text(self, session_id: str) -> str:
        """The session's final assistant text (TS subagent-completion.ts ``text``).

        The last ``AssistantMessage`` of the session, its text parts joined;
        a child that settled without any assistant text answers the TS
        no-text sentence verbatim. Cursor-paged to the true tail so a long
        child history is not truncated by the first page. ONE extraction
        shared by the tool's foreground face (the S5 behavior, byte-identical
        since T-C1-098) and the background delivery (T-C1-101 §3-2), so the
        same child run always yields the same text either way.
        """
        after: str | None = None
        last: AssistantMessage | None = None
        while True:
            page = await self.messages(session_id, after_message_id=after)
            for message in page.items:
                if isinstance(message, AssistantMessage):
                    last = message
            if not page.has_more:
                break
            after = page.next_after_message_id
        if last is None:
            return SUBAGENT_NO_TEXT_RESPONSE
        texts = [part.text for part in last.parts if isinstance(part, AssistantTextPart)]
        if not texts:
            return SUBAGENT_NO_TEXT_RESPONSE
        return "\n".join(texts)

    async def commit_subagent_background(
        self,
        parent_session_id: str,
        *,
        child_session_id: str,
        run_id: str,
        agent: str,
        description: str,
    ) -> str:
        """Hand one child generation to the background observer (§3-1/§3-3).

        The durable per-generation marker (``py.subagent.backgrounded``)
        commits to the PARENT's log under the parent's session lock, then
        the process-local observer arms over it -- the TS
        ``SubagentJob.background`` (jobs.background + notify) with the
        in-memory Set replaced by the durable row keyed by the deterministic
        notification id. Returns the notification id (the admission's
        completeBackground key). Fail-closed double face: the ids must
        match their ^ses_/^run_ patterns and the parent must be live
        (missing or tombstoned -> the same NotFound); a duplicate marker
        (the same run observed twice) folds illegal and the commit fails
        closed, never a silent overwrite.
        """
        _require_session_id_arg(parent_session_id)
        _require_run_id_arg(run_id)
        if not isinstance(child_session_id, str) or not child_session_id.startswith(_SES_PREFIX):
            raise StoreValidationError("child_session_id must be a session id matching ^ses_")
        if not isinstance(agent, str) or not agent:
            raise StoreValidationError("agent must be a non-empty string")
        if not isinstance(description, str):
            raise StoreValidationError("description must be a string")
        notification_id = f"{SUBAGENT_NOTIFICATION_PREFIX}{run_id}"
        async with self._session_lock(parent_session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._read_live_record(parent_session_id)
            await self._commit_subagent_background(
                parent_session_id,
                child_session_id=child_session_id,
                run_id=run_id,
                agent=agent,
                description=description,
                notification_id=notification_id,
            )
        self._arm_subagent_observer(
            parent_session_id,
            child_session_id=child_session_id,
            run_id=run_id,
            agent=agent,
            description=description,
            notification_id=notification_id,
        )
        return notification_id

    async def _commit_subagent_background(
        self,
        parent_session_id: str,
        *,
        child_session_id: str,
        run_id: str,
        agent: str,
        description: str,
        notification_id: str,
    ) -> None:
        """Commit one ``py.subagent.backgrounded`` under the session lock.

        Bounded ConflictError retries with the same batch (the scheduled
        runner commits concurrently, so a stale expected_seq is retried);
        no dedupe key -- the duplicate-generation fence is the fold's
        duplicate-notification_id rejection.
        """
        draft = EventDraft[SubagentBackgroundedData](
            id=self._ids.event_id(),
            type=PY_SUBAGENT_BACKGROUNDED,
            version=EVENT_VERSION,
            session_id=parent_session_id,
            created_at=self._clock(),
            data=SubagentBackgroundedData(
                child_session_id=child_session_id,
                run_id=run_id,
                agent=agent,
                description=description,
                notification_id=notification_id,
            ),
            metadata={},
        )
        batch = EventBatch(self._ids.batch_id(), (draft,))
        conflict: ConflictError | None = None
        for _ in range(_COMMIT_ATTEMPTS):
            expected_seq = await self._repos.log_high(parent_session_id)
            try:
                committed = await self._store.commit(parent_session_id, batch, expected_seq)
            except ConflictError as error:
                conflict = error
                continue
            self._bus.notify(parent_session_id, committed)
            return
        assert conflict is not None
        raise conflict

    def _arm_subagent_observer(
        self,
        parent_session_id: str,
        *,
        child_session_id: str,
        run_id: str,
        agent: str,
        description: str,
        notification_id: str,
    ) -> None:
        """Arm the process-local observer for one outstanding generation.

        Idempotent by notification id (a re-arm over an already-armed
        generation is a no-op) and never armed on a closing service -- the
        durable marker survives, so the next boot's re-arm picks the
        generation up (the TS durable-notification-marker recovery shape).
        """
        if notification_id in self._subagent_observers:
            return
        if self._closed or self._closing:
            return
        task = asyncio.create_task(
            self._observe_subagent(
                parent_session_id,
                child_session_id=child_session_id,
                run_id=run_id,
                agent=agent,
                description=description,
                notification_id=notification_id,
            ),
            name=f"opencode-subagent-observer-{notification_id}",
        )
        self._subagent_observers[notification_id] = (parent_session_id, task)

    async def _observe_subagent(
        self,
        parent_session_id: str,
        *,
        child_session_id: str,
        run_id: str,
        agent: str,
        description: str,
        notification_id: str,
    ) -> None:
        """One observer task: poll the durable child run, deliver once.

        The TS notify fork (jobs.wait -> deliver, global scope so the
        observer outlives the tool call): the durable run_status face is
        the only truth (never an in-memory task handle). The running arm
        of TS ``deliver`` (status running -> no delivery) is structural
        here -- the loop only breaks on a terminal status, so a delivery
        never precedes the settle.

        The delivery RE-ENTERS on ConflictError rather than leaning on
        the inbox commit's tight same-batch bound: a settle landing
        inside a live parent run's commit burst (the parked-item wake
        run, a successor prompt) exhausts that bound -- the runner's
        EventCommitter shares no lock with the service's session lock,
        and each tight attempt's await gap hands the runner one more
        batch -- so a drained burst, not a fresher CAS, is what makes
        the retry commit clean. One poll tick between attempts; re-entry
        is safe (the pending-row gate folds an already-admitted
        generation, a rejected batch leaves nothing partial behind);
        the bound's last ConflictError re-raises.

        A storage/read error is recorded per notification id and the
        marker stays outstanding (the _compaction_errors face; a
        restart's re-arm retries); the CancelledError of a close-time
        cancel propagates clean.
        """
        try:
            while True:
                status = await self.run_status(run_id)
                if status.status.is_terminal:
                    break
                await asyncio.sleep(SUBAGENT_OBSERVER_POLL_S)
            # The honest PY terminal -> TS state mapping (recorded): a
            # succeeded child is "completed", a failed child is "error",
            # and every other terminal (interrupted -- the cooperative
            # cancel and the R4 recovery fold both land here) is
            # "cancelled", the TS third state.
            if status.status is RunStatusValue.SUCCEEDED:
                state = "completed"
                error_text: str | None = None
            elif status.status is RunStatusValue.FAILED:
                state = "error"
                error_text = status.error
            else:
                state = "cancelled"
                error_text = None
            # T-C3-005: the durable outbox row. Settle writes it (the
            # recovery payload carries everything the replay construction
            # needs), a successful delivery deletes it (completeBackground),
            # and a failed delivery leaves it standing for the next boot's
            # replay -- the marker stays outstanding either way, so the
            # re-arm and the row can never double-deliver (the pending-row
            # gate folds the duplicate). The row status keeps the shell-word
            # terminal vocabulary ("succeeded", not TS "completed" -- the
            # BEFORE-eval discovery; the report word maps at construction).
            if self._database is not None:
                from opencode.tools.background import BackgroundJobRow, upsert_row

                row_status = "succeeded" if state == "completed" else state
                output = await self._subagent_response_text(
                    state=state, error_text=error_text, child_session_id=child_session_id
                )
                await upsert_row(
                    self._database,
                    BackgroundJobRow(
                        notification_id=notification_id,
                        session_id=parent_session_id,
                        tool_call_id=notification_id,
                        kind="subagent",
                        status=row_status,
                        title=description,
                        payload={
                            "kind": "subagent",
                            "parentSessionID": parent_session_id,
                            "childSessionID": child_session_id,
                            "agent": agent,
                            "description": description,
                            "output": output,
                        },
                    ),
                )
            delivered = False
            for attempt in range(_SUBAGENT_DELIVERY_ATTEMPTS):
                try:
                    await self._deliver_subagent_completion(
                        parent_session_id,
                        child_session_id=child_session_id,
                        agent=agent,
                        description=description,
                        notification_id=notification_id,
                        state=state,
                        error_text=error_text,
                    )
                    delivered = True
                    break
                except ConflictError:
                    if attempt == _SUBAGENT_DELIVERY_ATTEMPTS - 1:
                        raise
                    await asyncio.sleep(SUBAGENT_OBSERVER_POLL_S)
            if delivered and self._database is not None:
                from opencode.tools.background import delete_row

                await delete_row(self._database, notification_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._subagent_delivery_failures[notification_id] = error
        finally:
            self._subagent_observers.pop(notification_id, None)

    async def _subagent_response_text(self, *, state: str, error_text: str | None, child_session_id: str) -> str:
        """The TS three-state text mapping (``SubagentCompletion.deliver``).

        completed -> the shared final-text extraction (the no-text case
        folds into the TS sentence); error -> ``error ?? "Subagent
        failed"``; cancelled -> the TS three-tier chain (the child's own
        last output, else the interrupting error, else the constant).
        Shared by the delivery and the durable row's recovery payload so
        the replayed report is byte-identical to the live one.
        """
        if state == "completed":
            return await self.subagent_final_text(child_session_id)
        if state == "error":
            return error_text if error_text is not None else SUBAGENT_ERROR_RESPONSE
        final = await self.subagent_final_text(child_session_id)
        if final != SUBAGENT_NO_TEXT_RESPONSE:
            return final
        if error_text is not None:
            return error_text
        return SUBAGENT_CANCELLED_RESPONSE

    async def _subagent_outstanding(self, session_id: str) -> list[str]:
        """Outstanding background-work lines under a session (TS ``outstanding``).

        Queried live at delivery time -- never snapshotted -- so a settle
        racing a still-running row reads the truth (the plan's live-read
        decision). Empty when no database handle is wired.
        """
        if self._database is None:
            return []
        from opencode.tools.background import outstanding_lines

        return await outstanding_lines(self._database, session_id)

    async def _deliver_subagent_completion(
        self,
        parent_session_id: str,
        *,
        child_session_id: str,
        agent: str,
        description: str,
        notification_id: str,
        state: str,
        error_text: str | None,
    ) -> None:
        """TS ``SubagentCompletion.deliver``: three-state text + XML + admission.

        The text mapping is TS verbatim (see :meth:`_subagent_response_text`);
        the wrapper and the metadata carrier are interpolated through the
        shared :func:`opencode.tools.background.subagent_report_text`
        construction -- a COMPLETED child with outstanding background rows
        reports ``state="running"`` in the wrapper (the keep-alive) plus
        the "Background work still running:" tail, while the metadata keeps
        the TRUE state verbatim. The admission is the S1 inbox synthetic
        seam -- steer into the active run, or queue + immediate promote
        when the parent is idle (PY has no TS ``resume:false`` suspension
        face, so the honest idle mapping wakes the parent; disclosed) --
        and the ``completeBackground`` acknowledgment is the admission
        fold's observation pop, keyed by the same notification id.
        """
        from opencode.tools.background import subagent_report_text

        text = await self._subagent_response_text(state=state, error_text=error_text, child_session_id=child_session_id)
        outstanding = await self._subagent_outstanding(child_session_id)
        wrapped = subagent_report_text(
            child_session_id=child_session_id,
            state=state,
            description=description,
            body=text,
            outstanding=outstanding,
        )
        message_metadata: JsonObject = {
            "source": "subagent",
            "childID": child_session_id,
            "agent": agent,
            "state": state,
        }
        async with self._session_lock(parent_session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            record = await self._read_live_record(parent_session_id)
            # The completeBackground idempotency: a still-pending synthetic
            # row under the same notification id means this generation
            # already delivered (an armed duplicate or a re-arm race); the
            # admission fold's pop owns the real consumption.
            if await self._repos.inbox_item(parent_session_id, notification_id) is not None:
                return
            delivery: Literal["steer", "queue"] = "steer" if record.active_run_id is not None else "queue"
            await self._inbox.admit_synthetic(
                parent_session_id,
                inbox_id=notification_id,
                text=wrapped,
                message_metadata=message_metadata,
                delivery=delivery,
            )
            if delivery == "queue":
                await self._promote_next(parent_session_id, "input")

    async def deliver_event_wake(
        self,
        session_id: str,
        *,
        inbox_id: str,
        text: str,
        metadata: JsonObject,
    ) -> None:
        """T-C3-006 ``watch_mcp_event`` wake: one synthetic event delivery.

        The MCP-events watch tool's delivery port -- the same S1 synthetic
        seam as :meth:`_deliver_subagent_completion` (steer into the active
        run, or queue + immediate promote when idle), with the same
        ``inbox_item`` idempotency keyed by the deterministic
        ``msg_mcp_<hash>`` id: a server replaying an event maps to the same
        message, admitted once. PY difference: the TS
        ``sessions.synthetic`` carries a ``description``; the PY admission
        has no description face, so provenance rides the ``metadata``
        carrier (``{source, server, event, eventID}``). A missing or
        deleted session raises here -- the watch caller treats a delivery
        failure as its stop condition (a dead session has nowhere to
        deliver, no stopped notice).

        Idempotency is DURABLE (stronger than the subagent face's
        pending-row check): the events log is append-only, so a server
        replaying an event whose wake was already consumed admits nothing
        -- the TS deterministic-message-id face ("a replayed event maps
        to the same message, which is admitted once").
        """
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            record = await self._read_live_record(session_id)
            if await self._repos.inbox_enqueued_seen(session_id, inbox_id):
                return
            delivery: Literal["steer", "queue"] = "steer" if record.active_run_id is not None else "queue"
            await self._inbox.admit_synthetic(
                session_id,
                inbox_id=inbox_id,
                text=text,
                message_metadata=metadata,
                delivery=delivery,
            )
            if delivery == "queue":
                await self._promote_next(session_id, "input")

    async def deliver_background_settlement(self, row: BackgroundJobRow) -> None:
        """T-C3-014 ``completeBackground``: one settled shell row as the wake.

        The invoker's settlement continuation calls this through the
        registry's delivery port the moment the terminal row is upserted:
        the text is the settlement's own ``text`` payload (byte-identical
        to the boot replay via
        :func:`opencode.tools.background.shell_report_text`), provenance
        rides the metadata carrier, and the same S1 synthetic seam as the
        subagent/MCP wakes steers into the active run or promotes the idle
        session (``deliver_event_wake``'s idempotency makes a re-settled
        row deliver once). The caller deletes the row on success and keeps
        it on failure (at-least-once replay on the next boot).
        """
        from opencode.tools.background import shell_report_text

        metadata: JsonObject = {
            "source": "background",
            "kind": row.kind,
            "status": row.status,
            "title": row.title,
        }
        await self.deliver_event_wake(
            row.session_id,
            inbox_id=row.notification_id,
            text=shell_report_text(row.payload),
            metadata=metadata,
        )

    async def replay_background_pending(self, *, before_ms: int | None = None) -> None:
        """T-C3-015 boot sweep: deliver every pending row, then consume it.

        The per-row law of the upstream ``recoverToolJob`` (2.3.2
        restart.ts): map each row through the SAME construction
        ``replay_pending`` uses (:func:`opencode.tools.background.replay_notice_for`
        -- zero drift), deliver via the ``deliver_event_wake`` synthetic
        seam with ``inbox_id`` = the row's notification id (the durable
        ``inbox_enqueued_seen`` idempotency makes a re-settled row admit
        once), then delete the row. A successful delivery INCLUDING the
        idempotent seen-noop consumes the row; a session that no longer
        exists raises ``NotFoundError`` out of the wake -- the notice is
        owed to nobody, so the row is acknowledged by deletion; any other
        delivery failure keeps the row standing for the next boot
        (at-least-once) without breaking the sweep over the remaining
        rows. Kept rows (a running subagent child PY cannot re-drive) are
        skipped whole, and a row whose parent left no durable trace is
        dropped silently.

        ``before_ms`` scopes the sweep to the boot backlog: the App boot
        task passes a cutoff captured synchronously in ``App.__init__``,
        and only rows created strictly before it are swept. Rows born
        during the scan window belong to the in-process delivery path
        (T-C3-014), and sweeping them would race its ownership (the
        87eaee6 CI attempt-2 lesson -- the task's first step runs at the
        first loop yield, which can already be inside the seeding). A
        real backlog row that lands in the same millisecond as the cutoff
        is conservatively left for the next boot (at-least-once). ``None``
        (the direct-call face) sweeps every row.
        """
        if self._database is None:
            return
        from opencode.tools.background import delete_row, list_rows, replay_notice_for

        for stored in await list_rows(self._database):
            if before_ms is not None and stored.created_at_ms >= before_ms:
                continue
            notice, keep_row = await replay_notice_for(self._database, stored)
            if keep_row:
                continue
            if notice is None:
                await delete_row(self._database, stored.row.notification_id)
                continue
            metadata: JsonObject = {
                "source": "background",
                "kind": notice.kind,
                "status": notice.status,
                "title": stored.row.title,
            }
            try:
                await self.deliver_event_wake(
                    notice.session_id,
                    inbox_id=notice.notification_id,
                    text=notice.text,
                    metadata=metadata,
                )
            except NotFoundError:
                await delete_row(self._database, notice.notification_id)
            except Exception:
                continue  # keep the row; the sweep never breaks on one row
            else:
                await delete_row(self._database, notice.notification_id)

    async def pending_subagent_wakes(self, session_id: str) -> list[SubagentObservation]:
        """T-C3-014 read-only face: the session's outstanding subagent wakes.

        The wait loop's third read (after the live shell handles and the
        inbox): the folded ``py.subagent.backgrounded`` observations no
        delivery has popped yet -- each one is a child session whose run is
        still in flight somewhere in this process. A fold failure answers
        empty (the wait loop's exit condition stays read-only, never fatal).
        """
        if self._closed or self._closing:
            return []
        try:
            state = await self._fold_session_state(session_id)
        except StoreError:
            return []
        return list(state.subagent_observations.values())

    async def active_run_id(self, session_id: str) -> str | None:
        """T-C3-014 read-only face: the session's live active run, if any.

        The wait loop's race guard: a wake's promoted run can start inside
        the window between the inbox consume and the ``py.run.started``
        event's delivery, so the pending reads alone cannot prove
        quiescence -- the live record can.
        """
        _require_session_id_arg(session_id)
        if self._closed or self._closing:
            return None
        record = await self._read_live_record(session_id)
        return record.active_run_id

    async def request_session_background(self, session_id: str) -> bool:
        """§3-5 ``session.background``: the wire face survives as a no-op.

        T-C3-005 deleted the subagent foreground wait this used to convert
        (every call backgrounds now), so there is structurally nothing to
        arm: the face keeps its upstream 2.3.2 wire shape (the endpoint
        lives on over the job system's ``backgroundAll``) but answers
        ``False`` and commits nothing on every arm -- idle or not. A
        missing or deleted session still answers NotFound (the TS
        SessionNotFound error); the later job-system port re-gives this
        face teeth.
        """
        _require_session_id_arg(session_id)
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        await self._read_live_record(session_id)
        return False

    async def recover_subagent_observations(self) -> list[str]:
        """§3-3 restart recovery: re-arm observers for outstanding markers.

        The post-recovery-scan follow-up (the _wake_pending_inbox_after_scan
        shape): every session that ever committed a ``py.subagent.backgrounded``
        marker gets its full log folded -- the fold pops every marker whose
        delivery already admitted, so exactly the still-outstanding
        generations re-arm. A parent deleted before its delivery keeps its
        marker (idempotent) and is skipped, never an error; a storage error
        on one parent skips that parent without blocking the others (the
        wake_pending_inbox discipline). In-flight children the R4 scan
        folded to interrupted deliver through the re-armed observer's own
        terminal poll (§4-6), so no special recovery delivery path exists.
        """
        if self._closed or self._closing:
            return []
        armed: list[str] = []
        for session_id in await self._repos.subagent_backgrounded_session_ids():
            try:
                state = await self._fold_session_state(session_id)
            except StoreError:
                continue
            for observation in state.subagent_observations.values():
                self._arm_subagent_observer(
                    session_id,
                    child_session_id=observation.child_session_id,
                    run_id=observation.run_id,
                    agent=observation.agent,
                    description=observation.description,
                    notification_id=observation.notification_id,
                )
                armed.append(observation.notification_id)
        return armed

    def _subagent_event_log(self) -> EventLog:
        """Lazily-built event-log reader over the wired engine (the compaction
        precedent: the same wiring discriminant -- a service constructed
        without the storage engine cannot fold the committed log, so the read
        face fails closed with StoreValidationError instead of half-answering)."""
        if self._subagent_log is None:
            if self._engine is None:
                raise StoreValidationError("subagent recovery requires the storage engine (engine=)")
            self._subagent_log = EventLog(self._engine, repos=self._repos)
        return self._subagent_log

    async def _fold_session_state(self, session_id: str) -> SessionState:
        """Fold one session's full committed log (the recovery.py ``_fold`` shape)."""
        log = self._subagent_event_log()
        state = SessionState()
        after: EventCursor | None = None
        while True:
            page = await log.read(session_id, after, MAX_READ_LIMIT)
            for event in page.items:
                state = apply_event(state, event)
            if not page.has_more:
                return state
            after = page.next_cursor

    async def interrupt(self, session_id: str, *, resume: bool = False) -> InterruptReceipt:
        """§3-5 interrupt: stop the active run (the cancel machinery), optionally resume.

        The cancel face verbatim underneath (``py.run.cancel_requested``
        reason="user" -> cooperative signal -> honest terminal): a session
        with no active run is the idle no-op ``{interrupted: False}`` --
        no event, no error. ``resume=True`` after an interrupted settle
        promotes the oldest pending STEER into a fresh run; queued prompts
        stay parked under BOTH faces (the interrupted settle never
        auto-continues the queue -- the resume drive is the explicit
        control point). A non-local active run is N/A on this face: the
        in-process ``active_run_id`` implies the live task.
        """
        _require_session_id_arg(session_id)
        if not isinstance(resume, bool):
            raise StoreValidationError("resume must be a boolean")
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            record = await self._read_live_record(session_id)
            active = record.active_run_id
        if active is None:
            return InterruptReceipt(
                interrupted=False, session_id=session_id, run_id=None, status=None, resumed_run_id=None
            )
        status = await self.cancel(active)
        interrupted = status.status is RunStatusValue.INTERRUPTED
        resumed: str | None = None
        if resume and interrupted:
            async with self._session_lock(session_id):
                ref = await self._promote_next(session_id, "steer")
            if ref is not None:
                resumed = ref.run_id
        return InterruptReceipt(
            interrupted=interrupted,
            session_id=session_id,
            run_id=active,
            status=status.status,
            resumed_run_id=resumed,
        )

    async def consume_steer(self, session_id: str, run_id: str) -> str | None:
        """§3-3: the runner-facing step-boundary steer hook.

        Called at the top of every step-loop iteration before the state
        re-fold; the per-session lock is taken HERE (the runner's
        EventCommitter shares no service lock) and the oldest pending steer
        commits its ``py.inbox.delivered``. Returns the consumed
        ``inbox_id`` for provenance, or ``None`` (nothing pending -- the
        common case costs one read-model query per step).
        """
        if self._closed or self._closing:
            return None
        async with self._session_lock(session_id):
            return await self._inbox.deliver_steer(session_id, run_id)

    async def wake_pending_inbox(self) -> list[RunRef]:
        """§3-10: the post-recovery-scan idle-wake.

        Every session that still owns pending inbox rows after the startup
        recovery scan gets its oldest queued input auto-started -- one run
        per session; each successor run's own settle recheck chains the
        rest. Steer rows are never woken (an active run consumes them at
        its step boundaries, §3-3). A storage error on one session skips
        that session (its items stay pending and listed) without blocking
        the others. No runner (the storage-only composition) keeps the face
        a strict no-op: an accepted run nobody drives would be a zombie
        busy slot -- the exact R1 hazard this composition avoids.
        """
        if self._runner is None or self._closed or self._closing:
            return []
        woken: list[RunRef] = []
        for session_id in await self._repos.pending_inbox_session_ids():
            try:
                async with self._session_lock(session_id):
                    ref = await self._promote_next(session_id, "input")
            except StoreError:
                continue
            if ref is not None:
                woken.append(ref)
        return woken

    async def _promote_next(self, session_id: str, scope: Literal["input", "steer"]) -> RunRef | None:
        """Promote one pending item into a fresh run (caller holds the session lock).

        ``scope="input"`` mirrors TS ``Promotable="input"``: only
        queue-delivery rows are candidates (the idle boundary consumes
        queued input one at a time, in enqueue order); ``scope="steer"``
        (the interrupt resume drive) consumes only steer rows. A session
        holding an active run, a write-gated status, or a closing service
        never auto-promotes; ``None`` = nothing promoted.
        """
        if self._closed or self._closing:
            return None
        record = await self._read_record(session_id)
        if record.active_run_id is not None:
            return None
        if SessionStatus(record.status) is not SessionStatus.ACTIVE:
            return None
        wanted = "steer" if scope == "steer" else "queue"
        for item in await self._inbox.pending(session_id):
            if item.delivery != wanted:
                continue
            run_id, request_id = await self._inbox.promote(session_id, item)
            self._schedule_run(session_id, run_id)
            return RunRef(session_id=session_id, run_id=run_id, request_id=request_id)
        return None

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

    # -- §7 skills (§6.3 activation half) -------------------------------------

    async def activate(
        self,
        session_id: str,
        skill_id: str,
        source: str,
        expected_hash: str,
        *,
        confirm_hash_change: bool = False,
    ) -> SkillActivationReceipt:
        """Activate one discovered skill: audit, gate, commit ``py.skill.activated``.

        The §6.3 fixed check order (大小→路径→信任→hash) runs before the
        session lock is taken -- pure filesystem validation against the
        current discovery state, state-independent like the prompt argument
        checks (the store's own validate-before-lock pattern). Inside the
        lock: the record status gate (a read_only_diagnostic session never
        activates), the re-activation confirmation gate (§6.3 L347-350: a
        committed activation with a different hash for the same skill id or
        the same declared path is refused unless the host explicitly passes
        ``confirm_hash_change=True``; same-hash re-activation is idempotent
        and commits a second audit event), then the durable commit with
        bounded ConflictError retries. The activation grants nothing (§6.3
        L351-353): no permission, no execution -- only the audit record,
        returned as the receipt.
        """
        _require_session_id_arg(session_id)
        if not isinstance(confirm_hash_change, bool):
            raise StoreValidationError("confirm_hash_change must be a bool (no implicit conversion)")
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        if self._catalog is None:
            raise StoreValidationError("activate requires the skill catalog (catalog=)")
        receipt = self._catalog.activate(skill_id, source, expected_hash)

        async with self._session_lock(session_id):
            record = await self._read_record(session_id)
            status = record.status
            if status == SessionStatus.READ_ONLY_DIAGNOSTIC:
                raise SessionReadOnlyError(
                    f"session {session_id} is read_only_diagnostic", details={"session_id": session_id}
                )
            if status == SessionStatus.CLEANUP_FAILED:
                raise LifecycleCleanupFailedError(
                    f"session {session_id} failed resource cleanup", details={"session_id": session_id}
                )
            if status == SessionStatus.CLOSED:
                raise SessionClosedError(f"session {session_id} is closed", details={"session_id": session_id})
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._require_hash_change_confirmation(session_id, receipt, confirm_hash_change=confirm_hash_change)
            await self._commit_activated(session_id, receipt)
            return receipt

    async def _require_hash_change_confirmation(
        self, session_id: str, receipt: SkillActivationReceipt, *, confirm_hash_change: bool
    ) -> None:
        """The §6.3 L347-350 re-activation gate: a changed hash needs explicit confirmation.

        Reads the session's committed ``py.skill.activated`` events (paged;
        worst-case bounded by the log) and keeps the last committed hash per
        skill id and per declared path. The union is deliberately
        conservative: either face -- same id with a different hash, or same
        path with a different hash -- means the body changed under a face
        the host may not have been looking at, so both refuse without the
        explicit ``confirm_hash_change=True``. A same-hash re-activation
        passes (the fold is idempotent; the commit still writes the audit
        event).
        """
        after: EventCursor | None = None
        by_skill: dict[str, str] = {}
        by_path: dict[str, str] = {}
        log = self._activation_log()
        while True:
            page = await log.read(session_id, after, MAX_READ_LIMIT)
            for event in page.items:
                if event.type == PY_SKILL_ACTIVATED and isinstance(event.data, SkillActivatedData):
                    by_skill[event.data.skill_id] = event.data.hash
                    by_path[event.data.path] = event.data.hash
            if not page.has_more:
                break
            after = page.next_cursor
        previous = by_skill.get(receipt.skill_id)
        if previous is None:
            previous = by_path.get(receipt.path)
        if previous is not None and previous != receipt.hash and not confirm_hash_change:
            raise ConflictError(
                f"skill {receipt.skill_id!r} was already activated with a different body hash "
                f"({previous} -> {receipt.hash}); pass confirm_hash_change=True to confirm the change",
                details={
                    "session_id": session_id,
                    "skill_id": receipt.skill_id,
                    "path": receipt.path,
                    "previous_hash": previous,
                    "new_hash": receipt.hash,
                    "reason": REASON_SKILL_HASH_CHANGED,
                },
            )

    async def _commit_activated(self, session_id: str, receipt: SkillActivationReceipt) -> None:
        """Commit one ``py.skill.activated`` under the session lock (§6.3 L332-343).

        Bounded ConflictError retries with the same batch (the scheduled
        runner commits concurrently, so a stale expected_seq is retried);
        there is no dedupe key -- a same-hash re-activation is a legal
        second audit event, and the changed-hash case is fenced by the
        confirmation gate, not by idempotency.
        """
        draft = EventDraft[SkillActivatedData](
            id=self._ids.event_id(),
            type=PY_SKILL_ACTIVATED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=receipt.durable_record(),
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

    def _activation_log(self) -> EventLog:
        """The lazily-built event-log reader for the confirmation gate.

        The engine is the wiring discriminant (same as the run faces): a
        service constructed without one cannot read the committed log, so it
        fails closed with StoreValidationError instead of skipping the gate.
        """
        if self._skill_log is None:
            if self._engine is None:
                raise StoreValidationError("activate requires the storage engine (engine=)")
            self._skill_log = EventLog(self._engine, repos=self._repos)
        return self._skill_log

    # -- M6 compaction internals (§2.4, T-C1-062) ---------------------------------

    def _compaction_event_log(self) -> EventLog:
        """The lazily-built event-log reader for the settled read face.

        Same wiring discriminant as ``_activation_log``: a service constructed
        without the storage engine cannot fold the committed log, so the read
        face fails closed with StoreValidationError instead of half-answering.
        """
        if self._compaction_log is None:
            if self._engine is None:
                raise StoreValidationError("compaction requires the storage engine (engine=)")
            self._compaction_log = EventLog(self._engine, repos=self._repos)
        return self._compaction_log

    def _compaction(self) -> CompactionEngine | None:
        """The lazily-built §2.4 engine, or ``None`` while the face is dormant.

        Dormant = no summarizer injected (the pre-M6 face, zero behavior).
        Built once over REAL faces only: this service's store (the §3.2 commit
        interface), the settled read face (``settled_history`` below -- the
        EventLog fold plus the repos' log_high), the shared id factory and
        clock, and this service's own per-session commit lock (§3.2 L209: the
        capture, the H read and the short commit lock are the same lock every
        other commit path takes).
        """
        if self._compaction_summarizer is None:
            return None
        if self._compaction_engine is None:
            self._compaction_engine = CompactionEngine(
                store=self._store,
                history=self,
                summarizer=self._compaction_summarizer,
                ids=self._ids,
                clock=self._clock,
                lock=self._session_lock,
            )
        return self._compaction_engine

    async def settled_history(self, session_id: str) -> SettledHistoryView:
        """The real settled read face (§2.4 L118/L126): fold the committed log.

        The engine calls this INSIDE the per-session commit lock (its capture
        phase), so the messages and the watermark below are read under the
        same lock the §3.2 expected_seq/CAS basis uses. ``settled`` here means
        "present in the committed fold with a settling seq": user messages
        settle at ``py.prompt.accepted``, terminal assistants at
        ``py.step.finished``, idle rows at ``py.run.finished`` -- a
        still-open step's generated message rides at its landing seq (the
        engine's boundary math sees it exactly where the fold does; an open
        step is recovery's face to close, §3.3 L210). The materialized
        compaction summary is ``None``-linked and never counted: it is the
        replacement face, not history, which also keeps the strictly
        ascending seqs the engine's fail-closed view validation requires.
        """
        log = self._compaction_event_log()
        state = SessionState()
        after: EventCursor | None = None
        while True:
            page = await log.read(session_id, after=after)
            for event in page.items:
                state = apply_event(state, event)
            if not page.has_more:
                break
            after = page.next_cursor
        settled = tuple(
            SettledMessage(message=message, seq=seq)
            for message, seq in zip(state.messages, state.message_seqs, strict=True)
            if seq is not None
        )
        return SettledHistoryView(messages=settled, log_high=await self._repos.log_high(session_id))

    async def _post_run_compaction(self, session_id: str) -> None:
        """The threshold trigger: one engine pass after a run settles (§2.4 L128).

        Trigger-point ruling (contract leaves the location blank -- §2.4 L128
        names only the threshold, so this is the minimal-intrusion pick,
        recorded in T-C1-062): post-settlement. The session is quiescent (no
        in-flight generation consumes the state mid-replacement), the run's
        own outcome is already terminal and cannot be corrupted by a
        compaction failure, and the settled history is at its local maximum
        when the check runs. The engine itself is stateless per call: every
        trigger re-checks the threshold, so a skipped or failed pass simply
        retries on the next settlement. Failure policy (§2.4 L132): the red
        line is the CONTEXT -- zero replacement, zero partial writes; the
        engine's exception propagates to here and is recorded per session in
        ``_compaction_errors`` (never dropped, never folded into the run's
        schedule-error channel -- the run already settled). ``CancelledError``
        is not caught: a close-time cancel aborts the pass like every other
        await here. Only a committed event notifies the bus (§3.2 L159 step 5
        shape; below_threshold and the discarded pass are no-ops).
        """
        engine = self._compaction()
        if engine is None:
            return
        try:
            outcome = await engine.compact(session_id)
        except Exception as error:  # §2.4 L132: no replacement, error recorded per session
            self._compaction_errors[session_id] = error
            return
        if outcome.event is not None:
            self._bus.notify(session_id, [outcome.event])

    # -- T-C1-094 (P6-S1): the session resource faces ---------------------------

    async def list_sessions(self) -> list[SessionSummary]:
        """Every live session, newest-created first (the ``GET /session`` base).

        The v1 face is the full list: no cursor pagination, no sort or
        filter parameters (the TS SessionsQuery faces stay unported,
        ticket §5 disclosures 1/2). ``deleted`` sessions never appear — the
        tombstone's read-model projection filter lives in
        :meth:`opencode.storage.ReadRepositories.session_summaries`, the
        enumeration base — while their event logs stay in place (the
        delete never erases data, §3 decision 4). A stored status outside
        the §3.2 value set is corruption, classified fail-closed like the
        by-id read instead of surfaced raw.
        """
        rows: list[SessionListRow] = await self._repos.session_summaries()
        summaries: list[SessionSummary] = []
        for row in rows:
            try:
                SessionStatus(row.status)
            except ValueError as error:
                raise StorageCorruptError(
                    f"session {row.session_id} has unknown status {row.status!r}",
                    isolate=True,
                    details={"session_id": row.session_id, "status": row.status},
                ) from error
            summaries.append(
                SessionSummary(
                    session_id=row.session_id,
                    status=row.status,
                    applied_seq=row.applied_seq,
                    active_run_id=row.active_run_id,
                    created_at_ms=row.created_epoch_ms,
                )
            )
        return summaries

    async def get_session(self, session_id: str) -> SessionInfo:
        """The four-field get wire of one live session (§3 decision 2).

        The serialization whitelist is exactly ``{session_id, status,
        applied_seq, active_run_id}`` — ``config_snapshot``/``snapshot_hash``
        /``diagnostic`` are transport-internal faces that never leave the
        service through this route (the export face carries the snapshot
        explicitly instead). A missing or deleted session answers NotFound:
        the tombstone is indistinguishable from never-created on the
        resource face, which is the point of the delete.
        """
        _require_session_id_arg(session_id)
        record = await self._read_live_record(session_id)
        return SessionInfo(
            session_id=record.session_id,
            status=record.status,
            applied_seq=record.applied_seq,
            active_run_id=record.active_run_id,
        )

    async def session_created_data(self, session_id: str) -> SessionCreatedData:
        """The typed ``py.session.created`` payload of one session (P6-S5, T-C1-098).

        The public read face over :meth:`_session_creation` for callers that
        need the created payload without the export/fork bulk: the subagent
        tool reads the parent's config snapshot (the child inherits it
        verbatim, the fork precedent) and walks ``parent_id`` up the chain
        for the depth guard. The read runs over the events log through the
        same fail-closed version routing, so a tombstoned session still
        answers with its creation payload (the durable row survives the
        delete); a session that never existed has no created row and
        surfaces as read-model corruption -- callers that need the resource
        face (``NotFound`` for missing or deleted) read ``get_session``
        first, exactly like fork/export do.
        """
        _require_session_id_arg(session_id)
        data, _ = await self._session_creation(session_id)
        return data

    async def message(self, session_id: str, message_id: str) -> SessionMessage:
        """One projected message by ``(session_id, message_id)`` (§3 decision 6).

        A single-row point probe over the messages unique key — not a page
        walk. The session is read live first (missing or deleted ->
        NotFound), then the message: a mismatched pair (the id exists in
        another session, or nowhere) answers the same NotFound, never a
        cross-session leak.
        """
        _require_session_id_arg(session_id)
        if not isinstance(message_id, str) or not message_id:
            raise StoreValidationError("message_id must be a non-empty string")
        await self._read_live_record(session_id)
        found = await self._repos.message_by_id(session_id, message_id)
        if found is None:
            raise NotFoundError(
                f"message {message_id} does not exist in session {session_id}",
                details={"session_id": session_id, "message_id": message_id},
            )
        return found

    async def delete_session(self, session_id: str) -> None:
        """Tombstone one session (§3 decision 4): one ``py.session.deleted`` commit.

        The event-sourced delete: the tombstone event lands in the log
        (nothing is erased) and the read model flips ``sessions.status`` to
        ``deleted``, which hides every resource face — get/list/messages/
        events all answer NotFound after it, and a repeated delete is the
        same NotFound (the TS NoContent-then-NotFound form). The §7 gate
        order runs under the per-session lock: existence first, then the
        status classes (read_only / cleanup_failed / closed answer their
        own codes), then the active-run gate — a session with an in-flight
        run is SessionBusy (409: a live run is never deleted under; the
        run settles first). No child sessions and no cascade exist to
        delete (PY has no parent chain, DIFFERENT-BY-DESIGN, §5
        disclosure 3).
        """
        _require_session_id_arg(session_id)
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            record = await self._read_live_record(session_id)
            if record.status == SessionStatus.READ_ONLY_DIAGNOSTIC:
                raise SessionReadOnlyError(
                    f"session {session_id} is read_only_diagnostic", details={"session_id": session_id}
                )
            if record.status == SessionStatus.CLEANUP_FAILED:
                raise LifecycleCleanupFailedError(
                    f"session {session_id} failed resource cleanup", details={"session_id": session_id}
                )
            if record.status == SessionStatus.CLOSED:
                raise SessionClosedError(f"session {session_id} is closed", details={"session_id": session_id})
            if record.active_run_id is not None:
                raise SessionBusyError(
                    f"session {session_id} already holds an active run",
                    details={"session_id": session_id, "active_run_id": record.active_run_id},
                )
            await self._commit_deleted(session_id)

    async def _commit_deleted(self, session_id: str) -> None:
        """Commit one ``py.session.deleted`` under the session lock (§3 decision 4).

        Bounded ConflictError retries with the same batch (the scheduled
        runner commits concurrently, so a stale expected_seq is retried);
        there is no dedupe key — the repeated-delete fence is the
        live-record read above (a deleted session answers NotFound before
        this is ever reached).
        """
        draft = EventDraft[SessionDeletedData](
            id=self._ids.event_id(),
            type=PY_SESSION_DELETED,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=SessionDeletedData(),
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

    async def fork_session(self, session_id: str, *, before: str | None = None) -> SessionRef:
        """Copy one session's projected history into a fresh session (§3 decision 3).

        The replay is message-level, not a raw event-row copy: ``events.id``
        and ``runs.run_id`` are global primary keys (0001 schema), so rows
        cannot be re-inserted verbatim. Instead the fork reads the source's
        created snapshot (working_dir + config_snapshot ride verbatim; the
        created wire version follows the source's created row) and its
        projected messages, then commits ONE atomic batch under the new
        id: ``py.session.created`` + one ``py.message.imported`` per copied
        message (``expected_seq=0`` — the new session is new). ``before``
        truncates the copy to the messages before that id (exclusive); an
        omitted ``before`` copies the full history. The source is never
        touched (a read-only copy — no event lands in its log, so fork
        after fork is stable). A missing or deleted source, or a ``before``
        message that does not exist in the source, answers NotFound.
        """
        _require_session_id_arg(session_id)
        if before is not None and (not isinstance(before, str) or not before):
            raise StoreValidationError("before must be None or a non-empty string")
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            await self._read_live_record(session_id)
            data, _ = await self._session_creation(session_id)
            history = await self._projected_messages(session_id, before=before)
            fork_id = self._ids.session_id()
            created_at = self._clock()
            drafts: list[EventDraft[Any]] = [
                self._created_draft(fork_id, data.working_dir, data.config_snapshot, created_at)
            ]
            drafts.extend(
                EventDraft[MessageImportedData](
                    id=self._ids.event_id(),
                    type=PY_MESSAGE_IMPORTED,
                    version=EVENT_VERSION,
                    session_id=fork_id,
                    created_at=created_at,
                    data=MessageImportedData(message=message),
                    metadata={},
                )
                for message in history
            )
            committed = await self._store.commit(fork_id, EventBatch(self._ids.batch_id(), tuple(drafts)), 0)
            self._bus.notify(fork_id, committed)
            return SessionRef(session_id=fork_id)

    async def export_session(self, session_id: str) -> SessionExport:
        """The self-describing transfer of one live session (§3 decision 5).

        The exact shape :meth:`import_session` restores: identity + the
        created-event faces (working_dir, config_snapshot as wire JSON,
        snapshot_hash, created_at_ms) + the current status + the full
        projected message history in order. Everything import needs rides
        in the payload — no server-side state is consulted on the way back
        in, so an export is portable across boots and hosts.
        """
        _require_session_id_arg(session_id)
        record = await self._read_live_record(session_id)
        data, created_epoch_ms = await self._session_creation(session_id)
        history = await self._projected_messages(session_id, before=None)
        return SessionExport(
            session_id=session_id,
            status=record.status,
            working_dir=data.working_dir,
            config_snapshot=data.config_snapshot.model_dump(mode="json"),
            snapshot_hash=data.snapshot_hash,
            created_at_ms=created_epoch_ms,
            messages=tuple(history),
        )

    async def import_session(self, payload: SessionExport | JsonObject) -> SessionRef:
        """Restore one exported payload as a session under its own id (§3 decision 5).

        Strict fail-closed validation first (ticket §8: nothing is silently
        dropped): the exact key set, the field types, the §6.2 canonical
        snapshot-hash re-verification, and every message through the §2.1
        wire union (an undecodable message is ValidationError; the message
        count is conserved or the import refuses). The restore replays as
        events like every write — no read-model writes: ONE atomic batch
        of ``py.session.created`` (``created_at`` restored from the
        payload, so the list ordering survives the round trip) plus one
        ``py.message.imported`` per message, committed with
        ``expected_seq=0`` under the payload's own session id. A session
        already living at that id answers Conflict first — a deleted
        tombstone included, since its sessions row survives the delete —
        and the ``expected_seq=0`` CAS fences the check-to-commit race (no
        retry loop: a conflict here means "the id is occupied", which is
        the answer, not a retryable condition). The restored session is
        ACTIVE regardless of the exported status (read_only /
        cleanup_failed / closed are boot-local marks, not transferable
        state; disclosed).
        """
        raw = payload.to_payload() if isinstance(payload, SessionExport) else payload
        session_id, _status, working_dir = _import_identity(raw)
        raw_snapshot = raw["config_snapshot"]
        if not isinstance(raw_snapshot, dict):
            raise StoreValidationError("import payload field 'config_snapshot' must be a JSON object")
        snapshot = _coerce_config_snapshot(raw_snapshot)
        _verify_snapshot_hash(raw["snapshot_hash"], snapshot)
        created_at_ms = _require_epoch_ms(raw["created_at_ms"])
        messages = _import_messages(session_id, raw["messages"])
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        if await self._repos.session(session_id) is not None:
            raise ConflictError(f"session {session_id} already exists", details={"session_id": session_id})
        created_at = from_epoch_ms(created_at_ms)
        drafts: list[EventDraft[Any]] = [self._created_draft(session_id, working_dir, snapshot, created_at)]
        drafts.extend(
            EventDraft[MessageImportedData](
                id=self._ids.event_id(),
                type=PY_MESSAGE_IMPORTED,
                version=EVENT_VERSION,
                session_id=session_id,
                created_at=created_at,
                data=MessageImportedData(message=message),
                metadata={},
            )
            for message in messages
        )
        committed = await self._store.commit(session_id, EventBatch(self._ids.batch_id(), tuple(drafts)), 0)
        self._bus.notify(session_id, committed)
        return SessionRef(session_id=session_id)

    async def _session_creation(self, session_id: str) -> tuple[SessionCreatedData, int]:
        """The decoded ``py.session.created`` data + its epoch-ms stamp, fail closed.

        The resource faces that need the created faces (fork's verbatim
        snapshot copy, export's transfer payload) route the wire model by
        the row's version exactly like the CLI selection face; a stored row
        outside 1/2/3 cannot appear (repos pins it) and still answers
        corruption here rather than coercing through the nearest model. A
        sessions row with no created row is read-model corruption: the
        projector inserts the row in the same transaction as the event, so
        the pair is atomic by construction.
        """
        row: SessionCreatedRow | None = await self._repos.session_created_by_id(session_id)
        if row is None:
            raise StorageCorruptError(
                f"session {session_id} has a sessions row but no py.session.created row",
                isolate=True,
                details={"reason": REASON_READ_MODEL_MISSING, "session_id": session_id},
            )
        model: type[SessionCreatedData]
        if row.version == 3:
            model = SessionCreatedDataV3
        elif row.version == 2:
            model = SessionCreatedDataV2
        elif row.version == 1:
            model = SessionCreatedData
        else:
            raise StorageCorruptError(
                f"session {session_id} has a py.session.created row of unexpected version {row.version}",
                isolate=True,
                details={"session_id": session_id, "version": row.version},
            )
        try:
            return model.model_validate(row.data), row.created
        except PydanticValidationError as error:
            raise StorageCorruptError(
                f"session {session_id} has an undecodable py.session.created row",
                isolate=True,
                details={"session_id": session_id},
            ) from error

    async def _projected_messages(self, session_id: str, *, before: str | None) -> list[SessionMessage]:
        """Collect the projected messages in seq order, truncated before ``before``.

        ``before`` is validated by point probe first (a message id that
        exists nowhere in this session is NotFound — the TS MessageNotFound
        face), then the pages walk until the probe id is hit (exclusive)
        or the history ends. The walk runs under the caller's session
        lock, so the collected history is a consistent snapshot against
        concurrent prompt commits.
        """
        if before is not None and await self._repos.message_by_id(session_id, before) is None:
            raise NotFoundError(
                f"message {before} does not exist in session {session_id}",
                details={"session_id": session_id, "message_id": before},
            )
        hit = False
        collected: list[SessionMessage] = []
        after: str | None = None
        while not hit:
            page = await self._repos.messages(session_id, after_message_id=after, limit=MAX_PAGE_LIMIT)
            for item in page.items:
                if before is not None and item.id == before:
                    hit = True
                    break
                collected.append(item)
            if not page.has_more:
                break
            after = page.next_after_message_id
        if before is not None and not hit:
            # Unreachable by construction (the probe found the row and the
            # walk enumerates the whole projection) — fail closed anyway.
            raise StorageCorruptError(
                f"session {session_id} is missing the projected row of message {before}",
                isolate=True,
                details={"session_id": session_id, "message_id": before},
            )
        return collected

    # -- T-C1-096 (P6-S3): the manual compaction face -----------------------------

    async def compact_session(self, session_id: str) -> CompactionOutcome:
        """One manual compaction pass on a live session (T-C1-096 §3 decision 2).

        The guard order is the S1 delete precedent (§3 decision 4), all under
        the session lock: the live-record read (missing or deleted tombstone
        -> NotFound), then the status classes (read_only / cleanup_failed /
        closed answer their own codes), then the active-run gate (409 -- a
        live run is never compacted under; it settles first). The lock is
        then RELEASED before the engine trip: the engine re-takes it for the
        capture and the short commit (its captured expected_seq/CAS owns the
        release-to-commit race) and the summary call runs unlocked by design
        (§2.4 L126). A composition without the summarizer seam answers
        StoreValidationError -- the ``_compaction_event_log`` precedent: the
        face fails closed instead of half-answering or silently no-oping
        (a 200 for a pass that never ran is the forbidden fiction).

        Error policy (§3 decision 6, deliberately split from the auto
        trigger): the manual face does NOT swallow -- the engine's exception
        propagates to the caller on the normal §8 mapping, and it is NOT
        recorded in ``_compaction_errors`` (that ledger belongs to the auto
        trigger's best-effort background policy; a manual failure is
        caller-visible, §8 delivery discipline). The bus notification keeps
        the ``_post_run_compaction`` shape: only a committed event notifies
        (below_threshold and the discarded pass are no-ops). One synchronous
        engine trip per call -- its latency is the summary model call
        whenever the threshold holds; there is no background job and no
        queue steering (ticket §5 disclosure 4).
        """
        _require_session_id_arg(session_id)
        if self._closed or self._closing:
            raise SessionClosedError("service is closed")
        async with self._session_lock(session_id):
            if self._closed or self._closing:
                raise SessionClosedError("service is closed")
            record = await self._read_live_record(session_id)
            if record.status == SessionStatus.READ_ONLY_DIAGNOSTIC:
                raise SessionReadOnlyError(
                    f"session {session_id} is read_only_diagnostic", details={"session_id": session_id}
                )
            if record.status == SessionStatus.CLEANUP_FAILED:
                raise LifecycleCleanupFailedError(
                    f"session {session_id} failed resource cleanup", details={"session_id": session_id}
                )
            if record.status == SessionStatus.CLOSED:
                raise SessionClosedError(f"session {session_id} is closed")
            if record.active_run_id is not None:
                raise SessionBusyError(
                    f"session {session_id} already holds an active run",
                    details={"session_id": session_id, "active_run_id": record.active_run_id},
                )
        engine = self._compaction()
        if engine is None:
            raise StoreValidationError("manual compaction requires the compaction engine (compaction_summarizer=)")
        outcome = await engine.compact(session_id)
        if outcome.event is not None:
            self._bus.notify(session_id, [outcome.event])
        return outcome

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
            except ConflictError:
                # #174: the check-first + CAS retry loop exhausted its
                # attempts, so this run's intent row is NOT committed -- but
                # the close lifecycle itself must not die here: the
                # cooperative signal below still fires, Phases 1.5/2/3 still
                # run, and close() still reaches _closed=True. The
                # uncommitted intent on a non-terminal run is owned by R4
                # durable startup recovery (CORE-CONTRACTS L464), never by a
                # shutdown hook. TS alignment: run-coordinator.ts L142-162
                # fires interrupt fire-and-forget ("nobody benefits from
                # waiting out cleanup here ... must acknowledge immediately
                # even when finalizers are slow"), and execution.ts L110-146
                # routes lifecycle-report failures through reportLifecycle
                # (tapCause -> logError -> asVoid): a shutdown-face publish
                # failure is logged, never escalated.
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
        # Phase 2.5 (T-C1-101): the background subagent observers are
        # process-local arms of durable markers. Every scheduled run has
        # settled, so nothing new can deliver: cancel the observers and let
        # a mid-flight (bounded) admission commit finish first -- the
        # marker of an undelivered generation stays outstanding and the
        # next boot's re-arm re-observes it (the TS durable notification
        # marker surviving the process). The conversion bookkeeping dies
        # with the process faces (no wait can still be live).
        observers = [entry[1] for entry in self._subagent_observers.values()]
        self._subagent_observers.clear()
        for task in observers:
            task.cancel()
        if observers:
            await asyncio.gather(*observers, return_exceptions=True)
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

    async def _read_live_record(self, session_id: str) -> SessionRecord:
        """The live-record read of the resource faces: missing OR deleted -> NotFound.

        The T-C1-094 delete tombstone is a data-preserving hide (§3
        decision 4): after it, the session's resource faces answer exactly
        like a never-created session. The folded log and the sessions row
        survive (``rebuild`` still reaches them); only the resource faces
        refuse. ``_read_record``'s corruption mapping applies unchanged
        underneath.
        """
        record = await self._read_record(session_id)
        if SessionStatus(record.status) is SessionStatus.DELETED:
            raise NotFoundError(f"session {session_id} is deleted", details={"session_id": session_id})
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
        an error. R4-S4: a cross-process crash leaves the same non-terminal
        durable log with no in-memory record; the startup recovery scan
        (T-C1-037) closes it from the log alone. A NORMAL completion (the
        run settled, whatever its terminal outcome) is followed by one
        threshold-checked compaction pass (§2.4 L128, ``_post_run_compaction``
        -- dormant unless the summarizer seam is injected).
        """
        assert self._runner is not None
        settled = False
        result: object | None = None
        try:
            result = await self._runner.run(session_id, run_id)
            settled = True
        except asyncio.CancelledError:
            raise
        except Exception as error:  # recorded here; surfaced by cancel/close
            self._schedule_errors[run_id] = error
        finally:
            self._tasks.pop(run_id, None)
        if settled:
            # §2.4 L128 (T-C1-062): the run settled -- one threshold-checked
            # compaction pass follows it. A cancelled or crashed runner never
            # reaches here (no trigger, no replacement, §2.4 L132 matrix).
            await self._post_run_compaction(session_id)
            # §3-4 (T-C1-100): the idle-wake doorbell. The queued-input
            # recheck runs under the per-session commit lock, so a prompt
            # that admitted a queue item while this run's terminal event
            # committed (the runner's EventCommitter shares no service lock)
            # cannot slip past: either the admission lands before this
            # recheck reads the inbox (promoted here) or the prompt's own
            # busy-gate read already saw the settle (never parked). One
            # queued input per settle; the successor run's own settle
            # chains the rest.
            if getattr(result, "outcome", None) == "interrupted":
                # §3-5: a deliberately interrupted run never auto-continues
                # the queue -- interrupt leaves everything parked and the
                # resume drive is the explicit control point (RunResult's
                # outcome read structurally: runner imports this module,
                # so the concrete type cannot).
                return
            async with self._session_lock(session_id):
                await self._promote_next(session_id, "input")

    async def _commit_cancel_intent(self, session_id: str, run_id: str, reason: Literal["user", "shutdown"]) -> None:
        """Commit one ``py.run.cancel_requested`` under the session lock.

        Check-first idempotency: the ``(session_id, "cancel:"+run_id)`` row is
        queried before committing (the store replays only same-batch retries),
        so repeated cancels and a cancel/close overlap never re-commit the
        intent. Each ConflictError retry re-checks the run state first -- the
        scheduled runner commits concurrently and may have committed the
        terminal while we retried, and an intent must never land on a
        terminal run (late cancel never overwrites, §7 L335).

        T-C3-020 (CI 45031014521): inside one attempt the watermark is read
        FIRST and ``runs.status`` LAST, so the status read is the final read
        before the CAS. The runner's terminal is one store transaction (CAS +
        event + the ``runs.status`` projection, §3.2): a terminal visible to
        the status read is durable (the attempt returns above), and one that
        commits after it moves the watermark, so the CAS raises ConflictError
        and the retry re-checks the status. The reverse order read the
        watermark AFTER the status, so a terminal landing in between passed
        the CAS and reached the fold, which rejects the intent as an illegal
        transition (StorageCorruptError, isolate=False) -- neither ``cancel``
        nor the close lifecycle treats that as a late cancel, and close()
        died in Phase 1 on CI.
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
            expected_seq = await self._repos.log_high(session_id)
            status = await self._runs_status_value(run_id)
            if status is not None and status.is_terminal:
                # The runner finished first; the caller re-reads and returns
                # the original terminal status (no intent, no overwrite).
                return
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
