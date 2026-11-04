"""R3-S3 contract tests: the static read trio's specs and registration face.

The real ``FsLsAdapter``/``FsGlobAdapter``/``FsGrepAdapter`` (plus
``FsReadAdapter`` as the R2 precedent) are registered through the same
public ``ToolRegistry.register`` face the host uses, pinned by one
snapshot, and resolved back through their reversible wire names -- no
filesystem and no ripgrep involved here (behavior is covered by
``tests/integration/test_tool_static_trio.py``). Contract anchors: plan
§2 R3-S3, CORE-CONTRACTS §5.1 L251-253/L255 (wire names), §5.2 L266
(root-internal allow), PHASE1-SLIM §4 L91 (acceptance A front half).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts.events import EventDraft
from opencode.session.service import IdFactory
from opencode.tools import RegistrationError, ToolRegistry
from opencode.tools.builtin import (
    GLOB_DEFAULT_LIMIT,
    GLOB_INPUT_SCHEMA,
    GLOB_MAX_LIMIT,
    GLOB_SPEC,
    GREP_DEFAULT_LIMIT,
    GREP_INPUT_SCHEMA,
    GREP_MAX_LIMIT,
    GREP_SPEC,
    LS_DEFAULT_LIMIT,
    LS_INPUT_SCHEMA,
    LS_MAX_LIMIT,
    LS_SPEC,
    READ_SPEC,
    FsGlobAdapter,
    FsGrepAdapter,
    FsLsAdapter,
    FsReadAdapter,
)

_FIXED_TIME = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)


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


def _registry() -> ToolRegistry:
    """A registry with the host's default static read quartet, one pinned snapshot."""
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast(IdFactory, _FixedIds()))
    for adapter in (FsReadAdapter(), FsLsAdapter(), FsGlobAdapter(), FsGrepAdapter()):
        registry.register(adapter)
    registry.snapshot("ses_listing")
    return registry


# ---------------------------------------------------------------------------
# spec identity: fs namespace, reversible wire names (§5.1 L255)
# ---------------------------------------------------------------------------


def test_trio_specs_use_fs_namespace_and_version_one() -> None:
    for spec in (LS_SPEC, GLOB_SPEC, GREP_SPEC):
        assert spec.namespace == "fs"
        assert spec.version == 1
        assert spec.name in ("ls", "glob", "grep")
        assert {LS_SPEC.name, GLOB_SPEC.name, GREP_SPEC.name} == {"ls", "glob", "grep"}


def test_trio_wire_names_partition_reversibly() -> None:
    """§5.1 L255: ``namespace__name`` splits back at the first ``__``."""
    for spec in (LS_SPEC, GLOB_SPEC, GREP_SPEC):
        wire_name = f"{spec.namespace}__{spec.name}"
        assert wire_name.partition("__") == (spec.namespace, "__", spec.name)


def test_trio_quartet_registers_through_public_face_and_snapshots() -> None:
    """The four builtins register through ``ToolRegistry.register`` (the face
    cli.py L276 assembles) and one snapshot resolves every wire name."""
    registry = _registry()
    catalog = registry.snapshot("ses_listing")

    # Sorted by (namespace, name): glob, grep, ls, read -- all in fs.
    assert [spec.name for spec in catalog.specs] == ["glob", "grep", "ls", "read"]
    assert all(spec.namespace == "fs" for spec in catalog.specs)
    for spec in (READ_SPEC, LS_SPEC, GLOB_SPEC, GREP_SPEC):
        assert catalog.resolve(f"fs__{spec.name}") is spec
    assert catalog.resolve("fs__missing") is None


def test_trio_registration_rejects_duplicate_wire_names() -> None:
    registry = _registry()
    for adapter in (FsLsAdapter(), FsGlobAdapter(), FsGrepAdapter()):
        with pytest.raises(RegistrationError, match="already registered"):
            registry.register(adapter)


# ---------------------------------------------------------------------------
# spec shape: read-only metadata + the strict validator's schema subset
# ---------------------------------------------------------------------------


def test_trio_specs_are_side_effect_free_read_tools() -> None:
    for spec in (LS_SPEC, GLOB_SPEC, GREP_SPEC):
        assert spec.side_effects is False
        assert spec.idempotency_support is True
        assert spec.supported_content_types == ["text"]
        assert spec.description  # non-empty model-facing description


def test_trio_input_schemas_stay_in_the_validator_subset() -> None:
    """Registration already proves the subset (register validates); the
    shapes are pinned so a schema edit that would break registration or
    the model contract is caught here first."""
    for schema in (LS_INPUT_SCHEMA, GLOB_INPUT_SCHEMA, GREP_INPUT_SCHEMA):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        allowed = {"type", "description", "properties", "required", "additionalProperties"}
        assert set(schema) <= allowed
        property_allowed = {"type", "description", "minimum", "maximum"}
        for property_schema in cast("dict[str, Any]", schema["properties"]).values():
            assert set(property_schema) <= property_allowed
            assert property_schema["type"] in ("string", "integer", "boolean")


def test_trio_input_schema_fields_match_the_ts_anchors() -> None:
    """glob.ts L16-27 / grep.ts L16-32 input faces (minus TS-only keywords)."""
    assert GLOB_INPUT_SCHEMA["required"] == ["pattern"]
    assert GREP_INPUT_SCHEMA["required"] == ["pattern"]
    assert LS_INPUT_SCHEMA["required"] == []

    glob_properties = cast("dict[str, dict[str, Any]]", GLOB_INPUT_SCHEMA["properties"])
    assert set(glob_properties) == {"pattern", "path", "hidden", "limit"}
    assert glob_properties["pattern"]["type"] == "string"
    assert glob_properties["hidden"]["type"] == "boolean"
    assert glob_properties["limit"]["type"] == "integer"
    assert glob_properties["limit"]["minimum"] == 1

    grep_properties = cast("dict[str, dict[str, Any]]", GREP_INPUT_SCHEMA["properties"])
    assert set(grep_properties) == {"pattern", "path", "include", "literal", "caseSensitive", "limit"}
    assert grep_properties["include"]["type"] == "string"
    assert grep_properties["literal"]["type"] == "boolean"
    assert grep_properties["caseSensitive"]["type"] == "boolean"
    assert grep_properties["limit"]["minimum"] == 1

    ls_properties = cast("dict[str, dict[str, Any]]", LS_INPUT_SCHEMA["properties"])
    assert set(ls_properties) == {"path", "hidden", "limit"}
    assert ls_properties["path"]["type"] == "string"


def test_trio_limits_follow_the_ts_default() -> None:
    """filesystem.ts L66 DEFAULT_SEARCH_LIMIT = 100 anchors all three defaults;
    the 1000 hard cap is the Python-side safety face (progress-file float report)."""
    assert (LS_DEFAULT_LIMIT, GLOB_DEFAULT_LIMIT, GREP_DEFAULT_LIMIT) == (100, 100, 100)
    assert (LS_MAX_LIMIT, GLOB_MAX_LIMIT, GREP_MAX_LIMIT) == (1_000, 1_000, 1_000)
