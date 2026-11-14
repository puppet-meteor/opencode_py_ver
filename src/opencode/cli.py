"""Stdlib-only CLI host (R2-S8, OP-5 #92; R3-S7 approval face + closures).

The CLI is an ordinary host over the App public face (the composition root
stays untouched): it runs the alembic migration step as a subprocess, then
composes ``App`` with the MAAS adapter and the fs builtins and drives the
§7 command surface (create / prompt / cancel / run_status / messages /
reply_permission). Its only responsibilities are input and display (ROADMAP
L102); the argument face is the R3 ConfigLoader injection point, not a
config layer — since R3-S6 the ConfigLoader owns layering (CORE-CONTRACTS
§6 L283: builtin < global < project-root→cwd < CLI) and main() runs on the
resolved :class:`~opencode.config.ResolvedRuntime` (RuntimeSettings incl.
the assembled system_prompt, declared roots, and the v2 config snapshot
persisted through ``service.create``). The layer load runs twice by design:
once at parse time (model/context-limit completeness, early config errors,
and — R3-S7 — ``permission.rules`` entry shape, all on the exit-2 face) and
once at resolve time (the full runtime face; errors there land on exit 70).

R3-S7 wiring: the snapshot's validated ``permission_rules`` assemble into
the live engine at the App constructor seam via
:func:`opencode.tools.policy.permission_rules_from_snapshot` with
``source="project"`` — config-file rules tighten the defaults but never
outrank the host tier (§5.2 L266).

The approval host face (§7 L316): ``py.permission.requested`` joins the
live-preview set and prints a pending line (permission_id / action /
resources / reply guidance); the REPL commands ``/approve <id> [always]``
and ``/reject <id>`` carry the §3.1 L136 once/always/reject vocabulary; and
a parallel input pump keeps reading while a run waits, so an approval can
complete mid-run (§7 L316 independence from the run locks). Reply failures
surface as one user-readable line, never a traceback. The pending list face
is not in this slice; a single-shot approval rides the §5.2 300 s timeout
closure (both disclosed ④).

Terminal authority is ``service.run_status`` polling only (§7 L314 durable
truth); the bus subscription is a live preview, so a ``SlowConsumerError``
disconnect or any lost preview event never changes the terminal
determination or the exit code.

SIGINT routing is deterministic (R2-S8-review n-1 / P-1): a module router
delivers the signal in-band — into the active input object while the loop
is driven (prompt face → 130 through the REPL; run face → one idempotent
cancel intent, then keep waiting), or the default raise before that
(migration phase → 130). The prompt-face reader is a daemon thread that is
abandoned at interpreter exit and never joined, so ``asyncio.run``'s
default-executor join cannot hang the process on a parked ``input()``.

Console hardening (R3-S7 row #17): stdout/stderr are reconfigured to
``errors="replace"`` so model text a legacy console codec cannot encode
degrades lossily instead of crashing the run; on win32 the
CREATE_NEW_PROCESS_GROUP Ctrl+C ignore flag is cleared so hosts that spawn
this process in a new group keep a working Ctrl-C face.

Exit codes (#94): 0 succeeded (or /exit / EOF), 1 failed, 2 interrupted
(argparse usage errors exit 2 as well), 3 cancel cleanup failed, 4
session selection stopped (R4-S6: an unknown ``--session`` id, an
unreadable creation row, or recovery refusing to resume — v1 snapshot,
vanished working directory, MCP fingerprint drift), 70 migration failure
or any other uncaught exception, 130 prompt-face interrupt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import importlib.metadata
import io
import os
import signal
import subprocess
import sys
import threading
import traceback
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.bus import Event, SlowConsumerError
from opencode.config import CliOverrides, ConfigError, ConfigLoader, RecoveryStoppedError, ResolvedRuntime
from opencode.contracts.events import (
    PY_PERMISSION_REQUESTED,
    PY_REASONING_DELTA,
    PY_REASONING_FINALIZED,
    PY_TEXT_DELTA,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_STARTED,
    ConfigSnapshotV3,
    JobBudget,
    McpServerRef,
    SessionCreatedData,
    SessionCreatedDataV2,
    SessionCreatedDataV3,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantReasoningPart,
    AssistantTextPart,
    SessionMessage,
    UserMessage,
)
from opencode.mcp import McpManager, StdioServerDefinition
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.llm_call import MaasLlmCall, checkpointer_path_for
from opencode.orchestrator.registration import register_pipelines
from opencode.orchestrator.tool import run_command_tool_body
from opencode.orchestrator.workers import WORKER_DEFAULT_TIMEOUT_S, WorkerManager, workers_runs_path_for
from opencode.session.runner import DEFAULT_MAX_STEPS
from opencode.session.service import (
    ApprovalExpiredError,
    RunStatus,
    RunStatusValue,
    SessionService,
)
from opencode.storage import (
    ConflictError,
    LifecycleCleanupFailedError,
    NotFoundError,
    SessionReadOnlyError,
    StoreError,
    open_database,
)
from opencode.storage.repos import ReadRepositories, SessionCreatedRow
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

#: Terminal poll cadence for ``service.run_status`` (the durable authority).
_POLL_INTERVAL_S = 0.05
#: How long the preview printer keeps draining after the terminal status.
_DRAIN_S = 0.2
#: REPL prompt face.
_PROMPT = "opencode> "
#: /history default and clamp bounds (per-page reads stay <= 100).
_HISTORY_DEFAULT_LIMIT = 50
_HISTORY_MAX_LIMIT = 1000
_HISTORY_PAGE_SIZE = 100
#: Migration failure exit code (the #94 "not 0/1/2" family).
_MIGRATION_EXIT_CODE = 70
#: Cancel cleanup failure exit code (§7 L335 honest cleanup).
_CLEANUP_EXIT_CODE = 3
#: Session-selection stop exit code (R4-S6, §6.2 L107; #94 "not 0/1/2" family).
_SELECTION_EXIT_CODE = 4
#: Windows-required-variables minimal set (T-B-038 ⑥②): a stdio server's
#: argv is exec'd with NO inherited host environment, so the composition
#: must hand it at least these two or nearly every child binary dies at
#: startup (SystemRoot for crypto/encoding roots, PATH for finding anything).
_WIN32_MINIMAL_ENV_KEYS = ("SystemRoot", "PATH")
#: The ``py.session.created`` wire-model routing by stored version (R4-S6):
#: the repos face returns raw columns; deciding which model decodes ``data``
#: is the CLI's (the same raw-columns split as the run rows).
_SESSION_CREATED_MODELS: dict[int, type[SessionCreatedData]] = {
    1: SessionCreatedData,
    2: SessionCreatedDataV2,
    3: SessionCreatedDataV3,
}
#: Single-shot terminal status -> process exit code (#94).
_ROUND_EXIT_CODES = {
    RunStatusValue.SUCCEEDED: 0,
    RunStatusValue.FAILED: 1,
    RunStatusValue.INTERRUPTED: 2,
}
#: The seven live-preview event types the printer subscribes to (R3-S7 adds
#: the pending-approval notice).
_PREVIEW_TYPES = frozenset(
    {
        PY_TEXT_DELTA,
        PY_TEXT_FINALIZED,
        PY_REASONING_DELTA,
        PY_REASONING_FINALIZED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
        PY_PERMISSION_REQUESTED,
    }
)

# §5.3: every C0 control except \t and \n, DEL, and C1 become U+FFFD; the
# ESC replacement also dissolves CSI sequences. Deterministic, side-effect free.
_REPLACEMENT = "\ufffd"
_CONTROL_TABLE = str.maketrans(
    {chr(code): _REPLACEMENT for code in range(0x20) if chr(code) not in "\t\n"}
    | {chr(0x7F): _REPLACEMENT}
    | {chr(code): _REPLACEMENT for code in range(0x80, 0xA0)}
)

#: The input object the SIGINT router routes into while the loop is driven
#: (None before/after: the router then re-raises KeyboardInterrupt, which is
#: the pre-loop default face).
_ACTIVE_INPUT: _ReplInput | None = None


def escape_for_display(text: str) -> str:
    """Replace every display-dangerous control character with U+FFFD (§5.3)."""
    return text.translate(_CONTROL_TABLE)


def _int_at_least_one(name: str) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer, got {text!r}") from None
        if value < 1:
            raise argparse.ArgumentTypeError(f"{name} must be >= 1, got {value}")
        return value

    return parse


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"timeout-s must be a number, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"timeout-s must be > 0, got {value!r}")
    return value


def _package_version() -> str:
    """The installed distribution version (T-C1-081); '0.0.1' before any install metadata exists."""
    try:
        return importlib.metadata.version("opencode-py")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.1"


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="opencode",
        description="Single-shot prompt or interactive REPL over the MAAS provider (stdlib-only host).",
    )
    parser.add_argument("prompt", nargs="*", help="single-shot prompt words (space-joined); omit for the REPL")
    parser.add_argument("--db", default="opencode.db", help="database file path (default ./opencode.db)")
    parser.add_argument(
        "--base-url",
        default=None,
        help="MAAS base URL (flag > env OPENCODE_PY_BASE_URL > the product/global config 'endpoint' layer)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="MAAS API key (flag > env OPENCODE_PY_API_KEY > the product/global config 'api_key' layer)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="explicit global config file path (default ~/.config/opencode-py/opencode.json, §6 L283)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="project root override for config layer discovery (default: nearest VCS root, else the cwd, §6 L283)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model name (required unless a config/product layer supplies it; CLI overrides config, §6 L283)",
    )
    parser.add_argument(
        "--context-limit",
        default=None,
        type=_int_at_least_one("--context-limit"),
        help="model context window in tokens (required unless a config layer supplies it; CLI overrides config)",
    )
    parser.add_argument(
        "--output-reserve",
        type=_int_at_least_one("--output-reserve"),
        default=None,
        help="output reserve in tokens (unset falls through to config, builtin default 4096)",
    )
    parser.add_argument(
        "--timeout-s",
        type=_positive_seconds,
        default=None,
        help="request total timeout in seconds (unset falls through to config, builtin default 30)",
    )
    parser.add_argument(
        "--max-steps",
        type=_int_at_least_one("--max-steps"),
        default=None,
        help=f"runner step budget (unset falls through to config, builtin default {DEFAULT_MAX_STEPS})",
    )
    # T-C1-081 (TS parity): '--version' prints 'opencode v{V}' on stdout and
    # exits 0 — the exact TS CLI face. The argparse version action fires inside
    # parse_args, before any config discovery, so it needs no MAAS configuration
    # and leaves no database behind. The '-v' short flag is deliberately NOT
    # aliased (a later ticket owns that face).
    parser.add_argument(
        "--version",
        action="version",
        version=f"opencode v{_package_version()}",
        help="print the CLI version and exit",
    )
    # R4-S6 (§6.2 L107): the session-selection face. Mutually exclusive —
    # one resume intent per process; both given is a usage error (exit 2).
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        default=False,
        help="resume the most recent session created in this working directory (no match starts a fresh session)",
    )
    selection.add_argument(
        "--session",
        dest="session_id",
        default=None,
        metavar="ID",
        help="resume one specific session by id instead of creating a new one",
    )
    args = parser.parse_args(argv)
    args.base_url = args.base_url or os.environ.get("OPENCODE_PY_BASE_URL")
    args.api_key = args.api_key or os.environ.get("OPENCODE_PY_API_KEY")
    # §6 L283 layer-aware completeness (R3-S6): --model/--context-limit are no
    # longer argparse-required — a config layer may supply them; the CLI flag
    # wins when both are present. T-C1-067: the credentials walk the SAME layer
    # fallback — 'endpoint'/'api_key' ride the product/global tiers (the
    # product bridge reads the TS opencode gateway config) and the model
    # arrives from the product tier's models first key, so none of
    # --base-url/--api-key/--model is required once the gateway is configured.
    # Discovery runs once here so a broken config fails on the argparse exit-2
    # face; ConfigErrors after this point (the full resolve in main) land on
    # exit 70 instead. R3-S7: the load-time ``permission.rules`` entry-shape
    # validation rides the same face (必答②).
    try:
        layered = ConfigLoader(global_config=args.config, project_root=args.project_root).load()
    except ConfigError as error:
        parser.error(f"config: {error}")
    values = layered.values
    # T-C1-067 credential fallback: CLI flag > env > merged layers (the
    # product/global tiers carry 'endpoint'/'api_key'). A layer value that is
    # not a non-empty string counts as absent — fail closed, never a blind
    # assign; nothing read is ever echoed back in the error below.
    endpoint = values.get("endpoint")
    args.base_url = args.base_url or (endpoint if isinstance(endpoint, str) and endpoint else None)
    layer_api_key = values.get("api_key")
    args.api_key = args.api_key or (layer_api_key if isinstance(layer_api_key, str) and layer_api_key else None)
    if not args.base_url:
        parser.error(
            "--base-url is required (or set OPENCODE_PY_BASE_URL, or supply 'endpoint' in the product/global config)"
        )
    if not args.api_key:
        parser.error(
            "--api-key is required (or set OPENCODE_PY_API_KEY, or supply 'api_key' in the product/global config)"
        )
    if args.model is None and values.get("model") is None:
        parser.error('--model is required (or set "model" in a config file)')
    if args.context_limit is None and values.get("context_limit") is None:
        parser.error('--context-limit is required (or set "context_limit" in a config file)')
    return args


def _alembic_root() -> Path:
    """alembic.ini discovery: cwd upward first, then the package location."""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "alembic.ini").is_file():
            return candidate
    return Path(__file__).resolve().parents[2]


def _run_migrations(db_path: Path) -> int:
    """``python -m alembic upgrade head`` as a subprocess (idempotent, honest failure)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=_alembic_root(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        print(f"database migration failed for {db_path} (exit {result.returncode})", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return _MIGRATION_EXIT_CODE
    return 0


def _harden_console_streams() -> None:
    """Best-effort non-UTF-8 console survival (R3-S7 row #17).

    Model text the console codec cannot encode would otherwise crash the
    preview printer mid-run with UnicodeEncodeError; ``errors="replace"``
    degrades lossily instead. Only the error policy changes — the encoding
    itself stays the console's choice.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(errors="replace")


def _enable_windows_ctrl_c() -> None:
    """Clear the CREATE_NEW_PROCESS_GROUP Ctrl+C ignore flag (win32).

    A child spawned with CREATE_NEW_PROCESS_GROUP has Ctrl+C disabled at the
    OS level (SetConsoleCtrlHandler semantics); restoring normal processing
    is production-legitimate for any host that spawns this way (the R3-S7
    subprocess twins exercise exactly that face)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)


def _sigint_router(signum: int, frame: FrameType | None) -> None:
    """Deterministic SIGINT routing (P-1): in-band into the active input while
    the loop is driven, the default raise otherwise."""
    active = _ACTIVE_INPUT
    if active is None:
        # Pre-loop face (e.g. during the migration subprocess): the default
        # raise — main()'s KeyboardInterrupt handler prints ^C and exits 130.
        raise KeyboardInterrupt
    active.interrupt()


def _install_sigint_router() -> Callable[[int, FrameType | None], Any] | int | None:
    """Install the router; returns the previous handler for restoration."""
    try:
        previous: Callable[[int, FrameType | None], Any] | int | None = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _sigint_router)
        return previous
    except (OSError, ValueError):
        # Not the main thread (an in-process embedding) or an unsupported
        # platform: the default KeyboardInterrupt face stays in place.
        return None


