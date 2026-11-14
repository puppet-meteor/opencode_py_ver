"""Production host for the local single-user HTTP server (P2-S3 / M5-A).

``serve()`` is the CLI-main→``_drive`` chain reassembled for a server process
(isomorphic, deliberately not shared with :mod:`opencode.cli` — no common
factory is extracted from the frozen faces): env face first (token, port —
fail-closed before anything listens), the alembic migration subprocess, then
the provider/config/MCP assembly and one programmatic ``uvicorn.Server``
over :func:`opencode.httpapi.app.create_app`.

Process model (PHASE2-MODULES L104 / ROADMAP L48): ONE server process, ONE
ASGI worker, ONE ``SessionService``/task table. The ``workers``/``reload``
parameter faces are structurally absent — :func:`serve` and
:func:`_server_config` take neither, and uvicorn's defaults (one process, no
reload) are pinned by tests; a second worker/server or hot reload would split
the Phase-1 single-writer/task-ownership assumptions and is
``UnsupportedCapability`` before M1, not a configuration away.

Binding (L106/L152): ``127.0.0.1`` is fixed and not overridable (conservative
first version); the port defaults to 4096 with ``OPENCODE_HTTP_PORT``
overriding it (1..65535, anything else refuses to start). The token comes
from ``OPENCODE_HTTP_TOKEN`` and is validated before any listening — missing,
empty or whitespace-only refuses the boot with the env name in the message.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import uvicorn

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.config import ConfigLoader
from opencode.contracts.events import ConfigSnapshotV3
from opencode.httpapi.app import create_app
from opencode.mcp import McpManager, StdioServerDefinition
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.llm_call import MaasLlmCall, checkpointer_path_for
from opencode.orchestrator.tool import run_command_tool_body
from opencode.tools.builtin import (
    BashAdapter,
    FsEditAdapter,
    FsGlobAdapter,
    FsGrepAdapter,
    FsLsAdapter,
    FsReadAdapter,
    FsWriteAdapter,
    GitDiffAdapter,
    GitStatusAdapter,
    GitWorktreeAddAdapter,
    GitWorktreeListAdapter,
    GitWorktreeRemoveAdapter,
    pty_adapters,
)
from opencode.tools.policy import permission_rules_from_snapshot
from opencode.tools.registry import ToolAdapter

#: §7 L106: loopback-only binding, fixed for the first version (L152 risk
#: line — opening the host face is a later, deliberate decision).
HOST = "127.0.0.1"

#: The default port (ruling c); ``OPENCODE_HTTP_PORT`` overrides it.
DEFAULT_PORT = 4096

#: The single-user token env (validated fail-closed before listening).
TOKEN_ENV = "OPENCODE_HTTP_TOKEN"

#: The port override env (1..65535 integers only, fail-closed).
PORT_ENV = "OPENCODE_HTTP_PORT"

#: T-B-038 ⑥②: a stdio server's argv is exec'd with NO inherited host
#: environment, so the composition hands it at least these two keys or nearly
#: every child binary dies at startup (the CLI's minimal set, mirrored).
_WIN32_MINIMAL_ENV_KEYS = ("SystemRoot", "PATH")

_BASE_URL_ENV = "OPENCODE_PY_BASE_URL"
_API_KEY_ENV = "OPENCODE_PY_API_KEY"


class ServerConfigurationError(RuntimeError):
    """The server refuses to start (bad env face, failed migration, ...).

    A host-process startup failure, never a per-request wire error: the
    message is operator-facing and names the offending env variable.
    """


def _token_from_env() -> str:
    """``OPENCODE_HTTP_TOKEN`` fail-closed: missing/empty/whitespace refuses.

    The value is used verbatim (no trimming, no normalization) — the client
    must present exactly the configured token.
    """
    token = os.environ.get(TOKEN_ENV)
    if token is None or not token.strip():
        raise ServerConfigurationError(
            f"{TOKEN_ENV} must be set to a non-empty single-user token "
            "(missing, empty or whitespace-only values refuse to start)"
        )
    return token


def _port_from_env() -> int:
    """``OPENCODE_HTTP_PORT`` override of the 4096 default (1..65535)."""
    raw = os.environ.get(PORT_ENV)
    if raw is None:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ServerConfigurationError(f"{PORT_ENV} must be an integer in 1..65535, got {raw!r}") from None
    if not 1 <= port <= 65535:
        raise ServerConfigurationError(f"{PORT_ENV} must be an integer in 1..65535, got {port}")
    return port


def _maas_env() -> tuple[str, str]:
    """The provider face from the environment (the CLI's env fallback face)."""
    base_url = os.environ.get(_BASE_URL_ENV)
    api_key = os.environ.get(_API_KEY_ENV)
    if not base_url or not api_key:
        raise ServerConfigurationError(f"{_BASE_URL_ENV} and {_API_KEY_ENV} must be set for the server's provider face")
    return base_url, api_key


def _alembic_root() -> Path:
    """alembic.ini discovery: cwd upward first, then the package location."""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "alembic.ini").is_file():
            return candidate
    return Path(__file__).resolve().parents[3]


