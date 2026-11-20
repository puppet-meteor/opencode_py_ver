"""Read-only repositories over the S4 read model (R1-S6, CORE-CONTRACTS §3.2.1/§7).

Two query surfaces only -- no writes, no bus imports (storage stays bus-free,
asserted structurally by tests/integration/test_eventlog.py), no external
calls (no clock, no randomness, no network/subprocess, the S4 assertion
pattern): queries are deterministic functions of stored rows.

- :meth:`ReadRepositories.session` reads one sessions row (status/applied_seq
  and the other S4 read-model fields) for the EventLog diagnostic gate and the
  S7 status query. A missing row returns ``None`` (the caller decides whether
  that is NotFound or corruption); per §3.2.1 "session_status 仍可查" holds in
  every non-active status, ``read_only_diagnostic`` included.
- :meth:`ReadRepositories.messages` pages the projected messages in event-seq
  order (§2.1: message ownership and ordering live in the session projection,
  never in ID lexicographic order), cursor-paginated by ``after_message_id``
  (exclusive) with a clamped limit.

T-C1-037 (R4-S4) appended the startup-recovery scan faces -- still read-only,
bus-free, no external calls: :meth:`ReadRepositories.all_session_owners` /
:meth:`ReadRepositories.non_terminal_runs` /
:meth:`ReadRepositories.pending_permissions` enumerate the sessions rows, the
non-terminal runs rows and the still-pending permissions rows for the recovery
coordinator's shortlist (§3.2.1: these queries never trigger recovery -- the
coordinator is the only consumer and runs once at startup).

T-C1-010 (R1-S7) appended the command-layer query faces -- still read-only,
bus-free, no external calls: :meth:`ReadRepositories.log_high` (the §3.2
expected_seq source), :meth:`ReadRepositories.prompt_by_dedupe_key` /
:meth:`ReadRepositories.prompt_by_run_id` (the §7 idempotency/run lookups over
``PromptAcceptedData``) and :meth:`ReadRepositories.session_by_active_run`
(the R1 run_status lookup). All of them fail closed on undecodable stored
rows with the sanitized reason pattern below.

T-C1-040 (R4-S6) appended the CLI session-selection faces
(:meth:`ReadRepositories.session_created_by_id` /
:meth:`ReadRepositories.latest_session_created_by_working_dir`); T-C1-094
(P6-S1) appends the session resource faces (:meth:`ReadRepositories.session_summaries`
-- the created-row-enumeration list base -- and :meth:`ReadRepositories.message_by_id`
-- the single-message point query), all still read-only, bus-free and
fail-closed; ``messages`` additionally answers NotFound for a ``deleted``
session (the tombstone's projection filter).

Fail-closed semantics (§3.2.1 "读取仍可用" boundary): a sessions row missing
while the event log exists is NOT faked as an empty history -- it raises
StorageCorrupt (``sessions_row_missing_with_log``); a message row whose data
no longer decodes into the §2.1 message union (user/assistant/idle) raises
StorageCorrupt locating the offending row. Unknown cursor message ids raise
InvalidCursor; a never-created session raises NotFound.

The JSON-column decode helper ``_json_column`` is imported from
:mod:`opencode.storage.txn` (private cross-module import -- a known tradeoff
accepted by the task book: the shared codec module extraction is deliberately
NOT done here; see the progress file for the reviewer note).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.contracts import (
    EVENT_VERSION,
    PY_PROMPT_ACCEPTED,
    CoreErrorCode,
    JsonObject,
    PromptAcceptedData,
)
from opencode.contracts.messages import (
    AssistantMessage,
    IdleMessage,
    SessionMessage,
    UserMessage,
)
from opencode.storage.txn import (
    REASON_READ_MODEL_MISSING,
    REASON_STORED_DECODE_FAILED,
    StorageCorruptError,
    StoreError,
    _json_column,
)

#: §2.1 message-union dispatch: the stored ``data["type"]`` tag selects the
#: wire model; a missing or unknown tag is a corrupt row (fail closed below).
_MESSAGE_MODELS: dict[str, type[SessionMessage]] = {
    "user": UserMessage,
    "assistant": AssistantMessage,
    "idle": IdleMessage,
}

#: Read-side limit bounds (symmetric with the EventLog read contract, §4).
MIN_PAGE_LIMIT = 1
MAX_PAGE_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 100

#: A projected message row that no longer decodes (sanitized reason).
REASON_MESSAGE_ROW_DECODE_FAILED = "message_row_decode_failed"

#: A sessions row whose JSON columns no longer decode (sanitized reason).
REASON_SESSION_ROW_DECODE_FAILED = "session_row_decode_failed"

#: A dedupe-keyed events row that is not the expected py.prompt.accepted v1
#: (sanitized reason; T-C1-010 §7 idempotency query face).
REASON_DEDUPE_ROW_UNEXPECTED = "dedupe_row_unexpected_type"

#: A ``py.session.created`` events row whose JSON data no longer decodes or is
#: not a JSON object (sanitized reason; T-C1-040 R4-S6 selection face).
REASON_CREATED_ROW_DECODE_FAILED = "created_row_decode_failed"

#: More than one ``py.session.created`` row for one session_id, or the same
#: session appearing twice in the working-dir enumeration (sanitized reason;
#: T-C1-040 — one session has exactly one creation event by construction).
REASON_CREATED_ROW_DUPLICATED = "created_row_duplicated"

#: A ``py.session.created`` row whose version is outside the registry's
#: registered 1/2/3 set (sanitized reason; T-C1-040 — the commit path rejects
#: unknown versions, so a stored row outside the set is corruption).
REASON_CREATED_ROW_UNEXPECTED_VERSION = "created_row_unexpected_version"

#: An inbox row whose message payload no longer decodes as a user message
#: (sanitized reason; T-C1-100 — the parked payload rode the committed
#: ``py.inbox.enqueued`` event verbatim, so a row that does not decode is
#: stored corruption).
REASON_INBOX_ROW_DECODE_FAILED = "inbox_row_decode_failed"

#: The registered wire versions of ``py.session.created`` (the three
#: EventDefinition rows in the contracts registry; pinned here so repos stays
#: contracts-registry-free — the same pinned-vocabulary tradeoff as the runs
#: and permissions scan statements above).
_SESSION_CREATED_VERSIONS = (1, 2, 3)

_SELECT_SESSION = sa.text(
    "SELECT session_id, status, active_run_id, config_snapshot, snapshot_hash, applied_seq, diagnostic "
    "FROM sessions WHERE session_id = :sid"
)
_SELECT_SESSION_ID_BY_RUN = sa.text(
    "SELECT session_id FROM sessions WHERE active_run_id = :rid ORDER BY session_id LIMIT 1"
)
_SELECT_LOG_HIGH = sa.text("SELECT COALESCE(MAX(seq), 0) AS h FROM events WHERE session_id = :sid")
_SELECT_MESSAGE_SEQ = sa.text("SELECT seq FROM messages WHERE session_id = :sid AND message_id = :mid")
_SELECT_MESSAGE_PAGE = sa.text(
    "SELECT message_id, seq, data FROM messages WHERE session_id = :sid AND seq > :after ORDER BY seq LIMIT :limit"
)
_SELECT_EVENT_BY_DEDUPE = sa.text(
    "SELECT id, session_id, seq, type, version, data FROM events WHERE session_id = :sid AND dedupe_key = :dk"
)
_SELECT_PROMPT_EVENTS = sa.text(
    "SELECT id, session_id, seq, type, version, data FROM events "
    "WHERE session_id = :sid AND type = 'py.prompt.accepted' ORDER BY seq"
)
# -- T-C1-022 (R2-S7 ④-3): the service's consolidated single-row statements --
# verbatim column sets/WHERE clauses (the pre-consolidation sa.text block in
# session/service.py; existence probes intentionally lighter than the full-row
# faces -- no JSON column decode, classification stays with the caller).
_SELECT_RUN_BY_ID = sa.text("SELECT session_id, request_id, status, error FROM runs WHERE run_id = :rid")
_SELECT_RUN_ID_BY_REQUEST = sa.text("SELECT run_id FROM runs WHERE session_id = :sid AND request_id = :rid")
_SELECT_RUN_STATUS_BY_ID = sa.text("SELECT status FROM runs WHERE run_id = :rid")
_SELECT_SESSION_OWNER = sa.text("SELECT session_id, status FROM sessions WHERE session_id = :sid")
_SELECT_MESSAGE_SESSION_ID = sa.text("SELECT session_id FROM messages WHERE message_id = :mid")
_SELECT_EVENT_ENVELOPE_BY_DEDUPE = sa.text(
    "SELECT id, type, version FROM events WHERE session_id = :sid AND dedupe_key = :dk"
)
# -- T-C1-037 (R4-S4): the startup-recovery scan statements. The non-terminal
# run vocabulary restates §2.1 L59-61 -- pinned to ``_RUN_STATUS_NON_TERMINAL``
# in session/service.py (L258) and mirrored by ``_RUN_NON_TERMINAL`` in
# session/state.py; repos must not import the session layer (import cycle), so
# the literal set is the scan's own pinned copy. ``'pending'`` likewise restates
# ``PermissionState.PENDING`` (session/state.py).
_SELECT_ALL_SESSION_OWNERS = sa.text("SELECT session_id, status FROM sessions ORDER BY session_id")
_SELECT_NON_TERMINAL_RUNS = sa.text(
    "SELECT run_id, session_id, request_id, status, error FROM runs "
    "WHERE status IN ('accepted', 'running', 'cancelling') ORDER BY session_id, run_id"
)
_SELECT_PENDING_PERMISSIONS = sa.text(
    "SELECT session_id, permission_id, execution_id FROM permissions "
    "WHERE state = 'pending' ORDER BY session_id, permission_id"
)
# -- T-C1-100 (P7-S1): the steering-inbox read faces. The 0004 table is
# pending-only -- ``py.inbox.delivered``/``py.inbox.cancelled`` delete the
# row in the same commit as the consumption/revocation -- so the list face
# needs no state filter; ``enqueued_seq`` is the enqueue-order key.
_SELECT_INBOX = sa.text(
    "SELECT session_id, inbox_id, kind, delivery, request_id, enqueued_seq, message "
    "FROM inbox WHERE session_id = :sid ORDER BY enqueued_seq"
)
_SELECT_INBOX_ITEM = sa.text(
    "SELECT session_id, inbox_id, kind, delivery, request_id, enqueued_seq, message "
    "FROM inbox WHERE session_id = :sid AND inbox_id = :iid"
)
_SELECT_PENDING_INBOX_SESSIONS = sa.text("SELECT session_id FROM inbox GROUP BY session_id ORDER BY session_id")
# -- T-C1-101 (P7-S2): the subagent background re-arm scan statement. The
# startup re-arm enumerates every session that ever committed a
# ``py.subagent.backgrounded`` marker (the parent's log); the fold of that
# parent's full log then decides which observations are still outstanding
# (a delivered generation's marker was popped at the synthetic admission),
# so this face is the candidate set, the same one-shot GROUP BY shape as
# the pending-inbox wake scan above. The literal type string restates the
# contracts registry the same way the pinned statements above do (repos
# must not import the session layer; the events table's type column is the
# scan's own vocabulary).
_SELECT_SUBAGENT_BACKGROUNDED_SESSIONS = sa.text(
    "SELECT session_id FROM events WHERE type = 'py.subagent.backgrounded' GROUP BY session_id ORDER BY session_id"
)
# -- T-C3-006: the durable wake-idempotency scan. The 0004 inbox table is
# pending-only (delivered/cancelled rows delete in the same commit), so a
# replayed event's "admitted once" face must read the append-only log:
# every synthetic admission commits one ``py.inbox.enqueued`` carrying the
# caller's ``inbox_id`` verbatim. The literal type string restates the
# contracts registry like every pinned statement above (repos must not
# import the session layer).
_SELECT_INBOX_ENQUEUED_DATA = sa.text("SELECT data FROM events WHERE session_id = :sid AND type = 'py.inbox.enqueued'")
# -- T-C1-040 (R4-S6): the CLI session-selection statements. The sessions
# table carries no working_dir column (0001_initial), so both selection
# queries scan the events log by type; ``created`` is the INTEGER epoch-ms
# column (0001_initial L64), so the numeric ORDER BY is the "most recent
# session" ordering with the session_id DESC tail as the deterministic
# tie-break for rows minted within the same millisecond.
_SELECT_SESSION_CREATED_BY_ID = sa.text(
    "SELECT version, created, data FROM events WHERE session_id = :sid AND type = 'py.session.created' ORDER BY seq"
)
_SELECT_SESSION_CREATED_ALL = sa.text(
    "SELECT session_id, version, created, data FROM events "
    "WHERE type = 'py.session.created' ORDER BY created DESC, session_id DESC"
)
# -- T-C1-094 (P6-S1): the session resource read faces. The list base is the
# events table's created-row enumeration (the T-C1-040 pattern) LEFT JOINed
# with the sessions row for the read-model faces -- the join direction
# matters: a minimal diagnostic record (no created row) is legal §3.2.1
# state and simply never appears, while a created row whose sessions row is
# missing is read-model corruption raised below (never silently dropped).
_SELECT_SESSION_CREATED_WITH_RECORD = sa.text(
    "SELECT e.session_id AS session_id, s.status AS status, s.applied_seq AS applied_seq, "
    "s.active_run_id AS active_run_id, e.created AS created FROM events e "
    "LEFT JOIN sessions s ON s.session_id = e.session_id "
    "WHERE e.type = 'py.session.created' ORDER BY e.created DESC, e.session_id DESC"
)
_SELECT_MESSAGE_BY_ID = sa.text(
    "SELECT message_id, seq, data FROM messages WHERE session_id = :sid AND message_id = :mid"
)


class NotFoundError(StoreError):
    """The requested session was never created (§4: 未建 session 报 NotFound)."""

    code = CoreErrorCode.NOT_FOUND


class InvalidCursorError(StoreError):
    """A pagination cursor is malformed or does not belong to this session/query."""

    code = CoreErrorCode.INVALID_CURSOR


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One sessions row as the S4 read model sees it (status/applied_seq face)."""

    session_id: str
    status: str
    applied_seq: int
    active_run_id: str | None = None
    config_snapshot: JsonObject | None = None
    snapshot_hash: str | None = None
    diagnostic: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class MessagePage:
    """One ordered page of projected messages (cursor-paginated by message id)."""

    items: list[SessionMessage]
    next_after_message_id: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class AcceptedPrompt:
    """One committed ``py.prompt.accepted`` located by dedupe key or run id (§7).

    The §7 idempotency comparison needs the original ``message.text`` verbatim
    (no trimming, no Unicode normalization); ``run_id``/``request_id`` rebuild
    the original :class:`~opencode.session.service.RunRef`. T-C1-010 query face.
    """

    request_id: str
    run_id: str
    message_text: str


