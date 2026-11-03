"""Stdlib-only CLI host (R2-S8, OP-5 #92): single-shot prompt or REPL.

The CLI is an ordinary host over the App public face (the composition root
stays untouched): it runs the alembic migration step as a subprocess, then
composes ``App`` with the MAAS adapter and the fs.read builtin and drives the
§7 command surface (create / prompt / cancel / run_status / messages). Its
only responsibilities are input and display (ROADMAP L102; R2 has no
approval face); the argument face is the R3 ConfigLoader injection point,
not a config layer.

Terminal authority is ``service.run_status`` polling only (§7 L314 durable
truth); the bus subscription is a live preview, so a ``SlowConsumerError``
disconnect or any lost preview event never changes the terminal
determination or the exit code.

Exit codes (#94): 0 succeeded (or /exit / EOF), 1 failed, 2 interrupted
(argparse usage errors exit 2 as well), 3 cancel cleanup failed, 70
migration failure or any other uncaught exception, 130 prompt-face
interrupt.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import subprocess
import sys
import traceback
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from pathlib import Path

from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.bus import Event, SlowConsumerError
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import (
    PY_REASONING_DELTA,
    PY_REASONING_FINALIZED,
    PY_TEXT_DELTA,
    PY_TEXT_FINALIZED,
    PY_TOOL_FINISHED,
    PY_TOOL_STARTED,
)
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantReasoningPart,
    AssistantTextPart,
    SessionMessage,
    UserMessage,
)
from opencode.session.context import RuntimeSettings
from opencode.session.runner import DEFAULT_MAX_STEPS
from opencode.session.service import RunStatus, RunStatusValue, SessionService
from opencode.storage import LifecycleCleanupFailedError
from opencode.tools.builtin.read import FsReadAdapter

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
#: Single-shot terminal status -> process exit code (#94).
_ROUND_EXIT_CODES = {
    RunStatusValue.SUCCEEDED: 0,
    RunStatusValue.FAILED: 1,
    RunStatusValue.INTERRUPTED: 2,
}
#: The six live-preview event types the printer subscribes to.
_PREVIEW_TYPES = frozenset(
    {
        PY_TEXT_DELTA,
        PY_TEXT_FINALIZED,
        PY_REASONING_DELTA,
        PY_REASONING_FINALIZED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
    }
)

# §5.3 L277: every C0 control except \t and \n, DEL, and C1 become U+FFFD; the
# ESC replacement also dissolves CSI sequences. Deterministic, side-effect free.
_REPLACEMENT = "\ufffd"
_CONTROL_TABLE = str.maketrans(
    {chr(code): _REPLACEMENT for code in range(0x20) if chr(code) not in "\t\n"}
    | {chr(0x7F): _REPLACEMENT}
    | {chr(code): _REPLACEMENT for code in range(0x80, 0xA0)}
)


def escape_for_display(text: str) -> str:
    """Replace every display-dangerous control character with U+FFFD (§5.3 L277)."""
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
        raise argparse.ArgumentTypeError(f"timeout-s must be > 0, got {value}")
    return value


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="opencode",
        description="Single-shot prompt or interactive REPL over the MAAS provider (stdlib-only host).",
    )
    parser.add_argument("prompt", nargs="*", help="single-shot prompt words (space-joined); omit for the REPL")
    parser.add_argument("--db", default="opencode.db", help="database file path (default ./opencode.db)")
    parser.add_argument("--base-url", default=None, help="MAAS base URL (env OPENCODE_PY_BASE_URL fallback)")
    parser.add_argument("--api-key", default=None, help="MAAS API key (env OPENCODE_PY_API_KEY fallback)")
    parser.add_argument("--model", required=True, help="model name (provider is fixed to maas in Phase 1)")
    parser.add_argument(
        "--context-limit",
        required=True,
        type=_int_at_least_one("--context-limit"),
        help="model context window limit in tokens (host-configured, no default)",
    )
    parser.add_argument(
        "--output-reserve",
        type=_int_at_least_one("--output-reserve"),
        default=4096,
        help="output reserve in tokens (default 4096)",
    )
    parser.add_argument(
        "--timeout-s",
        type=_positive_seconds,
        default=30.0,
        help="request total timeout in seconds (default 30)",
    )
    parser.add_argument(
        "--max-steps",
        type=_int_at_least_one("--max-steps"),
        default=DEFAULT_MAX_STEPS,
        help=f"runner step budget (default {DEFAULT_MAX_STEPS})",
    )
    args = parser.parse_args(argv)
    args.base_url = args.base_url or os.environ.get("OPENCODE_PY_BASE_URL")
    args.api_key = args.api_key or os.environ.get("OPENCODE_PY_API_KEY")
    if not args.base_url:
        parser.error("--base-url is required (or set OPENCODE_PY_BASE_URL)")
    if not args.api_key:
        parser.error("--api-key is required (or set OPENCODE_PY_API_KEY)")
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


def main(
    argv: Sequence[str] | None = None,
    *,
    lines: Iterable[str] | None = None,
    wait_hook: Callable[[], None] | None = None,
) -> int:
    """CLI entry; ``lines``/``wait_hook`` are in-process test seams (production omits them)."""
    args = _parse_arguments(argv)
    db_path = Path(args.db).resolve()
    migration_code = _run_migrations(db_path)
    if migration_code != 0:
        return migration_code
    try:
        # Host-config errors (e.g. context_limit <= output_reserve, the
        # RuntimeSettings invariant) land on the 70 face too: a traceback +
        # exit 70 per #94, never a raw escape the shell would render as the
        # failed-run exit 1.
        provider = MaasAdapter(args.base_url, args.api_key)
        settings = RuntimeSettings(
            model=ModelRef(provider="maas", model=args.model),
            context_limit=args.context_limit,
            output_reserve=args.output_reserve,
            timeout_s=args.timeout_s,
        )
        return asyncio.run(
            _drive(
                db_path,
                provider,
                settings,
                args.max_steps,
                prompt_text=" ".join(args.prompt),
                lines=lines,
                wait_hook=wait_hook,
            )
        )
    except KeyboardInterrupt:
        # A real SIGINT outside the injection seams: asyncio.run has already
        # cancelled and drained the scheduled tasks (their handlers commit the
        # durable terminals); the graceful close is skipped on this path.
        print("^C", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 70


class _EndOfInput(Exception):
    """The line source is exhausted (EOF or a drained scripted iterator)."""


def _read_line(source: Iterator[str] | None) -> str:
    """One input line, off the event loop (the REPL prompt face)."""
    if source is None:
        try:
            return input(_PROMPT)
        except EOFError:
            raise _EndOfInput from None
    try:
        return next(source)
    except StopIteration:
        raise _EndOfInput from None


async def _drive(
    db_path: Path,
    provider: MaasAdapter,
    settings: RuntimeSettings,
    max_steps: int,
    *,
    prompt_text: str,
    lines: Iterable[str] | None,
    wait_hook: Callable[[], None] | None,
) -> int:
    """One process = one migrated database + one App + one fresh session."""
    app = App(db_path, provider=provider, tools=(FsReadAdapter(),), settings=settings, max_steps=max_steps)
    try:
        session = await app.service.create(ConfigSnapshot.create())
        session_id = session.session_id
        if prompt_text:
            status = await _run_round(app, session_id, prompt_text, request_id="req_cli_1", wait_hook=wait_hook)
            if status is None:
                return _CLEANUP_EXIT_CODE
            _print_terminal(status)
            return _ROUND_EXIT_CODES[status.status]
        return await _repl(app, session_id, lines=lines, wait_hook=wait_hook)
    finally:
        await app.close()


async def _repl(
    app: App,
    session_id: str,
    *,
    lines: Iterable[str] | None,
    wait_hook: Callable[[], None] | None,
) -> int:
    source = iter(lines) if lines is not None else None
    counter = 0
    while True:
        try:
            line = await asyncio.to_thread(_read_line, source)
        except _EndOfInput:
            return 0
        except KeyboardInterrupt:
            print("^C")
            return 130
        command = line.strip()
        if not command:
            continue
        if command == "/exit":
            return 0
        if command.split(maxsplit=1)[0] == "/history":
            await _print_history(app, session_id, command)
            continue
        counter += 1
        status = await _run_round(app, session_id, command, request_id=f"req_cli_{counter}", wait_hook=wait_hook)
        if status is None:
            return _CLEANUP_EXIT_CODE
        _print_terminal(status)


async def _run_round(
    app: App,
    session_id: str,
    text: str,
    *,
    request_id: str,
    wait_hook: Callable[[], None] | None,
) -> RunStatus | None:
    """One run: subscribe first (no early events lost), prompt, then poll the
    durable terminal; the bus printer is preview-only, never authoritative."""
    async with app.bus.subscribe(session_id, _PREVIEW_TYPES) as events:
        printer = asyncio.create_task(_print_events(events))
        try:
            ref = await app.service.prompt(session_id, text, request_id)
            status = await _wait_terminal(app.service, ref.run_id, wait_hook=wait_hook)
            await asyncio.sleep(_DRAIN_S)
        finally:
            printer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await printer
    return status


async def _wait_terminal(
    service: SessionService,
    run_id: str,
    *,
    wait_hook: Callable[[], None] | None,
) -> RunStatus | None:
    """Poll ``run_status`` until a terminal status; Ctrl-C maps to one cancel
    intent (idempotent on repeats) and keeps waiting (§7 L335/L337)."""
    while True:
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
    the output), tool events single-line. A SlowConsumerError disconnect is
    display-only: the terminal stays with run_status polling."""
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
    except SlowConsumerError:
        print("[bus] live preview disconnected (slow consumer); waiting for the durable terminal", file=sys.stderr)


async def _print_history(app: App, session_id: str, command: str) -> None:
    """/history [limit]: forward pagination through the union face, <= 100 per page."""
    limit = _HISTORY_DEFAULT_LIMIT
    parts = command.split()
    if len(parts) > 1:
        with contextlib.suppress(ValueError):
            limit = int(parts[1])
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
