"""Event wire contracts (CORE-CONTRACTS §3.1): envelopes, type+version registry, R1+R2+R3+R4+skills+compaction subsets.

Decode is fail closed: unknown type/version, wrong durability, malformed JSON, or any
schema violation raises and nothing is silently skipped or coerced. Field-level failures
surface as ``pydantic.ValidationError``; envelope/registry failures as ``EventDecodeError``
subclasses so S4/S6 can map them to ``StorageCorrupt`` and read-only diagnostics.

TS mapping (doc/parity.md §3, ``packages/schema/src/event.ts``): the envelope mirrors
``Event.durable()/ephemeral()`` with Python differences seq >= 1 (initial 1, not 0/-1)
and a ``py.*`` event domain that never claims TS wire compatibility. The R3 permission
family (``py.permission.*``, six durable types) registers per §3.1 L140; TS keeps its
permission events ephemeral (``packages/schema/src/permission.ts`` Asked/Replied), a
Python-contract difference, not a port.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from json import dumps, loads
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator, model_validator

from opencode.contracts.errors import CoreError
from opencode.contracts.llm import ModelRef, Usage
from opencode.contracts.messages import (
    AssistantMessage,
    JsonObject,
    ToolContent,
    ToolTextContent,
    UserMessage,
    UtcDateTime,
)
from opencode.contracts.tools import ToolRef

Durability = Literal["durable", "ephemeral"]

PY_SESSION_CREATED = "py.session.created"
PY_PROMPT_ACCEPTED = "py.prompt.accepted"

# R2 event domain (§3.1 L124-140): run/step/text/reasoning/tool durable rows
# plus the four ephemeral live-preview types.
PY_RUN_STARTED = "py.run.started"
PY_RUN_CANCEL_REQUESTED = "py.run.cancel_requested"
PY_RUN_FINISHED = "py.run.finished"
PY_STEP_STARTED = "py.step.started"
PY_STEP_GENERATED = "py.step.generated"
PY_STEP_FINISHED = "py.step.finished"
PY_TEXT_FINALIZED = "py.text.finalized"
PY_REASONING_FINALIZED = "py.reasoning.finalized"
PY_TOOL_REQUESTED = "py.tool.requested"
PY_TOOL_STARTED = "py.tool.started"
PY_TOOL_FINISHED = "py.tool.finished"
PY_TEXT_DELTA = "py.text.delta"
PY_REASONING_DELTA = "py.reasoning.delta"
PY_TOOL_INPUT_DELTA = "py.tool.input.delta"
PY_TOOL_PROGRESS = "py.tool.progress"

# R3 permission family (§3.1 L140 rows): the approval lifecycle
# (requested/replied/closed) plus the saved-decision lifecycle
# (saved/revoked/expired). All six are durable; S1 registers the wire +
# fold + projection, producers (Policy/approval protocol) land in R3-S2.
PY_PERMISSION_REQUESTED = "py.permission.requested"
PY_PERMISSION_REPLIED = "py.permission.replied"
PY_PERMISSION_CLOSED = "py.permission.closed"
PY_PERMISSION_SAVED = "py.permission.saved"
PY_PERMISSION_REVOKED = "py.permission.revoked"
PY_PERMISSION_EXPIRED = "py.permission.expired"

# Skills family (§3.1 L142 row): ``py.skill.activated`` -- the durable
# activation record (§6.3). Producers (SkillCatalog.activate check order +
# state fold) land in P2-S2; the registration below is contract face only.
PY_SKILL_ACTIVATED = "py.skill.activated"

# Compaction family (§3.1 L176 row; §2.4 L108-139): ``py.context.compacted``
# -- the durable effective-context-replacement record. The engine core
# (threshold + lock/CAS capture-commit order + the injected summary face)
# lands in P2-S8 slice one (T-C1-060); the registration below is contract
# face only, like the skills precedent.
PY_CONTEXT_COMPACTED = "py.context.compacted"

EVENT_VERSION = 1

#: Event types that belong to a run; their envelope metadata must carry run_id (§3.1).
#: Flipped to the full R2 run-scoped set (scheduler ledger #71, T-C1-014):
#: py.prompt.accepted plus every R2 type (11 durable + 4 ephemeral). The
#: R1-only interim state (T-C1-013 float report, open question 1) is resolved
#: -- the three frozen R1 test envelope constructors that built run-scoped
#: types without metadata.run_id (test_bus.py _ephemeral, test_eventlog.py
#: _ephemeral, test_projector.py "future") were patched with run_id envelopes
#: in the same slice, so decode now rejects a missing run_id on every type in
#: this set. py.session.created is not run-scoped and stays out. The
#: whenever-present validation below also still guards any metadata.run_id.
RUN_SCOPED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        PY_PROMPT_ACCEPTED,
        PY_RUN_STARTED,
        PY_RUN_CANCEL_REQUESTED,
        PY_RUN_FINISHED,
        PY_STEP_STARTED,
        PY_STEP_GENERATED,
        PY_STEP_FINISHED,
        PY_TEXT_FINALIZED,
        PY_REASONING_FINALIZED,
        PY_TOOL_REQUESTED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
        PY_TEXT_DELTA,
        PY_REASONING_DELTA,
        PY_TOOL_INPUT_DELTA,
        PY_TOOL_PROGRESS,
    }
)

_SHA256_HEX = r"^[0-9a-f]{64}$"

_EVT_ID = r"^evt_"
_SES_ID = r"^ses_"
_RUN_ID = r"^run_"
_MSG_ID = r"^msg_"


def _reject_bool(field: str, value: Any) -> Any:
    """§1 repo rule: integer-semantic fields reject bools before type conversion.

    Pydantic's bool→int/Literal equality would let True masquerade as 1; guard both
    model construction and decode paths with the same behavior.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    return value


