"""P2-S0 production MCP path (p2-slices §2.0, OP-P2-2 path A): spawn, list, call.

Everything here drives the production CLI/App path with an explicitly
configured stdio MCP server (``--config`` with an ``mcp.servers`` block):
session start spawns the server through ``App.start_mcp_servers`` (§6 L304
first sentence), the listed remote tools are registered under the ``mcp``
namespace and policy-bound, and the model actually calls one through the
full approval roundtrip (``mcp__echo`` -> ``py.permission.requested`` ->
``/approve`` -> the tool result riding the next provider request).

The fixture server (``mcp_stdio_server.py``) always runs as a REAL
subprocess. The failure/disconnect/close legs mirror the transport-level
contracts (``test_mcp_stdio.py``) at the production seam: a server that
dies before the handshake is one stderr line with no empty tool face (the
manager's failed marker), a spawn-level OSError (a missing binary) is the
same one-line face but WITHOUT the failed marker (④: the manager does not
mark spawn errors), a mid-call crash is effect-unknown with zero
auto-retry and exactly one rebuild on the next explicit call (the
side-effect counter), and the App lifecycle close reaps the whole process
tree in the §7 L339 order (MCP before provider).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from opencode.ai.maas import MaasAdapter
from opencode.cli import main
from opencode.config.loader import mcp_startup_fingerprint
from opencode.mcp import McpManager, MCPRemoteTool, StdioServerDefinition
from opencode.tools import ToolRegistry
from opencode.tools.policy import McpToolBinding, PermissionPolicy
from opencode.tools.registry import ToolAdapter
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
)

_PLATFORM = sys.platform

if _PLATFORM == "win32":
    import ctypes
    from ctypes import wintypes

SERVER = Path(__file__).parent / "mcp_stdio_server.py"
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------------------------------------------------------------------------
# provider host + scripted rounds (test_cli_r4.py pattern)
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


def _mcp_echo_round(text: str, call_id: str) -> ScriptedResponse:
    """One scripted generation asking for the fixture's ``echo`` tool (the mcp__echo wire name)."""
    return ScriptedResponse(
        frames=[
            Frame(tool_call_start_frame(0, call_id, "mcp__echo")),
            Frame(tool_call_args_frame(0, json.dumps({"text": text}))),
            Frame(finish_frame("tool_calls")),
        ]
    )


def _argv(db_path: Path, base_url: str, *rest: str) -> list[str]:
    argv = [
        "--db",
        str(db_path),
        "--base-url",
        base_url,
        "--api-key",
        "r0-test-key",
        "--model",
        "fake-model",
        "--context-limit",
        "100000",
    ]
    argv.extend(rest)
    return argv


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _mcp_config(path: Path, server: str, command: Sequence[str], environment: dict[str, str] | None = None) -> Path:
    """One explicit ``mcp.servers`` config: the production trust act is the config file itself."""
    entry: dict[str, Any] = {"type": "local", "command": list(command)}
    if environment is not None:
        entry["environment"] = environment
    return _write_json(
        path,
        {
            "model": "fake-model",
            "context_limit": 100000,
            "mcp": {"servers": {server: entry}},
        },
    )


def _fixture_environment(log: Path, mode: str) -> dict[str, str]:
    """The fixture server's declared environment (mcp_stdio_server.py contract)."""
    return {"MCP_FIXTURE_LOG": str(log), "MCP_FIXTURE_MODE": mode, "PYTHONUNBUFFERED": "1"}


# ---------------------------------------------------------------------------
# storage polling (test_cli_r4.py pattern)
# ---------------------------------------------------------------------------


def _query_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """One read query, connect-per-call, with a short retry (transient WAL teardown on Windows)."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            with closing(sqlite3.connect(db_path)) as connection:
                return connection.execute(statement, params).fetchall()
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _event_count(db_path: Path, event_type: str) -> int:
    rows = _query_all(db_path, "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,))
    return int(rows[0][0]) if rows else 0


def _event_payloads(db_path: Path, event_type: str) -> list[dict[str, Any]]:
    """Every stored payload of one event type, decoded (undecodable rows are skipped by the pollers' fail path)."""
    rows = _query_all(db_path, "SELECT data FROM events WHERE type = ?", (event_type,))
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[0]) if isinstance(row[0], str | bytes) else None
        except ValueError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _latest_permission_id(db_path: Path) -> str | None:
    """The newest ``py.permission.requested`` permission_id, if the DB has one yet."""
    rows = _query_all(
        db_path,
        "SELECT data FROM events WHERE type = 'py.permission.requested' ORDER BY seq DESC LIMIT 1",
    )
    if not rows:
        return None
    try:
        payload = json.loads(rows[0][0]) if isinstance(rows[0][0], str | bytes) else None
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    permission_id = payload.get("permission_id")
    return permission_id if isinstance(permission_id, str) else None