def _restore_sigint(previous: Callable[[int, FrameType | None], Any] | int | None) -> None:
    if previous is None:
        previous = signal.default_int_handler
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGINT, previous)


# -- R4-S6 (§6.2 L107): session selection + recovery --------------------------


class _SelectionError(Exception):
    """Session selection refused to proceed (R4-S6): one stderr line, exit 4.

    The diagnostic face, not a failed run: unknown ``--session`` id, an
    unreadable creation row, a storage stop, or recovery refusing to resume
    (v1 snapshot / vanished working directory / MCP fingerprint drift,
    §6.1 L302). Never a traceback — the user must be able to read the one
    line and reconfigure.
    """


@dataclass(frozen=True, slots=True)
class _RecoveredSession:
    """The selection act's outcome (R4-S6): identity + the driving runtime.

    ``session_id`` is the row's id (skips ``service.create``); ``working_dir``
    is the RECORDED directory (the App sweep roots and the service working
    directory replay it — the host does not silently re-root a session);
    ``runtime`` is the recovered face (recorded snapshot as the behavioral
    baseline, §6.1 L302), never a re-read of the current config.
    """

    session_id: str
    working_dir: str
    runtime: ResolvedRuntime


def _model_created_data(row: SessionCreatedRow) -> SessionCreatedData:
    """Route one raw creation row onto its wire model by version (R4-S6).

    The routing the repos face deliberately does not do: version 1/2/3 map
    to their own models (an unknown version is a selection stop, not a
    coerce through the nearest model), and a decode failure — including the
    §6.2 L297 snapshot-hash mismatch the model validator recomputes — is one
    single-line diagnostic, never a raw pydantic traceback on the exit-4
    face.
    """
    model = _SESSION_CREATED_MODELS.get(row.version)
    if model is None:
        raise _SelectionError(
            f"session {row.session_id} has a py.session.created row of unsupported version {row.version}"
        )
    try:
        return model.model_validate(row.data)
    except ValidationError as error:
        message = str(error).splitlines()[0] if str(error) else "validation failed"
        raise _SelectionError(
            f"session {row.session_id} has an unreadable py.session.created row: {message}"
        ) from error


