"""The pipeline spec wire contract (PHASE3 §5.2/§11 S2; CORE-CONTRACTS §5.4, T-C1-077).

The pipeline spec (``graph.json``) is the single hand-off artifact between the
smart layer and the execution layer (PHASE3 §3 discipline 2: disputes are
settled by the artifact, not by negotiation). This module pins its wire shape:

* the closed node-type set -- ``llm_call`` / ``opencode_agent`` / ``tool`` /
  ``human_gate`` / ``subworkflow`` (PHASE3 §5.2 v1 five). S2 executes only
  ``llm_call``/``tool``; the other three are *recognized* names (an unknown
  type is a structural rejection here) whose execution is an explicit
  semantic-gate rejection delivered by S3/S5a, never a silent skip;
* ``schema_version`` (int, bool rejected before coercion per CORE-CONTRACTS §1)
  -- the registered-pipeline validation field of PHASE3 §5.1;
* the spec hash: sha256 over the canonical JSON of the serialized spec (the
  §6.2 canonical-JSON/SHA-256 recipe), computed at load time and carried into
  the job binding chain (PHASE3 §5.1 hash lock; running graphs never hot-rebind).

Topology constraints (unique entry, edge-reference closure, no orphans,
per-type field requirements, the subworkflow depth cap) are *semantic-layer*
rules -- they live in the engine's gate (``opencode.orchestrator.engine``),
not in the pydantic structural layer: a spec that parses but fails them is a
semantic rejection with a structured error, which is exactly what the
three-layer gate (structure -> semantic -> rehearsal) tests pin.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opencode.contracts.events import canonical_json
from opencode.contracts.messages import JsonObject

#: The closed v1 node-type set (PHASE3 §5.2). The wire contract recognizes all
#: five names; S2 implements executors for the first two only -- the engine's
#: semantic gate rejects the other three with an explicit
#: "unimplemented node type" error naming the S3/S5a delivery.
PIPELINE_NODE_TYPES: tuple[str, ...] = (
    "llm_call",
    "opencode_agent",
    "tool",
    "human_gate",
    "subworkflow",
)

NodeType = Literal["llm_call", "opencode_agent", "tool", "human_gate", "subworkflow"]

#: The hard cap on workflow nesting (PHASE3 §5.2: subworkflow depth <= 3; the
#: lineage guard is the gateway-side first line, this is the spec-side line).
MAX_SUBWORKFLOW_DEPTH = 3


def _reject_bool(field: str, value: Any) -> Any:
    """CORE-CONTRACTS §1 repo rule: integer-semantic fields reject bools pre-coercion.

    Local twin of the events/messages helpers (importing events' copy would be
    fine here, but each contracts module keeps the repo rule self-contained;
    the behavior is identical and pinned by tests).
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    return value


class PipelineNode(BaseModel):
    """One spec node: identity + closed-set type + the per-type constraint fields.

    The per-type fields (``model``/``prompt_template`` for ``llm_call``,
    ``command``/``tool_ref`` for ``tool``, ``depth`` for ``subworkflow``) are
    *optional on the wire*: whether a node of a given type carries the right
    ones is a semantic-layer rule, so a spec with an ``llm_call`` node missing
    its model tier parses here and is rejected by the engine's semantic gate
    with a structured error (CORE-CONTRACTS §8 shape) -- the layered-gate
    contract tests pin that split.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    node_type: NodeType
    model: str | None = Field(default=None, min_length=1)
    prompt_template: str | None = Field(default=None, min_length=1)
    command: list[str] | None = None
    tool_ref: str | None = Field(default=None, min_length=1)
    workflow_ref: str | None = Field(default=None, min_length=1)
    depth: int | None = Field(default=None, ge=0)

    @field_validator("depth", mode="before")
    @classmethod
    def _depth_not_bool(cls, value: Any) -> Any:
        return _reject_bool("depth", value)


class PipelineEdge(BaseModel):
    """One spec edge: a plain sequential/conditional topology edge (v1: from -> to).

    Conditions land with conditional routing; v1's wire face carries the two
    endpoints only (CORE-CONTRACTS §8: no same-version optional-field growth).
    """

    model_config = ConfigDict(extra="forbid")

    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)


class PipelineSpec(BaseModel):
    """One registered pipeline (the ``graph.json`` wire shape, PHASE3 §5.2).

    ``schema_version`` is the registered-pipeline validation field (§5.1):
    the engine loads specs at v1 only. ``entry`` is the single entry node id;
    the semantic gate enforces that it exists, is unique (no incoming edges)
    and that no node is orphaned from it.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    pipeline_id: str = Field(min_length=1)
    entry: str = Field(min_length=1)
    nodes: list[PipelineNode] = Field(min_length=1)
    edges: list[PipelineEdge] = Field(default_factory=list)
    #: The declared params face (P3-S4, T-C1-079): a JSON Schema (draft
    #: 2020-12 subset) the gateway validates submitted params against with
    #: zero model involvement (missing/type-error/unknown fields -> structured
    #: 4xx, PHASE3 §1 condition 4). Optional on the wire: a spec without it
    #: accepts the empty object only (fail-closed default, CORE-CONTRACTS
    #: §5.4 S4). Whether a declared schema stays inside the strict subset the
    #: gateway validator supports is a registration-time/submit-time gate,
    #: not wire shape.
    params_schema: JsonObject | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    def node_ids(self) -> list[str]:
        """The node ids in declaration order (duplicates are semantic-gate land)."""
        return [node.node_id for node in self.nodes]


def compute_spec_hash(spec: PipelineSpec) -> str:
    """The pipeline spec hash: sha256 over the canonical JSON of the whole spec.

    The §6.2 recipe (sorted keys, no whitespace, UTF-8, NaN rejected) over the
    model's JSON serialization, so the hash is byte-stable across processes
    and platforms. Computed at load time, before any execution, and pinned by
    contract tests; a re-registered spec with a different hash never rebinds a
    running graph (PHASE3 §5.2: cancel -> revise -> resubmit instead).
    """
    canonical = canonical_json(spec.model_dump(mode="json"))
    return sha256(canonical.encode("utf-8")).hexdigest()
