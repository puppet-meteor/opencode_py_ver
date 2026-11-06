"""R3-S6 integration tests: ConfigLoader hierarchy/merge, instructions, roots, recovery, CLI face.

Covers the T-C1-030 delivery faces end to end: the §6 L283-285 layer chain
(builtin < global < project-root→cwd < CLI) and merge laws, AGENTS.md
instruction assembly with the §6.1 L287 caps, host-only trust keys rejected
in project layers (acceptance C), the multi-root fs tool boundary, the §6.1
L302 recovery faces, and the CLI injection surface persisting a real v2
snapshot into a migrated database.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from opencode.cli import main
from opencode.config import CliOverrides, ConfigError, ConfigLoader, InstructionLimitError, RecoveryStoppedError
from opencode.config.loader import HOST_ONLY_KEYS, _merge_json, _sanitize_endpoint
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import (
    ConfigSnapshot,
    ConfigSnapshotV2,
    SessionCreatedData,
    SessionCreatedDataV2,
    SessionCreatedDataV3,
    compute_snapshot_hash,
    upcast_session_created,
)
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.session.service import _coerce_config_snapshot
from opencode.storage import StoreValidationError
from opencode.tools.builtin import FsReadAdapter, FsWriteAdapter
from opencode.tools.invoker import ToolContext
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
)


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id="ses_test00000000000001",
        run_id="run_test00000000000001",
        message_id="msg_test0000000000001",
        tool_call_id="tcl_test00000000000001",
        working_dir=str(working_dir),
    )


def _text(result: Any) -> str:
    parts = [part for part in result.content if isinstance(part, ToolTextContent)]
    assert len(parts) == 1
    return parts[0].text


def _symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink, skipping when the OS privilege is unavailable (R0-B precedent)."""
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("symlink creation privilege not held (winerror 1314)")
        raise


# ---------------------------------------------------------------------------
# §6 L285 merge laws.
# ---------------------------------------------------------------------------


def test_merge_laws_scalar_map_list_and_two_append_exceptions() -> None:
    base: JsonObject = {
        "model": "a",
        "permission": {"rules": [{"r": 1}]},
        "instructions": ["x"],
        "other": {"keep": [1, 2]},
    }
    override: JsonObject = {
        "model": "b",
        "permission": {"rules": [{"r": 2}]},
        "instructions": ["y"],
        "other": {"keep": [9]},
    }
    merged = _merge_json(base, override)
    assert merged["model"] == "b"  # scalar: the later layer overrides
    permission = merged["permission"]
    assert isinstance(permission, dict)
    assert permission["rules"] == [{"r": 1}, {"r": 2}]  # append exception
    assert merged["instructions"] == ["x", "y"]  # append exception
    other = merged["other"]
    assert isinstance(other, dict)
    assert other["keep"] == [9]  # every other list replaces
    recursed = _merge_json(base, {"other": {"nested": True}})
    assert recursed["other"] == {"keep": [1, 2], "nested": True}  # maps recurse


# ---------------------------------------------------------------------------
# §6 L283 hierarchy + §6 L285 validation.
# ---------------------------------------------------------------------------


