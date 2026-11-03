"""Deterministic session-state fold (R1-S4 + R2-S2; CORE-CONTRACTS §2.1/§3.1/§3.3/§6.1).

:func:`apply` is the single source of the fold rules. R1: ``py.session.created``
builds the active state (working_dir + config_snapshot + snapshot_hash, §6.1) and
``py.prompt.accepted`` appends the embedded user message plus the accepted run slot
(§2.1 ``user`` row, §7 L316). R2-S2 extends the fold to the full R2 event subset:
finalized parts land by ``message_id+part_id`` with unique ordinals (§3.1 L144),
``py.step.generated`` appends the assistant message and registers the pending tool
plan (§2.1 L43), ``py.tool.requested/started/finished`` drive the tool state
machine (L44-52, effect_status recorded independently per L35), ``py.step.finished``
replaces the assistant message with the self-contained terminal message
(consistency with the folded parts, §3.1 L144), and ``py.run.finished`` appends
the idle message and releases ``active_run_id`` (L65 "idle 只由 run.finished 投影").
The fold is a pure function (§3.3): no clock, no randomness, no external calls;
read-model writes live in :mod:`opencode.session.projector` and the rebuild
replays the log through this same fold. Event types outside the folded subset
raise instead of being skipped (fail closed).

Legacy compatibility (plan §0.2): R1 logs (created + accepted*, several accepted
per session) fold identically through the R2 rules -- the fold adds no "previous
run must be terminal before the next accepted" gate; a repeated run identity under
the same request_id is an idempotent re-accept (R1 logs legitimately reuse one run
id across prompts), a different request_id under a used run id is illegal. The
one-active-run invariant stays a §7 SessionService protocol gate, not a fold gate.

TS mapping (``packages/core/src/session/projector.ts`` / ``message-updater.ts``):
TS keeps the state machine inside the projector/updater adapters; Python extracts
the pure fold so commit-time projection, rebuild and tests share one rule set.
``run()/SessionMessageUpdater`` L261-476 and ``projectIdle`` L506-535 map onto the
handlers below. The idle message id is derived from the run id
(``msg_idle_{run_id}``, §3.3 "idle ID 由 run_id 确定") instead of the event id
(TS parity deviation, float-reported); the terminal assistant message may drop a
committed reasoning part and keep only model-readable tool text (§2.1 L72), which
the step.finished consistency check accepts as ordered containment.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import JsonValue

from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    CoreError,
    DurableEvent,
    JsonObject,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    canonical_json,
)
from opencode.contracts.events import (
    PY_REASONING_FINALIZED,
    PY_RUN_CANCEL_REQUESTED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_STEP_FINISHED,
    PY_STEP_GENERATED,
    PY_STEP_STARTED,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    ReasoningFinalizedData,
    RunCancelRequestedData,
    RunFinishedData,
    RunStartedData,
    StepFinishedData,
    StepGeneratedData,
    StepStartedData,
    TextFinalizedData,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolContent,
)


class SessionStatus(StrEnum):
    """The four §3.2 session statuses; ``.value`` is the stored wire text."""

    ACTIVE = "active"
    READ_ONLY_DIAGNOSTIC = "read_only_diagnostic"
    CLEANUP_FAILED = "cleanup_failed"
    CLOSED = "closed"


class RunStatus(StrEnum):
    """§2.1 L55-63 run states; terminal outcomes match ``py.run.finished``."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self not in _RUN_NON_TERMINAL


class StepStatus(StrEnum):
    """§2.1 L62-63 step states; the terminal values equal the message status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self is not StepStatus.RUNNING


class ToolState(StrEnum):
    """§2.1 L35/43-52 tool execution states (pending/running + five terminals)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self is not ToolState.PENDING and self is not ToolState.RUNNING


class EffectStatus(StrEnum):
    """§2.1 L35: side-effect determinism, independent of the call outcome."""

    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


