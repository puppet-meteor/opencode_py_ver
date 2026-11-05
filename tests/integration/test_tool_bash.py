"""R3-S5 integration tests: ``bash__run`` and the controlled subprocess environment.

Real interpreter + real process trees over a real temporary workspace
(``tmp_path`` redirected by ``tests/integration/conftest.py``). Plan §2.5
test plan, all five branches:

1. timeout -> the process tree is reaped: a child that spawns a grandchild
   is killed at the deadline and **both generations are verified gone**
   through a process-level probe (OpenProcess + GetExitCodeProcess on
   Windows; the Windows Job Object is the chosen OP-R3-9 mechanism).
2. cancel -> tree reaped + honest effect semantics (§2.1 L35 /
   §7 L337): a cancelled started command ends ``failed``/``unknown``
   (the frozen Invoker maps adapter errors of side-effecting tools to
   unknown -- disclosed in the progress file), never a fake success.
3. output flood -> capped + no dead-lock (L277 "输出截断后仍排空管道"):
   the fast-flood face pins the marker + original size, and the
   flood-then-hang face proves the reader kept draining past the cap
   (the original byte count reaches the full payload although the process
   is killed mid-sleep).
4. deny hint fires but never blocks (L96: advisory only, not a sandbox).
5. Windows/Linux dual-platform subprocess commands (PHASE1-SLIM L94):
   every command is built per platform. A-PLATFORM disclosure: only the
   Windows leg ran in anger on this host; the Unix (bash/killpg) leg is
   implemented and honest-skips without bash -- no faked Linux evidence.

Plus the approval chain through the full App composition (once approves
and executes; reject denies without execution; the approval binds the
full command/cwd pair, §5.2 L268), the explicit-interpreter diagnostic
(discovery injection, the rg availability-test precedent), the
120 s/0-disables timeout semantics, and the missing-cwd/empty faces.

④ disclosures: the interpreter-unavailable, empty-command and
missing-cwd diagnostics land after the Invoker committed
``py.tool.started``, so the frozen Invoker maps them to
``failed``/``unknown`` (§2.1 L35: effect proven by adapter, otherwise unknown -- the
conservative legal mapping; a sharper ``not_started`` is not expressible
through the frozen Invoker surface); the Unix pid probe treats a
permission-denied probe as alive and a still-unreaped zombie as alive
(bounded retry window); no test fakes a Linux run.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, TypeGuard, cast

import pytest
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.events import (
    PY_PERMISSION_REQUESTED,
    PY_TOOL_FINISHED,
    PY_TOOL_STARTED,
    EventDraft,
)
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.process.interpreter import resolve_interpreter
from opencode.session import RunStatusValue, SessionService
from opencode.session.context import RuntimeSettings
from opencode.session.service import DecisionReceipt, IdFactory, RunStatus
from opencode.storage import NotFoundError
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import BASH_DEFAULT_TIMEOUT_S, BASH_MAX_OUTPUT_BYTES, BashAdapter

if _PLATFORM == "win32":
    import ctypes
    from ctypes import wintypes

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
_SESSION_ID = "ses_itbash"
_RUN_ID = "run_itbash"
_MESSAGE_ID = "msg_itbash"
_TOOL_CALL_ID = "call_itbash"
_FIXED_TIME = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ToolCallStartChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    UsageChunk,
    FinishChunk,
)
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


# ---------------------------------------------------------------------------
# platform-dependent command builders (L94: Windows/Linux dual-platform legs)
# ---------------------------------------------------------------------------


def _echo_command(text: str) -> str:
    if _PLATFORM == "win32":
        return f"Write-Output '{text}'"
    return f"echo '{text}'"


def _write_file_command(name: str) -> str:
    if _PLATFORM == "win32":
        return f"Set-Content -Path {name} -Value 'hello'"
    return f"printf 'hello' > {name}"


def _flood_command(character: str, amount: int, *, hang_s: int = 0) -> str:
    if _PLATFORM == "win32":
        command = f"Write-Output ('{character}' * {amount})"
        if hang_s:
            command += f"; Start-Sleep -Seconds {hang_s}"
        return command
    # flush=True: the full payload (including print's trailing newline) must
    # reach the pipe before the hang -- a buffered trailing byte is lost when
    # the timeout kill interrupts the sleep (the first Linux CI run drained
    # 150000 of 150001 bytes).
    code = f"print('{character}' * {amount}, flush=True)"
    if hang_s:
        code += f"; import time; time.sleep({hang_s})"
    return f"'{sys.executable}' -c \"{code}\""


def _interpreter_or_skip() -> None:
    interpreter, _problem = resolve_interpreter()
    if interpreter is None:
        pytest.skip("no explicit shell interpreter (bash/PowerShell) available on this platform")


# ---------------------------------------------------------------------------
# process-level probes and the two-generation tree helper (OP-R3-9 evidence)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """A process-level probe: True when ``pid`` names a still-running process."""
    if _PLATFORM == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but is not ours to signal
    return True


def _write_tree_child(directory: Path) -> Path:
    """A helper script: writes its pid, optionally spawns a grandchild, sleeps."""
    script = directory / "tree_child.py"
    script.write_text(
        "import os, subprocess, sys, time\n"
        "pid_file, mode = sys.argv[1], sys.argv[2]\n"
        "with open(pid_file, 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        "if mode == 'parent':\n"
        "    subprocess.Popen([sys.executable, sys.argv[0], sys.argv[3], 'child'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


def _tree_command(script: Path, gen1_pid: Path, gen2_pid: Path) -> str:
    if _PLATFORM == "win32":
        return f"& '{sys.executable}' '{script}' '{gen1_pid}' parent '{gen2_pid}'"
    return f"'{sys.executable}' '{script}' '{gen1_pid}' parent '{gen2_pid}'"


def _wait_pid_file(path: Path, *, timeout_s: float = 15.0) -> int:
    """Poll until the tree helper has written its pid file; return the pid."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"pid file {path} never appeared (timeout {timeout_s}s)")


