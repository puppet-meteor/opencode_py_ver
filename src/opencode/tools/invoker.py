"""Tool invoke pipeline (R2-S5; CORE-CONTRACTS §5.1 L255-257, plan §2 R2-S5).

This module owns the runtime side of the tool chain: :class:`ToolLimits`
(§5.3 L272/L276 numbers), :class:`ToolContext` (§5.1 L255 runtime dataclass),
:class:`ToolOutcome` (the model-facing terminal mirroring
``py.tool.finished``), the injected :class:`EventSink` commit callback, and
:func:`invoke_tool` -- the §5.1 L257 sequence driven by
:meth:`opencode.tools.registry.ToolRegistry.invoke` (the only call entry):

1. record ``py.tool.requested`` (raw requested_name, resolved tool_ref?,
   canonical input + sha256 hash) -- even for calls that fail resolution;
2. strict catalog/schema validation -- an unknown tool or schema-violating
   input produces a ``failed``/``not_started`` terminal (§2.1 L46); input is
   never implicitly converted or modified;
3. the cancellation checkpoint before ``py.tool.started`` (§2.1 L45:
   started requires not-cancelled);
4. the §5.1 L259 policy/approval gate -- with an injected
   :class:`~opencode.tools.policy.PermissionPolicy` (R3-S2) the §5.2
   evaluation runs here: deny folds straight to the §2.1 L47
   denied/not_started terminal (no faked host reply), ask runs the §5.2
   L268 approval protocol (requested -> replied|closed, 300 s default,
   closed-first paired terminals), allow proceeds; without a policy the
   R2 default-allow face is unchanged (§5.2 L264);
5. ``py.tool.started`` commit confirmation carrying the effective
   ``permission_decision`` (an ask-approved start carries the host
   reply's "allow");
6. adapter execution with a bounded progress callback, then result
   normalization (§2.1 L72 model-readable text, §5.3 L276
   structured_output/progress-metadata caps);
7. ``py.tool.finished`` commit; every terminal carries at least one
   model-readable text (§2.1 L72).

Adapter exceptions become model-readable failed results (§7 L341); only
infrastructure errors (sink/wiring/caller contract violations) propagate.
Phase 1 executes calls serially in call-start order (PHASE1-SLIM L85) --
the serialization lock lives in the registry.

TS mapping (``packages/core/src/tool/runtime.ts``): ``execute`` L29
(validation+execution pipeline), ``normalizeContent`` L272 (result
normalization), ``normalizedName``/``effectiveName`` L287/L289 (the
``namespace__name`` reversible wire name, provider spike §6-2). The TS
Effect service graph stays a single async pipeline here (plan §3 item 1).

Dependency note: the default clock/ID source reuses
:class:`opencode.session.service.AscendingIds` -- a single source for the
``evt_`` ascending ID format. Extracting it into a shared module would
touch the frozen R1 surface, so it is imported instead of duplicated
(float report, progress file).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_PERMISSION_CLOSED,
    PY_PERMISSION_EXPIRED,
    PY_PERMISSION_REPLIED,
    PY_PERMISSION_REQUESTED,
    PY_PERMISSION_SAVED,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    PermissionClosedData,
    PermissionExpiredData,
    PermissionRepliedData,
    PermissionRequestedData,
    PermissionSavedData,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
    canonical_json,
)
from opencode.contracts.messages import JsonObject, JsonValue, ToolContent, ToolTextContent
from opencode.contracts.tools import ToolRef, ToolResult, ToolSpec
from opencode.session.service import Clock, IdFactory
from opencode.tools.policy import (
    DEFAULT_APPROVAL_TIMEOUT_S,
    REASON_APPROVAL_RUN_CANCELLED,
    REASON_APPROVAL_UNCONNECTED,
    REASON_DECISION_EXPIRED,
    ApprovalCoordinator,
    PermissionPolicy,
    PermissionRequest,
    SavedDecision,
    extract_resources,
)

if TYPE_CHECKING:
    # ResolvedTool carries the adapter (registry-owned protocol); string
    # annotation only -- no runtime import cycle.
    from opencode.tools.registry import ToolAdapter

_KIB = 1024
_MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# §5.1 L255 runtime dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """§5.3 L272/L276 limits, injected via :class:`ToolContext` (OP-1 injection face).

    ``max_argument_bytes``/``max_tool_calls_per_generation``/
    ``max_text_stream_bytes`` are the generation-level §5.3 L276 caps: they
    stop the *generation* (runner-owned, R2-S6), so this slice only defines
    them for the runner to consume. The invoke/tool layer enforces the caps
    it owns: ``max_structured_output_bytes`` (py.tool.finished payload),
    ``max_progress_metadata_bytes`` (py.tool.progress metadata) and the two
    read-tool caps (window/output).
    """

    max_argument_bytes: int = 64 * _KIB
    max_tool_calls_per_generation: int = 16
    max_text_stream_bytes: int = _MIB
    max_structured_output_bytes: int = 64 * _KIB
    max_progress_metadata_bytes: int = 64 * _KIB
    max_read_bytes: int = _MIB
    max_read_output_bytes: int = 64 * _KIB


def _require_pattern(value: Any, name: str, prefix: str) -> None:
    """Fail closed on missing/malformed prefixed identity strings (§3.1)."""
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError(f"{name} must be a non-empty string matching ^{prefix}")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """§5.1 L255 runtime context passed through ``invoke`` to the adapter.

    The contract names session_id/message_id/working_dir/limits, a
    cancellation signal and a progress callback. Two identity fields are
    added on top because the committed events require them and TS parity
    (``packages/schema/src/tool.ts`` ``Context`` L13-19) carries the call id
    in the context: ``run_id`` (py.tool.* envelope metadata must carry it,
    §3.1 L110) and ``tool_call_id`` (py.tool.requested data requires it,
    §3.1 L129). Both are float-reported.

    ``cancel`` is the cooperative cancellation signal: checked by the
    Invoker before ``py.tool.started``; a started call keeps its determined
    result (§7 L335). ``progress`` is the runner-side callback the Invoker
    bounds (§5.3 L276 metadata cap) before handing the context to the
    adapter; ``None`` disables publication.
    """

    session_id: str
    run_id: str
    message_id: str
    tool_call_id: str
    working_dir: str
    limits: ToolLimits = field(default_factory=ToolLimits)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    progress: Callable[[JsonObject], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.session_id, "session_id", "ses_")
        _require_pattern(self.run_id, "run_id", "run_")
        _require_pattern(self.message_id, "message_id", "msg_")
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
            raise ValueError("tool_call_id must be a non-empty string")
        if not isinstance(self.working_dir, str) or not self.working_dir:
            raise ValueError("working_dir must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """The model-facing terminal of one invoke; mirrors ``py.tool.finished``.

    The runner pairs this with its ``tool_call_id`` to build the tool_result
    (§2.2 L80) and the step-terminal message (§5.1 L257: the folded tool
    events and ``py.step.finished`` must agree -- the outcome is that
    terminal, never an older snapshot).
    """

    execution_id: str
    requested_name: str
    tool_ref: ToolRef | None
    input_hash: str
    state: Literal["succeeded", "failed", "denied", "cancelled", "unknown"]
    effect_status: Literal["not_started", "completed", "unknown"]
    content: tuple[ToolContent, ...]
    structured_output: JsonValue | None = None
    error: CoreError | None = None


class ToolInvokeError(ValueError):
    """Caller/wiring contract violation on the tools runtime faces.

    Raised for malformed invoke arguments, a non-object input, a
    non-canonical-JSON-encodable input (NaN/Infinity), a missing event
    sink, or a bad session_id on ``snapshot``. These are infrastructure
    failures, not model-facing tool results (§7 L341: storage/wiring errors
    fail closed immediately).
    """


#: §5.1 L257: only the Invoker's event callback may commit; adapters never
#: see the store. The runner (R2-S6) wires this to SessionStore.commit under
#: its own expected_seq bookkeeping; tool events commit one per phase.
type EventSink = Callable[[Sequence[EventDraft[Any]]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    """A catalog-resolved (spec, adapter) pair; registry-internal value type."""

    spec: ToolSpec
    adapter: ToolAdapter


# ---------------------------------------------------------------------------
# Strict input validation (CORE-CONTRACTS §5.1 L257, plan §2 R2-S5 ②)
#
# No jsonschema dependency exists in the project, so the validator below
# implements the exact subset the registry accepts at registration time
# (fail closed: a schema outside the subset is a RegistrationError, never
# an invoke-time surprise). The subset: type object/string/integer/number/
# boolean/null, properties, required, additionalProperties, minimum,
# maximum and the description annotation.
# ---------------------------------------------------------------------------

_SUPPORTED_TYPE_NAMES = frozenset({"object", "string", "integer", "number", "boolean", "null"})
_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {"type", "description", "properties", "required", "additionalProperties", "minimum", "maximum"}
)


def validate_schema_supported(schema: JsonObject, *, root: bool = False) -> None:
    """Reject schemas outside the strict validator's supported subset.

    Registration-time check (the caller maps ValueError to
    :class:`~opencode.tools.registry.RegistrationError`); guarantees that
    :func:`validate_input_strict` is exact for every registered schema.
    """
    if not isinstance(schema, dict):
        raise ValueError("the schema must be a JSON object")
    unsupported = sorted(set(schema) - _SUPPORTED_SCHEMA_KEYWORDS)
    if unsupported:
        raise ValueError(f"unsupported schema keyword(s) {unsupported!r} (strict validator subset)")
    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in _SUPPORTED_TYPE_NAMES:
        raise ValueError(f"schema 'type' must be one of {sorted(_SUPPORTED_TYPE_NAMES)!r}")
    if root and schema_type != "object":
        raise ValueError("the root input schema must be of type 'object' (tool inputs are JSON objects)")
    if "description" in schema and not isinstance(schema["description"], str):
        raise ValueError("schema 'description' must be a string")
    if schema_type == "object":
        properties = schema.get("properties")
        if properties is not None:
            if not isinstance(properties, dict):
                raise ValueError("schema 'properties' must be an object")
            for name, sub in properties.items():
                if not isinstance(name, str):
                    raise ValueError("schema property names must be strings")
                try:
                    validate_schema_supported(sub)  # type: ignore[arg-type]
                except ValueError as error:
                    raise ValueError(f"property {name!r}: {error}") from error
        required = schema.get("required")
        if required is not None:
            if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
                raise ValueError("schema 'required' must be an array of strings")
        additional = schema.get("additionalProperties")
        if additional is not None and not isinstance(additional, bool):
            raise ValueError("schema 'additionalProperties' must be a boolean")
        return
    for keyword in ("properties", "required", "additionalProperties"):
        if keyword in schema:
            raise ValueError(f"schema keyword {keyword!r} is only valid on 'object' schemas")
    if schema_type in ("integer", "number"):
        for keyword in ("minimum", "maximum"):
            if keyword in schema:
                bound = schema[keyword]
                if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                    raise ValueError(f"schema {keyword!r} must be a number")
    else:
        for keyword in ("minimum", "maximum"):
            if keyword in schema:
                raise ValueError(f"schema keyword {keyword!r} is only valid on 'integer'/'number' schemas")


def _join(path: str, name: str) -> str:
    return name if not path else f"{path}.{name}"


def _where(path: str) -> str:
    return "root: " if not path else f"{path}: "


def _bounds_issues(schema: JsonObject, value: int | float, path: str) -> list[str]:
    issues: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        issues.append(f"{_where(path)}must be >= {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        issues.append(f"{_where(path)}must be <= {maximum}")
    return issues


def validate_input_strict(schema: JsonObject, value: Any, path: str = "") -> list[str]:
    """Strictly validate ``value`` against a supported-subset schema.

    No implicit conversion or modification: a bool never satisfies an
    integer/number field (Pydantic's bool→int trap, §1 repo rule), a float
    like ``3.0`` never satisfies ``integer``, and the input object is
    passed through unchanged on success. Returns human/model-readable
    issue strings; an empty list means valid.
    """
    schema_type = schema["type"]
    if schema_type == "object":
        if not isinstance(value, dict):
            return [f"{_where(path)}expected an object"]
        issues: list[str] = []
        if not all(isinstance(key, str) for key in value):
            issues.append(f"{_where(path)}object keys must be strings")
        # Registration-time validate_schema_supported guarantees these shapes;
        # the casts are static narrows of that invariant, not a second
        # validation pass.
        properties = cast("dict[str, JsonObject]", schema.get("properties", {}))
        required = cast("list[str]", schema.get("required", []))
        for name in required:
            if name not in value:
                issues.append(f"{_join(path, name)}: required field is missing")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    issues.append(f"{_join(path, name)}: unknown field (additionalProperties is false)")
        for name, sub in properties.items():
            if name in value:
                issues.extend(validate_input_strict(sub, value[name], _join(path, name)))
        return issues
    if schema_type == "string":
        if not isinstance(value, str):
            return [f"{_where(path)}expected a string"]
        return []
    if schema_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{_where(path)}expected an integer (no implicit conversion)"]
        return _bounds_issues(schema, value, path)
    if schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{_where(path)}expected a number"]
        return _bounds_issues(schema, value, path)
    if schema_type == "boolean":
        if not isinstance(value, bool):
            return [f"{_where(path)}expected a boolean"]
        return []
    if value is not None:
        return [f"{_where(path)}expected null"]
    return []


def _format_issues(requested_name: str, issues: Sequence[str], raw_input: JsonObject) -> str:
    """TS runtime.ts formatInputIssues parity: a model-readable fix-it message."""
    details = "\n".join(f"- {issue}" for issue in issues)
    return (
        f'Invalid arguments for tool "{requested_name}":\n{details}\n\n'
        f"Arguments provided:\n{canonical_json(raw_input)}\n\n"
        "Update the arguments and call the tool again."
    )


# ---------------------------------------------------------------------------
# Result normalization (§2.1 L72, §5.3 L276; TS normalizeContent L272)
# ---------------------------------------------------------------------------


def _canonical_bytes(value: JsonValue) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _wire_name(spec: ToolSpec) -> str:
    return f"{spec.namespace}__{spec.name}"


def _normalize_result(
    spec: ToolSpec, result: ToolResult, limits: ToolLimits
) -> tuple[list[ToolContent], JsonValue | None]:
    """Normalize an adapter result into commit-safe content (§5.1 L257).

    - ``structured_output`` over the §5.3 L276 64 KiB cap is dropped and a
      bounded marker text keeps the truncation + original size explicit;
    - a pure structured result regenerates the capped canonical JSON text
      for the model (§2.1 L72 -- never ``str(result)``);
    - any terminal without a text item gets one (error message, canonical
      JSON, or a no-output note) so ``py.tool.finished`` always carries
      model-readable text;
    - ``metadata`` has no py.tool.finished wire field (§3.1 L131) and is
      dropped at this boundary (float report); the metadata cap is enforced
      on py.tool.progress via the bounded progress callback.
    """
    content: list[ToolContent] = list(result.content)
    structured: JsonValue | None = result.structured_output
    if structured is not None and _canonical_bytes(structured) > limits.max_structured_output_bytes:
        original = _canonical_bytes(structured)
        structured = None
        content.append(
            ToolTextContent(
                type="text",
                text=(
                    f"[structured_output exceeded {limits.max_structured_output_bytes} bytes "
                    f"({original} bytes original) and was omitted]"
                ),
            )
        )
    if not any(isinstance(item, ToolTextContent) for item in content):
        if structured is not None:
            text: str = canonical_json(structured)
        elif result.error is not None:
            text = result.error.message
        else:
            text = f'Tool "{_wire_name(spec)}" completed with no output.'
        content.append(ToolTextContent(type="text", text=text))
    return content, structured


def _bounded_progress(
    progress: Callable[[JsonObject], Awaitable[None]] | None,
    limits: ToolLimits,
) -> Callable[[JsonObject], Awaitable[None]]:
    """Wrap the runner-side progress callback with the §5.3 L276 metadata cap."""

    async def publish(metadata: JsonObject) -> None:
        if progress is None:
            return
        size = _canonical_bytes(metadata)
        if size > limits.max_progress_metadata_bytes:
            metadata = {
                "truncated": f"progress metadata exceeded {limits.max_progress_metadata_bytes} bytes",
                "original_bytes": size,
            }
        await progress(metadata)

    return publish


# ---------------------------------------------------------------------------
# The §5.1 L257 pipeline
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _draft[DataT: BaseModel](
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    event_type: str,
    data: DataT,
    *,
    metadata: JsonObject | None = None,
) -> EventDraft[DataT]:
    """Build one tool-domain event draft (run metadata by default, §3.1 L110).

    ``metadata=None`` pins ``{"run_id": context.run_id}`` (required by the
    fold for run-scoped events); the session-scoped permission trio members
    (saved/revoked/expired) pass ``metadata={}`` instead -- they are
    deliberately outside RUN_SCOPED_EVENT_TYPES.
    """
    return EventDraft[DataT](
        id=ids.event_id(),
        type=event_type,
        version=EVENT_VERSION,
        session_id=context.session_id,
        created_at=clock(),
        data=data,
        metadata={"run_id": context.run_id} if metadata is None else metadata,
    )


async def _emit[DataT: BaseModel](
    sink: EventSink,
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    event_type: str,
    data: DataT,
) -> None:
    """Commit one tool event through the Invoker's callback (§5.1 L257).

    Each phase commits as its own single-event batch: the sequence's
    ordering guarantees (requested before validation, started confirmation
    before execution, finished after normalization) are per-event commits.
    Sink errors propagate (§7 L341: storage infrastructure errors fail
    closed; no synthetic finished event).
    """
    await sink((_draft(ids, clock, context, event_type, data),))


async def _finish(
    *,
    sink: EventSink,
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    execution_id: str,
    requested_name: str,
    tool_ref: ToolRef | None,
    input_hash: str,
    state: Literal["succeeded", "failed", "denied", "cancelled", "unknown"],
    effect_status: Literal["not_started", "completed", "unknown"],
    content: Sequence[ToolContent],
    structured_output: JsonValue,
    error: CoreError | None,
    pre: Sequence[EventDraft[Any]] = (),
) -> ToolOutcome:
    """Commit the terminal ``py.tool.finished`` (plus optional same-batch predecessors).

    ``pre`` carries the T-B-027 ④-2 pairing: an approval protocol event
    (replied / closed) that must land in the *same* batch as the not-started
    terminal it causes, permission event first (§3.3 L212 closed-first), so
    no path leaves a permanently pending permission.
    """
    finished = _draft(
        ids,
        clock,
        context,
        PY_TOOL_FINISHED,
        ToolFinishedData(
            execution_id=execution_id,
            state=state,
            effect_status=effect_status,
            content=list(content),
            structured_output=structured_output,
            error=error,
        ),
    )
    await sink((*pre, finished))
    return ToolOutcome(
        execution_id=execution_id,
        requested_name=requested_name,
        tool_ref=tool_ref,
        input_hash=input_hash,
        state=state,
        effect_status=effect_status,
        content=tuple(content),
        structured_output=structured_output,
        error=error,
    )


# ---------------------------------------------------------------------------
# The §5.1 L259 policy/approval gate (R3-S2)
# ---------------------------------------------------------------------------


async def _apply_policy(
    *,
    policy: PermissionPolicy,
    sink: EventSink,
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    tool: ResolvedTool,
    tool_ref: ToolRef,
    requested_name: str,
    raw_input: JsonObject,
    input_hash: str,
    execution_id: str,
) -> ToolOutcome | Literal["started", "proceed"]:
    """The §5.1 L259 policy gate between validation and ``py.tool.started``.

    Returns a terminal :class:`ToolOutcome` when the call must not start
    (policy deny / host reject / expiry / cancellation / unconnected
    approval channel); ``"started"`` when the approved-ask flow already
    committed the ``[replied (, saved), started]`` batch (the caller
    proceeds straight to adapter execution); ``"proceed"`` when a plain
    allow leaves the started confirmation to the caller. The input is
    already strictly validated and is never modified here (§5.1 L261).
    """
    action = _wire_name(tool.spec)
    resources = extract_resources(action, raw_input)
    due = policy.due_expired(context.session_id)
    if due:
        # §5.2 L270: expire due saved decisions durably first (through the
        # same commit-lock sink, session-scoped metadata), then re-evaluate
        # -- the expired decision no longer matches.
        await sink(
            tuple(
                _draft(
                    ids,
                    clock,
                    context,
                    PY_PERMISSION_EXPIRED,
                    PermissionExpiredData(decision_id=decision.decision_id, reason=REASON_DECISION_EXPIRED),
                    metadata={},
                )
                for decision in due
            )
        )
        policy.mark_expired([decision.decision_id for decision in due])
    decision = policy.evaluate(
        session_id=context.session_id,
        action=action,
        resources=resources,
        input_hash=input_hash,
        working_dir=context.working_dir,
        side_effects=tool.spec.side_effects,
    )
    if decision.effect == "deny":
        # §5.2 L268: a policy deny never fakes a host reply -- no
        # py.permission.* event, straight to the §2.1 L47 denied terminal.
        message = f'Tool "{requested_name}" was denied by the permission policy: {decision.reason}'
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="denied",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.PERMISSION_DENIED, message=message, retryable=False),
        )
    if decision.effect == "allow":
        return "proceed"
    return await _request_approval(
        policy=policy,
        sink=sink,
        ids=ids,
        clock=clock,
        context=context,
        tool=tool,
        tool_ref=tool_ref,
        requested_name=requested_name,
        action=action,
        resources=resources,
        input_hash=input_hash,
        execution_id=execution_id,
    )


async def _request_approval(
    *,
    policy: PermissionPolicy,
    sink: EventSink,
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    tool: ResolvedTool,
    tool_ref: ToolRef,
    requested_name: str,
    action: str,
    resources: tuple[str, ...],
    input_hash: str,
    execution_id: str,
) -> ToolOutcome | Literal["started"]:
    """The §5.2 L268 approval protocol for one ask decision (300 s default).

    ``py.permission.requested`` commits alone first (the durable record
    precedes the in-memory wait; T-B-027 ④-1: the Invoker is the sole
    py.permission.requested/replied/closed producer). Without a connected
    approval channel the call folds to closed(cancelled) + the cancelled/
    not_started terminal carrying ApprovalUnavailable -- never a silent
    allow. With one, the registered future resolves exactly once (§7 L173
    CAS) and the outcome pairs with the tool terminal in one batch (④-2):
    allow -> ``[replied (, saved), started]``; reject -> ``[replied,
    finished(denied)]``; expired/cancelled -> ``[closed, finished]``
    closed-first (§3.3 L212).
    """
    approvals: ApprovalCoordinator | None = policy.approvals
    now = clock()
    expires_at = (
        approvals.expires_at(now) if approvals is not None else now + timedelta(seconds=DEFAULT_APPROVAL_TIMEOUT_S)
    )
    permission_id = policy.mint_permission_id()
    await _emit(
        sink,
        ids,
        clock,
        context,
        PY_PERMISSION_REQUESTED,
        PermissionRequestedData(
            permission_id=permission_id,
            execution_id=execution_id,
            action=action,
            resources=list(resources),
            input_hash=input_hash,
            expires_at=expires_at,
        ),
    )
    if approvals is None:
        # §5.2 L268: approval channel not connected -> closed(cancelled) +
        # ApprovalUnavailable, the pending tool ends cancelled/not_started.
        message = f'Tool "{requested_name}" was cancelled: no approval channel is connected.'
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="cancelled",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.APPROVAL_UNAVAILABLE, message=message, retryable=False),
            pre=(
                _draft(
                    ids,
                    clock,
                    context,
                    PY_PERMISSION_CLOSED,
                    PermissionClosedData(
                        permission_id=permission_id, outcome="cancelled", reason=REASON_APPROVAL_UNCONNECTED
                    ),
                ),
            ),
        )
    future = await approvals.register(
        PermissionRequest(
            permission_id=permission_id,
            session_id=context.session_id,
            run_id=context.run_id,
            execution_id=execution_id,
            action=action,
            resources=resources,
            input_hash=input_hash,
            expires_at=expires_at,
        )
    )
    outcome = await future
    if outcome.outcome == "allow":
        # §7 L173: re-check the cooperative cancel after the wait -- a
        # started commit is fold-illegal once the run is cancelling, so an
        # allow racing a cancel folds to the cancelled pair instead (the
        # reply stays in-memory; the permission is still fold-pending).
        if context.cancel.is_set():
            message = f'Tool "{requested_name}" was cancelled before it started.'
            return await _finish(
                sink=sink,
                ids=ids,
                clock=clock,
                context=context,
                execution_id=execution_id,
                requested_name=requested_name,
                tool_ref=tool_ref,
                input_hash=input_hash,
                state="cancelled",
                effect_status="not_started",
                content=[ToolTextContent(type="text", text=message)],
                structured_output=None,
                error=None,
                pre=(
                    _draft(
                        ids,
                        clock,
                        context,
                        PY_PERMISSION_CLOSED,
                        PermissionClosedData(
                            permission_id=permission_id, outcome="cancelled", reason=REASON_APPROVAL_RUN_CANCELLED
                        ),
                    ),
                ),
            )
        # an allow resolves only from a host reply, so the vocabulary is pinned
        assert outcome.reply is not None
        await _start_approved(
            policy=policy,
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            action=action,
            resources=resources,
            input_hash=input_hash,
            execution_id=execution_id,
            permission_id=permission_id,
            reply=outcome.reply,
        )
        return "started"
    if outcome.outcome == "deny":
        # §5.2 L268: an actual reject writes py.permission.replied, paired
        # same-batch with the denied/not_started terminal (④-2).
        message = f'Tool "{requested_name}" was rejected by the host approval.'
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="denied",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.PERMISSION_DENIED, message=message, retryable=False),
            pre=(
                _draft(
                    ids,
                    clock,
                    context,
                    PY_PERMISSION_REPLIED,
                    PermissionRepliedData(permission_id=permission_id, reply="reject", source="host"),
                ),
            ),
        )
    # expired / cancelled: the system close, paired closed-first with the
    # cancelled/not_started terminal (§3.3 L212; expiry surfaces
    # ApprovalUnavailable, a plain cancellation carries no error).
    closed_outcome: Literal["expired", "cancelled", "restart"] = (
        "expired" if outcome.outcome == "expired" else "cancelled"
    )
    if outcome.outcome == "expired":
        message = f'Tool "{requested_name}" was cancelled: the approval timed out.'
        error: CoreError | None = CoreError(code=CoreErrorCode.APPROVAL_UNAVAILABLE, message=message, retryable=False)
    else:
        message = f'Tool "{requested_name}" was cancelled before it started.'
        error = None
    return await _finish(
        sink=sink,
        ids=ids,
        clock=clock,
        context=context,
        execution_id=execution_id,
        requested_name=requested_name,
        tool_ref=tool_ref,
        input_hash=input_hash,
        state="cancelled",
        effect_status="not_started",
        content=[ToolTextContent(type="text", text=message)],
        structured_output=None,
        error=error,
        pre=(
            _draft(
                ids,
                clock,
                context,
                PY_PERMISSION_CLOSED,
                PermissionClosedData(permission_id=permission_id, outcome=closed_outcome, reason=outcome.reason),
            ),
        ),
    )


async def _start_approved(
    *,
    policy: PermissionPolicy,
    sink: EventSink,
    ids: IdFactory,
    clock: Clock,
    context: ToolContext,
    action: str,
    resources: tuple[str, ...],
    input_hash: str,
    execution_id: str,
    permission_id: str,
    reply: Literal["once", "always", "reject"],
) -> None:
    """Commit the ``[replied (, saved), started]`` batch for an approved ask (④-2).

    The host reply carries "allow" onto ``py.tool.started`` (the §5.2
    effect vocabulary records the decision that took effect, not the ask
    that preceded it). An "always" reply adds ``py.permission.saved`` (the
    concrete action/resource rule, session+boot scoped, no wildcards) and
    registers the in-memory decision only after the durable commit.
    """
    replied = _draft(
        ids,
        clock,
        context,
        PY_PERMISSION_REPLIED,
        PermissionRepliedData(permission_id=permission_id, reply=reply, source="host"),
    )
    started = _draft(
        ids,
        clock,
        context,
        PY_TOOL_STARTED,
        ToolStartedData(execution_id=execution_id, permission_decision="allow", input_hash=input_hash),
    )
    if reply == "always":
        decision_id = policy.mint_decision_id()
        saved = _draft(
            ids,
            clock,
            context,
            PY_PERMISSION_SAVED,
            PermissionSavedData(
                decision_id=decision_id,
                scope={"session_id": context.session_id, "boot_id": policy.boot_id},
                rule={"action": action, "resources": list(resources)},
                expires_at=None,
                boot_id=policy.boot_id,
            ),
            metadata={},
        )
        await sink((replied, saved, started))
        policy.register_saved(
            SavedDecision(
                decision_id=decision_id,
                session_id=context.session_id,
                action=action,
                resources=resources,
                input_hash=input_hash,
                boot_id=policy.boot_id,
                expires_at=None,
            )
        )
        return
    await sink((replied, started))


async def invoke_tool(
    *,
    tool: ResolvedTool | None,
    context: ToolContext,
    requested_name: str,
    raw_input: JsonObject,
    execution_id: str,
    sink: EventSink,
    clock: Clock,
    ids: IdFactory,
    policy: PermissionPolicy | None = None,
) -> ToolOutcome:
    """Drive the §5.1 L257 sequence for one tool call (registry-internal).

    The caller (:meth:`ToolRegistry.invoke`) owns the serialization lock
    and the pinned-catalog resolution. Adapter exceptions become
    model-readable failed results; ``asyncio.CancelledError`` is *not*
    caught (hard cancellation propagates; R2 cancellation flows through
    the cooperative signal and the pre-start checkpoint, and an unfinished
    started call is closed by the R2-S7 close/cancel paths). With an
    injected policy the §5.1 L259 policy/approval gate runs between the
    cancellation checkpoint and ``py.tool.started`` (§5.2: PermissionPolicy
    is injected into the Invoker; a tool never approves itself); without
    one the R2 default-allow face is unchanged (§5.2 L264).
    """
    if not isinstance(raw_input, dict):
        raise ToolInvokeError("input must be a JSON object")
    if not isinstance(execution_id, str) or not execution_id:
        raise ToolInvokeError("execution_id must be a non-empty string")
    try:
        input_hash = sha256(canonical_json(raw_input).encode("utf-8")).hexdigest()
    except ValueError as error:
        raise ToolInvokeError("input must be canonical-JSON encodable (NaN/Infinity are not JSON)") from error
    tool_ref = (
        None if tool is None else ToolRef(namespace=tool.spec.namespace, name=tool.spec.name, version=tool.spec.version)
    )

    # §5.1 L257: record the raw request first -- the original requested_name,
    # the resolved tool_ref (None when resolution will fail), the canonical
    # input and its hash (§3.1 L129).
    await _emit(
        sink,
        ids,
        clock,
        context,
        PY_TOOL_REQUESTED,
        ToolRequestedData(
            message_id=context.message_id,
            tool_call_id=context.tool_call_id,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input=raw_input,
            input_hash=input_hash,
        ),
    )

    # Catalog miss: failed/not_started, no started event (§2.1 L46).
    if tool is None:
        message = (
            f'No tool named "{requested_name}" is currently available. Please use a tool from the available tool list.'
        )
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=None,
            input_hash=input_hash,
            state="failed",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.NOT_FOUND, message=message, retryable=False),
        )

    # Strict schema validation: no implicit conversion, no modification.
    issues = validate_input_strict(tool.spec.input_schema, raw_input)
    if issues:
        message = _format_issues(requested_name, issues, raw_input)
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="failed",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.VALIDATION_ERROR, message=message, retryable=False),
        )

    # Cancellation checkpoint: started requires not-cancelled (§2.1 L45);
    # the terminal keeps its not_started effect status (§2.1 L48).
    if context.cancel.is_set():
        message = f'Tool "{requested_name}" was cancelled before it started.'
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="cancelled",
            effect_status="not_started",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=None,
        )

    # §5.1 L259: the policy/approval gate sits between validation and the
    # started confirmation. Without an injected policy (the R2 face) the
    # §5.2 L264 default allow goes on the wire unchanged.
    # the catalog-miss terminal returned above: the resolved tool and its
    # tool_ref are pinned from here on (the type system carries no such join)
    assert tool_ref is not None
    gate: ToolOutcome | Literal["started", "proceed"]
    if policy is not None:
        gate = await _apply_policy(
            policy=policy,
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            tool=tool,
            tool_ref=tool_ref,
            requested_name=requested_name,
            raw_input=raw_input,
            input_hash=input_hash,
            execution_id=execution_id,
        )
        if isinstance(gate, ToolOutcome):
            return gate
    else:
        gate = "proceed"
    if gate == "proceed":
        # R3-S2: every started commit carries the effective decision on the
        # wire (the R2_PERMISSION_DECISION placeholder is gone); both the
        # policy-allow and default-allow paths record "allow".
        await _emit(
            sink,
            ids,
            clock,
            context,
            PY_TOOL_STARTED,
            ToolStartedData(
                execution_id=execution_id,
                permission_decision="allow",
                input_hash=input_hash,
            ),
        )
    # gate == "started": the approved-ask batch [replied (, saved), started]
    # is already committed -- proceed straight to adapter execution.

    exec_context = replace(context, progress=_bounded_progress(context.progress, context.limits))
    try:
        result = await tool.adapter.execute(exec_context, raw_input)
    except Exception as error:
        # §2.1 L50: the started commit already proved the call began, so a
        # crash after it cannot claim the effect is not_started -- every
        # post-start failure is effect=unknown, even for a side-effect-free
        # tool (T-C1-033: the old side_effects ternary produced the illegal
        # failed/not_started pair, which the fold rejects from running and
        # which wedged the run).
        if policy is not None:
            # §5.2 L270: an identical retry after an unknown-effect terminal
            # must re-ask, never reuse an old once/always decision.
            policy.record_unknown(context.session_id, _wire_name(tool.spec), input_hash)
        message = f'Tool "{requested_name}" failed: {type(error).__name__}: {error}'
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="failed",
            effect_status="unknown",
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.TOOL_FAILED, message=message, retryable=False),
        )

    content, structured_output = _normalize_result(tool.spec, result, context.limits)
    if result.error is not None:
        # A graceful adapter failure: the pre-start policy deny maps to the
        # §2.1 L47 "denied" terminal (R3-S2 gate above); a failure reported
        # by the adapter itself arrives after the started commit, so it ends
        # failed/unknown (§2.1 L50 -- a started call cannot claim
        # not_started, even without side effects; T-C1-033 re-mapping).
        if policy is not None:
            # §5.2 L270: same unknown-effect retry rule as the exception path.
            policy.record_unknown(context.session_id, _wire_name(tool.spec), input_hash)
        return await _finish(
            sink=sink,
            ids=ids,
            clock=clock,
            context=context,
            execution_id=execution_id,
            requested_name=requested_name,
            tool_ref=tool_ref,
            input_hash=input_hash,
            state="failed",
            effect_status="unknown",
            content=content,
            structured_output=structured_output,
            error=result.error,
        )
    return await _finish(
        sink=sink,
        ids=ids,
        clock=clock,
        context=context,
        execution_id=execution_id,
        requested_name=requested_name,
        tool_ref=tool_ref,
        input_hash=input_hash,
        state="succeeded",
        effect_status="completed",
        content=content,
        structured_output=structured_output,
        error=None,
    )
