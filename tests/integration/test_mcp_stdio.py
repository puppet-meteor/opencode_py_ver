"""R4-S2 integration tests: the MCP stdio transport/manager/bridge.

Every test drives ``mcp_stdio_server.py`` as a REAL subprocess
(PHASE1-SLIM §4 L105: "测试自有的最小 MCP server + 故障注入" + "MCP SDK
集成测在进程边界运行") -- real pipes, real process trees, real exit
codes, never an in-process stub. r4-slices §2.1 test-plan mapping:

- (a) process-boundary roundtrip initialize -> tools/list -> call_tool
  -> ``test_roundtrip_initialize_tools_call_and_close``;
- (b) timeout injection, startup + catalog
  -> ``test_startup_timeout_marks_failed_and_kills_server`` /
  ``test_catalog_timeout_marks_failed_no_empty_tools``;
- (c) mid-call exit -> unknown + stderr tail
  -> ``test_exit_during_call_unknown_with_stderr_tail``;
- (d) tree kill, both paths (close escalation + crash sweep)
  -> ``test_close_escalation_kills_tree`` / ``test_crash_sweeps_tree``;
- (e) frame overflow rejected -> ``test_frame_overflow_rejected``;
- (f) tools/list failure -> failed, no empty tool table
  -> ``test_tools_list_error_marks_failed_no_empty_tools``;

plus the §6 L304 no-auto-retry/rebuild-on-next-explicit-call semantics
(a log-file side-effect counter, L105), the bridge faces (§5.1 L253),
and the app/service close-chain hookup (§7 L339 MCP -> provider -> DB).

A-PLATFORM disclosure (the test_tool_bash.py OP-R3-9 precedent): only
the Windows leg (Job Object) ran in anger on this host; the POSIX legs
(start_new_session + killpg) run the same assertions -- 双 OS CI 复验归
调度器合并流程.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any

import pytest

from opencode.app import App
from opencode.bus import VolatileBus
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.llm import GenerationRequest, ModelRef, ProviderChunk
from opencode.contracts.messages import ToolFileContent, ToolTextContent
from opencode.contracts.tools import ToolSpec
from opencode.mcp import (
    MCPCallToolResult,
    McpConnectError,
    McpFrameTooLargeError,
    McpManager,
    MCPMediaContent,
    MCPTextContent,
    MCPToolAdapter,
    McpUnknownOutcomeError,
    StdioServerDefinition,
    StdioTransport,
    to_tool_result,
)
from opencode.session import SessionProjector, SessionService
from opencode.session.context import RuntimeSettings
from opencode.storage import ReadRepositories, SessionStore, open_database
from opencode.tools import ToolContext

if _PLATFORM == "win32":
    import ctypes
    from ctypes import wintypes

SERVER = Path(__file__).parent / "mcp_stdio_server.py"
REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = ModelRef(provider="maas", model="test-model")
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# ---------------------------------------------------------------------------
# process-level probe + async poll helpers (test_tool_bash.py precedent)
# ---------------------------------------------------------------------------


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


async def _wait_pids_gone(pids: Sequence[int], *, timeout_s: float = 10.0) -> None:
    """Poll the probe until every pid is confirmed terminated (no child leak)."""
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _pid_alive(pid)}
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"processes survived the tree kill: {sorted(remaining)}"


# ---------------------------------------------------------------------------
# fixture-server plumbing
# ---------------------------------------------------------------------------


def _fixture_definition(name: str, mode: str, tmp_path: Path, **extra_env: str) -> tuple[StdioServerDefinition, Path]:
    """One fixture server definition + its side-effect log path (L105 counter file)."""
    log = tmp_path / f"{name}.log.jsonl"
    env = {
        "MCP_FIXTURE_MODE": mode,
        "MCP_FIXTURE_LOG": str(log),
        "PYTHONUNBUFFERED": "1",
    }
    env.update(extra_env)
    definition = StdioServerDefinition(name=name, command=sys.executable, args=(str(SERVER),), env=env)
    return definition, log


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


async def _wait_for_events(log: Path, event: str, *, count: int = 1, timeout_s: float = 20.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = [item for item in _log_events(log) if item.get("event") == event]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"log never recorded {count} {event!r} event(s) within {timeout_s}s: {_log_events(log)}")


def _event_pid(event: dict[str, Any]) -> int:
    pid = event.get("pid")
    assert isinstance(pid, int)
    return pid


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


# ---------------------------------------------------------------------------
# (a) the process-boundary roundtrip
# ---------------------------------------------------------------------------


async def test_roundtrip_initialize_tools_call_and_close(tmp_path: Path) -> None:
    """(a) initialize -> tools/list -> call_tool over real pipes; close -> stopped + no leak."""
    definition, log = _fixture_definition("fixture", "ok", tmp_path)
    manager = McpManager()
    manager.add_server(definition)

    tools = await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    assert {tool.name for tool in tools} == {"echo", "structured", "fail"}
    assert manager.status("fixture") == "connected"

    echoed = await manager.call_tool("fixture", "echo", {"text": "hello"})
    assert echoed.is_error is False
    assert echoed.content == (MCPTextContent(text="echo: hello"),)

    structured = await manager.call_tool("fixture", "structured", {})
    assert structured.is_error is False
    assert structured.structured == {"answer": 42, "nested": {"ok": True}}
    assert structured.content == (MCPTextContent(text="structured ok"),)

    failed = await manager.call_tool("fixture", "fail", {})
    assert failed.is_error is True
    assert failed.content == (MCPTextContent(text="intentional fixture failure"),)

    await manager.close()
    assert manager.status("fixture") == "stopped"
    assert manager.closed
    await manager.close()  # idempotent
    await _wait_pids_gone([_event_pid(start)])


# ---------------------------------------------------------------------------
# bridge faces (§5.1 L253)
# ---------------------------------------------------------------------------


async def test_bridge_to_tool_result_faces() -> None:
    """text/structured 回填 + isError -> ToolFailed + media -> file content."""
    ok = to_tool_result(
        MCPCallToolResult(
            is_error=False,
            structured={"k": [1, 2]},
            content=(MCPTextContent(text="body text"), MCPMediaContent(data="aGk=", mime_type="text/plain")),
        ),
        server="srv",
    )
    assert ok.error is None
    assert [part.type for part in ok.content] == ["text", "file"]
    assert isinstance(ok.content[0], ToolTextContent)
    assert ok.content[0].text == "body text"
    assert isinstance(ok.content[1], ToolFileContent)
    assert ok.content[1].mime == "text/plain"
    assert ok.content[1].uri == "data:text/plain;base64,aGk="
    assert ok.structured_output == {"k": [1, 2]}
    assert ok.metadata == {"mcp_server": "srv"}

    bad = to_tool_result(
        MCPCallToolResult(is_error=True, content=(MCPTextContent(text="boom details"),)),
        server="srv",
    )
    assert bad.error is not None
    assert bad.error.code == CoreErrorCode.TOOL_FAILED
    assert bad.error.message == "boom details"
    assert bad.error.retryable is False
    assert bad.error.details == {"mcp_server": "srv"}
    # the error text still rides the content for the model (§5.1 L253)
    assert isinstance(bad.content[0], ToolTextContent)
    assert bad.content[0].text == "boom details"

    empty = to_tool_result(MCPCallToolResult(is_error=True), server="srv")
    assert empty.error is not None
    assert empty.error.message == "MCP server srv reported a tool error"


async def test_mcp_tool_adapter_executes_through_manager(tmp_path: Path) -> None:
    """The ToolAdapter face: spec injected (namespace policy is S3's), execute routes the manager."""
    definition, _log = _fixture_definition("fixture", "ok", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    remote = next(tool for tool in await manager.start_server("fixture") if tool.name == "echo")
    spec = ToolSpec(
        namespace="mcpfixture",
        name="echo",
        version=1,
        description=remote.description,
        input_schema=remote.input_schema,
        supported_content_types=["text"],
        side_effects=True,
        idempotency_support=False,
    )
    adapter = MCPToolAdapter(manager=manager, server="fixture", tool=remote, spec=spec)
    context = ToolContext(
        session_id="ses_mcp",
        run_id="run_mcp",
        message_id="msg_mcp",
        tool_call_id="call_mcp",
        working_dir=str(tmp_path),
    )
    result = await adapter.execute(context, {"text": "adapter hi"})
    assert result.error is None
    assert result.structured_output is None
    assert isinstance(result.content[0], ToolTextContent)
    assert result.content[0].text == "echo: adapter hi"
    await manager.close()


# ---------------------------------------------------------------------------
# (b) timeout injection + (f) tools/list failure
# ---------------------------------------------------------------------------


async def test_startup_timeout_marks_failed_and_kills_server(tmp_path: Path) -> None:
    """(b) startup hang -> McpConnectError + failed; the hung process is killed, not leaked."""
    definition, log = _fixture_definition("fixture", "startup_hang", tmp_path)
    manager = McpManager(startup_timeout_s=0.3)
    manager.add_server(definition)
    with pytest.raises(McpConnectError, match="failed to start"):
        await manager.start_server("fixture")
    assert manager.status("fixture") == "failed"
    assert manager.server_error("fixture") is not None
    assert manager.tools("fixture") is None
    start = (await _wait_for_events(log, "start"))[0]
    await _wait_pids_gone([_event_pid(start)])
    await manager.close()


async def test_catalog_timeout_marks_failed_no_empty_tools(tmp_path: Path) -> None:
    """(b) catalog hang -> failed (never connected-with-empty-tools)."""
    definition, _log = _fixture_definition("fixture", "catalog_hang", tmp_path)
    manager = McpManager(catalog_timeout_s=0.3)
    manager.add_server(definition)
    with pytest.raises(McpConnectError, match="failed to start"):
        await manager.start_server("fixture")
    assert manager.status("fixture") == "failed"
    assert manager.tools("fixture") is None
    await manager.close()


async def test_tools_list_error_marks_failed_no_empty_tools(tmp_path: Path) -> None:
    """(f) tools/list JSON-RPC error -> failed; the rebuild fails the same way (no empty table)."""
    definition, _log = _fixture_definition("fixture", "tools_list_error", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    with pytest.raises(McpConnectError, match="fixture catalog failure"):
        await manager.start_server("fixture")
    assert manager.status("fixture") == "failed"
    assert manager.tools("fixture") is None
    with pytest.raises(McpConnectError, match="fixture catalog failure"):
        await manager.call_tool("fixture", "echo", {"text": "x"})
    assert manager.status("fixture") == "failed"
    assert manager.tools("fixture") is None
    await manager.close()


# ---------------------------------------------------------------------------
# (c) mid-call exit + §6 L304 no-auto-retry / rebuild-on-next-call
# ---------------------------------------------------------------------------


async def test_exit_during_call_unknown_with_stderr_tail(tmp_path: Path) -> None:
    """(c) mid-call exit(3) -> unknown + exit code + stderr tail in the diagnostic; entry failed."""
    definition, log = _fixture_definition("fixture", "exit_during_call", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    with pytest.raises(McpUnknownOutcomeError) as excinfo:
        await manager.call_tool("fixture", "echo", {"text": "boom"})
    message = str(excinfo.value)
    assert "unknown" in message
    assert "exited with code 3" in message
    assert "fixture: boom during call" in message  # the stderr tail rode the diagnostic
    assert manager.status("fixture") == "failed"
    assert manager.server_error("fixture") is not None
    await _wait_pids_gone([_event_pid(start)])
    await manager.close()


async def test_no_auto_retry_rebuild_only_on_next_explicit_call(tmp_path: Path) -> None:
    """§6 L304: a lost call is never retried; the next explicit call rebuilds (L105 counter file)."""
    definition, log = _fixture_definition("fixture", "crash_once", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")

    with pytest.raises(McpUnknownOutcomeError):
        await manager.call_tool("fixture", "echo", {"text": "one"})
    await asyncio.sleep(0.4)  # any background retry would land in this window
    assert len([item for item in _log_events(log) if item.get("event") == "call"]) == 1
    assert len([item for item in _log_events(log) if item.get("event") == "start"]) == 1

    result = await manager.call_tool("fixture", "echo", {"text": "two"})
    assert result.content == (MCPTextContent(text="echo: two"),)
    assert manager.status("fixture") == "connected"
    assert len([item for item in _log_events(log) if item.get("event") == "call"]) == 2
    assert len([item for item in _log_events(log) if item.get("event") == "start"]) == 2
    await manager.close()


# ---------------------------------------------------------------------------
# (d) tree kill, both paths
# ---------------------------------------------------------------------------


async def test_close_escalation_kills_tree(tmp_path: Path) -> None:
    """(d1) close path: an EOF-ignoring server + child -> grace expires -> tree terminated (验收 A 拆面)."""
    definition, log = _fixture_definition("fixture", "ignore_eof_with_child", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    child = (await _wait_for_events(log, "child"))[0]

    await manager.close()
    await _wait_pids_gone([_event_pid(start), _event_pid(child)])
    # the graceful stage really expired before the escalation fired
    assert any(item.get("event") == "eof_ignored" for item in _log_events(log))
    assert manager.status("fixture") == "stopped"


async def test_crash_sweeps_tree(tmp_path: Path) -> None:
    """(d2) crash path: the server exits mid-session leaving a child -> teardown sweeps the tree."""
    definition, log = _fixture_definition("fixture", "crash_with_child", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    child = (await _wait_for_events(log, "child"))[0]

    with pytest.raises(McpUnknownOutcomeError, match="exited with code 3"):
        await manager.call_tool("fixture", "echo", {"text": "bye"})
    await _wait_pids_gone([_event_pid(start), _event_pid(child)])
    await manager.close()


# ---------------------------------------------------------------------------
# (e) frame cap
# ---------------------------------------------------------------------------


async def test_frame_overflow_rejected(tmp_path: Path) -> None:
    """(e) a frame over the cap tears the connection down (16 MiB semantics, injected cap)."""
    definition, log = _fixture_definition("fixture", "big_frame", tmp_path, MCP_FIXTURE_FRAME_BYTES="4096")
    transport = StdioTransport(
        command=definition.command,
        args=definition.args,
        env=definition.env,
        server="fixture",
        max_frame_bytes=2048,
    )
    await transport.start()
    try:
        result = await transport.request("initialize", {}, timeout_s=10.0)
        assert isinstance(result, dict)  # sanity: the handshake itself worked
        with pytest.raises(McpFrameTooLargeError, match="exceeded"):
            await transport.request("tools/call", {"name": "echo", "arguments": {}}, timeout_s=10.0)
    finally:
        await transport.close()
    assert transport.closed
    start = (await _wait_for_events(log, "start"))[0]
    await _wait_pids_gone([_event_pid(start)])


# ---------------------------------------------------------------------------
# the close-chain hookup (§7 L339: MCP -> provider -> DB)
# ---------------------------------------------------------------------------


async def test_service_close_chain_mcp_before_provider(tmp_path: Path) -> None:
    """The §7 L339 order pin: the MCP close runs (fully) before the provider close."""
    db_path = tmp_path / "svc.db"
    assert _upgrade_head(db_path).returncode == 0
    definition, log = _fixture_definition("fixture", "ok", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    order: list[str] = []

    async def _mcp_close() -> None:
        order.append("mcp:start")
        await manager.close()
        order.append("mcp:done")

    async def _provider_close() -> None:
        order.append("provider")

    async with open_database(db_path, mode="create") as database:
        service = SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=VolatileBus(),
            working_dir="C:/work",
            mcp_close=_mcp_close,
            provider_close=_provider_close,
        )
        await service.close()
        assert order == ["mcp:start", "mcp:done", "provider"]
        assert service.closed
        await service.close()  # idempotent: no duplicate close events
        assert order == ["mcp:start", "mcp:done", "provider"]
    await _wait_pids_gone([_event_pid(start)])


async def test_app_close_closes_mcp_manager_before_provider(tmp_path: Path) -> None:
    """App wiring: app.close() -> the service lifecycle closes the manager, then the provider."""
    db_path = tmp_path / "app.db"
    assert _upgrade_head(db_path).returncode == 0
    definition, log = _fixture_definition("fixture", "ok", tmp_path)
    manager = McpManager()
    manager.add_server(definition)
    await manager.start_server("fixture")
    start = (await _wait_for_events(log, "start"))[0]
    order: list[str] = []

    class _CloseOrderProvider:
        """Fake Provider: never streams; records its close (test_tool_bash.py precedent)."""

        async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
            raise AssertionError("no generation is requested by this test")
            yield  # pragma: no cover - async-generator shape only

        async def close(self) -> None:
            order.append("provider")

    app = App(
        db_path,
        working_dir=str(tmp_path),
        provider=_CloseOrderProvider(),
        settings=RuntimeSettings(model=_MODEL, context_limit=100_000, output_reserve=4096, timeout_s=30.0),
        mcp_manager=manager,
    )
    assert app.mcp_manager is manager
    await app.close()
    assert manager.closed
    assert order == ["provider"]  # closed after the MCP close (order pinned by the service test)
    assert app.service.closed
    await _wait_pids_gone([_event_pid(start)])
