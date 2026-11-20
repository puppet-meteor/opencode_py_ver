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
from typing import Any, cast

import uvicorn

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.config import ConfigLoader
from opencode.config.product import default_db_path
from opencode.contracts.events import ConfigSnapshotV3, JobBudget
from opencode.httpapi.app import create_app
from opencode.mcp import McpManager, StdioServerDefinition
from opencode.memory import MemoryGatewayClient, memory_config_from_values
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.gates import GateBroker
from opencode.orchestrator.llm_call import MaasLlmCall, checkpointer_path_for
from opencode.orchestrator.tool import run_command_tool_body
from opencode.orchestrator.workers import WORKER_DEFAULT_TIMEOUT_S, WorkerManager, workers_runs_path_for
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
    MemoryConversationSearchAdapter,
    MemoryListScenesAdapter,
    MemoryListSpacesAdapter,
    MemoryReadSceneAdapter,
    MemorySearchAdapter,
    MemoryWriteAdapter,
    PatchApplyAdapter,
    QuestionAskAdapter,
    SubagentRunAdapter,
    TodoWriteAdapter,
    WebFetchAdapter,
    WebSearchAdapter,
    pty_adapters,
)
from opencode.tools.policy import permission_rules_from_snapshot
from opencode.tools.registry import ToolAdapter
from opencode.websearch import KV_FILE_NAME, WebSearchService

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
_TLS_CERT_ENV = "OPENCODE_PY_TLS_CERT"
_TLS_KEY_ENV = "OPENCODE_PY_TLS_KEY"
_TLS_PASSPHRASE_ENV = "OPENCODE_PY_TLS_PASSPHRASE"


def _client_tls_from_env(loader: Any) -> Any:
    """T-C3-010: the mTLS certificate for the serving host (env > layer).

    Same per-field fallback as the CLI chain minus the flags (this host
    chain is a frozen face): env first, then the config layers' upstream-
    shaped ``tls`` map. Empty cert or key leaves it unset (upstream
    Provider.tls semantics); a missing {file:...} fails fast at adapter
    construction.
    """
    from opencode.ai.maas import ClientTls

    layer_tls = loader.load().values.get("tls")
    layer_map = layer_tls if isinstance(layer_tls, dict) else {}

    def _field(env: str, layer_key: str) -> str | None:
        env_value = os.environ.get(env)
        if isinstance(env_value, str) and env_value:
            return env_value
        layer_value = layer_map.get(layer_key)
        return layer_value if isinstance(layer_value, str) and layer_value else None

    cert = _field(_TLS_CERT_ENV, "cert")
    key = _field(_TLS_KEY_ENV, "key")
    passphrase = _field(_TLS_PASSPHRASE_ENV, "passphrase")
    if cert and key:
        return ClientTls(cert=cert, key=key, passphrase=passphrase)
    return None


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


#: The workflow-tools face of the server host (T-C1-LG-001): the native
#: workflow pair (workflow__run/workflow__status) and the resident
#: BrainDriver stay off by default; "1"/"true"/"yes" (case-insensitive)
#: opts the serving host in, mirroring the CLI's ``--workflow-tools`` flag.
_WORKFLOW_TOOLS_ENV = "OPENCODE_PY_WORKFLOW_TOOLS"

#: The auto-approve face of the server host (T-C1-PT-001, parity D1): the
#: TS `opencode run --auto` parity — ask-tier defaults return allow; deny
#: faces still deny. Same truthy vocabulary as the workflow-tools env.
_AUTO_APPROVE_ENV = "OPENCODE_PY_AUTO_APPROVE"


