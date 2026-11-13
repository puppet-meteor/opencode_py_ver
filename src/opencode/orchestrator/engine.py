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
  set instead). S5a (T-C1-080) wires the ``human_gate`` and ``subworkflow``
  bodies behind optional constructor flags (``gate_broker`` /
  ``subworkflow``), so the rejection fires only for compositions that
  genuinely lack the executor;
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
import re
import sys
from collections import deque
from collections.abc import Collection
from contextvars import ContextVar
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
from opencode.contracts.events import JobBudget, JobLineage
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import (
    MAX_SUBWORKFLOW_DEPTH,
    PipelineNode,
    PipelineSpec,
    compute_spec_hash,
)
from opencode.contracts.pydag_import import parse_pipeline_spec
from opencode.orchestrator.conditions import (
    ConditionSyntaxError,
    evaluate_step_condition,
    resolve_ref,
    validate_condition_syntax,
)
from opencode.orchestrator.gates import GateBroker
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
from opencode.tools.invoker import validate_input_strict, validate_schema_supported

#: The node types this engine executes when no worker manager is wired (the
#: S2 face, the default constructor). A worker-wired engine registers its
#: ``opencode_agent`` executor into this set at ``register`` time.
DEFAULT_IMPLEMENTED: frozenset[str] = frozenset({"llm_call", "tool"})

#: The S5a subworkflow poll interval: a nested job parked on a human gate
#: legitimately leaves the parent node waiting, so the node body polls the
#: nested job's read model until it turns terminal (the interval paces the
#: read only -- the semantics are the fold's, never the poll's).
_SUBWORKFLOW_POLL_S = 0.05


@dataclass(frozen=True, slots=True)
class DriveContext:
    """One drive's service face for the engine-internal node bodies (S5a).

    The ``human_gate`` and ``subworkflow`` bodies need the jobs service (the
    gate emits / the nested submit) plus the job's identity facts; the
    constructor cannot take the service (the engine is built *before* the
    JobService -- the executor seam), so ``drive`` sets this ContextVar
    around ``ainvoke`` (the S3 ``AGENT_JOB_CONTEXT`` precedent: the
    graph-state channel set is pinned by the S2 tests, so no new channels).
    ``depth`` is the §5.4 workflow-nesting depth and ``session_anchor`` the
    job's conversation ref -- the nested submission inherits both.
    """

    service: JobService
    job_id: str
    depth: int
    session_anchor: str


#: The per-drive context (set/reset around ``ainvoke`` in ``drive``).
DRIVE_CONTEXT: ContextVar[DriveContext | None] = ContextVar("opencode_pipeline_drive_context", default=None)


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
    the failure to the node and map the §8 code: a ``WorkerError`` keeps its
    own code, the unstructured rest defaults by node type
    (``ProviderUnavailable`` for ``llm_call``, ``ToolFailed`` for the others);
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


#: The closed key set of the v2 condition-skip record (T-C1-084): the one
#: explicit marker a skipped node leaves in ``node_results``.
_SKIP_RECORD_KEYS = frozenset({"skipped", "condition", "reason"})


def is_skip_record(value: JsonValue) -> bool:
    """True when *value* is a v2 condition-skip record (the explicit skip face).

    Closed key-set check: ``{"skipped": true, "condition": <expr>, "reason":
    <why>}`` exactly -- a node body returning a same-shaped object by
    accident would be indistinguishable on the wire, so the record is the
    contract and this predicate is its single recognizer.
    """
    return (
        isinstance(value, dict)
        and set(value) == _SKIP_RECORD_KEYS
        and value.get("skipped") is True
        and isinstance(value.get("condition"), str)
        and isinstance(value.get("reason"), str)
    )


class NodeBodyState(EngineState, total=False):
    """The state view a node *body* sees: EngineState plus the v2 params view.

    The engine's node wrapper resolves ``node.params`` at node entry and hands
    the body this per-call view (``node_params``: the resolved values). It is
    deliberately NOT a field of :class:`EngineState` -- a TypedDict key would
    become a langgraph channel in the checkpointed graph state, and the S2
    channel set is pinned by contract tests; the view is a plain dict cast,
    never a channel update and never persisted. Bodies read it through
    :func:`node_params_of`.
    """

    node_params: JsonObject


def node_params_of(state: EngineState) -> JsonObject:
    """The resolved v2 params the wrapper passed this body ({} when the node has none)."""
    view = cast("dict[str, Any]", state)
    value = view.get("node_params")
    return value if isinstance(value, dict) else {}