async def _select_session_async(
    db_path: Path,
    *,
    session_id: str | None,
    working_dir: str,
) -> SessionCreatedRow | None:
    """The selection window (R4-S6): a readonly database, read repos, closed.

    Its own small loop and its own database handle, both closed before any
    App exists (the chicken-and-egg: the App needs the recovered runtime,
    so the row that produces it must be read first). ``--session`` with no
    row is a stop (§4 未建 session 报 NotFound); ``--continue`` with no row
    for this working directory is ``None`` — the caller's fresh-session
    branch (§6.2 L107 没有则新建). Storage failures stay single-line through
    the same ``_SelectionError`` face.
    """
    try:
        async with open_database(db_path, mode="readonly") as database:
            repos = ReadRepositories(database.engine)
            if session_id is not None:
                row = await repos.session_created_by_id(session_id)
                if row is None:
                    raise _SelectionError(f"session {session_id} was not found in the database")
            else:
                row = await repos.latest_session_created_by_working_dir(working_dir)
    except StoreError as error:
        raise _SelectionError(f"session selection stopped: {error}") from error
    return row


def _select_session(
    db_path: Path,
    *,
    session_id: str | None,
    working_dir: str,
    loader: ConfigLoader,
    host_mcp_servers: Sequence[McpServerRef],
) -> _RecoveredSession | None:
    """The full selection act (R4-S6): window, decode, then recover-verify.

    Returns ``None`` only for ``--continue`` with no session ever created in
    this working directory (the fresh-session branch). Everything else is a
    :class:`_SelectionError`: unknown id, unreadable row, storage stop, or
    recovery refusing to resume. The working-directory match is the exact
    recorded string (OP-R4-3: no normalization, no case folding), and the
    drift comparison runs against the HOST's current refs — the recovered
    session must be compatible with the process about to drive it.
    """
    row = asyncio.run(_select_session_async(db_path, session_id=session_id, working_dir=working_dir))
    if row is None:
        return None
    data = _model_created_data(row)
    try:
        runtime = loader.recover(data, current_mcp_servers=list(host_mcp_servers))
    except RecoveryStoppedError as error:
        raise _SelectionError(f"cannot resume session {row.session_id}: {error}") from error
    return _RecoveredSession(session_id=row.session_id, working_dir=data.working_dir, runtime=runtime)


