"""R4-S3 integration tests: the MCP catalog/policy/snapshot wiring end to end.

Every test drives ``mcp_stdio_server.py`` as a REAL subprocess (the S2
precedent: real pipes, real process trees, never an in-process stub):

- the host-tier ``mcp`` config block loads into a v3 snapshot, the snapshot
  rides ``py.session.created`` v3 through the real store (alembic-migrated
  sqlite), and recovery replays the recorded v3 face unchanged (§6 L287,
  §6.1 L302);
- a changed ``tools/list`` swaps the server's registry set atomically, the
  catalog digest moves, and an always-approval bound to the old catalog
  stops activating (§6 L304 + §5.2 L268/L270);
- two servers exposing the same tool name are rejected (§5.1 L255).

A-PLATFORM disclosure (the test_tool_bash.py / S2 OP-R3-9 precedent): only
the Windows leg ran in anger on this host. The fixture-server environment
is the S2 minimal set (MCP_FIXTURE_MODE / MCP_FIXTURE_LOG /
PYTHONUNBUFFERED) — the Windows-required-variables minimal-set ruling
(SystemRoot/PATH etc. in ``definition.env``) belongs to the S4/S5
activation wiring: this slice's loader constructs no
``StdioServerDefinition`` (env is whole-replacement manager semantics), so
the tests construct their own definitions exactly like the S2 suite.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from opencode.bus import VolatileBus
from opencode.config import ConfigLoader
from opencode.config.loader import mcp_startup_fingerprint
from opencode.contracts.events import ConfigSnapshotV3, SessionCreatedDataV3, compute_snapshot_hash
from opencode.mcp import McpManager, MCPRemoteTool, MCPToolAdapter, StdioServerDefinition
from opencode.session import SessionProjector, SessionService
from opencode.storage import ReadRepositories, SessionStore, open_database
from opencode.tools.policy import McpToolBinding, PermissionPolicy, SavedDecision
from opencode.tools.registry import RegistrationError, ToolRegistry, mcp_tool_spec

SERVER = Path(__file__).parent / "mcp_stdio_server.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The S2 minimal fixture environment (see the module docstring).
_FIXTURE_ENV_KEYS = ("MCP_FIXTURE_LOG", "MCP_FIXTURE_MODE", "PYTHONUNBUFFERED")


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (test_service.py precedent)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fixture_definition(name: str, tmp_path: Path) -> StdioServerDefinition:
    """One ``ok``-mode fixture server whose side-effect log lands in ``tmp_path``."""
    return StdioServerDefinition(
        name=name,
        command=sys.executable,
        args=(str(SERVER),),
        env={
            "MCP_FIXTURE_MODE": "ok",
            "MCP_FIXTURE_LOG": str(tmp_path / f"{name}.log.jsonl"),
            "PYTHONUNBUFFERED": "1",
        },
    )


def _register_remote_tools(
    registry: ToolRegistry, manager: McpManager, server: str, tools: Sequence[MCPRemoteTool]
) -> None:
    for tool in tools:
        registry.register_mcp(
            server,
            MCPToolAdapter(
                manager=manager,
                server=server,
                tool=tool,
                spec=mcp_tool_spec(tool.name, tool.description, tool.input_schema),
            ),
        )


async def test_v3_snapshot_loads_creates_and_recovers(tmp_path: Path) -> None:
    """Host-tier block -> v3 snapshot -> py.session.created v3 in the store -> recovery replay."""
    command = [sys.executable, str(SERVER)]
    environment = {"MCP_FIXTURE_MODE": "ok", "MCP_FIXTURE_LOG": str(tmp_path / "fixture.log.jsonl")}
    global_path = _write_json(
        tmp_path / "global.json",
        {
            "model": "m",
            "context_limit": 100000,
            "mcp": {"servers": {"fixture": {"type": "local", "command": command, "environment": environment}}},
        },
    )
    loader = ConfigLoader(working_dir=tmp_path, global_config=global_path)
    runtime = loader.resolve_runtime_v3(endpoint_url="https://api.example.test/v1")
    snapshot = runtime.snapshot
    assert isinstance(snapshot, ConfigSnapshotV3)
    (ref,) = snapshot.mcp_servers
    assert ref.server == "fixture"
    assert ref.command == command
    assert ref.env_refs == ["MCP_FIXTURE_LOG", "MCP_FIXTURE_MODE"]
    assert ref.fingerprint == mcp_startup_fingerprint("fixture", command, ["MCP_FIXTURE_LOG", "MCP_FIXTURE_MODE"])
    assert environment["MCP_FIXTURE_LOG"] not in snapshot.model_dump_json()  # env values never ride (L302)

    db_path = tmp_path / "v3.db"
    assert _upgrade_head(db_path).returncode == 0
    async with open_database(db_path, mode="create") as database:
        service = SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=VolatileBus(),
            working_dir=str(tmp_path),
        )
        created = await service.create(snapshot)
        assert created.session_id.startswith("ses_")
        await service.close()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version, data FROM events WHERE type = 'py.session.created'").fetchone()
    assert row is not None
    version, raw = row
    assert version == 3
    payload = json.loads(raw) if isinstance(raw, str | bytes) else raw
    data = SessionCreatedDataV3.model_validate(payload)
    assert data.snapshot_hash == compute_snapshot_hash(data.config_snapshot)
    assert data.config_snapshot.mcp_servers[0].fingerprint == ref.fingerprint

    recovered = ConfigLoader(working_dir=tmp_path).recover(data)
    assert isinstance(recovered.snapshot, ConfigSnapshotV3)
    assert recovered.snapshot == snapshot  # the recorded MCP refs replay unchanged (L302, no backfill)
    assert recovered.settings == runtime.settings
    assert recovered.roots == runtime.roots


async def test_tools_list_change_replaces_catalog_and_re_asks(tmp_path: Path) -> None:
    """A changed tools/list -> new catalog digest -> the old always-approval re-asks (L304/L268)."""
    definition = _fixture_definition("fixture", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    tools = await manager.start_server("fixture")
    assert {tool.name for tool in tools} == {"echo", "structured", "fail"}

    registry = ToolRegistry()
    _register_remote_tools(registry, manager, "fixture", tools)
    assert registry.mcp_server_of("echo") == "fixture"
    pinned = registry.snapshot("ses_1")
    assert pinned.resolve("mcp__fail") is not None

    fingerprint = mcp_startup_fingerprint("fixture", [sys.executable, str(SERVER)], list(_FIXTURE_ENV_KEYS))
    policy = PermissionPolicy(boot_id="boot_1")
    old_catalog = registry.mcp_catalog_version()
    policy.bind_mcp_tools(
        "mcp__echo", McpToolBinding(server="fixture", fingerprint=fingerprint, catalog_version=old_catalog)
    )
    policy.register_saved(
        SavedDecision(
            decision_id="dec_1",
            session_id="ses_1",
            action="mcp__echo",
            resources=(),
            input_hash="h0",
            boot_id="boot_1",
            expires_at=None,
        )
    )
    verdict = policy.evaluate(
        session_id="ses_1",
        action="mcp__echo",
        resources=(),
        input_hash="h1",
        working_dir=str(tmp_path),
        side_effects=True,
    )
    assert (verdict.effect, verdict.source) == ("allow", "saved_decision:dec_1")

    # The host observed a changed tools/list: "fail" is gone, echo's listing changed.
    echo = next(tool for tool in tools if tool.name == "echo")
    structured = next(tool for tool in tools if tool.name == "structured")
    registry.replace_mcp_server(
        "fixture",
        [
            MCPToolAdapter(
                manager=manager,
                server="fixture",
                tool=echo,
                spec=mcp_tool_spec(echo.name, echo.description + " (changed listing)", echo.input_schema),
            ),
            MCPToolAdapter(
                manager=manager,
                server="fixture",
                tool=structured,
                spec=mcp_tool_spec(structured.name, structured.description, structured.input_schema),
            ),
        ],
    )
    new_catalog = registry.mcp_catalog_version()
    assert new_catalog != old_catalog  # any tools/list change moves the digest (L304)
    fresh = registry.snapshot("ses_2")
    assert fresh.resolve("mcp__fail") is None  # a dropped tool is not callable from the new catalog
    assert fresh.resolve("mcp__echo") is not None
    assert pinned.resolve("mcp__fail") is not None  # already-pinned catalogs stay immutable

    policy.bind_mcp_tools(
        "mcp__echo", McpToolBinding(server="fixture", fingerprint=fingerprint, catalog_version=new_catalog)
    )
    verdict = policy.evaluate(
        session_id="ses_1",
        action="mcp__echo",
        resources=(),
        input_hash="h1",
        working_dir=str(tmp_path),
        side_effects=True,
    )
    assert (verdict.effect, verdict.source) == ("ask", "default")  # the old approval no longer activates
    await manager.close()


async def test_two_servers_exposing_the_same_tool_name_are_rejected(tmp_path: Path) -> None:
    """Cross-server name collision on a real double subprocess run (§5.1 L255)."""
    manager = McpManager()
    manager.add_server(_fixture_definition("alpha", tmp_path))
    manager.add_server(_fixture_definition("beta", tmp_path))
    alpha_tools = await manager.start_server("alpha")
    beta_tools = await manager.start_server("beta")

    registry = ToolRegistry()
    _register_remote_tools(registry, manager, "alpha", alpha_tools)
    beta_echo = next(tool for tool in beta_tools if tool.name == "echo")
    with pytest.raises(RegistrationError, match="already owned by server 'alpha'"):
        _register_remote_tools(registry, manager, "beta", [beta_echo])
    await manager.close()