# ---------------------------------------------------------------------------
# fixture-server log + process probes (test_mcp_stdio.py pattern)
# ---------------------------------------------------------------------------


def _log_events(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _event_pid(event: dict[str, Any]) -> int:
    pid = event.get("pid")
    assert isinstance(pid, int)
    return pid


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a still-running process (OpenProcess probe on win32)."""
    # Direct sys.platform: mypy narrows the win32-typed ctypes face on Linux.
    if sys.platform == "win32":
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


def _wait_pids_gone(pids: Sequence[int], *, timeout_s: float = 10.0) -> None:
    """Poll the probe until every pid is confirmed terminated (no child leak)."""
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            time.sleep(0.05)
    assert not remaining, f"processes survived the tree kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# approval pump (test_cli_r4.py _ApprovalLines pattern)
# ---------------------------------------------------------------------------


class _ApprovalScript:
    """Scripted REPL lines whose approval command targets the CLI's own pending permission.

    Stage 1 yields ``first`` (the run prompt). Stage 2 polls the CLI's
    database until a NEW ``py.permission.requested`` event beyond
    ``after_count`` is durably visible, waits the commit-before-register
    grace, then yields ``reply.format(pid=...)``. Every later call drains
    ``then`` (``{pid}`` formats against the same permission). A poll timeout
    raises StopIteration (EOF) with ``timed_out`` set, so a broken approval
    flow fails on assertions instead of hanging — the reader is a daemon
    thread, so ``pytest.fail`` from here would never surface.
    """

    _POLL_TIMEOUT_S = 20.0
    _REGISTER_GRACE_S = 0.1

    def __init__(
        self,
        db_path: Path,
        first: str,
        reply: str,
        *,
        after_count: int = 0,
        then: Sequence[str] = (),
    ) -> None:
        self._db_path = db_path
        self._first = first
        self._reply = reply
        self._after_count = after_count
        self._then: Iterator[str] = iter(then)
        self._stage = 0
        self._last_id = ""
        self.timed_out = False

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        if self._stage == 0:
            self._stage = 1
            return self._first
        if self._stage == 1:
            self._stage = 2
            deadline = time.monotonic() + self._POLL_TIMEOUT_S
            permission_id: str | None = None
            while time.monotonic() < deadline:
                if _event_count(self._db_path, "py.permission.requested") > self._after_count:
                    permission_id = _latest_permission_id(self._db_path)
                    if permission_id is not None:
                        break
                time.sleep(0.02)
            if permission_id is None:
                self.timed_out = True
                raise StopIteration
            self._last_id = permission_id
            time.sleep(self._REGISTER_GRACE_S)
            return self._reply.format(pid=permission_id)
        return next(self._then).format(pid=self._last_id)


# ---------------------------------------------------------------------------
# T-1: the production roundtrip (§2.0 句1: 显式配置的 stdio server 可 list+call).
# ---------------------------------------------------------------------------


def test_production_spawns_lists_and_calls_tool_through_approval_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-1 (§2.0 first sentence / §6 L304): one explicitly configured stdio
    server is spawned at session start, its catalog is listed into the
    registry and policy-bound to the boot fingerprint + catalog digest, and
    the model's ``mcp__echo`` call goes through the real approval roundtrip
    — the permission is requested and replied, the tool result ("echo:
    hello") rides the next provider request back to the model."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "fixture.log.jsonl"
    host = _ServerHost(
        [
            _mcp_echo_round("hello", call_id="call_echo_1"),  # the model asks for mcp__echo
            _ok_response("done"),  # the finish after the approved call answered
        ]
    )
    base_url = host.start()
    config = _mcp_config(
        tmp_path / "mcp-config.json",
        "fixture",
        [sys.executable, str(SERVER)],
        _fixture_environment(log, "ok"),
    )
    db_path = tmp_path / "cli-mcp-production.db"
    # Class-level spies: the spawn+bind wiring runs inside main().
    started: list[tuple[McpManager, str]] = []
    original_start_server = McpManager.start_server

    async def spy_start_server(manager: McpManager, name: str) -> tuple[MCPRemoteTool, ...]:
        started.append((manager, name))
        return await original_start_server(manager, name)

    registered: list[tuple[str, str]] = []
    original_register_mcp = ToolRegistry.register_mcp

    def spy_register_mcp(registry: ToolRegistry, server_id: str, adapter: ToolAdapter) -> None:
        registered.append((server_id, adapter.spec.name))
        original_register_mcp(registry, server_id, adapter)

    catalog_versions: list[str] = []
    original_catalog_version = ToolRegistry.mcp_catalog_version

    def spy_catalog_version(registry: ToolRegistry) -> str:
        value = original_catalog_version(registry)
        catalog_versions.append(value)
        return value

    bound: list[tuple[str, McpToolBinding]] = []
    original_bind_mcp_tools = PermissionPolicy.bind_mcp_tools

    def spy_bind_mcp_tools(policy: PermissionPolicy, action: str, binding: McpToolBinding) -> None:
        bound.append((action, binding))
        original_bind_mcp_tools(policy, action, binding)

    monkeypatch.setattr(McpManager, "start_server", spy_start_server)
    monkeypatch.setattr(ToolRegistry, "register_mcp", spy_register_mcp)
    monkeypatch.setattr(ToolRegistry, "mcp_catalog_version", spy_catalog_version)
    monkeypatch.setattr(PermissionPolicy, "bind_mcp_tools", spy_bind_mcp_tools)
    lines = _ApprovalScript(db_path, "call the echo tool", "/approve {pid}", then=["/exit"])
    try:
        code = main(_argv(db_path, f"{base_url}/v1", "--config", str(config)), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "the mcp__echo permission never became durably visible"
    assert code == 0
    # Spawn + list: one production spawn of the configured server, the full
    # catalog registered under the fixed namespace.
    assert len(started) == 1, "the App must spawn the configured server exactly once"
    assert started[0][1] == "fixture"
    manager = started[0][0]
    assert registered == [("fixture", "echo"), ("fixture", "structured"), ("fixture", "fail")]
    catalog = manager.tools("fixture")
    assert catalog is not None and len(catalog) == 3, "the listed catalog stays queryable on the manager"
    assert manager.closed, "the App lifecycle must have closed the manager"
    # Bind: every remote tool pinned to the boot fingerprint + the single
    # post-registration catalog digest (§5.2 L268).
    assert catalog_versions, "the App must compute the catalog digest after every registration"
    expected_fingerprint = mcp_startup_fingerprint(
        "fixture", [sys.executable, str(SERVER)], ["MCP_FIXTURE_LOG", "MCP_FIXTURE_MODE", "PYTHONUNBUFFERED"]
    )
    assert {action for action, _ in bound} == {"mcp__echo", "mcp__structured", "mcp__fail"}
    for action, binding in bound:
        assert binding.server == "fixture", action
        assert binding.fingerprint == expected_fingerprint, action
        assert binding.catalog_version == catalog_versions[0], action
    # Call: the provider saw the three remote tools in its first request,
    # and the approved result rode the last request back to the model.
    chat_requests = [request for request in host.server.requests if request.path == "/v1/chat/completions"]
    assert len(chat_requests) == 2
    first_body = chat_requests[0].body
    assert isinstance(first_body, dict)
    tools = first_body.get("tools")
    assert isinstance(tools, list)
    wire_names = {tool["function"]["name"] for tool in tools if isinstance(tool, dict)}
    assert {"mcp__echo", "mcp__structured", "mcp__fail"} <= wire_names
    assert "echo: hello" in json.dumps(chat_requests[-1].body)
    assert _event_count(db_path, "py.permission.requested") == 1
    assert _event_count(db_path, "py.permission.replied") == 1
    assert _event_count(db_path, "py.permission.saved") == 0
    assert [payload["effect_status"] for payload in _event_payloads(db_path, "py.tool.finished")] == ["completed"]
    assert _event_count(db_path, "py.session.created") == 1


# ---------------------------------------------------------------------------
# T-2/T-3: the startup-failure legs (one stderr line, no empty tool face).
# ---------------------------------------------------------------------------


def test_production_startup_failure_marks_failed_and_boot_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-2 (§2.0 leg 2): a server that dies before speaking MCP is one stderr
    line (no traceback), the manager carries its failed marker, NOTHING is
    registered or bound (no empty tool face), and the boot continues — the
    session is created and the REPL exits cleanly."""
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "spawned.marker"
    spawn_command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path(r'{marker}').write_text('spawned')",
    ]
    config = _mcp_config(tmp_path / "mcp-config.json", "probe", spawn_command)
    captured: list[McpManager] = []
    original_add_server = McpManager.add_server

    def spy_add_server(manager: McpManager, definition: StdioServerDefinition) -> None:
        captured.append(manager)
        original_add_server(manager, definition)

    registered: list[tuple[str, str]] = []
    original_register_mcp = ToolRegistry.register_mcp

    def spy_register_mcp(registry: ToolRegistry, server_id: str, adapter: ToolAdapter) -> None:
        registered.append((server_id, adapter.spec.name))
        original_register_mcp(registry, server_id, adapter)

    bound: list[tuple[str, McpToolBinding]] = []
    original_bind_mcp_tools = PermissionPolicy.bind_mcp_tools

    def spy_bind_mcp_tools(policy: PermissionPolicy, action: str, binding: McpToolBinding) -> None:
        bound.append((action, binding))
        original_bind_mcp_tools(policy, action, binding)

    monkeypatch.setattr(McpManager, "add_server", spy_add_server)
    monkeypatch.setattr(ToolRegistry, "register_mcp", spy_register_mcp)
    monkeypatch.setattr(PermissionPolicy, "bind_mcp_tools", spy_bind_mcp_tools)
    db_path = tmp_path / "cli-mcp-startup-failure.db"
    # The dead base URL is honest: a /exit-only REPL never touches the provider.
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--config", str(config)), lines=["/exit"])
    assert code == 0
    assert marker.exists(), "the session-start spawn must have run the marker command"
    captured_err = capsys.readouterr().err
    assert "MCP server 'probe' failed to start" in captured_err
    assert "Traceback" not in captured_err
    assert registered == [], "a failed server must not register any tool face"
    assert bound == [], "a failed server must not pin any policy binding"
    manager = captured[0]
    assert manager.server_error("probe") is not None
    assert manager.tools("probe") is None
    assert manager.status("probe") == "stopped"
    assert manager.closed
    assert _event_count(db_path, "py.session.created") == 1


