"""The P3-S2 LangGraph engine minimal loop (PHASE3 §11 S2, T-C1-077; S3 agent wiring T-C1-078).

``PipelineEngine`` turns a registered pipeline spec into a real compiled
LangGraph state machine and drives jobs through it as the jobs domain's
``JobExecutor`` (``started`` -> graph execution -> ``finished``), with the
official sqlite checkpointer as the *advisory cursor* store.

The three-layer semantic gate (PHASE3 §9; the agent_dsl_flow_framework
registration pipeline re-anchored for the gateway):

* **structure** -- ``PipelineSpec.model_validate`` (the pydantic wire contract
  in ``opencode.contracts.pipelines``): unknown fields, unknown node types,
  missing structural fields, bool-for-int coercion are rejected here;
* **semantic** -- :func:`validate_pipeline_spec`: node-id uniqueness,
  edge-reference closure, unique entry (exists, no incoming edges), no orphan
  nodes, per-type constraints (``llm_call`` carries a model tier + prompt
  template; ``tool`` carries exactly one of command / static tool ref; the
  subworkflow nesting depth cap), and the node-type execution face: the
  closed set's other names are recognized-but-unimplemented -- rejected with
  an explicit error naming the S3/S5a delivery (the default face stays
  byte-identical to the S2 message; a worker-wired engine names its executed
  set instead);
* **rehearsal** -- ``_compile``: the graph is actually compiled (a dry run
  with no side effects; the injected executors are never invoked).

Rejections raise :class:`PipelineSpecRejected`, a structured error carrying
the CORE-CONTRACTS §8 ``CoreError`` code face (``ValidationError`` for spec
shape/topology, ``UnsupportedCapability`` for the unimplemented node types);
no new ``CoreErrorCode`` value was added for this slice (T-C1-077 decision).

Executor injection (PHASE3 §3 discipline, the S1 fake-executor precedent):
``llm_call`` and ``tool`` bodies are injectable async callables, and since S3
so is ``opencode_agent`` (:class:`~opencode.orchestrator.workers.WorkerManager`
-- a real CLI subprocess in a per-node worktree). The default constructors take
no bodies, so tests inject fakes (honestly labeled); the real LLM-tier wiring
lands with S4.

The dual-database boundary (PHASE3 §14 / M-7, CC §5.4): the ``jobs`` table is
the single source of truth for job status and is written only through
``JobService``'s ``py.job.*`` commits; the checkpointer lives in its own
sqlite file and stores only the graph cursor/graph state. The engine never
derives job status from the checkpointer -- it reads job facts through the
service (the ``JobHandle`` face) and answers recovery questions
(:meth:`PipelineEngine.read`) with the advisory cursor facts alone.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, TypedDict, cast

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import JobBudget
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import (
    MAX_SUBWORKFLOW_DEPTH,
    PipelineNode,
    PipelineSpec,
    compute_spec_hash,
)
from opencode.orchestrator.jobs import (
    TERMINAL_STATUSES,
    JobCheckpoint,
    JobHandle,
    JobService,
    JobTransitionError,
)
from opencode.orchestrator.workers import (
    AGENT_JOB_CONTEXT,
    AgentJobContext,
    WorkerError,
    WorkerSuspended,
)

#: The node types this engine executes when no worker manager is wired (the
#: S2 face, the default constructor). A worker-wired engine registers its
#: ``opencode_agent`` executor into this set at ``register`` time.
DEFAULT_IMPLEMENTED: frozenset[str] = frozenset({"llm_call", "tool"})


class PipelineSpecRejected(ValueError):
    """A structured semantic-gate rejection (CORE-CONTRACTS §8 shape).

    ``code`` reuses the closed ``CoreErrorCode`` registry (``ValidationError``
    for spec/topology/field constraints, ``UnsupportedCapability`` for the
    recognized-but-unimplemented node types); ``details`` is the desensitized
    diagnostic object the gateway can turn into a 4xx for the brain to fix.
    """

    def __init__(self, message: str, *, code: CoreErrorCode, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details: JsonObject = details if details is not None else {}

    def to_core_error(self) -> CoreError:
        """The §8 wire error for this rejection (retryable: no -- fix the spec)."""
        return CoreError(code=self.code, message=str(self), retryable=False, details=self.details)


class EnginePause(RuntimeError):
    """A stop-the-drive signal that leaves the job non-terminal.

    The drive returns with the job still ``running`` and the advisory cursor
    holding the graph position -- the exact state a gateway restart would see.
    S2's integration tests use it (through an injected fake node body) to
    simulate mid-graph interruption honestly; it is also the future graceful
    suspend seam. Distinct from a node *failure*, which fails the job.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NodeFailure(Exception):
    """An injected node body raised; carries the node for the §8 error mapping.

    The engine's node wrapper raises this so the drive handler can attribute
    the failure to the node and map the §8 code by node type
    (``ProviderUnavailable`` for ``llm_call``, ``ToolFailed`` for ``tool``);
    unexpected failures outside node bodies (graph machinery) propagate
    uncaught rather than being mislabeled with a node code.
    """

    def __init__(self, node: PipelineNode, cause: BaseException) -> None:
        super().__init__(f"node '{node.node_id}' ({node.node_type}) failed: {cause}")
        self.node = node


