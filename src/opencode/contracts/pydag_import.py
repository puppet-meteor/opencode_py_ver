"""The pydag DSL import face (T-C1-083): ``graph.json`` -> :class:`PipelineSpec`.

The ported pipeline contract speaks one wire dialect -- the v1 ``graph.json``
of ``opencode.contracts.pipelines`` (the smart layer -> execution layer
hand-off artifact, PHASE3 §5.2). The ``agent_dsl_flow_framework`` relay
skills carry a *different* wire dialect: ``pydag-editable-graph-v1``
(evidence: ``relay/skills/gocm-sop/pipeline.graph.json`` (full form),
``relay/skills/dws-disk-diagnosis/pipeline.graph.json`` (minified form),
``relay/dag_engine/dsl/pydag/step_def.py`` (the step schema)). This module
is the syntax-level fusion of that dialect into the ported contract: what
v1 can express converts, everything beyond it is rejected with structured
diagnostics and the import fails -- the *semantics* of the rejected
features land with v2 (T-C1-084).

The mapping (grounded in the evidence graphs):

* header -- ``pipeline.name`` becomes ``pipeline_id``; ``schema_version``
  becomes the ported integer ``1``. ``pipeline.description``,
  ``execution_mode`` and ``skill_name`` have no ported field and are
  ignored (the ported header carries no description or execution mode).
* params surface -- ``pipeline.input_vars`` is the declared params face.
  The ported spec *does* have the declaration field (``params_schema``,
  the P3-S4/T-C1-079 face the gateway validates submits against with zero
  model involvement), so ``input_vars`` maps there: an object schema whose
  properties are the declared vars, all required, unknown fields rejected
  (fail-closed like the empty default). The dialect declares names only,
  never types; the import pins each var to ``string`` -- the strict
  validator subset (``opencode.tools.invoker``) has no unconstrained
  property form, and the dialect itself coerces input refs through
  ``str()`` at prompt-substitution time, so a non-string input is honestly
  a submit-time 4xx rather than a silent coercion.
* nodes -- ``tool == "builtin.compute"`` with an ``llm_call`` parameter
  becomes an ``llm_call`` node; every other named tool becomes a ``tool``
  node carrying the tool name as ``tool_ref`` (the ported v1 tool node is
  an identified invocation -- its body is the injected executor seam, and
  the dialect's tool names are an open set, so no name is "unknown" per
  se). ``builtin.compute`` *without* an llm prompt is a pure compute step:
  the ported closed node set (llm_call/tool) has no such node kind, so it
  is rejected with ``PYDAG_UNKNOWN_TOOL``.
* llm prompts -- the dialect's ``{{ref}}`` template refs convert into the
  ported ``str.format`` keys: the declared input vars (the ``{{inputs.x}}``
  form and the bare ``{{x}}`` form both become ``{x}``) and, since v2
  (T-C1-084), step refs (``{{step_2.sop_data}}`` becomes
  ``{step_2[sop_data]}`` -- the engine's render context merges the
  non-skipped node results, and ``str.format``'s index chains step into
  them; a root-only ``{{step_2}}`` renders the whole result). Nested
  input paths (``{{inputs.x.y}}``) and unknown names stay
  ``PYDAG_REF_UNSUPPORTED``. The ported ``prompt_template`` uses
  ``str.format`` keys, so the conversion also escapes every residual
  literal brace (``{`` -> ``{{``) -- the rendered template is format-safe
  by construction, and a literal brace in the source prompt round-trips
  through ``str.format`` unchanged. The dialect declares no model on llm
  steps; the import pins the ported small-model tier (``small`` -- the
  ``LlmCallFn`` face is the small-model critique/verify/summarize seam,
  PHASE3 §11 S2; per-tier selection lands with the S4 wiring).
* tool params -- since v2 (T-C1-084) the converted node carries them on
  ``params``: literals pass through verbatim, and ``{"ref": ...}`` values
  convert when their root binds -- ``inputs.<declared var>`` or a known
  step id of this graph (the engine resolves them strictly at node entry).
  A ref with any other root is ``PYDAG_REF_UNSUPPORTED`` (the
  lossy-conversion rule: what the converted node cannot bind is rejected,
  never guessed). The ``context_bundle``/``template`` parameter modes
  remain beyond the import surface, and the llm payload key itself never
  lands on ``params``.
* llm schema/fallback -- since v2 the llm payload's ``schema`` converts to
  the node's ``output_schema`` (structured output) and ``fallback`` to the
  node's degrade object; a fallback without a schema is still
  ``PYDAG_UNSUPPORTED_FIELD`` (it only engages on schema-validation
  failure). Whether the schema sits inside the strict validator subset
  the engine's gate supports is the registration gate's check, not this
  module's.
* step condition/extract -- since v2 they convert: ``condition`` (a
  non-blank string; the engine's ported safe-subset evaluator decides
  skip/execute) and ``extract`` (output name -> dot path, or an ordered
  fallback chain; unresolved paths project ``null``). Malformed shapes
  are structural rejections.
* edges -- only ``pydagDependency`` edges with ``data.kind ==
  "depends_on"`` are topology; they become spec edges verbatim
  (source/target). ``pydagReference`` edges (``kind: param_ref``) are the
  canvas mirror of step-output parameter refs -- ignored editor state
  since v2 (the refs themselves are validated where they bind, on the
  nodes' params/prompts), and any other edge shape is malformed.
* entry -- the dialect declares no entry; the ported spec requires exactly
  one. The entry is computed as the unique node with no incoming
  dependency edges; zero or multiple roots is a structural rejection (the
  ported unique-entry rule, surfaced at import time instead of at the
  semantic gate).
* canvas metadata -- ``position``, ``data.label``, ``data.ui``,
  ``data.diagnostics``, ``data.provenance``, ``data.param_resolution``,
  ``viewport`` and edge ids are editor state, ignored by the converter.

Diagnostics: every rejection carries ``{code, node_id, field, message}``
(node_id is None for pipeline-level findings) and the import fails with
:class:`PydagImportError` carrying *all* collected diagnostics in
deterministic order (header, nodes in declaration order, edges in order,
then the depends_on consistency and entry checks). Codes:

* ``PYDAG_STRUCTURAL`` -- the graph is malformed or not representable in
  the ported topology (wrong schema_version, missing/malformed sections,
  step shape violations, duplicate edges, unknown edge endpoints,
  depends_on/edge mismatches, no unique entry);
* ``PYDAG_UNSUPPORTED_FIELD`` -- a recognized field carries a feature
  beyond the ported v2 import surface: ``transform`` /
  ``output_format`` / ``instance_binding`` / ``fallback_tools``, a
  non-default ``output_mode``, an unknown step key, an llm ``fallback``
  without a ``schema``, a non-string llm prompt (the
  structured template form), ``llm_call`` on a non-compute tool, and the
  ``context_bundle``/``template`` parameter modes;
* ``PYDAG_UNKNOWN_TOOL`` -- a tool with no ported node kind (a
  ``builtin.compute`` step without an llm prompt);
* ``PYDAG_REF_UNSUPPORTED`` -- a ``{"ref": ...}`` parameter value whose
  root binds neither to a declared input var nor to a step of this graph,
  or a prompt ref that names neither a declared input var, nor a step of
  this graph (nested input paths included).

Seam grounding: :func:`parse_pipeline_spec` is the loading-seam router --
``PipelineEngine.register`` is the single place in ``src/`` where a
pipeline file's wire object reaches ``PipelineSpec`` (the jobs store
persists job bindings, not specs; the serving face submits by pipeline
id; the skills catalog deliberately never opens the declared pipeline
file -- the manifest -> engine glue is S5b). The router detects
``schema_version == "pydag-editable-graph-v1"`` and routes through the
converter; a converted spec then flows the unchanged v1 path (the
three-layer gates and the §5.1 spec hash), so a converted-but-semantically
broken graph (orphans, duplicate ids) is still the semantic gate's
rejection, not this module's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from opencode.contracts.messages import JsonObject
from opencode.contracts.pipelines import PipelineEdge, PipelineNode, PipelineSpec

#: The source dialect marker (the framework's ``graph.json`` schema tag).
PYDAG_SCHEMA_VERSION = "pydag-editable-graph-v1"

#: The diagnostic codes (T-C1-083; the closed import-rejection taxonomy).
PYDAG_STRUCTURAL = "PYDAG_STRUCTURAL"
PYDAG_UNSUPPORTED_FIELD = "PYDAG_UNSUPPORTED_FIELD"
PYDAG_UNKNOWN_TOOL = "PYDAG_UNKNOWN_TOOL"
PYDAG_REF_UNSUPPORTED = "PYDAG_REF_UNSUPPORTED"

#: The model tier the import pins on converted llm nodes (the dialect
#: declares no model; the ported ``LlmCallFn`` face is the small-model
#: critique/verify/summarize seam -- PHASE3 §11 S2).
PYDAG_LLM_MODEL_TIER = "small"

#: The dialect's llm engine tool name (a compute step's params carry the
#: ``llm_call`` payload; a compute step without one has no ported kind).
_BUILTIN_COMPUTE = "builtin.compute"

#: The ported node kind floor: the dialect's only node type.
_NODE_TYPE = "pydagStep"

#: The two edge faces: dependency edges are topology; reference edges are
#: the canvas mirror of step-output parameter refs.
_DEPENDENCY_EDGE_TYPE = "pydagDependency"
_DEPENDS_ON_KIND = "depends_on"
_REFERENCE_EDGE_TYPE = "pydagReference"

#: The default output mode -- the only one the ported graph knows (the
#: node-results channel is an accumulating keyed merge, i.e. "replace").
_DEFAULT_OUTPUT_MODE = "replace"

#: The closed step key set of the dialect's node ``data.step`` payload
#: (step_def.py's StepDef fields the editor serializes, ``fallback_tools``
#: included -- the dws evidence graph carries it). An unknown key is a
#: feature the converter does not know, so it is rejected, never guessed.
_STEP_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "tool",
        "params",
        "output",
        "output_mode",
        "depends_on",
        "extract",
        "transform",
        "output_format",
        "instance_binding",
        "condition",
        "description",
        "fallback_tools",
    }
)

#: The step fields that are recognized by the dialect but beyond the ported
#: v2 import surface whenever they carry a value (None/absent = not used).
#: ``condition`` / ``extract`` / the tool-params face and the llm
#: ``schema``/``fallback`` objects legalized with v2 (T-C1-084);
#: ``output`` and ``description`` are ignored instead: they name and
#: document the step's result, which the ported graph keys by node id.
_STEP_UNSUPPORTED_WHEN_SET: tuple[str, ...] = (
    "transform",
    "output_format",
    "instance_binding",
)

#: A prompt ``{{ref}}`` (the dialect's substitution syntax, pydag_engine's
#: ``runtime_sub``; refs resolve through the context like parameter refs).
_PROMPT_REF = re.compile(r"\{\{([^{}]*)\}\}")

#: An input var name must survive as a ``str.format`` key, so it must be a
#: plain identifier (the evidence graphs' vars all are).
_INPUT_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Sentinel pair for the two-pass prompt conversion: pass one replaces
#: ``{{var}}`` refs with ``\x00var\x01`` (brace-free), pass two escapes the
#: residual literal braces, pass three restores the sentinels as format
#: keys. JSON can carry the sentinels literally, so their presence in a
#: source prompt is an explicit structural rejection (never corruption).
_PROMPT_KEY_OPEN = "\x00"
_PROMPT_KEY_CLOSE = "\x01"

#: The accepted prefix form of an input ref (the ParamDef ref syntax).
_INPUTS_PREFIX = "inputs."


@dataclass(frozen=True, slots=True)
class PydagDiagnostic:
    """One structured import rejection: ``{code, node_id, field, message}``.

    ``field`` is a path relative to the graph (``pipeline.input_vars``,
    ``data.step.tool``, ``edges[0].type``, ``entry``); ``node_id`` is None
    for pipeline-level findings and the offending step's id otherwise.
    """

    code: str
    field: str
    message: str
    node_id: str | None = None


class PydagImportError(ValueError):
    """The rejected import: the diagnostic list is the error face.

    Raised only with a non-empty diagnostic list; carries every diagnostic
    collected (deterministic order) so the caller -- the loading seam or a
    test -- can surface node-level context without re-parsing the graph.
    """

    def __init__(self, diagnostics: list[PydagDiagnostic] | tuple[PydagDiagnostic, ...]) -> None:
        assert diagnostics, "PydagImportError is raised only on a non-empty diagnostic face"
        first = diagnostics[0]
        where = f" on node '{first.node_id}'" if first.node_id is not None else ""
        super().__init__(
            f"pydag graph rejected with {len(diagnostics)} diagnostic(s); "
            f"first: [{first.code}]{where} {first.field}: {first.message}"
        )
        self.diagnostics: tuple[PydagDiagnostic, ...] = tuple(diagnostics)


@dataclass(slots=True)
class _StepScan:
    """One node's conversion outcome (internal): identity + declared deps + the converted node.

    ``node_id`` is the node's valid id even when the conversion was
    rejected (the topology checks anchor on the source graph, not on the
    converted nodes); ``depends_on`` is None when the declared shape was
    unreadable (already diagnosed); ``node`` is None when the step did not
    convert (already diagnosed).
    """

    node_id: str | None
    depends_on: list[str] | None
    node: PipelineNode | None


def convert_pydag_graph(raw: dict[str, Any]) -> PipelineSpec:
    """Parse, validate and convert one ``pydag-editable-graph-v1`` graph object.

    The structural reading and the v1 mapping happen in one pass over the
    graph (header, nodes in declaration order, edges in order, then the
    depends_on consistency and the entry computation); every violation
    collects a :class:`PydagDiagnostic` and, if any were collected, the
    whole import is rejected with :class:`PydagImportError`. On success the
    returned spec is a plain ported v1 spec -- the three-layer gates and
    the §5.1 hash treat it exactly like a hand-authored one.
    """
    diagnostics: list[PydagDiagnostic] = []
    pipeline_id = _header(raw, diagnostics)
    input_vars = _input_vars(raw, diagnostics)
    raw_nodes = _raw_nodes(raw, diagnostics)
    # The v2 ref pre-pass: every ref (params values, prompt step refs) binds
    # against the ids of THIS graph, so all node ids are collected before any
    # node converts (a ref to a step declared later in the file is legal).
    collected_ids: list[str] = []
    for raw_node in raw_nodes:
        if isinstance(raw_node, dict):
            step_node_id = raw_node.get("id")
            if isinstance(step_node_id, str) and step_node_id:
                collected_ids.append(step_node_id)
    node_ids = frozenset(collected_ids)
    scans = [_convert_node(raw_node, frozenset(input_vars), node_ids, diagnostics) for raw_node in raw_nodes]
    edges, incoming = _edges(raw, [scan.node_id for scan in scans if scan.node_id is not None], diagnostics)
    _check_depends_on(scans, incoming, diagnostics)
    entry = _entry(
        [scan.node_id for scan in scans if scan.node_id is not None],
        incoming,
        diagnostics,
    )
    if diagnostics:
        raise PydagImportError(diagnostics)
    nodes = [scan.node for scan in scans if scan.node is not None]
    assert entry != ""  # an invalid entry always collected a diagnostic (raised above)
    return PipelineSpec(
        schema_version=1,
        pipeline_id=pipeline_id,
        entry=entry,
        nodes=nodes,
        edges=edges,
        params_schema=_params_schema(input_vars),
    )


def parse_pipeline_spec(raw: JsonObject) -> PipelineSpec:
    """The loading-seam router (T-C1-083): pick the dialect by ``schema_version``.

    A ``schema_version`` of ``pydag-editable-graph-v1`` routes through the
    pydag converter; every other wire object is the unchanged v1 face (the
    pydantic contract -- a string schema_version that is not the pydag
    marker stays the structural rejection it always was).
    """
    if isinstance(raw, dict) and raw.get("schema_version") == PYDAG_SCHEMA_VERSION:
        return convert_pydag_graph(raw)
    return PipelineSpec.model_validate(raw)


def _header(raw: dict[str, Any], diagnostics: list[PydagDiagnostic]) -> str:
    """The pipeline header: the schema marker and ``pipeline.name`` -> ``pipeline_id``."""
    if raw.get("schema_version") != PYDAG_SCHEMA_VERSION:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                "schema_version",
                f"expected the dialect marker {PYDAG_SCHEMA_VERSION!r}, got {raw.get('schema_version')!r}",
            )
        )
    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, dict):
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "pipeline", "the 'pipeline' section must be a JSON object")
        )
        return ""
    name = pipeline.get("name")
    if not isinstance(name, str) or not name:
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "pipeline.name", "pipeline 'name' must be a non-empty string")
        )
        return ""
    return name


def _input_vars(raw: dict[str, Any], diagnostics: list[PydagDiagnostic]) -> list[str]:
    """The declared params surface: ``pipeline.input_vars`` in declaration order.

    A malformed declaration (non-array, non-string or non-identifier items,
    duplicates) is a structural rejection; the returned list is empty then,
    so the prompt conversion never resolves refs against a broken surface.
    """
    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, dict):
        return []
    declared = pipeline.get("input_vars", [])
    if declared is None:
        declared = []
    if not isinstance(declared, list):
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL, "pipeline.input_vars", "'input_vars' must be an array of input variable names"
            )
        )
        return []
    collected: list[str] = []
    seen: set[str] = set()
    valid = True
    for item in declared:
        if not isinstance(item, str) or _INPUT_VAR_NAME.fullmatch(item) is None:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    "pipeline.input_vars",
                    f"input var {item!r} is not a plain identifier (it must survive as a str.format key)",
                )
            )
            valid = False
            continue
        if item in seen:
            diagnostics.append(
                PydagDiagnostic(PYDAG_STRUCTURAL, "pipeline.input_vars", f"duplicate input var {item!r}")
            )
            valid = False
            continue
        seen.add(item)
        collected.append(item)
    return collected if valid else []


def _raw_nodes(raw: dict[str, Any], diagnostics: list[PydagDiagnostic]) -> list[Any]:
    """The node array, structurally checked (non-empty array of JSON values)."""
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        diagnostics.append(PydagDiagnostic(PYDAG_STRUCTURAL, "nodes", "'nodes' must be a non-empty array"))
        return []
    return raw_nodes


def _convert_node(
    raw_node: Any,
    input_vars: frozenset[str],
    node_ids: frozenset[str],
    diagnostics: list[PydagDiagnostic],
) -> _StepScan:
    """One ``pydagStep`` -> one ported node (or the diagnostics that reject it)."""
    if not isinstance(raw_node, dict):
        diagnostics.append(PydagDiagnostic(PYDAG_STRUCTURAL, "nodes", "each node must be a JSON object"))
        return _StepScan(None, None, None)
    node_id = raw_node.get("id")
    if not isinstance(node_id, str) or not node_id:
        diagnostics.append(PydagDiagnostic(PYDAG_STRUCTURAL, "nodes[].id", "node 'id' must be a non-empty string"))
        return _StepScan(None, None, None)
    if raw_node.get("type") != _NODE_TYPE:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                "nodes[].type",
                f"node type must be '{_NODE_TYPE}', got {raw_node.get('type')!r}",
                node_id=node_id,
            )
        )
        return _StepScan(node_id, None, None)
    data = raw_node.get("data")
    if not isinstance(data, dict):
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "nodes[].data", "node 'data' must be a JSON object", node_id=node_id)
        )
        return _StepScan(node_id, None, None)
    step = data.get("step")
    if not isinstance(step, dict):
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "data.step", "the step payload must be a JSON object", node_id=node_id)
        )
        return _StepScan(node_id, None, None)
    step_id = step.get("id")
    if not isinstance(step_id, str) or not step_id:
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "data.step.id", "step 'id' must be a non-empty string", node_id=node_id)
        )
        return _StepScan(node_id, None, None)
    if step_id != node_id:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                "data.step.id",
                f"step id '{step_id}' does not match node id '{node_id}' (the editor keeps them equal)",
                node_id=node_id,
            )
        )
    tool = step.get("tool")
    if not isinstance(tool, str) or not tool:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL, "data.step.tool", "step 'tool' must be a non-empty string", node_id=node_id
            )
        )
        return _StepScan(node_id, None, None)

    # The closed step key set: an unknown key is a feature this converter
    # does not know -- rejected, never guessed at.
    for key in step:
        if key not in _STEP_KEYS:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNSUPPORTED_FIELD,
                    f"data.step.{key}",
                    f"unknown step field '{key}' is beyond the pydag v1 import surface",
                    node_id=node_id,
                )
            )
    # The recognized-but-beyond-v1 step fields (None/absent = not used).
    for field in _STEP_UNSUPPORTED_WHEN_SET:
        if step.get(field) is not None:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNSUPPORTED_FIELD,
                    f"data.step.{field}",
                    f"step field '{field}' is beyond ported v1 (legalized with v2, T-C1-084)",
                    node_id=node_id,
                )
            )
    fallback_tools = step.get("fallback_tools")
    if fallback_tools is not None and fallback_tools != []:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_UNSUPPORTED_FIELD,
                "data.step.fallback_tools",
                "step fallback tools are beyond ported v1 (legalized with v2, T-C1-084)",
                node_id=node_id,
            )
        )
    output_mode = step.get("output_mode")
    if output_mode is not None and output_mode != _DEFAULT_OUTPUT_MODE:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_UNSUPPORTED_FIELD,
                "data.step.output_mode",
                f"output_mode {output_mode!r} is beyond ported v1 (only '{_DEFAULT_OUTPUT_MODE}' converts)",
                node_id=node_id,
            )
        )

    # depends_on: shape now, edge consistency after the edges are known.
    depends_on = step.get("depends_on", [])
    if depends_on is None:
        depends_on = []
    if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL, "data.step.depends_on", "'depends_on' must be an array of node ids", node_id=node_id
            )
        )
        depends_on = None

    # params: the v2 carrying face. Literals pass through onto the node's
    # ``params``; ``{"ref": ...}`` values convert when their root binds
    # (inputs.<declared var> or a known step id of this graph); the other
    # ParamDef modes and malformed refs are diagnosed.
    params = step.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "data.step.params", "'params' must be a JSON object", node_id=node_id)
        )
        params = {}
    llm_key: str | None = None
    carried: dict[str, Any] = {}
    for key, value in params.items():
        if not isinstance(value, dict):
            carried[key] = value  # a literal scalar/container: the pass-through case
            continue
        if set(value) == {"ref"} and isinstance(value.get("ref"), str) and value["ref"]:
            ref: str = value["ref"]
            if _param_ref_binds(ref, input_vars, node_ids):
                carried[key] = {"ref": ref}
            else:
                diagnostics.append(
                    PydagDiagnostic(
                        PYDAG_REF_UNSUPPORTED,
                        f"data.step.params.{key}",
                        f"parameter ref {ref!r} binds neither to a declared input var nor to a step of this graph",
                        node_id=node_id,
                    )
                )
            continue
        if "ref" in value:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    f"data.step.params.{key}",
                    "a parameter ref must be exactly a single 'ref' key with a non-empty string path",
                    node_id=node_id,
                )
            )
            continue
        if "context_bundle" in value or "template" in value:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNSUPPORTED_FIELD,
                    f"data.step.params.{key}",
                    "the context_bundle/template parameter modes are beyond the ported v2 import surface",
                    node_id=node_id,
                )
            )
            continue
        if "llm_call" in value:
            if llm_key is not None:
                diagnostics.append(
                    PydagDiagnostic(
                        PYDAG_STRUCTURAL,
                        f"data.step.params.{key}.llm_call",
                        "only one llm_call parameter per step converts",
                        node_id=node_id,
                    )
                )
            llm_key = key
            continue
        carried[key] = value  # a literal JSON object: the pass-through case

    # The v2 step fields (T-C1-084): condition and extract convert with their
    # shapes checked; the carried params land on the node when non-empty.
    extra: dict[str, Any] = {}
    condition = step.get("condition")
    if condition is not None:
        if not isinstance(condition, str) or not condition.strip():
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    "data.step.condition",
                    "step 'condition' must be a non-blank string",
                    node_id=node_id,
                )
            )
        else:
            extra["condition"] = condition
    extract = step.get("extract")
    if extract is not None:
        converted_extract = _convert_extract(extract, node_id, diagnostics)
        if converted_extract is not None:
            extra["extract"] = converted_extract
    if carried:
        extra["params"] = carried

    # The tool mapping: the closed ported node set has exactly one shape
    # per dialect tool face.
    node: PipelineNode | None = None
    if tool == _BUILTIN_COMPUTE:
        if llm_key is None:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNKNOWN_TOOL,
                    "data.step.tool",
                    f"'{_BUILTIN_COMPUTE}' without an llm_call parameter is a pure compute step: "
                    "the ported v1 node set (llm_call/tool) has no such node kind",
                    node_id=node_id,
                )
            )
        else:
            node = _llm_node(node_id, params, llm_key, input_vars, node_ids, extra, diagnostics)
    else:
        if llm_key is not None:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNSUPPORTED_FIELD,
                    f"data.step.params.{llm_key}.llm_call",
                    "llm_call parameters convert only on 'builtin.compute' steps",
                    node_id=node_id,
                )
            )
        node = PipelineNode(node_id=node_id, node_type="tool", tool_ref=tool, **extra)
    return _StepScan(node_id, depends_on, node)


def _param_ref_binds(ref: str, input_vars: frozenset[str], node_ids: frozenset[str]) -> bool:
    """A params ref root closure check: ``inputs.<declared var>`` or a step id."""
    root, _, tail = ref.partition(".")
    if root == "inputs":
        head = tail.split(".", 1)[0]
        return bool(tail) and head in input_vars
    return root in node_ids


def _convert_extract(
    raw: Any,
    node_id: str,
    diagnostics: list[PydagDiagnostic],
) -> dict[str, str | list[str]] | None:
    """The ``extract`` projection face: output name -> dot path (or fallback chain).

    Malformed shapes (a non-object, non-string/empty paths, empty dot
    segments, empty chains) are structural rejections; an empty object is the
    editor's unused marker and converts as absent (None).
    """
    if not isinstance(raw, dict):
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                "data.step.extract",
                "'extract' must be a JSON object of output name -> dot path",
                node_id=node_id,
            )
        )
        return None
    if not raw:
        return None  # the editor's unused marker: absent, not a rejection
    converted: dict[str, str | list[str]] = {}
    ok = True
    for key, chain in raw.items():
        paths = [chain] if isinstance(chain, str) else chain
        if not isinstance(paths, list) or not paths:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    f"data.step.extract.{key}",
                    "each projection must be a non-empty dot path or a non-empty fallback chain of them",
                    node_id=node_id,
                )
            )
            ok = False
            continue
        good_paths: list[str] = []
        for path in paths:
            if not isinstance(path, str) or not path or any(not segment for segment in path.split(".")):
                diagnostics.append(
                    PydagDiagnostic(
                        PYDAG_STRUCTURAL,
                        f"data.step.extract.{key}",
                        f"extract path {path!r} is not a non-empty dot path",
                        node_id=node_id,
                    )
                )
                ok = False
                continue
            good_paths.append(path)
        if not good_paths:
            # every path in the chain was malformed: the diagnostics
            # already fired -- never index into the empty chain.
            continue
        converted[key] = good_paths[0] if isinstance(chain, str) else good_paths
    return converted if ok else None


def _llm_node(
    node_id: str,
    params: dict[str, Any],
    llm_key: str,
    input_vars: frozenset[str],
    node_ids: frozenset[str],
    extra: dict[str, Any],
    diagnostics: list[PydagDiagnostic],
) -> PipelineNode | None:
    """The ``builtin.compute`` + llm_call mapping -> one ``llm_call`` node."""
    base = f"data.step.params.{llm_key}.llm_call"
    payload = params[llm_key].get("llm_call") if isinstance(params[llm_key], dict) else None
    if not isinstance(payload, dict):
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, base, "the llm_call payload must be a JSON object", node_id=node_id)
        )
        return None
    schema = payload.get("schema")
    if schema is not None:
        # v2 (T-C1-084): the structured-output schema converts. Shape only --
        # the strict-subset check is the engine's registration gate.
        if not isinstance(schema, dict):
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL, f"{base}.schema", "the llm schema must be a JSON object", node_id=node_id
                )
            )
        else:
            extra["output_schema"] = schema
    fallback = payload.get("fallback")
    if fallback is not None:
        if not isinstance(schema, dict):
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_UNSUPPORTED_FIELD,
                    f"{base}.fallback",
                    "llm fallback without a structured-output schema is beyond the import surface "
                    "(fallback converts only together with a schema)",
                    node_id=node_id,
                )
            )
        else:
            extra["fallback"] = fallback
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_UNSUPPORTED_FIELD,
                f"{base}.prompt",
                "the llm prompt must be a non-empty string (the structured template form is beyond ported v1)",
                node_id=node_id,
            )
        )
        return None
    if _PROMPT_KEY_OPEN in prompt or _PROMPT_KEY_CLOSE in prompt:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                f"{base}.prompt",
                "the prompt contains control characters the converter cannot round-trip",
                node_id=node_id,
            )
        )
        return None
    template = _convert_prompt(prompt, input_vars, node_ids, f"{base}.prompt", node_id, diagnostics)
    if template is None:
        return None
    return PipelineNode(
        node_id=node_id,
        node_type="llm_call",
        model=PYDAG_LLM_MODEL_TIER,
        prompt_template=template,
        **extra,
    )


def _convert_prompt(
    prompt: str,
    input_vars: frozenset[str],
    node_ids: frozenset[str],
    field: str,
    node_id: str,
    diagnostics: list[PydagDiagnostic],
) -> str | None:
    """``{{ref}}`` refs -> ``str.format`` keys; residual braces escaped.

    The input ref forms convert (``{{inputs.x}}`` and the bare ``{{x}}``
    where ``x`` is a declared input var, both to ``{x}``); since v2 step
    refs convert to index chains (``{{step_2.sop_data}}`` ->
    ``{step_2[sop_data]}``, a root-only ``{{step_2}}`` to ``{step_2}`` --
    the engine's render context carries the non-skipped node results).
    Anything else -- unknown names, nested input paths (``{{inputs.x.y}}``),
    malformed paths -- collects a ``PYDAG_REF_UNSUPPORTED`` diagnostic and
    the caller's import fails.
    """
    failed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal failed
        ref = match.group(1).strip()
        name: str | None = None
        if ref in input_vars:
            name = ref
        elif ref.startswith(_INPUTS_PREFIX):
            tail = ref[len(_INPUTS_PREFIX) :]
            if tail in input_vars and "." not in tail:
                name = tail
        if name is not None:
            return _PROMPT_KEY_OPEN + name + _PROMPT_KEY_CLOSE
        root, _, tail = ref.partition(".")
        segments = tail.split(".") if tail else []
        bad_segment = any(not segment or any(ch in segment for ch in "][{}") for segment in segments)
        if (
            root in node_ids
            and root.isidentifier()
            and not bad_segment
            and not ref.startswith(".")
            and not ref.endswith(".")
        ):
            # The v2 step-ref form: {step_2[sop_data]} index chains (the
            # root-only form renders the node's whole result).
            return _PROMPT_KEY_OPEN + root + "".join(f"[{segment}]" for segment in segments) + _PROMPT_KEY_CLOSE
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_REF_UNSUPPORTED,
                field,
                f"prompt ref '{{{{{ref}}}}}' names neither a declared input var, nor a step of "
                "this graph (nested input paths are beyond the import surface)",
                node_id=node_id,
            )
        )
        failed = True
        return match.group(0)

    keyed = _PROMPT_REF.sub(replace, prompt)
    if failed:
        return None
    escaped = keyed.replace("{", "{{").replace("}", "}}")
    return escaped.replace(_PROMPT_KEY_OPEN, "{").replace(_PROMPT_KEY_CLOSE, "}")


def _edges(
    raw: dict[str, Any],
    node_ids: list[str],
    diagnostics: list[PydagDiagnostic],
) -> tuple[list[PipelineEdge], dict[str, set[str]]]:
    """The edge array -> spec edges plus the incoming-dependency index.

    Only ``pydagDependency``/``depends_on`` edges are topology;
    ``pydagReference`` edges (the canvas mirror of step-output parameter
    refs) are ignored editor state (the refs bind on the nodes), and any
    other edge shape is malformed.
    """
    raw_edges = raw.get("edges", [])
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        diagnostics.append(PydagDiagnostic(PYDAG_STRUCTURAL, "edges", "'edges' must be an array"))
        return [], {}
    known = set(node_ids)
    edges: list[PipelineEdge] = []
    incoming: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for index, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, dict):
            diagnostics.append(PydagDiagnostic(PYDAG_STRUCTURAL, f"edges[{index}]", "each edge must be a JSON object"))
            continue
        edge_type = raw_edge.get("type")
        if edge_type == _REFERENCE_EDGE_TYPE:
            # v2 (T-C1-084): reference edges are the canvas mirror of the
            # step-output refs the nodes themselves carry -- the refs are
            # validated where they bind (params/prompts on the nodes), and
            # the canvas mirror is ignored editor state like position/label.
            continue
        if edge_type != _DEPENDENCY_EDGE_TYPE:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    f"edges[{index}].type",
                    f"unknown edge type {edge_type!r}; only '{_DEPENDENCY_EDGE_TYPE}' converts",
                )
            )
            continue
        data = raw_edge.get("data")
        if not isinstance(data, dict) or data.get("kind") != _DEPENDS_ON_KIND:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    f"edges[{index}].data.kind",
                    f"a dependency edge must carry data.kind '{_DEPENDS_ON_KIND}'",
                )
            )
            continue
        source = raw_edge.get("source")
        target = raw_edge.get("target")
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            diagnostics.append(
                PydagDiagnostic(PYDAG_STRUCTURAL, f"edges[{index}]", "edge 'source'/'target' must be non-empty strings")
            )
            continue
        for endpoint in (source, target):
            if endpoint not in known:
                diagnostics.append(
                    PydagDiagnostic(PYDAG_STRUCTURAL, f"edges[{index}]", f"edge references unknown node '{endpoint}'")
                )
        if (source, target) in seen:
            diagnostics.append(
                PydagDiagnostic(PYDAG_STRUCTURAL, f"edges[{index}]", f"duplicate edge {source} -> {target}")
            )
            continue
        seen.add((source, target))
        incoming.setdefault(target, set()).add(source)
        edges.append(PipelineEdge(from_node=source, to_node=target))
    return edges, incoming


def _check_depends_on(
    scans: list[_StepScan],
    incoming: dict[str, set[str]],
    diagnostics: list[PydagDiagnostic],
) -> None:
    """The declared/edge consistency rule: ``depends_on`` == incoming edges, per node."""
    for scan in scans:
        if scan.node_id is None or scan.depends_on is None:
            continue  # already diagnosed structurally
        declared = set(scan.depends_on)
        actual = incoming.get(scan.node_id, set())
        if declared != actual:
            diagnostics.append(
                PydagDiagnostic(
                    PYDAG_STRUCTURAL,
                    "data.step.depends_on",
                    f"declared depends_on {sorted(declared)} does not match the dependency edges {sorted(actual)}",
                    node_id=scan.node_id,
                )
            )


def _entry(node_ids: list[str], incoming: dict[str, set[str]], diagnostics: list[PydagDiagnostic]) -> str:
    """The unique entry: the one node with no incoming dependency edges.

    The dialect declares no entry; the ported unique-entry rule surfaces
    here -- zero roots (a full cycle) or multiple roots (a forest) are not
    representable as one ported pipeline.
    """
    roots = [node_id for node_id in dict.fromkeys(node_ids) if node_id not in incoming]
    if not roots:
        diagnostics.append(
            PydagDiagnostic(PYDAG_STRUCTURAL, "entry", "every node has incoming dependency edges: no entry candidate")
        )
        return ""
    if len(roots) > 1:
        diagnostics.append(
            PydagDiagnostic(
                PYDAG_STRUCTURAL,
                "entry",
                f"multiple entry candidates (nodes with no incoming dependency edges): {sorted(roots)}",
            )
        )
        return ""
    return roots[0]


def _params_schema(input_vars: list[str]) -> JsonObject:
    """The declared params face built from ``input_vars`` (see the module docstring)."""
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in input_vars},
        "required": list(input_vars),
        "additionalProperties": False,
    }