def _truthy_env(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes")


def _workflow_tools_env() -> bool:
    return _truthy_env(_WORKFLOW_TOOLS_ENV)


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


def _builtin_adapters(
    roots: Sequence[str],
    *,
    subagent: SubagentRunAdapter | None = None,
    webfetch: WebFetchAdapter | None = None,
    websearch: WebSearchAdapter | None = None,
    memory_client: MemoryGatewayClient | None = None,
) -> tuple[ToolAdapter, ...]:
    """The builtin tool adapters (the CLI ``_drive`` list, same order).

    Platform gate: ``*pty_adapters()`` appends the win32-verified PTY T1
    adapter on win32 and is empty elsewhere (PHASE2-MODULES L68: an
    unverified platform is not enabled; T-B-087 ruling 2 / T-C1-073).

    P6-S4 (T-C1-097, toolkit A): the patch/todo/question trio rides this
    composition (and the CLI ``_drive`` twin) beside the twelve -- hosts
    carry 16 adapters. The trio is host-face wiring, never an App default:
    the app.py ``tools`` loop is host-param plumbing, and a tool-less App
    composition (recovery scans, minimal harnesses) must stay tool-less.

    P6-S5 (T-C1-098): the foreground subagent tool rides the same twin
    compositions (17 adapters per host face). ``serve`` passes the named
    instance it binds to the App service right after construction; a
    caller that omits it gets a fresh unbound instance, so the census
    face keeps composing the complete host set.

    P7-S3 (T-C1-102): the webfetch tool rides the same twin compositions
    (18 adapters per host face = 17 base + the platform-gated PTY
    adapter). ``serve`` passes the named instance it binds to the App
    permission policy right after construction (the redirect-hop
    assertNotDenied face); a caller that omits it gets a fresh unbound
    instance, so the census face keeps composing the complete host set.

    P7-S4 (T-C1-103): the websearch tool rides the same twin compositions
    (19 adapters per host face = 18 base + the platform-gated PTY
    adapter). ``serve`` passes the named instance it binds to a
    WebSearchService right after construction (the provider registry +
    KV selection seam); a caller that omits it gets a fresh unbound
    instance, so the census face keeps composing the complete host set.

    T-C1-PT-012 (plan §4): the read-only memory trio rides the same twin
    compositions (22 adapters per host face = 21 base + the platform-
    gated PTY adapter). The fail-soft inversion of the named-instance
    pattern: the trio takes the gateway client at CONSTRUCTION (a
    ``None`` client -- unconfigured -- is the expected headless state of
    plan §4.2, not a wiring defect), every call answered with the
    structured service-unavailable payload, never a raise (§4.3).
    """
    if subagent is None:
        subagent = SubagentRunAdapter()
    if webfetch is None:
        webfetch = WebFetchAdapter()
    if websearch is None:
        websearch = WebSearchAdapter()
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
        PatchApplyAdapter(),
        TodoWriteAdapter(),
        QuestionAskAdapter(),
        subagent,
        webfetch,
        websearch,
        # T-C1-PT-012 (plan §4): the memory trio -- construction-injected
        # client (None = unconfigured = the fail-soft face, §4.2/§4.3),
        # same order as the CLI ``_drive`` twin.
        MemorySearchAdapter(memory_client),
        MemoryConversationSearchAdapter(memory_client),
        MemoryReadSceneAdapter(memory_client),
        # T-C1-PT-013 (the U-6a enumeration pair): same twin, same face.
        MemoryListSpacesAdapter(memory_client),
        MemoryListScenesAdapter(memory_client),
        # T-C1-PT-014 (the U-5(a) Day-1 write tool): same twin, the
        # side-effect face (ask + the NOT-written outage payload).
        MemoryWriteAdapter(memory_client),
        *pty_adapters(),
    )


def _server_config(api: Callable[..., Any], *, port: int) -> uvicorn.Config:
    """One programmatic uvicorn config: loopback host, one worker, no reload.

    The ``workers``/``reload`` faces are structurally absent from this
    factory's parameters — multi-worker or hot reload is not a knob this
    server exposes (PHASE2-MODULES L104).
    """
    return uvicorn.Config(api, host=HOST, port=port, lifespan="on")


