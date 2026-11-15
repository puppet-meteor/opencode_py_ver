"""Builtin ``subagent__run`` tool (P6-S5; T-C1-098, ticket §3-4).

Foreground subagent runs, adapted from TS
``packages/core/src/tool/plugin/subagent.ts``: the model delegates a
self-contained task to a child session -- the adapter creates (or
resumes) the child, submits the prompt, WAITS IN THE FOREGROUND until
the child run settles, and returns the subagent's final assistant text
(ticket §1).

TS mapping / deviations, quantified (progress file):

- The TS plugin runs over a full agent directory: ``agents.resolve``
  with unknown-agent and ``primary``-mode rejections, per-agent model
  resolution (``Model.Ref.parse`` + chat-type/variant checks) and the
  three config-plugin auto-agents (explore/title/summary). PY has no
  agent directory and a single provider composition, so ``agent``
  converges to the single builtin agent ``"task"``
  (``SUBAGENT_AGENTS``), the model face is not ported (the child
  inherits the host provider through its inherited config snapshot,
  the fork precedent) and the primary-mode rejection arm is N/A.
  DIFFERENT-BY-DESIGN (progress file §5-1/§5-2/§5-3), pinned by
  tests.
- ``background: true`` is the T-C1-101 (P7-S2) face: the child runs on
  and the call returns immediately with the TS four-line guidance
  (``backgroundResult``); the service's background observer watches the
  durable run and delivers the completion into the parent's S1 inbox
  (the synthetic seam) exactly once per generation, restart included.
  The TS ``SubagentJob``/``Job.Service`` in-memory registry maps onto
  the durable ``py.subagent.backgrounded`` marker + the process-local
  observer arm on the service (the TS per-generation Set becomes the
  deterministic ``subagent:{run_id}`` notification id).
- The TS plugin's new-child+background ``resume:false`` prompt option has
  no PY counterpart: the PY ``prompt`` face admits and starts
  atomically and there is no suspension face to suppress, so the
  background child simply starts (N/A, recorded in the progress file).
- The parent link is the ticket §3-3 mechanism ①: the optional
  ``parent_id`` on the durable ``py.session.created`` payload. It
  carries NO lifecycle -- no delete cascade, no status coupling (the
  service.py no-cascade record stands verbatim); the child is a fully
  independent session that merely remembers its origin for the depth
  guard.
- The depth guard walks the durable parent chain
  (``SessionService.session_created_data``) against the
  ``SUBAGENT_MAX_DEPTH`` constant (default 1 = nesting forbidden, the
  TS ``experimental.subagent_depth`` default as a constant because PY
  has no experimental config face). DIFFERENT-BY-DESIGN (progress
  file §5-7).
- TS ``sessions.create({title: description, ...})`` names the child
  session; the PY ``create`` face has no title parameter, so the
  description stays a model-side label only (N/A, progress file).
- The TS completion delivery imports the final text into the parent
  session (``subagent-completion.deliver``); the FOREGROUND delivery
  keeps its S5 form (the tool result itself, no XML wrapper -- a
  recorded S5 deviation, progress file §5-5), while the BACKGROUND
  delivery is the T-C1-101 import (the S1 inbox synthetic seam; the XML
  wrapper + metadata live on that path).
- The TS tool's foreground ``content`` wraps the completed output in the
  XML envelope too; PY keeps the S5 raw-text content face for the
  foreground (zero back-change, ticket §3-8) and reserves the wrapper
  for the background notification.
- The TS ``permission.assert`` subagent special case is not ported:
  the PY policy face carries the ask default (ticket §3-6, progress
  file §5-6).
- The description text is seat-written (ticket §3-9): the TS
  subagent.ts description adapted to the single-agent form, renovated
  for the background face (T-C1-101).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import (
    JsonObject,
    JsonValue,
    ToolTextContent,
)
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session.service import (
    SUBAGENT_NO_TEXT_RESPONSE,
    RunStatusValue,
    SessionBusyError,
)
from opencode.storage import NotFoundError, StoreError
from opencode.tools.builtin.read import _failure
from opencode.tools.invoker import ToolInvokeError

if TYPE_CHECKING:
    # Annotation-only (the question.py house rule): the runtime instances
    # flow in through bind() (SessionService) and the Invoker (ToolContext).
    from opencode.session.service import SessionService
    from opencode.tools.invoker import ToolContext

SUBAGENT_NAMESPACE = "subagent"
SUBAGENT_NAME = "run"
SUBAGENT_VERSION = 1

#: The closed agent enumeration (ticket §3-2): the TS agent directory
#: converges to one builtin agent; an ``agent`` outside the set is
#: rejected (the TS unknown-agent rejection, same form).
SUBAGENT_AGENTS = frozenset({"task"})

#: The nesting limit (ticket §3-3): the TS ``experimental.subagent_depth``
#: default (1 -- nesting forbidden) as a constant; PY has no experimental
#: config face (the S4 constant-form ruling precedent).
SUBAGENT_MAX_DEPTH = 1

#: The total foreground-wait bound for one child run (ticket §3-4 ⑦): a run
#: that outlives it surfaces the ToolTimeout face and KEEPS RUNNING (the
#: orphan stays resumable by ``sessionID``; a prompt into it while active
#: answers SessionBusy).
SUBAGENT_TIMEOUT_S = 600.0

#: The ``run_status`` poll cadence during the foreground wait.
SUBAGENT_POLL_INTERVAL_S = 0.05

#: The cooperative-cancel cleanup bound: ``SessionService.cancel`` awaits
#: the child task's settle, which the cancel path must never do unbounded.
SUBAGENT_CANCEL_WAIT_S = 5.0

#: The TS subagent-completion.ts no-text sentence, verbatim (§3-4 ⑧). The
#: canonical constant lives on the service (the T-C1-101 delivery face's
#: three-state mapping shares it); this S5 name is the alias so the two
#: faces can never drift.
NO_TEXT_RESPONSE = SUBAGENT_NO_TEXT_RESPONSE

#: Seat-written description (ticket §3-9): the TS subagent.ts description
#: adapted to the single-agent form, renovated for the background face
#: (T-C1-101 -- the TS "Background mode" lines restored in PY form).
SUBAGENT_DESCRIPTION = (
    "Run a subagent: a fresh agent session that executes a self-contained task and returns\n"
    "its final answer. This allows you to:\n"
    "1. Delegate a well-scoped subtask while keeping this conversation focused\n"
    "2. Give long or noisy work its own isolated session history\n"
    "3. Resume a previous subagent of this session with follow-up instructions\n"
    "\n"
    "Usage notes:\n"
    '- `agent` selects the subagent kind; the available agent is "task"\n'
    "- `description` is a short (3-5 word) label of what the subagent does\n"
    "- `prompt` must be fully self-contained: the subagent starts with no prior\n"
    "  context and cannot ask you questions, so include every detail it needs\n"
    "- Foreground (the default) waits for the subagent to settle and returns its\n"
    "  final text\n"
    "- Background mode (`background=true`) launches it asynchronously and returns\n"
    "  immediately; you are notified when it finishes\n"
    "- Use background only for independent work that can run while you continue\n"
    "  elsewhere\n"
    "- Pass `sessionID` to continue a subagent this session created earlier\n"
    "- The subagent inherits this session's provider and configuration and\n"
    "  cannot spawn further subagents itself"
)

SUBAGENT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Run a subagent: create (or resume) a child session and submit the prompt. The "
        "foreground form waits for the child run to settle and returns its final text; "
        "background=true returns immediately and delivers a completion notification."
    ),
    "properties": {
        "agent": {
            "type": "string",
            "description": 'The subagent kind to run; the only available agent is "task".',
        },
        "description": {
            "type": "string",
            "description": "A short 3-5 word label of what the subagent does.",
        },
        "prompt": {
            "type": "string",
            "description": (
                "The fully self-contained instructions for the subagent (it sees no prior "
                "conversation and cannot ask questions)."
            ),
        },
        "sessionID": {
            "type": "string",
            "description": (
                "Optional child session id to resume; it must be a subagent created by the current session."
            ),
        },
        "background": {
            "type": "boolean",
            "description": (
                "Run the subagent in the background and return immediately. You will be notified "
                "when it completes. DO NOT sleep, poll, or proactively check on its progress."
            ),
        },
    },
    "required": ["agent", "description", "prompt"],
    "additionalProperties": False,
}

SUBAGENT_SPEC = ToolSpec(
    namespace=SUBAGENT_NAMESPACE,
    name=SUBAGENT_NAME,
    version=SUBAGENT_VERSION,
    description=SUBAGENT_DESCRIPTION,
    input_schema=SUBAGENT_INPUT_SCHEMA,
    supported_content_types=["text"],
    # Spawning a session means provider spend and durable writes (ticket
    # §3-5): a real side effect, unlike the S4 echo pair.
    side_effects=True,
    # Two runs of the same input create two children (fresh ses_ ids); only
    # an explicit sessionID resume is convergent, so idempotency is honestly
    # off (the Spec pin, ticket §3-5).
    idempotency_support=False,
)


def _timeout_failure(message: str) -> ToolResult:
    """A bounded foreground wait exhausted its deadline (§3-4 ⑦).

    Retryable like the bash/pty timeout faces: the child run keeps going,
    so a later invocation can still collect it by ``sessionID``.
    """
    return ToolResult(
        content=[ToolTextContent(type="text", text=message)],
        error=CoreError(code=CoreErrorCode.TOOL_TIMEOUT, message=message, retryable=True),
    )


async def _cancel_child_best_effort(service: SessionService, run_id: str) -> None:
    """Bounded, best-effort cooperative cancel of the child run (never raises).

    ``SessionService.cancel`` awaits the child task's settle, which the
    cancel path must never do unbounded: the wait is capped and every
    failure (deadline, store error) is swallowed -- an orphaned child
    keeps its ``cancelling`` state for the R4 recovery coordinator.
    """
    try:
        await asyncio.wait_for(service.cancel(run_id), timeout=SUBAGENT_CANCEL_WAIT_S)
    except (TimeoutError, StoreError):
        pass


def _background_result(child_id: str) -> ToolResult:
    """The TS ``backgroundResult`` (T-C1-101 §3-1): running + four-line guidance.

    The Output face carries ``{sessionID, status: "running", output}`` with
    the TS four-line text VERBATIM (subagent.ts L19-28, joined by newlines),
    structured/metadata/content in the S5 same-carried form.
    """
    output = "\n".join(
        (
            f"The subagent is working in the background (sessionID: {child_id}). "
            "You will be notified automatically when it finishes.",
            "DO NOT sleep, poll for progress, ask the subagent for status, or duplicate this "
            "subagent's work; avoid working with the same files or topics it is using.",
            "Work on non-overlapping tasks, or briefly tell the user what you launched and end your response.",
            "To send it follow-ups or change its instructions while it runs, call subagent again with this sessionID.",
        )
    )
    payload: dict[str, JsonValue] = {"sessionID": child_id, "status": "running", "output": output}
    return ToolResult(
        content=[ToolTextContent(type="text", text=output)],
        structured_output=payload,
        metadata=payload,
    )


async def _run_subagent(service: SessionService, context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against SUBAGENT_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the re-checks below are
    # the fail-closed double face (ticket §8), the read.py precedent.
    #
    # -- §3-4 ①: the parent session must exist on the resource face
    #    (missing or tombstoned -> the same NotFound, fail-closed).
    try:
        await service.get_session(context.session_id)
    except NotFoundError:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Parent session {context.session_id} does not exist",
        )
    # -- §3-4 ②: the depth guard over the durable parent chain. The walk
    #    reads the typed created payload (mechanism ①), crossing tombstoned
    #    ancestors (created rows survive the delete); a missing created row
    #    over a live session is read-model corruption -> StorageCorrupt
    #    (infrastructure, §7 L341), never a model-facing result. The loop is
    #    bounded by the reject-on-limit check, so a corrupt cycle cannot
    #    spin it.
    parent_created = await service.session_created_data(context.session_id)
    depth = 0
    ancestor = parent_created
    while ancestor.parent_id is not None:
        depth += 1
        if depth >= SUBAGENT_MAX_DEPTH:
            return _failure(
                CoreErrorCode.VALIDATION_ERROR,
                f"Subagent nesting exceeds the maximum depth of {SUBAGENT_MAX_DEPTH}: "
                "the current session is already a subagent",
            )
        ancestor = await service.session_created_data(ancestor.parent_id)
    # -- §3-4 ③: the agent check (the single builtin agent; the TS
    #    unknown-agent rejection, primary-mode arm N/A).
    agent = input.get("agent")
    if not isinstance(agent, str) or agent not in SUBAGENT_AGENTS:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Unknown agent {agent!r}: available agents are {', '.join(sorted(SUBAGENT_AGENTS))}",
        )
    # -- §3-4 ③ (T-C1-101): the description double face -- the durable
    #    marker and every notification carry it verbatim (the TS
    #    ``recovery.description``), so a non-string fails closed here.
    description = input.get("description")
    if not isinstance(description, str):
        return _failure(CoreErrorCode.VALIDATION_ERROR, "description is required and must be a string")
    prompt = input.get("prompt")
    if not isinstance(prompt, str):
        return _failure(CoreErrorCode.VALIDATION_ERROR, "prompt is required and must be a string")
    # -- §3-4 ④: the resume face (TS same form) -- an existing child of
    #    THIS session only. A resume with a different agent is N/A (the
    #    single-agent set makes same-agent trivially true, ticket §3-4).
    resume_id = input.get("sessionID")
    if resume_id is not None:
        if not isinstance(resume_id, str) or not resume_id.startswith("ses_"):
            return _failure(
                CoreErrorCode.VALIDATION_ERROR,
                "sessionID must be a child session id matching ^ses_",
            )
        try:
            await service.get_session(resume_id)
        except NotFoundError:
            return _failure(
                CoreErrorCode.VALIDATION_ERROR,
                f"Session {resume_id} does not exist",
            )
        resume_created = await service.session_created_data(resume_id)
        if resume_created.parent_id != context.session_id:
            return _failure(
                CoreErrorCode.VALIDATION_ERROR,
                f"Session {resume_id} is not a child of the current session",
            )
        child_id = resume_id
    else:
        # -- §3-4 ⑤: create the child -- the parent's config snapshot
        #    verbatim (the fork precedent) plus the §3-3 ① parent link.
        #    The TS ``title=description`` face is N/A: the PY create face
        #    has no title parameter (DIFFERENT-BY-DESIGN, progress file).
        child = await service.create(parent_created.config_snapshot, parent_id=context.session_id)
        child_id = child.session_id
    # -- §3-4 ⑥: submit the prompt. The request id is unique per
    #    invocation, so a resumed child never collides with its earlier
    #    accepted prompts; a retried identical call re-dedupes to the
    #    original RunRef.
    request_id = f"req_subagent_{context.message_id}_{context.tool_call_id}"
    try:
        run = await service.prompt(child_id, prompt, request_id)
    except SessionBusyError:
        return _failure(
            CoreErrorCode.SESSION_BUSY,
            f"Session {child_id} already holds an active run; let it settle before resuming the subagent",
        )
    # -- §3-4 ⑥ (T-C1-101) the background arm: the durable per-generation
    #    marker commits to the PARENT's log and the observer arms on the
    #    service (TS: recovery -> subagents.start -> background -> notify),
    #    then the call returns immediately with the TS backgroundResult.
    #    The child keeps running -- its durable run IS the background work.
    if input.get("background") is True:
        await service.commit_subagent_background(
            context.session_id,
            child_session_id=child_id,
            run_id=run.run_id,
            agent=agent,
            description=description,
        )
        return _background_result(child_id)
    # -- §3-4 ⑦: the foreground wait. Poll the durable run_status (never an
    #    in-memory task) to a terminal status, throttled, bounded by
    #    SUBAGENT_TIMEOUT_S. Cancellation is cooperative and a started call
    #    keeps its determined result, so the cancel arm only fires while
    #    the run is still non-terminal.
    #
    #    T-C1-101 (§3-4) adds the BLOCK CONVERSION (the TS
    #    ``jobs.block({id, sessionID: parent})`` PY mapping, poll-based --
    #    the recorded seat form): each cycle also checks (a) the
    #    session.background endpoint's armed request (the TS
    #    ``backgrounded`` deferred) and (b) growth of the parent's pending
    #    inbox (a new steer/queue arriving behind the blocked run -- the
    #    seat-ruled PY trigger); either converts the wait: the same
    #    marker+observer handoff as the background arm, then the same
    #    backgroundResult. The registry's per-session serialization keeps
    #    one live wait per parent, so the begin/end bookkeeping is a
    #    single-slot record.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SUBAGENT_TIMEOUT_S
    baseline = len(await service.inbox_list(context.session_id))
    service.subagent_foreground_wait_begin(context.session_id, run.run_id)
    try:
        status = await service.run_status(run.run_id)
        while not status.status.is_terminal:
            if context.cancel.is_set():
                await _cancel_child_best_effort(service, run.run_id)
                return _failure(
                    CoreErrorCode.TOOL_FAILED,
                    f"Subagent run {run.run_id} (session {child_id}) was cancelled before it settled",
                )
            if loop.time() >= deadline:
                return _timeout_failure(
                    f"Subagent run {run.run_id} (session {child_id}) did not settle within "
                    f"{SUBAGENT_TIMEOUT_S:g} seconds; the run keeps going -- resume it with "
                    "subagent__run once it settles"
                )
            if (
                service.consume_subagent_background_request(context.session_id)
                or len(await service.inbox_list(context.session_id)) > baseline
            ):
                await service.commit_subagent_background(
                    context.session_id,
                    child_session_id=child_id,
                    run_id=run.run_id,
                    agent=agent,
                    description=description,
                )
                return _background_result(child_id)
            await asyncio.sleep(SUBAGENT_POLL_INTERVAL_S)
            status = await service.run_status(run.run_id)
        # -- §3-4 ⑦ terminal: a failed/interrupted child is fail-closed (never
        #    a silent completed; ticket §8).
        if status.status is not RunStatusValue.SUCCEEDED:
            detail = f" ({status.error})" if status.error else ""
            return _failure(
                CoreErrorCode.TOOL_FAILED,
                f"Subagent run {run.run_id} (session {child_id}) ended in status {status.status.value}{detail}",
            )
        # -- §3-4 ⑧: the final assistant text (the shared service
        #    extraction; foreground semantics byte-identical to S5).
        output = await service.subagent_final_text(child_id)
        # -- §3-4 ⑨: the Output face -- structured_output + metadata (the TS
        #    Output object) plus the same text as the model-visible content.
        payload: dict[str, JsonValue] = {"sessionID": child_id, "status": "completed", "output": output}
        return ToolResult(
            content=[ToolTextContent(type="text", text=output)],
            structured_output=payload,
            metadata=payload,
        )
    finally:
        # Balanced with the begin above: a cancelled wait (the cooperative
        # cancel arm or a hard task cancel) never leaves a stale
        # conversion target behind.
        service.subagent_foreground_wait_end(context.session_id)


class SubagentRunAdapter:
    """The ``subagent__run`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    Unlike the stateless S4 trio this adapter is stateful in exactly one
    field: the host-bound ``SessionService``. The registry owns adapters
    process-wide while services are per-App, so the service cannot ride
    the constructor -- each host composition (the CLI ``_drive`` tuple
    and the serving ``_builtin_adapters`` twin) keeps a named instance
    in its tool list and calls :meth:`bind` right after ``App(...)``
    construction. An unbound adapter is a host wiring defect,
    fail-closed as :class:`ToolInvokeError` (§7 L341: wiring errors are
    infrastructure failures, not model-facing results). No ``roots``
    constructor argument: the tool touches no files.
    """

    def __init__(self) -> None:
        self._service: SessionService | None = None

    def bind(self, service: SessionService) -> None:
        """Bind the App's session service (each host calls this once, after construction)."""
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return SUBAGENT_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        service = self._service
        if service is None:
            raise ToolInvokeError(
                "subagent__run is not bound to a session service "
                "(host wiring defect: call bind(app.service) after App construction)"
            )
        return await _run_subagent(service, context, input)
