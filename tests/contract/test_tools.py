"""R2-S5 contract tests: registry registration/snapshot rules + the invoke pipeline.

Fake adapters plus a recording EventSink drive the §5.1 L257 sequence
without touching the filesystem (the real ``fs__read`` adapter is covered
by ``tests/integration/test_tool_read.py``; committing ``py.tool.*``
events through the real SessionStore is runner work, R2-S6, so the sink
here records the exact drafts the Invoker commits). Contract anchors:
plan §2 R2-S5 ④, CORE-CONTRACTS §5.1 L247-257, §2.1 L46-50/L72, §7 L341.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import (
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
    canonical_json,
)
from opencode.contracts.messages import JsonObject, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolRef, ToolResult, ToolSpec
from opencode.session.service import IdFactory
from opencode.tools import (
    RegistrationError,
    ToolAdapter,
    ToolContext,
    ToolInvokeError,
    ToolLimits,
    ToolRegistry,
)

_FIXED_TIME = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    """EventSink stand-in: records every committed batch verbatim."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []
        self.drafts: list[EventDraft[BaseModel]] = []

    async def __call__(self, drafts: Sequence[EventDraft[BaseModel]]) -> None:
        self.batches.append(tuple(drafts))
        self.drafts.extend(drafts)


class _FakeAdapter:
    """ToolAdapter implementation the tests script: result / exception / progress."""

    def __init__(
        self,
        spec: ToolSpec,
        result: ToolResult | None = None,
        *,
        exc: Exception | None = None,
        progress: list[JsonObject] | None = None,
    ) -> None:
        self._spec = spec
        self.result = result
        self.exc = exc
        self.progress = progress
        self.calls: list[JsonObject] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        self.calls.append(input)
        if self.progress is not None and context.progress is not None:
            for metadata in self.progress:
                await context.progress(metadata)
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        return ToolResult(content=[ToolTextContent(type="text", text="ok")])


def _spec(
    *,
    namespace: str = "fs",
    name: str = "read",
    version: int = 1,
    input_schema: JsonObject | None = None,
    side_effects: bool = False,
) -> ToolSpec:
    if input_schema is None:
        input_schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        }
    return ToolSpec(
        namespace=namespace,
        name=name,
        version=version,
        description="contract-test tool",
        input_schema=input_schema,
        supported_content_types=["text"],
        side_effects=side_effects,
        idempotency_support=True,
    )


def _context(
    *,
    session_id: str = "ses_contract",
    limits: ToolLimits | None = None,
    cancel: asyncio.Event | None = None,
    progress: object = None,
) -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_contract",
        message_id="msg_contract",
        tool_call_id="call_contract",
        working_dir="C:/unused-working-dir",
        limits=limits if limits is not None else ToolLimits(),
        cancel=cancel if cancel is not None else asyncio.Event(),
        progress=cast("Any", progress),
    )


def _registry(sink: object = None, *, ids: IdFactory | None = None) -> ToolRegistry:
    return ToolRegistry(
        sink=cast("Any", sink) if sink is not None else _RecordingSink(),
        clock=_clock,
        ids=ids if ids is not None else _FixedIds(),
    )


def _input_hash(input: JsonObject) -> str:
    return sha256(canonical_json(input).encode("utf-8")).hexdigest()


def _requested(draft: EventDraft[BaseModel]) -> ToolRequestedData:
    assert draft.type == PY_TOOL_REQUESTED
    assert isinstance(draft.data, ToolRequestedData)
    return draft.data


def _started(draft: EventDraft[BaseModel]) -> ToolStartedData:
    assert draft.type == PY_TOOL_STARTED
    assert isinstance(draft.data, ToolStartedData)
    return draft.data


def _finished(draft: EventDraft[BaseModel]) -> ToolFinishedData:
    assert draft.type == PY_TOOL_FINISHED
    assert isinstance(draft.data, ToolFinishedData)
    return draft.data


# ---------------------------------------------------------------------------
# register / snapshot (§5.1 L251-253)
# ---------------------------------------------------------------------------


