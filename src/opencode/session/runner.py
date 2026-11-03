"""R2-S6: the session runner generation loop (CORE-CONTRACTS §2.3, §3.1, §7).

One runner coroutine drives one accepted run from ``py.run.started`` to
``py.run.finished``:

* step loop -- per step: pin the catalog snapshot (§5.1 L253), build the
  ``GenerationRequest`` through the frozen ``ContextBuilder`` (a
  ``ContextLimitExceeded`` fails the run with no step footprint, §7 L339),
  commit ``py.step.started`` (``attempt`` is always 1 in R2 -- OP-6: the retry
  owner is the adapter only; a user retry is a new run), consume
  ``provider.stream`` aggregating text/reasoning deltas per ``part_id`` and
  tool-call fragments per ``call_index`` (§2.3 L100) while publishing live
  ephemeral deltas on the bus (§4 L226) and enforcing the three generation
  caps inline (§5.3 L276: text stream 1 MiB / arguments per call 64 KiB / at
  most 16 calls) -- over-limit, duplicate tool ids, incomplete or non-object
  JSON fail the whole generation before any tool executes;
* generation batch -- all ``py.text.finalized`` / ``py.reasoning.finalized``
  events by ordinal, then exactly one ``py.step.generated`` (the complete
  ``AssistantMessage`` call plan, tools initially pending/not_started), one
  atomic ``EventBatch`` (§3.1 L65/L144);
* tool loop -- strictly after the whole plan is durable (§2.3 L100): serial
  ``ToolRegistry.invoke`` per call in call-start order (PHASE1-SLIM L85);
* terminals -- ``py.step.finished`` carries the self-contained terminal
  message (finish/usage/cost only on the terminal message), ``py.run.finished``
  folds the outcome and releases the active-run slot (the idle message and
  the slot release are fold-side projections).

Interrupt faces (验收 C): an error chunk or EOF without a terminal chunk ends
the step/run interrupted with the fully-received committed text (不拼接不重发,
§8 L350); cancellation is cooperative -- ``py.run.cancel_requested`` lands
first, the signal in :meth:`Runner.cancel_event` follows; pending tools get a
synthesized ``cancelled/not_started`` terminal, started tools keep their
committed terminals, and no new step/tool starts (§7 L335). A hard
``CancelledError`` (adapter propagation, §2.2 L84) is finalized the same way
when the durable state says cancelling, then re-raised.

TS grounding (``packages/core/src/session/runner/``): the Effect-services
graph collapses into one asyncio coroutine state machine here;
``runner/step.ts`` Outcome -> step terminal + needs-continuation (any finish
reason completes the step; tool calls present mean another step);
``max-steps.ts`` MAX_STEPS_PROMPT is reference semantics only (the run
fails, §7 L341); ``retry.ts`` is deliberately not ported (§8 L350).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from opencode.ai.provider import Provider
from opencode.bus.bus import VolatileBus
from opencode.bus.eventlog import EventCursor, EventLog
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_REASONING_DELTA,
    PY_REASONING_FINALIZED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_STEP_FINISHED,
    PY_STEP_GENERATED,
    PY_STEP_STARTED,
    PY_TEXT_DELTA,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_INPUT_DELTA,
    PY_TOOL_PROGRESS,
    DurableEvent,
    EphemeralEvent,
    EventDraft,
    ReasoningDeltaData,
    ReasoningFinalizedData,
    RunFinishedData,
    RunStartedData,
    StepFinishedData,
    StepGeneratedData,
    StepStartedData,
    TextDeltaData,
    TextFinalizedData,
    ToolFinishedData,
    ToolInputDeltaData,
    ToolProgressData,
    canonical_json,
)
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    GenerationRequest,
    ModelRef,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    UsageChunk,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantPart,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    JsonObject,
    ToolContent,
    ToolTextContent,
)
from opencode.session.context import ContextBuilder, ContextLimitExceeded
from opencode.session.service import AscendingIds, Clock, IdFactory
from opencode.session.state import (
    RunStatus,
    SessionState,
    StepInfo,
    ToolState,
)
from opencode.session.state import (
    apply as apply_event,
)
from opencode.storage.repos import ReadRepositories
from opencode.storage.store import EventBatch, SessionStore
from opencode.storage.txn import ConflictError, StoreError
from opencode.tools.invoker import ToolContext, ToolLimits
from opencode.tools.registry import ToolRegistry

#: §7 L341: the default step ceiling (r2-slices §2 R2-S6).
DEFAULT_MAX_STEPS = 20

#: §3.2: bounded ConflictError retries per commit (same drafts + batch id).
_COMMIT_ATTEMPTS = 3

RunOutcome = Literal["succeeded", "failed", "interrupted"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventCommitter:
    """§3.2 L159 step-5 composition owner: commit, then notify.

    Every durable event of a running session -- the runner's run/step/
    generation batches and the Invoker's single-event tool phases via
    :meth:`sink` -- flows through :meth:`commit`: read the log high watermark
    ``H`` under a per-session lock, commit one :class:`EventBatch` through the
    unique :meth:`SessionStore.commit`, then notify the :class:`VolatileBus`
    with the returned durable events in seq order. A stale ``expected_seq``
    (another writer committed first) is retried with the same drafts and batch
    id; the whole-batch retry is idempotent (§3.2). ``sink`` is the Invoker's
    :term:`EventSink` face over the same path, so tool events reach the same
    store and bus as run events (one composition seam, no second writer).
    """

    def __init__(
        self,
        repos: ReadRepositories,
        store: SessionStore,
        bus: VolatileBus,
        *,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repos = repos
        self._store = store
        self._bus = bus
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def commit(self, session_id: str, drafts: Sequence[EventDraft[Any]]) -> list[DurableEvent[Any]]:
        """Commit one atomic batch and notify the bus with the result."""
        if not drafts:
            raise ValueError("commit requires at least one event draft")
        for draft in drafts:
            if draft.session_id != session_id:
                raise ValueError(f"draft {draft.id} belongs to session {draft.session_id!r}, not {session_id!r}")
        async with self._lock(session_id):
            batch = EventBatch(self._ids.batch_id(), tuple(drafts))
            conflict: ConflictError | None = None
            for _ in range(_COMMIT_ATTEMPTS):
                high = await self._repos.log_high(session_id)
                try:
                    committed = await self._store.commit(session_id, batch, high)
                except ConflictError as error:
                    conflict = error
                    continue
                self._bus.notify(session_id, committed)
                return committed
            assert conflict is not None
            raise conflict

    async def sink(self, drafts: Sequence[EventDraft[Any]]) -> None:
        """The Invoker EventSink face: one batch per phase (§5.1 L257)."""
        if not drafts:
            return
        await self.commit(drafts[0].session_id, list(drafts))


@dataclass(frozen=True, slots=True)
class RunResult:
    """The terminal face of one ``Runner.run`` execution."""

    outcome: RunOutcome
    steps: int = 0
    error: CoreError | None = None


@dataclass(slots=True)
class _TextSlot:
    """One text/reasoning part aggregated from deltas (§2.3 L92)."""

    kind: Literal["text", "reasoning"]
    part_id: str
    deltas: list[str] = field(default_factory=list)
    byte_size: int = 0

    @property
    def text(self) -> str:
        return "".join(self.deltas)


@dataclass(slots=True)
class _CallSlot:
    """One tool call aggregated by ``call_index`` (§2.3 L100)."""

    call_index: int
    tool_call_id: str | None = None
    name: str | None = None
    args_bytes: int = 0
    end: ToolCallEndChunk | None = None


@dataclass(slots=True)
class _StreamResult:
    """What one consumed provider stream left behind."""

    finish: FinishChunk | None = None
    usage: UsageChunk | None = None
    error: CoreError | None = None
    interrupted: bool = False
    slots: list[_TextSlot | _CallSlot] = field(default_factory=list)


class _GenerationViolation(Exception):
    """An inline §5.3 L276 cap or a malformed call plan (§2.3 L100).

    The generation fails and no tool of the plan executes; the plan is never
    guessed into shape.
    """

    def __init__(self, violation: str, message: str) -> None:
        super().__init__(message)
        self.error = CoreError(
            code=CoreErrorCode.VALIDATION_ERROR,
            message=message,
            retryable=False,
            details={"violation": violation},
        )


def _cancelled_content(execution_id: str) -> list[ToolContent]:
    """The single model-readable text of a synthesized cancel terminal (§2.1 L72)."""
    return [ToolTextContent(type="text", text=f"Tool execution {execution_id} was cancelled before it started.")]


# ---------------------------------------------------------------------------
# The R2-S6 runner
# ---------------------------------------------------------------------------


def _wire_tool_name(dotted: str) -> str:
    """§2.2 seam: dotted ``namespace.name`` -> the registry wire ``namespace__name``.

    Provider chunks carry the dotted form (the MAAS adapter reverse-parses
    ``fs__read`` into ``fs.read``, provider.md §6-2), while
    :meth:`ToolRegistry.invoke` resolves the raw requested name as the wire
    form. Mirrors ``opencode.ai.maas.tool_wire_name_from_dotted`` inline: the
    runner is provider-agnostic and must not import the concrete adapter
    (④ float report).
    """
    namespace, separator, name = dotted.partition(".")
    if separator and namespace and name:
        return f"{namespace}__{name}"
    return dotted


class Runner:
    """The R2-S6 generation loop: one accepted run from started to finished.

    TS grounding (``packages/core/src/session/runner/``): the Effect-services
    graph collapses into this single coroutine state machine. Per step: pin
    the catalog snapshot (§5.1 L253), build the request through the frozen
    ``ContextBuilder``, commit ``py.step.started`` (``attempt`` is always 1:
    OP-6 -- the retry owner is the adapter only, §8 L350), consume
    ``provider.stream`` (aggregate + publish ephemerals + enforce the three
    §5.3 L276 caps inline), commit the generation batch atomically, execute
    the durable tool plan serially through ``ToolRegistry.invoke``, then
    close the step. The loop ends when the model stops calling tools (run
    succeeded), the step ceiling is hit (§7 L341), the context budget
    overflows (§7 L339), or a failure face interrupts.

    The durable log is the only truth (§3.3): every decision about
    run/step/tool state is re-derived from :meth:`_load_state`; the in-memory
    :meth:`cancel_event` signal is an advisory fast path only (§7 L335).
    """

    def __init__(
        self,
        *,
        provider: Provider,
        context_builder: ContextBuilder,
        registry: ToolRegistry,
        committer: EventCommitter,
        log: EventLog,
        bus: VolatileBus,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
        limits: ToolLimits | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> None:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError(f"max_steps must be an integer >= 1, got {max_steps!r}")
        self._provider = provider
        self._context_builder = context_builder
        self._registry = registry
        self._committer = committer
        self._log = log
        self._bus = bus
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)
        self._limits: ToolLimits = limits if limits is not None else ToolLimits()
        self._max_steps = max_steps
        self._cancel_events: dict[str, asyncio.Event] = {}
        #: Per-run open-step context (model, started_at) -- not folded state:
        #: single-flight runner memory between step.started and step closure,
        #: consumed by the state-driven interrupt finalizer.
        self._active_steps: dict[str, tuple[ModelRef, datetime]] = {}

    # -- §7 L335: cooperative cancellation -------------------------------------

    def cancel_event(self, run_id: str) -> asyncio.Event:
        """The cooperative cancellation signal for ``run_id`` (create on demand).

        The durable ``py.run.cancel_requested`` commit is the truth; the host
        sets this in-memory signal only after that commit lands. The runner
        treats the signal as an advisory fast path: every finalize decision
        is re-derived from the folded log, and ``run.finished(interrupted)``
        is fold-legal from RUNNING even without a durable cancel (§2.1
        L59-61), so a signal set without durable intent still lands on a legal
        terminal instead of corrupting the fold (④ float report: unverified
        out-of-protocol path).
        """
        event = self._cancel_events.get(run_id)
        if event is None:
            event = asyncio.Event()
            self._cancel_events[run_id] = event
        return event

    # -- state and event plumbing ------------------------------------------------

    async def _load_state(self, session_id: str) -> SessionState:
        """Fold the whole durable log of one session (the only truth, §3.3)."""
        state = SessionState()
        after: EventCursor | None = None
        while True:
            page = await self._log.read(session_id, after=after)
            for event in page.items:
                state = apply_event(state, event)
            if not page.has_more:
                return state
            after = page.next_cursor

    def _draft[DataT: BaseModel](self, session_id: str, run_id: str, event_type: str, data: DataT) -> EventDraft[DataT]:
        """One durable run-scoped draft: fresh ``evt_`` id, clock stamp, run metadata."""
        return EventDraft[DataT](
            id=self._ids.event_id(),
            type=event_type,
            version=EVENT_VERSION,
            session_id=session_id,
            created_at=self._clock(),
            data=data,
            metadata={"run_id": run_id},
        )

    def _publish[DataT: BaseModel](self, session_id: str, run_id: str, event_type: str, data: DataT) -> None:
        """Publish one live-only event on the bus (§4 L226: never persisted, never blocking)."""
        self._bus.notify(
            session_id,
            [
                EphemeralEvent[DataT](
                    id=self._ids.event_id(),
                    type=event_type,
                    session_id=session_id,
                    created_at=self._clock(),
                    data=data,
                    metadata={"run_id": run_id},
                )
            ],
        )

    # -- the loop ------------------------------------------------------------------

    async def run(self, session_id: str, run_id: str) -> RunResult:
        """Drive one accepted run from ``py.run.started`` to ``py.run.finished``.

        Pre-checks the folded run state: unknown or already-terminal ->
        :class:`ConflictError`; cancelling -> the run finalizes interrupted
        without a ``run.started`` (§2.1 L59/L65: a cancel that commits first
        rejects the subsequent run.started). A hard ``CancelledError``
        (adapter propagation, §2.2 L84) is finalized interrupted only when
        the durable log says cancelling, then re-raised.
        """
        state = await self._load_state(session_id)
        run = state.runs.get(run_id)
        if run is None:
            raise ConflictError(f"run {run_id!r} is unknown in session {session_id!r}")
        if run.status.is_terminal:
            raise ConflictError(f"run {run_id!r} is already {run.status.value}")
        if run.status is RunStatus.CANCELLING:
            await self._finalize_interrupted(session_id, run_id, state, None)
            return RunResult(outcome="interrupted", steps=0)
        assert run.status is RunStatus.ACCEPTED
        try:
            await self._committer.commit(
                session_id,
                [self._draft(session_id, run_id, PY_RUN_STARTED, RunStartedData(run_id=run_id))],
            )
        except StoreError:
            # §2.1 L65 accepted/run.started race: a cancel_requested that
            # committed first rejects run.started; finalize interrupted.
            state = await self._load_state(session_id)
            run = state.runs.get(run_id)
            if run is not None and run.status is RunStatus.CANCELLING:
                await self._finalize_interrupted(session_id, run_id, state, None)
                return RunResult(outcome="interrupted", steps=0)
            raise
        try:
            return await self._step_loop(session_id, run_id)
        except asyncio.CancelledError:
            # The durable log decides: only a cancelling run gets a terminal;
            # a hard cancel without durable intent leaves the open step for
            # the recovery coordinator (§3.3 L210, ④ float report).
            state = await self._load_state(session_id)
            run = state.runs.get(run_id)
            if run is not None and run.status is RunStatus.CANCELLING:
                await self._finalize_interrupted(session_id, run_id, state, None)
            raise

    async def _step_loop(self, session_id: str, run_id: str) -> RunResult:
        """Generate steps until the run terminates (§2.3 L100 main loop).

        Per step: re-read the folded state (cancelling -> interrupted finalize;
        §7 L341 step ceiling -> failed), pin the catalog snapshot (§5.1 L253
        wiring), build the request (a ``ContextLimitExceeded`` fails the run
        with no step footprint, §7 L339), commit ``py.step.started``
        (``attempt``=1, OP-6), consume the stream, validate + commit the
        generation batch, then execute the tool plan serially (§5.1 L257).
        """
        steps = 0
        while True:
            state = await self._load_state(session_id)
            run = state.runs[run_id]
            if run.status is RunStatus.CANCELLING:
                # §3.1 L125: cancel blocks new step starts; no step is open.
                await self._finalize_interrupted(session_id, run_id, state, None)
                return RunResult(outcome="interrupted", steps=steps)
            if steps >= self._max_steps:
                # §7 L341: the step ceiling fails the run (TS MAX_STEPS_PROMPT
                # is reference semantics only); the last step is terminal.
                error = self._max_steps_error(run_id)
                await self._committer.commit(
                    session_id,
                    [self._run_finished_draft(session_id, run_id, "failed", error)],
                )
                return RunResult(outcome="failed", steps=steps, error=error)

            catalog = self._registry.snapshot(session_id)
            step_number = steps + 1
            step_id = f"step_{run_id[4:]}_{step_number:04d}"
            message_id = self._ids.message_id()
            request_id = f"req_{run_id[4:]}_{step_number:04d}"
            try:
                request = self._context_builder.build(
                    request_id=request_id,
                    history=state.messages,
                    tools=catalog.specs,
                )
            except ContextLimitExceeded as error:
                # §7 L339: history is preserved for a new session; the run
                # fails before any step footprint exists.
                await self._committer.commit(
                    session_id,
                    [self._run_finished_draft(session_id, run_id, "failed", error.core)],
                )
                return RunResult(outcome="failed", steps=steps, error=error.core)

            started_at = self._clock()
            try:
                await self._committer.commit(
                    session_id,
                    [
                        self._draft(
                            session_id,
                            run_id,
                            PY_STEP_STARTED,
                            StepStartedData(
                                run_id=run_id,
                                step_id=step_id,
                                message_id=message_id,
                                model=request.model,
                                attempt=1,
                            ),
                        )
                    ],
                )
            except StoreError:
                # A cancel_requested that committed after the state read
                # rejects step.started (the fold requires RUNNING): finalize
                # interrupted; any other store fault re-raises.
                state = await self._load_state(session_id)
                if state.runs[run_id].status is RunStatus.CANCELLING:
                    await self._finalize_interrupted(session_id, run_id, state, None)
                    return RunResult(outcome="interrupted", steps=steps)
                raise
            self._active_steps[run_id] = (request.model, started_at)
            steps += 1

            try:
                result = await self._consume(session_id, run_id, request, message_id)
                parts = self._plan(state, run_id, message_id, result, step_number=step_number)
            except _GenerationViolation as violation:
                # §2.3 L100 / §5.3 L276: the malformed/over-limit generation
                # fails and NO tool of the plan executes (never guessed).
                await self._fail_generation(
                    session_id,
                    run_id,
                    step_id,
                    message_id,
                    request.model,
                    started_at,
                    violation,
                )
                return RunResult(outcome="failed", steps=steps, error=violation.error)
            if result.interrupted:
                # §8 L350: the received text is committed once, never resent.
                partial = self._finalized_drafts(session_id, run_id, message_id, result.slots)
                if partial:
                    await self._committer.commit(session_id, partial)
                state = await self._load_state(session_id)
                await self._finalize_interrupted(session_id, run_id, state, result.error)
                return RunResult(outcome="interrupted", steps=steps, error=result.error)

            # §3.1 L144: all finalized parts by ordinal + exactly one
            # step.generated, one atomic EventBatch.
            batch = self._finalized_drafts(session_id, run_id, message_id, result.slots)
            generated = AssistantMessage(
                id=message_id,
                type="assistant",
                created_at=started_at,
                metadata={},
                model=request.model,
                parts=parts,
                status="running",
            )
            batch.append(
                self._draft(
                    session_id,
                    run_id,
                    PY_STEP_GENERATED,
                    StepGeneratedData(step_id=step_id, message=generated),
                )
            )
            await self._committer.commit(session_id, batch)

            # §2.3 L100: tools start strictly after the whole plan is durable.
            working_dir = state.working_dir
            assert working_dir is not None  # the fold pins it at session.created
            cancelled = await self._run_tools(session_id, run_id, message_id, parts, working_dir)
            if cancelled:
                state = await self._load_state(session_id)
                await self._finalize_interrupted(session_id, run_id, state, None)
                return RunResult(outcome="interrupted", steps=steps)
            finished = AssistantMessage(
                id=message_id,
                type="assistant",
                created_at=started_at,
                metadata={},
                model=request.model,
                parts=parts,
                status="completed",
                finish=result.finish.reason if result.finish is not None else None,
                raw_finish=result.finish.raw_reason if result.finish is not None else None,
                usage=result.usage.usage if result.usage is not None else None,
                cost_usd=result.usage.cost_usd if result.usage is not None else None,
                completed_at=self._clock(),
            )
            await self._committer.commit(
                session_id,
                [
                    self._draft(
                        session_id, run_id, PY_STEP_FINISHED, StepFinishedData(step_id=step_id, message=finished)
                    )
                ],
            )
            if not any(isinstance(part, AssistantToolPart) for part in parts):
                # No tool calls -> the run completes (TS step.ts Outcome:
                # every finish reason completes the step; continuation is
                # tool-part driven).
                try:
                    await self._committer.commit(
                        session_id,
                        [self._run_finished_draft(session_id, run_id, "succeeded", None)],
                    )
                except StoreError:
                    # A cancel that landed between step.finished and
                    # run.finished(succeeded) makes succeeded illegal from
                    # cancelling (§2.1 L59-61): finalize interrupted instead.
                    state = await self._load_state(session_id)
                    if state.runs[run_id].status is RunStatus.CANCELLING:
                        await self._finalize_interrupted(session_id, run_id, state, None)
                        return RunResult(outcome="interrupted", steps=steps)
                    raise
                return RunResult(outcome="succeeded", steps=steps)
            # else: the tool results feed the next request (A-006 replay
            # consistency -- the folded history replays the plan verbatim).

    async def _consume(
        self, session_id: str, run_id: str, request: GenerationRequest, message_id: str
    ) -> _StreamResult:
        """Consume one provider stream to its terminal chunk (§2.3 L88-102).

        Aggregates text/reasoning deltas per ``part_id`` and tool-call fragments
        per ``call_index`` (L100), publishes live ephemeral deltas on the bus
        (§4 L226) and enforces the three generation caps inline (§5.3 L276):
        cumulative text stream 1 MiB, 64 KiB arguments per call, at most 16
        calls per generation -- over-limit stops the generation via
        :class:`_GenerationViolation` so no over-limit plan executes. An error
        chunk or an EOF without a terminal chunk marks the result interrupted
        (§8 L350: no concatenation, no resend -- exactly one generation).
        """
        result = _StreamResult()
        limits = self._limits
        slots: list[_TextSlot | _CallSlot] = []
        text_slots: dict[str, _TextSlot] = {}
        call_slots: dict[int, _CallSlot] = {}
        text_bytes = 0
        ended_calls = 0

        def call_slot(call_index: int) -> _CallSlot:
            """Get-or-create the aggregation slot for ``call_index`` (§2.3 L100)."""
            slot = call_slots.get(call_index)
            if slot is None:
                slot = _CallSlot(call_index=call_index)
                call_slots[call_index] = slot
                slots.append(slot)
            return slot

        try:
            async for chunk in self._provider.stream(request):
                if isinstance(chunk, TextDeltaChunk | ReasoningDeltaChunk):
                    kind: Literal["text", "reasoning"] = "text" if isinstance(chunk, TextDeltaChunk) else "reasoning"
                    slot = text_slots.get(chunk.part_id)
                    if slot is not None and slot.kind != kind:
                        raise _GenerationViolation(
                            "part_id_reused",
                            f"part {chunk.part_id!r} carries both text and reasoning fragments (§2.3 L92)",
                        )
                    if slot is None:
                        slot = _TextSlot(kind=kind, part_id=chunk.part_id)
                        text_slots[chunk.part_id] = slot
                        slots.append(slot)
                    slot.deltas.append(chunk.delta)
                    text_bytes += len(chunk.delta.encode("utf-8"))
                    if text_bytes > limits.max_text_stream_bytes:
                        raise _GenerationViolation(
                            "max_text_stream_bytes",
                            f"generation text stream exceeded {limits.max_text_stream_bytes} bytes (§5.3 L276)",
                        )
                    if kind == "text":
                        self._publish(
                            session_id,
                            run_id,
                            PY_TEXT_DELTA,
                            TextDeltaData(message_id=message_id, part_id=chunk.part_id, delta=chunk.delta),
                        )
                    else:
                        self._publish(
                            session_id,
                            run_id,
                            PY_REASONING_DELTA,
                            ReasoningDeltaData(message_id=message_id, part_id=chunk.part_id, delta=chunk.delta),
                        )
                elif isinstance(chunk, ToolCallStartChunk):
                    call = call_slot(chunk.call_index)
                    if chunk.tool_call_id is not None:
                        call.tool_call_id = chunk.tool_call_id
                    if chunk.name is not None:
                        call.name = chunk.name
                elif isinstance(chunk, ToolCallArgsDeltaChunk):
                    call = call_slot(chunk.call_index)
                    call.args_bytes += len(chunk.delta.encode("utf-8"))
                    if call.args_bytes > limits.max_argument_bytes:
                        raise _GenerationViolation(
                            "max_argument_bytes",
                            f"tool call {chunk.call_index} arguments exceeded "
                            f"{limits.max_argument_bytes} bytes (§5.3 L276)",
                        )
                    self._publish(
                        session_id,
                        run_id,
                        PY_TOOL_INPUT_DELTA,
                        ToolInputDeltaData(call_index=chunk.call_index, delta=chunk.delta),
                    )
                elif isinstance(chunk, ToolCallEndChunk):
                    call = call_slot(chunk.call_index)
                    if call.end is None:
                        ended_calls += 1
                        if ended_calls > limits.max_tool_calls_per_generation:
                            raise _GenerationViolation(
                                "max_tool_calls_per_generation",
                                f"generation exceeded {limits.max_tool_calls_per_generation} tool calls (§5.3 L276)",
                            )
                    call.end = chunk
                elif isinstance(chunk, UsageChunk):
                    result.usage = chunk  # L96: may arrive after finish; never lost
                elif isinstance(chunk, FinishChunk):
                    if result.finish is None:
                        result.finish = chunk
                else:
                    assert isinstance(chunk, ErrorChunk)
                    # §2.3 L98: terminal chunk; the error rides verbatim and
                    # the generation ends interrupted (no resend, §8 L350).
                    result.error = chunk.error
                    result.interrupted = True
                    break
        except asyncio.CancelledError:
            # §3.1 L142: a normal cancel preserves as much received text as
            # possible -- commit the aggregated parts best-effort, then let
            # the hard-cancel path finalize from the folded truth.
            partial = self._finalized_drafts(session_id, run_id, message_id, slots)
            if partial:
                try:
                    await self._committer.commit(session_id, partial)
                except StoreError:
                    pass  # best-effort: the interrupt terminal is state-driven
            raise

        result.slots = slots
        if result.error is not None:
            result.interrupted = True
            return result
        if result.finish is None:
            result.interrupted = True
            result.error = CoreError(
                code=CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
                message=(
                    f"provider stream for request {request.request_id} ended without a "
                    "finish or error chunk (§8 L350: no concatenation, no resend)"
                ),
                retryable=True,
            )
        return result

    def _plan(
        self,
        state: SessionState,
        run_id: str,
        message_id: str,
        result: _StreamResult,
        *,
        step_number: int,
    ) -> list[AssistantPart]:
        """Validate the aggregated call plan and build the parts (§2.3 L100).

        Fails the whole generation (no tool executes) on: a call without its
        end chunk, arguments that are not complete JSON (including NaN/
        Infinity and float overflow -- not JSON, §1), a JSON value that is not
        an object (④-8: the ``AssistantToolPart.input`` wire face is
        ``JsonObject`` -- a non-object cannot be represented, so the
        generation fails instead of executing anything), a ``tool_call_id``
        duplicated within this response, or one already used by an earlier
        response (④-5, §2.1 L37 identity).
        """
        parts: list[AssistantPart] = []
        seen: set[str] = _history_call_ids(state)
        call_ordinal = 0
        for slot in result.slots:
            if isinstance(slot, _TextSlot):
                if slot.kind == "text":
                    parts.append(AssistantTextPart(type="text", part_id=slot.part_id, text=slot.text))
                else:
                    parts.append(AssistantReasoningPart(type="reasoning", part_id=slot.part_id, text=slot.text))
                continue
            end = slot.end
            if end is None:
                raise _GenerationViolation(
                    "tool_call_without_end",
                    f"tool call {slot.call_index} never sent its end chunk (§2.3 L100)",
                )
            try:
                arguments = json.loads(end.raw_arguments, parse_constant=_reject_json_constant)
            except ValueError as error:
                raise _GenerationViolation(
                    "tool_arguments_not_json",
                    f"tool call {end.tool_call_id!r} arguments are not complete JSON (§2.3 L100): {error}",
                ) from error
            if not isinstance(arguments, dict):
                raise _GenerationViolation(
                    "tool_arguments_not_object",
                    f"tool call {end.tool_call_id!r} arguments are valid JSON but not an object (§2.3 L100)",
                )
            try:
                canonical_json(arguments)
            except ValueError as error:
                # e.g. 1e999 overflows to infinity: parses, but is not JSON.
                raise _GenerationViolation(
                    "tool_arguments_not_json",
                    f"tool call {end.tool_call_id!r} arguments contain non-JSON values (§1): {error}",
                ) from error
            if end.tool_call_id in seen:
                raise _GenerationViolation(
                    "duplicate_tool_call_id",
                    f"tool_call_id {end.tool_call_id!r} is not unique across the history (§2.1 L37)",
                )
            seen.add(end.tool_call_id)
            call_ordinal += 1
            parts.append(
                AssistantToolPart(
                    type="tool",
                    tool_call_id=end.tool_call_id,
                    execution_id=f"exec_{run_id[4:]}_{step_number:04d}_{call_ordinal:04d}",
                    name=end.name,
                    input=arguments,
                    state="pending",
                    effect_status="not_started",
                    content=[],
                )
            )
        return parts

    def _finalized_drafts(
        self, session_id: str, run_id: str, message_id: str, slots: Sequence[_TextSlot | _CallSlot]
    ) -> list[EventDraft[Any]]:
        """The finalized events of one generation in ordinal order (§3.1 L144).

        Text slots emit ``py.text.finalized``/``py.reasoning.finalized`` with
        unique ascending ordinals (the counter runs over text/reasoning slots
        only -- tool-call slots carry no finalized event); call slots are
        skipped.
        """
        drafts: list[EventDraft[Any]] = []
        ordinal = 0
        for slot in slots:
            if isinstance(slot, _CallSlot):
                continue
            if slot.kind == "text":
                data: TextFinalizedData | ReasoningFinalizedData = TextFinalizedData(
                    message_id=message_id, part_id=slot.part_id, ordinal=ordinal, text=slot.text
                )
                event_type = PY_TEXT_FINALIZED
            else:
                data = ReasoningFinalizedData(
                    message_id=message_id, part_id=slot.part_id, ordinal=ordinal, text=slot.text
                )
                event_type = PY_REASONING_FINALIZED
            drafts.append(self._draft(session_id, run_id, event_type, data))
            ordinal += 1
        return drafts

    async def _run_tools(
        self,
        session_id: str,
        run_id: str,
        message_id: str,
        parts: list[AssistantPart],
        working_dir: str,
    ) -> bool:
        """Execute the durable call plan serially (§5.1 L257, PHASE1-SLIM L85).

        Returns ``True`` when the loop stopped for cancellation (the caller
        finalizes interrupted); ``False`` when every planned call terminated.
        Each call goes through :meth:`ToolRegistry.invoke` with the raw
        requested wire name (§5.1 L253 -- no caller-side pre-resolution; the
        dotted->wire seam conversion is the one mapping this module owns);
        unknown tools resolve to nothing in the pinned catalog and the Invoker
        produces their ``failed/not_started`` terminals (§2.3 L100), keeping
        the tool_result pairing complete (§3.3 L212).
        """
        for position, part in enumerate(parts):
            if not isinstance(part, AssistantToolPart):
                continue
            if self.cancel_event(run_id).is_set():
                # §3.1 L125: a cancel intent blocks new tool starts; pending
                # calls get synthesized cancelled terminals in the finalize.
                return True
            context = ToolContext(
                session_id=session_id,
                run_id=run_id,
                message_id=message_id,
                tool_call_id=part.tool_call_id,
                working_dir=working_dir,
                limits=self._limits,
                cancel=self.cancel_event(run_id),
                progress=self._progress_publisher(session_id, run_id, part.execution_id),
            )
            try:
                outcome = await self._registry.invoke(
                    context, _wire_tool_name(part.name), part.input, part.execution_id
                )
            except StoreError:
                # A cancel_requested that landed mid-loop rejects the next
                # tool.requested commit (the fold requires RUNNING): re-read
                # and finalize interrupted only for that race; a genuine
                # storage fault re-raises untouched.
                state = await self._load_state(session_id)
                if state.runs[run_id].status is RunStatus.CANCELLING:
                    return True
                raise
            parts[position] = part.model_copy(
                update={
                    "state": outcome.state,
                    "effect_status": outcome.effect_status,
                    "content": list(outcome.content),
                    "structured_output": outcome.structured_output,
                    "error": outcome.error,
                }
            )
        return False

    def _progress_publisher(
        self, session_id: str, run_id: str, execution_id: str
    ) -> Callable[[JsonObject], Awaitable[None]]:
        """The §5.1 L255 progress face: adapter metadata -> ``py.tool.progress`` (live-only)."""

        async def publish(metadata: JsonObject) -> None:
            self._publish(
                session_id,
                run_id,
                PY_TOOL_PROGRESS,
                ToolProgressData(execution_id=execution_id, metadata=metadata),
            )

        return publish

    async def _finalize_interrupted(
        self,
        session_id: str,
        run_id: str,
        state: SessionState,
        error: CoreError | None,
    ) -> None:
        """Terminate the open step (if any) and the run as interrupted.

        The single state-driven implementation for every interrupt path (§7
        L335, §8 L350): one atomic batch in fold order (§2.1 L65) --
        synthesized terminals for the still-open tools first (pending ->
        ``cancelled/not_started``; started without a terminal ->
        ``unknown/unknown``, §3.3 L210), then the interrupted
        ``py.step.finished``, then ``py.run.finished``. The terminal message
        is rebuilt from the folded truth: the generated branch keeps the
        committed text and tool facts; the non-generated branch carries
        exactly the committed finalized text (§3.3 L212); ``finish``/``usage``
        stay conservative ``None``. The open step's ``(model, started_at)``
        comes from the single-flight ``_active_steps`` bookkeeping.
        """
        drafts: list[EventDraft[Any]] = []
        #: The synthesized tool terminals of this batch, by execution id: the
        #: step terminal message must carry exactly these (the pre-batch fold
        #: still shows them pending/running -- §3.3 L210, the fold is truth but
        #: this batch is the next truth).
        synthesized: dict[str, ToolFinishedData] = {}
        step = next(
            (info for info in state.steps.values() if info.run_id == run_id and not info.status.is_terminal),
            None,
        )
        if step is not None:
            for tool in state.tools.values():
                if tool.run_id != run_id or tool.state.is_terminal:
                    continue
                if tool.state is ToolState.PENDING:
                    # §3.3 L210: pending -> cancelled/not_started (§2.1 L72
                    # model-readable synthesized text).
                    terminal = ToolFinishedData(
                        execution_id=tool.execution_id,
                        state="cancelled",
                        effect_status="not_started",
                        content=_cancelled_content(tool.execution_id),
                    )
                else:
                    # §3.3 L210/§7 L335: a started call without a determined
                    # result keeps the honest unknown terminal.
                    terminal = ToolFinishedData(
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
                synthesized[tool.execution_id] = terminal
                drafts.append(self._draft(session_id, run_id, PY_TOOL_FINISHED, terminal))
            entry = self._active_steps.get(run_id)
            assert entry is not None  # an open step implies its step.started set the entry
            model, started_at = entry
            message = self._interrupted_message(
                state, step, error, synthesized=synthesized, model=model, started_at=started_at
            )
            drafts.append(
                self._draft(
                    session_id, run_id, PY_STEP_FINISHED, StepFinishedData(step_id=step.step_id, message=message)
                )
            )
        drafts.append(self._run_finished_draft(session_id, run_id, "interrupted", error))
        await self._committer.commit(session_id, drafts)

    def _interrupted_message(
        self,
        state: SessionState,
        step: StepInfo,
        error: CoreError | None,
        *,
        model: ModelRef,
        started_at: datetime,
        synthesized: dict[str, ToolFinishedData],
    ) -> AssistantMessage:
        """The interrupted terminal message, rebuilt from the folded truth.

        ``synthesized`` carries this batch's synthesized tool terminals: their
        parts must reflect those (the pre-batch fold still shows them
        pending/running); tools that already committed their own terminal keep
        the folded facts.
        """
        if step.generated:
            existing = next(
                (
                    message
                    for message in state.messages
                    if message.id == step.message_id and isinstance(message, AssistantMessage)
                ),
                None,
            )
            assert existing is not None  # the fold guarantees the generated message
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
                tool = state.tools[part.execution_id]  # the plan registered every execution
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
                error=error,
                completed_at=self._clock(),
            )
        # §3.3 L212: without a generated plan, only the committed finalized
        # text composes the interrupted terminal (no tool parts possible).
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
            error=error,
            completed_at=self._clock(),
        )

    async def _fail_generation(
        self,
        session_id: str,
        run_id: str,
        step_id: str,
        message_id: str,
        model: ModelRef,
        started_at: datetime,
        violation: _GenerationViolation,
    ) -> None:
        """Fail the open step and the run for a malformed/over-limit generation.

        §2.3 L100 / §5.3 L276: the generation fails and NO tool of the plan
        executes -- the received stream is not committed (nothing finalized;
        §3.1 L142 preserves received text for *cancels*, not violations) and
        the step terminal carries only the violation error. One atomic batch:
        ``py.step.finished`` (failed, empty parts) then ``py.run.finished``.
        """
        message = AssistantMessage(
            id=message_id,
            type="assistant",
            created_at=started_at,
            metadata={},
            model=model,
            parts=[],
            status="failed",
            error=violation.error,
            completed_at=self._clock(),
        )
        await self._committer.commit(
            session_id,
            [
                self._draft(session_id, run_id, PY_STEP_FINISHED, StepFinishedData(step_id=step_id, message=message)),
                self._run_finished_draft(session_id, run_id, "failed", violation.error),
            ],
        )

    def _run_finished_draft(
        self, session_id: str, run_id: str, outcome: RunOutcome, error: CoreError | None
    ) -> EventDraft[RunFinishedData]:
        """One ``py.run.finished`` draft (the idle projection folds from it, §2.1 L65)."""
        return self._draft(
            session_id,
            run_id,
            PY_RUN_FINISHED,
            RunFinishedData(run_id=run_id, outcome=outcome, error=error),
        )

    def _max_steps_error(self, run_id: str) -> CoreError:
        """§7 L341: the step-ceiling failure (reference semantics only, no prompt text)."""
        return CoreError(
            code=CoreErrorCode.VALIDATION_ERROR,
            message=(
                f"run {run_id} exceeded the step ceiling of {self._max_steps} without finishing "
                "(§7 L341: the run fails; a user retry is a new run)"
            ),
            retryable=False,
            details={"violation": "max_steps", "max_steps": self._max_steps},
        )


def _reject_json_constant(value: str) -> Any:
    """``json.loads`` hook: NaN/Infinity/-Infinity are not JSON (§1)."""
    raise ValueError(f"{value} is not valid JSON")


def _history_call_ids(state: SessionState) -> set[str]:
    """Every ``tool_call_id`` already used by folded assistant tool parts (④-5).

    §2.1 L37 identity: a model re-issuing a used ``tool_call_id`` -- same or
    different content -- is rejected across responses, never silently reused.
    """
    ids: set[str] = set()
    for message in state.messages:
        if not isinstance(message, AssistantMessage):
            continue
        for part in message.parts:
            if isinstance(part, AssistantToolPart):
                ids.add(part.tool_call_id)
    return ids