def versioned_type(event_type: str, version: int) -> str:
    """Registry key format, mirrors TS ``versionedType`` (``${type}.${version}``)."""
    return f"{event_type}.{version}"


def canonical_json(value: JsonValue) -> str:
    """Deterministic JSON text for hashing: sorted keys, no whitespace, UTF-8, NaN rejected."""
    return dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compute_snapshot_hash(snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3) -> str:
    """sha256 hex of the snapshot's canonical JSON (CORE-CONTRACTS §6.2).

    R3-S6 generalization, R4-S3 widened to three schema versions: the same
    canonical serializer + sha256 over any registered snapshot schema
    version. The R1 value domain stays the constant
    ``{"permission_rules":[],"schema_version":1}`` — UTF-8, no BOM, 64
    lowercase hex, fingerprint frozen (§9 L368); a v2/v3 domain hashes its
    own canonical text through the identical pipeline.
    """
    return sha256(canonical_json(snapshot.model_dump(mode="json")).encode("utf-8")).hexdigest()


class ConfigSnapshot(BaseModel):
    """R1 minimal legal config snapshot (CORE-CONTRACTS §6.1, T-A-002 ruling).

    Exactly ``{"schema_version": 1, "permission_rules": []}``: both fields must be
    present, rules must be empty, and no extra keys (an embedded hash is rejected —
    the digest lives only in event data ``snapshot_hash``). Executable snapshots
    (R3+) use a new schema_version; version 1 decoding and old fixtures are kept.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    permission_rules: list[JsonObject] = Field(max_length=0)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    @classmethod
    def create(cls) -> ConfigSnapshot:
        """The unique legal R1 snapshot object."""
        return cls(schema_version=1, permission_rules=[])


class InstructionSource(BaseModel):
    """One instruction contribution with provenance (§6.1 L287): file path + text.

    ``path`` is the resolved file that contributed ``text`` — an AGENTS.md
    path, or the declaring config file for config-declared instruction
    strings. Instructions only shape the system prompt; they never raise
    permissions (§6.1 L287).
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    text: str


class SnapshotLimits(BaseModel):
    """The §6.1 L287 limits block of a v2 snapshot (RuntimeSettings + max_steps).

    Wire shape of the runtime knobs the §7 L339 face consumes; every field is
    explicit (no fabricated defaults, §6.1 L291) and the integer-semantic
    fields reject bools (§1 rule) with the field's own name.
    """

    model_config = ConfigDict(extra="forbid")

    context_limit: int = Field(ge=1)
    output_reserve: int = Field(ge=1)
    timeout_s: float = Field(gt=0)
    safety_margin: int = Field(ge=0)
    temperature: float | None = None
    max_steps: int = Field(ge=1)

    @field_validator("context_limit", "output_reserve", "safety_margin", "max_steps", mode="before")
    @classmethod
    def _integer_field_not_bool(cls, value: Any, info: ValidationInfo) -> Any:
        return _reject_bool(info.field_name or "", value)


