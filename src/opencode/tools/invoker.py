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
4. ``py.tool.started`` commit confirmation -- R2 has no PermissionPolicy,
   so the §5.2 L264 default allow goes into the event's
   ``permission_decision`` (py.permission.* events are R3; float report);
5. adapter execution with a bounded progress callback, then result
   normalization (§2.1 L72 model-readable text, §5.3 L276
   structured_output/progress-metadata caps);
6. ``py.tool.finished`` commit; every terminal carries at least one
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
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
    canonical_json,
)
from opencode.contracts.messages import JsonObject, JsonValue, ToolContent, ToolTextContent
from opencode.contracts.tools import ToolRef, ToolResult, ToolSpec
from opencode.session.service import Clock, IdFactory

if TYPE_CHECKING:
    # ResolvedTool carries the adapter (registry-owned protocol); string
    # annotation only -- no runtime import cycle.
    from opencode.tools.registry import ToolAdapter

_KIB = 1024
_MIB = 1024 * 1024

#: §5.2 L264: R2 has no PermissionPolicy (R3 injects it into the Invoker);
#: every R2 py.tool.started carries the default allow decision.
R2_PERMISSION_DECISION: Literal["allow"] = "allow"


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


def _noop_progress(_metadata: JsonObject) -> None:
    """Progress with no runner callback: adapters may always call it."""


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
    draft = EventDraft[DataT](
        id=ids.event_id(),
        type=event_type,
        version=EVENT_VERSION,
        session_id=context.session_id,
        created_at=clock(),
        data=data,
        metadata={"run_id": context.run_id},
    )
    await sink((draft,))


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
) -> ToolOutcome:
    await _emit(
        sink,
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
) -> ToolOutcome:
    """Drive the §5.1 L257 sequence for one tool call (registry-internal).

    The caller (:meth:`ToolRegistry.invoke`) owns the serialization lock
    and the pinned-catalog resolution. Adapter exceptions become
    model-readable failed results; ``asyncio.CancelledError`` is *not*
    caught (hard cancellation propagates; R2 cancellation flows through
    the cooperative signal and the pre-start checkpoint, and an unfinished
    started call is closed by the R2-S7 close/cancel paths).
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

    # R2: no PermissionPolicy (§5.2 L264 default allow goes on the wire;
    # R3 replaces this with the policy decision at this exact point).
    await _emit(
        sink,
        ids,
        clock,
        context,
        PY_TOOL_STARTED,
        ToolStartedData(
            execution_id=execution_id,
            permission_decision=R2_PERMISSION_DECISION,
            input_hash=input_hash,
        ),
    )

    exec_context = replace(context, progress=_bounded_progress(context.progress, context.limits))
    try:
        result = await tool.adapter.execute(exec_context, raw_input)
    except Exception as error:
        effect: Literal["not_started", "completed", "unknown"] = (
            "not_started" if not tool.spec.side_effects else "unknown"
        )
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
            effect_status=effect,
            content=[ToolTextContent(type="text", text=message)],
            structured_output=None,
            error=CoreError(code=CoreErrorCode.TOOL_FAILED, message=message, retryable=False),
        )

    content, structured_output = _normalize_result(tool.spec, result, context.limits)
    if result.error is not None:
        # A graceful adapter failure: no policy layer in R2 maps an adapter
        # PermissionDenied to the §2.1 L47 "denied" terminal (that
        # transition is pending→denied, i.e. pre-start policy; R3). The
        # call already started, so it ends failed (§2.1 L50).
        effect = "not_started" if not tool.spec.side_effects else "unknown"
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
            effect_status=effect,
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