def _merge_node_results(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The node-results channel reducer: accumulating merge (LastValue loses updates)."""
    return {**left, **right}


class EngineState(TypedDict, total=False):
    """The graph state channels (the checkpointer's persisted graph state).

    ``params``/``params_hash`` bind the job's submitted params (the spec_bound
    chain's hash), ``pipeline_spec_hash`` binds the loaded pipeline spec (the
    §5.1 hash lock; a mismatch on resume means the spec was re-registered and
    the graph must not silently rebind), ``node_results`` accumulates one
    result per executed node, and ``outcome``/``result`` are written only by
    terminal nodes -- the graph-completion facts the recovery write-back
    branch reads off the advisory cursor.
    """

    params: JsonObject
    params_hash: str
    pipeline_id: str
    pipeline_spec_hash: str
    attempt_id: str
    node_results: Annotated[dict[str, JsonValue], _merge_node_results]
    outcome: str
    result: JsonValue


class LlmCallFn(Protocol):
    """The injectable ``llm_call`` node body (small-model critique/verify/summarize).

    S2 ships no real body: tests inject fakes (honestly labeled); the LLM-tier
    wiring lands with S4. Receives the node (model tier + prompt template)
    and the current graph state; returns the node's JSON result value.
    """

    async def __call__(self, node: PipelineNode, state: EngineState) -> JsonValue: ...


class ToolFn(Protocol):
    """The injectable ``tool`` node body (shell command / static tool reference).

    S2 ships no real body either (the optional ``run_command``-backed real
    implementation was left out to keep the injection face the single tested
    seam); a future slice may bind command nodes onto the process primitives.
    """

    async def __call__(self, node: PipelineNode, state: EngineState) -> JsonValue: ...


class AgentFn(Protocol):
    """The injectable ``opencode_agent`` node body (the S3 worker face, T-C1-078).

    The real body is :class:`opencode.orchestrator.workers.WorkerManager`:
    a real CLI subprocess in a per-node git worktree with the proactive
    interception trio, the three-line budget enforcement and the
    structured-return acceptance gate. It reads the drive's job context (the
    ``ContextVar`` the engine sets around ``ainvoke``) for the job id and
    budget; the job params carry the worker's ``context`` /
    ``authorized_files`` / ``acceptance`` face. A raised
    :class:`~opencode.orchestrator.workers.WorkerError` carries its own §8
    code; a ``WorkerSuspended`` ends the drive non-terminated (the fold owns
    the outcome).
    """

    async def __call__(self, node: PipelineNode, state: EngineState) -> JsonValue: ...


def validate_pipeline_spec(
    spec: PipelineSpec,
    *,
    implemented: frozenset[str] = DEFAULT_IMPLEMENTED,
) -> None:
    """The semantic-gate layer (PHASE3 §3 discipline 2): topology + type constraints.

    Raises :class:`PipelineSpecRejected` with the first violation found
    (deterministic order: the version gate, the nesting-depth cap, the
    unimplemented node-type face, per-type field constraints, node-id
    uniqueness, edge-reference closure, the entry rules, orphan reachability).
    ``implemented`` is the executed node-type set: the default is the S2 face
    (``llm_call``/``tool``) whose rejection message is pinned byte-identical
    by the S2 tests; a worker-wired engine passes its executed set so
    ``opencode_agent`` passes the face and the message names the set.
    """
    ids = spec.node_ids()
    id_set = set(ids)

    # -- the version gate: this engine is a v1 reader (pipelines.py's §5.1
    # note; CORE-CONTRACTS §8: an old reader fails explicitly on a newer
    # version, never guesses). First, so no other rule ever runs against a
    # spec from a schema generation it does not understand.
    if spec.schema_version != 1:
        raise PipelineSpecRejected(
            f"pipeline spec schema_version {spec.schema_version} is not supported (this engine reads v1 specs only)",
            code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
            details={"schema_version": spec.schema_version},
        )

    # -- the nesting-depth hard cap (spec-side second line, PHASE3 §5.2; the
    # gateway's lineage guard is the first). Checked before the
    # unimplemented-type face so the cap stays operative for whichever type
    # starts carrying depth fields.
    for node in spec.nodes:
        if node.depth is not None and node.depth > MAX_SUBWORKFLOW_DEPTH:
            raise PipelineSpecRejected(
                f"node '{node.node_id}' declares nesting depth {node.depth} over the hard cap {MAX_SUBWORKFLOW_DEPTH}",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node.node_id, "node_type": node.node_type, "depth": node.depth},
            )

    # -- the execution face: closed-set names, the non-executed ones rejected
    # with an explicit message naming the delivery slice. The default-face
    # message is pinned byte-identical to the S2 wording by test_pipelines.py;
    # a worker-wired engine names its executed set instead.
    for node in spec.nodes:
        if node.node_type in implemented:
            continue
        if "opencode_agent" in implemented:
            message = (
                f"unimplemented node type '{node.node_type}' on node '{node.node_id}': "
                "this engine executes llm_call/tool/opencode_agent nodes only "
                "(human_gate and subworkflow land with S5a)"
            )
        else:
            message = (
                f"unimplemented node type '{node.node_type}' on node '{node.node_id}': "
                "S2 executes llm_call/tool nodes only "
                "(opencode_agent lands with S3 workers; human_gate and subworkflow with S5a)"
            )
        raise PipelineSpecRejected(
            message,
            code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
            details={"node_id": node.node_id, "node_type": node.node_type},
        )

    # -- per-type field constraints (semantic layer by design: see pipelines.py).
    for node in spec.nodes:
        if node.node_type == "llm_call" and (node.model is None or node.prompt_template is None):
            raise PipelineSpecRejected(
                f"llm_call node '{node.node_id}' must carry both a model tier ('model') "
                "and a prompt template ('prompt_template')",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node.node_id, "node_type": node.node_type},
            )
        if node.node_type == "opencode_agent" and node.prompt_template is None:
            raise PipelineSpecRejected(
                f"{node.node_type} node '{node.node_id}' must carry a prompt template "
                "('prompt_template') as its goal for the worker brief",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node.node_id, "node_type": node.node_type},
            )
        if node.node_type == "tool":
            has_command = node.command is not None and len(node.command) > 0
            has_ref = node.tool_ref is not None
            if has_command and has_ref:
                raise PipelineSpecRejected(
                    f"tool node '{node.node_id}' carries both 'command' and 'tool_ref' (exactly one is required)",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id, "node_type": node.node_type},
                )
            if not has_command and not has_ref:
                raise PipelineSpecRejected(
                    f"tool node '{node.node_id}' must carry a command ('command') or a "
                    "static tool reference ('tool_ref')",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id, "node_type": node.node_type},
                )

    # -- node-id uniqueness.
    if len(ids) != len(id_set):
        duplicate: str | None = None
        seen: set[str] = set()
        for node_id in ids:
            if node_id in seen:
                duplicate = node_id
                break
            seen.add(node_id)
        assert duplicate is not None
        raise PipelineSpecRejected(
            f"duplicate node id '{duplicate}'",
            code=CoreErrorCode.VALIDATION_ERROR,
            details={"node_id": duplicate},
        )

    # -- edge-reference closure.
    for edge in spec.edges:
        for endpoint in (edge.from_node, edge.to_node):
            if endpoint not in id_set:
                raise PipelineSpecRejected(
                    f"edge references unknown node '{endpoint}'",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"from_node": edge.from_node, "to_node": edge.to_node},
                )

    # -- the unique entry: exists and carries no incoming edges.
    if spec.entry not in id_set:
        raise PipelineSpecRejected(
            f"entry node '{spec.entry}' is not declared",
            code=CoreErrorCode.VALIDATION_ERROR,
            details={"entry": spec.entry},
        )
    if any(edge.to_node == spec.entry for edge in spec.edges):
        raise PipelineSpecRejected(
            f"entry node '{spec.entry}' must be unique: an edge points into it",
            code=CoreErrorCode.VALIDATION_ERROR,
            details={"entry": spec.entry},
        )

    # -- no orphan nodes: every node reachable from the entry.
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in spec.edges:
        adjacency[edge.from_node].append(edge.to_node)
    reachable: set[str] = {spec.entry}
    queue: deque[str] = deque((spec.entry,))
    while queue:
        for neighbor in adjacency[queue.popleft()]:
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    orphans = sorted(id_set - reachable)
    if orphans:
        raise PipelineSpecRejected(
            f"orphan nodes unreachable from the entry: {orphans}",
            code=CoreErrorCode.VALIDATION_ERROR,
            details={"entry": spec.entry, "orphans": cast("list[JsonValue]", orphans)},
        )


class EngineCheckpointer:
    """The advisory-cursor store: the official sqlite saver at an independent file.

    langgraph-checkpoint-sqlite's ``AsyncSqliteSaver`` over an ``aiosqlite``
    connection opened lazily on first use (the sync ``SqliteSaver`` cannot
    serve an async graph: its sync-only face raises on ``ainvoke``). The file
    is *not* the main jobs database -- the M-7/§14 dual-database boundary:
    no alembic/census surface of the main DB is touched, and the only tables
    ever created here are the saver's own ``checkpoints``/``writes``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._saver: AsyncSqliteSaver | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """The checkpointer file path (the ConfigSnapshot/app wiring face)."""
        return self._path

    async def saver(self) -> AsyncSqliteSaver:
        """The live saver, opening (and schema-setup) the file on first use."""
        async with self._lock:
            if self._saver is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(self._path)
                saver = AsyncSqliteSaver(conn)
                await saver.setup()
                self._conn = conn
                self._saver = saver
            return self._saver

    async def close(self) -> None:
        """Close the underlying connection (idempotent; the App close order)."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
                self._saver = None


@dataclass(frozen=True, slots=True)
class BoundPipeline:
    """One registered pipeline: the spec, its §5.1 hash, and the compiled graph."""

    spec: PipelineSpec
    spec_hash: str
    graph: CompiledStateGraph


class PipelineEngine:
    """The engine: registration gate + job drive + advisory-cursor reader.

    * ``register`` runs the three-layer gate and keeps the bound pipeline by
      ``pipeline_id`` (v1 executes registered pipelines only -- PHASE3 §1
      non-goal: no ad-hoc brain-authored specs);
    * ``drive`` is the ``JobExecutor`` face over ``JobService``: it starts the
      job, runs the real compiled graph under the checkpointer (thread id =
      job id) and finishes it with the graph's structured result;
    * ``read`` is the ``JobCheckpointReader`` face for the §8 recovery scan:
      the advisory-cursor facts (attempt, the job's params hash binding, and
      the graph-completion outcome/result) with no job-status claims;
    * node failures fail the job with the §8 ``CoreError`` face
      (``ToolFailed``/``ProviderUnavailable`` by node type); ``EnginePause``
      leaves the job ``running`` with the cursor holding the position.
    """

    def __init__(
        self,
        *,
        checkpointer: EngineCheckpointer,
        llm_call: LlmCallFn,
        tool: ToolFn,
        opencode_agent: AgentFn | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._llm_call = llm_call
        self._tool = tool
        self._opencode_agent = opencode_agent
        self._pipelines: dict[str, BoundPipeline] = {}

    def _implemented(self) -> frozenset[str]:
        """The executed node-type set: the S2 two, plus the agent when wired."""
        if self._opencode_agent is None:
            return DEFAULT_IMPLEMENTED
        return DEFAULT_IMPLEMENTED | frozenset({"opencode_agent"})

    # -- the registration gate (structure -> semantic -> rehearsal) -----------

    async def register(self, raw: JsonObject) -> BoundPipeline:
        """Load, gate and bind one pipeline spec; returns the bound pipeline.

        ``raw`` is the ``graph.json`` wire object. The structural layer is the
        pydantic contract; the semantic layer is :func:`validate_pipeline_spec`;
        the rehearsal layer is the real ``compile`` (no node body runs). The
        compiled graph is bound to this engine's checkpointer, so a later
        re-registration under the same ``pipeline_id`` with a changed spec is
        a *new* binding (new hash): running graphs resume against the hash
        they were started with or fail closed (PHASE3 §5.2 no hot rebind).
        """
        spec = PipelineSpec.model_validate(raw)
        validate_pipeline_spec(spec, implemented=self._implemented())
        saver = await self._checkpointer.saver()
        graph = self._compile(spec, saver)
        bound = BoundPipeline(spec=spec, spec_hash=compute_spec_hash(spec), graph=graph)
        self._pipelines[spec.pipeline_id] = bound
        return bound

    def bound(self, pipeline_id: str) -> BoundPipeline | None:
        """The registered binding for ``pipeline_id`` (None = never registered)."""
        return self._pipelines.get(pipeline_id)

    @property
    def opencode_agent(self) -> AgentFn | None:
        """The wired ``opencode_agent`` node body (None = the S2-only face).

        The App wiring face reads this to derive the jobs cancel hook: a
        worker manager instance doubles as the ``RunCanceller``.
        """
        return self._opencode_agent

    def _compile(self, spec: PipelineSpec, saver: AsyncSqliteSaver) -> CompiledStateGraph:
        """The rehearsal layer + the actual graph: build, wire topology, compile.

        Every node becomes one wrapped async node calling the injected body of
        its type; terminal nodes (no outgoing edges) additionally write the
        graph-completion facts (``outcome``/``result``) the recovery reader
        and the job finish read. Sequential execution is the S2 contract:
        fan-out topologies compile and run, but the terminal result assembly
        is only deterministic on a single-terminal chain (disclosed; parallel
        branch execution is not an S2 face).
        """
        terminal_ids = {
            node.node_id for node in spec.nodes if not any(edge.from_node == node.node_id for edge in spec.edges)
        }

        builder = StateGraph(EngineState)
        node_bodies: dict[str, PipelineNode] = {node.node_id: node for node in spec.nodes}

        def make_node_fn(node: PipelineNode) -> Any:
            async def node_fn(state: EngineState) -> dict[str, Any]:
                try:
                    if node.node_type == "llm_call":
                        value = await self._llm_call(node, state)
                    elif node.node_type == "tool":
                        value = await self._tool(node, state)
                    else:
                        # The registration gate proved this type executed; the
                        # only remaining closed-set name is the agent (the old
                        # two-way else->tool dispatch would have misrouted it).
                        if self._opencode_agent is None:
                            raise _NodeFailure(
                                node,
                                WorkerError(
                                    "opencode_agent executor is not wired (the gate should have rejected this spec)",
                                    code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
                                ),
                            )
                        value = await self._opencode_agent(node, state)
                except EnginePause:
                    raise  # the suspend seam passes through untouched
                except WorkerSuspended:
                    raise  # the S3 cancel seam: the drive returns, the fold owns the outcome
                except Exception as cause:
                    raise _NodeFailure(node, cause) from cause
                update: dict[str, Any] = {"node_results": {node.node_id: value}}
                if node.node_id in terminal_ids:
                    results = {**state.get("node_results", {}), node.node_id: value}
                    update["outcome"] = "done"
                    update["result"] = {"pipeline_id": spec.pipeline_id, "nodes": results}
                return update

            return node_fn

        for node_id, node in node_bodies.items():
            builder.add_node(node_id, make_node_fn(node))
        builder.add_edge(START, spec.entry)
        for edge in spec.edges:
            builder.add_edge(edge.from_node, edge.to_node)
        for node_id in terminal_ids:
            builder.add_edge(node_id, END)
        return builder.compile(checkpointer=saver)

    # -- the JobExecutor face --------------------------------------------------

    async def _finish(
        self,
        handle: JobHandle,
        outcome: Literal["done", "failed", "cancelled"],
        *,
        result: JsonValue | None = None,
        error: CoreError | None = None,
    ) -> None:
        """Finish the job with the FakeJobExecutor discipline: a lost race is silent.

        A ``JobTransitionError`` from the emit means a concurrent resolution
        (a cancel race, or a recovery write-back) already made the job
        terminal -- the read model's fold is the authority, never the drive's
        local view, so the drive swallows it and returns.
        """
        try:
            await handle.finished(outcome, result=result, error=error)
        except JobTransitionError:
            return

    async def drive(self, handle: JobHandle, *, resume: bool = False) -> None:
        """Drive one job through its bound pipeline graph (the executor seam).

        Idempotent re-entry: a drive over an already-running job (the start
        emit rejected because a previous drive won it) continues from the
        advisory cursor instead of restarting the graph -- completed nodes are
        never re-executed. A drive over a terminal job is a silent no-op (a
        cancel race already resolved it; the fold is the authority).
        """
        service: JobService = handle.service
        record = await service.get_job(handle.job_id)
        if record.status in TERMINAL_STATUSES:
            return
        bound = self._pipelines.get(record.pipeline_id)
        if bound is None:
            await self._finish(
                handle,
                "failed",
                error=CoreError(
                    code=CoreErrorCode.NOT_FOUND,
                    message=f"pipeline '{record.pipeline_id}' is not registered with this engine",
                    retryable=False,
                    details={"job_id": handle.job_id, "pipeline_id": record.pipeline_id},
                ),
            )
            return

        resume_mode = resume
        if not resume:
            try:
                await handle.started()
            except JobTransitionError:
                # started is legal only from created: an already-running job is
                # idempotent re-entry (continue from the cursor); a terminal
                # job is a lost race (silent no-op); anything else re-raises.
                record = await service.get_job(handle.job_id)
                if record.status in TERMINAL_STATUSES:
                    return
                if record.status != "running":
                    raise
                resume_mode = True

        saver = await self._checkpointer.saver()
        config: dict[str, Any] = {"configurable": {"thread_id": handle.job_id}}
        state0: dict[str, Any] = {
            "params": record.params if record.params is not None else {},
            "params_hash": record.params_hash if record.params_hash is not None else "",
            "pipeline_id": record.pipeline_id,
            "pipeline_spec_hash": bound.spec_hash,
            "attempt_id": record.attempt_id if record.attempt_id is not None else "",
        }

        if resume_mode:
            tuple_ = await saver.aget_tuple(cast(Any, config))
            if tuple_ is None:
                # Defense ① (T-B-092 ①, landed with S3): a resume drive with
                # no advisory cursor cannot prove the side-effect frontier.
                # The S2 fallback re-ran the whole graph under EmptyInputError
                # -- acceptable while node bodies were fakes; with real worker
                # side effects a re-run would duplicate work, so it fails
                # closed instead (the recovery matrix routes here only after
                # losing the checkpoint).
                await self._finish(
                    handle,
                    "failed",
                    error=CoreError(
                        code=CoreErrorCode.EXECUTION_UNKNOWN,
                        message=(
                            "resume drive found no advisory cursor for a running job; the side-effect frontier "
                            "cannot be proven, so the graph is not re-run (fail closed)"
                        ),
                        retryable=False,
                        details={"job_id": handle.job_id, "pipeline_id": record.pipeline_id},
                    ),
                )
                return
            values = tuple_.checkpoint.get("channel_values", {})
            # Defense ② (T-B-092 ②, landed with S3): a cursor without a spec
            # hash cannot prove its binding. S2 let None pass (skip the
            # check); S3 fails closed with a message distinct from the
            # re-registration mismatch below.
            graph_spec_hash = values.get("pipeline_spec_hash")
            if graph_spec_hash is None:
                await self._finish(
                    handle,
                    "failed",
                    error=CoreError(
                        code=CoreErrorCode.EXECUTION_UNKNOWN,
                        message=(
                            "advisory cursor carries no pipeline spec hash; the graph's pipeline binding "
                            "cannot be proven, so the graph is not resumed (fail closed)"
                        ),
                        retryable=False,
                        details={"job_id": handle.job_id, "pipeline_id": record.pipeline_id},
                    ),
                )
                return
            # PHASE3 §5.2: a running graph never hot-rebinds to a re-registered
            # spec. A hash mismatch means the spec changed after this graph
            # started: side effects cannot be proven against the new binding.
            if graph_spec_hash != bound.spec_hash:
                await self._finish(
                    handle,
                    "failed",
                    error=CoreError(
                        code=CoreErrorCode.EXECUTION_UNKNOWN,
                        message=(
                            "pipeline spec hash changed after the graph started; "
                            "running graphs are never rebound (cancel, revise, resubmit)"
                        ),
                        retryable=False,
                        details={
                            "job_id": handle.job_id,
                            "pipeline_id": record.pipeline_id,
                            "registered_spec_hash": bound.spec_hash,
                            "graph_spec_hash": values.get("pipeline_spec_hash"),
                        },
                    ),
                )
                return

        # The S3 context seam: the job id + decoded budget ride a ContextVar
        # (the graph-state channel set is pinned by the S2 tests, so no new
        # channels); the opencode_agent node body reads it. The committed
        # budget row was validated at emit time -- the decode here is the
        # defensive second line and degrades to "no budget" on garbage.
        try:
            job_budget = JobBudget.model_validate(record.budget if isinstance(record.budget, dict) else {})
        except ValidationError:
            job_budget = JobBudget()

        try:
            context_token = AGENT_JOB_CONTEXT.set(AgentJobContext(job_id=handle.job_id, budget=job_budget))
            try:
                if resume_mode:
                    final = await bound.graph.ainvoke(None, cast(Any, config))
                else:
                    final = await bound.graph.ainvoke(state0, cast(Any, config))
            finally:
                AGENT_JOB_CONTEXT.reset(context_token)
        except EnginePause:
            # Leave the job running: the advisory cursor holds the position; the
            # recovery scan (or a plain re-drive) resumes from it.
            return
        except WorkerSuspended:
            # The S3 cancel face: the fold already committed the terminal
            # outcome (cancel beats the drive); leave it untouched.
            return
        except _NodeFailure as failure:
            # The injected body raised: attribute to the node and map the §8
            # code by type (llm_call -> ProviderUnavailable; a WorkerError
            # carries its own code; the rest default to ToolFailed).
            node = failure.node
            if node.node_type == "llm_call":
                code = CoreErrorCode.PROVIDER_UNAVAILABLE
            elif isinstance(failure.__cause__, WorkerError):
                code = failure.__cause__.code
            else:
                code = CoreErrorCode.TOOL_FAILED
            await self._finish(
                handle,
                "failed",
                error=CoreError(
                    code=code,
                    message=f"pipeline node '{node.node_id}' failed: {failure.__cause__}",
                    retryable=False,
                    details={"job_id": handle.job_id, "node_id": node.node_id, "node_type": node.node_type},
                ),
            )
            return

        outcome = final.get("outcome") if final else None
        if outcome == "done":
            await self._finish(handle, "done", result=final.get("result") if final else None)
        else:
            await self._finish(
                handle,
                "failed",
                error=CoreError(
                    code=CoreErrorCode.VALIDATION_ERROR,
                    message="graph completed without a terminal outcome marker",
                    retryable=False,
                    details={"job_id": handle.job_id, "pipeline_id": record.pipeline_id},
                ),
            )

    # -- the JobCheckpointReader face -------------------------------------------

    async def read(self, job_id: str) -> JobCheckpoint | None:
        """The §8 recovery reader: the advisory-cursor facts for one job.

        ``None`` when no checkpoint exists (the fail-closed branch: the
        gateway can prove nothing about side effects). With a cursor, the
        checkpoint carries the attempt id, the job's ``params_hash`` binding
        (the S1 ``JobCheckpoint.spec_hash`` semantics: the recovery scan
        compares it against the job row's binding hash), and -- when the graph
        reached its terminal node -- the completion outcome/result plus the
        deterministic write-back envelope (stable batch/event ids derived
        from the job id, never colliding with the service's counter-minted
        ids because of their non-hex ``wb`` infix, and the checkpoint's own
        timestamp), so a repeated recovery is the writer's idempotent
        whole-batch replay.
        """
        saver = await self._checkpointer.saver()
        config: dict[str, Any] = {"configurable": {"thread_id": job_id}}
        tuple_ = await saver.aget_tuple(cast(Any, config))
        if tuple_ is None:
            return None
        values = dict(tuple_.checkpoint.get("channel_values", {}))
        outcome = values.get("outcome")
        if outcome != "done":
            outcome = None  # partial graph: cursor only, no completion claim
        created_at_ms: int | None = None
        if outcome == "done":
            ts = tuple_.checkpoint.get("ts")
            if isinstance(ts, str):
                try:
                    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    created_at_ms = int(parsed.timestamp() * 1000)
                except ValueError:
                    created_at_ms = None
            job_hex = job_id[4:] if job_id.startswith("job_") else job_id
            return JobCheckpoint(
                job_id=job_id,
                attempt_id=values.get("attempt_id") if isinstance(values.get("attempt_id"), str) else None,
                spec_hash=values.get("params_hash") if isinstance(values.get("params_hash"), str) else None,
                batch_id=f"bat_wb{job_hex}",
                outcome="done",
                result=values.get("result"),
                error=None,
                event_id=f"evt_wb{job_hex}",
                created_at_ms=created_at_ms,
            )
        return JobCheckpoint(
            job_id=job_id,
            attempt_id=values.get("attempt_id") if isinstance(values.get("attempt_id"), str) else None,
            spec_hash=values.get("params_hash") if isinstance(values.get("params_hash"), str) else None,
        )

    # -- lifecycle ----------------------------------------------------------------

    async def close(self) -> None:
        """Close the advisory-cursor store (the App close order: jobs first, then this)."""
        await self._checkpointer.close()