class ConfigSnapshotV2(BaseModel):
    """R3-S6 executable config snapshot (CORE-CONTRACTS §6.1 L287, schema_version 2).

    Deliberately a standalone model, NOT a ``ConfigSnapshot`` subclass: v1 is
    the fixed minimal domain whose canonical text and fingerprint are frozen
    (§6.2/§9 L368); v2 is the executable face (§6.1 L289) where every runtime
    field is explicit and none carries a fabricated default. MCP refs and
    boot fingerprints are omitted outright (R4 face, disclosed) rather than
    defaulted, per the §6.1 L291 no-silent-backfill spirit; ``endpoint_ref``
    is the sanitized provider base URL (userinfo/query stripped, loader-side
    normalization); the provider stays ``maas`` (Phase 1, OP-R3-10).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    model: ModelRef
    endpoint_ref: str = Field(min_length=1)
    roots: list[str]
    permission_rules: list[JsonObject]
    instructions: list[InstructionSource]
    limits: SnapshotLimits

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    @field_validator("roots")
    @classmethod
    def _roots_entries_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("roots entries must be non-empty path strings")
        return value


class McpServerRef(BaseModel):
    """One host-tier local MCP server reference + its boot fingerprint (§6.1 L287, R4-S3).

    The v3 snapshot's MCP face: ``server`` is the host-tier server name;
    ``command`` is the argv list (exec'd directly, never through a shell);
    ``env_refs`` are the environment KEY NAMES the server needs — values
    never ride the snapshot (§6.2.1/L302: secrets and credentials stay
    references); ``fingerprint`` is the deterministic startup digest over
    server+command+env_refs (OP-R4-2 conservative default: ``cwd`` is
    carried but excluded from the digest — the launch identity the policy
    binds to is argv + referenced environment shape, §5.2 L268).
    """

    model_config = ConfigDict(extra="forbid")

    server: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    cwd: str | None = Field(default=None, min_length=1)
    env_refs: list[str] = Field(default_factory=list)
    fingerprint: str = Field(pattern=_SHA256_HEX)

    @field_validator("command")
    @classmethod
    def _command_entries_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("command entries must be non-empty argument strings")
        return value

    @field_validator("env_refs")
    @classmethod
    def _env_refs_sorted_unique(cls, value: list[str]) -> list[str]:
        """Canonical form: sorted, de-duplicated, non-empty key names.

        Sorted-and-unique is a wire invariant (not a convenience): the
        fingerprint and the canonical snapshot hash are computed over this
        list, so any order wobble would silently fork identities.
        """
        if any(not item for item in value):
            raise ValueError("env_refs entries must be non-empty environment key names")
        if value != sorted(set(value)):
            raise ValueError("env_refs must be sorted and free of duplicates (canonical wire form)")
        return value


class ConfigSnapshotV3(BaseModel):
    """R4-S3 executable config snapshot (§6.1 L287, schema_version 3).

    Standalone like v2 (NOT a ``ConfigSnapshotV2`` subclass): each schema
    version pins its own domain, and a subclass would make every v2-typed
    ``isinstance`` gate (the service create/coerce routing, the CLI
    face) silently accept v3 rows. Adds exactly one face over v2:
    ``mcp_servers``, the host-tier local MCP server references with their
    startup fingerprints (§6 L287). Required with no default — a snapshot
    without the face must say so explicitly with ``[]`` (§6.1 L291
    no-fabrication spirit).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    model: ModelRef
    endpoint_ref: str = Field(min_length=1)
    roots: list[str]
    permission_rules: list[JsonObject]
    instructions: list[InstructionSource]
    limits: SnapshotLimits
    mcp_servers: list[McpServerRef]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    @field_validator("roots")
    @classmethod
    def _roots_entries_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("roots entries must be non-empty path strings")
        return value


class SessionCreatedData(BaseModel):
    """``py.session.created`` v1 data (§3.1/§6.1): working_dir + config_snapshot + snapshot_hash."""

    model_config = ConfigDict(extra="forbid")

    working_dir: str = Field(min_length=1)
    config_snapshot: ConfigSnapshot
    snapshot_hash: str = Field(pattern=_SHA256_HEX)

    @model_validator(mode="after")
    def _snapshot_hash_verified(self) -> SessionCreatedData:
        """Recompute the snapshot digest on every decode; a mismatch is ValidationError (§6.2.3)."""
        if compute_snapshot_hash(self.config_snapshot) != self.snapshot_hash:
            raise ValueError("snapshot_hash does not match the canonical sha256 of config_snapshot")
        return self


class SessionCreatedDataV2(SessionCreatedData):
    """``py.session.created`` v2 data (§6.1 L287): the executable v2 snapshot.

    Subclasses v1 so every frozen consumer that types the field as
    ``SessionCreatedData`` (the state fold's isinstance gate, the projector's
    canonical_json store) accepts v2 rows unchanged; overrides only
    ``config_snapshot``. The inherited model validator recomputes the digest
    on every decode (§6.2 L297 drift re-check) through the generalized
    ``compute_snapshot_hash``.
    """

    config_snapshot: ConfigSnapshotV2  # type: ignore[assignment]


class SessionCreatedDataV3(SessionCreatedDataV2):
    """``py.session.created`` v3 data (§6.1 L287, R4-S3): the executable v3 snapshot.

    Subclasses v2 so every frozen consumer typing the field as
    ``SessionCreatedData``/``SessionCreatedDataV2`` (the state fold's
    isinstance gate, the projector's canonical_json store) accepts v3 rows
    unchanged; overrides only ``config_snapshot``. The inherited model
    validator recomputes the digest on every decode (§6.2 L297 drift
    re-check) through the three-version ``compute_snapshot_hash``.
    """

    config_snapshot: ConfigSnapshotV3  # type: ignore[assignment]


@dataclass(frozen=True)
class SessionCreatedRuntime:
    """The §6.1 recovery face of one ``py.session.created`` row (L302).

    ``snapshot`` is None exactly for v1 rows: the minimal snapshot has no
    runtime face, and recovery must stop and ask the host instead of silently
    backfilling from the current config (§6.1 L291, L302). A v2 row carries
    its v2 snapshot; a v3 row its v3 snapshot (R4-S3) — v2 is never
    silently upgraded to v3 (the ``mcp_servers`` face is never backfilled).
    """

    working_dir: str
    snapshot: ConfigSnapshotV2 | ConfigSnapshotV3 | None


def upcast_session_created(data: SessionCreatedData) -> SessionCreatedRuntime:
    """Deterministic v1->v2->v3 upcast (§8 L356/L358; scheduler ledger row #24).

    A v3 row passes through with its v3 snapshot — checked FIRST: v3 data
    IS-A v2 data, so the v2 branch would otherwise swallow it (the
    isinstance order is load-bearing). A v2 row passes through with its v2
    snapshot and is never upgraded to v3 — the upcast never fabricates the
    ``mcp_servers`` face (§6.1 L291). A v1 row becomes ``snapshot=None``:
    the upcast never fabricates runtime fields. Rows older than v1 cannot
    reach here (the registry rejects unknown versions at decode), so there
    is no other historical branch (④ disclosure).
    """
    if isinstance(data, SessionCreatedDataV3):
        return SessionCreatedRuntime(working_dir=data.working_dir, snapshot=data.config_snapshot)
    if isinstance(data, SessionCreatedDataV2):
        return SessionCreatedRuntime(working_dir=data.working_dir, snapshot=data.config_snapshot)
    return SessionCreatedRuntime(working_dir=data.working_dir, snapshot=None)


class PromptAcceptedData(BaseModel):
    """``py.prompt.accepted`` v1 data (§3.1): request_id + run_id + the accepted user message."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    message: UserMessage


# ---------------------------------------------------------------------------
# R2 event data models (§3.1 rows L124-132 + ephemeral L140; plan §2 R2-S1).
# ID rules: message_id carries the ^msg_ pattern (§3.1 L114); step_id/part_id/
# execution_id/tool_call_id are internal conventions — plain non-empty strings,
# no pattern (OP-3 conservative reading; generation conventions land in S6).
# ---------------------------------------------------------------------------


class RunStartedData(BaseModel):
    """``py.run.started`` v1 data (§3.1 L124): run identity only."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)


class RunCancelRequestedData(BaseModel):
    """``py.run.cancel_requested`` v1 data (§3.1 L125): user/shutdown intent; never overrides a result."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    reason: Literal["user", "shutdown"]


class RunFinishedData(BaseModel):
    """``py.run.finished`` v1 data (§3.1 L138): folded outcome; idle messages project from this."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    outcome: Literal["succeeded", "failed", "interrupted"]
    error: CoreError | None = None


class StepStartedData(BaseModel):
    """``py.step.started`` v1 data (§3.1 L126): one generation attempt begins.

    ``attempt`` >= 1; R2 always writes 1 (OP-6: no user-facing retry command,
    a retry is a new prompt; attempt > 1 is reserved for recovery/retry faces).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, pattern=_RUN_ID)
    step_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    model: ModelRef
    attempt: int = Field(ge=1)

    @field_validator("attempt", mode="before")
    @classmethod
    def _attempt_not_bool(cls, value: Any) -> Any:
        return _reject_bool("attempt", value)


class TextFinalizedData(BaseModel):
    """``py.text.finalized`` v1 data (§3.1 L127): full final text at message_id+part_id.

    ``ordinal`` is 0-based and unique within the generation batch; batch ordering
    and second-finalized idempotency are commit-time rules (§3.1 L144, R2-S2).
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    text: str

    @field_validator("ordinal", mode="before")
    @classmethod
    def _ordinal_not_bool(cls, value: Any) -> Any:
        return _reject_bool("ordinal", value)


class ReasoningFinalizedData(BaseModel):
    """``py.reasoning.finalized`` v1 data (§3.1 L127): same shape as text, batch-ordered by ordinal."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    text: str

    @field_validator("ordinal", mode="before")
    @classmethod
    def _ordinal_not_bool(cls, value: Any) -> Any:
        return _reject_bool("ordinal", value)


class StepGeneratedData(BaseModel):
    """``py.step.generated`` v1 data (§3.1 L128): the last event of the generation batch.

    The embedded AssistantMessage is the complete call plan with every tool
    part pending/not_started; batch atomicity and consistency with the finalized
    parts are commit-time validation (§3.1 L144, R2-S2), not wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    message: AssistantMessage


class StepFinishedData(BaseModel):
    """``py.step.finished`` v1 data (§3.1 L132): self-contained terminal message."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    message: AssistantMessage


class ToolRequestedData(BaseModel):
    """``py.tool.requested`` v1 data (§3.1 L129): raw request, resolved ref, canonical hash.

    ``tool_ref`` is optional because the raw requested_name may fail catalog
    resolution (unknown tool); the failed/not_started terminal then lands on
    py.tool.finished. Identity/ownership constraints (§2.1 L37) are commit-time
    validation (R2-S2).
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    tool_call_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    requested_name: str = Field(min_length=1)
    tool_ref: ToolRef | None = None
    input: JsonObject
    input_hash: str = Field(pattern=_SHA256_HEX)


class ToolStartedData(BaseModel):
    """``py.tool.started`` v1 data (§3.1 L130): execution confirmed after the permission decision.

    permission_decision uses the §5.2 effect vocabulary; R2 has no policy layer
    (read defaults to allow), so R2 producers always write "allow" — ask/deny
    become producible only from R3+ (float-reported, T-C1-013).
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    permission_decision: Literal["allow", "ask", "deny"]
    input_hash: str = Field(pattern=_SHA256_HEX)


class ToolFinishedData(BaseModel):
    """``py.tool.finished`` v1 data (§3.1 L131): terminal state + model-readable content.

    ``state`` is terminal-only — pending/running live on AssistantToolPart and
    the fold, never on the finished event. §2.1 L72: every terminal state carries
    at least one text content, including denied/cancelled/unknown explanations;
    file-only content is rejected here, not at display time.
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    state: Literal["succeeded", "failed", "denied", "cancelled", "unknown"]
    effect_status: Literal["not_started", "completed", "unknown"]
    content: list[ToolContent] = Field(min_length=1)
    structured_output: JsonValue | None = None
    error: CoreError | None = None

    @model_validator(mode="after")
    def _has_model_readable_text(self) -> ToolFinishedData:
        if not any(isinstance(part, ToolTextContent) for part in self.content):
            raise ValueError("tool terminal state requires at least one text content (§2.1 L72)")
        return self


class TextDeltaData(BaseModel):
    """``py.text.delta`` ephemeral data (§3.1 L140): live preview only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    delta: str


class ReasoningDeltaData(BaseModel):
    """``py.reasoning.delta`` ephemeral data (§3.1 L140): live preview only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, pattern=_MSG_ID)
    part_id: str = Field(min_length=1)
    delta: str


