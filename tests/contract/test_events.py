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
from opencode.contracts.events import (
    EVENT_VERSION,
    PY_REASONING_DELTA,
    PY_REASONING_FINALIZED,
    PY_RUN_CANCEL_REQUESTED,
    PY_RUN_FINISHED,
    PY_RUN_STARTED,
    PY_STEP_FINISHED,
    PY_STEP_GENERATED,
    PY_STEP_STARTED,
    PY_TEXT_DELTA,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_INPUT_DELTA,
    PY_TOOL_PROGRESS,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    R1_EVENT_DEFINITIONS,
    R2_EVENT_DEFINITIONS,
    RUN_SCOPED_EVENT_TYPES,
    ReasoningDeltaData,
    ReasoningFinalizedData,
    RunCancelRequestedData,
    RunFinishedData,
    StepStartedData,
    TextDeltaData,
    TextFinalizedData,
    ToolFinishedData,
    ToolInputDeltaData,
    ToolProgressData,
    ToolRequestedData,
    ToolStartedData,
)
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import AssistantMessage, AssistantToolPart, ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolRef

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
        # T-C1-030: the probe flipped from py.session.created v2 (now a
        # registered R3-S6 version) to version 3 -- a permanently-unknown
        # version for this type (the T-C1-025 unknown-type precedent).
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


# ---------------------------------------------------------------------------
# R2-S1 (plan §2): the 15 new event types — fixtures, registration, data shapes.
# ---------------------------------------------------------------------------

R2_DURABLE_FIXTURES = {
    PY_RUN_STARTED: "events_run_started_valid.json",
    PY_RUN_CANCEL_REQUESTED: "events_run_cancel_requested_valid.json",
    PY_RUN_FINISHED: "events_run_finished_valid.json",
    PY_STEP_STARTED: "events_step_started_valid.json",
    PY_STEP_GENERATED: "events_step_generated_valid.json",
    PY_STEP_FINISHED: "events_step_finished_valid.json",
    PY_TEXT_FINALIZED: "events_text_finalized_valid.json",
    PY_REASONING_FINALIZED: "events_reasoning_finalized_valid.json",
    PY_TOOL_REQUESTED: "events_tool_requested_valid.json",
    PY_TOOL_STARTED: "events_tool_started_valid.json",
    PY_TOOL_FINISHED: "events_tool_finished_valid.json",
}

R2_EPHEMERAL_FIXTURES = {
    PY_TEXT_DELTA: "events_text_delta_valid.json",
    PY_REASONING_DELTA: "events_reasoning_delta_valid.json",
    PY_TOOL_INPUT_DELTA: "events_tool_input_delta_valid.json",
    PY_TOOL_PROGRESS: "events_tool_progress_valid.json",
}

R2_ALL_FIXTURES = {**R2_DURABLE_FIXTURES, **R2_EPHEMERAL_FIXTURES}

R2_EPHEMERAL_TYPES = frozenset(R2_EPHEMERAL_FIXTURES)


def _decode_r2(registry: EventRegistry, event_type: str, raw: Any) -> Any:
    """Decode through the durability path the type is registered under."""
    if event_type in R2_EPHEMERAL_TYPES:
        return registry.decode_ephemeral(raw)
    return registry.decode_durable(raw)