def _assert_pids_gone(pids: Sequence[int], *, timeout_s: float = 10.0) -> None:
    """Poll the process-level probe until every pid is confirmed terminated."""
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            time.sleep(0.05)
    assert not remaining, f"processes survived the tree kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# direct-registry harness (mechanical faces; the App tests own the approval)
# ---------------------------------------------------------------------------


def _clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    """EventSink stand-in: records every committed batch verbatim."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []
        self.drafts: list[EventDraft[BaseModel]] = []

    async def __call__(self, drafts: Sequence[EventDraft[BaseModel]]) -> None:
        self.batches.append(tuple(drafts))
        self.drafts.extend(drafts)


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast(IdFactory, _FixedIds()))
    registry.register(BashAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


async def _run(tmp_path: Path, input: JsonObject) -> ToolOutcome:
    context = _context(tmp_path)
    return await _registry().invoke(context, "bash__run", input, "exec_itbash")


def _texts(outcome: ToolOutcome) -> list[str]:
    return [part.text for part in outcome.content if isinstance(part, ToolTextContent)]


def _one_text(outcome: ToolOutcome) -> str:
    texts = _texts(outcome)
    assert len(texts) == 1
    return texts[0]


def _structured_exit(outcome: ToolOutcome) -> Any:
    assert outcome.structured_output is not None
    return cast("dict[str, Any]", outcome.structured_output).get("exit")


# ---------------------------------------------------------------------------
# branch 5 + basic semantics: explicit interpreter, exit codes, timeout=0
# ---------------------------------------------------------------------------


async def test_bash_runs_command_through_explicit_interpreter(tmp_path: Path) -> None:
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": _echo_command("hello from bash")})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    text = _one_text(outcome)
    assert "hello from bash" in text
    assert "[advisory]" not in text  # a clean command carries no deny hint
    assert _structured_exit(outcome) == 0


async def test_bash_nonzero_exit_code_is_data_not_tool_failure(tmp_path: Path) -> None:
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": "exit 3"})

    # A completed command succeeds as a tool run (TS result.ts notice family).
    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert "Exited with code 3" in _one_text(outcome)
    assert _structured_exit(outcome) == 3


async def test_bash_timeout_zero_disables_the_deadline(tmp_path: Path) -> None:
    _interpreter_or_skip()
    if _PLATFORM == "win32":
        command = "Start-Sleep -Seconds 1; Write-Output done"
    else:
        command = "sleep 1 && echo done"

    started = time.monotonic()
    outcome = await _run(tmp_path, {"command": command, "timeout": 0})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert "done" in _one_text(outcome)
    assert time.monotonic() - started >= 1.0  # the process really ran, no instant timeout


def test_bash_default_timeout_is_120_seconds() -> None:
    """TS shell.ts L24 anchor: DEFAULT_TIMEOUT_MS = 2 * 60 * 1_000 == 120 s exactly."""
    assert BASH_DEFAULT_TIMEOUT_S == 120.0
    assert BASH_MAX_OUTPUT_BYTES == 64 * 1024


# ---------------------------------------------------------------------------
# branch 1: timeout kills and reaps the process tree (two generations)
# ---------------------------------------------------------------------------


async def test_bash_timeout_kills_the_two_generation_tree(tmp_path: Path) -> None:
    _interpreter_or_skip()
    script = _write_tree_child(tmp_path)
    gen1_pid_file = tmp_path / "gen1.pid"
    gen2_pid_file = tmp_path / "gen2.pid"
    command = _tree_command(script, gen1_pid_file, gen2_pid_file)

    outcome = await _run(tmp_path, {"command": command, "timeout": 6})

    # A killed command's effects are not provable: failed/unknown (§2.1 L35).
    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_TIMEOUT
    joined = "\n".join(_texts(outcome))
    assert "Timed out before completion" in joined
    assert "Command exceeded timeout of 6 seconds" in outcome.error.message

    # Process-level tree reaping evidence (OP-R3-9): both generations gone.
    gen1 = _wait_pid_file(gen1_pid_file)
    gen2 = _wait_pid_file(gen2_pid_file)
    assert gen1 != gen2
    _assert_pids_gone((gen1, gen2))


# ---------------------------------------------------------------------------
# branch 2: cancel reaps the tree and keeps the honest unknown terminal
# ---------------------------------------------------------------------------


async def test_bash_cancel_reaps_tree_and_reports_unknown(tmp_path: Path) -> None:
    _interpreter_or_skip()
    script = _write_tree_child(tmp_path)
    gen1_pid_file = tmp_path / "gen1.pid"
    gen2_pid_file = tmp_path / "gen2.pid"
    command = _tree_command(script, gen1_pid_file, gen2_pid_file)

    context = _context(tmp_path)
    registry = _registry()
    task = asyncio.create_task(registry.invoke(context, "bash__run", {"command": command, "timeout": 0}, "exec_itbash"))
    gen1 = await asyncio.to_thread(_wait_pid_file, gen1_pid_file)
    gen2 = await asyncio.to_thread(_wait_pid_file, gen2_pid_file)

    context.cancel.set()  # the cooperative §7 L335 signal, mid-execution
    outcome = await task

    assert (outcome.state, outcome.effect_status) == ("failed", "unknown")
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.EXECUTION_UNKNOWN
    _assert_pids_gone((gen1, gen2))


# ---------------------------------------------------------------------------
# branch 3: output flood -> capped, marked, and no pipe dead-lock
# ---------------------------------------------------------------------------


async def test_bash_output_flood_truncates_with_original_size(tmp_path: Path) -> None:
    _interpreter_or_skip()
    # One 200_001-byte line (payload + newline): far over the 64 KiB cap.
    outcome = await _run(tmp_path, {"command": _flood_command("x", 200_000)})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    texts = _texts(outcome)
    joined = "\n".join(texts)
    assert "[stdout truncated: showing 65536 of 200001 bytes (limit 65536 bytes)]" in joined
    stdout_item = texts[0]
    assert len(stdout_item.encode("utf-8")) == 65536  # the cap is byte-exact
    assert "Exited with code" not in joined  # exit 0: no notice


async def test_bash_flood_then_hang_times_out_while_draining_pipes(tmp_path: Path) -> None:
    """L277: after the truncation the pipes keep draining -- the original byte
    count reaches the full payload although the process is killed mid-sleep."""
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": _flood_command("y", 150_000, hang_s=60), "timeout": 3})

    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_TIMEOUT
    joined = "\n".join(_texts(outcome))
    assert "of 150001 bytes" in joined  # the flood was drained past the cap
    assert "[stdout truncated:" in joined


# ---------------------------------------------------------------------------
# branch 4: the deny list is an advisory hint, never a block (L96)
# ---------------------------------------------------------------------------


async def test_bash_deny_hint_fires_but_does_not_block(tmp_path: Path) -> None:
    _interpreter_or_skip()
    # The string matches the mkfs pattern while the command itself is harmless:
    # exactly the "advisory, not a sandbox" semantics (§5.3 L276 last sentence).
    outcome = await _run(tmp_path, {"command": _echo_command("mentions mkfs somewhere")})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    joined = "\n".join(_texts(outcome))
    assert "mentions mkfs somewhere" in joined  # it executed
    assert "[advisory]" in joined
    assert "filesystem format" in joined


# ---------------------------------------------------------------------------
# diagnostics: interpreter availability (discovery injection precedent), cwd, empty
# ---------------------------------------------------------------------------


async def test_bash_interpreter_unavailable_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rg availability-test precedent: PATH discovery and the probe fail."""
    from types import SimpleNamespace

    import opencode.process.interpreter as interpreter_module

    monkeypatch.setattr(interpreter_module, "_INTERPRETER_CACHE", {})
    monkeypatch.setattr(interpreter_module, "shutil", SimpleNamespace(which=lambda name: None))
    monkeypatch.setattr(interpreter_module, "_probe_ok", lambda interpreter: False)

    outcome = await _run(tmp_path, {"command": _echo_command("never")})

    assert outcome.state == "failed"
    assert outcome.effect_status == "unknown"  # frozen-Invoker mapping (④ disclosure)
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_FAILED
    text = _one_text(outcome)
    assert "explicit shell interpreter" in text
    assert "no silent fallback" in text
    if _PLATFORM == "win32":
        assert "PowerShell" in text
    else:
        assert "bash" in text


