"""R5-S4 contract tests: schema-artifact drift guard + fixture lock-diff reconciliation.

CORE-CONTRACTS §1 L17: the unified JSON Schema is generated from the real models'
``model_json_schema()`` and the differences get locked. T-C1-045 ruled the locking
form = committed artifacts + drift tests:

* drift guard -- regenerating must reproduce ``doc/reference/schema/`` byte for byte;
* lock-diff guard -- every positive fixture validates against its envelope's
  committed JSON Schema and decodes through the registry; every negative fixture is
  classified as schema-level (the JSON Schema itself rejects the shape) or
  behavior-level (the shape is schema-legal; a model or registry rule rejects it),
  and that classification is pinned per file;
* capability guard -- the closed ``CoreErrorCode`` registry keeps
  ``UnsupportedCapability`` reachable on the schema face (errors.json).

The JSON-Schema checker below covers the keyword subset pydantic v2 emits for these
models (draft 2020-12: type/enum/const/properties/required/additionalProperties/
items/prefixItems/minItems/maxItems/pattern/minLength/maxLength/minimum/maximum/
exclusiveMinimum/exclusiveMaximum/anyOf/allOf/oneOf/$ref+$defs). ``format`` is
annotation-only per the draft and deliberately not asserted -- datetime strictness
is model behavior, which is exactly what the schema/behavior split records. The
repo carries no jsonschema dependency and T-C1-045 adds none.
"""

from __future__ import annotations