class TestR2PositiveFixtures:
    """R2 positive fixtures: decode with every field present and round-trip equal."""

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_round_trip_is_lossless(self, event_type: str, fixture_name: str) -> None:
        registry = default_event_registry()
        event = _decode_r2(registry, event_type, _load_fixture(fixture_name))
        assert event.type == event_type
        if event_type in R2_EPHEMERAL_TYPES:
            assert not hasattr(event, "seq")
            assert not hasattr(event, "version")
        else:
            assert event.version == 1
            assert event.seq >= 1
        assert _decode_r2(registry, event_type, event.model_dump_json()) == event

    def test_fixtures_expose_py_naming_version_and_run_metadata(self) -> None:
        """All 15 R2 types are run-internal: producers write run_id into metadata (§3.1 L110)."""
        for fixture_name in R2_ALL_FIXTURES.values():
            raw = _load_fixture(fixture_name)
            assert raw["type"].startswith("py.")
            assert raw["metadata"]["run_id"] == "run_0000000000000000000000000002"
        for fixture_name in R2_DURABLE_FIXTURES.values():
            assert _load_fixture(fixture_name)["version"] == 1
        for fixture_name in R2_EPHEMERAL_FIXTURES.values():
            raw = _load_fixture(fixture_name)
            assert "version" not in raw
            assert "seq" not in raw

    def test_run_metadata_matches_data_run_id(self) -> None:
        registry = default_event_registry()
        for event_type in (PY_RUN_STARTED, PY_RUN_CANCEL_REQUESTED, PY_RUN_FINISHED, PY_STEP_STARTED):
            event = _decode_r2(registry, event_type, _load_fixture(R2_ALL_FIXTURES[event_type]))
            assert event.metadata["run_id"] == event.data.run_id

    def test_step_fixtures_carry_assistant_messages(self) -> None:
        registry = default_event_registry()
        generated = registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_STEP_GENERATED]))
        finished = registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_STEP_FINISHED]))
        for event in (generated, finished):
            message = event.data.message
            assert isinstance(message, AssistantMessage)
            assert message.model.provider == "maas"
        plan_part = generated.data.message.parts[-1]
        assert isinstance(plan_part, AssistantToolPart)
        assert plan_part.state == "pending" and plan_part.effect_status == "not_started"
        assert finished.data.message.status == "completed"

    def test_tool_finished_fixture_is_model_readable(self) -> None:
        registry = default_event_registry()
        event = registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_TOOL_FINISHED]))
        assert event.data.state == "succeeded"
        assert any(isinstance(content, ToolTextContent) for content in event.data.content)
        assert any(isinstance(content, ToolFileContent) for content in event.data.content)


class TestR2NegativeFixtures:
    """R2 negative fixtures: every rejection is loud (§1; never silently skipped)."""

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_missing_required_field_rejected(self, event_type: str, fixture_name: str) -> None:
        stem = fixture_name.removesuffix("_valid.json")
        with pytest.raises(ValidationError):
            _decode_r2(default_event_registry(), event_type, _load_fixture(f"{stem}_missing_field.json"))

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_unknown_data_field_rejected(self, event_type: str, fixture_name: str) -> None:
        stem = fixture_name.removesuffix("_valid.json")
        with pytest.raises(ValidationError):
            _decode_r2(default_event_registry(), event_type, _load_fixture(f"{stem}_bad_extension.json"))

    def test_durability_mismatch_rejected_both_directions(self) -> None:
        registry = default_event_registry()
        with pytest.raises(DurabilityMismatchError):
            registry.decode_ephemeral(_load_fixture(R2_ALL_FIXTURES[PY_RUN_STARTED]))
        with pytest.raises(DurabilityMismatchError):
            registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_TEXT_DELTA]) | {"version": 1})

    def test_ephemeral_version_key_rejected(self) -> None:
        raw = _load_fixture(R2_ALL_FIXTURES[PY_TOOL_PROGRESS]) | {"version": 1}
        with pytest.raises(ValidationError):
            default_event_registry().decode_ephemeral(raw)