class ToolInputDeltaData(BaseModel):
    """``py.tool.input.delta`` ephemeral data (§3.1 L140): raw argument fragments by call_index."""

    model_config = ConfigDict(extra="forbid")

    call_index: int = Field(ge=0)
    delta: str

    @field_validator("call_index", mode="before")
    @classmethod
    def _call_index_not_bool(cls, value: Any) -> Any:
        return _reject_bool("call_index", value)


class ToolProgressData(BaseModel):
    """``py.tool.progress`` ephemeral data (§3.1 L140): adapter progress metadata by execution."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# R3 permission event data models (§3.1 L140 rows; plan §2 R3-S1). ID rules:
# permission_id/decision_id/boot_id follow the request_id convention (plain
# non-empty strings) — the four patterned ID types (evt_/msg_/ses_/run_) do
# not cover them. ``resources`` mirrors the TS ``Permission.Request`` anchor
# (``packages/schema/src/permission.ts``: action string + resources string
# array); ``reply`` mirrors ``Permission.Reply`` (once/always/reject).
# ---------------------------------------------------------------------------


class PermissionRequestedData(BaseModel):
    """``py.permission.requested`` v1 data (§3.1 L140): approval for one pending call.

    ``expires_at`` is the approval deadline (§5.2's 300 s default is producer
    policy, not wire shape). Ownership of ``execution_id``/``input_hash``
    against the folded tool plan is commit-time fold validation (R3-S1), not
    wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    permission_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    resources: list[str]
    input_hash: str = Field(pattern=_SHA256_HEX)
    expires_at: UtcDateTime


