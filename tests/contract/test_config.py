"""R3-S6 contract tests: ConfigSnapshot v2 + ``py.session.created`` v2 + the v1->v2 upcaster.

Covers the T-C1-030 contract faces: the §6.1 L287 v2 field face (extra/missing
field rejection, integer-bool rejection, non-empty permission rules — the v1
contrast), §6.2 hash re-verification on decode, the §8 L356/L358 version
evolution bar (v1 fixture still decodes, both versions registered, version 3
still unknown), the frozen v1 fingerprint (§9 L368), and the deterministic
upcaster semantics (§6.1 L291: a v1 row never gains a runtime face).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.events import (
    PY_SESSION_CREATED,
    ConfigSnapshot,
    ConfigSnapshotV2,
    SessionCreatedData,
    SessionCreatedDataV2,
    SnapshotLimits,
    UnknownEventVersionError,
    compute_snapshot_hash,
    default_event_registry,
    upcast_session_created,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: The frozen R1 canonical-text fingerprint (§6.2/§9 L368) — hardcoded here so
#: any accidental change to the v1 value domain fails loudly.
R1_SNAPSHOT_DIGEST = "77c932c31585517c128479af02c40cd377e26d4d25ce57df197fc8509e14f802"

V2_FIXTURES = {
    "valid": "events_session_created_v2_valid.json",
    "bad_hash": "events_session_created_v2_bad_hash.json",
    "bad_extension": "events_session_created_v2_bad_extension.json",
}


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _v2_snapshot_dict() -> dict[str, Any]:
    return _load_fixture(V2_FIXTURES["valid"])["data"]["config_snapshot"]


class TestConfigSnapshotV2Model:
    """The §6.1 L287 field face: explicit, fail-closed, no fabricated defaults."""

    def test_legal_snapshot_round_trips(self) -> None:
        snapshot = ConfigSnapshotV2.model_validate(_v2_snapshot_dict())
        assert snapshot.schema_version == 2
        assert snapshot.permission_rules == []
        assert snapshot.instructions[0].path == "D:/work/demo/AGENTS.md"
        assert ConfigSnapshotV2.model_validate(json.loads(snapshot.model_dump_json())) == snapshot

    def test_extra_field_is_rejected(self) -> None:
        raw = _v2_snapshot_dict()
        raw["mcp_servers"] = []
        with pytest.raises(ValidationError):
            ConfigSnapshotV2.model_validate(raw)

    def test_missing_limits_is_rejected(self) -> None:
        raw = _v2_snapshot_dict()
        del raw["limits"]
        with pytest.raises(ValidationError):
            ConfigSnapshotV2.model_validate(raw)

    def test_bool_is_rejected_for_integer_limits(self) -> None:
        raw = _v2_snapshot_dict()
        raw["limits"]["context_limit"] = True
        with pytest.raises(ValidationError, match="must be an integer, not a boolean"):
            ConfigSnapshotV2.model_validate(raw)

    def test_empty_roots_entry_is_rejected(self) -> None:
        raw = _v2_snapshot_dict()
        raw["roots"] = [""]
        with pytest.raises(ValidationError, match="non-empty path strings"):
            ConfigSnapshotV2.model_validate(raw)

    def test_non_empty_permission_rules_are_legal_in_v2(self) -> None:
        """The v1 contrast: v1 pins rules empty (§6.1 L289); v2 is the executable face (L287)."""
        raw = _v2_snapshot_dict()
        raw["permission_rules"] = [{"effect": "ask", "action": "fs__write"}]
        assert ConfigSnapshotV2.model_validate(raw).permission_rules == [{"effect": "ask", "action": "fs__write"}]
        with pytest.raises(ValidationError):
            ConfigSnapshot.model_validate({**raw, "schema_version": 1})

    def test_v1_fingerprint_is_frozen(self) -> None:
        assert compute_snapshot_hash(ConfigSnapshot.create()) == R1_SNAPSHOT_DIGEST


class TestSessionCreatedV2Wire:
    """Registry + decode faces for the first py.* v2 event family (§8 L356/L358)."""

    def test_valid_fixture_decodes_and_round_trips(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(V2_FIXTURES["valid"]))
        assert (event.type, event.version) == (PY_SESSION_CREATED, 2)
        assert isinstance(event.data, SessionCreatedDataV2)
        # §6.2 L297: decode re-verifies the digest; the subclass also satisfies
        # every frozen consumer typing the field as the v1 model.
        assert isinstance(event.data, SessionCreatedData)
        assert event.data.snapshot_hash == compute_snapshot_hash(event.data.config_snapshot)
        assert default_event_registry().decode_durable(event.model_dump_json()) == event

    def test_bad_hash_fixture_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="snapshot_hash does not match"):
            default_event_registry().decode_durable(_load_fixture(V2_FIXTURES["bad_hash"]))

    def test_bad_extension_fixture_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(_load_fixture(V2_FIXTURES["bad_extension"]))

    def test_v1_fixture_still_decodes_version_one(self) -> None:
        """§8 L356 minimum bar: the old fixture reads unchanged, v1 semantics intact (§9 L368)."""
        event = default_event_registry().decode_durable(_load_fixture("events_session_created_valid.json"))
        assert (event.type, event.version) == (PY_SESSION_CREATED, 1)
        assert type(event.data) is SessionCreatedData
        assert event.data.snapshot_hash == R1_SNAPSHOT_DIGEST

    def test_version_three_is_still_unknown(self) -> None:
        raw = _load_fixture(V2_FIXTURES["valid"])
        raw["version"] = 3
        with pytest.raises(UnknownEventVersionError):
            default_event_registry().decode_durable(raw)

    def test_both_versions_are_registered_side_by_side(self) -> None:
        registry = default_event_registry()
        assert registry.resolve_durable(PY_SESSION_CREATED, 1).data_model is SessionCreatedData
        assert registry.resolve_durable(PY_SESSION_CREATED, 2).data_model is SessionCreatedDataV2


class TestUpcaster:
    """Deterministic v1->v2 conversion (§6.1 L291/L302: no silent runtime backfill)."""

    def test_v2_row_passes_through_with_its_snapshot(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(V2_FIXTURES["valid"]))
        runtime = upcast_session_created(event.data)
        assert runtime.working_dir == "D:/work/demo"
        assert runtime.snapshot == event.data.config_snapshot

    def test_v1_row_upcasts_to_no_runtime_face(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture("events_session_created_valid.json"))
        runtime = upcast_session_created(event.data)
        assert runtime.working_dir == "D:/work/demo"
        assert runtime.snapshot is None


def test_snapshot_limits_wire_shape_pins_the_runtime_knobs() -> None:
    """The limits block is the wire shape of the §7 L339 runtime knobs (§6.1 L287)."""
    limits = SnapshotLimits(
        context_limit=100000,
        output_reserve=4096,
        timeout_s=30.0,
        safety_margin=0,
        temperature=None,
        max_steps=20,
    )
    assert limits.model_dump() == {
        "context_limit": 100000,
        "output_reserve": 4096,
        "timeout_s": 30.0,
        "safety_margin": 0,
        "temperature": None,
        "max_steps": 20,
    }