def test_full_hierarchy_builtin_global_project_layers_then_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    (project / "mid" / "cwd").mkdir(parents=True)
    (project / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # worktree-style marker file
    global_path = _write_json(
        tmp_path / "global.json",
        {
            "model": "global-model",
            "context_limit": 2000,
            "output_reserve": 50,
            "max_steps": 7,
            "instructions": ["global"],
        },
    )
    _write_json(project / "opencode.json", {"model": "proj-model", "context_limit": 111, "instructions": ["proj"]})
    _write_json(project / "mid" / "cwd" / "opencode.json", {"context_limit": 222, "instructions": ["cwd"]})
    monkeypatch.chdir(project / "mid" / "cwd")

    loader = ConfigLoader(global_config=global_path)
    layered = loader.load()
    # project beats global on every overlapping key; the cwd layer beats the root layer.
    assert layered.values["model"] == "proj-model"
    assert layered.values["context_limit"] == 222
    assert layered.values["output_reserve"] == 50
    assert layered.values["max_steps"] == 7

    runtime = loader.resolve_runtime(cli=CliOverrides(model="cli-model"), endpoint_url="https://api.example.test/v1")
    # CLI is the top layer (L283): the flag beats the project layer's model.
    assert runtime.settings.model.model == "cli-model"
    assert runtime.settings.context_limit == 222
    assert runtime.settings.output_reserve == 50
    assert runtime.settings.timeout_s == 30.0  # builtin layer default
    assert runtime.settings.safety_margin == 0  # builtin layer default
    assert runtime.snapshot.limits.max_steps == 7
    assert runtime.snapshot.endpoint_ref == "https://api.example.test/v1"
    assert runtime.roots == ()
    # Instructions assemble outer-to-inner across tiers (global, project root, cwd).
    assert [source.text for source in runtime.snapshot.instructions] == ["global", "proj", "cwd"]
    assert runtime.settings.system_prompt == "global\n\nproj\n\ncwd"


def test_endpoint_ref_strips_userinfo_and_query() -> None:
    loader = ConfigLoader(working_dir=Path.cwd())
    runtime = loader.resolve_runtime(
        cli=CliOverrides(model="m", context_limit=1000, output_reserve=16),
        endpoint_url="https://user:secret@api.example.test:8443/v1?token=x#frag",
    )
    assert runtime.snapshot.endpoint_ref == "https://api.example.test:8443/v1"


@pytest.mark.parametrize("key", sorted(HOST_ONLY_KEYS))
def test_host_only_keys_are_rejected_in_project_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Acceptance C: a project config can never extend host roots/endpoint/MCP/credentials trust."""
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "opencode.json", {key: []})
    with pytest.raises(ConfigError, match="host-tier"):
        ConfigLoader().load()


def test_unknown_field_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "opencode.json", {"bogus": 1})
    with pytest.raises(ConfigError, match="unknown config field"):
        ConfigLoader().load()


def test_no_vcs_marker_means_cwd_is_the_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "cwd").mkdir()
    _write_json(tmp_path / "opencode.json", {"model": "parent-model"})
    _write_json(tmp_path / "cwd" / "opencode.json", {"model": "cwd-model"})
    monkeypatch.chdir(tmp_path / "cwd")
    assert ConfigLoader().load().values["model"] == "cwd-model"


def test_project_root_must_be_the_cwd_or_an_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="ancestor"):
        ConfigLoader(project_root=other)


def test_explicit_global_config_path_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="does not exist"):
        ConfigLoader(global_config=tmp_path / "missing.json")


def test_broken_global_config_json_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="not valid JSON"):
        ConfigLoader(global_config=tmp_path / "broken.json").load()


# ---------------------------------------------------------------------------
# §6.1 L287 instructions: AGENTS.md + config strings, caps, surrogate pre-scan.
# ---------------------------------------------------------------------------


def test_agents_md_and_config_instructions_assemble_outer_to_inner_with_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("PA", encoding="utf-8")
    global_path = _write_json(
        tmp_path / "opencode.json", {"model": "m", "context_limit": 100000, "instructions": ["ps"]}
    )
    # The global config directory doubles as the project root: the same
    # AGENTS.md is discovered by both tiers and must dedup by resolved path.
    monkeypatch.chdir(tmp_path)
    runtime = ConfigLoader(global_config=global_path).resolve_runtime(endpoint_url="https://e.test")
    assert [source.text for source in runtime.snapshot.instructions] == ["PA", "ps"]
    assert runtime.snapshot.instructions[0].path == str((tmp_path / "AGENTS.md").resolve())
    assert runtime.snapshot.instructions[1].path == str(global_path.resolve())
    assert runtime.settings.system_prompt == "PA\n\nps"


def test_instruction_per_source_cap_exact_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    exactly = _write_json(tmp_path / "g.json", {"model": "m", "context_limit": 100000, "instructions": ["a" * 65536]})
    assert ConfigLoader(global_config=exactly).resolve_runtime(endpoint_url="https://e.test").settings.system_prompt
    over = _write_json(tmp_path / "g2.json", {"model": "m", "context_limit": 100000, "instructions": ["a" * 65537]})
    with pytest.raises(InstructionLimitError, match="per-source cap"):
        ConfigLoader(global_config=over).resolve_runtime(endpoint_url="https://e.test")


def test_instruction_total_cap_exact_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    exact = _write_json(
        tmp_path / "g.json",
        {"model": "m", "context_limit": 100000, "instructions": ["a" * 65536] * 4},  # exactly 256 KiB
    )
    assert ConfigLoader(global_config=exact).resolve_runtime(endpoint_url="https://e.test").settings.system_prompt
    over = _write_json(
        tmp_path / "g2.json",
        {"model": "m", "context_limit": 100000, "instructions": ["a" * 65536] * 4 + ["b"]},
    )
    with pytest.raises(InstructionLimitError, match="byte cap"):
        ConfigLoader(global_config=over).resolve_runtime(endpoint_url="https://e.test")


def test_surrogate_instruction_is_rejected_by_the_pre_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduler ledger row #19: a named ConfigError, never a raw UnicodeEncodeError."""
    (tmp_path / "g.json").write_text(
        json.dumps({"model": "m", "context_limit": 100000, "instructions": ["\ud800"]}), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="surrogate"):
        ConfigLoader(global_config=tmp_path / "g.json").resolve_runtime(endpoint_url="https://e.test")


# ---------------------------------------------------------------------------
# §6 L285/L287 roots: global-only declaration, loader/adapter consistency.
# ---------------------------------------------------------------------------


def test_roots_declared_globally_reach_snapshot_and_adapters_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    global_path = _write_json(
        tmp_path / "g.json",
        {"model": "m", "context_limit": 100000, "roots": [str(extra)]},
    )
    monkeypatch.chdir(tmp_path)
    runtime = ConfigLoader(global_config=global_path).resolve_runtime(endpoint_url="https://e.test")
    assert runtime.roots == (str(extra),)
    assert runtime.snapshot.roots == [str(extra)]  # one loader source, no drift

    relative = _write_json(
        tmp_path / "g2.json",
        {"model": "m", "context_limit": 100000, "roots": ["relative/path"]},
    )
    with pytest.raises(ConfigError, match="absolute path"):
        ConfigLoader(global_config=relative).resolve_runtime(endpoint_url="https://e.test")


async def test_read_adapter_multi_root_allow_and_reject(tmp_path: Path) -> None:
    working = tmp_path / "wd"
    extra = tmp_path / "extra"
    working.mkdir()
    extra.mkdir()
    (working / "in.txt").write_text("inside\n", encoding="utf-8")
    (extra / "out.txt").write_text("declared-root file\n", encoding="utf-8")
    (tmp_path / "victim.txt").write_text("outside everything\n", encoding="utf-8")

    adapter = FsReadAdapter(roots=(str(extra),))
    context = _context(working)

    stayed = await adapter.execute(context, {"path": "in.txt"})  # working_dir stays a root (⑤)
    assert stayed.error is None
    assert _text(stayed).endswith("1: inside")
    by_absolute = await adapter.execute(context, {"path": str(extra / "out.txt")})
    assert by_absolute.error is None
    assert "declared-root file" in _text(by_absolute)
    by_relative = await adapter.execute(context, {"path": "out.txt"})  # resolves in the declared root
    assert by_relative.error is None
    assert "declared-root file" in _text(by_relative)

    denied = await adapter.execute(context, {"path": str(tmp_path / "victim.txt")})
    assert denied.error is not None and denied.error.code == CoreErrorCode.PERMISSION_DENIED
    not_found = await adapter.execute(context, {"path": "missing.txt"})
    assert not_found.error is not None and not_found.error.code == CoreErrorCode.NOT_FOUND

    default_adapter = FsReadAdapter()  # roots=() keeps the exact R2 semantics
    rejected = await default_adapter.execute(context, {"path": str(extra / "out.txt")})
    assert rejected.error is not None and rejected.error.code == CoreErrorCode.PERMISSION_DENIED
    # A relative miss inside the (still-rooted) working_dir is NOT_FOUND, not a permission error.
    rel_missing = await default_adapter.execute(context, {"path": "out.txt"})
    assert rel_missing.error is not None and rel_missing.error.code == CoreErrorCode.NOT_FOUND


async def test_write_adapter_multi_root(tmp_path: Path) -> None:
    working = tmp_path / "wd"
    extra = tmp_path / "extra"
    working.mkdir()
    extra.mkdir()
    adapter = FsWriteAdapter(roots=(str(extra),))
    context = _context(working)

    # A relative creation target lands in the working_dir (first root wins, ⑤).
    in_working = await adapter.execute(context, {"path": "new.txt", "content": "w"})
    assert in_working.error is None
    assert (working / "new.txt").read_text(encoding="utf-8") == "w"
    # The declared root is writable through its absolute path.
    in_extra = await adapter.execute(context, {"path": str(extra / "new.txt"), "content": "e"})
    assert in_extra.error is None
    assert (extra / "new.txt").read_text(encoding="utf-8") == "e"
    assert not (working / "subdir-new.txt").exists()


async def test_cross_root_symlink_escape_is_denied(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    extra = tmp_path / "extra"
    extra.mkdir()
    _symlink_or_skip(outside / "secret.txt", extra / "link.txt")

    adapter = FsReadAdapter(roots=(str(extra),))
    denied = await adapter.execute(_context(extra), {"path": "link.txt"})
    assert denied.error is not None and denied.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "escapes the working directory" in _text(denied)


# ---------------------------------------------------------------------------
# §6.1 L302 recovery faces.
# ---------------------------------------------------------------------------


def _resolved_runtime(tmp_path: Path) -> Any:
    return ConfigLoader(working_dir=tmp_path).resolve_runtime(
        cli=CliOverrides(model="m", context_limit=100000),
        endpoint_url="https://api.example.test/v1",
    )


def test_recover_replays_the_recorded_v2_snapshot(tmp_path: Path) -> None:
    runtime = _resolved_runtime(tmp_path)
    data = SessionCreatedDataV2(
        working_dir=str(tmp_path),
        config_snapshot=runtime.snapshot,
        snapshot_hash=compute_snapshot_hash(runtime.snapshot),
    )
    recovered = ConfigLoader(working_dir=tmp_path).recover(data)
    assert recovered.settings == runtime.settings
    assert recovered.roots == runtime.roots
    assert recovered.snapshot == runtime.snapshot


def test_recover_stops_on_a_v1_snapshot(tmp_path: Path) -> None:
    """§6.1 L291/L302: the minimal snapshot has no runtime face; no silent backfill."""
    data = SessionCreatedData(
        working_dir=str(tmp_path),
        config_snapshot=ConfigSnapshot.create(),
        snapshot_hash="77c932c31585517c128479af02c40cd377e26d4d25ce57df197fc8509e14f802",
    )
    with pytest.raises(RecoveryStoppedError, match="v1 minimal config snapshot"):
        ConfigLoader(working_dir=tmp_path).recover(data)


def test_recover_stops_when_the_working_directory_is_gone(tmp_path: Path) -> None:
    runtime = _resolved_runtime(tmp_path)
    data = SessionCreatedDataV2(
        working_dir=str(tmp_path / "vanished"),
        config_snapshot=runtime.snapshot,
        snapshot_hash=compute_snapshot_hash(runtime.snapshot),
    )
    with pytest.raises(RecoveryStoppedError, match="no longer exists"):
        ConfigLoader(working_dir=tmp_path).recover(data)


# ---------------------------------------------------------------------------
# CLI injection face end to end (§6 L283 + §7 create with the v2 snapshot).
# ---------------------------------------------------------------------------


class _ServerHost:
    """Dedicated-thread event loop hosting the fake server across main() runs (test_cli.py pattern)."""

    def __init__(self, responses: list[ScriptedResponse]) -> None:
        self.server = FakeOpenAIServer(responses)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(started,), daemon=True)
        self._thread.start()
        if not started.wait(timeout=10.0):
            raise RuntimeError("fake server thread did not start")
        return self.server.base_url

    def _run(self, started: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.server.start())
            started.set()
            loop.run_forever()
        finally:
            loop.run_until_complete(self.server.aclose())
            loop.close()
            asyncio.set_event_loop(None)

    def stop(self) -> None:
        if self._loop is not None and self._thread is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10.0)