import functools
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tools.r5.generate_schema_artifacts import SCHEMA_DIR, build_artifacts

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import (
    DurableEvent,
    EphemeralEvent,
    EventRegistry,
    UnknownEventTypeError,
    UnknownEventVersionError,
    default_event_registry,
    versioned_type,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ENVELOPE_ARTIFACT = SCHEMA_DIR / "event-envelopes.json"

#: Pinned census (doc/reference/r5-fixture-inventory.md closes the same counts):
#: 25 positive, 48 schema-level negatives, 19 behavior-level negatives
#: (15 missing run_id + 2 bad snapshot hash + 2 registry probes).
PINNED_TOTAL = 92
PINNED_COUNTS = {"positive": 25, "schema-level": 48, "behavior-level": 19}

#: Pinned model counts per module artifact (seat full recount at R5-S4; the
#: scheduler's grep pre-check of 53 counts class lines mentioning BaseModel and
#: misses 10 subclass-only declarations -- both counts are reconciled in the
#: inventory, the artifact covers all 63 actual pydantic models).
PINNED_MODEL_COUNTS = {"errors.json": 1, "events.json": 34, "llm.json": 17, "messages.json": 8, "tools.json": 3}
PINNED_DURABLE_KEYS = 21
PINNED_EPHEMERAL_KEYS = 4


def _fixture_files() -> list[str]:
    return sorted(path.name for path in FIXTURES_DIR.glob("*.json"))


FIXTURE_FILES = _fixture_files()


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _classify(name: str) -> str:
    """The pinned negative-classification map (T-C1-045 task 2b): suffix families."""
    if name.endswith("_valid.json"):
        return "positive"
    if name.endswith("_bad_extension.json") or name.endswith("_missing_field.json"):
        return "schema-level"
    if name.endswith("_missing_run_id.json") or name.endswith("_bad_hash.json"):
        return "behavior-level"
    if name in ("events_unknown_type.json", "events_unknown_version.json"):
        return "behavior-level"
    raise AssertionError(f"fixture not covered by the T-C1-045 classification: {name}")


@functools.cache
def _envelope_document() -> dict[str, Any]:
    return json.loads(ENVELOPE_ARTIFACT.read_text(encoding="utf-8"))


@functools.cache
def _registry() -> EventRegistry:
    return default_event_registry()


def _envelope_schema_for(raw: dict[str, Any]) -> dict[str, Any]:
    """The committed envelope schema a fixture locks: the registered (type, version)
    parametrization, or the generic durable envelope for the registry probes."""
    document = _envelope_document()
    if raw["type"] in document["ephemeral"]:
        return document["ephemeral"][raw["type"]]
    key = versioned_type(raw["type"], raw["version"])
    if key in document["durable"]:
        return document["durable"][key]
    return document["generic"]["DurableEvent"]


def _decode(raw: dict[str, Any]) -> DurableEvent | EphemeralEvent:
    """Decode through the durability path the fixture's type is registered under."""
    if raw["type"] in _envelope_document()["ephemeral"]:
        return _registry().decode_ephemeral(raw)
    return _registry().decode_durable(raw)


def _expected_rejection(name: str) -> type[Exception]:
    if name == "events_unknown_type.json":
        return UnknownEventTypeError
    if name == "events_unknown_version.json":
        return UnknownEventVersionError
    return ValidationError


class _SchemaRejected(Exception):
    """The JSON Schema rejected the value at the given JSON pointer."""


def _json_type_matches(expected: str, value: Any) -> bool:
    """JSON type gate; Python bools are never JSON integers/numbers (CORE-CONTRACTS §1)."""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _json_equal(left: Any, right: Any) -> bool:
    """Equality without the Python bool/int overlap (True == 1 must not match)."""
    return left == right and isinstance(left, bool) == isinstance(right, bool)


def _resolve_pointer(root: dict[str, Any], ref: str) -> dict[str, Any]:
    assert ref.startswith("#/"), f"only document-local refs are emitted: {ref}"
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(node, dict)
    return node


def _valid(schema: dict[str, Any], value: Any, root: dict[str, Any]) -> bool:
    try:
        _validate(schema, value, root, "$")
    except _SchemaRejected:
        return False
    return True


def _validate(schema: dict[str, Any], value: Any, root: dict[str, Any], pointer: str) -> None:
    if not schema:
        return  # {} -- pydantic's JsonValue face: any JSON value is legal
    if "$ref" in schema:
        _validate(_resolve_pointer(root, schema["$ref"]), value, root, pointer)
    expected_types = schema.get("type")
    if isinstance(expected_types, str) and not _json_type_matches(expected_types, value):
        raise _SchemaRejected(f"{pointer}: expected {expected_types}, got {type(value).__name__}")
    if isinstance(expected_types, list) and not any(_json_type_matches(t, value) for t in expected_types):
        raise _SchemaRejected(f"{pointer}: expected one of {expected_types}")
    if "enum" in schema and not any(_json_equal(option, value) for option in schema["enum"]):
        raise _SchemaRejected(f"{pointer}: {value!r} not in enum")
    if "const" in schema and not _json_equal(schema["const"], value):
        raise _SchemaRejected(f"{pointer}: {value!r} != const {schema['const']!r}")
    if "anyOf" in schema and not any(_valid(member, value, root) for member in schema["anyOf"]):
        raise _SchemaRejected(f"{pointer}: no anyOf member matched")
    if "allOf" in schema:
        for member in schema["allOf"]:
            _validate(member, value, root, pointer)
    if "oneOf" in schema and sum(_valid(member, value, root) for member in schema["oneOf"]) != 1:
        raise _SchemaRejected(f"{pointer}: oneOf match count != 1")
    if isinstance(value, dict):
        _validate_object(schema, value, root, pointer)
    if isinstance(value, list):
        _validate_array(schema, value, root, pointer)
    if isinstance(value, str):
        _validate_string(schema, value, pointer)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(schema, value, pointer)


def _validate_object(schema: dict[str, Any], value: dict[str, Any], root: dict[str, Any], pointer: str) -> None:
    for key in schema.get("required", []):
        if key not in value:
            raise _SchemaRejected(f"{pointer}: missing required property {key!r}")
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in properties:
            _validate(properties[key], item, root, f"{pointer}/{key}")
        elif isinstance(additional, dict):
            _validate(additional, item, root, f"{pointer}/{key}")
        elif additional is False:
            raise _SchemaRejected(f"{pointer}: additional property {key!r} is not allowed")


def _validate_array(schema: dict[str, Any], value: list[Any], root: dict[str, Any], pointer: str) -> None:
    if "items" in schema:
        for index, item in enumerate(value):
            _validate(schema["items"], item, root, f"{pointer}/{index}")
    prefix = schema.get("prefixItems")
    if isinstance(prefix, list):
        for index, item in enumerate(value[: len(prefix)]):
            _validate(prefix[index], item, root, f"{pointer}/{index}")
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise _SchemaRejected(f"{pointer}: fewer than {schema['minItems']} items")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise _SchemaRejected(f"{pointer}: more than {schema['maxItems']} items")


def _validate_string(schema: dict[str, Any], value: str, pointer: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise _SchemaRejected(f"{pointer}: shorter than {schema['minLength']}")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise _SchemaRejected(f"{pointer}: longer than {schema['maxLength']}")
    if "pattern" in schema and re.search(schema["pattern"], value) is None:
        raise _SchemaRejected(f"{pointer}: {value!r} does not match {schema['pattern']!r}")


def _validate_number(schema: dict[str, Any], value: int | float, pointer: str) -> None:
    if "minimum" in schema and value < schema["minimum"]:
        raise _SchemaRejected(f"{pointer}: below minimum {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise _SchemaRejected(f"{pointer}: above maximum {schema['maximum']}")
    exclusive_min = schema.get("exclusiveMinimum")
    if isinstance(exclusive_min, (int, float)) and not isinstance(exclusive_min, bool) and value <= exclusive_min:
        raise _SchemaRejected(f"{pointer}: at or below exclusiveMinimum {exclusive_min}")
    exclusive_max = schema.get("exclusiveMaximum")
    if isinstance(exclusive_max, (int, float)) and not isinstance(exclusive_max, bool) and value >= exclusive_max:
        raise _SchemaRejected(f"{pointer}: at or above exclusiveMaximum {exclusive_max}")


def _walk(node: Any) -> Iterator[Any]:
    """Yield every node of a JSON tree, depth-first (dicts and lists included)."""
    yield node
    if isinstance(node, dict):
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


class TestArtifactDrift:
    """CORE-CONTRACTS §1 L17 '锁定差异' -- the committed artifacts are the lock."""

    def test_committed_artifacts_regenerate_byte_identical(self) -> None:
        for filename, text in build_artifacts().items():
            committed = (SCHEMA_DIR / filename).read_bytes()
            assert committed == text.encode("utf-8"), filename

    def test_generation_is_deterministic_across_calls(self) -> None:
        assert build_artifacts() == build_artifacts()

    def test_artifact_directory_is_exactly_the_generated_set(self) -> None:
        on_disk = {path.name for path in SCHEMA_DIR.iterdir()}
        assert on_disk == set(build_artifacts())


class TestSchemaEnumeration:
    """The artifacts enumerate the full model face (63 models; see the inventory)."""

    def test_module_artifacts_enumerate_the_pinned_model_counts(self) -> None:
        for filename, expected in PINNED_MODEL_COUNTS.items():
            document = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
            assert len(document["models"]) == expected, filename

    def test_envelope_artifact_covers_every_registered_definition(self) -> None:
        document = _envelope_document()
        assert len(document["durable"]) == PINNED_DURABLE_KEYS
        assert len(document["ephemeral"]) == PINNED_EPHEMERAL_KEYS
        registry = _registry()
        for key in document["durable"]:
            event_type, _, version = key.rpartition(".")
            registry.resolve_durable(event_type, int(version))
        for event_type in document["ephemeral"]:
            registry.resolve_ephemeral(event_type)


class TestFixtureCensus:
    """The 92-file closure (Done L16) and the pinned classification mix."""

    def test_fixture_directory_is_exactly_the_pinned_files(self) -> None:
        assert len(FIXTURE_FILES) == PINNED_TOTAL
        assert {path.name for path in FIXTURES_DIR.iterdir()} == set(FIXTURE_FILES)

    def test_classification_counts_close_at_92(self) -> None:
        assert Counter(_classify(name) for name in FIXTURE_FILES) == PINNED_COUNTS


class TestPositiveLockDiff:
    """PHASE1-SLIM §3 acceptance C, first half: the old JSON fixtures stay readable --
    every positive fixture passes its envelope's committed JSON Schema and decodes."""

    @pytest.mark.parametrize("fixture_name", [n for n in FIXTURE_FILES if _classify(n) == "positive"])
    def test_positive_fixture_passes_schema_and_decodes(self, fixture_name: str) -> None:
        raw = _load_fixture(fixture_name)
        schema = _envelope_schema_for(raw)
        _validate(schema, raw, schema, "$")
        event = _decode(raw)
        assert event.type == raw["type"]


class TestSchemaLevelLockDiff:
    """Schema-level negatives: the committed JSON Schema itself rejects the shape
    (missing required fields, unknown extra fields via additionalProperties: false),
    and the model decode path rejects the same files."""

    @pytest.mark.parametrize("fixture_name", [n for n in FIXTURE_FILES if _classify(n) == "schema-level"])
    def test_schema_level_negative_fails_schema_and_decode(self, fixture_name: str) -> None:
        raw = _load_fixture(fixture_name)
        schema = _envelope_schema_for(raw)
        with pytest.raises(_SchemaRejected):
            _validate(schema, raw, schema, "$")
        with pytest.raises(ValidationError):
            _decode(raw)


class TestBehaviorLevelLockDiff:
    """Behavior-level negatives: the shape is schema-legal; the rejection is model
    or registry semantics (metadata.run_id rule, snapshot digest rule, registry
    membership) -- the schema-uncovered corners itemized in the inventory."""

    @pytest.mark.parametrize("fixture_name", [n for n in FIXTURE_FILES if _classify(n) == "behavior-level"])
    def test_behavior_level_negative_is_schema_legal_but_semantically_rejected(self, fixture_name: str) -> None:
        raw = _load_fixture(fixture_name)
        schema = _envelope_schema_for(raw)
        _validate(schema, raw, schema, "$")
        with pytest.raises(_expected_rejection(fixture_name)):
            _decode(raw)


class TestRegistryRejections:
    """The two registry probes stay registry-level rejections (CORE-CONTRACTS §3.1:
    unknown type/version are never silently skipped), distinct from field failures."""

    def test_unknown_type_fixture_rejected_as_unknown_type(self) -> None:
        with pytest.raises(UnknownEventTypeError):
            _registry().decode_durable(_load_fixture("events_unknown_type.json"))

    def test_unknown_version_fixture_rejected_as_unknown_version(self) -> None:
        with pytest.raises(UnknownEventVersionError):
            _registry().decode_durable(_load_fixture("events_unknown_version.json"))


class TestUnsupportedCapabilityFace:
    """PHASE1-SLIM §3 acceptance C '未知能力显式拒绝' -- schema-face reachability of
    the closed code registry. The behavioral face is pinned by five tests/integration
    anchors (re-anchored at R5-S4; mapped in doc/reference/r5-fixture-inventory.md):
    test_read_directory_is_unsupported, test_ls_file_path_is_unsupported,
    test_glob_search_path_not_a_directory, test_write_directory_target_is_unsupported,
    test_edit_directory_target_is_unsupported."""

    def test_code_value_pinned_at_definition_face(self) -> None:
        assert CoreErrorCode.UNSUPPORTED_CAPABILITY.value == "UnsupportedCapability"

    def test_code_reachable_in_committed_schema_artifact(self) -> None:
        document = json.loads((SCHEMA_DIR / "errors.json").read_text(encoding="utf-8"))
        core_error = document["models"]["CoreError"]
        assert any(
            isinstance(node, dict) and "UnsupportedCapability" in node.get("enum", []) for node in _walk(core_error)
        )
