"""Native workflow tools (P3-S4, T-C1-079; PHASE3 §2 main brain, §7 M-5).

The two-phase submit/poll pair the main brain drives workflows through
(PHASE3 §7 M-5: no orphan tool calls -- the tool returns when the gateway
has committed, never when the job has finished):

* ``workflow__run`` -- submit. The gateway face runs in order: the shared
  params validator (zero-model strict JSON-Schema validation against the
  pipeline's declared ``params_schema``, PHASE3 §1 condition 4 -- the same
  validator instance the future S5b router will consume), then the M-9
  lineage/depth recursion guard, then the A fork-3 commit
  (``py.job.created`` + ``py.job.spec_bound`` atomically, before any
  execution), then the executor drive is *spawned* as a tracked task. The
  tool returns the ``job_id`` immediately -- non-blocking by contract.
  The submission's lineage is derived from the call context: origin
  ``session``, the brain session as both anchor and initiating session, no
  parent job (a subworkflow parent is the S5a ``subworkflow`` node's face,
  not this tool's).
* ``workflow__status`` -- poll. One read of the job's read-model projection
  (status/result/error) as a structured snapshot plus model-readable text.
  Job events never pass through the brain (PHASE3 §4 two-channel
  discipline: events ride the control channel straight to the frontend; the
  brain reads this projection only -- informed, never a relay).

Both adapters map the submit face's structured 4xx rejections
(:class:`~opencode.storage.txn.StoreError`, each carrying its §8
:class:`~opencode.contracts.errors.CoreError`) onto
``ToolResult(error=...)`` so the model sees the same structured code/message
the HTTP face would return, instead of an exception. Registration isolation
(C#5): these adapters are injected only into the main brain's
:class:`~opencode.tools.registry.ToolRegistry` (the App's single registry,
``workflow_tools=True``); worker catalogs built from the builtin set never
see them. The module is deliberately NOT re-exported from
``opencode.tools.__init__`` (the builtin precedent: importing it here would
make every ``opencode.tools`` import pull the orchestrator).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from opencode.contracts import canonical_json
from opencode.contracts.events import JobLineage
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.orchestrator.jobs import JobService
from opencode.storage.txn import StoreError

if TYPE_CHECKING:
    # Annotation-only (the ls.py precedent): importing the runtime package
    # here would create an import cycle through the registry.
    from opencode.tools.invoker import ToolContext

WORKFLOW_NAMESPACE = "workflow"
RUN_NAME = "run"
STATUS_NAME = "status"
WORKFLOW_VERSION = 1

RUN_DESCRIPTION = (
    "Submit one registered pipeline as a background job and return its job id "
    "immediately (non-blocking; the job runs after the tool returns). Params are "
    "strictly validated against the pipeline's declared params schema before the "
    "job is created. Poll workflow__status with the returned job_id for the outcome."
)

RUN_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Submit one registered pipeline as a job (two-phase submit: returns the job id immediately).",
    "properties": {
        "pipeline_id": {
            "type": "string",
            "description": "The registered pipeline id (the graph.json pipeline_id).",
        },
        "params": {
            "type": "object",
            "description": (
                "The pipeline params object, strictly validated against the pipeline's "
                "declared params schema (missing fields, type errors and unknown fields are rejected)."
            ),
        },
    },
    "required": ["pipeline_id", "params"],
    "additionalProperties": False,
}

RUN_SPEC = ToolSpec(
    namespace=WORKFLOW_NAMESPACE,
    name=RUN_NAME,
    version=WORKFLOW_VERSION,
    description=RUN_DESCRIPTION,
    input_schema=RUN_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    idempotency_support=False,
)

STATUS_DESCRIPTION = (
    "Read one job's current projection: status, and the result or error once "
    "terminal. A pure read (no side effects); poll as needed."
)

STATUS_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Read one job's current projection (the poll face of the two-phase submit).",
    "properties": {
        "job_id": {
            "type": "string",
            "description": "The job id returned by workflow__run.",
        },
    },
    "required": ["job_id"],
    "additionalProperties": False,
}

STATUS_SPEC = ToolSpec(
    namespace=WORKFLOW_NAMESPACE,
    name=STATUS_NAME,
    version=WORKFLOW_VERSION,
    description=STATUS_DESCRIPTION,
    input_schema=STATUS_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


def _error_result(error: StoreError) -> ToolResult:
    """A submit-face rejection as a model-readable structured terminal.

    The StoreError family already carries the §8 CoreError the HTTP face
    maps to 4xx; the tool terminal carries the same code/message so the
    model sees exactly what an API caller would see.
    """
    return ToolResult(content=[ToolTextContent(type="text", text=error.core.message)], error=error.core)


class WorkflowRunAdapter:
    """The ``workflow__run`` builtin: submit (validate + A fork-3) + spawn + job id.

    Bound to the composition's :class:`~opencode.orchestrator.jobs.JobService`
    (the App wires it with the params-validator-injected instance). The
    Invoker strictly validated the input against ``RUN_INPUT_SCHEMA`` before
    ``execute`` (adapter contract, §5.1 L257); the casts below are static
    narrows of that invariant.
    """

    def __init__(self, *, jobs: JobService) -> None:
        self._jobs = jobs

    @property
    def spec(self) -> ToolSpec:
        return RUN_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        pipeline_id = cast("str", input["pipeline_id"])
        params = cast("JsonObject", input["params"])
        lineage = JobLineage(parent_job_id=None, session_id=context.session_id, origin="session")
        try:
            handle = await self._jobs.submit(pipeline_id, params, context.session_id, lineage=lineage)
        except StoreError as error:
            return _error_result(error)
        # A fork-3 order: submit has committed created + spec_bound; only now
        # is the executor drive scheduled (as a tracked background task, so
        # this return is the non-blocking submit half of M-5).
        self._jobs.spawn(handle.job_id)
        return ToolResult(
            content=[
                ToolTextContent(
                    type="text",
                    text=(
                        f"Submitted job {handle.job_id} for pipeline {pipeline_id} "
                        f"(status: created; poll workflow__status with this job_id)."
                    ),
                )
            ],
            structured_output={"job_id": handle.job_id, "pipeline_id": pipeline_id, "status": "created"},
        )


class WorkflowStatusAdapter:
    """The ``workflow__status`` builtin: one job projection read (the poll half)."""

    def __init__(self, *, jobs: JobService) -> None:
        self._jobs = jobs

    @property
    def spec(self) -> ToolSpec:
        return STATUS_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        job_id = cast("str", input["job_id"])
        try:
            record = await self._jobs.get_job(job_id)
        except StoreError as error:
            return _error_result(error)
        structured: JsonObject = {
            "job_id": record.job_id,
            "pipeline_id": record.pipeline_id,
            "status": record.status,
            "depth": record.depth,
        }
        result: JsonValue | None = record.result
        if result is not None:
            structured["result"] = result
        if record.error is not None:
            structured["error"] = record.error
        lines = [f"job {record.job_id} (pipeline {record.pipeline_id}): status={record.status}"]
        if result is not None:
            lines.append(f"result: {canonical_json(result)}")
        elif record.error is not None:
            lines.append(f"error: {canonical_json(record.error)}")
        return ToolResult(
            content=[ToolTextContent(type="text", text="\n".join(lines))],
            structured_output=structured,
        )