async def test_bash_missing_cwd_fails_with_diagnostic(tmp_path: Path) -> None:
    _interpreter_or_skip()
    missing = tmp_path / "no-such-dir"
    outcome = await _run(tmp_path, {"command": _echo_command("hello"), "cwd": str(missing)})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_FAILED
    assert f"Working directory does not exist: {missing}" in _one_text(outcome)


async def test_bash_empty_command_is_rejected(tmp_path: Path) -> None:
    _interpreter_or_skip()
    outcome = await _run(tmp_path, {"command": ""})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.VALIDATION_ERROR
    assert "Command must not be empty" in _one_text(outcome)


# ---------------------------------------------------------------------------
# the approval chain through the full App composition (§5.2 L266/L268, §7 L316)
# ---------------------------------------------------------------------------


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


async def _await_permission_requested(db_path: Path, session_id: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        rows = _fetch_all(
            db_path,
            "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session_id, PY_PERMISSION_REQUESTED),
        )
        if rows:
            return cast("dict[str, Any]", json.loads(str(rows[0][0])))
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no py.permission.requested row appeared for {session_id}")
        await asyncio.sleep(0.01)


async def _reply_registered(service: SessionService, permission_id: str, reply: str) -> DecisionReceipt:
    """Reply once the coordinator holds the permission pending (poll-retry)."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        try:
            return await service.reply_permission(permission_id, reply)
        except NotFoundError:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"permission {permission_id} was never registered") from None
            await asyncio.sleep(0.01)


async def _await_run_status(
    service: SessionService, run_id: str, wanted: frozenset[RunStatusValue], *, timeout_s: float = 20.0
) -> RunStatus:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status in wanted:
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"run {run_id} did not reach {wanted} (stuck at {status.status})")
        await asyncio.sleep(0.01)


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending chunk indexes."""

    def __init__(self) -> None:
        self.items: list[BaseModel] = []
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            TextDeltaChunk(type="text_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta)
        )
        return self

    def call(self, tool_call_id: str, name: str, arguments: str) -> _Script:
        self.items.append(
            ToolCallStartChunk(
                type="tool_call_start",
                request_id="req_script",
                index=self._next(),
                call_index=0,
                tool_call_id=tool_call_id,
                name=name,
            )
        )
        self.items.append(
            ToolCallArgsDeltaChunk(
                type="tool_call_args_delta",
                request_id="req_script",
                index=self._next(),
                call_index=0,
                delta=arguments,
            )
        )
        self.items.append(
            ToolCallEndChunk(
                type="tool_call_end",
                request_id="req_script",
                index=self._next(),
                call_index=0,
                tool_call_id=tool_call_id,
                name=name,
                raw_arguments=arguments,
            )
        )
        return self

    def usage(self) -> _Script:
        self.items.append(
            UsageChunk(
                type="usage",
                request_id="req_script",
                index=self._next(),
                usage=Usage(input=5, output=7),
                cost_usd="0.0012",
            )
        )
        return self

    def finish(self) -> _Script:
        self.items.append(
            FinishChunk(type="finish", request_id="req_script", index=self._next(), reason=FinishReason.STOP)
        )
        return self


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request."""

    def __init__(self, scripts: Sequence[Sequence[BaseModel | _Script]]) -> None:
        self.scripts = [
            [item for entry in script for item in (entry.items if isinstance(entry, _Script) else (entry,))]
            for script in scripts
        ]
        self.requests: list[GenerationRequest] = []

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if _is_chunk(item):
                yield item

    async def close(self) -> None:
        """Required by App's ``_ProviderHandle`` protocol (test_approval.py precedent)."""


def _bash_call_script(command: str, cwd: str) -> _Script:
    arguments = json.dumps({"command": command, "cwd": cwd})
    return _Script().text("p1", "let me run").call("call_1", "bash__run", arguments).usage().finish()


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


def _bash_app(db_path: Path, provider: _ScriptedProvider, tmp_path: Path) -> App:
    return App(
        db_path,
        working_dir=str(tmp_path),
        provider=provider,
        tools=[BashAdapter()],
        settings=RuntimeSettings(model=_MODEL, context_limit=100_000, output_reserve=4096, timeout_s=30.0),
    )


async def test_bash_once_approval_executes_and_binds_full_command_cwd(tmp_path: Path) -> None:
    _interpreter_or_skip()
    db_path = tmp_path / "app.db"
    assert _upgrade_head(db_path).returncode == 0, "alembic upgrade head failed"

    command = _write_file_command("out.txt")
    provider = _ScriptedProvider([[_bash_call_script(command, str(tmp_path))], [_final_script()]])

    async with _bash_app(db_path, provider, tmp_path) as app:
        session = await app.service.create(_SNAPSHOT)
        run_ref = await app.service.prompt(session.session_id, "run it", "req_1")

        requested = await _await_permission_requested(db_path, session.session_id)
        permission_id = str(requested["permission_id"])
        # §5.2 L268: the shell approval binds the full command/cwd pair.
        assert requested["action"] == "bash__run"
        assert requested["resources"] == [command, str(tmp_path)]

        receipt = await _reply_registered(app.service, permission_id, "once")
        assert receipt.decision == "allow"

        status = await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert status.error is None
        assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "hello"

        started = _fetch_all(
            db_path,
            "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session.session_id, PY_TOOL_STARTED),
        )
        assert len(started) == 1
        assert json.loads(str(started[0][0]))["permission_decision"] == "allow"
        finished = _fetch_all(
            db_path,
            "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session.session_id, PY_TOOL_FINISHED),
        )
        assert len(finished) == 1
        data = json.loads(str(finished[0][0]))
        assert (data["state"], data["effect_status"]) == ("succeeded", "completed")


async def test_bash_reject_denies_without_execution(tmp_path: Path) -> None:
    _interpreter_or_skip()
    db_path = tmp_path / "app.db"
    assert _upgrade_head(db_path).returncode == 0, "alembic upgrade head failed"

    command = _write_file_command("out.txt")
    provider = _ScriptedProvider([[_bash_call_script(command, str(tmp_path))], [_final_script()]])

    async with _bash_app(db_path, provider, tmp_path) as app:
        session = await app.service.create(_SNAPSHOT)
        run_ref = await app.service.prompt(session.session_id, "run it", "req_1")

        requested = await _await_permission_requested(db_path, session.session_id)
        receipt = await _reply_registered(app.service, str(requested["permission_id"]), "reject")
        assert receipt.decision == "deny"

        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert not (tmp_path / "out.txt").exists()  # the command never ran
        finished = _fetch_all(
            db_path,
            "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session.session_id, PY_TOOL_FINISHED),
        )
        assert len(finished) == 1
        data = json.loads(str(finished[0][0]))
        assert (data["state"], data["effect_status"]) == ("denied", "not_started")
        started = _fetch_all(
            db_path,
            "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session.session_id, PY_TOOL_STARTED),
        )
        assert started == []