async def test_register_and_snapshot_catalog() -> None:
    registry = _registry()
    spec_a = _spec()
    spec_b = _spec(namespace="app", name="echo")
    registry.register(_FakeAdapter(spec_a))
    registry.register(_FakeAdapter(spec_b))
    catalog = registry.snapshot("ses_catalog")

    # Sorted by (namespace, name): app__echo before fs__read.
    assert [s.namespace for s in catalog.specs] == ["app", "fs"]
    assert catalog.resolve("fs__read") == spec_a
    assert catalog.resolve("app__echo") == spec_b
    assert catalog.resolve("fs__missing") is None


async def test_snapshot_is_pinned_per_session() -> None:
    sink = _RecordingSink()
    registry_with_sink = ToolRegistry(sink=sink, clock=_clock, ids=_FixedIds())
    spec_a = _spec()
    adapter_b = _FakeAdapter(_spec(name="echo"))
    registry_with_sink.register(_FakeAdapter(spec_a))
    registry_with_sink.snapshot("ses_first")

    # Registering later must not leak into the already-pinned session...
    registry_with_sink.register(adapter_b)
    pinned_context = _context(session_id="ses_first")
    outcome = await registry_with_sink.invoke(pinned_context, "fs__echo", {"text": "hi"}, "exec_001")
    assert outcome.state == "failed"
    assert outcome.effect_status == "not_started"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND

    # ...until that session takes a new snapshot (each request re-pins).
    registry_with_sink.snapshot("ses_first")
    outcome = await registry_with_sink.invoke(pinned_context, "fs__echo", {"text": "hi"}, "exec_002")
    assert outcome.state == "succeeded"


async def test_register_rejects_exact_duplicate() -> None:
    registry = _registry()
    registry.register(_FakeAdapter(_spec()))
    with pytest.raises(RegistrationError, match="already registered"):
        registry.register(_FakeAdapter(_spec()))


async def test_register_rejects_wire_name_version_collision() -> None:
    registry = _registry()
    registry.register(_FakeAdapter(_spec()))
    with pytest.raises(RegistrationError, match="already registered as v1"):
        registry.register(_FakeAdapter(_spec(version=2)))


async def test_register_rejects_invalid_names() -> None:
    registry = _registry()
    # T-B-017 n-1: the "__" rejection is pinned on both sides ("a__b" as a
    # name and as a namespace), not just the namespace pattern failure.
    for bad_spec in (
        _spec(namespace="bad ns"),
        _spec(name="a__b"),
        _spec(name="a/b"),
        _spec(namespace="a__b"),
    ):
        with pytest.raises(RegistrationError, match="wire-name reversibility"):
            registry.register(_FakeAdapter(bad_spec))


async def test_register_rejects_namespace_ending_with_underscore() -> None:
    """T-B-017 M-1: ("fs_", "read") wires to "fs___read" -- the same wire name
    as ("fs", "_read") -- and would silently shadow it in the snapshot
    (provider-visible but not callable) and break the first-__ round trip.
    The partition guard rejects it."""
    registry = _registry()
    with pytest.raises(RegistrationError, match="must not end with '_'"):
        registry.register(_FakeAdapter(_spec(namespace="fs_")))


async def test_register_leading_underscore_name_round_trips_over_wire() -> None:
    """T-B-017 M-1 pinning: ("fs", "_read") stays registrable (a single
    leading "_" in the name is legal) and is the unique surviving
    interpretation of the wire name "fs___read" on both sides of the S5<->S3
    seam (registry snapshot/invoke + the maas first-__ reverse parse)."""
    from opencode.ai.maas import parse_tool_wire_name, tool_wire_name_from_dotted

    registry = _registry(_RecordingSink())
    spec = _spec(name="_read")
    adapter = _FakeAdapter(spec)
    registry.register(adapter)

    # Snapshot-visible and resolvable back to exactly the registered pair.
    catalog = registry.snapshot("ses_contract")
    assert catalog.resolve("fs___read") == spec

    # Invocable through the raw wire name the model would send.
    outcome = await registry.invoke(_context(), "fs___read", {"text": "hi"}, "exec_001")
    assert outcome.state == "succeeded"
    assert outcome.tool_ref == ToolRef(namespace="fs", name="_read", version=1)
    assert adapter.calls == [{"text": "hi"}]

    # S3 adapter side: "fs___read" reverse-parses to the dotted "fs._read"
    # (and the dotted form maps back), matching the registered (fs, _read).
    assert parse_tool_wire_name("fs___read") == "fs._read"
    assert tool_wire_name_from_dotted("fs._read") == "fs___read"


