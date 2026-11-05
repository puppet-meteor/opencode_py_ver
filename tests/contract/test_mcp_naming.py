"""R4-S3 contract tests: MCP naming, the catalog digest, the policy binding, the v3 wire.

Covers the T-C1-036 contract faces: the fixed ``mcp`` namespace with the
reversible ``mcp__<name>`` wire mapping and both collision branches (same
server / different server, CORE-CONTRACTS §5.1 L255), the deterministic
catalog digest and the atomic ``tools/list`` swap (§6 L304), the
saved-decision binding to the server startup fingerprint + catalog version
with the unknown-retry re-ask (§5.2 L268/L270), the ConfigSnapshotV3 /
``py.session.created`` v3 wire with its deterministic upcaster (§6.1 L287,
§8 L356/L358), and the host-tier ``mcp`` config block fail-closed matrix
(§6 L285/L287).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.config import ConfigError, ConfigLoader
from opencode.config.loader import ResolvedRuntime, mcp_startup_fingerprint
from opencode.contracts.events import (
    PY_SESSION_CREATED,
    ConfigSnapshotV2,
    ConfigSnapshotV3,
    SessionCreatedData,
    SessionCreatedDataV2,
    SessionCreatedDataV3,
    compute_snapshot_hash,
    default_event_registry,
    upcast_session_created,
)
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.invoker import ToolContext
from opencode.tools.policy import McpToolBinding, PermissionDecision, PermissionPolicy, SavedDecision
from opencode.tools.registry import RegistrationError, ToolRegistry, mcp_tool_spec

FIXTURES_DIR = Path(__file__).parent / "fixtures"

V3_FIXTURES = {
    "valid": "events_session_created_v3_valid.json",
    "bad_hash": "events_session_created_v3_bad_hash.json",
    "bad_extension": "events_session_created_v3_bad_extension.json",
}

#: The frozen v3 fixture fingerprint (generated through the real loader face)
#: — hardcoded so any accidental change to the canonical launch identity
#: fails loudly.
R4S3_FINGERPRINT = "944fd6b13b92ef8d97c17fa7744d52debd79c16bce9116d960de359219d8bc56"

_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}}


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _v3_snapshot_dict() -> dict[str, Any]:
    return _load_fixture(V3_FIXTURES["valid"])["data"]["config_snapshot"]


class _StaticAdapter:
    """Minimal ToolAdapter double: a fixed spec; the registry faces under test never execute."""

    def __init__(self, spec: ToolSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: dict[str, Any]) -> ToolResult:
        raise AssertionError("registry contract tests never execute adapters")


def _mcp_adapter(name: str, description: str = "the tool") -> _StaticAdapter:
    return _StaticAdapter(mcp_tool_spec(name, description, _SCHEMA))


# ---------------------------------------------------------------------------
# §5.1 L255: the fixed mcp namespace, ownership, and the catalog digest.
# ---------------------------------------------------------------------------


class TestMcpNaming:
    """Naming/ownership faces: wire ``mcp__<name>`` + name->server reversibility."""

    def test_mcp_tool_spec_pins_the_conservative_catalog_face(self) -> None:
        spec = mcp_tool_spec("echo", "Echo tool", _SCHEMA)
        assert spec.namespace == "mcp"
        assert spec.name == "echo"
        assert spec.version == 1
        assert spec.side_effects is True
        assert spec.idempotency_support is False
        assert spec.supported_content_types == ["text", "file"]

    def test_wire_name_and_ownership_roundtrip(self) -> None:
        registry = ToolRegistry()
        registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        catalog = registry.snapshot("ses_1")
        spec = catalog.resolve("mcp__echo")
        assert spec is not None
        assert (spec.namespace, spec.name) == ("mcp", "echo")
        assert registry.mcp_server_of("echo") == "alpha"
        with pytest.raises(RegistrationError, match="no owning server"):
            registry.mcp_server_of("structured")

    def test_same_server_same_name_rides_the_duplicate_rejection(self) -> None:
        registry = ToolRegistry()
        registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        with pytest.raises(RegistrationError, match="is already registered"):
            registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))

    def test_second_server_cannot_claim_an_owned_name(self) -> None:
        registry = ToolRegistry()
        registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        with pytest.raises(RegistrationError, match="already owned by server 'alpha'"):
            registry.register_mcp("beta", _mcp_adapter("echo", "Echo tool"))

    def test_non_mcp_namespace_is_rejected(self) -> None:
        registry = ToolRegistry()
        spec = ToolSpec(
            namespace="fs",
            name="echo",
            version=1,
            description="d",
            input_schema=_SCHEMA,
            supported_content_types=["text"],
            side_effects=True,
            idempotency_support=False,
        )
        with pytest.raises(RegistrationError, match="fixed namespace 'mcp'"):
            registry.register_mcp("alpha", _StaticAdapter(spec))

    def test_catalog_digest_is_deterministic_and_mcp_scoped(self) -> None:
        first = ToolRegistry()
        second = ToolRegistry()
        empty = first.mcp_catalog_version()
        first.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        first.register_mcp("alpha", _mcp_adapter("zzz", "Zzz tool"))
        second.register_mcp("alpha", _mcp_adapter("zzz", "Zzz tool"))
        second.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        assert first.mcp_catalog_version() == second.mcp_catalog_version() != empty
        before = first.mcp_catalog_version()
        first.register(
            _StaticAdapter(
                ToolSpec(
                    namespace="fs",
                    name="read",
                    version=1,
                    description="d",
                    input_schema=_SCHEMA,
                    supported_content_types=["text"],
                    side_effects=False,
                    idempotency_support=False,
                )
            )
        )
        assert first.mcp_catalog_version() == before  # non-MCP tools never move the digest

    def test_catalog_digest_tracks_the_visible_face(self) -> None:
        registry = ToolRegistry()
        registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        first = registry.mcp_catalog_version()
        registry.replace_mcp_server("alpha", [_mcp_adapter("echo", "Echo tool v2")])
        changed = registry.mcp_catalog_version()
        assert changed != first  # a description edit is a new catalog (L304)
        registry.replace_mcp_server("alpha", [_mcp_adapter("echo", "Echo tool v2")])
        assert registry.mcp_catalog_version() == changed  # the same face digests equal

    def test_rejected_replace_leaves_the_previous_set_intact(self) -> None:
        registry = ToolRegistry()
        registry.register_mcp("alpha", _mcp_adapter("echo", "Echo tool"))
        registry.register_mcp("beta", _mcp_adapter("shared", "Beta tool"))
        with pytest.raises(RegistrationError, match="already owned by server 'beta'"):
            registry.replace_mcp_server(
                "alpha", [_mcp_adapter("echo", "Echo tool"), _mcp_adapter("shared", "Alpha tool")]
            )
        with pytest.raises(RegistrationError, match="appears twice"):
            registry.replace_mcp_server("alpha", [_mcp_adapter("dup", "x"), _mcp_adapter("dup", "x")])
        assert registry.mcp_server_of("echo") == "alpha"
        assert registry.mcp_server_of("shared") == "beta"
        assert registry.snapshot("ses_1").resolve("mcp__echo") is not None


# ---------------------------------------------------------------------------
# §5.2 L268/L270: the saved-decision MCP binding.
# ---------------------------------------------------------------------------


def _evaluate(policy: PermissionPolicy, *, input_hash: str = "h1") -> PermissionDecision:
    return policy.evaluate(
        session_id="ses_1",
        action="mcp__echo",
        resources=(),
        input_hash=input_hash,
        working_dir="D:/work",
        side_effects=True,
    )


def _saved_decision(decision_id: str = "dec_1") -> SavedDecision:
    return SavedDecision(
        decision_id=decision_id,
        session_id="ses_1",
        action="mcp__echo",
        resources=(),
        input_hash="h0",
        boot_id="boot_1",
        expires_at=None,
    )


class TestPolicyMcpBinding:
    """The §5.2 L268 binding: fingerprint + catalog version gate saved approvals."""

    def test_mcp_namespace_defaults_to_ask(self) -> None:
        verdict = _evaluate(PermissionPolicy(boot_id="boot_1"))
        assert (verdict.effect, verdict.source) == ("ask", "default")

    def test_register_saved_enriches_with_the_current_binding(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")
        binding = McpToolBinding(server="fixture", fingerprint="f" * 64, catalog_version="c" * 64)
        policy.bind_mcp_tools("mcp__echo", binding)
        decision = _saved_decision()
        policy.register_saved(decision)
        stored = policy.decision("dec_1")
        assert stored is not None
        assert stored.mcp_binding == binding
        assert decision.mcp_binding is None  # the caller's frozen object is never rewritten
        verdict = _evaluate(policy)
        assert (verdict.effect, verdict.source) == ("allow", "saved_decision:dec_1")

    def test_catalog_version_change_forces_a_re_ask(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")
        policy.bind_mcp_tools(
            "mcp__echo", McpToolBinding(server="fixture", fingerprint="f" * 64, catalog_version="c" * 64)
        )
        policy.register_saved(_saved_decision())
        assert _evaluate(policy).effect == "allow"
        policy.bind_mcp_tools(
            "mcp__echo", McpToolBinding(server="fixture", fingerprint="f" * 64, catalog_version="d" * 64)
        )
        verdict = _evaluate(policy)
        assert (verdict.effect, verdict.source) == ("ask", "default")

    def test_fingerprint_change_forces_a_re_ask(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")
        policy.bind_mcp_tools(
            "mcp__echo", McpToolBinding(server="fixture", fingerprint="f" * 64, catalog_version="c" * 64)
        )
        policy.register_saved(_saved_decision())
        assert _evaluate(policy).effect == "allow"
        policy.bind_mcp_tools(
            "mcp__echo", McpToolBinding(server="fixture", fingerprint="e" * 64, catalog_version="c" * 64)
        )
        verdict = _evaluate(policy)
        assert (verdict.effect, verdict.source) == ("ask", "default")

    def test_identical_retry_after_unknown_re_asks(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")
        policy.bind_mcp_tools(
            "mcp__echo", McpToolBinding(server="fixture", fingerprint="f" * 64, catalog_version="c" * 64)
        )
        policy.register_saved(_saved_decision())
        assert _evaluate(policy, input_hash="h1").effect == "allow"
        policy.record_unknown("ses_1", "mcp__echo", "h1")
        verdict = _evaluate(policy, input_hash="h1")
        assert (verdict.effect, verdict.source) == ("ask", "default")

    def test_unwired_host_keeps_pre_r4_s3_semantics(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")  # bind_mcp_tools never called
        policy.register_saved(_saved_decision())
        stored = policy.decision("dec_1")
        assert stored is not None
        assert stored.mcp_binding is None
        assert _evaluate(policy).effect == "allow"

    def test_bind_rejects_non_mcp_actions(self) -> None:
        policy = PermissionPolicy(boot_id="boot_1")
        with pytest.raises(ValueError, match="not an MCP tool wire name"):
            policy.bind_mcp_tools(
                "fs__read", McpToolBinding(server="s", fingerprint="f" * 64, catalog_version="c" * 64)
            )


# ---------------------------------------------------------------------------
# §8 L356/L358: the py.session.created v3 wire + the deterministic upcaster.
# ---------------------------------------------------------------------------


class TestSessionCreatedV3Wire:
    """Registry + decode faces for the v3 event family."""

    def test_valid_fixture_decodes_and_round_trips(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(V3_FIXTURES["valid"]))
        assert (event.type, event.version) == (PY_SESSION_CREATED, 3)
        assert isinstance(event.data, SessionCreatedDataV3)
        assert isinstance(event.data, SessionCreatedDataV2)  # fold-through for frozen v2 consumers
        assert isinstance(event.data, SessionCreatedData)
        assert event.data.snapshot_hash == compute_snapshot_hash(event.data.config_snapshot)
        assert event.data.config_snapshot.mcp_servers[0].fingerprint == R4S3_FINGERPRINT
        assert default_event_registry().decode_durable(event.model_dump_json()) == event

    def test_bad_hash_fixture_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="snapshot_hash does not match"):
            default_event_registry().decode_durable(_load_fixture(V3_FIXTURES["bad_hash"]))

    def test_bad_extension_fixture_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="protocol"):
            default_event_registry().decode_durable(_load_fixture(V3_FIXTURES["bad_extension"]))

    def test_v1_and_v2_fixtures_still_decode(self) -> None:
        v1 = default_event_registry().decode_durable(_load_fixture("events_session_created_valid.json"))
        assert (v1.type, v1.version) == (PY_SESSION_CREATED, 1)
        assert type(v1.data) is SessionCreatedData
        v2 = default_event_registry().decode_durable(_load_fixture("events_session_created_v2_valid.json"))
        assert (v2.type, v2.version) == (PY_SESSION_CREATED, 2)
        assert type(v2.data) is SessionCreatedDataV2

    def test_three_versions_are_registered_side_by_side(self) -> None:
        registry = default_event_registry()
        assert registry.resolve_durable(PY_SESSION_CREATED, 1).data_model is SessionCreatedData
        assert registry.resolve_durable(PY_SESSION_CREATED, 2).data_model is SessionCreatedDataV2
        assert registry.resolve_durable(PY_SESSION_CREATED, 3).data_model is SessionCreatedDataV3

    def test_v3_snapshot_is_standalone_not_a_v2_instance(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(V3_FIXTURES["valid"]))
        snapshot = event.data.config_snapshot
        assert isinstance(snapshot, ConfigSnapshotV3)
        assert not isinstance(snapshot, ConfigSnapshotV2)  # every v2-typed isinstance gate stays v2-only


class TestUpcasterV3:
    """Deterministic v1->v2->v3 conversion (no silent backfill in either step)."""

    def test_v3_row_passes_through_with_its_v3_snapshot(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture(V3_FIXTURES["valid"]))
        runtime = upcast_session_created(event.data)
        assert runtime.working_dir == "D:/work/demo"
        assert isinstance(runtime.snapshot, ConfigSnapshotV3)
        assert runtime.snapshot == event.data.config_snapshot

    def test_v2_row_passes_through_with_its_v2_snapshot(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture("events_session_created_v2_valid.json"))
        runtime = upcast_session_created(event.data)
        assert isinstance(runtime.snapshot, ConfigSnapshotV2)
        assert not isinstance(runtime.snapshot, ConfigSnapshotV3)  # the mcp_servers face is never backfilled

    def test_v1_row_upcasts_to_no_runtime_face(self) -> None:
        event = default_event_registry().decode_durable(_load_fixture("events_session_created_valid.json"))
        runtime = upcast_session_created(event.data)
        assert runtime.snapshot is None


class TestConfigSnapshotV3Model:
    """The §6.1 L287 v3 field face: explicit, fail-closed, no fabricated defaults."""

    def test_missing_mcp_servers_is_rejected(self) -> None:
        raw = _v3_snapshot_dict()
        del raw["mcp_servers"]
        with pytest.raises(ValidationError):
            ConfigSnapshotV3.model_validate(raw)

    def test_empty_mcp_servers_is_the_explicit_empty_face(self) -> None:
        raw = _v3_snapshot_dict()
        raw["mcp_servers"] = []
        assert ConfigSnapshotV3.model_validate(raw).mcp_servers == []

    def test_env_refs_must_be_sorted(self) -> None:
        raw = _v3_snapshot_dict()
        raw["mcp_servers"][0]["env_refs"] = ["MCP_FIXTURE_MODE", "MCP_FIXTURE_LOG"]
        with pytest.raises(ValidationError, match="sorted"):
            ConfigSnapshotV3.model_validate(raw)

    def test_env_refs_must_be_unique(self) -> None:
        raw = _v3_snapshot_dict()
        raw["mcp_servers"][0]["env_refs"] = ["A", "A"]
        with pytest.raises(ValidationError, match="duplicates"):
            ConfigSnapshotV3.model_validate(raw)

    def test_fingerprint_must_be_sha256_hex(self) -> None:
        raw = _v3_snapshot_dict()
        raw["mcp_servers"][0]["fingerprint"] = "not-a-digest"
        with pytest.raises(ValidationError, match="pattern"):
            ConfigSnapshotV3.model_validate(raw)

    def test_empty_command_entry_is_rejected(self) -> None:
        raw = _v3_snapshot_dict()
        raw["mcp_servers"][0]["command"] = [""]
        with pytest.raises(ValidationError, match="non-empty"):
            ConfigSnapshotV3.model_validate(raw)


# ---------------------------------------------------------------------------
# §6 L285/L287: host-tier mcp loading (the fail-closed matrix).
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _server_entry(command: list[str], **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "local", "command": command}
    entry.update(extra)
    return entry


def _resolve_v3(tmp_path: Path, servers: dict[str, Any]) -> ResolvedRuntime:
    global_path = _write_json(
        tmp_path / "global.json",
        {"model": "m", "context_limit": 100000, "mcp": {"servers": servers}},
    )
    return ConfigLoader(working_dir=tmp_path, global_config=global_path).resolve_runtime_v3(
        endpoint_url="https://api.example.test/v1"
    )


class TestHostTierMcpLoading:
    """The host-tier ``mcp`` block: legal face + every out-of-scope shape fails closed."""

    def test_legal_block_loads_v3_refs_and_keeps_the_v2_face(self, tmp_path: Path) -> None:
        command = [sys.executable, "server.py"]
        runtime = _resolve_v3(
            tmp_path,
            {"fixture": _server_entry(command, environment={"SECRET_TOKEN": "super-secret", "A": "b"}, cwd="D:/srv")},
        )
        v2 = ConfigLoader(working_dir=tmp_path, global_config=tmp_path / "global.json").resolve_runtime(
            endpoint_url="https://api.example.test/v1"
        )
        snapshot = runtime.snapshot
        assert isinstance(snapshot, ConfigSnapshotV3)
        assert snapshot.schema_version == 3
        assert runtime.settings == v2.settings  # the v2 assembly runs unchanged underneath
        assert runtime.roots == v2.roots
        (ref,) = snapshot.mcp_servers
        assert ref.server == "fixture"
        assert ref.command == command
        assert ref.cwd == "D:/srv"
        assert ref.env_refs == ["A", "SECRET_TOKEN"]
        assert ref.fingerprint == mcp_startup_fingerprint("fixture", command, ["A", "SECRET_TOKEN"])
        assert "super-secret" not in snapshot.model_dump_json()  # environment values never ride (L302)
        assert isinstance(v2.snapshot, ConfigSnapshotV2)  # the CLI e2e red line: resolve_runtime stays v2

    def test_no_mcp_block_yields_the_explicit_empty_face(self, tmp_path: Path) -> None:
        global_path = _write_json(tmp_path / "global.json", {"model": "m", "context_limit": 100000})
        runtime = ConfigLoader(working_dir=tmp_path, global_config=global_path).resolve_runtime_v3(
            endpoint_url="https://api.example.test/v1"
        )
        assert isinstance(runtime.snapshot, ConfigSnapshotV3)
        assert runtime.snapshot.mcp_servers == []

    def test_disabled_server_is_excluded(self, tmp_path: Path) -> None:
        runtime = _resolve_v3(
            tmp_path,
            {"on": _server_entry(["python", "on.py"]), "off": _server_entry(["python", "off.py"], disabled=True)},
        )
        assert isinstance(runtime.snapshot, ConfigSnapshotV3)
        assert [ref.server for ref in runtime.snapshot.mcp_servers] == ["on"]

    def test_servers_are_emitted_sorted_by_name(self, tmp_path: Path) -> None:
        runtime = _resolve_v3(
            tmp_path,
            {"zeta": _server_entry(["python", "z.py"]), "alpha": _server_entry(["python", "a.py"])},
        )
        assert isinstance(runtime.snapshot, ConfigSnapshotV3)
        assert [ref.server for ref in runtime.snapshot.mcp_servers] == ["alpha", "zeta"]

    def test_remote_type_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="remote servers are not supported"):
            _resolve_v3(tmp_path, {"r": _server_entry(["python", "x.py"], type="remote")})

    def test_missing_type_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r'must be "local"'):
            _resolve_v3(tmp_path, {"s": {"command": ["python", "x.py"]}})

    def test_timeout_key_gets_a_named_out_of_scope_error(self, tmp_path: Path) -> None:
        global_path = _write_json(
            tmp_path / "global.json",
            {"model": "m", "context_limit": 100000, "mcp": {"timeout": {"startup": 5}, "servers": {}}},
        )
        with pytest.raises(ConfigError, match="not supported in this build"):
            ConfigLoader(working_dir=tmp_path, global_config=global_path).resolve_runtime_v3(
                endpoint_url="https://api.example.test/v1"
            )

    @pytest.mark.parametrize("field,value", [("protocol", "2025-06-18"), ("codemode", "ask")])
    def test_out_of_scope_server_fields_fail_closed(self, tmp_path: Path, field: str, value: str) -> None:
        with pytest.raises(ConfigError, match="not a supported local server field"):
            _resolve_v3(tmp_path, {"s": _server_entry(["python", "x.py"], **{field: value})})

    def test_command_shape_is_rejected(self, tmp_path: Path) -> None:
        for bad in ("python", [], [""], [1]):
            with pytest.raises(ConfigError, match="must be a non-empty list"):
                _resolve_v3(tmp_path, {"s": {"type": "local", "command": bad}})

    def test_environment_shape_is_rejected(self, tmp_path: Path) -> None:
        for bad in ({"A": 1}, ["A"], "A"):
            entry = {"type": "local", "command": ["python", "x.py"], "environment": bad}
            with pytest.raises(ConfigError, match="must be an object mapping"):
                _resolve_v3(tmp_path, {"s": entry})

    def test_project_layer_cannot_extend_host_tier_mcp(self, tmp_path: Path) -> None:
        _write_json(
            tmp_path / "opencode.json",
            {"mcp": {"servers": {"s": {"type": "local", "command": ["python", "x.py"]}}}},
        )
        global_path = _write_json(tmp_path / "global.json", {"model": "m", "context_limit": 100000})
        with pytest.raises(ConfigError, match="host-tier configuration"):
            ConfigLoader(working_dir=tmp_path, global_config=global_path).resolve_runtime_v3(
                endpoint_url="https://api.example.test/v1"
            )

    def test_fingerprint_tracks_argv_and_env_keys_not_values_or_cwd(self, tmp_path: Path) -> None:
        base = _resolve_v3(tmp_path, {"s": _server_entry(["python", "x.py"], environment={"A": "v1"}, cwd="D:/one")})
        assert isinstance(base.snapshot, ConfigSnapshotV3)
        base_fingerprint = base.snapshot.mcp_servers[0].fingerprint
        for servers in (
            {"s": _server_entry(["python", "x.py"], environment={"A": "v2"}, cwd="D:/one")},  # value change only
            {"s": _server_entry(["python", "x.py"], environment={"A": "v1"}, cwd="D:/two")},  # cwd change only
        ):
            runtime = _resolve_v3(tmp_path, servers)
            assert isinstance(runtime.snapshot, ConfigSnapshotV3)
            assert runtime.snapshot.mcp_servers[0].fingerprint == base_fingerprint
        for servers in (
            {"s": _server_entry(["python", "y.py"], environment={"A": "v1"}, cwd="D:/one")},  # argv change
            {
                "s": _server_entry(["python", "x.py"], environment={"A": "v1", "B": "v1"}, cwd="D:/one")
            },  # env key change
        ):
            runtime = _resolve_v3(tmp_path, servers)
            assert isinstance(runtime.snapshot, ConfigSnapshotV3)
            assert runtime.snapshot.mcp_servers[0].fingerprint != base_fingerprint

    def test_surrogate_in_the_v3_face_fails_closed(self, tmp_path: Path) -> None:
        entry = {"type": "local", "command": ["python", "x.py"], "cwd": "\ud800"}
        with pytest.raises(ConfigError, match="not a legal local server reference"):
            _resolve_v3(tmp_path, {"s": entry})