class PermissionRepliedData(BaseModel):
    """``py.permission.replied`` v1 data (§3.1 L140): the host reply.

    §5.2 L268: only an actual reject/once/always reply writes this event (a
    policy deny never fakes a host reply); §7 L173: the first legal
    transition wins and a late reply cannot override a closed permission.
    ``source`` is pinned to ``host`` for v1 (the only approval channel).
    """

    model_config = ConfigDict(extra="forbid")

    permission_id: str = Field(min_length=1)
    reply: Literal["once", "always", "reject"]
    source: Literal["host"]


class PermissionClosedData(BaseModel):
    """``py.permission.closed`` v1 data (§3.1 L140): system close, never a fake host reject.

    ``outcome`` covers expiry, cancellation (including the unconnected-approval
    case, §5.2 L268) and restart recovery closure (§3.3).
    """

    model_config = ConfigDict(extra="forbid")

    permission_id: str = Field(min_length=1)
    outcome: Literal["expired", "cancelled", "restart"]
    reason: str = Field(min_length=1)


class PermissionSavedData(BaseModel):
    """``py.permission.saved`` v1 data (§3.1 L140/§5.2): one saved always-decision.

    ``scope``/``rule`` carry the concrete, displayable action/resource binding
    (§5.2: no unbounded wildcards; shell rules bind the full command/cwd).
    Their inner shape is policy-owned — R3-S1 has no producer, so the wire
    pins them as JSON objects, the ``ToolRequestedData.input`` precedent for
    tool-owned shapes. ``expires_at`` is optional (None = no explicit expiry;
    activation is still bounded by ``boot_id``). Only decisions of the current
    boot activate (§5.2).
    """

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1)
    scope: JsonObject
    rule: JsonObject
    expires_at: UtcDateTime | None = None
    boot_id: str = Field(min_length=1)


class PermissionRevokedData(BaseModel):
    """``py.permission.revoked`` v1 data (§3.1 L140): host revocation of a saved decision."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PermissionExpiredData(BaseModel):
    """``py.permission.expired`` v1 data (§3.1 L140): expiry-check terminal of a saved decision."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Skills event data model (§3.1 L142 row; §6.3 L334-343). Plain wire fields
# per the T-C1-052a ruling: the §6.3 field-table bounds (non-empty, hash
# shape, body cap) are activation check-order semantics (§6.3 L345, P2-S2),
# not wire constraints. extra="forbid" and the size bool guard stay, per the
# §1 L16/L17 repo-wide rules.
# ---------------------------------------------------------------------------

#: Per-skill instruction body cap: the §6 L289 per-instruction limit (64 KiB).
#: Consumed by the activation check order (P2-S2), not by the wire model.
SKILL_BODY_MAX_BYTES = 64 * 1024