class TestR2RunScopedMetadata:
    """run_id metadata correlation: required on every run-scoped type.

    RUN_SCOPED_EVENT_TYPES covers the full R2 set (scheduler ledger #71,
    T-C1-014): py.prompt.accepted plus the 15 R2 types; py.session.created
    stays out. Every envelope of a set member must carry metadata.run_id at
    construction and decode, and a run_id key that appears anywhere stays
    validated.
    """

    def test_run_scoped_set_covers_the_full_r2_subset(self) -> None:
        assert RUN_SCOPED_EVENT_TYPES == frozenset({PY_PROMPT_ACCEPTED} | set(R2_ALL_FIXTURES))
        assert PY_SESSION_CREATED not in RUN_SCOPED_EVENT_TYPES

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_missing_run_id_metadata_rejected(self, event_type: str, fixture_name: str) -> None:
        """Flipped with the ledger-#71 expansion: a run-scoped envelope
        without metadata.run_id fails validation at decode."""
        raw = _load_fixture(fixture_name)
        del raw["metadata"]["run_id"]
        with pytest.raises(ValidationError):
            _decode_r2(default_event_registry(), event_type, raw)

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_missing_run_id_fixture_rejected(self, event_type: str, fixture_name: str) -> None:
        """The file-based negative fixtures pin the same rule (R2 naming)."""
        stem = fixture_name.removesuffix("_valid.json")
        with pytest.raises(ValidationError):
            _decode_r2(default_event_registry(), event_type, _load_fixture(f"{stem}_missing_run_id.json"))

    @pytest.mark.parametrize(("event_type", "fixture_name"), sorted(R2_ALL_FIXTURES.items()))
    def test_bad_run_id_metadata_rejected_whenever_present(self, event_type: str, fixture_name: str) -> None:
        for bad in ("rn_1", None, 1, ""):
            raw = _load_fixture(fixture_name)
            raw["metadata"]["run_id"] = bad
            with pytest.raises(ValidationError):
                _decode_r2(default_event_registry(), event_type, raw)


class TestR2Registration:
    """R1 definitions untouched; R2 definitions additive; registry resolution."""

    def test_r1_definitions_unchanged(self) -> None:
        assert R1_EVENT_DEFINITIONS == (
            EventDefinition(event_type=PY_SESSION_CREATED, durability="durable", data_model=SessionCreatedData),
            EventDefinition(event_type=PY_PROMPT_ACCEPTED, durability="durable", data_model=PromptAcceptedData),
        )

    def test_r2_definitions_shape(self) -> None:
        assert len(R2_EVENT_DEFINITIONS) == 15
        durable = [definition for definition in R2_EVENT_DEFINITIONS if definition.durability == "durable"]
        ephemeral = [definition for definition in R2_EVENT_DEFINITIONS if definition.durability == "ephemeral"]
        assert len(durable) == 11
        assert len(ephemeral) == 4
        assert all(definition.version == EVENT_VERSION for definition in R2_EVENT_DEFINITIONS)

    def test_r1_and_r2_types_are_disjoint_and_permission_rows_absent(self) -> None:
        r1_types = {definition.event_type for definition in R1_EVENT_DEFINITIONS}
        r2_types = {definition.event_type for definition in R2_EVENT_DEFINITIONS}
        assert r1_types & r2_types == set()
        # py.permission.* belongs to R3 (plan §0.1): it must not have leaked in.
        assert not any(event_type.startswith("py.permission.") for event_type in r2_types)

    def test_default_registry_resolves_all_r2_types(self) -> None:
        registry = default_event_registry()
        for definition in R2_EVENT_DEFINITIONS:
            if definition.durability == "durable":
                assert registry.resolve_durable(definition.event_type, EVENT_VERSION) is definition
            else:
                assert registry.resolve_ephemeral(definition.event_type) is definition

    def test_unknown_version_for_r2_type_rejected(self) -> None:
        with pytest.raises(UnknownEventVersionError):
            default_event_registry().resolve_durable(PY_RUN_STARTED, 2)

    def test_unknown_type_still_rejected(self) -> None:
        # T-C1-025: the probe flipped from py.permission.requested (now a
        # registered R3 type) to a permanently-unknown py.* type -- same
        # assertion semantics, same T-C1-014 probe-flip precedent.
        with pytest.raises(UnknownEventTypeError):
            default_event_registry().resolve_durable("py.permission.granted", 1)