def _run_migrations(db_path: Path) -> None:
    """``python -m alembic upgrade head`` as a subprocess (the CLI's face).

    Idempotent; a failed upgrade refuses the boot with the captured stderr.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=_alembic_root(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ServerConfigurationError(f"database migration failed for {db_path} (exit {result.returncode}): {detail}")


def _compose_mcp_manager(loader: ConfigLoader) -> McpManager | None:
    """Config entries -> manager registration (the CLI's composition shape).

    The declared environment keys resolve against the boot ``os.environ``
    with the config-declared value as the fallback (os.environ wins), plus
    the win32 minimal set. Registration only — ``App.start_mcp_servers``
    spawns at session start and the App owns the close.
    """
    entries = loader.mcp_server_entries()
    if not entries:
        return None
    manager = McpManager()
    for entry in entries:
        env = {key: os.environ.get(key, value) for key, value in entry.environment.items()}
        if sys.platform == "win32":
            for key in _WIN32_MINIMAL_ENV_KEYS:
                if key not in env:
                    resolved = os.environ.get(key)
                    if resolved is not None:
                        env[key] = resolved
        manager.add_server(
            StdioServerDefinition(
                name=entry.name,
                command=entry.command[0],
                args=tuple(entry.command[1:]),
                cwd=entry.cwd,
                env=env,
            )
        )
    return manager


def _builtin_adapters(roots: Sequence[str]) -> tuple[ToolAdapter, ...]:
    """The builtin tool adapters (the CLI ``_drive`` list, same order).

    Platform gate: ``*pty_adapters()`` appends the win32-verified PTY T1
    adapter on win32 and is empty elsewhere (PHASE2-MODULES L68: an
    unverified platform is not enabled; T-B-087 ruling 2 / T-C1-073).
    """
    return (
        FsReadAdapter(roots=roots),
        FsLsAdapter(roots=roots),
        FsGlobAdapter(roots=roots),
        FsGrepAdapter(roots=roots),
        FsWriteAdapter(roots=roots),
        FsEditAdapter(roots=roots),
        BashAdapter(),
        GitStatusAdapter(roots=roots),
        GitDiffAdapter(roots=roots),
        GitWorktreeAddAdapter(roots=roots),
        GitWorktreeListAdapter(roots=roots),
        GitWorktreeRemoveAdapter(roots=roots),
        *pty_adapters(),
    )


def _server_config(api: Callable[..., Any], *, port: int) -> uvicorn.Config:
    """One programmatic uvicorn config: loopback host, one worker, no reload.

    The ``workers``/``reload`` faces are structurally absent from this
    factory's parameters — multi-worker or hot reload is not a knob this
    server exposes (PHASE2-MODULES L104).
    """
    return uvicorn.Config(api, host=HOST, port=port, lifespan="on")


def serve(db_path: str | Path = "opencode.db") -> None:
    """Run the local single-user server until shutdown (the module entry).

    ``python -m opencode.httpapi`` lands here. The env face is validated
    first (token, port, provider credentials — fail-closed before anything
    listens), then the migration subprocess runs, then the provider/config/
    MCP assembly builds one :class:`~opencode.app.App` exactly like the CLI
    composition, and one programmatic ``uvicorn.Server`` serves the FastAPI
    transport until shutdown; the ASGI lifespan closes the App (reclaiming
    the scheduled runs) on the way down. Sync like the CLI's ``main`` — the
    one coroutine run is uvicorn's own ``serve()``.
    """
    token = _token_from_env()
    port = _port_from_env()
    base_url, api_key = _maas_env()
    resolved_db = Path(db_path).resolve()
    _run_migrations(resolved_db)
    loader = ConfigLoader()
    runtime = loader.resolve_runtime_v3(endpoint_url=base_url)
    mcp_manager = _compose_mcp_manager(loader)
    if isinstance(runtime.snapshot, ConfigSnapshotV3):
        mcp_refs = list(runtime.snapshot.mcp_servers)
    else:  # unreachable: resolve_runtime_v3 always assembles the v3 face
        mcp_refs = []
    provider = MaasAdapter(base_url, api_key)
    # P4-S1 (T-C1-082): the production pipeline engine, composed here
    # isomorphically to the CLI's assembly (deliberately not a shared
    # factory -- this host chain stays a frozen face): the real llm_call
    # body over this process's own provider (tier fallback = the resolved
    # runtime's model, the --model/config source), the real run_command-
    # backed tool body (P5-S5, T-C1-089: the command arm is real, a
    # tool_ref node refuses honestly until the registry slice), and the
    # advisory-cursor store at its own sqlite file derived from the DB
    # path (M-7: never a table in the main DB). Host-only discipline:
    # base_url/api_key/model stay in this composition -- never the
    # snapshot, events or graph state.
    pipeline_engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(resolved_db)),
        llm_call=MaasLlmCall(provider=provider, default_model=runtime.settings.model),
        tool=run_command_tool_body,
    )
    # P5-S1 (T-C1-085): the registration glue's input root -- the same
    # loader face the CLI composition uses (one source, no drift). The
    # registration itself runs in the ASGI lifespan startup (app.py, the
    # startup twin of the shutdown reclaim face) on the serving loop.
    skills_dir = loader.skills_directory()
    app = App(
        resolved_db,
        provider=provider,
        tools=_builtin_adapters(runtime.roots),
        settings=runtime.settings,
        max_steps=runtime.snapshot.limits.max_steps,
        rules=permission_rules_from_snapshot(runtime.snapshot.permission_rules),
        mcp_manager=mcp_manager,
        mcp_refs=mcp_refs,
        pipeline_engine=pipeline_engine,
        skills_dir=skills_dir,
    )
    api = create_app(app, token=token)
    server = uvicorn.Server(_server_config(api, port=port))
    asyncio.run(server.serve())