async def test_register_rejects_schema_outside_validator_subset() -> None:
    registry = _registry()
    with pytest.raises(RegistrationError, match="unsupported schema keyword"):
        registry.register(_FakeAdapter(_spec(input_schema={"type": "object", "pattern": "^x$"})))
    with pytest.raises(RegistrationError, match="root input schema must be of type 'object'"):
        registry.register(_FakeAdapter(_spec(input_schema={"type": "string"})))


async def test_register_rejects_malformed_adapter() -> None:
    class _JunkSpec:
        spec: Any = "not-a-spec"

        async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
            raise AssertionError("never reached")

    class _NoExecute:
        def __init__(self, spec: ToolSpec) -> None:
            self._spec = spec

        @property
        def spec(self) -> ToolSpec:
            return self._spec

    class _NotCallableExecute(_NoExecute):
        execute: Any = "not-callable"

    registry = _registry()
    with pytest.raises(RegistrationError, match=r"adapter\.spec must be a ToolSpec"):
        registry.register(_JunkSpec())
    with pytest.raises(RegistrationError, match=r"adapter\.execute must be a callable"):
        registry.register(cast(ToolAdapter, _NoExecute(_spec())))
    with pytest.raises(RegistrationError, match=r"adapter\.execute must be a callable"):
        registry.register(cast(ToolAdapter, _NotCallableExecute(_spec())))


async def test_snapshot_rejects_bad_session_id() -> None:
    registry = _registry()
    for bad in ("", "session-1", 123):
        with pytest.raises(ToolInvokeError, match="session_id"):
            registry.snapshot(cast("Any", bad))


# ---------------------------------------------------------------------------
# invoke: the §5.1 L257 sequence
# ---------------------------------------------------------------------------


async def test_invoke_requires_event_sink() -> None:
    registry = ToolRegistry(sink=None, clock=_clock, ids=_FixedIds())
    with pytest.raises(ToolInvokeError, match="event sink"):
        await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")


async def test_invoke_rejects_malformed_arguments() -> None:
    registry = _registry()
    with pytest.raises(ToolInvokeError, match="raw requested name"):
        await registry.invoke(_context(), "", {"text": "hi"}, "exec_001")
    with pytest.raises(ToolInvokeError, match="input must be a JSON object"):
        await registry.invoke(_context(), "fs__read", cast(JsonObject, ["nope"]), "exec_001")
    with pytest.raises(ToolInvokeError, match="NaN"):
        await registry.invoke(_context(), "fs__read", {"text": cast("Any", float("nan"))}, "exec_001")
    with pytest.raises(ToolInvokeError, match="execution_id"):
        await registry.invoke(_context(), "fs__read", {"text": "hi"}, "")


async def test_invoke_unknown_tool_is_failed_not_started() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    input: JsonObject = {"text": "hi"}

    outcome = await registry.invoke(_context(), "fs__read", input, "exec_001")

    assert outcome.state == "failed"
    assert outcome.effect_status == "not_started"
    assert outcome.tool_ref is None
    assert outcome.requested_name == "fs__read"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert outcome.input_hash == _input_hash(input)
    assert any(isinstance(part, ToolTextContent) for part in outcome.content)

    # No py.tool.started for a call that never started (§2.1 L46).
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_FINISHED]
    requested = _requested(sink.drafts[0])
    assert requested.requested_name == "fs__read"
    assert requested.tool_ref is None
    assert requested.input == input
    assert requested.input_hash == _input_hash(input)
    assert requested.execution_id == "exec_001"
    finished = _finished(sink.drafts[1])
    assert finished.state == "failed"
    assert finished.effect_status == "not_started"


async def test_invoke_schema_failure_is_failed_not_started() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    schema: JsonObject = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    adapter = _FakeAdapter(_spec(input_schema=schema))
    registry.register(adapter)
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"path": 123}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.effect_status == "not_started"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "expected a string" in outcome.error.message
    assert "Update the arguments" in outcome.error.message
    assert adapter.calls == []  # never reached the adapter
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_FINISHED]