def _compose_mcp_manager(loader: ConfigLoader) -> McpManager | None:
    """§6 L304 first sentence (R4-S6): config entries -> manager registration.

    The host-tier ``mcp.servers`` block becomes one
    :class:`~opencode.mcp.manager.StdioServerDefinition` per entry — argv
    exec'd directly, the declared environment keys resolved AGAINST the
    boot ``os.environ`` with the config-declared value as the fallback
    (os.environ wins: a secret the operator exported overrides the file),
    plus the win32 minimal set (T-B-038 ⑥②). Registration only:
    ``add_server`` never spawns here — P2-S0 moved the spawn to session
    start (``App.start_mcp_servers``: §6 L304 first sentence, the driving
    path spawns, lists and binds) and the App owns the close.
    No entries -> no manager (``None`` = the App's no-MCP composition).
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


def main(
    argv: Sequence[str] | None = None,
    *,
    lines: Iterable[str] | None = None,
    wait_hook: Callable[[], None] | None = None,
) -> int:
    """CLI entry; ``lines``/``wait_hook`` are in-process test seams (production omits them)."""
    _harden_console_streams()
    _enable_windows_ctrl_c()
    previous_handler = _install_sigint_router()
    try:
        args = _parse_arguments(argv)
        db_path = Path(args.db).resolve()
        migration_code = _run_migrations(db_path)
        if migration_code != 0:
            return migration_code
        # R3-S6 (§6 L283): the CLI is the top config layer. The loader turns
        # the merged layers + the explicit flags into the runtime face; a
        # ConfigError here (bad values, host-tier violations deep in a layer,
        # context_limit <= output_reserve via the RuntimeSettings invariant)
        # lands on the 70 face — a traceback + exit 70 per #94, never a raw
        # escape the shell would render as the failed-run exit 1.
        provider = MaasAdapter(args.base_url, args.api_key)
        loader = ConfigLoader(global_config=args.config, project_root=args.project_root)
        # R4-S6: the host resolves its CURRENT v3 face first — it carries the
        # MCP refs the App scan and the recovered session's drift check both
        # compare against, and it stays the driving runtime for the
        # fresh-session branch. A ConfigError here still lands on the 70 face.
        host_runtime = loader.resolve_runtime_v3(
            cli=CliOverrides(
                model=args.model,
                context_limit=args.context_limit,
                output_reserve=args.output_reserve,
                timeout_s=args.timeout_s,
                max_steps=args.max_steps,
            ),
            endpoint_url=args.base_url,
        )
        if isinstance(host_runtime.snapshot, ConfigSnapshotV3):
            host_mcp_refs = list(host_runtime.snapshot.mcp_servers)
        else:  # unreachable: resolve_runtime_v3 always assembles the v3 face
            host_mcp_refs = []
        # P5-S7 (T-C1-091): workers capability closure -- the production
        # WorkerManager face behind ``opencode_agent`` nodes: a real CLI
        # subprocess per node in its three-part arena (detached worktree /
        # run dir / ``python -m opencode``, the default worker_command).
        # Its host values are this host's own -- the loader's pinned project
        # root (the worktree source repo: one discovery source, no drift),
        # the provider credentials, and the HOST runtime's model id +
        # context_limit (the manager takes the model id string, not the
        # ModelRef). The runs root is a sibling directory of the DB path
        # (workers_runs_path_for; mkdir'd here -- the base directory is the
        # composition root's guarantee, the per-worker tree beneath it
        # belongs to the manager), and the wall-clock line is the
        # WORKER_DEFAULT_TIMEOUT_S constant -- deliberately NOT
        # RuntimeSettings.timeout_s (that is the per-LLM-request deadline,
        # provider.md §3; a worker run is a whole CLI drive, not one
        # request). The cancel chain needs zero new code: the App's
        # isinstance probe wires this manager as the jobs canceller.
        # Host-only discipline: base_url/api_key/model stay in this
        # composition and never enter the config snapshot, an event
        # payload, or the graph state.
        workers_runs_root = workers_runs_path_for(db_path)
        workers_runs_root.mkdir(parents=True, exist_ok=True)
        opencode_agent = WorkerManager(
            repo_root=loader.project_root(),
            runs_root=workers_runs_root,
            default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S),
            base_url=args.base_url,
            api_key=args.api_key,
            model=host_runtime.settings.model.model,
            context_limit=host_runtime.settings.context_limit,
        )
        # P4-S1 (T-C1-082): the production pipeline engine. The real llm_call
        # body runs over this process's own MAAS provider (P5-S6, T-C1-090:
        # the tier table comes from the layered config's ``pipeline.tiers`` --
        # absent/empty folds to None and every tier falls back to the HOST
        # runtime's --model source, never the possibly-recovered session's
        # replayed snapshot), the tool body is the real run_command-backed
        # command arm (P5-S5, T-C1-089; a tool_ref node refuses honestly
        # until the registry slice), the S3 worker face is the production
        # WorkerManager wired above (P5-S7, T-C1-091: opencode_agent nodes
        # are production-reachable), and the advisory-cursor store is its own
        # sqlite file derived from the DB path (M-7: never a table in the
        # main DB). Host-only discipline: base_url/api_key/model stay in this
        # composition and never enter the config snapshot, an event payload,
        # or the graph state.
        pipeline_engine = PipelineEngine(
            checkpointer=EngineCheckpointer(checkpointer_path_for(db_path)),
            llm_call=MaasLlmCall(
                provider=provider,
                default_model=host_runtime.settings.model,
                tiers=loader.pipeline_tiers(),
            ),
            tool=run_command_tool_body,
            opencode_agent=opencode_agent,
        )
        # R4-S6 selection (§6.2 L107): --continue/--session resolve BEFORE any
        # App exists — a refusal exits without ever triggering the startup
        # recovery scan (structural: recover must precede App, so other
        # sessions' non-terminal run hygiene defers to the next successful
        # boot — ④ disclosed, not a defect). --continue with no match in this
        # working directory falls through to the fresh-session branch.
        recovery: _RecoveredSession | None = None
        if args.continue_session or args.session_id is not None:
            recovery = _select_session(
                db_path,
                session_id=args.session_id,
                working_dir=os.getcwd(),
                loader=loader,
                host_mcp_servers=host_mcp_refs,
            )
        runtime = recovery.runtime if recovery is not None else host_runtime
        mcp_manager = _compose_mcp_manager(loader)
        return asyncio.run(
            _drive(
                db_path,
                provider,
                runtime,
                prompt_text=" ".join(args.prompt),
                lines=lines,
                wait_hook=wait_hook,
                session_id=recovery.session_id if recovery is not None else None,
                working_dir=recovery.working_dir if recovery is not None else None,
                mcp_manager=mcp_manager,
                mcp_refs=host_mcp_refs,
                pipeline_engine=pipeline_engine,
                skills_dir=loader.skills_directory(),
            )
        )
    except _SelectionError as error:
        # R4-S6: the selection stop face — one stderr line, exit 4, no
        # traceback (a diagnostic the user reads and reconfigures on).
        print(f"opencode: {error}", file=sys.stderr)
        return _SELECTION_EXIT_CODE
    except KeyboardInterrupt:
        # A real SIGINT outside the driven loop: asyncio.run has already
        # cancelled and drained the scheduled tasks (their handlers commit the
        # durable terminals); the graceful close is skipped on this path.
        print("^C", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 70
    finally:
        _restore_sigint(previous_handler)


class _EndOfInput(Exception):
    """The line source is exhausted (EOF or a drained scripted iterator)."""


class _ReplInput:
    """The CLI's line source: one daemon reader thread + in-band SIGINT routing.

    Why a daemon reader (R2-S8-review n-1 fix, R3-S7 P-1(b)): parking
    ``input()`` in the default executor (``asyncio.to_thread``) leaves a
    non-daemon executor thread that ``asyncio.run`` joins on shutdown, so a
    prompt-face SIGINT hung the process forever. Here the reader is a plain
    daemon thread that every exit path abandons at interpreter exit instead
    — nothing ever joins it.

    Cross-thread state lives behind ``_lock``; the reader completes lines
    into ``_lines`` and wakes the loop through ``loop.call_soon_threadsafe``
    (the self-pipe write also breaks the loop's selector wait, which is what
    makes in-band routing work while the loop is parked). SIGINT is routed
    in-band: the router (main thread) or a reader-side KeyboardInterrupt
    only sets ``_sigint`` and wakes; ``read_line`` then raises
    KeyboardInterrupt locally inside the coroutine, so the exception never
    lands in a random main-thread frame. EOF is sticky; lines deferred by
    the mid-run pump replay from ``_pushback`` at the next prompt.
    """

    def __init__(self, lines: Iterable[str] | None, *, run_reader: bool) -> None:
        self._source = iter(lines) if lines is not None else None
        self._loop = asyncio.get_running_loop()
        self._lock = threading.Lock()
        self._lines: deque[str] = deque()
        self._pushback: deque[str] = deque()
        self._ready = asyncio.Event()
        self._eof = False
        self._sigint = False
        if run_reader:
            self._thread: threading.Thread | None = threading.Thread(
                target=self._read_lines, name="opencode-repl-reader", daemon=True
            )
            self._thread.start()
        else:
            self._thread = None

    @property
    def interactive(self) -> bool:
        """True when the reader parks on real stdin (the echoed prompt face)."""
        return self._source is None

    def _wake(self) -> None:
        def _set() -> None:
            self._ready.set()

        try:
            self._loop.call_soon_threadsafe(_set)
        except RuntimeError:
            pass  # the loop is already closed (interpreter teardown)

    def _read_lines(self) -> None:
        """Reader thread body: one line at a time, forever (daemon; never joined)."""
        while True:
            try:
                if self._source is not None:
                    line = next(self._source)
                else:
                    line = input("")
            except (StopIteration, EOFError):
                with self._lock:
                    self._eof = True
                self._wake()
                return
            except KeyboardInterrupt:
                # Only reachable through the scripted test seam (a real
                # SIGINT lands on the main thread, which routes through the
                # module router): route it in-band the same way and park.
                with self._lock:
                    self._sigint = True
                self._wake()
                return
            with self._lock:
                self._lines.append(line)
            self._wake()

    def interrupt(self) -> None:
        """SIGINT router entry (main thread): set the flag and wake the loop."""
        with self._lock:
            self._sigint = True
        self._wake()

    def route_sigint(self) -> None:
        """Re-arm a KeyboardInterrupt that ``read_line`` already consumed (pump)."""
        with self._lock:
            self._sigint = True
        self._wake()

    def consume_sigint(self) -> bool:
        """Check-and-clear the SIGINT flag (the run-wait cancel intent)."""
        with self._lock:
            if self._sigint:
                self._sigint = False
                return True
            return False

    def push_back(self, line: str) -> None:
        """Defer one non-approval line typed mid-run to the next prompt."""
        with self._lock:
            self._pushback.append(line)
        self._wake()

    async def read_line(self, *, include_pushback: bool = True) -> str:
        """Next input line; _EndOfInput on (sticky) EOF, KeyboardInterrupt on SIGINT.

        ``include_pushback=False`` is the mid-run pump face: deferred lines
        replay at the next *prompt* (the REPL's read), never back into the
        pump — re-reading its own pushback would spin the pump, and each
        spin's ``_wake`` write can fill the loop's self-pipe socket (a
        blocking ``send`` on the Windows proactor) and deadlock the process.
        """
        while True:
            with self._lock:
                if include_pushback and self._pushback:
                    return self._pushback.popleft()
                if self._lines:
                    return self._lines.popleft()
                if self._sigint:
                    self._sigint = False
                    raise KeyboardInterrupt
                if self._eof:
                    raise _EndOfInput
            await self._ready.wait()
            self._ready.clear()


async def _drive(
    db_path: Path,
    provider: MaasAdapter,
    runtime: ResolvedRuntime,
    *,
    prompt_text: str,
    lines: Iterable[str] | None,
    wait_hook: Callable[[], None] | None,
    session_id: str | None,
    working_dir: str | None,
    mcp_manager: McpManager | None,
    mcp_refs: Sequence[McpServerRef],
    pipeline_engine: PipelineEngine,
    skills_dir: Path,
) -> int:
    """One process = one migrated database + one App + one driving session.

    R3-S6: the resolved runtime face drives every injection point — the fs
    adapters receive the declared roots (working_dir always stays a root),
    App receives the loader-built RuntimeSettings (system_prompt included)
    and the snapshot's max_steps, and ``service.create`` persists the v3
    config snapshot as the session's behavioral baseline (§6.1 L302).

    R3-S7: the snapshot's permission rules also reach the live engine
    (``rules=`` on App, source="project"), and one ``_ReplInput`` owns all
    line reading + in-band SIGINT routing for both the single-shot and REPL
    faces.

    R4-S6: ``session_id`` decides the driving session — ``None`` creates a
    fresh one (``service.create``); a recovered id skips create and drives
    that session directly (the first gated write is the first prompt's run
    event, so the startup scan stays write-before-session semantics). The
    recovered runtime is the recorded snapshot replayed (fs roots, rules,
    max_steps, system_prompt all come from it, §6.1 L302); the App's
    ``working_dir`` is the RECORDED directory (never silently re-rooted);
    ``mcp_manager``/``mcp_refs`` are the host's current MCP composition —
    spawned and bound at session start (P2-S0, §6 L304) and closed by the
    App lifecycle (§7 L339).

    P5-S1 (T-C1-085): the drive's first face registers the skills
    manifest's langgraph pipelines into the engine (the manifest ->
    engine glue, on this same loop -- the checkpointer's aiosqlite
    connection is loop-bound; the report is disclosed on stderr only).
    """
    global _ACTIVE_INPUT
    # P5-S1 (T-C1-085): the production registration face -- the skills
    # manifest -> engine glue. It runs inside THIS loop's lifetime (the
    # same loop that later drives the engine below: the checkpointer's
    # aiosqlite connection is loop-bound). Disclosure policy (the MCP boot
    # precedent, ADR-0002 L60): a missing skills directory/manifest is the
    # silent fresh-project zero state; a broken manifest or a rejected
    # entry is exactly one stderr line each and the boot continues. stdout
    # stays the answer stream -- the registration face never writes it.
    report = await register_pipelines(pipeline_engine, skills_dir)
    if report.manifest_error is not None:
        print(f"opencode: skills manifest discovery failed: {report.manifest_error}", file=sys.stderr)
    for rejection in report.rejected:
        print(
            f"opencode: skill {rejection.skill_id!r} pipeline {rejection.pipeline!r} "
            f"rejected: {rejection.error_type}: {rejection.message}",
            file=sys.stderr,
        )
    # R4-S6: idempotency keys on (session_id, request_id, text), and a
    # recovered session's log already carries ids minted by earlier boots —
    # a new boot reusing a plain ``req_cli_1`` with a different text would
    # falsely collide, so every driven boot gets its own uuid namespace.
    request_base = f"req_cli_{uuid4().hex[:12]}"
    rules = permission_rules_from_snapshot(runtime.snapshot.permission_rules)
    app = App(
        db_path,
        provider=provider,
        tools=(
            FsReadAdapter(roots=runtime.roots),
            FsLsAdapter(roots=runtime.roots),
            FsGlobAdapter(roots=runtime.roots),
            FsGrepAdapter(roots=runtime.roots),
            FsWriteAdapter(roots=runtime.roots),
            FsEditAdapter(roots=runtime.roots),
            BashAdapter(),
            GitStatusAdapter(roots=runtime.roots),
            GitDiffAdapter(roots=runtime.roots),
            GitWorktreeAddAdapter(roots=runtime.roots),
            GitWorktreeListAdapter(roots=runtime.roots),
            GitWorktreeRemoveAdapter(roots=runtime.roots),
            *pty_adapters(),
        ),
        settings=runtime.settings,
        max_steps=runtime.snapshot.limits.max_steps,
        rules=rules,
        working_dir=working_dir,
        mcp_manager=mcp_manager,
        mcp_refs=mcp_refs,
        pipeline_engine=pipeline_engine,
    )
    # R4-S6: the driving act itself waits for the startup scan. The gated
    # store already orders every WRITE behind the scan, but prompt()'s
    # active-run busy check is a READ — a recovered session whose crashed run
    # the scan has not folded yet would falsely report busy. Awaiting the
    # gate first keeps the scan-before-driving order total (fail-closed: a
    # scan failure surfaces here exactly as it would at the first write).
    await app.recovery_gate.wait()
    # P2-S0 (§6 L304 first sentence): session start spawns every explicitly
    # configured stdio server, lists and binds its tools. A failed startup
    # is one stderr line per server (the manager keeps the failed marker,
    # the tool face never gains a silent empty entry) and the boot continues.
    mcp_failures = await app.start_mcp_servers()
    for server, error in mcp_failures:
        print(f"opencode: MCP server {server!r} failed to start: {error}", file=sys.stderr)
    repl_input = _ReplInput(lines, run_reader=not prompt_text)
    try:
        if session_id is None:
            session = await app.service.create(runtime.snapshot)
            session_id = session.session_id
        _ACTIVE_INPUT = repl_input
        if prompt_text:
            status = await _run_round(
                app,
                session_id,
                prompt_text,
                request_id=f"{request_base}_1",
                wait_hook=wait_hook,
                repl_input=repl_input,
            )
            if status is None:
                return _CLEANUP_EXIT_CODE
            _print_terminal(status)
            return _ROUND_EXIT_CODES[status.status]
        return await _repl(app, session_id, repl_input=repl_input, wait_hook=wait_hook, request_base=request_base)
    finally:
        # Dropped before close: a SIGINT during the graceful close falls back
        # to the default raise (the 130 face) instead of vanishing in-band.
        _ACTIVE_INPUT = None
        await app.close()


async def _repl(
    app: App,
    session_id: str,
    *,
    repl_input: _ReplInput,
    wait_hook: Callable[[], None] | None,
    request_base: str,
) -> int:
    """The REPL face: commands first (/exit, /history, /approve, /reject),
    any other line is one round; the prompt echoes only on real stdin."""
    counter = 0
    while True:
        if repl_input.interactive:
            print(_PROMPT, end="", flush=True)
        try:
            line = await repl_input.read_line()
        except _EndOfInput:
            return 0
        except KeyboardInterrupt:
            print("^C")
            return 130
        command = line.strip()
        if not command:
            continue
        head = command.split(maxsplit=1)[0]
        if command == "/exit":
            return 0
        if head == "/history":
            await _print_history(app, session_id, command)
            continue
        if head == "/approve":
            await _reply_permission(app, command, default_reply="once")
            continue
        if head == "/reject":
            await _reply_permission(app, command, default_reply="reject")
            continue
        counter += 1
        status = await _run_round(
            app,
            session_id,
            command,
            request_id=f"{request_base}_{counter}",
            wait_hook=wait_hook,
            repl_input=repl_input,
        )
        if status is None:
            return _CLEANUP_EXIT_CODE
        _print_terminal(status)


async def _reply_permission(app: App, command: str, *, default_reply: str) -> None:
    """Dispatch one /approve//reject command onto the host approval channel (§7 L316).

    ``/approve <id> [always]`` defaults to the once reply; ``/reject <id>``
    carries the reject vocabulary (§3.1 L136). reply_permission failures
    surface as one user-readable line (unknown / expired / already-resolved
    ids, read-only sessions) — never a traceback.
    """
    parts = command.split()
    reply = default_reply
    if default_reply == "once" and len(parts) >= 3 and parts[2] == "always":
        reply = "always"
    if len(parts) < 2 or not parts[1]:
        usage = "/approve <id> [always]" if default_reply == "once" else "/reject <id>"
        print(f"[approve] usage: {usage}")
        return
    permission_id = parts[1]
    try:
        receipt = await app.service.reply_permission(permission_id, reply)
    except (NotFoundError, ApprovalExpiredError, ConflictError, SessionReadOnlyError) as error:
        print(_permission_error_line(error))
        return
    outcome = "approved" if receipt.decision == "allow" else "rejected"
    print(f"[approve] {escape_for_display(permission_id)} {outcome}")


#: reply_permission error -> user-readable label (the /approve mapping face).
_PERMISSION_ERROR_LABELS: tuple[tuple[type[Exception], str], ...] = (
    (NotFoundError, "unknown permission id"),
    (ApprovalExpiredError, "approval expired"),
    (ConflictError, "already resolved"),
    (SessionReadOnlyError, "read-only session"),
)


def _permission_error_line(error: Exception) -> str:
    """One sanitized line for a reply_permission failure (pure mapping face)."""
    for kind, label in _PERMISSION_ERROR_LABELS:
        if isinstance(error, kind):
            return f"[approve] {label}: {escape_for_display(str(error))}"
    return f"[approve] failed: {escape_for_display(str(error))}"


async def _run_round(
    app: App,
    session_id: str,
    text: str,
    *,
    request_id: str,
    wait_hook: Callable[[], None] | None,
    repl_input: _ReplInput,
) -> RunStatus | None:
    """One run: subscribe first (no early events lost), prompt, then poll the
    durable terminal; the bus printer is preview-only, never authoritative.

    R3-S7: a parallel input pump keeps reading while the run waits, so an
    approval can complete mid-run (§7 L316 independence from the run locks);
    /approve//reject dispatch immediately, any other line defers to the next
    prompt, and a SIGINT becomes the (idempotent) cancel intent."""
    async with app.bus.subscribe(session_id, _PREVIEW_TYPES) as events:
        printer = asyncio.create_task(_print_events(events))
        pump = asyncio.create_task(_pump_input(app, repl_input))
        try:
            ref = await app.service.prompt(session_id, text, request_id)
            status = await _wait_terminal(app.service, ref.run_id, wait_hook=wait_hook, repl_input=repl_input)
            await asyncio.sleep(_DRAIN_S)
        finally:
            for task in (printer, pump):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    return status


async def _pump_input(app: App, repl_input: _ReplInput) -> None:
    """Mid-run input pump: approval commands complete immediately (§7 L316),
    other lines defer to the next prompt, EOF parks, SIGINT re-arms for the
    terminal wait's cancel intent. Deferred lines are *not* re-read here —
    they replay at the next prompt (see ``_ReplInput.read_line``)."""
    while True:
        try:
            line = await repl_input.read_line(include_pushback=False)
        except _EndOfInput:
            return
        except KeyboardInterrupt:
            # a mid-run SIGINT stops only this round's dispatch (T-B-033 open
            # point 1): nothing is lost — unread/deferred lines stay buffered
            # and replay at the next prompt.
            repl_input.route_sigint()
            return
        command = line.strip()
        if not command:
            continue
        head = command.split(maxsplit=1)[0]
        if head == "/approve":
            await _reply_permission(app, command, default_reply="once")
        elif head == "/reject":
            await _reply_permission(app, command, default_reply="reject")
        else:
            repl_input.push_back(command)


async def _wait_terminal(
    service: SessionService,
    run_id: str,
    *,
    wait_hook: Callable[[], None] | None,
    repl_input: _ReplInput,
) -> RunStatus | None:
    """Poll ``run_status`` until a terminal status; Ctrl-C maps to one cancel
    intent (idempotent on repeats) and keeps waiting (§7 L335/L337). The
    SIGINT router's in-band flag is consumed here the same way."""
    while True:
        if repl_input.consume_sigint():
            if not await _cancel_run(service, run_id):
                return None
            continue
        try:
            if wait_hook is not None:
                wait_hook()
            status = await service.run_status(run_id)
        except KeyboardInterrupt:
            if not await _cancel_run(service, run_id):
                return None
            continue
        if status.status.is_terminal:
            return status
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _cancel_run(service: SessionService, run_id: str) -> bool:
    """One cancel intent (§7 L335, idempotent); False = cleanup failed (exit 3)."""
    try:
        await service.cancel(run_id)
    except LifecycleCleanupFailedError:
        print("cancel requested but session cleanup failed; exiting without a graceful close", file=sys.stderr)
        return False
    return True