def serve(db_path: str | Path | None = None) -> None:
    """Run the local single-user server until shutdown (the module entry).

    ``db_path`` None (the new default, T-C1-PT-008 parity D4) resolves to the
    global data directory — the same ``default_db_path()`` the CLI walks —
    so the server stops dropping ``opencode.db``/``opencode-workers``/
    ``writer.lock`` into whatever directory it was started from.

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
    resolved_db = (default_db_path() if db_path is None else Path(db_path)).resolve()
    if db_path is None:
        # T-C1-PT-008: the global default's parent may not exist yet — and only
        # that default may be created; an explicit path with a missing parent
        # keeps the honest migration failure (the cli Row #15 exit-70 face).
        resolved_db.parent.mkdir(parents=True, exist_ok=True)
    _run_migrations(resolved_db)
    loader = ConfigLoader()
    runtime = loader.resolve_runtime_v3(endpoint_url=base_url)
    # T-C1-PT-006: the MAAS custom-auth-header channel. The provider base
    # URL/key stay env-only (this host chain is a frozen face); headers ride
    # the config layers instead (product bridge from the TS config's provider
    # options.headers, or the -py global config's ``headers`` map) — the
    # same host-tier discipline and validation the CLI path walks.
    headers_value = loader.load().values.get("headers")
    # The loader's layer validation already pinned the shape (string keys and
    # string values, host tiers only); this cast is pure type narrowing.
    auth_headers = cast(
        "dict[str, str] | None",
        headers_value if isinstance(headers_value, dict) and headers_value else None,
    )
    # T-C1-PT-012 (plan §4): the memory gateway client from the same host
    # tiers (the ``memory`` block) -- None when absent/incomplete, the
    # fail-soft face the trio answers with as the structured
    # service-unavailable payload.
    memory_config = memory_config_from_values(loader.load().values)
    memory_client = MemoryGatewayClient(memory_config) if memory_config is not None else None
    mcp_manager = _compose_mcp_manager(loader)
    if isinstance(runtime.snapshot, ConfigSnapshotV3):
        mcp_refs = list(runtime.snapshot.mcp_servers)
    else:  # unreachable: resolve_runtime_v3 always assembles the v3 face
        mcp_refs = []
    provider = MaasAdapter(base_url, api_key, default_headers=auth_headers, client_tls=_client_tls_from_env(loader))
    # P5-S7 (T-C1-091): workers capability closure -- the production
    # WorkerManager face behind ``opencode_agent`` nodes, composed here
    # isomorphically to the CLI's assembly (deliberately not a shared
    # factory -- this host chain stays a frozen face): a real CLI subprocess
    # per node in its three-part arena (the default worker_command), with
    # this host's own values -- the loader's pinned project root (the
    # worktree source repo: one discovery source, no drift), the provider
    # credentials, and the resolved runtime's model id + context_limit (the
    # manager takes the model id string, not the ModelRef). The runs root is
    # a sibling directory of the DB path (workers_runs_path_for; mkdir'd
    # here -- the base directory is the composition root's guarantee, the
    # per-worker tree beneath it belongs to the manager), and the
    # wall-clock line is the WORKER_DEFAULT_TIMEOUT_S constant --
    # deliberately NOT RuntimeSettings.timeout_s (that is the per-LLM-request
    # deadline, provider.md §3; a worker run is a whole CLI drive, not one
    # request). The cancel chain needs zero new code: the App's isinstance
    # probe wires this manager as the jobs canceller. Host-only discipline:
    # base_url/api_key/model stay in this composition -- never the
    # snapshot, events or graph state.
    workers_runs_root = workers_runs_path_for(resolved_db)
    workers_runs_root.mkdir(parents=True, exist_ok=True)
    opencode_agent = WorkerManager(
        repo_root=loader.project_root(),
        runs_root=workers_runs_root,
        default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S),
        base_url=base_url,
        api_key=api_key,
        model=runtime.settings.model.model,
        context_limit=runtime.settings.context_limit,
        headers=auth_headers,
    )
    # P4-S1 (T-C1-082): the production pipeline engine, composed here
    # isomorphically to the CLI's assembly (deliberately not a shared
    # factory -- this host chain stays a frozen face): the real llm_call
    # body over this process's own provider (P5-S6, T-C1-090: the tier
    # table comes from the config's ``pipeline.tiers`` -- absent/empty
    # means tier fallback = the resolved runtime's model, the
    # --model/config source), the real run_command-backed tool body
    # (P5-S5, T-C1-089: the command arm is real, a tool_ref node refuses
    # honestly until the registry slice), the S3 worker face is the
    # production WorkerManager wired above (P5-S7, T-C1-091:
    # opencode_agent nodes are production-reachable), the S5a gate face
    # is a fresh GateBroker (P5-S8, T-C1-092, gate registry re-cast:
    # human_gate nodes are production-reachable -- the reply chain
    # closes through the App's pre-seeded gate_broker pass-through),
    # the S5a subworkflow face is switched on (P5-S9, T-C1-093:
    # subworkflow restart idempotency -- the body's re-entry adoption
    # closes the disclosed leftover, and the production composition now
    # executes the closed set's full five elements),
    # and the advisory-cursor store at its own sqlite file derived from
    # the DB path (M-7: never a table in the main DB). Host-only
    # discipline: base_url/api_key/model stay in this composition --
    # never the snapshot, events or graph state.
    pipeline_engine = PipelineEngine(
        checkpointer=EngineCheckpointer(checkpointer_path_for(resolved_db)),
        llm_call=MaasLlmCall(
            provider=provider,
            default_model=runtime.settings.model,
            tiers=loader.pipeline_tiers(),
        ),
        tool=run_command_tool_body,
        opencode_agent=opencode_agent,
        gate_broker=GateBroker(),
        subworkflow=True,
    )
    # P5-S1 (T-C1-085): the registration glue's input root -- the same
    # loader face the CLI composition uses (one source, no drift). The
    # registration itself runs in the ASGI lifespan startup (app.py, the
    # startup twin of the shutdown reclaim face) on the serving loop.
    skills_dir = loader.skills_directory()
    # P6-S5 (T-C1-098): the one stateful builtin -- a named instance so
    # ``serve`` can bind the App's session service right after construction.
    subagent_run = SubagentRunAdapter()
    # P7-S3 (T-C1-102): the webfetch twin -- a named instance so ``serve``
    # can bind the App's permission policy right after construction (the
    # redirect-hop assertNotDenied face rides the same policy the Invoker
    # gates the initial URL with).
    webfetch_fetch = WebFetchAdapter()
    # P7-S4 (T-C1-103): the websearch twin -- a named instance so ``serve``
    # can bind a WebSearchService (the provider registry + KV selection
    # seam) right after construction.
    websearch_search = WebSearchAdapter()
    app = App(
        resolved_db,
        provider=provider,
        tools=_builtin_adapters(
            runtime.roots,
            subagent=subagent_run,
            webfetch=webfetch_fetch,
            websearch=websearch_search,
            memory_client=memory_client,
        ),
        settings=runtime.settings,
        max_steps=runtime.snapshot.limits.max_steps,
        rules=permission_rules_from_snapshot(runtime.snapshot.permission_rules),
        mcp_manager=mcp_manager,
        mcp_refs=mcp_refs,
        pipeline_engine=pipeline_engine,
        skills_dir=skills_dir,
        workflow_tools=_workflow_tools_env(),
        auto_approve=_truthy_env(_AUTO_APPROVE_ENV),
    )
    # P6-S5 (T-C1-098): late-bind before the server starts taking requests
    # (an unbound adapter would fail closed as a wiring defect at first
    # invoke).
    subagent_run.bind(app.service)
    # P7-S3 (T-C1-102): the policy bind is the webfetch twin of the service
    # bind above (the provider composition guarantees the policy exists).
    assert app.policy is not None
    webfetch_fetch.bind(app.policy)
    # P7-S4 (T-C1-103): the service bind -- the provider registry + KV
    # selection seam, with the KV document landing beside the App's
    # database (the data directory the writer lock already owns).
    websearch_search.bind(WebSearchService(app.database.path.parent / KV_FILE_NAME))
    api = create_app(app, token=token)
    server = uvicorn.Server(_server_config(api, port=port))
    asyncio.run(server.serve())
