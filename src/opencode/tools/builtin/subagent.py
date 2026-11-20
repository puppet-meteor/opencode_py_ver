"""Builtin ``subagent__run`` tool (T-C3-005: the always-background face).

Always-background subagent runs, adapted from the upstream 2.3.2
``packages/core/src/tool/plugin/subagent.ts`` semantics flip (diffed
against 2.2.2): the model delegates a self-contained task to a child
session -- the adapter creates (or resumes) the child, submits the
prompt, and RETURNS IMMEDIATELY with the running face; every settled
generation delivers its own ``<subagent>`` receipt into the parent's
inbox. The v1 foreground wait chain (poll to terminal, ToolTimeout,
cooperative cancel, block conversion) is deleted with the upstream.

TS mapping / deviations, quantified (progress file + plan T-C3-005):

- The TS plugin runs over a full agent directory: ``agents.resolve``
  with unknown-agent and ``primary``-mode rejections. PY has no agent
  directory and a single provider composition, so ``agent`` converges to
  the single builtin agent ``"task"`` (``SUBAGENT_AGENTS``); the model
  face is not ported (the child inherits the host provider through its
  inherited config snapshot, the fork precedent) and the primary-mode
  rejection arm is N/A. DIFFERENT-BY-DESIGN, pinned by tests.
- Spec v2: the ``background`` input key is GONE (upstream 2.3.2 drops
  it), the Output face carries ``status: "running"`` only, and the
  four-line guidance names the per-round receipt contract verbatim
  ("when it goes idle, and again after each later round of work").
- The ALWAYS-background handoff is the T-C1-101 marker+observer
  mechanism, now unconditional and per-round: the durable
  ``py.subagent.backgrounded`` marker commits per generation, the
  process-local observer arms over it (keyed by the deterministic
  ``subagent:{run_id}`` notification id), and a follow-up round hands a
  NEW generation to the observer -- one receipt per settled run,
  restart included.
- Follow-ups steer (the upstream "a follow-up to a running child joins
  its current run"): the prompt rides ``delivery="steer"`` -- an idle
  child admits-and-starts, a running child parks in the current run's
  tail. The v1 SessionBusy rejection on a mid-run follow-up is gone
  (the upstream flip); the TS ``resume:false`` option still has no PY
  counterpart (T-C1-101 recorded: no suspension face to suppress).
- The parent link is the durable ``parent_id`` on the
  ``py.session.created`` payload (no lifecycle coupling); the depth
  guard walks that chain against ``SUBAGENT_MAX_DEPTH`` (default 1, the
  TS ``experimental.subagent_depth`` default as a constant -- the S4
  ruling, the TS config face stays unported).
- TS ``sessions.create({title: description, ...})`` names the child
  session; the PY ``create`` face has no title parameter (N/A,
  progress file).
- The delivery face (wrapper, metadata, outstanding keep-alive tail)
  lives on the service (``_deliver_subagent_completion``) and is shared
  with the restart replay construction.
- The TS ``permission.assert`` subagent special case is not ported:
  the PY policy face carries the ask default (ticket §3-6, progress
  file §5-6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import (
    JsonObject,
    JsonValue,
    ToolTextContent,
)
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session.service import (
    SUBAGENT_NO_TEXT_RESPONSE,
    RunRef,
)
from opencode.storage import NotFoundError
from opencode.tools.builtin.read import _failure
from opencode.tools.invoker import ToolInvokeError

if TYPE_CHECKING:
    # Annotation-only (the question.py house rule): the runtime instances
    # flow in through bind() (SessionService) and the Invoker (ToolContext).
    from opencode.session.service import SessionService
    from opencode.tools.invoker import ToolContext

SUBAGENT_NAMESPACE = "subagent"
SUBAGENT_NAME = "run"
#: v2 (T-C3-005): the always-background face -- the ``background`` input key
#: is GONE and every call returns immediately with the running output (the
#: upstream 2.3.2 semantics flip; the v1 foreground wait chain is deleted).
SUBAGENT_VERSION = 2

#: The closed agent enumeration (ticket §3-2): the TS agent directory
#: converges to one builtin agent; an ``agent`` outside the set is
#: rejected (the TS unknown-agent rejection, same form).
SUBAGENT_AGENTS = frozenset({"task"})

#: The nesting limit (ticket §3-3): the TS ``experimental.subagent_depth``
#: default (1 -- nesting forbidden) as a constant; PY has no experimental
#: config face (the S4 constant-form ruling precedent).
SUBAGENT_MAX_DEPTH = 1

#: The TS subagent-completion.ts no-text sentence, verbatim (§3-4 ⑧). The
#: canonical constant lives on the service (the T-C1-101 delivery face's
#: three-state mapping shares it); this S5 name is the alias so the two
#: faces can never drift.
NO_TEXT_RESPONSE = SUBAGENT_NO_TEXT_RESPONSE

#: Seat-written description (T-C3-005): the upstream 2.3.2 always-background
#: form -- subagents run in the background unconditionally and the parent
#: receives a ``<subagent>`` receipt each time the child goes idle.
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
    "- Subagents always run in the background: the call returns immediately with\n"
    "  a sessionID, and you receive a `<subagent>` message each time the child\n"
    "  goes idle, including after later work\n"
    "- Keep working on non-overlapping tasks, or briefly tell the user what you\n"
    "  launched and end your response. Do not sleep, poll, or proactively check\n"
    "  on the child's progress\n"
    "- Pass the returned `sessionID` to continue that specific child\n"
    "  conversation. A follow-up to a running child joins its current run\n"
    "- The subagent inherits this session's provider and configuration and\n"
    "  cannot spawn further subagents itself"
)

SUBAGENT_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Run a subagent: create (or resume) a child session and submit the prompt. Subagents "
        "always run in the background: the call returns immediately with a sessionID, and you "
        "receive a `<subagent>` message each time the child goes idle, including after later "
        "work."
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
            "description": ("Send a follow-up to an existing subagent, running or finished. Omit to start a new one."),
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


def _background_result(child_id: str) -> ToolResult:
    """The TS ``backgroundResult`` (2.3.2): running + the per-round guidance.

    The Output face carries ``{sessionID, status: "running", output}`` with
    the upstream 2.3.2 four-line text VERBATIM (the idle/each-later-round
    receipt contract), structured/metadata/content in the S5 same-carried
    form. Every spawn answers this face now -- the call returns while the
    child runs, unconditionally (the v1 opt-in arm is gone).
    """
    output = "\n".join(
        (
            f"The subagent is working in the background (sessionID: {child_id}). "
            "You will receive a <subagent> message when it goes idle, and again after each later round of work.",
            "DO NOT sleep, poll, proactively check on its progress, or duplicate this subagent's work.",
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
    # -- §3-4 ⑥ (T-C3-005): submit the prompt through the steer admission.
    #    The request id is unique per invocation, so a resumed child never
    #    collides with its earlier accepted prompts; a retried identical
    #    call re-dedupes to the original RunRef. ``delivery="steer"`` is the
    #    upstream 2.3.2 "a follow-up to a running child joins its current
    #    run" face (T-C1-100): an idle child -- new spawn or settled
    #    follow-up -- admits-and-starts atomically, a running child parks in
    #    the current run's tail. The busy rejection is structural now: the
    #    v1 SessionBusy face on a mid-run follow-up is gone (the TS flip).
    #    The TS ``resume:false`` option has no PY counterpart (T-C1-101
    #    recorded): PY has no suspension face to suppress, the child starts.
    request_id = f"req_subagent_{context.message_id}_{context.tool_call_id}"
    run = await service.prompt(child_id, prompt, request_id, delivery="steer")
    # -- §3-4 ⑥ (T-C3-005) the ALWAYS-background handoff: the durable
    #    per-generation marker commits to the PARENT's log and the observer
    #    arms on the service (TS 2.3.2: recovery -> subagents.start, one
    #    job per generation, background: true unconditionally), then the
    #    call returns immediately with the running face. The child keeps
    #    running -- its durable run IS the background work; each settled
    #    generation delivers its own receipt under subagent:{run_id}.
    #    ONLY a started generation arms one (idle child: admit-and-start,
    #    a RunRef): a follow-up that PARKED behind the child's current run
    #    (an InboxRef) arms nothing -- that run's existing observer owns
    #    the single receipt for the whole generation (the TS "job's resume
    #    owns the start" face).
    if isinstance(run, RunRef):
        await service.commit_subagent_background(
            context.session_id,
            child_session_id=child_id,
            run_id=run.run_id,
            agent=agent,
            description=description,
        )
    return _background_result(child_id)


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