async def _print_events(events: AsyncIterator[Event]) -> None:
    """Live preview printer: deltas inline, the durable finalized value on its
    own ``[final]`` line (§4 L244/L246 preview -> override, distinguishable in
    the output), tool events single-line, and one pending-approval notice per
    ``py.permission.requested`` (R3-S7: the prompt face of the approval
    channel). A SlowConsumerError disconnect is display-only: the terminal
    stays with run_status polling."""
    inline_open = False
    try:
        async for event in events:
            data = event.data
            if event.type in (PY_TEXT_DELTA, PY_REASONING_DELTA):
                sys.stdout.write(escape_for_display(data.delta))
                sys.stdout.flush()
                inline_open = True
            elif event.type in (PY_TEXT_FINALIZED, PY_REASONING_FINALIZED):
                if inline_open:
                    sys.stdout.write("\n")
                    inline_open = False
                print(f"[final] {escape_for_display(data.text)}")
            elif event.type == PY_TOOL_STARTED:
                print(f"[tool] {escape_for_display(data.execution_id)} started")
            elif event.type == PY_TOOL_FINISHED:
                print(f"[tool] {escape_for_display(data.execution_id)} {data.state}")
            elif event.type == PY_PERMISSION_REQUESTED:
                print(
                    f"[permission] {escape_for_display(data.permission_id)} {escape_for_display(data.action)} "
                    "awaiting approval: /approve <id> [always] or /reject <id>"
                )
                if data.resources:
                    print(f"[permission] resources: {escape_for_display(', '.join(data.resources))}")
    except SlowConsumerError:
        print("[bus] live preview disconnected (slow consumer); waiting for the durable terminal", file=sys.stderr)