def _walk_strict(value: JsonValue, segments: list[str], ref: str, node_id: str) -> JsonValue:
    """An exact dot-path walk (params refs): any miss is the honest failure.

    No camel/snake tolerance here (the condition walk keeps the ported
    tolerant face; params refs are strict -- a documented asymmetry). Lists
    accept digit segments as indices.
    """
    current: Any = value
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                raise WorkerError(
                    f"node '{node_id}' params ref '{ref}' cannot resolve segment '{segment}'",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            raise WorkerError(
                f"node '{node_id}' params ref '{ref}' steps into a value that is not an "
                f"object/array at segment '{segment}'",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
    return current


def _resolve_node_params(
    node: PipelineNode,
    inputs: JsonObject,
    node_results: dict[str, JsonValue],
    node_ids: frozenset[str],
) -> JsonObject:
    """Resolve ``node.params`` at node entry: literals pass, refs bind strictly.

    ``inputs.x`` binds the submitted param (a miss is a structured
    ``ValidationError`` naming it); ``step_N...`` binds a completed step's
    result -- a skipped or not-yet-completed step is the honest failure,
    never a bare ``KeyError`` or a silent null (the T-C1-084 decision:
    refs onto skipped steps error instead of nulling).
    """
    resolved: JsonObject = {}
    for name, value in (node.params or {}).items():
        if not (isinstance(value, dict) and set(value) == {"ref"}):
            resolved[name] = value  # a literal: the pass-through case
            continue
        ref = str(value["ref"])
        root, _, tail = ref.partition(".")
        if root == "inputs":
            head, _, rest = tail.partition(".")
            if not tail:
                raise WorkerError(
                    f"node '{node.node_id}' params ref '{ref}' must name an input key",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
            if head not in inputs:
                raise WorkerError(
                    f"node '{node.node_id}' params ref '{ref}' names input '{head}' the job did not submit",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
            resolved[name] = _walk_strict(inputs[head], rest.split(".") if rest else [], ref, node.node_id)
            continue
        if root not in node_ids:
            # The semantic gate proved root closure; reaching here means a
            # body ran outside the registration gate -- fail honestly.
            raise WorkerError(
                f"node '{node.node_id}' params ref '{ref}' has unknown root '{root}'",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
        if root not in node_results:
            raise WorkerError(
                f"node '{node.node_id}' params ref '{ref}' references step '{root}' "
                "which has not completed (declared upstream order broken or dangling)",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
        step_result = node_results[root]
        if is_skip_record(step_result):
            raise WorkerError(
                f"node '{node.node_id}' params ref '{ref}' references skipped step '{root}' "
                "(its condition evaluated false; a skipped result is not bindable)",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
        resolved[name] = _walk_strict(step_result, tail.split(".") if tail else [], ref, node.node_id)
    return resolved


def _make_condition_resolver(node_ids: frozenset[str], node_results: dict[str, JsonValue]) -> Any:
    """The runtime condition resolver: the ported tolerant walk with skip guards.

    Refs onto *known* steps guard first (skipped -> the honest WorkerError,
    which propagates out of the ported evaluator because WorkerError is not
    a KeyError/TypeError/ValueError; not-completed -> the same face), while
    unknown roots and unknown fields keep the ported missing/empty
    semantics (a typo'd condition reads as false, not as an error -- the
    framework's own behavior, documented as the conditions/params asymmetry).
    """

    def resolver(path: str, context: dict[str, Any]) -> Any:
        root = path.split(".", 1)[0]
        if root in node_ids:
            if root not in node_results:
                raise WorkerError(
                    f"condition ref '{path}' references step '{root}' which has not completed",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
            if is_skip_record(node_results[root]):
                raise WorkerError(
                    f"condition ref '{path}' references skipped step '{root}' "
                    "(its condition evaluated false; a skipped result is not bindable)",
                    code=CoreErrorCode.VALIDATION_ERROR,
                )
        return resolve_ref(path, context)

    return resolver


def _project_extract(value: JsonValue, extract: dict[str, str | list[str]]) -> JsonObject:
    """The v2 extract projection over the node's own executed result.

    A plain exact dot-path walk (no camel/snake tolerance and none of the
    framework's dynamic-field/content-0 heuristics -- a documented deviation),
    ``first-hit-wins`` across an ordered fallback chain, unresolved chains
    project ``null`` (the ported framework core semantics: extract is a
    projection, not a validation). Only the executed path reaches here --
    skip records are never projected.
    """
    projected: JsonObject = {}
    for key, chain in extract.items():
        paths = [chain] if isinstance(chain, str) else chain
        resolved: JsonValue | None = None
        for path in paths:
            current: Any = value
            ok = True
            for segment in path.split("."):
                if isinstance(current, dict) and segment in current:
                    current = current[segment]
                elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
                    current = current[int(segment)]
                else:
                    ok = False
                    break
            if ok:
                resolved = current
                break
        projected[key] = resolved
    return projected


# -- the T-C1-086 ported StepDef four-field face (transform / output_format /
# instance_binding / fallback_tools); the framework source is the semantic
# authority (pydag_engine.py / structured_ir.py / transform_constants.py) ----

#: The not-found sentinel (distinguishes "absent" from a legit ``None``).
_MISSING: Any = object()

#: A bare ``{identifier}`` placeholder (the ported literal-template face,
#: pydag_engine.py ``render_param_template`` L147-155).
_TEMPLATE_KEY = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: The reserved framework compute-transform names (transform_constants.py:
#: ``_REGISTERED_CORE_TRANSFORMS`` + ``_LEGACY_SPECIAL_TRANSFORMS``). They
#: parse on the wire but have no native executor -- skipped at the write
#: point (disclosed; domain-registered names are not ported, fail-closed).
_RESERVED_COMPUTE_TRANSFORMS: frozenset[str] = frozenset(
    {
        "format_check_results",
        "format_engine_result",
        "format_modelarts_check_results",
        "normalize_alarm_events",
        "normalize_cma_alarm_context",
        "normalize_input_context",
        "normalize_logs",
        "normalize_metric_thresholds",
        "normalize_modelarts_alarm_context",
        "normalize_modelarts_obs_artifact",
        "normalize_modelarts_topology",
        "normalize_obs_artifact",
        "normalize_topology_artifact",
        "query_records",
        "build_param_grid",
        "reduce_records",
        "apply_judgments",
        "build_report_rows",
        "format_output",
        "split_csv",
        "date_window",
        "time_window_ms",
        "build_dws_diagnose_request",
        "coalesce_fields",
        "collect_resource_ids",
        "aggregate_rows",
        "filter_alarms",
    }
)

#: The ported field-op set (structured_ir.py ``_ALLOWED_TRANSFORM_OPS``
#: L90-104) with each op's key set (the ``default`` op's value key is
#: optional: ``{"op", "args"}`` is the minimum).
_TRANSFORM_OPS: dict[str, frozenset[str]] = {
    "copy": frozenset({"op", "args"}),
    "add": frozenset({"op", "args"}),
    "subtract": frozenset({"op", "args"}),
    "multiply": frozenset({"op", "args"}),
    "divide": frozenset({"op", "args"}),
    "int": frozenset({"op", "args"}),
    "str": frozenset({"op", "args"}),
    "concat": frozenset({"op", "args"}),
    "coalesce": frozenset({"op", "args"}),
    "default": frozenset({"op", "args", "default"}),
    "contains": frozenset({"op", "source", "needle"}),
    "if_contains": frozenset({"op", "source", "needle", "then", "else"}),
    "if_equals": frozenset({"op", "source", "value", "then", "else"}),
}

_BINARY_TRANSFORM_OPS = frozenset({"add", "subtract", "multiply", "divide"})


class _TransformSkip(Exception):
    """One transform op cannot compute: the framework logs and skips
    (pydag_engine.py L7373-7376) -- a transform never fails the node."""


def _gate_param_ref(
    ref: str,
    owner: str,
    *,
    node_id: str,
    declared: Collection[str],
    id_set: Collection[str],
    has_schema: bool,
) -> None:
    """One ref's static closure (the T-C1-086 shared seam): the node's own
    params and each fallback entry's params close over the same roots --
    ``inputs`` must name a declared property, any other root must be a
    declared node id."""
    root, _, tail = ref.partition(".")
    if root == "inputs":
        head = tail.split(".", 1)[0]
        if not has_schema:
            raise ValueError(
                f"node '{node_id}' {owner} ref '{ref}' cannot bind: the spec declares no "
                "params schema, so inputs refs are statically unbindable"
            )
        if not tail or head not in declared:
            raise ValueError(f"node '{node_id}' {owner} ref '{ref}' does not name a declared params schema property")
    elif root not in id_set:
        raise ValueError(
            f"node '{node_id}' {owner} ref '{ref}' has unknown root '{root}' (expected 'inputs' or a declared node id)"
        )


def _gate_transform_operand(name: str, operand: JsonValue) -> None:
    """An operand is exactly one key: ``field`` (non-empty dot path) or ``literal``."""
    if not isinstance(operand, dict) or len(operand) != 1:
        raise ValueError(
            f"transform.{name}: an operand must be exactly one of {{'field': path}} or {{'literal': value}}"
        )
    kind = next(iter(operand))
    if kind not in {"field", "literal"}:
        raise ValueError(f"transform.{name}: unknown operand kind {kind!r} (expected 'field' or 'literal')")
    if kind == "field" and (not isinstance(operand["field"], str) or not operand["field"]):
        raise ValueError(f"transform.{name}: a field operand must be a non-empty dot path")


def _gate_transform_op_entry(name: str, entry: JsonObject) -> None:
    """The per-op shape gate (structured_ir.py ``_compile_op`` L1461-1501 /
    ``_operand`` L1529-1542 evidence). Reserved compute-transform names pass
    through (no native executor); unknown ops and unknown keys are rejected
    -- deliberately stricter than the framework's compile, which silently
    drops malformed ops (a disclosed deviation)."""
    if name in _RESERVED_COMPUTE_TRANSFORMS:
        return
    if "op" not in entry:
        raise ValueError(f"transform.{name}: the entry must carry an 'op' key")
    op = entry["op"]
    if not isinstance(op, str) or op not in _TRANSFORM_OPS:
        raise ValueError(f"transform.{name}: unknown transform op {op!r}")
    keys = set(entry)
    required = _TRANSFORM_OPS[op]
    if op == "default":
        if not {"op", "args"} <= keys or not keys <= required:
            raise ValueError(
                f"transform.{name}: op '{op}' takes the keys {sorted(required)} ('default' optional, 'args' required)"
            )
    elif keys != required:
        raise ValueError(f"transform.{name}: op '{op}' takes exactly the keys {sorted(required)}")
    args = entry.get("args")
    if op in _BINARY_TRANSFORM_OPS:
        if not isinstance(args, list) or len(args) != 2:
            raise ValueError(f"transform.{name}: op '{op}' takes exactly 2 args")
    elif "args" in required and (not isinstance(args, list) or not args):
        raise ValueError(f"transform.{name}: op '{op}' takes a non-empty args list")
    if "source" in required:
        _gate_transform_operand(name, entry["source"])
    if isinstance(args, list):
        for operand in args:
            _gate_transform_operand(name, operand)


def _gate_output_format(output_format: JsonObject) -> None:
    """The evidence shape: exactly ``{kind, fields}``, ``kind == "field_map"``,
    a non-empty name -> field-spec object (the framework's other kinds are
    not ported, fail-closed)."""
    if set(output_format) != {"kind", "fields"}:
        raise ValueError(f"output_format: must be exactly the keys ['fields', 'kind'] (got {sorted(output_format)})")
    if output_format["kind"] != "field_map":
        raise ValueError(
            f"output_format: kind {output_format['kind']!r} is not supported (the evidence kind is 'field_map')"
        )
    fields = output_format["fields"]
    if not isinstance(fields, dict) or not fields:
        raise ValueError("output_format: 'fields' must be a non-empty object mapping output names to field specs")
    for name, field_spec in fields.items():
        if not isinstance(field_spec, dict):
            raise ValueError(f"output_format: fields.{name} must be an object (a field spec)")


def _gate_instance_binding(binding: JsonObject, declared: frozenset[str], has_schema: bool) -> None:
    """The evidence shape (skill_recipe.py L1176-1182 writes it; the engine
    renders the query id): closed key set, string requirements, the exact
    source wrapper, and ``resource_id`` = a declared request property."""
    unknown = set(binding) - {"node_type", "source", "resource_id", "query_id_param", "query_id_template"}
    if unknown:
        raise ValueError(f"instance_binding declares unknown keys {sorted(unknown)}")
    for required in ("node_type", "resource_id", "query_id_param", "query_id_template"):
        if required not in binding:
            raise ValueError(f"instance_binding is missing the required '{required}' key")
    for str_key in ("node_type", "query_id_param", "query_id_template"):
        if not isinstance(binding[str_key], str) or not binding[str_key]:
            raise ValueError(f"instance_binding '{str_key}' must be a non-empty string")
    if "source" in binding:
        source = binding["source"]
        if not isinstance(source, dict) or set(source) != {"source"} or not isinstance(source.get("source"), dict):
            raise ValueError("instance_binding 'source' must be exactly {{'source': <object>}}")
    resource_id = binding["resource_id"]
    if not isinstance(resource_id, dict) or set(resource_id) != {"ref"} or not isinstance(resource_id["ref"], str):
        raise ValueError("instance_binding 'resource_id' must be exactly {{'ref': 'request.<property>'}}")
    ref = resource_id["ref"]
    root, _, tail = ref.partition(".")
    if root != "request" or not tail:
        raise ValueError(
            f"instance_binding 'resource_id' must reference a request property (request.<name>), got {ref!r}"
        )
    if not has_schema:
        raise ValueError(
            "instance_binding cannot bind: the spec declares no params schema, "
            "so request refs are statically unbindable"
        )
    head = tail.split(".", 1)[0]
    if head not in declared:
        raise ValueError(
            f"instance_binding 'resource_id' references request property '{head}' the spec does not declare"
        )


def _is_empty_output(value: Any) -> bool:
    """The ported empty-output predicate (pydag_engine.py L7411-7427): None,
    an empty/all-empty dict, an empty/all-empty list, a blank string; ``0``
    and ``False`` are NOT empty."""
    if value is None:
        return True
    if isinstance(value, dict):
        return all(_is_empty_output(item) for item in value.values())
    if isinstance(value, list):
        return all(_is_empty_output(item) for item in value)
    if isinstance(value, str):
        return not value.strip()
    return False


def _output_format_fields(output_format: JsonObject) -> frozenset[str]:
    """The expected output-field names (pydag_engine.py ``_field_map_output_fields`` L7171-7184)."""
    fields = output_format.get("fields")
    return frozenset(fields) if isinstance(fields, dict) else frozenset()


def _find_nested_value(value: Any, key: str) -> Any:
    """The ported nested-key finder (pydag_engine.py L4319-4333): an exact-key
    recursion over dict values and list items, first non-None hit wins."""
    if isinstance(value, dict):
        if key in value and value[key] is not None:
            return value[key]
        for item in value.values():
            found = _find_nested_value(item, key)
            if found is not _MISSING:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_nested_value(item, key)
            if found is not _MISSING:
                return found
    return _MISSING


def _render_field_map(value: JsonValue, output_format: JsonObject) -> JsonObject:
    """The deterministic zero-LLM render (pydag_engine.py ``_format_field_map``
    L3989-4014 + ``_resolve_field_map_value`` L4002-4014): direct key, then the
    nested finder (the framework's context-envelope candidate is envelope-
    specific and not ported), then the spec's example/default; array coercion
    only for a declared array type over a non-None non-list value."""
    fields = output_format.get("fields")
    assert isinstance(fields, dict)  # the semantic gate proved the shape
    rendered: JsonObject = {}
    for name, field_spec in fields.items():
        spec = cast("dict[str, Any]", field_spec)
        resolved: Any = _MISSING
        if isinstance(value, dict) and name in value:
            resolved = value[name]
        if resolved is _MISSING or resolved is None:
            resolved = _find_nested_value(value, name)
        if resolved is _MISSING or resolved is None:
            resolved = spec.get("example", spec.get("default"))
        if spec.get("type") == "array" and resolved is not None and not isinstance(resolved, list):
            resolved = [resolved]
        rendered[name] = resolved
    return rendered


def _resolve_transform_operand(operand: JsonObject, value: Any, *, null_tolerant: bool = False) -> Any:
    """``{"field": path}`` walks the value strictly (a miss skips the op);
    ``{"literal": v}`` passes through (``None`` included -- the framework's
    ``_literal(None)`` evidence for ``default``'s null-tolerant operand)."""
    kind = next(iter(operand))
    if kind == "literal":
        return operand["literal"]
    current: Any = value
    for segment in str(operand["field"]).split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            if null_tolerant:
                return None
            raise _TransformSkip(str(operand["field"]))
    return current


def _eval_transform_op(entry: JsonObject, value: Any) -> JsonValue:
    """One field op (structured_ir.py ``_compile_op`` L1461-1501 + transform_eval
    semantics): Python arithmetic/str/containment; ``default`` is Python
    truthiness; ``coalesce`` is first-truthy-else-last with all operands resolved."""
    spec = cast("dict[str, Any]", entry)
    op = spec["op"]
    if op == "default":
        operand = _resolve_transform_operand(cast("JsonObject", spec["args"][0]), value, null_tolerant=True)
        return operand or spec.get("default")
    if op == "coalesce":
        args = [_resolve_transform_operand(cast("JsonObject", a), value) for a in spec["args"]]
        for arg in args:
            if arg:
                return arg
        return args[-1]
    if op in {"contains", "if_contains", "if_equals"}:
        source = _resolve_transform_operand(cast("JsonObject", spec["source"]), value)
        if op == "contains":
            return spec["needle"] in source
        if op == "if_contains":
            return spec["then"] if spec["needle"] in source else spec["else"]
        return spec["then"] if source == spec["value"] else spec["else"]
    args = [_resolve_transform_operand(cast("JsonObject", a), value) for a in spec["args"]]
    if op == "copy":
        return args[0]
    if op == "add":
        return args[0] + args[1]
    if op == "subtract":
        return args[0] - args[1]
    if op == "multiply":
        return args[0] * args[1]
    if op == "divide":
        return args[0] / args[1]
    if op == "int":
        return int(args[0])
    if op == "str":
        return str(args[0])
    if op == "concat":
        return "".join(str(arg) for arg in args)
    raise _TransformSkip(str(op))  # unreachable: the gate proved the op set


def _apply_transform(value: JsonValue, transform: dict[str, JsonObject]) -> JsonValue:
    """Derived fields onto the node's own result (pydag_engine.py
    ``_build_context_update`` L7362-7376): declaration order, later entries
    see earlier derived fields, reserved names and failing ops are skipped --
    the node never fails on a transform (L7373-7376)."""
    if not isinstance(value, dict):
        return value  # nothing to compute onto: skipped (disclosed)
    result: dict[str, Any] = dict(value)
    for name, entry in transform.items():
        if name in _RESERVED_COMPUTE_TRANSFORMS:
            continue
        try:
            result[name] = _eval_transform_op(entry, result)
        except (_TransformSkip, ArithmeticError, TypeError, ValueError):
            continue
    return result


def _materialize_step_value(value: JsonValue, node: PipelineNode) -> JsonValue:
    """The write-path materialization: the extract projection, then transform."""
    if node.extract is not None:
        value = _project_extract(value, node.extract)
    if node.transform is not None:
        value = _apply_transform(value, node.transform)
    return value


def _fallback_reasons(node: PipelineNode, raw: JsonValue, materialized: JsonValue) -> list[str]:
    """The ported trigger predicate (pydag_engine.py ``_fallback_trigger_reasons``
    L7214-7247): empty primary envelope (the RAW pre-extract value), empty
    declared extract, a non-dict materialization, and declared output fields
    empty in the materialized value; deduped in declaration order."""
    reasons: list[str] = []
    if _is_empty_output(raw):
        reasons.append("primary_data_empty")
    extracted: JsonValue | None = None
    if node.extract is not None:
        extracted = _project_extract(raw, node.extract)
        if _is_empty_output(extracted):
            reasons.append("extracted_empty")
    candidate: JsonObject | None = None
    if isinstance(materialized, dict):
        candidate = materialized
    elif isinstance(extracted, dict):
        candidate = extracted
    else:
        reasons.append("materialized_output_missing")
    if node.output_format is not None:
        expected = _output_format_fields(node.output_format)
        missing = sorted(name for name in expected if candidate is None or _is_empty_output(candidate.get(name)))
        if missing:
            reasons.append("missing_expected_fields:" + ",".join(missing))
    return list(dict.fromkeys(reasons))


def _merge_fallback_history(value: JsonValue, history: list[JsonObject]) -> JsonValue:
    """The disclosure record rides the written value under ``fallback_history``
    (a non-dict value is wrapped in ``{"value": ...}`` -- a native-defined
    shape, recorded as OPEN in the progress file)."""
    record = cast("list[JsonValue]", history)
    if isinstance(value, dict):
        return {**value, "fallback_history": record}
    return {"value": value, "fallback_history": record}


def _disclose_fallback_attempt(node_id: str, record: JsonObject) -> None:
    """The ticket's stderr disclosure (the ADR-0002 L60 precedent: one
    compact, non-fatal line per failure -- the same shape the CLI
    composition root uses for a refused session or a failed MCP server at
    boot). Every attempt prints exactly one operator-facing line; the
    structured record stays the durable ``fallback_history`` face riding
    the written value."""
    status = str(record["status"])
    detail = ""
    if status == "rejected":
        missing = cast("list[JsonValue]", record["missing_after"])
        summary = ", ".join(str(reason) for reason in missing)
        detail = f" (missing_after: {summary})"
    elif status == "exception":
        detail = f" ({record['error']})"
    print(
        f"opencode: node {node_id} fallback {record['tool']}: {status}{detail}",
        file=sys.stderr,
    )


def _apply_instance_binding(node: PipelineNode, inputs: JsonObject, resolved: JsonObject) -> JsonObject:
    """Render the query id into the params view (the ported literal-template
    face, pydag_engine.py ``render_param_template`` L147-155, over the bare-key
    value context L3788-3795: request + node params, ``None`` dropped; an
    unresolved placeholder is the hard error). Injected under ``query_id_param``
    only when the params do not already carry it -- params are the authority."""
    binding = node.instance_binding
    assert binding is not None
    template = str(binding["query_id_template"])
    values = {key: val for key, val in {**inputs, **resolved}.items() if val is not None}
    query_id = template
    for key, val in values.items():
        query_id = query_id.replace("{" + key + "}", str(val))
    unresolved = set(_TEMPLATE_KEY.findall(query_id)) - set(values)
    if unresolved:
        raise ValueError(
            f"node '{node.node_id}' instance_binding query_id_template has unresolved "
            f"placeholders: {sorted(unresolved)}"
        )
    param = str(binding["query_id_param"])
    if param in resolved:
        return resolved
    return {**resolved, param: query_id}


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
    uniqueness, edge-reference closure, the entry rules, orphan
    reachability, then the v2 feature checks: the reserved ref root and
    render-context collisions, condition syntax, params ref-root closure,
    the llm structured-output/fallback constraints, and the ported
    four-field face (T-C1-086: transform op closure and per-op shapes,
    output_format / instance_binding evidence shapes, and the fallback
    entries' params closing over the same roots as the node's own).
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
    # with an explicit message naming the delivery slice. The two pinned
    # variants (the S2 default face and the S3 worker-wired face) stay
    # byte-identical -- test_pipelines.py locks both; any other wiring
    # combination (an S5a partial face: gate-broker or subworkflow wired
    # without the full set) names its executed set dynamically.
    for node in spec.nodes:
        if node.node_type in implemented:
            continue
        if implemented == DEFAULT_IMPLEMENTED | frozenset({"opencode_agent"}):
            message = (
                f"unimplemented node type '{node.node_type}' on node '{node.node_id}': "
                "this engine executes llm_call/tool/opencode_agent nodes only "
                "(human_gate and subworkflow land with S5a)"
            )
        elif implemented == DEFAULT_IMPLEMENTED:
            message = (
                f"unimplemented node type '{node.node_type}' on node '{node.node_id}': "
                "S2 executes llm_call/tool nodes only "
                "(opencode_agent lands with S3 workers; human_gate and subworkflow with S5a)"
            )
        else:
            executed = "/".join(sorted(implemented))
            message = (
                f"unimplemented node type '{node.node_type}' on node '{node.node_id}': "
                f"this composition executes {executed} nodes only "
                "(the closed set's other node types are not wired into this engine)"
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
        if node.node_type == "subworkflow" and node.workflow_ref is None:
            # The S5a constraint: a subworkflow node that reached the
            # per-type checks (only an executed-subworkflow engine gets here)
            # must name its nested pipeline -- otherwise the node body has
            # nothing to submit and would fail at drive time instead of at
            # the registration gate.
            raise PipelineSpecRejected(
                f"subworkflow node '{node.node_id}' must carry a nested pipeline reference ('workflow_ref')",
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

    # -- v2 (T-C1-084): the reserved ref root and the render-context collisions.
    # The ref context is {"inputs": <submitted params>, **node_results} and the
    # prompt render context is {**params, **node_results} -- a node id that is
    # "inputs" or that names a declared param would shadow or be shadowed, so
    # the ambiguity is rejected up front instead of silently resolving one way.
    for node_id in sorted(id_set):
        if node_id == "inputs":
            raise PipelineSpecRejected(
                "node id 'inputs' is reserved (the v2 ref root for submitted params)",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node_id},
            )
    declared: frozenset[str] = frozenset()
    if spec.params_schema is not None:
        properties = spec.params_schema.get("properties")
        if isinstance(properties, dict):
            declared = frozenset(properties)
    collisions = sorted(id_set & declared)
    if collisions:
        raise PipelineSpecRejected(
            f"node ids collide with declared params (the v2 render context keys both): {collisions}",
            code=CoreErrorCode.VALIDATION_ERROR,
            details={"node_ids": cast("list[JsonValue]", collisions)},
        )

    # -- v2: condition syntax (the ported evaluator's grammar; refs stay
    # runtime-tolerant -- unknown names are the ported missing semantics,
    # only skipped/not-completed *known* steps error at evaluation time).
    for node in spec.nodes:
        if node.condition is None:
            continue
        try:
            validate_condition_syntax(node.condition)
        except ConditionSyntaxError as error:
            raise PipelineSpecRejected(
                f"node '{node.node_id}' condition is not a supported condition expression: {error}",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node.node_id, "condition": node.condition},
            ) from error

    # -- v2: params ref-root closure (the strict face). Roots must be the
    # reserved "inputs" (naming a declared property when a schema is
    # declared; without a schema inputs are statically unbindable -- the
    # spec accepts the empty object only) or a declared node id (the tail
    # path is runtime-validated by the strict walk).
    for node in spec.nodes:
        if node.params is None:
            continue
        for name, value in node.params.items():
            if not (isinstance(value, dict) and set(value) == {"ref"}):
                continue  # a literal value: no closure to check
            ref = str(value["ref"])
            try:
                _gate_param_ref(
                    ref,
                    "params",
                    node_id=node.node_id,
                    declared=declared,
                    id_set=id_set,
                    has_schema=spec.params_schema is not None,
                )
            except ValueError as error:
                raise PipelineSpecRejected(
                    str(error),
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id, "param": name, "ref": ref},
                ) from error

    # -- v2: the llm structured-output face. output_schema/fallback are
    # llm_call features (no other body consumes them), the schema must sit
    # inside the strict validator subset (a root object), fallback requires
    # a schema and must satisfy it -- a degrade object that itself violates
    # the schema would trade a validation failure for another one.
    for node in spec.nodes:
        if node.output_schema is not None and node.node_type != "llm_call":
            raise PipelineSpecRejected(
                f"node '{node.node_id}' carries 'output_schema' but is not an llm_call node "
                "(structured output is an llm_call feature)",
                code=CoreErrorCode.VALIDATION_ERROR,
                details={"node_id": node.node_id, "node_type": node.node_type},
            )
        if node.fallback is not None:
            if node.node_type != "llm_call":
                raise PipelineSpecRejected(
                    f"node '{node.node_id}' carries 'fallback' but is not an llm_call node "
                    "(the degrade object is an llm_call feature)",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id, "node_type": node.node_type},
                )
            if node.output_schema is None:
                raise PipelineSpecRejected(
                    f"llm_call node '{node.node_id}' declares a fallback without an output_schema "
                    "(the degrade object engages only on structured-output validation failure)",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id},
                )
        if node.output_schema is not None:
            try:
                validate_schema_supported(node.output_schema, root=True)
            except ValueError as error:
                raise PipelineSpecRejected(
                    f"node '{node.node_id}' output_schema is outside the supported schema subset: {error}",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id},
                ) from error
            if node.fallback is not None:
                issues = validate_input_strict(node.output_schema, node.fallback)
                if issues:
                    raise PipelineSpecRejected(
                        f"node '{node.node_id}' fallback does not satisfy its output_schema: {'; '.join(issues)}",
                        code=CoreErrorCode.VALIDATION_ERROR,
                        details={"node_id": node.node_id},
                    )

    # -- T-C1-086: the ported StepDef four-field face. transform: the op
    # closure and per-op shapes (one shared gate; the native gate is
    # deliberately stricter than the framework's compile, which silently
    # drops malformed ops); output_format / instance_binding: the evidence
    # shapes; fallback_tools: each entry's params close over the same roots
    # as the node's own params (one closure rule, the T-C1-086 shared seam).
    for node in spec.nodes:
        if node.transform is not None:
            for name, entry in node.transform.items():
                try:
                    _gate_transform_op_entry(name, entry)
                except ValueError as error:
                    raise PipelineSpecRejected(
                        f"node '{node.node_id}' {error}",
                        code=CoreErrorCode.VALIDATION_ERROR,
                        details={"node_id": node.node_id, "transform": name},
                    ) from error
        if node.output_format is not None:
            try:
                _gate_output_format(node.output_format)
            except ValueError as error:
                raise PipelineSpecRejected(
                    f"node '{node.node_id}' {error}",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id},
                ) from error
        if node.instance_binding is not None:
            try:
                _gate_instance_binding(node.instance_binding, declared, has_schema=spec.params_schema is not None)
            except ValueError as error:
                raise PipelineSpecRejected(
                    f"node '{node.node_id}' {error}",
                    code=CoreErrorCode.VALIDATION_ERROR,
                    details={"node_id": node.node_id},
                ) from error
        for fallback in node.fallback_tools:
            for name, value in fallback.params.items():
                if not (isinstance(value, dict) and set(value) == {"ref"}):
                    continue  # a literal value: no closure to check
                ref = str(value["ref"])
                try:
                    _gate_param_ref(
                        ref,
                        f"fallback tool '{fallback.tool}' params",
                        node_id=node.node_id,
                        declared=declared,
                        id_set=id_set,
                        has_schema=spec.params_schema is not None,
                    )
                except ValueError as error:
                    raise PipelineSpecRejected(
                        str(error),
                        code=CoreErrorCode.VALIDATION_ERROR,
                        details={"node_id": node.node_id, "tool": fallback.tool, "param": name, "ref": ref},
                    ) from error


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
    * node failures fail the job with the §8 ``CoreError`` face: a
      ``WorkerError`` from the body keeps its own code, the rest defaults by
      node type (``ProviderUnavailable`` for ``llm_call``,
      ``ToolFailed`` otherwise); ``EnginePause``
      leaves the job ``running`` with the cursor holding the position.
    """

    def __init__(
        self,
        *,
        checkpointer: EngineCheckpointer,
        llm_call: LlmCallFn,
        tool: ToolFn,
        opencode_agent: AgentFn | None = None,
        gate_broker: GateBroker | None = None,
        subworkflow: bool = False,
    ) -> None:
        self._checkpointer = checkpointer
        self._llm_call = llm_call
        self._tool = tool
        self._opencode_agent = opencode_agent
        self._gate_broker = gate_broker
        self._subworkflow_enabled = subworkflow
        self._pipelines: dict[str, BoundPipeline] = {}

    def _implemented(self) -> frozenset[str]:
        """The executed node-type set: the S2 two plus each wired executor.

        The S5a flags are injection-gated so the S2/S3 default faces stay
        byte-identical: an engine with no broker executes no ``human_gate``
        (the spec rejects it at the gate), and an engine without the
        ``subworkflow`` flag executes no nested pipelines.
        """
        implemented = DEFAULT_IMPLEMENTED
        if self._opencode_agent is not None:
            implemented = implemented | frozenset({"opencode_agent"})
        if self._gate_broker is not None:
            implemented = implemented | frozenset({"human_gate"})
        if self._subworkflow_enabled:
            implemented = implemented | frozenset({"subworkflow"})
        return implemented

    # -- the registration gate (structure -> semantic -> rehearsal) -----------

    async def register(self, raw: JsonObject) -> BoundPipeline:
        """Load, gate and bind one pipeline spec; returns the bound pipeline.

        ``raw`` is the ``graph.json`` wire object. The structural layer is the
        pydantic contract; the semantic layer is :func:`validate_pipeline_spec`;
        the rehearsal layer is the real ``compile`` (no node body runs). A
        ``schema_version`` of ``pydag-editable-graph-v1`` (the
        agent_dsl_flow_framework dialect, T-C1-083) routes through the pydag
        converter at this loading seam before the structural layer --
        everything after the routing is the unchanged v1 path (semantic gate,
        rehearsal, hash lock; rejections surface as
        :class:`~opencode.contracts.pydag_import.PydagImportError`). The
        compiled graph is bound to this engine's checkpointer, so a later
        re-registration under the same ``pipeline_id`` with a changed spec is
        a *new* binding (new hash): running graphs resume against the hash
        they were started with or fail closed (PHASE3 §5.2 no hot rebind).
        """
        spec = parse_pipeline_spec(raw)
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

    @property
    def gate_broker(self) -> GateBroker | None:
        """The wired gate-reply broker (None = the engine executes no ``human_gate``).

        The App wiring face derives the JobService's gate hand-off from this
        instance (the same derivation pattern as the ``opencode_agent``
        canceller): the reply face resolves onto it and the ``human_gate``
        node body takes from it -- one broker, both ends.
        """
        return self._gate_broker

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
        id_set = frozenset(node_bodies)

        def make_node_fn(node: PipelineNode) -> Any:
            async def node_fn(state: EngineState) -> dict[str, Any]:
                try:
                    inputs = state.get("params") or {}
                    prior_results = state.get("node_results") or {}
                    # -- v2 condition (T-C1-084): evaluated BEFORE execution via
                    # the ported safe-subset evaluator. False -> the explicit
                    # skip record (never a silent no-op); refs onto skipped /
                    # not-completed steps raise the honest WorkerError through
                    # the evaluator (it only degrades KeyError/TypeError/
                    # ValueError to missing); a blank condition is "no
                    # condition" (passes) per the ported semantics.
                    if node.condition is not None:
                        context: dict[str, Any] = {"inputs": inputs, **prior_results}
                        resolver = _make_condition_resolver(id_set, prior_results)
                        evaluation = evaluate_step_condition(node.condition, context, resolver)
                        if not evaluation.passed:
                            skip: JsonValue = {
                                "skipped": True,
                                "condition": node.condition,
                                "reason": evaluation.reason,
                            }
                            update_skip: dict[str, Any] = {"node_results": {node.node_id: skip}}
                            if node.node_id in terminal_ids:
                                skip_results = {**prior_results, node.node_id: skip}
                                update_skip["outcome"] = "done"
                                update_skip["result"] = {
                                    "pipeline_id": spec.pipeline_id,
                                    "nodes": skip_results,
                                }
                            return update_skip
                    # -- v2 params: resolved at node entry (strict refs; a miss
                    # is the structured WorkerError) and handed to the body as
                    # the ephemeral node_params view -- never a channel update,
                    # never persisted (see NodeBodyState).
                    body_state: EngineState = state
                    if node.params is not None or node.instance_binding is not None:
                        resolved_params: JsonObject = (
                            _resolve_node_params(node, inputs, prior_results, id_set) if node.params is not None else {}
                        )
                        if node.instance_binding is not None:
                            resolved_params = _apply_instance_binding(node, inputs, resolved_params)
                        view = dict(state)
                        view["node_params"] = resolved_params
                        body_state = cast("EngineState", view)
                    if node.node_type == "llm_call":
                        value = await self._llm_call(node, body_state)
                    elif node.node_type == "tool":
                        value = await self._tool(node, body_state)
                    elif node.node_type == "human_gate":
                        # The S5a gate body: the registration gate proved this
                        # type executed, so the broker is wired (the guard is
                        # the same defense-in-depth the agent branch carries).
                        if self._gate_broker is None:
                            raise _NodeFailure(
                                node,
                                WorkerError(
                                    "human_gate executor is not wired (the gate should have rejected this spec)",
                                    code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
                                ),
                            )
                        value = await self._human_gate(node, body_state)
                    elif node.node_type == "subworkflow":
                        if not self._subworkflow_enabled:
                            raise _NodeFailure(
                                node,
                                WorkerError(
                                    "subworkflow executor is not wired (the gate should have rejected this spec)",
                                    code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
                                ),
                            )
                        value = await self._subworkflow(node, body_state)
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
                        value = await self._opencode_agent(node, body_state)
                    # -- v2 extract + the T-C1-086 write-point face: the
                    # projection runs only on the executed path (a skipped
                    # node never projects) and replaces the downstream-visible
                    # result; transform computes derived fields onto it; the
                    # fallback chain fires on the ported trigger predicate and
                    # adopts the first clean attempt (an exhausted chain keeps
                    # the primary, unrendered); output_format renders the
                    # deterministic field map; the history record rides the
                    # written value.
                    raw_value = value
                    written: JsonValue = _materialize_step_value(value, node)
                    history: list[JsonObject] = []
                    render = True
                    if node.fallback_tools:
                        reasons = _fallback_reasons(node, raw_value, written)
                        if reasons:
                            adopted, history = await self._try_fallback_tools(
                                node, inputs, prior_results, id_set, state, reasons
                            )
                            if adopted is not None:
                                written = adopted
                            else:
                                render = False  # exhausted: the primary stays, unrendered
                    if render and node.output_format is not None:
                        written = _render_field_map(written, node.output_format)
                    if history:
                        written = _merge_fallback_history(written, history)
                    value = written
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

    # -- the T-C1-086 fallback chain -------------------------------------------

    async def _try_fallback_tools(
        self,
        node: PipelineNode,
        inputs: JsonObject,
        prior_results: dict[str, JsonValue],
        id_set: frozenset[str],
        state: EngineState,
        trigger_reasons: list[str],
    ) -> tuple[JsonValue | None, list[JsonObject]]:
        """The ported fallback chain (pydag_engine.py L6229-6294): declared
        order, every attempt dispatched through the tool seam (the framework
        runs fallback entries as tool calls regardless of the step's own
        type), each attempt's params resolved through the node-params
        machinery and its result through the same extract/transform
        pipeline; adoption requires the FULL predicate to come back empty
        over the attempt (L6461-6474 -- a re-evaluation, not a subset check).
        An exhausted chain returns ``None`` (the caller keeps the primary);
        a raised attempt is an exception record, never a job failure
        (L6426-6445), and the control seams pass through untouched. The
        record face is deliberately leaner than the framework's log-shaped
        one (no params echo, no "started" phase -- disclosed);
        instance_binding never applies to attempts (disclosed). Every
        attempt also prints one compact, non-fatal stderr line (the
        ADR-0002 L60 precedent -- the ticket's disclosure face; the durable
        record stays the written value's ``fallback_history``).
        """
        history: list[JsonObject] = []
        reasons = cast("list[JsonValue]", list(trigger_reasons))
        for fallback in node.fallback_tools:
            attempt_node = node.model_copy(update={"tool_ref": fallback.tool, "params": fallback.params})
            try:
                attempt_params = _resolve_node_params(attempt_node, inputs, prior_results, id_set)
                view = dict(state)
                view["node_params"] = attempt_params
                raw_attempt = await self._tool(attempt_node, cast("EngineState", view))
                attempt_value = _materialize_step_value(raw_attempt, node)
                missing_after = _fallback_reasons(node, raw_attempt, attempt_value)
                if not missing_after:
                    adopted: JsonObject = {
                        "tool": fallback.tool,
                        "trigger_reasons": reasons,
                        "status": "adopted",
                    }
                    history.append(adopted)
                    _disclose_fallback_attempt(node.node_id, adopted)
                    return attempt_value, history
                rejected: JsonObject = {
                    "tool": fallback.tool,
                    "trigger_reasons": reasons,
                    "status": "rejected",
                    "missing_after": cast("list[JsonValue]", missing_after),
                }
                history.append(rejected)
                _disclose_fallback_attempt(node.node_id, rejected)
            except EnginePause:
                raise  # the suspend seam is never an attempt record
            except WorkerSuspended:
                raise  # the S3 cancel seam
            except Exception as cause:
                failed: JsonObject = {
                    "tool": fallback.tool,
                    "trigger_reasons": reasons,
                    "status": "exception",
                    "error": str(cause),
                }
                history.append(failed)
                _disclose_fallback_attempt(node.node_id, failed)
        return None, history

    # -- the S5a node bodies (engine-internal executors; T-C1-080) --------------

    async def _human_gate(self, node: PipelineNode, state: EngineState) -> JsonValue:
        """The real ``human_gate`` body: pause for a human reply, resume on it.

        First look for an already-resolved reply (the resume drive after an
        approve/revise): a resolution present means the human already
        answered, and the reply becomes the node's result -- the graph
        continues. Otherwise the node gates: a job entering this body as
        ``gated`` (a resume without a reply -- a recovery drive, or a plain
        re-drive) first re-enters ``running`` so the fresh gate below is a
        legal transition, then the service mints the gate (id unique within
        the job, 300 s expiry, the conversation ref on the envelope for the
        card's routing -- the card rides the existing ``py.job.gated``
        event over the job's control channel, never the brain) and the body
        raises :class:`EnginePause`: the drive returns, the job stays
        ``gated`` with the cursor holding the node position.
        """
        broker = self._gate_broker
        assert broker is not None  # the node_fn dispatch guarded this
        context = DRIVE_CONTEXT.get()
        if context is None:
            raise WorkerError(
                "the human_gate node body runs outside a drive context",
                code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
            )
        resolution = broker.take(context.job_id)
        if resolution is not None:
            return resolution.node_result()
        record = await context.service.get_job(context.job_id)
        if record.status == "gated":
            # A resume drive entered without a reply: re-enter running so
            # the fresh gate below is the legal running -> gated transition.
            await context.service.emit_started(context.job_id)
        await context.service.emit_gated(context.job_id, conversation_ref=context.session_anchor)
        raise EnginePause(
            f"job {context.job_id} gated on node '{node.node_id}': waiting for a human reply (approve/reject/revise)"
        )

    async def _subworkflow(self, node: PipelineNode, state: EngineState) -> JsonValue:
        """The real ``subworkflow`` body: drive a nested job in-process.

        The nested spec must be registered with this same engine (the S2
        three-layer gate + §5.1 hash already ran at its registration). The
        nested job is an independent job record: lineage parent = this
        job's id, depth = this job's depth + 1 -- the M-9 lineage guard
        enforces the ancestor chain and the ≤3 cap on the nested submit, so
        a too-deep recursion surfaces here as the guard's structured 4xx
        wrapped into the node failure. Execution is in-process engine
        recursion (PHASE3 §12: never a subprocess): ``service.run`` drives
        the nested graph over the same checkpointer file under its own
        thread id. A nested job parked on its own human gate legitimately
        parks this body too (the poll loop below) until the reply resolves
        it. The node's result carries the nested job's identity, terminal
        status and result, so downstream nodes and tests can walk the
        chain. Restart mid-node is NOT idempotent in v1 (the nested job id
        cannot ride the graph state before the node completes -- disclosed
        leftover, CORE-CONTRACTS §5.4 S5a).
        """
        context = DRIVE_CONTEXT.get()
        if context is None:
            raise WorkerError(
                "the subworkflow node body runs outside a drive context",
                code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
            )
        assert node.workflow_ref is not None  # the semantic gate proved it
        anchor = context.session_anchor
        lineage = JobLineage(
            parent_job_id=context.job_id,
            session_id=None if anchor == "api" else anchor,
            origin="api" if anchor == "api" else "session",
        )
        handle = await context.service.submit(node.workflow_ref, {}, anchor, depth=context.depth + 1, lineage=lineage)
        await context.service.run(handle.job_id)
        while True:
            record = await context.service.get_job(handle.job_id)
            if record.status in TERMINAL_STATUSES:
                break
            await asyncio.sleep(_SUBWORKFLOW_POLL_S)
        if record.status == "cancelled":
            # The cancel seam: the fold already committed the terminal
            # outcome (a parent cancel propagated here) -- end the drive
            # like the S3 WorkerSuspended face, never touching the status.
            raise WorkerSuspended(f"nested job {handle.job_id} was cancelled")
        if record.status == "failed":
            error = record.error if record.error is not None else {}
            message = str(error.get("message") or f"nested job {handle.job_id} failed")
            try:
                code = CoreErrorCode(str(error.get("code")))
            except ValueError:
                code = CoreErrorCode.TOOL_FAILED
            raise WorkerError(f"subworkflow node '{node.node_id}' failed: {message}", code=code)
        return {"job_id": handle.job_id, "status": record.status, "result": record.result}

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
            prior_status = record.status
            try:
                await handle.started()
            except JobTransitionError:
                # started is legal only from created or gated (the S5a gate
                # reply chain): an already-running job is idempotent re-entry
                # (continue from the cursor); a terminal job is a lost race
                # (silent no-op); anything else re-raises.
                record = await service.get_job(handle.job_id)
                if record.status in TERMINAL_STATUSES:
                    return
                if record.status != "running":
                    raise
                resume_mode = True
            else:
                # A started-over-gated drive (S5a) is a re-entry, not a fresh
                # start: the gate cycle left the advisory cursor at the gate
                # node, so the drive resumes from it instead of restarting
                # the graph channels from the submitted state.
                if prior_status == "gated":
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
            # The S5a drive context: the engine-internal node bodies (the
            # human_gate emits, the subworkflow nested submit) read the
            # service and the job's identity facts from this ContextVar --
            # set around the ainvoke so a nested drive's context restores
            # the parent's on exit (token discipline).
            drive_token = DRIVE_CONTEXT.set(
                DriveContext(
                    service=service,
                    job_id=handle.job_id,
                    depth=record.depth,
                    session_anchor=record.session_anchor if record.session_anchor is not None else "api",
                )
            )
            try:
                if resume_mode:
                    final = await bound.graph.ainvoke(None, cast(Any, config))
                else:
                    final = await bound.graph.ainvoke(state0, cast(Any, config))
            finally:
                AGENT_JOB_CONTEXT.reset(context_token)
                DRIVE_CONTEXT.reset(drive_token)
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
            # code. A WorkerError carries its own code and wins first (S4's
            # llm body raises structured ValidationError/ProviderUnavailable
            # faces); the unstructured rest defaults by node type
            # (llm_call -> ProviderUnavailable, everything else -> ToolFailed).
            node = failure.node
            if isinstance(failure.__cause__, WorkerError):
                code = failure.__cause__.code
            elif node.node_type == "llm_call":
                code = CoreErrorCode.PROVIDER_UNAVAILABLE
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
