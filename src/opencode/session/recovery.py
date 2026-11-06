"""Startup recovery coordinator (R4-S4; CORE-CONTRACTS §3.3 L210-221, §6.1 L302, §7 L333).

The §3.3 L212 recovery recipe, consumed at composition-root startup: scan the
durable state, verify integrity (``sessions.applied_seq == log high``), fold
the full committed log, and close every still-open run of the previous (crashed
or killed) process with the interrupted terminal event sequence -- closed-first
orphan permission closes, then the synthesized tool terminals, then the
interrupted ``py.step.finished``, then ``py.run.finished(interrupted)`` -- as
one atomic batch per run through the shared :class:`EventCommitter` (one commit
seam, stable event ids, the store's bounded whole-batch ConflictError retry).
Recovery NEVER resumes or replays a run (§7 L333 绝不自动续跑): the durable log is
the only input, the terminals are the only output.

The three §6.1 L302 stop faces are consumed directly: a vanished recorded
working directory halts that session's recovery, and a v3 snapshot whose
recorded MCP fingerprints drifted from the current host refs halts it too
(:func:`opencode.config.loader.mcp_fingerprint_drift` -- one formula with
``ConfigLoader.recover``). The credentials-reference stop has no producer in
any snapshot version (protocol-unreachable, ④ disclosure in the progress
file). A v1 snapshot has no runtime face, but terminal hygiene is not runtime
resolution: §7 L333's "R1 遗留 accepted" wording keeps v1 sessions recoverable
here -- the v1-no-runtime-face stop belongs to ``ConfigLoader.recover`` alone.

Write reach is deliberately narrow (§7 L312-313): only ``active`` sessions get
recovery writes; ``read_only_diagnostic``/``closed``/``cleanup_failed`` sessions
are scanned and counted but never written (the txn write gate already answers
them, §7 L339 keeps the ``cleanup_failed`` record). Any integrity failure
(A != H, undecodable log, illegal fold) skips that session fail-closed and is
reported -- the coordinator never repairs read models and never fabricates a
parser for a face no snapshot version produces.

The composition-root integration is the :class:`RecoveryGate` (OP-R4-6
conservative default): ``App`` binds one scan coroutine; the gated store faces
(``commit``/``rebuild``/``cleanup_failed``) await the gate first, so the
composition root is unavailable for writes until recovery completes -- the
structural implementation of §3.3 L212's exclusive run ownership. Reads stay
ungated (§3.2.1: queries never trigger recovery) and the coordinator itself
uses the ungated inner store. A scan failure propagates to every gated caller
(fail-closed).

Row #27③ lives here as the host-operational sweep
(:func:`sweep_crash_temp_files`): ``.opencode-fs-*.tmp`` crash leftovers of the
write tool's atomic replace (T-C1-028 ④-1) are removed from the declared roots
at App startup -- the composition-root path, not the session service layer
(r3 ruling).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.bus.eventlog import MAX_READ_LIMIT, EventCursor, EventLog
from opencode.config.loader import mcp_fingerprint_drift
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_PERMISSION_CLOSED,
    PY_RUN_FINISHED,
    PY_STEP_FINISHED,
    PY_STEP_STARTED,
    PY_TOOL_FINISHED,
    ConfigSnapshotV3,
    EventDraft,
    McpServerRef,
    PermissionClosedData,
    RunFinishedData,
    StepFinishedData,
    StepStartedData,
    ToolFinishedData,
)
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantPart,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    ToolTextContent,
)
from opencode.session.runner import EventCommitter
from opencode.session.service import AscendingIds, Clock, IdFactory
from opencode.session.state import (
    PermissionState,
    SessionState,
    StepInfo,
    ToolInfo,
    ToolState,
)
from opencode.session.state import (
    apply as apply_event,
)
from opencode.storage.repos import PendingPermissionRow, ReadRepositories, ScanRunRow
from opencode.storage.txn import StoreError

#: The §2.1 wire status of an active session (sessions.status), pinned to
#: ``SessionStatus.ACTIVE`` -- restated as a plain literal for the scan's
#: status classification (the coordinator imports the enum; this constant is
#: the string the DB stores).
_STATUS_ACTIVE = "active"

#: Row #27①: the reason text of a recovery orphan close. ``cancelled`` (not
#: ``restart``) is the row#27① explicit prescription paired with the §2.1 L53
#: not-started tool terminal in the same batch; ``restart`` stays a zero-producer
#: recognized corner (r3-slices row #27② ruling, ④ disclosure).
REASON_ORPHAN_PERMISSION_CLOSED = "startup recovery closed a still-pending approval after an unclean shutdown"

#: The write-tool temp pattern (write.py ``tempfile.mkstemp`` prefix/suffix,
#: T-C1-028 ④-1 -- read-only anchor, never modified here).
TMP_FILE_PREFIX = ".opencode-fs-"
TMP_FILE_SUFFIX = ".tmp"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SessionRecoveryReport:
    """What the scan did (or refused to do) for one session.

    ``outcome`` is one of ``recovered`` (>= 1 terminal batch committed),
    ``clean`` (folded, nothing to write), ``halted`` (a §6.1 L302 stop face),
    ``skipped`` (outside the recovery write gate: non-active status or a
    missing sessions row), ``integrity_failure`` (A != H or the fold failed;
    no writes). ``notes`` carries the honest leftovers: projection drift and
    legally uncloseable orphans.
    """

    session_id: str
    outcome: str
    reason: str | None = None
    closed_runs: tuple[tuple[str, str], ...] = ()
    closed_permissions: tuple[str, ...] = ()
    tool_terminals: tuple[str, ...] = ()
    step_terminals: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The full startup-scan result (host-facing, honest counters only)."""

    scanned_sessions: int = 0
    active_sessions: int = 0
    read_only_diagnostic_sessions: int = 0
    closed_sessions: int = 0
    cleanup_failed_sessions: int = 0
    other_status_sessions: int = 0
    sessions: tuple[SessionRecoveryReport, ...] = ()
    tmp_files_removed: int = 0
    tmp_roots: tuple[str, ...] = ()
    tmp_errors: tuple[str, ...] = ()