class SkillActivatedData(BaseModel):
    """``py.skill.activated`` v1 data (§3.1 L142; §6.3 L334-343): the durable
    activation record -- identity (skill_id/source), provenance
    (path/hash/size) and the bounded instruction body carried on the event,
    so restore replays the saved content and never re-reads disk (§6.3 L345).
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    source: str
    path: str
    hash: str
    size: int
    body: str

    @field_validator("size", mode="before")
    @classmethod
    def _size_not_bool(cls, value: Any) -> Any:
        return _reject_bool("size", value)


# ---------------------------------------------------------------------------
# Compaction event data model (§3.1 L176 row; §2.4 L114-124 field table).
# Unlike the skills precedent (whose §6.3 bounds live in the activation check
# order, not the wire), the compaction contract pins its bounds ON the wire
# table itself: covered_seq/retained_from/message_count are non-negative ints,
# summary_text/summary_hash are non-empty strings, and model/usage reuse the
# §2.2/§2.3 domains. extra="forbid" and the integer bool guards stay per the
# §1 L16/L17 repo-wide rules.
# ---------------------------------------------------------------------------


class ContextCompactedData(BaseModel):
    """``py.context.compacted`` v1 data (§3.1 L176; §2.4 L114-126): the
    effective-context-replacement record. ``covered_seq`` is the settled
    history boundary seq captured in-lock -- the expected_seq/CAS basis, so a
    moved boundary discards the summary; ``retained_from`` is where the
    retained history starts (the ContextBuilder's original-history origin,
    §2.4 L119); ``summary_text``/``summary_hash`` carry the replacement body
    and its identity digest (§2.4 L120-121); ``model``/``usage`` record the
    extra model call (§2.4 L122-123, domains per §2.2/§2.3); ``message_count``
    measures the covered interval (§2.4 L124). Not run-scoped: compaction is
    engine-triggered, so no metadata.run_id is required (§3.1). The engine
    core lands in P2-S8 slice one (T-C1-060); the ContextBuilder wiring and
    the projector/fold faces land in the follow-up slice.
    """

    model_config = ConfigDict(extra="forbid")

    covered_seq: int = Field(ge=0)
    retained_from: int = Field(ge=0)
    summary_text: str = Field(min_length=1)
    summary_hash: str = Field(min_length=1)
    model: ModelRef
    usage: Usage
    message_count: int = Field(ge=0)

    @field_validator("covered_seq", "retained_from", "message_count", mode="before")
    @classmethod
    def _integer_fields_not_bool(cls, value: Any, info: ValidationInfo) -> Any:
        return _reject_bool(info.field_name or "", value)


def _require_run_metadata(event_type: str, metadata: JsonObject) -> None:
    """Run-scoped events must carry run_id in envelope metadata (§3.1).

    The run_id metadata key, whenever it appears, must be a string matching ^run_
    (§3.1); null, non-string, or wrongly-prefixed values are rejected.
    """
    if event_type in RUN_SCOPED_EVENT_TYPES and "run_id" not in metadata:
        raise ValueError(f"metadata.run_id is required for run-scoped event type {event_type!r}")
    if "run_id" in metadata:
        value = metadata["run_id"]
        if not isinstance(value, str) or not value.startswith("run_"):
            raise ValueError("metadata.run_id, when present, must be a string matching ^run_")


class EventDraft[DataT: BaseModel](BaseModel):
    """Pre-commit draft (§3.1): id/type/version/session_id/created_at/data/metadata, no seq.

    IDs are generated before the first retry attempt and reused across retries. The
    registry is not consulted at construction time; commit-time validation owns
    type/version/membership checks (§3.2).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=_EVT_ID)
    type: str = Field(min_length=1)
    version: int = Field(ge=1)
    session_id: str = Field(min_length=1, pattern=_SES_ID)
    created_at: UtcDateTime
    data: DataT
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("version", mode="before")
    @classmethod
    def _version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("version", value)

    @model_validator(mode="after")
    def _run_metadata_present(self) -> EventDraft[DataT]:
        _require_run_metadata(self.type, self.metadata)
        return self


class DurableEvent[DataT: BaseModel](EventDraft[DataT]):
    """Committed event: draft + seq, allocated per session inside the DB transaction (seq >= 1)."""

    seq: int = Field(ge=1)

    @field_validator("seq", mode="before")
    @classmethod
    def _seq_not_bool(cls, value: Any) -> Any:
        return _reject_bool("seq", value)


class EphemeralEvent[DataT: BaseModel](BaseModel):
    """Live-only envelope (§3.1): no version, no seq, no durable cursor."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=_EVT_ID)
    type: str = Field(min_length=1)
    session_id: str = Field(min_length=1, pattern=_SES_ID)
    created_at: UtcDateTime
    data: DataT
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _run_metadata_present(self) -> EphemeralEvent[DataT]:
        _require_run_metadata(self.type, self.metadata)
        return self


class EventDecodeError(Exception):
    """Envelope could not be decoded; decode paths raise instead of silently skipping."""


class UnknownEventTypeError(EventDecodeError):
    """The envelope's event type is not in the registry."""


class UnknownEventVersionError(EventDecodeError):
    """The event type is registered, but not under this (type, version) pair."""


class DurabilityMismatchError(EventDecodeError):
    """The event type exists, but on the other bus (durable vs ephemeral)."""


@dataclass(frozen=True)
class EventDefinition:
    """One registry entry; adding an event type = adding one definition line.

    ``version`` keys the durable registry. For ephemeral definitions it is
    bookkeeping only: the ephemeral envelope carries no version field.
    """

    # Named event_type: a field literally named `type` shadows the builtin in
    # subsequent class-body annotations (data_model: type[BaseModel]).
    event_type: str
    durability: Durability
    data_model: type[BaseModel]
    version: int = EVENT_VERSION