async def test_invoke_does_not_coerce_input_types() -> None:
    registry = _registry(_RecordingSink())
    schema: JsonObject = {"type": "object", "properties": {"offset": {"type": "integer"}}}
    registry.register(_FakeAdapter(_spec(input_schema=schema)))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"offset": True}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "expected an integer (no implicit conversion)" in outcome.error.message


async def test_invoke_minimum_bound_is_enforced_before_adapter() -> None:
    """T-B-017 ③-7: a JSON-legal integer 0 violates the schema-side minimum
    (``minimum: 1`` on offset -- the Invoker's ``_bounds_issues`` path, "must
    be >= 1"), a different checkpoint from the adapter's line-count check
    (offset=99 in test_tool_read.py pins that one). The path value is inert:
    the input is rejected before the adapter is ever reached."""
    from opencode.tools.builtin.read import READ_INPUT_SCHEMA

    registry = _registry(_RecordingSink())
    adapter = _FakeAdapter(_spec(input_schema=READ_INPUT_SCHEMA))
    registry.register(adapter)
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"path": "notes.txt", "offset": 0}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.effect_status == "not_started"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "offset: must be >= 1" in outcome.error.message
    assert adapter.calls == []  # schema rejection: the adapter is never reached


async def test_invoke_success_sequence() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    spec = _spec()
    registry.register(_FakeAdapter(spec))
    input: JsonObject = {"text": "hi"}
    context = _context()
    registry.snapshot(context.session_id)

    outcome = await registry.invoke(context, "fs__read", input, "exec_001")

    assert outcome.state == "succeeded"
    assert outcome.effect_status == "completed"
    assert outcome.error is None
    assert outcome.tool_ref == ToolRef(namespace="fs", name="read", version=1)
    assert outcome.input_hash == _input_hash(input)

    # requested -> started -> finished, one event per batch (§5.1 L257).
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED, PY_TOOL_FINISHED]
    assert [len(batch) for batch in sink.batches] == [1, 1, 1]
    for draft in sink.drafts:
        assert draft.version == 1
        assert draft.session_id == context.session_id
        assert draft.created_at == _FIXED_TIME
        assert draft.metadata == {"run_id": context.run_id}
    assert [draft.id for draft in sink.drafts] == ["evt_000001", "evt_000002", "evt_000003"]

    requested = _requested(sink.drafts[0])
    assert requested.message_id == context.message_id
    assert requested.tool_call_id == context.tool_call_id
    assert requested.execution_id == "exec_001"
    assert requested.tool_ref == ToolRef(namespace="fs", name="read", version=1)
    assert requested.input == input
    assert requested.input_hash == _input_hash(input)
    started = _started(sink.drafts[1])
    assert started.execution_id == "exec_001"
    assert started.permission_decision == "allow"  # §5.2 L264: R2 has no policy layer
    assert started.input_hash == _input_hash(input)
    finished = _finished(sink.drafts[2])
    assert finished.execution_id == "exec_001"
    assert finished.state == "succeeded"
    assert finished.effect_status == "completed"
    assert finished.structured_output is None
    assert finished.error is None
    assert [part.type for part in finished.content] == ["text"]
    assert finished.content[0].type == "text"
    assert finished.content[0].text == "ok"


async def test_invoke_passes_input_through_unmodified() -> None:
    registry = _registry(_RecordingSink())
    adapter = _FakeAdapter(_spec())
    registry.register(adapter)
    input: JsonObject = {"text": "hi"}
    registry.snapshot("ses_contract")
    context = _context()

    await registry.invoke(context, "fs__read", input, "exec_001")

    assert adapter.calls == [input]
    assert adapter.calls[0] is input  # same object: no copy, no coercion


async def test_cancel_before_start_is_cancelled_not_started() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    adapter = _FakeAdapter(_spec())
    registry.register(adapter)
    cancel = asyncio.Event()
    cancel.set()
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(cancel=cancel), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "cancelled"
    assert outcome.effect_status == "not_started"
    assert adapter.calls == []  # §2.1 L45: started requires not-cancelled
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_FINISHED]
    finished = _finished(sink.drafts[1])
    assert finished.state == "cancelled"
    assert "cancelled before it started" in cast(ToolTextContent, finished.content[0]).text


# ---------------------------------------------------------------------------
# adapter failures and result normalization (§7 L341, §2.1 L72, §5.3 L276)
# ---------------------------------------------------------------------------