def _ok_response(*deltas: str) -> ScriptedResponse:
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def test_cli_end_to_end_config_supplied_runtime_and_v3_snapshot_in_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host = _ServerHost([_ok_response("hel", "lo")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        global_path = _write_json(
            tmp_path / "global.json",
            {"model": "fake-model", "context_limit": 100000, "instructions": ["You are the test agent."]},
        )
        db_path = tmp_path / "cli-e2e.db"
        # No --model/--context-limit flags: the config layer supplies them (§6 L283).
        code = main(
            [
                "--db",
                str(db_path),
                "--base-url",
                f"{base_url}/v1",
                "--api-key",
                "r0-test-key",
                "--config",
                str(global_path),
                "say",
                "hello",
            ]
        )
    finally:
        host.stop()
    assert code == 0
    assert "[final] hello" in capsys.readouterr().out

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version, data FROM events WHERE type = 'py.session.created'").fetchone()
    assert row is not None
    version, raw = row
    assert version == 3
    payload = json.loads(raw) if isinstance(raw, str | bytes) else raw
    data = SessionCreatedDataV3.model_validate(payload)
    assert Path(data.working_dir) == tmp_path
    assert data.config_snapshot.model.model == "fake-model"
    assert data.config_snapshot.limits.context_limit == 100000
    assert data.config_snapshot.instructions[0].text == "You are the test agent."
    assert data.config_snapshot.mcp_servers == []

    runtime = upcast_session_created(data)
    assert runtime.snapshot is not None
    recovered = ConfigLoader(working_dir=tmp_path).recover(data)
    resolved = ConfigLoader(global_config=global_path).resolve_runtime(endpoint_url=f"{base_url}/v1")
    assert recovered.settings == resolved.settings
    assert recovered.roots == resolved.roots


def test_cli_missing_model_without_config_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    argv = ["--db", str(tmp_path / "x.db"), "--base-url", "http://127.0.0.1:9", "--api-key", "k"]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


def test_cli_broken_config_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "opencode.json").write_text("{broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argv = ["--db", str(tmp_path / "x.db"), "--base-url", "http://127.0.0.1:9", "--api-key", "k", "--model", "m"]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# R3-S7 nit-2 batch: snapshot coercion routing, merge type mismatch, endpoint
# sanitization rejections (scheduler row #32).
# ---------------------------------------------------------------------------


def _v2_snapshot_dict() -> dict[str, Any]:
    """A minimal legal v2 snapshot as raw JSON (the store/service wire face)."""
    return {
        "schema_version": 2,
        "model": {"provider": "maas", "model": "fake-model"},
        "endpoint_ref": "https://api.example.test/v1",
        "roots": [],
        "permission_rules": [{"action": "fs__write", "effect": "deny"}],
        "instructions": [],
        "limits": {
            "context_limit": 100000,
            "output_reserve": 4096,
            "timeout_s": 30.0,
            "safety_margin": 0,
            "temperature": None,
            "max_steps": 20,
        },
    }


def test_coerce_config_snapshot_routes_by_schema_version() -> None:
    """nit-2(c): a JSON object routes by ``schema_version`` — 2 decodes through
    the v2 model (permission_rules carried), anything else through the frozen
    v1 model; a non-dict is rejected before either."""
    v1 = _coerce_config_snapshot({"schema_version": 1, "permission_rules": []})
    assert isinstance(v1, ConfigSnapshot)
    v2 = _coerce_config_snapshot(_v2_snapshot_dict())
    assert isinstance(v2, ConfigSnapshotV2)
    assert v2.permission_rules == [{"action": "fs__write", "effect": "deny"}]
    with pytest.raises(StoreValidationError, match="must be a ConfigSnapshot"):
        _coerce_config_snapshot(cast("Any", ["not", "a", "dict"]))
    with pytest.raises(StoreValidationError, match="invalid config_snapshot"):
        _coerce_config_snapshot({"schema_version": 3})


def test_merge_json_type_mismatch_replaces_whole() -> None:
    """nit-2(d): a scalar/map/list type mismatch resolves as "override wins" —
    the later layer replaces the earlier value whole (§6 L285)."""
    assert _merge_json({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}  # scalar <- map
    assert _merge_json({"a": {"b": 1}}, {"a": 5}) == {"a": 5}  # map <- scalar
    assert _merge_json({"a": [1, 2]}, {"a": 9}) == {"a": 9}  # list (non-append path) <- scalar
    assert _merge_json({"a": 1}, {"a": [1, 2]}) == {"a": [1, 2]}  # scalar <- list
    assert _merge_json({"a": {"b": 1}}, {"a": [1]}) == {"a": [1]}  # map <- list


def test_sanitize_endpoint_rejects_non_absolute_and_non_http() -> None:
    """nit-2(e): only an absolute http(s) URL is a legal endpoint reference;
    userinfo/query/fragment strip, and http stays http (no silent upgrade)."""
    for bad in ("ftp://example.com/v1", "example.com/v1", "//example.com", "http://", "/relative/path"):
        with pytest.raises(ConfigError, match="absolute http"):
            _sanitize_endpoint(bad)
    assert _sanitize_endpoint("http://example.com/v1") == "http://example.com/v1"
    assert _sanitize_endpoint("https://user:secret@example.com/v1?q=1#f") == "https://example.com/v1"