def _reject_json_constant(token: str) -> None:
    """Reject NaN/Infinity/-Infinity literals: they are not valid JSON values."""
    raise ValueError(f"non-standard JSON constant {token!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Duplicate JSON object keys are ambiguous; reject instead of last-wins (§6.2.1)."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _load_envelope(raw: str | bytes | JsonObject) -> JsonObject:
    """Accept a dict, JSON text, or JSON bytes and return a JSON object envelope."""
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    try:
        # ValueError covers JSONDecodeError, UnicodeDecodeError, rejected NaN constants,
        # and duplicate object keys.
        parsed = loads(text, parse_constant=_reject_json_constant, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as error:
        raise EventDecodeError(f"malformed JSON event envelope: {error}") from error
    if not isinstance(parsed, dict):
        raise EventDecodeError("event envelope must be a JSON object")
    return parsed


def _parametrize(envelope_model: Any, data_model: type[BaseModel]) -> Any:
    """Parametrize a generic envelope model at runtime with the registered data model.

    mypy cannot express a ``type[X]`` value as a generic subscript argument, so the
    base class is routed through ``Any``; runtime behavior is the plain ``Base[Data]``.
    """
    return envelope_model[data_model]


class EventRegistry:
    """type+version -> data model registry; every decode path fails closed."""

    def __init__(self, definitions: Sequence[EventDefinition] = ()) -> None:
        self._durable: dict[tuple[str, int], EventDefinition] = {}
        self._durable_types: set[str] = set()
        self._ephemeral: dict[str, EventDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: EventDefinition) -> None:
        """Add one definition; duplicate keys are rejected loudly (TS durableMap parity)."""
        if definition.durability == "durable":
            key = (definition.event_type, definition.version)
            if key in self._durable:
                raise ValueError(f"Duplicate durable event definition for {versioned_type(*key)}")
            self._durable[key] = definition
            self._durable_types.add(definition.event_type)
            return
        if definition.event_type in self._ephemeral:
            raise ValueError(f"Duplicate ephemeral event definition for {definition.event_type}")
        self._ephemeral[definition.event_type] = definition

    def resolve_durable(self, event_type: str, version: int) -> EventDefinition:
        """Resolve a (type, version) pair to its durable definition."""
        definition = self._durable.get((event_type, version))
        if definition is not None:
            return definition
        if event_type in self._ephemeral:
            raise DurabilityMismatchError(f"Event type {event_type!r} is ephemeral, not durable")
        if event_type in self._durable_types:
            raise UnknownEventVersionError(f"Unknown version {version} for event type {event_type!r}")
        raise UnknownEventTypeError(f"Unknown event type {event_type!r}")

    def resolve_ephemeral(self, event_type: str) -> EventDefinition:
        """Resolve an ephemeral type to its definition; ephemeral envelopes carry no version."""
        definition = self._ephemeral.get(event_type)
        if definition is not None:
            return definition
        if event_type in self._durable_types:
            raise DurabilityMismatchError(f"Event type {event_type!r} is durable, not ephemeral")
        raise UnknownEventTypeError(f"Unknown event type {event_type!r}")

    def decode_durable(self, raw: str | bytes | JsonObject) -> DurableEvent:
        """Decode one committed durable event; unknown type/version raise, never skip."""
        envelope = _load_envelope(raw)
        event_type = envelope.get("type")
        version = envelope.get("version")
        if not isinstance(event_type, str) or isinstance(version, bool) or not isinstance(version, int):
            raise EventDecodeError("durable envelope requires string 'type' and integer 'version'")
        definition = self.resolve_durable(event_type, version)
        return _parametrize(DurableEvent, definition.data_model).model_validate(envelope)

    def decode_ephemeral(self, raw: str | bytes | JsonObject) -> EphemeralEvent:
        """Decode one live ephemeral event; 'version' is not part of the ephemeral envelope."""
        envelope = _load_envelope(raw)
        event_type = envelope.get("type")
        if not isinstance(event_type, str):
            raise EventDecodeError("ephemeral envelope requires string 'type'")
        definition = self.resolve_ephemeral(event_type)
        return _parametrize(EphemeralEvent, definition.data_model).model_validate(envelope)


R1_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedData),
    EventDefinition(event_type=PY_PROMPT_ACCEPTED, durability="durable", data_model=PromptAcceptedData),
)