async def test_adapter_exception_becomes_failed_result() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    registry.register(_FakeAdapter(_spec(), exc=RuntimeError("boom")))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.effect_status == "not_started"  # side_effects=False
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "RuntimeError" in outcome.error.message
    assert "boom" in outcome.error.message
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED, PY_TOOL_FINISHED]


async def test_adapter_exception_with_side_effects_keeps_effect_unknown() -> None:
    registry = _registry(_RecordingSink())
    registry.register(_FakeAdapter(_spec(side_effects=True), exc=RuntimeError("boom")))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"  # §2.1: started call, undetermined effect


async def test_adapter_error_result_maps_to_failed() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    error = CoreError(code=CoreErrorCode.TOOL_FAILED, message="graceful failure", retryable=False)
    registry.register(_FakeAdapter(_spec(), result=ToolResult(content=[], error=error)))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "failed"
    assert outcome.error == error
    # §2.1 L72: the terminal still carries model-readable text.
    assert cast(ToolTextContent, outcome.content[0]).text == "graceful failure"


async def test_structured_output_gets_model_readable_text() -> None:
    sink = _RecordingSink()
    registry = _registry(sink)
    structured: JsonObject = {"key": "value"}
    registry.register(_FakeAdapter(_spec(), result=ToolResult(content=[], structured_output=structured)))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "succeeded"
    assert outcome.structured_output == structured
    assert cast(ToolTextContent, outcome.content[0]).text == canonical_json(structured)  # §2.1 L72


async def test_oversized_structured_output_dropped_with_marker() -> None:
    registry = _registry(_RecordingSink())
    big: JsonObject = {"payload": "x" * 100}
    registry.register(_FakeAdapter(_spec(), result=ToolResult(content=[], structured_output=big)))
    registry.snapshot("ses_contract")
    context = _context(limits=ToolLimits(max_structured_output_bytes=16))

    outcome = await registry.invoke(context, "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "succeeded"
    assert outcome.structured_output is None  # dropped at the §5.3 L276 cap
    text = cast(ToolTextContent, outcome.content[0]).text
    assert "[structured_output exceeded 16 bytes (" in text
    assert "bytes original) and was omitted]" in text


async def test_result_without_output_gets_explanatory_text() -> None:
    registry = _registry(_RecordingSink())
    registry.register(_FakeAdapter(_spec(), result=ToolResult(content=[])))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "succeeded"
    assert cast(ToolTextContent, outcome.content[0]).text == 'Tool "fs__read" completed with no output.'


async def test_file_only_content_gains_text_item() -> None:
    registry = _registry(_RecordingSink())
    file_only = ToolFileContent(type="file", uri="file:///tmp/x.png", mime="image/png", name="x.png")
    registry.register(_FakeAdapter(_spec(), result=ToolResult(content=[file_only])))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    # §2.1 L72 via normalization: file-only terminals gain a text item.
    assert [part.type for part in outcome.content] == ["file", "text"]
    assert cast(ToolTextContent, outcome.content[1]).text == 'Tool "fs__read" completed with no output.'


# ---------------------------------------------------------------------------
# progress callback bounding (§5.3 L276)
# ---------------------------------------------------------------------------


async def test_progress_metadata_is_capped() -> None:
    registry = _registry(_RecordingSink())
    small: JsonObject = {"ok": 1}
    big: JsonObject = {"payload": "x" * 100}
    registry.register(_FakeAdapter(_spec(), progress=[small, big]))
    registry.snapshot("ses_contract")
    seen: list[JsonObject] = []

    async def record(metadata: JsonObject) -> None:
        seen.append(metadata)

    context = _context(limits=ToolLimits(max_progress_metadata_bytes=16), progress=record)
    await registry.invoke(context, "fs__read", {"text": "hi"}, "exec_001")

    assert seen == [
        small,
        {"truncated": "progress metadata exceeded 16 bytes", "original_bytes": len(canonical_json(big))},
    ]


async def test_progress_without_runner_callback_is_noop() -> None:
    registry = _registry(_RecordingSink())
    registry.register(_FakeAdapter(_spec(), progress=[{"anything": "ignored"}]))
    registry.snapshot("ses_contract")

    outcome = await registry.invoke(_context(), "fs__read", {"text": "hi"}, "exec_001")

    assert outcome.state == "succeeded"
