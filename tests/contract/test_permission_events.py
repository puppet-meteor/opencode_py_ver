"""R3-S1 contract tests: the py.permission.* wire family (CORE-CONTRACTS §3.1 L135-139).

Positive fixtures must decode with every field present and survive a
model -> JSON -> model round trip unchanged; negative fixtures prove the
fail-closed rule (missing required fields, extra keys, vocabulary and shape
violations are rejected loudly). The registration tests pin the additive
boundary: the six types live in their own R3 tuple, R1/R2 stay untouched,
and the closed-wire discipline (versioned_type + canonical_json, §8 L358)
means an unknown version is an explicit failure, never a silent
same-version field skip. No py.permission.* type is run-scoped at decode:
the approval trio's run linkage is enforced by the fold (envelope run_id
must match the pinned execution's run), not by the wire (the frozen
RUN_SCOPED_EVENT_TYPES membership pins exactly the R2 set).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts import (
    DurabilityMismatchError,
    EventDefinition,
    UnknownEventVersionError,
    default_event_registry,
)
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_PERMISSION_CLOSED,
    PY_PERMISSION_EXPIRED,
    PY_PERMISSION_REPLIED,
    PY_PERMISSION_REQUESTED,
    PY_PERMISSION_REVOKED,
    PY_PERMISSION_SAVED,
    R1_EVENT_DEFINITIONS,
    R2_EVENT_DEFINITIONS,
    R3_PERMISSION_EVENT_DEFINITIONS,
    RUN_SCOPED_EVENT_TYPES,
    PermissionRequestedData,
    PermissionSavedData,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PERMISSION_TYPES = (
    PY_PERMISSION_REQUESTED,
    PY_PERMISSION_REPLIED,
    PY_PERMISSION_CLOSED,
    PY_PERMISSION_SAVED,
    PY_PERMISSION_REVOKED,
    PY_PERMISSION_EXPIRED,
)

POSITIVE_FIXTURES = {
    PY_PERMISSION_REQUESTED: "events_permission_requested_valid.json",
    PY_PERMISSION_REPLIED: "events_permission_replied_valid.json",
    PY_PERMISSION_CLOSED: "events_permission_closed_valid.json",
    PY_PERMISSION_SAVED: "events_permission_saved_valid.json",
    PY_PERMISSION_REVOKED: "events_permission_revoked_valid.json",
    PY_PERMISSION_EXPIRED: "events_permission_expired_valid.json",
}

MISSING_FIELD_FIXTURES = tuple(
    f"events_permission_{kind}_missing_field.json"
    for kind in ("requested", "replied", "closed", "saved", "revoked", "expired")
)

BAD_EXTENSION_FIXTURES = tuple(
    f"events_permission_{kind}_bad_extension.json"
    for kind in ("requested", "replied", "closed", "saved", "revoked", "expired")
)


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _requested_raw() -> dict[str, Any]:
    return _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_REQUESTED])


class TestR3Registration:
    """The R3 tuple is additive: six durable rows, R1/R2 untouched (§3.1 L140)."""

    def test_r3_definitions_shape(self) -> None:
        assert len(R3_PERMISSION_EVENT_DEFINITIONS) == 6
        assert all(definition.durability == "durable" for definition in R3_PERMISSION_EVENT_DEFINITIONS)
        assert all(definition.version == EVENT_VERSION for definition in R3_PERMISSION_EVENT_DEFINITIONS)
        assert {definition.event_type for definition in R3_PERMISSION_EVENT_DEFINITIONS} == set(PERMISSION_TYPES)

    def test_r1_and_r2_tuples_unchanged(self) -> None:
        """The frozen R2 boundary pins hold: 15 rows, no py.permission.* leak."""
        assert len(R2_EVENT_DEFINITIONS) == 15
        assert len(R1_EVENT_DEFINITIONS) == 2
        r1_r2_types = {definition.event_type for definition in (*R1_EVENT_DEFINITIONS, *R2_EVENT_DEFINITIONS)}
        assert not any(event_type.startswith("py.permission.") for event_type in r1_r2_types)

    def test_default_registry_resolves_all_permission_types(self) -> None:
        registry = default_event_registry()
        for definition in R3_PERMISSION_EVENT_DEFINITIONS:
            assert registry.resolve_durable(definition.event_type, EVENT_VERSION) is definition

    def test_permission_types_are_not_run_scoped_at_decode(self) -> None:
        """The frozen RUN_SCOPED set pins the R2 membership; permission run
        linkage is fold-owned (envelope run_id vs the pinned execution's run),
        so a replied envelope without metadata.run_id decodes fine."""
        assert not (set(PERMISSION_TYPES) & RUN_SCOPED_EVENT_TYPES)
        raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_REPLIED])
        del raw["metadata"]["run_id"]
        event = default_event_registry().decode_durable(raw)
        assert event.type == PY_PERMISSION_REPLIED

    def test_unknown_version_rejected_closed_wire(self) -> None:
        """§8 L358: no same-version optional-field drift; a future v2 must be
        an explicit upcaster, and until then version 2 fails loudly."""
        with pytest.raises(UnknownEventVersionError):
            default_event_registry().resolve_durable(PY_PERMISSION_REQUESTED, 2)

    def test_durable_mismatch_rejected(self) -> None:
        with pytest.raises(DurabilityMismatchError):
            default_event_registry().resolve_ephemeral(PY_PERMISSION_REQUESTED)

    def test_duplicate_registration_rejected(self) -> None:
        registry = default_event_registry()
        duplicate = EventDefinition(
            event_type=PY_PERMISSION_SAVED, durability="durable", data_model=PermissionSavedData
        )
        with pytest.raises(ValueError, match=r"[Dd]uplicate"):
            registry.register(duplicate)


class TestPermissionPositiveFixtures:
    """Positive fixtures: decode with all fields present and round-trip equal."""

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(POSITIVE_FIXTURES.items()))
    def test_round_trip_is_lossless(self, event_type: str, fixture_name: str) -> None:
        registry = default_event_registry()
        event = registry.decode_durable(_load_fixture(fixture_name))
        assert event.type == event_type
        assert event.version == 1
        assert event.seq >= 1
        assert registry.decode_durable(event.model_dump_json()) == event

    def test_fixtures_expose_py_naming_and_version_one(self) -> None:
        for fixture_name in POSITIVE_FIXTURES.values():
            raw = _load_fixture(fixture_name)
            assert raw["type"].startswith("py.permission.")
            assert raw["version"] == 1

    def test_approval_trio_carries_run_id_metadata(self) -> None:
        """Producer shape for the run-scoped trio: the envelope pins the run."""
        for event_type in (PY_PERMISSION_REQUESTED, PY_PERMISSION_REPLIED, PY_PERMISSION_CLOSED):
            raw = _load_fixture(POSITIVE_FIXTURES[event_type])
            assert raw["metadata"] == {"run_id": "run_0000000000000000000000000002"}

    def test_decision_trio_has_no_run_linkage(self) -> None:
        """saved/revoked/expired are session-scoped: no run_id in the envelope."""
        for event_type in (PY_PERMISSION_SAVED, PY_PERMISSION_REVOKED, PY_PERMISSION_EXPIRED):
            raw = _load_fixture(POSITIVE_FIXTURES[event_type])
            assert raw["metadata"] == {}

    def test_requested_fixture_fields(self) -> None:
        event = default_event_registry().decode_durable(_requested_raw())
        assert isinstance(event.data, PermissionRequestedData)
        assert event.data.permission_id.startswith("per_")
        assert event.data.execution_id.startswith("exec_")
        assert event.data.action == "write"
        assert list(event.data.resources) == ["file://D:/work/demo/notes.md"]
        assert len(event.data.input_hash) == 64
        assert event.data.expires_at == datetime(2026, 11, 3, 8, 5, 20, tzinfo=UTC)

    def test_saved_fixture_fields(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_SAVED]))
        assert isinstance(event.data, PermissionSavedData)
        assert event.data.decision_id.startswith("psv_")
        assert event.data.scope == {"action": "write", "resources": ["file://D:/work/demo/notes.md"]}
        assert event.data.rule == {"kind": "file_write", "path": "D:/work/demo/notes.md"}
        assert event.data.boot_id.startswith("boot_")
        assert event.data.expires_at == datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC)


class TestPermissionNegativeFixtures:
    """Negative fixtures: every rejection is loud (CORE-CONTRACTS §1)."""

    @pytest.mark.parametrize("fixture_name", MISSING_FIELD_FIXTURES)
    def test_missing_required_field_rejected(self, fixture_name: str) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(_load_fixture(fixture_name))

    @pytest.mark.parametrize("fixture_name", BAD_EXTENSION_FIXTURES)
    def test_extra_data_key_rejected(self, fixture_name: str) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(_load_fixture(fixture_name))


class TestPermissionDataShapes:
    """Wire vocabulary and shape guards (§3.1 L135-139 rows, fail closed)."""

    def test_requested_input_hash_must_be_sha256_hex(self) -> None:
        raw = _requested_raw()
        raw["data"]["input_hash"] = "not-a-hash"
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)

    def test_requested_empty_identifiers_rejected(self) -> None:
        for field in ("permission_id", "execution_id", "action"):
            raw = _requested_raw()
            raw["data"][field] = ""
            with pytest.raises(ValidationError):
                default_event_registry().decode_durable(raw)

    def test_requested_naive_expires_at_rejected(self) -> None:
        raw = _requested_raw()
        raw["data"]["expires_at"] = "2026-11-03T08:05:20"
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)

    def test_replied_vocabulary_closed(self) -> None:
        for bad in ("always-forever", "deny", "ONCE", ""):
            raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_REPLIED])
            raw["data"]["reply"] = bad
            with pytest.raises(ValidationError):
                default_event_registry().decode_durable(raw)

    def test_replied_source_pinned_to_host(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_REPLIED])
        raw["data"]["source"] = "policy"
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)

    def test_closed_vocabulary_closed(self) -> None:
        for bad in ("timeout", "denied", ""):
            raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_CLOSED])
            raw["data"]["outcome"] = bad
            with pytest.raises(ValidationError):
                default_event_registry().decode_durable(raw)

    def test_closed_empty_reason_rejected(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_CLOSED])
        raw["data"]["reason"] = ""
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)

    def test_saved_scope_and_rule_must_be_json_objects(self) -> None:
        """scope/rule are producer-owned shapes pinned as JSON objects (the
        ToolRequestedData.input precedent); scalars and arrays are refused."""
        for field in ("scope", "rule"):
            for bad in ("write", ["file://D:/work/demo/notes.md"], 7):
                raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_SAVED])
                raw["data"][field] = bad
                with pytest.raises(ValidationError):
                    default_event_registry().decode_durable(raw)

    def test_saved_expires_at_is_nullable(self) -> None:
        """None = no explicit expiry; activation stays bounded by boot_id (§5.2)."""
        raw = _load_fixture(POSITIVE_FIXTURES[PY_PERMISSION_SAVED])
        raw["data"]["expires_at"] = None
        event = default_event_registry().decode_durable(raw)
        assert isinstance(event.data, PermissionSavedData)
        assert event.data.expires_at is None

    def test_terminal_pair_shapes(self) -> None:
        """revoked/expired share decision_id + reason; both are non-empty."""
        for event_type in (PY_PERMISSION_REVOKED, PY_PERMISSION_EXPIRED):
            raw = _load_fixture(POSITIVE_FIXTURES[event_type])
            assert set(raw["data"]) == {"decision_id", "reason"}
            raw["data"]["reason"] = ""
            with pytest.raises(ValidationError):
                default_event_registry().decode_durable(raw)

    def test_data_models_reject_wrong_type_payloads(self) -> None:
        """A registered permission type carrying another type's data fails
        the model validation (closed per-type data shapes)."""
        raw = _requested_raw()
        raw["data"] = {"decision_id": "psv_0000000000000000000000000001"}
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)