#: R2 additions (§3.1 R2 subset): 11 durable rows + 4 ephemeral live types.
#: The R3 permission family is its own tuple below; registration stays
#: additive so R1/R2 logs and fixtures decode identically (plan §0.2).
R2_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_RUN_STARTED, durability="durable", data_model=RunStartedData),
    EventDefinition(event_type=PY_RUN_CANCEL_REQUESTED, durability="durable", data_model=RunCancelRequestedData),
    EventDefinition(event_type=PY_RUN_FINISHED, durability="durable", data_model=RunFinishedData),
    EventDefinition(event_type=PY_STEP_STARTED, durability="durable", data_model=StepStartedData),
    EventDefinition(event_type=PY_STEP_GENERATED, durability="durable", data_model=StepGeneratedData),
    EventDefinition(event_type=PY_STEP_FINISHED, durability="durable", data_model=StepFinishedData),
    EventDefinition(event_type=PY_TEXT_FINALIZED, durability="durable", data_model=TextFinalizedData),
    EventDefinition(event_type=PY_REASONING_FINALIZED, durability="durable", data_model=ReasoningFinalizedData),
    EventDefinition(event_type=PY_TOOL_REQUESTED, durability="durable", data_model=ToolRequestedData),
    EventDefinition(event_type=PY_TOOL_STARTED, durability="durable", data_model=ToolStartedData),
    EventDefinition(event_type=PY_TOOL_FINISHED, durability="durable", data_model=ToolFinishedData),
    EventDefinition(event_type=PY_TEXT_DELTA, durability="ephemeral", data_model=TextDeltaData),
    EventDefinition(event_type=PY_REASONING_DELTA, durability="ephemeral", data_model=ReasoningDeltaData),
    EventDefinition(event_type=PY_TOOL_INPUT_DELTA, durability="ephemeral", data_model=ToolInputDeltaData),
    EventDefinition(event_type=PY_TOOL_PROGRESS, durability="ephemeral", data_model=ToolProgressData),
)


#: R3 permission family (§3.1 L140 rows): the approval lifecycle plus the
#: saved-decision lifecycle, six durable types. Kept in a separate tuple so
#: the frozen R2 boundary pins hold (len(R2_EVENT_DEFINITIONS) == 15; no
#: py.permission.* rows in R2) while ``default_event_registry`` resolves the
#: full §3.1 table through R3-S1 (plan §2 group B).
R3_PERMISSION_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_PERMISSION_REQUESTED, durability="durable", data_model=PermissionRequestedData),
    EventDefinition(event_type=PY_PERMISSION_REPLIED, durability="durable", data_model=PermissionRepliedData),
    EventDefinition(event_type=PY_PERMISSION_CLOSED, durability="durable", data_model=PermissionClosedData),
    EventDefinition(event_type=PY_PERMISSION_SAVED, durability="durable", data_model=PermissionSavedData),
    EventDefinition(event_type=PY_PERMISSION_REVOKED, durability="durable", data_model=PermissionRevokedData),
    EventDefinition(event_type=PY_PERMISSION_EXPIRED, durability="durable", data_model=PermissionExpiredData),
)


#: R3 config family (§6.1 L287): ``py.session.created`` v2 — the first py.*
#: event family to raise a version, so the §8 L358 minimum bar applies: the
#: v1 definition stays registered side by side (old logs/fixtures decode
#: unchanged, §8 L356), the upcast
#: (:func:`upcast_session_created`) is explicit and deterministic, and
#: ``UnknownEventVersionError`` still rejects every version above the
#: registered set.
R3_CONFIG_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedDataV2, version=2),
)


#: R4 config family (§6.1 L287, R4-S3): ``py.session.created`` v3 — the
#: executable v3 snapshot carrying the host-tier MCP server references
#: with their startup fingerprints. Same §8 L358 bar as the v2 raise: the
#: v1/v2 definitions stay registered side by side (old logs and fixtures
#: decode unchanged), the upcast above is explicit and deterministic
#: (v2 rows are never silently upgraded to v3), and
#: ``UnknownEventVersionError`` rejects every version > 3.
R4_CONFIG_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedDataV3, version=3),
)


#: Skills family (§3.1 L142 row; §6.3 L334-343): ``py.skill.activated`` --
#: the durable activation record, one type. Kept in a separate tuple so the
#: frozen R2 boundary pins hold (len(R2_EVENT_DEFINITIONS) == 15; no
#: py.skill.* rows in R2) while ``default_event_registry`` resolves the full
#: §3.1 table (T-C1-052a; producers/fold land in P2-S2). Durable per §6.3:
#: the body rides the event, so the state fold restores it without disk
#: re-read.
SKILL_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_SKILL_ACTIVATED, durability="durable", data_model=SkillActivatedData),
)


#: Compaction family (§3.1 L176 row; §2.4 L114-126): ``py.context.compacted``
#: -- the durable effective-context-replacement record, one type. Kept in a
#: separate tuple so the frozen R2 boundary pins hold (len(R2_EVENT_DEFINITIONS)
#: == 15; no py.context.* rows in R2) while ``default_event_registry``
#: resolves the full §3.1 table (T-C1-060; the engine core lands in P2-S8
#: slice one, the ContextBuilder/projection wiring in slice two). Durable per
#: §2.4: the replacement record itself is the recovery source of the
#: effective context.
COMPACTION_EVENT_DEFINITIONS: tuple[EventDefinition, ...] = (
    EventDefinition(event_type=PY_CONTEXT_COMPACTED, durability="durable", data_model=ContextCompactedData),
)


def default_event_registry() -> EventRegistry:
    """Fresh registry over the R1+R2+R3+R4+skills+compaction event subsets (§3.1; skills §6.3, compaction §2.4)."""
    return EventRegistry(
        (
            *R1_EVENT_DEFINITIONS,
            *R2_EVENT_DEFINITIONS,
            *R3_PERMISSION_EVENT_DEFINITIONS,
            *R3_CONFIG_EVENT_DEFINITIONS,
            *R4_CONFIG_EVENT_DEFINITIONS,
            *SKILL_EVENT_DEFINITIONS,
            *COMPACTION_EVENT_DEFINITIONS,
        )
    )