_RUN_NON_TERMINAL = frozenset({RunStatus.ACCEPTED, RunStatus.RUNNING, RunStatus.CANCELLING})
#: Legal py.run.finished outcomes per current status (§2.1 L59-61).
_RUN_FINISH_OUTCOMES: dict[RunStatus, frozenset[str]] = {
    RunStatus.ACCEPTED: frozenset({"interrupted"}),
    RunStatus.RUNNING: frozenset({"succeeded", "failed", "interrupted"}),
    RunStatus.CANCELLING: frozenset({"interrupted", "failed"}),
}


class IllegalTransitionError(ValueError):
    """The event cannot legally extend this state (§3.2 legal-transition checks).

    The fold raises instead of skipping or coercing; the projector maps this
    onto ``StorageCorruptError`` and the rebuild onto read-only diagnostics
    (§3.2.1).
    """


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One accepted run (the §3.2 runs read-model row source)."""

    run_id: str
    request_id: str
    status: RunStatus
    error: CoreError | None = None


@dataclass(frozen=True, slots=True)
class StepInfo:
    """One generation attempt of a run (§2.1 L62-63)."""

    step_id: str
    run_id: str
    message_id: str
    status: StepStatus
    generated: bool = False


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """One tool execution: the plan row plus the folded terminal facts."""

    execution_id: str
    run_id: str
    step_id: str
    message_id: str
    tool_call_id: str
    input_hash: str
    state: ToolState
    effect_status: EffectStatus
    content: tuple[ToolContent, ...] = ()
    structured_output: JsonValue | None = None
    error: CoreError | None = None


@dataclass(frozen=True, slots=True)
class FinalizedPart:
    """A committed ``py.text.finalized``/``py.reasoning.finalized`` value."""

    kind: str
    part_id: str
    ordinal: int
    text: str


def compute_input_hash(input: JsonObject) -> str:
    """sha256 hex of the canonical JSON of a tool input (§3.1 L129 ``input_hash``)."""
    return sha256(canonical_json(input).encode("utf-8")).hexdigest()


#: Folded message sequence: user (prompt), assistant (generated/finished), idle.
FoldedMessage = UserMessage | AssistantMessage | IdleMessage


@dataclass(frozen=True, slots=True)
class SessionState:
    """Fold state of one session: empty until ``py.session.created`` is folded.

    ``session_id``/``status`` are ``None`` before the create event. The R2 maps
    (``runs``/``steps``/``tools``/``finalized``) are copy-on-write: every
    transition rebuilds the dict through :func:`dataclasses.replace`, so the
    state stays a frozen value. The §3.2.1 minimal diagnostic record is not
    producible by the fold and is never represented here.
    """

    session_id: str | None = None
    status: SessionStatus | None = None
    applied_seq: int = 0
    working_dir: str | None = None
    config_snapshot: ConfigSnapshot | None = None
    snapshot_hash: str | None = None
    messages: tuple[FoldedMessage, ...] = ()
    active_run_id: str | None = None
    runs: dict[str, RunInfo] = field(default_factory=dict)
    steps: dict[str, StepInfo] = field(default_factory=dict)
    tools: dict[str, ToolInfo] = field(default_factory=dict)
    finalized: dict[tuple[str, str], FinalizedPart] = field(default_factory=dict)

    @property
    def created(self) -> bool:
        """True once ``py.session.created`` has been folded."""
        return self.session_id is not None


def apply(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """Fold one durable event onto ``state`` (pure; same input -> same output).

    Fail closed on: ownership mismatch (event belongs to another session), a
    second ``py.session.created``, ``py.prompt.accepted`` without an active
    session, a data model that does not match the event type, run-scoped
    ownership (envelope ``metadata.run_id`` must match the event's run), any
    illegal transition of the §2.1 run/step/tool tables, and any event type
    outside the folded subset.
    """
    if state.created and state.session_id != event.session_id:
        raise IllegalTransitionError(
            f"event {event.id} belongs to session {event.session_id!r}, not {state.session_id!r}"
        )
    if event.type == PY_SESSION_CREATED:
        return _apply_created(state, event)
    if event.type == PY_PROMPT_ACCEPTED:
        return _apply_prompt(state, event)
    if event.type == PY_RUN_STARTED:
        return _apply_run_started(state, event)
    if event.type == PY_RUN_CANCEL_REQUESTED:
        return _apply_run_cancel_requested(state, event)
    if event.type == PY_RUN_FINISHED:
        return _apply_run_finished(state, event)
    if event.type == PY_STEP_STARTED:
        return _apply_step_started(state, event)
    if event.type == PY_TEXT_FINALIZED:
        return _apply_finalized(state, event, "text")
    if event.type == PY_REASONING_FINALIZED:
        return _apply_finalized(state, event, "reasoning")
    if event.type == PY_STEP_GENERATED:
        return _apply_step_generated(state, event)
    if event.type == PY_STEP_FINISHED:
        return _apply_step_finished(state, event)
    if event.type == PY_TOOL_REQUESTED:
        return _apply_tool_requested(state, event)
    if event.type == PY_TOOL_STARTED:
        return _apply_tool_started(state, event)
    if event.type == PY_TOOL_FINISHED:
        return _apply_tool_finished(state, event)
    raise IllegalTransitionError(f"event type {event.type!r} is not foldable")


# -- R1 handlers (semantics unchanged; prompt additionally registers the run) --


def _apply_created(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.session.created``: empty -> active with the §6.1 snapshot fields."""
    if not isinstance(event.data, SessionCreatedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected SessionCreatedData"
        )
    if state.created:
        raise IllegalTransitionError(f"session {event.session_id} is already created (event {event.id})")
    return SessionState(
        session_id=event.session_id,
        status=SessionStatus.ACTIVE,
        applied_seq=event.seq,
        working_dir=event.data.working_dir,
        config_snapshot=event.data.config_snapshot,
        snapshot_hash=event.data.snapshot_hash,
    )


def _apply_prompt(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.prompt.accepted``: active -> active, user message + accepted run slot.

    The fold carries the accepted run slot (§7 L316): ``active_run_id`` becomes
    the event's ``run_id`` (last accepted run wins in the log order). A second
    ``py.prompt.accepted`` is NOT rejected while the slot is held (§0.2: the
    one-active-run invariant is a §7 SessionService gate, and multi-accepted
    logs are the R1 baseline). R2 additionally registers the run in the runs
    map: a fresh run id is accepted; a known run id under the same request_id
    is an idempotent re-accept (legacy logs reuse one run id); a known run id
    under a different request_id is an identity conflict (§2.1 L37 spirit).
    """
    if not isinstance(event.data, PromptAcceptedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected PromptAcceptedData"
        )
    if not state.created or state.status is not SessionStatus.ACTIVE:
        raise IllegalTransitionError(f"py.prompt.accepted requires an active session (event {event.id})")
    runs = state.runs
    known = runs.get(event.data.run_id)
    if known is not None and known.request_id != event.data.request_id:
        raise IllegalTransitionError(
            f"run {event.data.run_id} is already accepted under request {known.request_id!r} (event {event.id})"
        )
    if known is None:
        runs = {
            **state.runs,
            event.data.run_id: RunInfo(
                run_id=event.data.run_id, request_id=event.data.request_id, status=RunStatus.ACCEPTED
            ),
        }
    return replace(
        state,
        applied_seq=event.seq,
        active_run_id=event.data.run_id,
        runs=runs,
        messages=(*state.messages, event.data.message),
    )


# -- R2 handlers (§2.1 L39-65 tables; the fold is their single source) --


def _metadata_run_id(event: DurableEvent[Any]) -> str | None:
    """Envelope ``metadata.run_id`` when it is a string (validated at decode)."""
    value = event.metadata.get("run_id")
    return value if isinstance(value, str) else None


def _require_run(state: SessionState, event: DurableEvent[Any], run_id: str) -> RunInfo:
    """Fetch the run and enforce the envelope ownership (metadata.run_id)."""
    run = state.runs.get(run_id)
    if run is None:
        raise IllegalTransitionError(f"event {event.id} references unknown run {run_id!r}")
    if _metadata_run_id(event) != run_id:
        raise IllegalTransitionError(f"event {event.id} metadata.run_id does not match run {run_id!r}")
    return run


def _apply_run_started(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.run.started``: accepted -> running (§2.1 L59)."""
    if not isinstance(event.data, RunStartedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected RunStartedData"
        )
    run = _require_run(state, event, event.data.run_id)
    if run.status is not RunStatus.ACCEPTED:
        raise IllegalTransitionError(f"run {event.data.run_id} is {run.status.value}, not accepted (event {event.id})")
    return _with_run(state, event, replace(run, status=RunStatus.RUNNING))


def _apply_run_cancel_requested(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.run.cancel_requested``: accepted|running -> cancelling, once (§2.1 L60)."""
    if not isinstance(event.data, RunCancelRequestedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected RunCancelRequestedData"
        )
    run = _require_run(state, event, event.data.run_id)
    if run.status is not RunStatus.ACCEPTED and run.status is not RunStatus.RUNNING:
        raise IllegalTransitionError(
            f"run {event.data.run_id} is {run.status.value}; cancel_requested needs accepted or running "
            f"(event {event.id})"
        )
    return _with_run(state, event, replace(run, status=RunStatus.CANCELLING))


def _apply_run_finished(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.run.finished``: fold the outcome, append idle, release the slot (§2.1 L65)."""
    if not isinstance(event.data, RunFinishedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected RunFinishedData"
        )
    run = _require_run(state, event, event.data.run_id)
    if run.status.is_terminal:
        # §2.1 L60/L63: a terminal run is never overwritten; the identical
        # outcome is an idempotent replay, anything else is a conflict.
        if run.status.value == event.data.outcome and run.error == event.data.error:
            return replace(state, applied_seq=event.seq)
        raise IllegalTransitionError(f"run {event.data.run_id} is already terminal (event {event.id})")
    if event.data.outcome not in _RUN_FINISH_OUTCOMES[run.status]:
        raise IllegalTransitionError(
            f"run {event.data.run_id} cannot finish {event.data.outcome!r} from {run.status.value} (event {event.id})"
        )
    for step in state.steps.values():
        if step.run_id == event.data.run_id and not step.status.is_terminal:
            raise IllegalTransitionError(
                f"run {event.data.run_id} finished before step {step.step_id} terminated (event {event.id})"
            )
    idle = IdleMessage(
        id=f"msg_idle_{event.data.run_id}",
        type="idle",
        created_at=event.created_at,
        metadata={},
        run_id=event.data.run_id,
        outcome=event.data.outcome,
        error=event.data.error,
    )
    return replace(
        state,
        applied_seq=event.seq,
        runs={**state.runs, run.run_id: replace(run, status=RunStatus(event.data.outcome), error=event.data.error)},
        messages=(*state.messages, idle),
        active_run_id=None if state.active_run_id == event.data.run_id else state.active_run_id,
    )


def _apply_step_started(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.step.started``: register one generation attempt (§2.1 L62, L65)."""
    if not isinstance(event.data, StepStartedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected StepStartedData"
        )
    run = _require_run(state, event, event.data.run_id)
    if run.status is not RunStatus.RUNNING:
        raise IllegalTransitionError(
            f"run {event.data.run_id} is {run.status.value}; a step may only start while running (event {event.id})"
        )
    if event.data.step_id in state.steps:
        raise IllegalTransitionError(f"step {event.data.step_id!r} is already registered (event {event.id})")
    for step in state.steps.values():
        if step.run_id == event.data.run_id and not step.status.is_terminal:
            raise IllegalTransitionError(
                f"run {event.data.run_id} still has the running step {step.step_id} (event {event.id})"
            )
        if step.message_id == event.data.message_id:
            raise IllegalTransitionError(f"message id {event.data.message_id!r} is already used (event {event.id})")
    for message in state.messages:
        if message.id == event.data.message_id:
            raise IllegalTransitionError(f"message id {event.data.message_id!r} is already used (event {event.id})")
    return _with_step(
        state,
        event,
        StepInfo(
            step_id=event.data.step_id,
            run_id=event.data.run_id,
            message_id=event.data.message_id,
            status=StepStatus.RUNNING,
        ),
    )


def _apply_finalized(state: SessionState, event: DurableEvent[Any], kind: str) -> SessionState:
    """``py.text.finalized``/``py.reasoning.finalized``: locate + unique ordinal (§3.1 L144)."""
    if not isinstance(event.data, TextFinalizedData | ReasoningFinalizedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected a finalized data model"
        )
    data = event.data
    run_id = _metadata_run_id(event)
    if run_id is None or run_id not in state.runs:
        raise IllegalTransitionError(f"event {event.id} references unknown run {run_id!r}")
    if state.runs[run_id].status is RunStatus.ACCEPTED or state.runs[run_id].status.is_terminal:
        raise IllegalTransitionError(
            f"run {run_id} is {state.runs[run_id].status.value}; finalized needs running or cancelling "
            f"(event {event.id})"
        )
    step = _step_for_message(state, run_id, data.message_id)
    if step.generated:
        raise IllegalTransitionError(
            f"step {step.step_id} already closed its generation batch; no further finalized (event {event.id})"
        )
    key = (data.message_id, data.part_id)
    known = state.finalized.get(key)
    if known is not None:
        if (known.kind, known.ordinal, known.text) == (kind, data.ordinal, data.text):
            return replace(state, applied_seq=event.seq)
        raise IllegalTransitionError(
            f"part {data.part_id!r} already finalized with different content (event {event.id})"
        )
    for (message_id, _), part in state.finalized.items():
        if message_id == data.message_id and part.ordinal == data.ordinal:
            raise IllegalTransitionError(
                f"ordinal {data.ordinal} is already used by part {part.part_id!r} (event {event.id})"
            )
    return replace(
        state,
        applied_seq=event.seq,
        finalized={
            **state.finalized,
            key: FinalizedPart(kind=kind, part_id=data.part_id, ordinal=data.ordinal, text=data.text),
        },
    )


def _apply_step_generated(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.step.generated``: append the assistant message, register the plan (§2.1 L65)."""
    if not isinstance(event.data, StepGeneratedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected StepGeneratedData"
        )
    step = state.steps.get(event.data.step_id)
    if step is None:
        raise IllegalTransitionError(f"event {event.id} references unknown step {event.data.step_id!r}")
    run = _require_run(state, event, step.run_id)
    if run.status is RunStatus.ACCEPTED or run.status.is_terminal:
        raise IllegalTransitionError(
            f"run {step.run_id} is {run.status.value}; generated needs running or cancelling (event {event.id})"
        )
    if step.status is not StepStatus.RUNNING or step.generated:
        raise IllegalTransitionError(f"step {step.step_id} already closed its generation batch (event {event.id})")
    message = event.data.message
    if message.id != step.message_id:
        raise IllegalTransitionError(
            f"step {step.step_id} owns message {step.message_id!r}, not {message.id!r} (event {event.id})"
        )
    if message.status != "running":
        raise IllegalTransitionError(
            f"generated message {message.id} must be running, not {message.status!r} (event {event.id})"
        )
    _require_finalized_match(state, message)
    tools = dict(state.tools)
    planned_call_ids: set[str] = set()
    for part in message.parts:
        if not isinstance(part, AssistantToolPart):
            continue
        if part.state != "pending" or part.effect_status != "not_started":
            raise IllegalTransitionError(
                f"tool {part.tool_call_id!r} must enter the plan pending/not_started (event {event.id})"
            )
        if part.tool_call_id in planned_call_ids:
            raise IllegalTransitionError(
                f"tool_call_id {part.tool_call_id!r} is not unique within the plan (event {event.id})"
            )
        planned_call_ids.add(part.tool_call_id)
        if part.execution_id in tools:
            raise IllegalTransitionError(f"execution {part.execution_id!r} is already registered (event {event.id})")
        tools[part.execution_id] = ToolInfo(
            execution_id=part.execution_id,
            run_id=step.run_id,
            step_id=step.step_id,
            message_id=message.id,
            tool_call_id=part.tool_call_id,
            input_hash=compute_input_hash(part.input),
            state=ToolState.PENDING,
            effect_status=EffectStatus.NOT_STARTED,
        )
    return replace(
        state,
        applied_seq=event.seq,
        steps={
            **state.steps,
            step.step_id: replace(step, generated=True),
        },
        tools=tools,
        messages=(*state.messages, message),
    )


def _apply_step_finished(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.step.finished``: replace/append the terminal message (§3.1 L144)."""
    if not isinstance(event.data, StepFinishedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected StepFinishedData"
        )
    step = state.steps.get(event.data.step_id)
    if step is None:
        raise IllegalTransitionError(f"event {event.id} references unknown step {event.data.step_id!r}")
    run = _require_run(state, event, step.run_id)
    if run.status is RunStatus.ACCEPTED or run.status.is_terminal:
        raise IllegalTransitionError(
            f"run {step.run_id} is {run.status.value}; step.finished needs running or cancelling (event {event.id})"
        )
    message = event.data.message
    if message.id != step.message_id:
        raise IllegalTransitionError(
            f"step {step.step_id} owns message {step.message_id!r}, not {message.id!r} (event {event.id})"
        )
    if message.status == "running":
        raise IllegalTransitionError(
            f"step {step.step_id} finished with a non-terminal message status (event {event.id})"
        )
    if step.status.is_terminal:
        # §2.1 L63: identical terminal replay is idempotent, anything else corrupts.
        for existing in state.messages:
            if existing.id == message.id:
                if existing == message:
                    return replace(state, applied_seq=event.seq)
                break
        raise IllegalTransitionError(f"step {step.step_id} is already terminal (event {event.id})")
    if step.generated:
        index = next((position for position, existing in enumerate(state.messages) if existing.id == message.id), None)
        if index is None:
            raise IllegalTransitionError(
                f"generated message {message.id!r} missing from the folded sequence (event {event.id})"
            )
        existing = state.messages[index]
        assert isinstance(existing, AssistantMessage)
        _require_finished_consistency(state, event, existing, message)
        messages = (*state.messages[:index], message, *state.messages[index + 1 :])
    else:
        # §3.3 L212: without a generated plan only the committed finalized
        # text may compose an interrupted/failed terminal message.
        _require_finalized_match(state, message)
        if any(isinstance(part, AssistantToolPart) for part in message.parts):
            raise IllegalTransitionError(
                f"step {step.step_id} has no generated plan; tool parts are impossible (event {event.id})"
            )
        if message.status not in {"failed", "interrupted"}:
            raise IllegalTransitionError(
                f"step {step.step_id} cannot complete without a generated plan (event {event.id})"
            )
        messages = (*state.messages, message)
    return replace(
        state,
        applied_seq=event.seq,
        steps={**state.steps, step.step_id: replace(step, status=StepStatus(message.status))},
        messages=messages,
    )


def _apply_tool_requested(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.tool.requested``: pending -> pending, only catalog/input info (§2.1 L44)."""
    if not isinstance(event.data, ToolRequestedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected ToolRequestedData"
        )
    tool = _tool_for_event(state, event)
    if tool.state is not ToolState.PENDING:
        raise IllegalTransitionError(
            f"execution {tool.execution_id} is {tool.state.value}; only a pending call can be requested "
            f"(event {event.id})"
        )
    if tool.message_id != event.data.message_id or tool.tool_call_id != event.data.tool_call_id:
        raise IllegalTransitionError(
            f"tool.requested identity does not match execution {tool.execution_id} (event {event.id})"
        )
    if event.data.input_hash != tool.input_hash:
        raise IllegalTransitionError(f"tool.requested input_hash mismatch for {tool.execution_id} (event {event.id})")
    return replace(state, applied_seq=event.seq)


def _apply_tool_started(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.tool.started``: pending -> running (approved and not cancelled, §2.1 L45)."""
    if not isinstance(event.data, ToolStartedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected ToolStartedData"
        )
    tool = _tool_for_event(state, event)
    if tool.state is not ToolState.PENDING:
        raise IllegalTransitionError(
            f"execution {tool.execution_id} is {tool.state.value}; only a pending call can start (event {event.id})"
        )
    if event.data.input_hash != tool.input_hash:
        raise IllegalTransitionError(f"tool.started input_hash mismatch for {tool.execution_id} (event {event.id})")
    return _with_tool(state, event, replace(tool, state=ToolState.RUNNING))


def _apply_tool_finished(state: SessionState, event: DurableEvent[Any]) -> SessionState:
    """``py.tool.finished``: the terminal half of the §2.1 L46-52 table."""
    if not isinstance(event.data, ToolFinishedData):
        raise IllegalTransitionError(
            f"event {event.id} carries {type(event.data).__name__} data, expected ToolFinishedData"
        )
    tool = _tool_for_event(state, event)
    data = event.data
    if tool.state.is_terminal:
        # §2.1 L53: terminals are irreversible; identical content is idempotent.
        if (
            tool.state.value == data.state
            and tool.effect_status.value == data.effect_status
            and list(tool.content) == data.content
            and tool.structured_output == data.structured_output
            and tool.error == data.error
        ):
            return replace(state, applied_seq=event.seq)
        raise IllegalTransitionError(f"execution {tool.execution_id} is already terminal (event {event.id})")
    target = ToolState(data.state)
    effect = EffectStatus(data.effect_status)
    if tool.state is ToolState.PENDING:
        # A pending call has no side effects yet: not_started only.
        if (
            target not in {ToolState.FAILED, ToolState.DENIED, ToolState.CANCELLED}
            or effect is not EffectStatus.NOT_STARTED
        ):
            raise IllegalTransitionError(
                f"execution {tool.execution_id} cannot finish {data.state}/{data.effect_status} from pending "
                f"(event {event.id})"
            )
    else:
        legal: dict[ToolState, set[EffectStatus]] = {
            ToolState.SUCCEEDED: {EffectStatus.COMPLETED},
            ToolState.FAILED: {EffectStatus.COMPLETED, EffectStatus.UNKNOWN},
            ToolState.CANCELLED: {EffectStatus.COMPLETED, EffectStatus.UNKNOWN},
            ToolState.UNKNOWN: {EffectStatus.UNKNOWN},
        }
        if effect not in legal[target]:
            raise IllegalTransitionError(
                f"execution {tool.execution_id} cannot finish {data.state}/{data.effect_status} from running "
                f"(event {event.id})"
            )
    return _with_tool(
        state,
        event,
        replace(
            tool,
            state=target,
            effect_status=effect,
            content=tuple(data.content),
            structured_output=data.structured_output,
            error=data.error,
        ),
    )


# -- shared helpers -----------------------------------------------------------


def _with_run(state: SessionState, event: DurableEvent[Any], run: RunInfo) -> SessionState:
    return replace(state, applied_seq=event.seq, runs={**state.runs, run.run_id: run})


def _with_step(state: SessionState, event: DurableEvent[Any], step: StepInfo) -> SessionState:
    return replace(state, applied_seq=event.seq, steps={**state.steps, step.step_id: step})


def _with_tool(state: SessionState, event: DurableEvent[Any], tool: ToolInfo) -> SessionState:
    return replace(state, applied_seq=event.seq, tools={**state.tools, tool.execution_id: tool})


def _step_for_message(state: SessionState, run_id: str, message_id: str) -> StepInfo:
    """The run's step that owns ``message_id`` (a message belongs to one step)."""
    for step in state.steps.values():
        if step.run_id == run_id and step.message_id == message_id:
            return step
    raise IllegalTransitionError(f"no step of run {run_id!r} owns message {message_id!r}")


def _tool_for_event(state: SessionState, event: DurableEvent[Any]) -> ToolInfo:
    """Fetch the execution named by a tool event and enforce run ownership."""
    execution_id: str
    if isinstance(event.data, ToolRequestedData):
        execution_id = event.data.execution_id
    elif isinstance(event.data, ToolStartedData | ToolFinishedData):
        execution_id = event.data.execution_id
    else:
        raise IllegalTransitionError(f"event {event.id} is not a tool event")
    tool = state.tools.get(execution_id)
    if tool is None:
        raise IllegalTransitionError(f"event {event.id} references unknown execution {execution_id!r}")
    run = _require_run(state, event, tool.run_id)
    if event.type in {PY_TOOL_REQUESTED, PY_TOOL_STARTED}:
        # §3.1 L125: cancel_requested blocks new tool starts.
        if run.status is not RunStatus.RUNNING:
            raise IllegalTransitionError(
                f"run {tool.run_id} is {run.status.value}; the tool cannot start or be requested (event {event.id})"
            )
    elif run.status.is_terminal or run.status is RunStatus.ACCEPTED:
        raise IllegalTransitionError(
            f"run {tool.run_id} is {run.status.value}; the tool cannot finish (event {event.id})"
        )
    return tool


def _text_triples(message: AssistantMessage) -> list[tuple[str, str, str]]:
    """Ordered (kind, part_id, text) triples of the text/reasoning parts."""
    triples: list[tuple[str, str, str]] = []
    for part in message.parts:
        if isinstance(part, AssistantTextPart):
            triples.append(("text", part.part_id, part.text))
        elif isinstance(part, AssistantReasoningPart):
            triples.append(("reasoning", part.part_id, part.text))
    return triples


def _require_finalized_match(state: SessionState, message: AssistantMessage) -> None:
    """The text/reasoning parts must be exactly the committed finalized parts."""
    finalized = sorted(
        (part for (message_id, _), part in state.finalized.items() if message_id == message.id),
        key=lambda part: part.ordinal,
    )
    expected = [(part.kind, part.part_id, part.text) for part in finalized]
    if _text_triples(message) != expected:
        raise IllegalTransitionError(
            f"message {message.id} parts do not match the committed finalized parts (event seq {state.applied_seq + 1})"
        )


def _is_subsequence(candidate: list[Any], source: list[Any]) -> bool:
    """True when every ``candidate`` item appears in ``source`` in order (containment)."""
    position = 0
    for item in candidate:
        while position < len(source) and source[position] != item:
            position += 1
        if position == len(source):
            return False
        position += 1
    return True


def _require_finished_consistency(
    state: SessionState, event: DurableEvent[Any], existing: AssistantMessage, message: AssistantMessage
) -> None:
    """§3.1 L144: only terminal facts may be added; folded content must be kept.

    ``model``/``created_at``/``metadata`` must not change; every kept
    text/reasoning part must be an identical in-order containment of the
    generated ones (a committed reasoning part may be dropped, §2.1 L72);
    the tool plan must be the same set of calls, each terminal and matching
    the folded execution facts, and the terminal message's tool content must
    be an in-order containment of the committed content (file references may
    be dropped, model-readable text must be kept).
    """
    if (
        message.model != existing.model
        or message.created_at != existing.created_at
        or message.metadata != existing.metadata
    ):
        raise IllegalTransitionError(
            f"step.finished message {message.id} changes non-terminal facts (event {event.id})"
        )
    if not _is_subsequence(_text_triples(message), _text_triples(existing)):
        raise IllegalTransitionError(
            f"step.finished message {message.id} text diverges from the folded parts (event {event.id})"
        )
    generated_tools = {part.tool_call_id: part for part in existing.parts if isinstance(part, AssistantToolPart)}
    finished_tools = {part.tool_call_id: part for part in message.parts if isinstance(part, AssistantToolPart)}
    if len(finished_tools) != len([part for part in message.parts if isinstance(part, AssistantToolPart)]):
        raise IllegalTransitionError(f"step.finished message {message.id} repeats a tool_call_id (event {event.id})")
    if set(generated_tools) != set(finished_tools):
        raise IllegalTransitionError(f"step.finished message {message.id} changes the tool plan (event {event.id})")
    for tool_call_id, finished_part in finished_tools.items():
        generated_part = generated_tools[tool_call_id]
        if (
            finished_part.execution_id != generated_part.execution_id
            or finished_part.name != generated_part.name
            or finished_part.input != generated_part.input
        ):
            raise IllegalTransitionError(
                f"step.finished tool {tool_call_id!r} changes the call plan (event {event.id})"
            )
        tool = state.tools.get(finished_part.execution_id)
        if tool is None or not tool.state.is_terminal:
            raise IllegalTransitionError(f"step.finished before tool {tool_call_id!r} terminated (event {event.id})")
        if (
            finished_part.state != tool.state.value
            or finished_part.effect_status != tool.effect_status.value
            or finished_part.structured_output != tool.structured_output
            or finished_part.error != tool.error
            or not _is_subsequence(list(finished_part.content), list(tool.content))
        ):
            raise IllegalTransitionError(
                f"step.finished tool {tool_call_id!r} diverges from the folded execution (event {event.id})"
            )