def test_production_missing_command_startup_failure_is_one_line_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T-3 (§2.0 leg 2, spawn level): a missing binary is the same one-line
    stderr face (no traceback), but the spawn-level OSError leaves the
    manager entry WITHOUT a failed marker — ``server_error`` stays None (④:
    the manager only marks handshake/catalog failures; manager.py is frozen
    in this slice) — and the boot still continues."""
    monkeypatch.chdir(tmp_path)
    config = _mcp_config(tmp_path / "mcp-config.json", "probe", ["opencode-no-such-mcp-binary-5f0e"])
    captured: list[McpManager] = []
    original_add_server = McpManager.add_server

    def spy_add_server(manager: McpManager, definition: StdioServerDefinition) -> None:
        captured.append(manager)
        original_add_server(manager, definition)

    monkeypatch.setattr(McpManager, "add_server", spy_add_server)
    db_path = tmp_path / "cli-mcp-missing-command.db"
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--config", str(config)), lines=["/exit"])
    assert code == 0
    captured_err = capsys.readouterr().err
    failure_lines = [line for line in captured_err.splitlines() if "MCP server 'probe' failed to start" in line]
    assert len(failure_lines) == 1, f"the spawn failure is exactly one stderr line, got: {captured_err!r}"
    assert "Traceback" not in captured_err
    manager = captured[0]
    assert manager.server_error("probe") is None, "a spawn-level OSError is not the manager's failed-marker face"
    assert manager.tools("probe") is None
    assert manager.status("probe") == "stopped"
    assert manager.closed
    assert _event_count(db_path, "py.session.created") == 1


# ---------------------------------------------------------------------------
# T-4: the disconnect leg (unknown effect, zero auto-retry, one rebuild).
# ---------------------------------------------------------------------------


def test_production_disconnect_is_unknown_and_never_auto_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-4 (§6 L304 断连): the fixture (``crash_once``) dies under the first
    ``mcp__echo`` call — the effect is unknown, the call is never retried,
    and the second explicit call rebuilds the server and completes. The
    always-approval is saved once and auto-activates for the second call:
    its input differs, so the identical-retry block (§5.2 L270 — an unknown
    effect never silently replays the same call) does not apply, and the
    unchanged binding (same fingerprint, same catalog digest, §5.2 L268)
    still matches — exactly one permission request."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "fixture.log.jsonl"
    host = _ServerHost(
        [
            _mcp_echo_round("one", call_id="call_echo_1"),  # call 1: the server crashes under it
            _mcp_echo_round("two", call_id="call_echo_2"),  # call 2: the rebuild answers
            _ok_response("done"),
        ]
    )
    base_url = host.start()
    config = _mcp_config(
        tmp_path / "mcp-config.json",
        "fixture",
        [sys.executable, str(SERVER)],
        _fixture_environment(log, "crash_once"),
    )
    db_path = tmp_path / "cli-mcp-disconnect.db"
    lines = _ApprovalScript(db_path, "call the echo tool", "/approve {pid} always", then=["/exit"])
    try:
        code = main(_argv(db_path, f"{base_url}/v1", "--config", str(config)), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "the mcp__echo permission never became durably visible"
    assert code == 0
    events = _log_events(log)
    starts = [event for event in events if event.get("event") == "start"]
    calls = [event for event in events if event.get("event") == "call"]
    assert len(starts) == 2, "the crashed server is repaired only by the second explicit call (no background respawn)"
    assert len(calls) == 2, "the crashed call must never be auto-retried (the side-effect counter)"
    # The durable truth: the crashed call is unknown, the rebuilt call completed.
    assert [payload["effect_status"] for payload in _event_payloads(db_path, "py.tool.finished")] == [
        "unknown",
        "completed",
    ]
    # One ask, one always-save: the second call auto-activated the saved
    # decision (different input, binding unchanged across the rebuild) — no
    # re-ask.
    assert _event_count(db_path, "py.permission.requested") == 1
    assert _event_count(db_path, "py.permission.saved") == 1
    assert _event_count(db_path, "py.permission.replied") == 1
    chat_requests = [request for request in host.server.requests if request.path == "/v1/chat/completions"]
    assert len(chat_requests) == 3
    assert "echo: two" in json.dumps(chat_requests[-1].body), "the rebuilt call's result must reach the model"


# ---------------------------------------------------------------------------
# T-5: the close leg (tree reaped, MCP closed before the provider).
# ---------------------------------------------------------------------------


def test_production_close_reaps_process_tree_and_closes_mcp_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-5 (§7 L339): the App lifecycle close reaps the whole process tree
    (the ``ignore_eof_with_child`` fixture AND its child die) and closes the
    MCP manager BEFORE the provider — the production order pinned through
    the CLI path."""
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "fixture.log.jsonl"
    config = _mcp_config(
        tmp_path / "mcp-config.json",
        "fixture",
        [sys.executable, str(SERVER)],
        _fixture_environment(log, "ignore_eof_with_child"),
    )
    db_path = tmp_path / "cli-mcp-close.db"
    close_order: list[str] = []
    original_manager_close = McpManager.close

    async def spy_manager_close(manager: McpManager) -> None:
        close_order.append("mcp")
        await original_manager_close(manager)

    original_provider_close = MaasAdapter.close

    async def spy_provider_close(adapter: MaasAdapter) -> None:
        close_order.append("provider")
        await original_provider_close(adapter)

    monkeypatch.setattr(McpManager, "close", spy_manager_close)
    monkeypatch.setattr(MaasAdapter, "close", spy_provider_close)
    # The dead base URL is honest: a /exit-only REPL never touches the provider.
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--config", str(config)), lines=["/exit"])
    assert code == 0
    assert close_order == ["mcp", "provider"], "§7 L339: the MCP manager closes before the provider"
    # The tree-kill leg: the EOF-ignoring server AND its spawned child are both gone.
    events = _log_events(log)
    pids = [_event_pid(event) for event in events if event.get("event") in ("start", "child")]
    assert len(pids) == 2, f"expected one server start + one child event, got: {events!r}"
    _wait_pids_gone(pids)
    assert _event_count(db_path, "py.session.created") == 1