async def _print_history(app: App, session_id: str, command: str) -> None:
    """/history [limit]: forward pagination through the union face, <= 100 per page."""
    limit = _HISTORY_DEFAULT_LIMIT
    parts = command.split()
    if len(parts) > 1:
        try:
            limit = int(parts[1])
        except ValueError:
            # P-2 (row #2): a non-numeric argument warns and keeps the default.
            print(f"[history] limit must be an integer; ignoring {escape_for_display(parts[1])!r}")
    limit = max(1, min(_HISTORY_MAX_LIMIT, limit))
    remaining = limit
    after: str | None = None
    shown = 0
    while remaining > 0:
        page = await app.service.messages(session_id, after_message_id=after, limit=min(_HISTORY_PAGE_SIZE, remaining))
        if not page.items:
            break
        for message in page.items:
            _print_message(message)
            shown += 1
        if not page.has_more:
            break
        after = page.next_after_message_id
        remaining -= len(page.items)
    if shown == 0:
        print("[history] no messages yet")


def _print_message(message: SessionMessage) -> None:
    """One history line per part (user text; assistant parts with a one-line
    tool summary; idle run outcome + error), all through escape_for_display."""
    if isinstance(message, UserMessage):
        print(f"[user] {escape_for_display(message.text)}")
    elif isinstance(message, AssistantMessage):
        for part in message.parts:
            if isinstance(part, AssistantTextPart | AssistantReasoningPart):
                label = "text" if isinstance(part, AssistantTextPart) else "reasoning"
                print(f"[assistant.{label}] {escape_for_display(part.text)}")
            else:
                print(f"[assistant.tool] {escape_for_display(part.name)} {part.state}")
    else:
        suffix = f" error: {message.error.message}" if message.error is not None else ""
        print(f"[idle] {message.outcome}{escape_for_display(suffix)}")


def _print_terminal(status: RunStatus) -> None:
    line = f"[run] {status.status.value}"
    if status.error is not None:
        line += f" error: {escape_for_display(status.error)}"
    print(line)