def sweep_crash_temp_files(roots: Sequence[str | Path]) -> tuple[int, tuple[str, ...]]:
    """Row #27③: remove ``.opencode-fs-*.tmp`` crash leftovers under ``roots``.

    The write tool's atomic replace (mkstemp O_EXCL -> fsync -> os.replace)
    leaves a ``.opencode-fs-*.tmp`` sibling only when the process died between
    the temp write and the swap (T-C1-028 ④-1: cross-process kill has no
    injection seam, so this is host-operational startup hygiene, not a
    session-layer feature); the target file itself is intact, so removing the
    leftover is always safe. A vanished root and an individual unlink failure
    are reported, never raised.
    """
    removed = 0
    errors: list[str] = []
    for root in roots:
        path = Path(root)
        try:
            candidates = sorted(path.glob(f"{TMP_FILE_PREFIX}*{TMP_FILE_SUFFIX}"))
        except OSError as error:
            errors.append(f"{root}: {error}")
            continue
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                candidate.unlink()
                removed += 1
            except OSError as error:
                errors.append(f"{candidate}: {error}")
    return removed, tuple(errors)


class RecoveryGate:
    """The composition-root write gate over one bound startup scan (OP-R4-6).

    ``bind`` attaches the scan callable and schedules it immediately when a
    loop is already running (the ``async with App(...)`` composition); a
    constructor outside any loop defers the scheduling to the first
    :meth:`wait` (there is always a loop there -- every gated face is async).
    ``wait`` is idempotent, returns the scan's report, and re-raises the
    scan's failure to every caller (fail-closed: a failed startup scan keeps
    the composition root's write faces unavailable instead of silently
    proceeding).
    """

    def __init__(self) -> None:
        self._scan: Callable[[], Coroutine[Any, Any, RecoveryReport]] | None = None
        self._task: asyncio.Task[RecoveryReport] | None = None

    def bind(self, scan: Callable[[], Coroutine[Any, Any, RecoveryReport]]) -> None:
        """Attach the scan callable; schedules eagerly when a loop is running."""
        if self._scan is not None:
            raise RuntimeError("the startup recovery scan is already bound")
        self._scan = scan
        self._try_schedule()

    def _try_schedule(self) -> None:
        """Schedule the bound scan once, if possible (idempotent, best effort)."""
        if self._task is not None or self._scan is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._scan(), name="opencode-startup-recovery")

    async def wait(self) -> RecoveryReport:
        """Await the bound scan; starts it if it was not started yet."""
        if self._scan is None:
            raise RuntimeError("no startup recovery scan is bound to this gate")
        self._try_schedule()
        task = self._task
        assert task is not None  # wait() runs inside a loop, so _try_schedule started it
        return await task


