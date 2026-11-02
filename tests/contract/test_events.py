"""R1-S1 contract tests: fixture-driven decode/round-trip for the minimal event contracts.

Positive fixtures must decode with every field present and survive a
model -> JSON -> model round trip unchanged. Negative fixtures prove the
fail-closed rule: unknown type, unknown version, missing required fields,
and invalid extension values are rejected loudly, never silently skipped
(doc/plans/r1-slices.md §2 R1-S1; CORE-CONTRACTS §1/§3.1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    CoreError,
    CoreErrorCode,
    DurabilityMismatchError,
    DurableEvent,
    EphemeralEvent,
    EventDecodeError,
    EventDefinition,
    EventDraft,
    EventRegistry,
    PromptAcceptedData,
    SessionCreatedData,
    UnknownEventTypeError,
    UnknownEventVersionError,
    UserMessage,
    canonical_json,
    compute_snapshot_hash,
    default_event_registry,
    versioned_type,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: sha256 of the fixed R1 canonical snapshot text '{"permission_rules":[],"schema_version":1}' (§6.2.4).
R1_SNAPSHOT_DIGEST = "77c932c31585517c128479af02c40cd377e26d4d25ce57df197fc8509e14f802"

POSITIVE_FIXTURES = {
    PY_SESSION_CREATED: "events_session_created_valid.json",
    PY_PROMPT_ACCEPTED: "events_prompt_accepted_valid.json",
}


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _session_created_draft() -> EventDraft[SessionCreatedData]:
    snapshot = ConfigSnapshot.create()
    return EventDraft[SessionCreatedData](
        id="evt_0000000000000000000000000009",
        type=PY_SESSION_CREATED,
        version=1,
        session_id="ses_0000000000000000000000000002",
        created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
        data=SessionCreatedData(
            working_dir="D:/work/demo",
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ),
    )


class _HeartbeatData(BaseModel):
    """Test-local ephemeral data model; production ephemeral types arrive with R2."""

    model_config = ConfigDict(extra="forbid")

    beat: int = Field(ge=0)


def _ephemeral_registry() -> EventRegistry:
    return EventRegistry(
        [EventDefinition(event_type="py.test.heartbeat", durability="ephemeral", data_model=_HeartbeatData)]
    )


def _heartbeat_raw() -> dict[str, Any]:
    return {
        "id": "evt_0000000000000000000000000010",
        "type": "py.test.heartbeat",
        "session_id": "ses_0000000000000000000000000002",
        "created_at": "2026-11-02T08:00:08Z",
        "data": {"beat": 3},
        "metadata": {},
    }


class TestPositiveFixtures:
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
        """The py.* domain and version=1 must be inspectable in the fixtures themselves."""
        for fixture_name in POSITIVE_FIXTURES.values():
            raw = _load_fixture(fixture_name)
            assert isinstance(raw["type"], str)
            assert raw["type"].startswith("py.")
            assert raw["version"] == 1

    def test_session_created_fixture_hash_is_real_sha256(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(POSITIVE_FIXTURES[PY_SESSION_CREATED]))
        assert event.data.snapshot_hash == compute_snapshot_hash(event.data.config_snapshot)
        assert event.data.snapshot_hash == R1_SNAPSHOT_DIGEST

    def test_prompt_accepted_fixture_carries_run_id_metadata(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(POSITIVE_FIXTURES[PY_PROMPT_ACCEPTED]))
        assert event.metadata["run_id"] == event.data.run_id


class TestNegativeFixtures:
    """Negative fixtures: every rejection is loud (CORE-CONTRACTS §1: never silently ignore)."""

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(UnknownEventTypeError):
            default_event_registry().decode_durable(_load_fixture("events_unknown_type.json"))

    def test_unknown_version_rejected(self) -> None:
        with pytest.raises(UnknownEventVersionError):
            default_event_registry().decode_durable(_load_fixture("events_unknown_version.json"))

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "events_session_created_missing_field.json",
            "events_prompt_accepted_missing_field.json",
        ],
    )
    def test_missing_required_field_rejected(self, fixture_name: str) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(_load_fixture(fixture_name))

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "events_session_created_bad_extension.json",
            "events_prompt_accepted_bad_extension.json",
        ],
    )
    def test_invalid_extension_value_rejected(self, fixture_name: str) -> None:
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(_load_fixture(fixture_name))

    def test_snapshot_hash_mismatch_rejected(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(POSITIVE_FIXTURES[PY_SESSION_CREATED]))
        payload = event.model_dump(mode="json")
        payload["data"]["snapshot_hash"] = "0" * 64
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(payload)

    def test_missing_metadata_run_id_rejected(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_PROMPT_ACCEPTED])
        metadata = raw["metadata"]
        assert isinstance(metadata, dict)
        del metadata["run_id"]
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)


class TestEnvelopes:
    """Envelope mechanics shared by every event type."""

    def test_draft_has_no_seq_and_durable_adds_it(self) -> None:
        draft = _session_created_draft()
        assert not hasattr(draft, "seq")
        durable = DurableEvent[SessionCreatedData].model_validate({**draft.model_dump(), "seq": 1})
        assert durable.seq == 1
        assert durable.model_dump(exclude={"seq"}) == draft.model_dump()

    def test_durable_seq_must_be_positive(self) -> None:
        draft = _session_created_draft()
        with pytest.raises(ValidationError):
            DurableEvent[SessionCreatedData].model_validate({**draft.model_dump(), "seq": 0})

    def test_naive_datetime_rejected(self) -> None:
        payload = _session_created_draft().model_dump()
        payload["created_at"] = datetime(2026, 11, 2, 8, 0, 0)
        with pytest.raises(ValidationError):
            DurableEvent[SessionCreatedData].model_validate({**payload, "seq": 1})

    def test_created_at_normalized_to_utc(self) -> None:
        draft = _session_created_draft()
        payload = draft.model_dump()
        payload["created_at"] = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        event = DurableEvent[SessionCreatedData].model_validate({**payload, "seq": 1})
        assert event.created_at.utcoffset() == timedelta(0)

    def test_metadata_rejects_non_json_python_values(self) -> None:
        snapshot = ConfigSnapshot.create()
        kwargs: dict[str, Any] = {
            "id": "evt_0000000000000000000000000011",
            "type": PY_SESSION_CREATED,
            "version": 1,
            "session_id": "ses_0000000000000000000000000002",
            "created_at": datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            "data": SessionCreatedData(
                working_dir="D:/work/demo",
                config_snapshot=snapshot,
                snapshot_hash=compute_snapshot_hash(snapshot),
            ),
        }
        # Deliberately invalid runtime values; the wire model must reject them, not store them.
        bad_metadata: list[dict[str, Any]] = [
            {"when": datetime(2026, 11, 2, tzinfo=UTC)},
            {"blob": {1, 2}},
        ]
        for bad in bad_metadata:
            with pytest.raises(ValidationError):
                EventDraft[SessionCreatedData](**kwargs, metadata=bad)


class TestEphemeralEnvelope:
    """Ephemeral events: no version, no seq, live-only registry path."""

    def test_round_trip(self) -> None:
        registry = _ephemeral_registry()
        event = registry.decode_ephemeral(_heartbeat_raw())
        assert event.data.beat == 3
        assert registry.decode_ephemeral(event.model_dump_json()) == event

    def test_version_field_is_rejected(self) -> None:
        raw = _heartbeat_raw() | {"version": 1}
        with pytest.raises(ValidationError):
            _ephemeral_registry().decode_ephemeral(raw)

    def test_direct_construction_has_no_seq_or_version(self) -> None:
        event = EphemeralEvent[_HeartbeatData](
            id="evt_0000000000000000000000000012",
            type="py.test.heartbeat",
            session_id="ses_0000000000000000000000000002",
            created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            data=_HeartbeatData(beat=0),
        )
        assert not hasattr(event, "seq")
        assert not hasattr(event, "version")

    def test_durable_decode_rejects_ephemeral_definition(self) -> None:
        raw = _heartbeat_raw() | {"version": 1}
        with pytest.raises(DurabilityMismatchError):
            _ephemeral_registry().decode_durable(raw)

    def test_ephemeral_decode_rejects_durable_definition(self) -> None:
        with pytest.raises(DurabilityMismatchError):
            default_event_registry().decode_ephemeral(_heartbeat_raw() | {"type": PY_SESSION_CREATED})


class TestRegistry:
    """Registry behavior: duplicates, versioned keys, malformed input."""

    def test_duplicate_durable_definition_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate durable event definition"):
            EventRegistry(
                [
                    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedData),
                    EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedData),
                ]
            )

    def test_duplicate_ephemeral_definition_rejected(self) -> None:
        definition = EventDefinition(event_type="py.test.heartbeat", durability="ephemeral", data_model=_HeartbeatData)
        with pytest.raises(ValueError, match="Duplicate ephemeral event definition"):
            EventRegistry([definition, definition])

    def test_malformed_json_envelope_rejected(self) -> None:
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable("{not json")

    def test_non_json_constant_rejected(self) -> None:
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable('{"type": "py.session.created", "version": NaN}')

    def test_duplicate_json_object_keys_rejected(self) -> None:
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable(
                '{"id": "evt_a", "id": "evt_b", "type": "py.session.created", "version": 1}'
            )

    def test_missing_type_and_version_rejected(self) -> None:
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable({"id": "evt_x", "seq": 1})

    def test_boolean_version_rejected(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_SESSION_CREATED]) | {"version": True}
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable(raw)

    def test_versioned_type_matches_ts_format(self) -> None:
        assert versioned_type(PY_SESSION_CREATED, 1) == "py.session.created.1"


class TestSnapshotHashing:
    """canonical JSON and the R1 snapshot digest (§6.1/§6.2, T-A-002 ruling)."""

    def test_canonical_json_sorts_keys_and_compacts(self) -> None:
        value: dict[str, JsonValue] = {"b": 1, "a": [2, {"z": None, "y": True}]}
        assert canonical_json(value) == '{"a":[2,{"y":true,"z":null}],"b":1}'

    def test_canonical_json_rejects_nan(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_r1_snapshot_is_unique_and_digest_pinned(self) -> None:
        snapshot = ConfigSnapshot.create()
        assert snapshot == ConfigSnapshot.model_validate({"schema_version": 1, "permission_rules": []})
        assert compute_snapshot_hash(snapshot) == R1_SNAPSHOT_DIGEST

    def test_snapshot_requires_both_fields_and_literal_one(self) -> None:
        # bool is an int subclass; pydantic's Literal equality would accept True,
        # so §6.1's "no boolean version numbers" needs the explicit guard.
        raw_cases: list[dict[str, Any]] = [
            {},
            {"schema_version": 1},
            {"permission_rules": []},
            {"schema_version": None, "permission_rules": []},
            {"schema_version": "1", "permission_rules": []},
            {"schema_version": True, "permission_rules": []},
            {"schema_version": 2, "permission_rules": []},
        ]
        for raw in raw_cases:
            with pytest.raises(ValidationError):
                ConfigSnapshot.model_validate(raw)

    def test_snapshot_rejects_non_empty_rules_and_extra_keys(self) -> None:
        with pytest.raises(ValidationError):
            ConfigSnapshot.model_validate(
                {"schema_version": 1, "permission_rules": [{"effect": "allow", "action": "read"}]}
            )
        # An embedded hash inside the snapshot is explicitly rejected by §6.1.
        with pytest.raises(ValidationError):
            ConfigSnapshot.model_validate({"schema_version": 1, "permission_rules": [], "hash": "0" * 64})


class TestCoreError:
    """CoreError wire model (§8): shape, closed code registry, round trip."""

    def test_round_trip(self) -> None:
        error = CoreError(
            code=CoreErrorCode.STORAGE_CORRUPT,
            message="projection failed",
            retryable=False,
            details={"seq": 3},
        )
        assert CoreError.model_validate(json.loads(error.model_dump_json())) == error

    def test_details_default_to_empty_object(self) -> None:
        error = CoreError.model_validate({"code": "NotFound", "message": "gone", "retryable": False})
        assert error.code is CoreErrorCode.NOT_FOUND
        assert error.details == {}
        assert error.retryable is False

    def test_r1_codes_are_available(self) -> None:
        r1_codes = [
            CoreErrorCode.VALIDATION_ERROR,
            CoreErrorCode.NOT_FOUND,
            CoreErrorCode.SESSION_BUSY,
            CoreErrorCode.CONFLICT,
            CoreErrorCode.IDEMPOTENCY_CONFLICT,
            CoreErrorCode.INVALID_CURSOR,
            CoreErrorCode.SLOW_CONSUMER,
            CoreErrorCode.STORAGE_CORRUPT,
            CoreErrorCode.MIGRATION_REQUIRED,
            CoreErrorCode.SESSION_READ_ONLY,
        ]
        for code in r1_codes:
            assert CoreError(code=code, message="x", retryable=True).code is code

    def test_code_registry_matches_contract_section_8(self) -> None:
        expected = {
            "ValidationError",
            "UnsupportedCapability",
            "NotFound",
            "SessionBusy",
            "Conflict",
            "IdempotencyConflict",
            "InvalidCursor",
            "PermissionDenied",
            "ApprovalUnavailable",
            "ApprovalExpired",
            "ToolTimeout",
            "ToolFailed",
            "ExecutionUnknown",
            "ProviderUnavailable",
            "ProviderStreamInterrupted",
            "ContextLimitExceeded",
            "SlowConsumer",
            "StorageCorrupt",
            "MigrationRequired",
            "SessionReadOnly",
            "SessionClosed",
            "LifecycleCleanupFailed",
        }
        assert {code.value for code in CoreErrorCode} == expected

    def test_unknown_code_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CoreError.model_validate({"code": "Nope", "message": "x", "retryable": False})


class TestUserMessage:
    """UserMessage shape (§2.1 user row)."""

    def test_round_trip(self) -> None:
        message = UserMessage(
            id="msg_0000000000000000000000000009",
            type="user",
            created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            text="hi",
        )
        parsed = UserMessage.model_validate(json.loads(message.model_dump_json()))
        assert parsed == message
        assert parsed.metadata == {}

    def test_type_tag_enforced(self) -> None:
        with pytest.raises(ValidationError):
            UserMessage.model_validate(
                {
                    "id": "msg_0000000000000000000000000010",
                    "type": "assistant",
                    "created_at": "2026-11-02T08:00:00Z",
                    "text": "not a user message",
                }
            )

    def test_prompt_accepted_data_nests_user_message(self) -> None:
        data = PromptAcceptedData(
            request_id="req_0000000000000000000000000009",
            run_id="run_0000000000000000000000000009",
            message=UserMessage(
                id="msg_0000000000000000000000000011",
                type="user",
                created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
                text="nested",
            ),
        )
        assert data.message.type == "user"
        assert PromptAcceptedData.model_validate(json.loads(data.model_dump_json())) == data


class TestContractHardening:
    """T-C1-002 pins: §1 bool rule (M-1), §2.1 files option b, §3.1 ID prefixes, M-4/N-2."""

    def test_bool_version_rejected_on_construction(self) -> None:
        payload = _session_created_draft().model_dump()
        for bad in (True, False):
            with pytest.raises(ValidationError):
                EventDraft[SessionCreatedData].model_validate({**payload, "version": bad})

    def test_bool_seq_rejected_on_construction(self) -> None:
        payload = _session_created_draft().model_dump()
        for bad in (True, False):
            with pytest.raises(ValidationError):
                DurableEvent[SessionCreatedData].model_validate({**payload, "seq": bad})

    def test_string_version_rejected_on_decode_path(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_SESSION_CREATED]) | {"version": "1"}
        with pytest.raises(EventDecodeError):
            default_event_registry().decode_durable(raw)

    def test_bool_seq_rejected_on_decode_path(self) -> None:
        raw = _load_fixture(POSITIVE_FIXTURES[PY_SESSION_CREATED]) | {"seq": True}
        with pytest.raises(ValidationError):
            default_event_registry().decode_durable(raw)

    def test_lone_surrogate_fails_closed_in_canonical_bytes(self) -> None:
        # §6.2.1: illegal Unicode (lone surrogate) must never reach the hash. The
        # UTF-8 encode of the canonical text raises UnicodeEncodeError (⊂ ValueError).
        with pytest.raises(ValueError):
            canonical_json({"x": "\ud800"}).encode("utf-8")

    def test_empty_event_id_rejected(self) -> None:
        # N-2 first item, pinned directly: an empty id violates both min_length=1 and
        # ^evt_. EphemeralEvent declares its own id field, so each entry point is
        # exercised; DurableEvent inherits EventDraft's declaration.
        payload = _session_created_draft().model_dump()
        with pytest.raises(ValidationError):
            EventDraft[SessionCreatedData].model_validate({**payload, "id": ""})
        with pytest.raises(ValidationError):
            DurableEvent[SessionCreatedData].model_validate({**payload, "id": "", "seq": 1})
        with pytest.raises(ValidationError):
            EphemeralEvent[_HeartbeatData].model_validate({**_heartbeat_raw(), "id": ""})

    def test_event_and_session_id_prefixes_enforced(self) -> None:
        payload = _session_created_draft().model_dump()
        for bad_id in ("ev_1", "ses_1", "run_1", "msg_1"):
            with pytest.raises(ValidationError):
                EventDraft[SessionCreatedData].model_validate({**payload, "id": bad_id})
        for bad_session_id in ("se_1", "session_1", "evt_1", "run_1"):
            with pytest.raises(ValidationError):
                EventDraft[SessionCreatedData].model_validate({**payload, "session_id": bad_session_id})

    def test_ephemeral_id_prefixes_enforced(self) -> None:
        registry = _ephemeral_registry()
        with pytest.raises(ValidationError):
            registry.decode_ephemeral(_heartbeat_raw() | {"id": "ev_0001"})
        with pytest.raises(ValidationError):
            registry.decode_ephemeral(_heartbeat_raw() | {"session_id": "se_0001"})

    def test_user_message_id_prefix_enforced(self) -> None:
        base: dict[str, Any] = {"type": "user", "created_at": "2026-11-02T08:00:00Z", "text": "hi"}
        with pytest.raises(ValidationError):
            UserMessage.model_validate({**base, "id": "user_1"})
        message = UserMessage.model_validate({**base, "id": "msg_0000000000000000000000000099"})
        assert message.id.startswith("msg_")

    def test_user_message_files_key_rejected(self) -> None:
        base: dict[str, Any] = {
            "id": "msg_0000000000000000000000000099",
            "type": "user",
            "created_at": "2026-11-02T08:00:00Z",
            "text": "hi",
        }
        # §2.1 option b: the R1 v1 wire has no files field; the key itself — even
        # an empty list — is rejected, never silently dropped or tolerated.
        with pytest.raises(ValidationError):
            UserMessage.model_validate({**base, "files": []})
        with pytest.raises(ValidationError):
            UserMessage.model_validate({**base, "files": [{"path": "D:/work/demo/a.png"}]})

    def test_user_message_empty_text_allowed(self) -> None:
        message = UserMessage(
            id="msg_0000000000000000000000000098",
            type="user",
            created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            text="",
        )
        assert message.text == ""

    def test_run_id_prefix_enforced_in_data(self) -> None:
        valid = UserMessage(
            id="msg_0000000000000000000000000097",
            type="user",
            created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            text="hi",
        )
        with pytest.raises(ValidationError):
            PromptAcceptedData(request_id="req_1", run_id="rn_1", message=valid)

    def test_metadata_run_id_validated_wherever_present(self) -> None:
        payload = _session_created_draft().model_dump()
        # §3.1: whenever the metadata key run_id appears — even on a non-run-scoped
        # event — it must be a ^run_ string; null, non-string, wrong prefix rejected.
        for bad_metadata in ({"run_id": "rn_1"}, {"run_id": None}, {"run_id": 1}):
            with pytest.raises(ValidationError):
                EventDraft[SessionCreatedData].model_validate({**payload, "metadata": bad_metadata})
        accepted = EventDraft[SessionCreatedData].model_validate(
            {**payload, "metadata": {"run_id": "run_0000000000000000000000000095"}}
        )
        assert accepted.metadata["run_id"] == "run_0000000000000000000000000095"

    def test_request_id_has_no_prefix_pattern(self) -> None:
        valid = UserMessage(
            id="msg_0000000000000000000000000096",
            type="user",
            created_at=datetime(2026, 11, 2, 8, 0, 0, tzinfo=UTC),
            text="hi",
        )
        data = PromptAcceptedData(
            request_id="client-chosen-id-without-prefix",
            run_id="run_0000000000000000000000000096",
            message=valid,
        )
        assert data.request_id == "client-chosen-id-without-prefix"