class TestR2DataShapes:
    """Typed data field rules: ID prefixes, vocabularies, bool guards, tool-content rule."""

    def _step_started_raw(self, **overrides: Any) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "run_id": "run_0000000000000000000000000002",
            "step_id": "step_0000000000000000000000000002",
            "message_id": "msg_0000000000000000000000000012",
            "model": {"provider": "maas", "model": "glm-4.6"},
            "attempt": 1,
        }
        raw.update(overrides)
        return raw

    def test_step_started_shape_and_model_ref(self) -> None:
        data = StepStartedData.model_validate(self._step_started_raw())
        assert data.model == ModelRef(provider="maas", model="glm-4.6")
        with pytest.raises(ValidationError):
            StepStartedData.model_validate(self._step_started_raw(model={"provider": "", "model": "x"}))
        with pytest.raises(ValidationError):
            StepStartedData.model_validate(self._step_started_raw(message_id="message_1"))
        with pytest.raises(ValidationError):
            StepStartedData.model_validate(self._step_started_raw(run_id="rn_1"))

    @pytest.mark.parametrize("model", [TextFinalizedData, ReasoningFinalizedData])
    @pytest.mark.parametrize("bad", [True, False])
    def test_finalized_ordinal_rejects_bool(self, model: type[BaseModel], bad: bool) -> None:
        with pytest.raises(ValidationError):
            model.model_validate(
                {
                    "message_id": "msg_0000000000000000000000000012",
                    "part_id": "part_0000000000000000000000000001",
                    "ordinal": bad,
                    "text": "hi",
                }
            )

    @pytest.mark.parametrize("bad", [True, False])
    def test_step_started_attempt_rejects_bool(self, bad: bool) -> None:
        with pytest.raises(ValidationError):
            StepStartedData.model_validate(self._step_started_raw(attempt=bad))

    @pytest.mark.parametrize("bad", [True, False])
    def test_tool_input_delta_call_index_rejects_bool(self, bad: bool) -> None:
        with pytest.raises(ValidationError):
            ToolInputDeltaData.model_validate({"call_index": bad, "delta": "{"})

    def test_finalized_ids_are_located_by_message_and_part(self) -> None:
        for model in (TextFinalizedData, ReasoningFinalizedData):
            data = model.model_validate(
                {
                    "message_id": "msg_0000000000000000000000000012",
                    "part_id": "part_0000000000000000000000000001",
                    "ordinal": 0,
                    "text": "hi",
                }
            )
            assert data.ordinal == 0
            with pytest.raises(ValidationError):
                model.model_validate(
                    {
                        "message_id": "message_1",
                        "part_id": "part_0000000000000000000000000001",
                        "ordinal": 0,
                        "text": "hi",
                    }
                )

    def _tool_finished_raw(self, **overrides: Any) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "execution_id": "exec_0000000000000000000000000001",
            "state": "succeeded",
            "effect_status": "completed",
            "content": [{"type": "text", "text": "ok"}],
        }
        raw.update(overrides)
        return raw

    def test_tool_finished_requires_model_readable_text(self) -> None:
        with pytest.raises(ValidationError):
            ToolFinishedData.model_validate(
                self._tool_finished_raw(
                    content=[{"type": "file", "uri": "file:///D:/work/demo/notes.md", "mime": "text/markdown"}]
                )
            )
        with pytest.raises(ValidationError):
            ToolFinishedData.model_validate(self._tool_finished_raw(content=[]))
        data = ToolFinishedData.model_validate(
            self._tool_finished_raw(
                content=[
                    {"type": "file", "uri": "file:///D:/work/demo/notes.md", "mime": "text/markdown"},
                    {"type": "text", "text": "see attachment"},
                ]
            )
        )
        assert len(data.content) == 2

    def test_tool_finished_state_is_terminal_only(self) -> None:
        for good in ("succeeded", "failed", "denied", "cancelled", "unknown"):
            assert ToolFinishedData.model_validate(self._tool_finished_raw(state=good)).state == good
        for bad in ("pending", "running", ""):
            with pytest.raises(ValidationError):
                ToolFinishedData.model_validate(self._tool_finished_raw(state=bad))

    def test_tool_finished_effect_status_vocabulary(self) -> None:
        for good in ("not_started", "completed", "unknown"):
            assert ToolFinishedData.model_validate(self._tool_finished_raw(effect_status=good)).effect_status == good
        for bad in ("started", "partial"):
            with pytest.raises(ValidationError):
                ToolFinishedData.model_validate(self._tool_finished_raw(effect_status=bad))

    def test_tool_finished_error_nests_core_error(self) -> None:
        data = ToolFinishedData.model_validate(
            self._tool_finished_raw(
                state="failed",
                effect_status="not_started",
                error={"code": "ToolFailed", "message": "schema mismatch", "retryable": False},
            )
        )
        assert data.error is not None and data.error.code is not None
        assert data.error.code.value == "ToolFailed"

    def test_tool_started_permission_decision_vocabulary(self) -> None:
        for good in ("allow", "ask", "deny"):
            data = ToolStartedData.model_validate(
                {
                    "execution_id": "exec_0000000000000000000000000001",
                    "permission_decision": good,
                    "input_hash": "0" * 64,
                }
            )
            assert data.permission_decision == good
        with pytest.raises(ValidationError):
            ToolStartedData.model_validate(
                {
                    "execution_id": "exec_0000000000000000000000000001",
                    "permission_decision": "maybe",
                    "input_hash": "0" * 64,
                }
            )

    def test_run_finished_outcome_and_error(self) -> None:
        for good in ("succeeded", "failed", "interrupted"):
            assert RunFinishedData.model_validate({"run_id": "run_1", "outcome": good}).outcome == good
        for bad in ("cancelling", "cancelled", ""):
            with pytest.raises(ValidationError):
                RunFinishedData.model_validate({"run_id": "run_1", "outcome": bad})
        failed = RunFinishedData.model_validate(
            {
                "run_id": "run_0000000000000000000000000002",
                "outcome": "failed",
                "error": {"code": "ProviderUnavailable", "message": "endpoint down", "retryable": True},
            }
        )
        assert failed.error is not None and failed.error.retryable is True

    def test_run_cancel_requested_reason_vocabulary(self) -> None:
        for good in ("user", "shutdown"):
            assert RunCancelRequestedData.model_validate({"run_id": "run_1", "reason": good}).reason == good
        with pytest.raises(ValidationError):
            RunCancelRequestedData.model_validate({"run_id": "run_1", "reason": "timeout"})

    def test_tool_requested_shape_ref_and_hash(self) -> None:
        base: dict[str, Any] = {
            "message_id": "msg_0000000000000000000000000012",
            "tool_call_id": "call_0000000000000000000000000001",
            "execution_id": "exec_0000000000000000000000000001",
            "requested_name": "read",
            "input": {"path": "notes.md", "limit": 50},
            "input_hash": "5b8160c3ef7a2aa5fc1207a2740b1d783068c96fd850a1bbb969107e32c2d625",
        }
        data = ToolRequestedData.model_validate(base | {"tool_ref": {"namespace": "fs", "name": "read", "version": 1}})
        assert data.tool_ref == ToolRef(namespace="fs", name="read", version=1)
        assert ToolRequestedData.model_validate(base).tool_ref is None  # unresolved catalog name stays null
        with pytest.raises(ValidationError):
            ToolRequestedData.model_validate(base | {"input_hash": "xyz"})
        with pytest.raises(ValidationError):
            ToolRequestedData.model_validate(base | {"tool_ref": {"namespace": "fs", "name": "read", "version": True}})
        with pytest.raises(ValidationError):
            ToolRequestedData.model_validate(base | {"requested_name": ""})

    def test_step_generated_and_finished_locate_by_step_id(self) -> None:
        registry = default_event_registry()
        generated = registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_STEP_GENERATED]))
        finished = registry.decode_durable(_load_fixture(R2_ALL_FIXTURES[PY_STEP_FINISHED]))
        assert generated.data.step_id == finished.data.step_id == "step_0000000000000000000000000002"

    def test_delta_and_progress_shapes(self) -> None:
        for model in (TextDeltaData, ReasoningDeltaData):
            data = model.model_validate(
                {
                    "message_id": "msg_0000000000000000000000000012",
                    "part_id": "part_0000000000000000000000000001",
                    "delta": "Rea",
                }
            )
            assert data.delta == "Rea"
        progress = ToolProgressData.model_validate(
            {"execution_id": "exec_0000000000000000000000000001", "metadata": {"bytes_read": 512}}
        )
        assert progress.metadata == {"bytes_read": 512}
        assert ToolProgressData.model_validate({"execution_id": "exec_0000000000000000000000000001"}).metadata == {}