class RecoveryCoordinator:
    """The §3.3 L212 startup scan: durable-log-based terminal closure, never a resume.

    Inputs are the async storage engine, the shared :class:`EventCommitter`
    (same commit seam as the runner: one writer, per-session lock, bounded
    whole-batch ConflictError retry with stable event ids) and the optional
    current host MCP refs. When ``current_mcp_servers`` is ``None`` the host
    declares no MCP face and the v3 fingerprint comparison is skipped (the
    CLI composition currently declares none, ④ disclosure); when given, any
    drift halts that session's recovery (§6.1 L302).
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        committer: EventCommitter,
        current_mcp_servers: Sequence[McpServerRef] | None = None,
        repos: ReadRepositories | None = None,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._committer = committer
        self._current_mcp_servers = tuple(current_mcp_servers) if current_mcp_servers is not None else None
        self._repos = repos if repos is not None else ReadRepositories(engine)
        self._log = EventLog(self._engine, repos=self._repos)
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)

    async def scan(self) -> RecoveryReport:
        """One full startup pass: shortlist, integrity-check, close, report.

        The scan shortlists sessions with non-terminal runs or still-pending
        permissions (the only two write targets of §3.3 terminal hygiene),
        classifies every sessions row for the honest counters, then recovers
        each shortlisted ACTIVE session. Non-shortlisted sessions are never
        folded (a healthy idle store costs three cheap queries, ④ disclosure).
        """
        owners = await self._repos.all_session_owners()
        run_rows = await self._repos.non_terminal_runs()
        pending_rows = await self._repos.pending_permissions()

        active = read_only = closed_count = cleanup_failed = other = 0
        shortlist: dict[str, None] = {}
        for run_row in run_rows:
            shortlist.setdefault(run_row.session_id, None)
        for pending_row in pending_rows:
            shortlist.setdefault(pending_row.session_id, None)
        for owner in owners:
            if owner.status == _STATUS_ACTIVE:
                active += 1
            elif owner.status == "read_only_diagnostic":
                read_only += 1
            elif owner.status == "closed":
                closed_count += 1
            elif owner.status == "cleanup_failed":
                cleanup_failed += 1
            else:
                other += 1

        session_reports: list[SessionRecoveryReport] = []
        for session_id in sorted(shortlist):
            session_reports.append(await self._recover_session(session_id, run_rows, pending_rows))
        return RecoveryReport(
            scanned_sessions=len(owners),
            active_sessions=active,
            read_only_diagnostic_sessions=read_only,
            closed_sessions=closed_count,
            cleanup_failed_sessions=cleanup_failed,
            other_status_sessions=other,
            sessions=tuple(session_reports),
        )

    # -- per-session recovery -----------------------------------------------------

    async def _recover_session(
        self,
        session_id: str,
        run_rows: Sequence[ScanRunRow],
        pending_rows: Sequence[PendingPermissionRow],
    ) -> SessionRecoveryReport:
        """Integrity gate + L302 gates + one terminal batch per open run."""
        record = await self._repos.session(session_id)
        if record is None:
            return SessionRecoveryReport(
                session_id=session_id,
                outcome="skipped",
                reason="sessions row missing while the read model holds the run; the store owns that corruption",
            )
        if record.status != _STATUS_ACTIVE:
            # The txn write gate answers every non-active status; the
            # cleanup_failed record is kept verbatim (§7 L339 last sentence).
            return SessionRecoveryReport(
                session_id=session_id,
                outcome="skipped",
                reason=(
                    f"session status {record.status!r} is outside the recovery write gate; scanned and counted only"
                ),
            )
        high = await self._repos.log_high(session_id)
        if record.applied_seq != high:
            return SessionRecoveryReport(
                session_id=session_id,
                outcome="integrity_failure",
                reason=f"applied_seq {record.applied_seq} != log high watermark {high} (A != H)",
            )
        try:
            state, step_starts = await self._fold(session_id)
        except (StoreError, ValueError) as error:
            return SessionRecoveryReport(
                session_id=session_id,
                outcome="integrity_failure",
                reason=f"the committed log failed the full fold: {error}",
            )

        # §6.1 L302 stop faces, consumed directly (never a ConfigLoader call:
        # terminal hygiene needs no runtime face, and the v1 stop there would
        # permanently block v1 session recovery, §7 L333 "R1 遗留 accepted").
        working_dir = state.working_dir
        if working_dir is None or not await asyncio.to_thread(os.path.isdir, working_dir):
            return SessionRecoveryReport(
                session_id=session_id,
                outcome="halted",
                reason=f"the recorded working directory {working_dir!r} no longer exists (§6.1 L302)",
            )
        if isinstance(state.config_snapshot, ConfigSnapshotV3) and self._current_mcp_servers is not None:
            drift = mcp_fingerprint_drift(state.config_snapshot.mcp_servers, self._current_mcp_servers)
            if drift:
                return SessionRecoveryReport(
                    session_id=session_id,
                    outcome="halted",
                    reason=(
                        f"recorded MCP fingerprints drifted from the current host refs: {'; '.join(drift)} (§6.1 L302)"
                    ),
                )

        notes: list[str] = []
        table_runs = {row.run_id: row for row in run_rows if row.session_id == session_id}
        open_runs = {run_id: info for run_id, info in state.runs.items() if not info.status.is_terminal}
        for run_id, row in table_runs.items():
            if run_id not in open_runs:
                notes.append(f"runs table says {run_id} is {row.status!r} but the fold says it is terminal")
        table_pending = {row.permission_id for row in pending_rows if row.session_id == session_id}
        folded_pending = {
            permission.permission_id
            for permission in state.permissions.values()
            if permission.state is PermissionState.PENDING
        }
        for permission_id in sorted(table_pending - folded_pending):
            notes.append(f"permissions table holds pending {permission_id} but the fold does not")

        closed_runs: list[tuple[str, str]] = []
        closed_permissions: list[str] = []
        tool_terminals: list[str] = []
        step_terminals: list[str] = []

        # Orphan pendings and open tools whose run is already terminal are
        # legally uncloseable (the fold rejects every terminal-after-run-end
        # event) -- reported, never written.
        for permission in sorted(state.permissions.values(), key=lambda item: item.permission_id):
            if permission.state is not PermissionState.PENDING:
                continue
            run = state.runs.get(permission.run_id)
            if run is None or run.status.is_terminal:
                notes.append(
                    f"pending permission {permission.permission_id} sits on a terminal run; no legal close exists"
                )
        for tool in sorted(state.tools.values(), key=lambda item: item.execution_id):
            if tool.state.is_terminal:
                continue
            run = state.runs.get(tool.run_id)
            if run is None or run.status.is_terminal:
                notes.append(f"open tool {tool.execution_id} sits on a terminal run; no legal terminal exists")

        for run_id in sorted(open_runs):
            run_info = open_runs[run_id]
            drafts: list[EventDraft[Any]] = []
            # Closed-first orphan permission closes (§2.1 L53: the permission
            # close and the not-started tool terminal commit in one batch,
            # leaving no permanent pending).
            for permission in sorted(state.permissions.values(), key=lambda item: item.permission_id):
                if permission.state is PermissionState.PENDING and permission.run_id == run_id:
                    drafts.append(
                        self._draft(
                            session_id,
                            run_id,
                            PY_PERMISSION_CLOSED,
                            PermissionClosedData(
                                permission_id=permission.permission_id,
                                outcome="cancelled",
                                reason=REASON_ORPHAN_PERMISSION_CLOSED,
                            ),
                        )
                    )
                    closed_permissions.append(permission.permission_id)
            # Synthesized tool terminals (§3.3 L210): pending -> cancelled/
            # not_started, started-without-result -> the honest unknown.
            synthesized: dict[str, ToolFinishedData] = {}
            for tool in sorted(state.tools.values(), key=lambda item: item.execution_id):
                if tool.run_id != run_id or tool.state.is_terminal:
                    continue
                terminal = self._tool_terminal(tool)
                synthesized[tool.execution_id] = terminal
                drafts.append(self._draft(session_id, run_id, PY_TOOL_FINISHED, terminal))
                tool_terminals.append(tool.execution_id)
            # The interrupted step terminal, rebuilt from the folded truth
            # (§3.3 L212: the generated branch keeps the committed text and
            # tool facts; the non-generated branch carries exactly the
            # committed finalized text).
            step = next(
                (info for info in state.steps.values() if info.run_id == run_id and not info.status.is_terminal),
                None,
            )
            if step is not None:
                started = step_starts.get(step.step_id)
                if started is None:
                    return SessionRecoveryReport(
                        session_id=session_id,
                        outcome="integrity_failure",
                        reason=f"open step {step.step_id} has no py.step.started in the committed log",
                        closed_runs=tuple(closed_runs),
                        closed_permissions=tuple(closed_permissions),
                        tool_terminals=tuple(tool_terminals),
                        step_terminals=tuple(step_terminals),
                        notes=tuple(notes),
                    )
                message = self._interrupted_message(state, step, started, synthesized)
                drafts.append(
                    self._draft(
                        session_id, run_id, PY_STEP_FINISHED, StepFinishedData(step_id=step.step_id, message=message)
                    )
                )
                step_terminals.append(step.step_id)
            # The run terminal: accepted/running/cancelling -> interrupted
            # (§2.1 L59-61 legality; error stays the hard-cancel-conservative
            # None, the runner's own conservative path).
            drafts.append(
                self._draft(
                    session_id,
                    run_id,
                    PY_RUN_FINISHED,
                    RunFinishedData(run_id=run_id, outcome="interrupted", error=None),
                )
            )
            await self._committer.commit(session_id, drafts)
            closed_runs.append((run_id, run_info.status.value))

        outcome = "recovered" if closed_runs else "clean"
        return SessionRecoveryReport(
            session_id=session_id,
            outcome=outcome,
            closed_runs=tuple(closed_runs),
            closed_permissions=tuple(closed_permissions),
            tool_terminals=tuple(tool_terminals),
            step_terminals=tuple(step_terminals),
            notes=tuple(notes),
        )

    # -- derivation helpers -------------------------------------------------------

    async def _fold(self, session_id: str) -> tuple[SessionState, dict[str, tuple[ModelRef, datetime]]]:
        """Fold the full committed log through the §3.3 fold (fail-closed).

        Returns the folded state plus the open steps' ``(model, started_at)``
        side map collected from ``py.step.started`` -- the runner keeps that
        pair in its single-flight bookkeeping, which a crashed process never
        had durably; the log is the only source (④ disclosure).
        """
        state = SessionState()
        step_starts: dict[str, tuple[ModelRef, datetime]] = {}
        after: EventCursor | None = None
        while True:
            page = await self._log.read(session_id, after, MAX_READ_LIMIT)
            for event in page.items:
                if event.type == PY_STEP_STARTED and isinstance(event.data, StepStartedData):
                    step_starts[event.data.step_id] = (event.data.model, event.created_at)
                state = apply_event(state, event)
            if not page.has_more:
                return state, step_starts
            after = page.next_cursor

    def _tool_terminal(self, tool: ToolInfo) -> ToolFinishedData:
        """The synthesized terminal of an open tool (§3.3 L210, runner parity)."""
        if tool.state is ToolState.PENDING:
            return ToolFinishedData(
                execution_id=tool.execution_id,
                state="cancelled",
                effect_status="not_started",
                content=[
                    ToolTextContent(
                        type="text", text=f"Tool execution {tool.execution_id} was cancelled before it started."
                    )
                ],
            )
        return ToolFinishedData(
            execution_id=tool.execution_id,
            state="unknown",
            effect_status="unknown",
            content=[
                ToolTextContent(
                    type="text",
                    text=(
                        f"Tool execution {tool.execution_id} was cancelled while running; "
                        "its result could not be confirmed."
                    ),
                )
            ],
        )

    def _interrupted_message(
        self,
        state: SessionState,
        step: StepInfo,
        started: tuple[ModelRef, datetime],
        synthesized: dict[str, ToolFinishedData],
    ) -> AssistantMessage:
        """The interrupted terminal message, rebuilt from the folded truth.

        Mirrors the runner's ``_interrupted_message`` over the folded log
        instead of the in-memory state: the generated branch keeps the
        generated message's identity/model/created_at/metadata and applies
        this batch's synthesized tool terminals (or the already-committed
        folded facts); the non-generated branch carries exactly the committed
        finalized text (§3.3 L212). finish/usage/cost/error stay the
        hard-cancel-conservative ``None``: the consumed stream object is gone
        with the crashed process.
        """
        model, started_at = started
        if step.generated:
            existing = next(
                (
                    message
                    for message in state.messages
                    if message.id == step.message_id and isinstance(message, AssistantMessage)
                ),
                None,
            )
            if existing is None:
                raise ValueError(f"step {step.step_id} has no generated message in the folded sequence")
            parts: list[AssistantPart] = []
            for part in existing.parts:
                if not isinstance(part, AssistantToolPart):
                    parts.append(part)
                    continue
                terminal = synthesized.get(part.execution_id)
                if terminal is not None:
                    parts.append(
                        part.model_copy(
                            update={
                                "state": terminal.state,
                                "effect_status": terminal.effect_status,
                                "content": list(terminal.content),
                                "structured_output": terminal.structured_output,
                                "error": terminal.error,
                            }
                        )
                    )
                    continue
                tool = state.tools[part.execution_id]
                parts.append(
                    part.model_copy(
                        update={
                            "state": tool.state.value,
                            "effect_status": tool.effect_status.value,
                            "content": list(tool.content),
                            "structured_output": tool.structured_output,
                            "error": tool.error,
                        }
                    )
                )
            return AssistantMessage(
                id=existing.id,
                type="assistant",
                created_at=existing.created_at,
                metadata=existing.metadata,
                model=existing.model,
                parts=parts,
                status="interrupted",
                finish=None,
                raw_finish=None,
                usage=None,
                cost_usd=None,
                error=None,
                completed_at=self._clock(),
            )
        parts = []
        finalized = sorted(
            (committed for (message_id, _), committed in state.finalized.items() if message_id == step.message_id),
            key=lambda committed: committed.ordinal,
        )
        for committed in finalized:
            if committed.kind == "text":
                parts.append(AssistantTextPart(type="text", part_id=committed.part_id, text=committed.text))
            else:
                parts.append(AssistantReasoningPart(type="reasoning", part_id=committed.part_id, text=committed.text))
        return AssistantMessage(
            id=step.message_id,
            type="assistant",
            created_at=started_at,
            metadata={},
            model=model,
            parts=parts,
            status="interrupted",
            finish=None,
            raw_finish=None,
            usage=None,
            cost_usd=None,
            error=None,
            completed_at=self._clock(),
        )

    def _draft[DataT: BaseModel](self, session_id: str, run_id: str, event_type: str, data: DataT) -> EventDraft[DataT]:
        """One run-scoped recovery draft: fresh ``evt_`` id, clock stamp, run metadata."""
        return EventDraft[DataT](
            id=self._ids.event_id(),
            type=event_type,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=data,
            metadata={"run_id": run_id},
        )


__all__ = [
    "REASON_ORPHAN_PERMISSION_CLOSED",
    "RecoveryCoordinator",
    "RecoveryGate",
    "RecoveryReport",
    "SessionRecoveryReport",
    "sweep_crash_temp_files",
]