@dataclass(frozen=True, slots=True)
class RunRow:
    """One runs row's raw columns (T-C1-022 ④-3 consolidated run probe).

    ``error`` is passed through exactly as stored (no decode, no coercion) --
    deciding whether a stored error payload decodes is the service caller's
    job, matching the pre-consolidation inline read this replaces.
    """

    session_id: str
    request_id: str
    status: str
    error: str | None


@dataclass(frozen=True, slots=True)
class SessionOwnerRow:
    """A sessions row's identity/status pair (④-3 light owner probe)."""

    session_id: str
    status: str


@dataclass(frozen=True, slots=True)
class DedupeEventRow:
    """An events row's envelope columns only (④-3 cancel-intent probe)."""

    id: str
    type: str
    version: int


@dataclass(frozen=True, slots=True)
class ScanRunRow:
    """One non-terminal runs row for the recovery scan (T-C1-037).

    ``RunRow``'s raw-columns split plus the run id the cross-session
    enumeration needs (the by-PK probe never carried it); ``error`` is
    passed through exactly as stored, no decode, no coercion.
    """

    session_id: str
    run_id: str
    request_id: str
    status: str
    error: str | None


@dataclass(frozen=True, slots=True)
class PendingPermissionRow:
    """One still-pending permissions row for the recovery scan (T-C1-037).

    The row#27① orphan face: the coordinator cross-checks these against the
    folded truth and closes the orphans with ``permission.closed(cancelled)``.
    """

    session_id: str
    permission_id: str
    execution_id: str


@dataclass(frozen=True, slots=True)
class SessionCreatedRow:
    """One ``py.session.created`` events row's raw columns (T-C1-040, R4-S6).

    The CLI session-selection face: ``version``/``created``/``data`` are the
    raw stored columns (0001_initial) — deciding which wire model decodes
    ``data`` (SessionCreatedData v1/v2/v3 by version) stays with the caller,
    the same raw-columns split as :class:`RunRow`. ``created`` is the INTEGER
    epoch-ms event timestamp, the "most recent" ordering key.
    """

    session_id: str
    version: int
    created: int
    data: JsonObject


@dataclass(frozen=True, slots=True)
class SessionListRow:
    """One live session's list face (T-C1-094, P6-S1): the four get-wire faces + created.

    The base of ``GET /session``: the sessions-row status/applied_seq/
    active_run_id faces plus the created-row epoch-ms timestamp (the list
    ordering key). ``deleted`` sessions never appear (the tombstone's
    read-model projection filter); ``status`` is the raw stored text —
    classifying unknown values is the caller's job (the session-layer gate).
    """

    session_id: str
    status: str
    applied_seq: int
    active_run_id: str | None
    created_epoch_ms: int


@dataclass(frozen=True, slots=True)
class InboxItemRow:
    """One pending steering-inbox row (T-C1-100, P7-S1).

    The list/cancel/update read face: the parked user turn decodes
    fail-closed into ``UserMessage`` (it rode the committed
    ``py.inbox.enqueued`` event verbatim); ``enqueued_seq`` is the
    enqueue-order key; ``kind``/``delivery`` are the raw stored texts --
    classifying unknown values is the caller's job (the same
    pinned-vocabulary tradeoff as the scan statements).
    """

    session_id: str
    inbox_id: str
    kind: str
    delivery: str
    request_id: str | None
    enqueued_seq: int
    message: UserMessage


def _corrupt(reason: str, message: str, details: JsonObject) -> StorageCorruptError:
    """Sanitized StorageCorrupt over already-stored read-model data (§3.2.1)."""
    return StorageCorruptError(message, isolate=True, details=details)


def clamp_page_limit(limit: int) -> int:
    """Clamp a page limit into 1..1000 (§4 limit semantics; bools/non-ints reject)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer (bools are rejected)")
    return max(MIN_PAGE_LIMIT, min(MAX_PAGE_LIMIT, limit))


class ReadRepositories:
    """Read-only query surface over sessions/messages (S4 read model).

    One instance may serve any session. Every method opens its own short read
    transaction; nothing here writes, locks, notifies, or performs external
    calls (§3.2.1: queries neither replay the log nor trigger recovery).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def session(self, session_id: str) -> SessionRecord | None:
        """Read one sessions row; ``None`` when the row does not exist.

        The status is returned as stored (unknown values included) -- deciding
        what an unknown status means is the caller's (commit-path) job. JSON
        columns are decoded; an undecodable column raises StorageCorrupt with
        the sanitized reason (no raw row content in details).
        """
        _require_session_id(session_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_SESSION, {"sid": session_id})).first()
        if row is None:
            return None
        try:
            config_snapshot = _json_column(row.config_snapshot)
            diagnostic = _json_column(row.diagnostic)
        except ValueError as error:
            raise _corrupt(
                REASON_SESSION_ROW_DECODE_FAILED,
                f"session {session_id} has an undecodable JSON column",
                {"reason": REASON_SESSION_ROW_DECODE_FAILED, "session_id": session_id},
            ) from error
        return SessionRecord(
            session_id=str(row.session_id),
            status=str(row.status),
            applied_seq=int(row.applied_seq),
            active_run_id=row.active_run_id,
            config_snapshot=config_snapshot if isinstance(config_snapshot, dict) else None,
            snapshot_hash=row.snapshot_hash,
            diagnostic=diagnostic if isinstance(diagnostic, dict) else None,
        )

    async def messages(
        self,
        session_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> MessagePage:
        """Page the projected messages of one session in seq order.

        ``after_message_id`` is an exclusive cursor (``None`` = from the
        first message); ``limit`` is clamped into 1..1000. A message id that
        does not exist in this session raises InvalidCursor. A missing
        sessions row is NotFound for a never-created session and
        StorageCorrupt (``sessions_row_missing_with_log``) when the event log
        still exists -- a missing projection is never faked as empty history
        (§3.2.1). Message rows are decoded fail-closed; one bad row fails the
        whole page without returning a half page.
        """
        _require_session_id(session_id)
        if after_message_id is not None and (not isinstance(after_message_id, str) or not after_message_id):
            raise ValueError("after_message_id must be None or a non-empty string")
        page_limit = clamp_page_limit(limit)

        record = await self.session(session_id)
        if record is None:
            high = await self._log_high(session_id)
            if high > 0:
                raise _corrupt(
                    REASON_READ_MODEL_MISSING,
                    f"session {session_id} has an event log but no sessions row",
                    {"reason": REASON_READ_MODEL_MISSING, "session_id": session_id},
                )
            raise NotFoundError(f"session {session_id} does not exist", details={"session_id": session_id})
        if record.status == "deleted":
            # T-C1-094: the delete tombstone hides the message face — the
            # same NotFound a never-created session answers. ``'deleted'`` is
            # the pinned sessions.status literal (the scan statements'
            # pinned-vocabulary tradeoff: repos stays session-layer-free).
            raise NotFoundError(f"session {session_id} is deleted", details={"session_id": session_id})

        async with self._engine.connect() as connection:
            after_seq = 0
            if after_message_id is not None:
                cursor_row = (
                    await connection.execute(_SELECT_MESSAGE_SEQ, {"sid": session_id, "mid": after_message_id})
                ).first()
                if cursor_row is None:
                    raise InvalidCursorError(
                        f"message id {after_message_id!r} does not exist in session {session_id!r}",
                        details={"session_id": session_id, "after_message_id": after_message_id},
                    )
                after_seq = int(cursor_row.seq)
            rows = (
                await connection.execute(
                    _SELECT_MESSAGE_PAGE, {"sid": session_id, "after": after_seq, "limit": page_limit + 1}
                )
            ).fetchall()

        items: list[SessionMessage] = []
        for row in rows:
            items.append(self._decode_message_row(session_id, row))
        has_more = len(items) > page_limit
        if has_more:
            items = items[:page_limit]
        next_after = items[-1].id if items else after_message_id
        return MessagePage(items=items, next_after_message_id=next_after, has_more=has_more)

    async def _log_high(self, session_id: str) -> int:
        """H = COALESCE(MAX(events.seq), 0) -- the committed log watermark."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_LOG_HIGH, {"sid": session_id})).first()
        return int(row.h) if row is not None else 0

    async def message_by_id(self, session_id: str, message_id: str) -> SessionMessage | None:
        """One projected message row by ``(session_id, message_id)``; ``None`` when absent.

        The T-C1-094 single-message point query (the ④-3 probe precedent
        shape): a single-row read over the messages unique(session_id,
        message_id) key rather than a page walk. The decode is the page
        face's fail-closed row decoder; the deleted-status gate stays with
        the caller (the service's live-record read precedes this probe).
        """
        _require_session_id(session_id)
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_MESSAGE_BY_ID, {"sid": session_id, "mid": message_id})).first()
        if row is None:
            return None
        return self._decode_message_row(session_id, row)

    def _decode_message_row(self, session_id: str, row: Any) -> SessionMessage:
        """Decode one projected message row, fail closed (§3.2.1 read boundary).

        The §2.1 union dispatches on the stored ``data["type"]`` tag
        (user/assistant/idle); a missing or unknown tag is a corrupt row, not a
        different page shape, and fails the whole page like any decode error.
        """
        try:
            data = _json_column(row.data)
            model = _MESSAGE_MODELS.get(str(data["type"]))
            if model is None:
                raise ValueError(f"unknown projected message type tag {data['type']!r}")
            message = model.model_validate(data)
        except (KeyError, ValueError, TypeError) as error:
            raise _corrupt(
                REASON_MESSAGE_ROW_DECODE_FAILED,
                f"session {session_id} has an undecodable projected message row",
                {
                    "reason": REASON_MESSAGE_ROW_DECODE_FAILED,
                    "session_id": session_id,
                    "message_id": str(row.message_id),
                    "seq": int(row.seq),
                },
            ) from error
        if message.id != str(row.message_id):
            raise _corrupt(
                REASON_MESSAGE_ROW_DECODE_FAILED,
                f"session {session_id} message row id and payload id disagree",
                {
                    "reason": REASON_MESSAGE_ROW_DECODE_FAILED,
                    "session_id": session_id,
                    "message_id": str(row.message_id),
                    "seq": int(row.seq),
                },
            )
        return message

    # -- T-C1-010 (R1-S7): expected_seq + §7 idempotency/run query faces -----------

    async def log_high(self, session_id: str) -> int:
        """H = COALESCE(MAX(events.seq), 0) -- the public §3.2 watermark query.

        The ``expected_seq`` source for the S7 command layer (§3.2: the caller
        reads H inside the session commit lock and passes it as ``expected_seq``).
        Read-only, session-keyed; a never-created session reports H=0.
        """
        _require_session_id(session_id)
        return await self._log_high(session_id)

    async def prompt_by_dedupe_key(self, session_id: str, dedupe_key: str) -> AcceptedPrompt | None:
        """The committed ``py.prompt.accepted`` under ``(session_id, dedupe_key)``.

        §7 step 2 face: the durable idempotency lookup. ``None`` when no event
        row carries the key. The unique(session_id, dedupe_key) index guarantees
        at most one row; a row that is not ``py.prompt.accepted`` v1 under a
        prompt-shaped key, or whose data no longer decodes into
        :class:`~opencode.contracts.PromptAcceptedData`, fails closed as
        StorageCorrupt locating the row (never skipped, never coerced).
        """
        _require_session_id(session_id)
        if not isinstance(dedupe_key, str) or not dedupe_key:
            raise ValueError("dedupe_key must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_EVENT_BY_DEDUPE, {"sid": session_id, "dk": dedupe_key})).first()
        if row is None:
            return None
        if str(row.type) != PY_PROMPT_ACCEPTED or int(row.version) != EVENT_VERSION:
            raise _corrupt(
                REASON_DEDUPE_ROW_UNEXPECTED,
                f"session {session_id} dedupe row {dedupe_key!r} is not {PY_PROMPT_ACCEPTED} v{EVENT_VERSION}",
                {"reason": REASON_DEDUPE_ROW_UNEXPECTED, "session_id": session_id, "event_id": str(row.id)},
            )
        return self._decode_prompt_row(session_id, row)

    async def prompt_by_run_id(self, session_id: str, run_id: str) -> AcceptedPrompt | None:
        """The ``py.prompt.accepted`` data carrying ``run_id`` in this session.

        §7 run_status face: the session's prompt events are scanned in seq
        order, every row decoded fail-closed (the first undecodable row fails
        the query as StorageCorrupt), and the highest-seq match wins (it is
        the event that set the active-run slot). ``None`` when no accepted
        prompt in this session carries ``run_id``.
        """
        _require_session_id(session_id)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_PROMPT_EVENTS, {"sid": session_id})).fetchall()
        match: AcceptedPrompt | None = None
        for row in rows:
            data = self._decode_prompt_row(session_id, row)
            if data.run_id == run_id:
                match = data
        return match

    async def session_by_active_run(self, run_id: str) -> SessionRecord | None:
        """The session whose active-run slot currently holds ``run_id``.

        §7 R1 run_status lookup face (open point C): the sessions table's
        ``active_run_id`` is the only run-tracking surface in R1. ``None``
        when no session holds the run. The full row decode reuses
        :meth:`session` (same fail-closed JSON column semantics).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_SESSION_ID_BY_RUN, {"rid": run_id})).first()
        if row is None:
            return None
        return await self.session(str(row.session_id))

    # -- T-C1-022 (R2-S7 ④-3): the service's consolidated single-row probes ------

    async def run_by_id(self, run_id: str) -> RunRow | None:
        """One runs row by primary key; ``None`` when the row does not exist.

        The S7 run-status owner probe (④-3): raw columns only, ``error``
        passed through as stored -- classifying it is the caller's job (the
        same split the pre-consolidation inline read had).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_RUN_BY_ID, {"rid": run_id})).first()
        if row is None:
            return None
        return RunRow(
            session_id=str(row.session_id),
            request_id=str(row.request_id),
            status=str(row.status),
            error=row.error if row.error is None else str(row.error),
        )

    async def run_status_by_id(self, run_id: str) -> str | None:
        """The stored ``runs.status`` of one run by PK; ``None`` when absent."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_RUN_STATUS_BY_ID, {"rid": run_id})).first()
        return None if row is None else str(row.status)

    async def run_id_by_request(self, session_id: str, request_id: str) -> str | None:
        """The run accepted under ``(session_id, request_id)``; ``None`` when absent.

        T-C1-100 (P7-S1) §3-1 step-2.5 retry probe: the 0002 runs read model
        owns the unique(session_id, request_id) mapping, so a run accepted
        through an inbox promotion or an idle admission -- whose
        ``py.prompt.accepted`` event commits inside a multi-event batch and
        therefore carries no dedupe-key row (unique(session_id, dedupe_key)
        makes a keyed batch single-row) -- is still discoverable for the
        prompt face's idempotent retry recognition.
        """
        _require_session_id(session_id)
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_RUN_ID_BY_REQUEST, {"sid": session_id, "rid": request_id})).first()
        return None if row is None else str(row.run_id)

    async def session_owner(self, session_id: str) -> SessionOwnerRow | None:
        """The ``(session_id, status)`` pair of one sessions row (light probe).

        Deliberately lighter than :meth:`session`: no JSON column decode, so
        an undecodable ``config_snapshot``/``diagnostic`` does not fail this
        probe -- matching the pre-consolidation inline read it replaces.
        """
        _require_session_id(session_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_SESSION_OWNER, {"sid": session_id})).first()
        if row is None:
            return None
        return SessionOwnerRow(session_id=str(row.session_id), status=str(row.status))

    async def session_id_by_active_run(self, run_id: str) -> str | None:
        """The session id whose active-run slot holds ``run_id`` (existence probe).

        Same statement as :meth:`session_by_active_run` without the full row
        decode -- the ④-3 owner-existence probe only needs the id (the
        ``.first()`` existence semantics of the inline read it replaces).
        """
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_SESSION_ID_BY_RUN, {"rid": run_id})).first()
        return None if row is None else str(row.session_id)

    async def session_id_by_message_id(self, message_id: str) -> str | None:
        """The owning session of one projected message row; ``None`` when absent.

        The ④-3 surviving-trace probe: a projected ``msg_idle_{run_id}`` row
        proves the run once existed even when its ``runs`` row is gone.
        """
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_MESSAGE_SESSION_ID, {"mid": message_id})).first()
        return None if row is None else str(row.session_id)

    async def event_by_dedupe_key(self, session_id: str, dedupe_key: str) -> DedupeEventRow | None:
        """The envelope ``(id, type, version)`` of one dedupe-keyed events row.

        The S7 cancel-intent probe (④-3): the envelope is returned
        unclassified -- deciding whether it is the expected
        ``py.run.cancel_requested`` v1 shape stays with the caller, matching
        the pre-consolidation inline read.
        """
        _require_session_id(session_id)
        if not isinstance(dedupe_key, str) or not dedupe_key:
            raise ValueError("dedupe_key must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(_SELECT_EVENT_ENVELOPE_BY_DEDUPE, {"sid": session_id, "dk": dedupe_key})
            ).first()
        if row is None:
            return None
        return DedupeEventRow(id=str(row.id), type=str(row.type), version=int(row.version))

    # -- T-C1-037 (R4-S4): the startup-recovery scan faces -----------------------

    async def all_session_owners(self) -> list[SessionOwnerRow]:
        """Every sessions row's ``(session_id, status)`` pair, ordered by id.

        The recovery coordinator's session enumeration: raw columns only (no
        JSON decode -- the scan classifies statuses itself), deterministic
        order by session_id.
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_ALL_SESSION_OWNERS)).fetchall()
        return [SessionOwnerRow(session_id=str(row.session_id), status=str(row.status)) for row in rows]

    async def non_terminal_runs(self) -> list[ScanRunRow]:
        """Every ``runs`` row still in a non-terminal status (§2.1 L59-61).

        The recovery scan's cross-session run enumeration over the pinned
        accepted/running/cancelling vocabulary (see the statement block). Raw
        columns, ordered by (session_id, run_id); the coordinator re-derives
        the truth from the folded log and reports any table/fold drift
        instead of trusting the projection.
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_NON_TERMINAL_RUNS)).fetchall()
        return [
            ScanRunRow(
                session_id=str(row.session_id),
                run_id=str(row.run_id),
                request_id=str(row.request_id),
                status=str(row.status),
                error=row.error if row.error is None else str(row.error),
            )
            for row in rows
        ]

    async def pending_permissions(self) -> list[PendingPermissionRow]:
        """Every ``permissions`` row still in the ``pending`` state.

        The row#27① orphan face for the recovery scan: ``'pending'`` restates
        ``PermissionState.PENDING`` (session/state.py -- repos stays
        session-layer-free, the same pinned-vocabulary tradeoff as the runs
        statement above). Ordered by (session_id, permission_id).
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_PENDING_PERMISSIONS)).fetchall()
        return [
            PendingPermissionRow(
                session_id=str(row.session_id),
                permission_id=str(row.permission_id),
                execution_id=str(row.execution_id),
            )
            for row in rows
        ]

    # -- T-C1-100 (P7-S1): the steering-inbox read faces ------------------------

    async def inbox_items(self, session_id: str) -> list[InboxItemRow]:
        """Every pending inbox row of one session, in enqueue order.

        The list endpoint's base face: the 0004 table is pending-only
        (delivered/cancelled delete the row in the same commit), so no state
        filter is needed and the ``enqueued_seq`` order IS the TS rowid
        order. Rows decode fail-closed; one bad row fails the whole list
        without returning a half page (the messages-page discipline).
        """
        _require_session_id(session_id)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_INBOX, {"sid": session_id})).fetchall()
        return [self._decode_inbox_row(session_id, row) for row in rows]

    async def inbox_item(self, session_id: str, inbox_id: str) -> InboxItemRow | None:
        """One pending inbox row; ``None`` when no such pending item exists.

        The cancel/update endpoints' pre-check face: a missing row means the
        item was already delivered or cancelled (the endpoint's no-op /
        Conflict decision), not an error.
        """
        _require_session_id(session_id)
        if not isinstance(inbox_id, str) or not inbox_id:
            raise ValueError("inbox_id must be a non-empty string")
        async with self._engine.connect() as connection:
            row = (await connection.execute(_SELECT_INBOX_ITEM, {"sid": session_id, "iid": inbox_id})).first()
        if row is None:
            return None
        return self._decode_inbox_row(session_id, row)

    async def inbox_enqueued_seen(self, session_id: str, inbox_id: str) -> bool:
        """True once ``inbox_id`` was EVER enqueued for the session (T-C3-006).

        The durable replay face: the events log is append-only, so this
        survives delivery/cancellation of the pending row (the 0004 table
        is pending-only and deletes on commit). One JSON decode miss or
        one malformed row is skipped, never an error -- a damaged row must
        not wedge the wake port (the fail-open read-model discipline).
        """
        _require_session_id(session_id)
        if not isinstance(inbox_id, str) or not inbox_id:
            raise ValueError("inbox_id must be a non-empty string")
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_INBOX_ENQUEUED_DATA, {"sid": session_id})).fetchall()
        for row in rows:
            try:
                decoded = json.loads(str(row[0]))
            except ValueError:
                continue
            if isinstance(decoded, dict) and decoded.get("inbox_id") == inbox_id:
                return True
        return False

    async def pending_inbox_session_ids(self) -> list[str]:
        """Every session that still owns pending inbox rows (T-C1-100 R4).

        The post-recovery-scan wake enumeration: pending inbox items are
        plain read-model rows that survive the scan untouched (zero recovery
        writes), so this face is the only re-check the startup wiring needs
        -- each listed session gets the idle-wake promote decision re-run
        against its fold truth.
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_PENDING_INBOX_SESSIONS)).fetchall()
        return [str(row.session_id) for row in rows]

    async def subagent_backgrounded_session_ids(self) -> list[str]:
        """Every session that ever committed a subagent background marker (T-C1-101).

        The startup re-arm enumeration: each listed parent gets its fold
        re-computed (the full-log replay pops every consumed marker at its
        synthetic admission), and one observer re-arms per still-outstanding
        observation -- the same zero-recovery-writes shape as the
        pending-inbox wake scan. A parent whose every marker was consumed
        simply re-derives an empty observation set and arms nothing.
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_SUBAGENT_BACKGROUNDED_SESSIONS)).fetchall()
        return [str(row.session_id) for row in rows]

    def _decode_inbox_row(self, session_id: str, row: Any) -> InboxItemRow:
        """Decode one pending inbox row fail-closed (§3.2.1)."""
        try:
            payload = _json_column(row.message)
        except ValueError as error:
            raise _corrupt(
                REASON_INBOX_ROW_DECODE_FAILED,
                f"session {session_id} has an inbox row with an undecodable message payload",
                {"reason": REASON_INBOX_ROW_DECODE_FAILED, "session_id": session_id},
            ) from error
        if not isinstance(payload, dict):
            raise _corrupt(
                REASON_INBOX_ROW_DECODE_FAILED,
                f"session {session_id} has an inbox row whose message payload is not an object",
                {"reason": REASON_INBOX_ROW_DECODE_FAILED, "session_id": session_id},
            )
        try:
            message = UserMessage.model_validate(payload)
        except ValueError as error:
            raise _corrupt(
                REASON_INBOX_ROW_DECODE_FAILED,
                f"session {session_id} has an inbox row that does not decode as a user message",
                {"reason": REASON_INBOX_ROW_DECODE_FAILED, "session_id": session_id},
            ) from error
        return InboxItemRow(
            session_id=str(row.session_id),
            inbox_id=str(row.inbox_id),
            kind=str(row.kind),
            delivery=str(row.delivery),
            request_id=row.request_id if row.request_id is None else str(row.request_id),
            enqueued_seq=int(row.enqueued_seq),
            message=message,
        )

    # -- T-C1-040 (R4-S6): the CLI session-selection faces -----------------------

    async def session_created_by_id(self, session_id: str) -> SessionCreatedRow | None:
        """The ``py.session.created`` row of one session; ``None`` when absent.

        The ``--session`` selection lookup: raw columns only (the caller routes
        the wire model by version). One session has exactly one creation event
        by construction — more than one row is StorageCorrupt with the named
        sanitized reason, never a "pick one" fiction.
        """
        _require_session_id(session_id)
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_SESSION_CREATED_BY_ID, {"sid": session_id})).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise _corrupt(
                REASON_CREATED_ROW_DUPLICATED,
                f"session {session_id} has {len(rows)} py.session.created rows",
                {"reason": REASON_CREATED_ROW_DUPLICATED, "session_id": session_id, "row_count": len(rows)},
            )
        return self._decode_created_row(session_id, rows[0])

    async def latest_session_created_by_working_dir(self, working_dir: str) -> SessionCreatedRow | None:
        """The most recent ``py.session.created`` row for one working directory.

        The ``--continue`` selection lookup (OP-R4-3 conservative reading):
        the recorded ``data["working_dir"]`` string must be exactly equal — no
        normalization, no prefix, no case folding, no separator folding. Every
        created row is decoded fail-closed in the deterministic
        ``created DESC, session_id DESC`` order (an undecodable row anywhere
        fails the query, the prompt_by_run_id discipline), a session appearing
        twice is StorageCorrupt, and ``None`` means no session was ever
        created in this directory (the caller's fresh-session branch).
        """
        if not isinstance(working_dir, str) or not working_dir:
            raise ValueError("working_dir must be a non-empty string")
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_SESSION_CREATED_ALL)).fetchall()
        decoded: list[SessionCreatedRow] = []
        seen: set[str] = set()
        for row in rows:
            record = self._decode_created_row(str(row.session_id), row)
            if record.session_id in seen:
                raise _corrupt(
                    REASON_CREATED_ROW_DUPLICATED,
                    f"session {record.session_id} has more than one py.session.created row",
                    {"reason": REASON_CREATED_ROW_DUPLICATED, "session_id": record.session_id},
                )
            seen.add(record.session_id)
            decoded.append(record)
        for record in decoded:
            recorded = record.data.get("working_dir")
            if isinstance(recorded, str) and recorded == working_dir:
                return record
        return None

    def _decode_created_row(self, session_id: str, row: Any) -> SessionCreatedRow:
        """Decode one created row's raw columns, fail closed (§3.2.1 read boundary).

        The version pin mirrors the dedupe-row discipline: the registry
        registers 1/2/3 and the commit path rejects anything else, so a stored
        row outside the set is corruption — never coerced through the nearest
        wire model. Details carry the sanitized reason + session id only.
        """
        version = int(row.version)
        if version not in _SESSION_CREATED_VERSIONS:
            raise _corrupt(
                REASON_CREATED_ROW_UNEXPECTED_VERSION,
                f"session {session_id} has a py.session.created row of unregistered version {version}",
                {"reason": REASON_CREATED_ROW_UNEXPECTED_VERSION, "session_id": session_id, "version": version},
            )
        try:
            data = _json_column(row.data)
        except ValueError as error:
            raise _corrupt(
                REASON_CREATED_ROW_DECODE_FAILED,
                f"session {session_id} has an undecodable py.session.created row",
                {"reason": REASON_CREATED_ROW_DECODE_FAILED, "session_id": session_id},
            ) from error
        if not isinstance(data, dict):
            raise _corrupt(
                REASON_CREATED_ROW_DECODE_FAILED,
                f"session {session_id} has a py.session.created row whose data is not a JSON object",
                {"reason": REASON_CREATED_ROW_DECODE_FAILED, "session_id": session_id},
            )
        return SessionCreatedRow(session_id=session_id, version=version, created=int(row.created), data=data)

    # -- T-C1-094 (P6-S1): the session resource read faces -----------------------

    async def session_summaries(self) -> list[SessionListRow]:
        """Every live session, newest-created first (the ``GET /session`` base).

        The base is the events table's ``py.session.created`` enumeration
        (the T-C1-040 selection-query pattern) joined with the sessions row
        for the read-model faces. Fail-closed semantics: a created row whose
        sessions row is missing is read-model corruption (§3.2.1), never
        silently dropped from the list; one session appearing twice in the
        created enumeration is the same duplicated-creation corruption the
        by-id face raises. ``deleted`` sessions are excluded — the
        tombstone's read-model projection filter — while their logs stay in
        place. Minimal diagnostic records (a sessions row without a created
        row) are not in the enumeration by construction and never appear.
        """
        async with self._engine.connect() as connection:
            rows = (await connection.execute(_SELECT_SESSION_CREATED_WITH_RECORD)).fetchall()
        listed: list[SessionListRow] = []
        seen: set[str] = set()
        for row in rows:
            session_id = str(row.session_id)
            if session_id in seen:
                raise _corrupt(
                    REASON_CREATED_ROW_DUPLICATED,
                    f"session {session_id} has more than one py.session.created row",
                    {"reason": REASON_CREATED_ROW_DUPLICATED, "session_id": session_id},
                )
            seen.add(session_id)
            if row.status is None:
                raise _corrupt(
                    REASON_READ_MODEL_MISSING,
                    f"session {session_id} has a py.session.created row but no sessions row",
                    {"reason": REASON_READ_MODEL_MISSING, "session_id": session_id},
                )
            if str(row.status) == "deleted":
                continue  # the tombstone: present in the log, absent from the list
            listed.append(
                SessionListRow(
                    session_id=session_id,
                    status=str(row.status),
                    applied_seq=int(row.applied_seq),
                    active_run_id=row.active_run_id,
                    created_epoch_ms=int(row.created),
                )
            )
        return listed

    def _decode_prompt_row(self, session_id: str, row: Any) -> AcceptedPrompt:
        """Decode one events row into AcceptedPrompt, fail closed (§3.2.1 read boundary).

        A version the R1 registry does not know (anything but v1) is stored
        corruption on this read path -- never skipped (T-A-002 fail-closed
        ruling), never coerced through the v1 data model.
        """
        if str(row.type) != PY_PROMPT_ACCEPTED or int(row.version) != EVENT_VERSION:
            raise _corrupt(
                REASON_STORED_DECODE_FAILED,
                f"session {session_id} has a non-v{EVENT_VERSION} {PY_PROMPT_ACCEPTED} row",
                {
                    "reason": REASON_STORED_DECODE_FAILED,
                    "session_id": session_id,
                    "first_bad_seq": int(row.seq),
                    "first_bad_event_id": str(row.id),
                },
            )
        try:
            data = PromptAcceptedData.model_validate(_json_column(row.data))
        except (ValueError, TypeError) as error:
            raise _corrupt(
                REASON_STORED_DECODE_FAILED,
                f"session {session_id} has an undecodable py.prompt.accepted row",
                {
                    "reason": REASON_STORED_DECODE_FAILED,
                    "session_id": session_id,
                    "first_bad_seq": int(row.seq),
                    "first_bad_event_id": str(row.id),
                },
            ) from error
        return AcceptedPrompt(request_id=data.request_id, run_id=data.run_id, message_text=data.message.text)


def _require_session_id(session_id: str) -> None:
    """State-independent caller-input check (rejected before any DB access)."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
