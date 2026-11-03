"""R2-S5 integration tests: the builtin ``fs__read`` tool on a real filesystem.

Real ``FsReadAdapter`` through a real ``ToolRegistry`` (recording sink:
committing ``py.tool.*`` through the SessionStore is runner work, R2-S6).
``tmp_path`` is redirected to the approved temp root by
``tests/integration/conftest.py``. Contract anchors: plan §2 R2-S5 ④,
CORE-CONTRACTS §5.1 L253/L255/L257, §5.3 L272-276, §2.1 L46.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.events import (
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
    canonical_json,
)
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolRef, ToolResult, ToolSpec
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import MAX_LINE_SUFFIX, FsReadAdapter

_FIXED_TIME = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_itread"
_RUN_ID = "run_itread"
_MESSAGE_ID = "msg_itread"
_TOOL_CALL_ID = "call_itread"


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


def _registry_with_read(sink: _RecordingSink) -> ToolRegistry:
    registry = ToolRegistry(sink=sink, clock=_clock, ids=_FixedIds())
    registry.register(FsReadAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


def _context(working_dir: Path, *, limits: ToolLimits | None = None) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=limits if limits is not None else ToolLimits(),
    )


async def _run(working_dir: Path, input: JsonObject) -> ToolOutcome:
    """One fs__read invoke through a fresh pinned registry; outcome only."""
    registry = _registry_with_read(_RecordingSink())
    return await registry.invoke(_context(working_dir), "fs__read", input, "exec_it")


def _text(outcome: ToolOutcome) -> str:
    """The single text content of a read outcome."""
    parts = [part for part in outcome.content if isinstance(part, ToolTextContent)]
    assert len(parts) == 1
    return parts[0].text


def _input_hash(input: JsonObject) -> str:
    return sha256(canonical_json(input).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# normal reads
# ---------------------------------------------------------------------------


async def test_read_renders_numbered_lines(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes(b"alpha\nbeta\ngamma\n")

    outcome = await _run(tmp_path, {"path": "notes.txt"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.requested_name == "fs__read"
    assert _text(outcome) == "Read file notes.txt, lines 1-3\n1: alpha\n2: beta\n3: gamma"


async def test_read_resolves_nested_and_absolute_paths_inside_root(tmp_path: Path) -> None:
    nested_dir = tmp_path / "sub"
    nested_dir.mkdir()
    (nested_dir / "nested.txt").write_bytes(b"nested\n")

    nested = await _run(tmp_path, {"path": "sub/nested.txt"})
    assert nested.state == "succeeded"
    assert _text(nested) == "Read file sub/nested.txt, lines 1-1\n1: nested"

    absolute = await _run(tmp_path, {"path": str(nested_dir / "nested.txt")})
    assert absolute.state == "succeeded"
    assert _text(absolute).endswith("\n1: nested")


async def test_read_normalizes_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "win.txt").write_bytes(b"one\r\ntwo\r\n")

    outcome = await _run(tmp_path, {"path": "win.txt"})

    assert _text(outcome) == "Read file win.txt, lines 1-2\n1: one\n2: two"


async def test_read_empty_file_is_zero_lines(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_bytes(b"")

    outcome = await _run(tmp_path, {"path": "empty.txt"})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "Read file empty.txt, 0 lines"


async def test_read_paginates_with_offset_and_limit(tmp_path: Path) -> None:
    (tmp_path / "ten.txt").write_bytes(b"".join(f"line-{n}\n".encode() for n in range(1, 11)))

    page = await _run(tmp_path, {"path": "ten.txt", "offset": 3, "limit": 2})
    # The limit stopped the page mid-file; TS marks such pages truncated (next !== undefined).
    assert _text(page) == (
        "Read file ten.txt, lines 3-4\n3: line-3\n4: line-4\n"
        "[Output truncated: showing 13 of 56 bytes. Continue reading with offset: 5]"
    )

    continued = await _run(tmp_path, {"path": "ten.txt", "offset": 5, "limit": 2})
    assert _text(continued).startswith("Read file ten.txt, lines 5-6\n5: line-5")


async def test_read_limit_is_capped_at_max_read_lines(tmp_path: Path) -> None:
    lines = [f"l{n:04d}" for n in range(1, 2501)]  # 2500 five-char lines
    (tmp_path / "many.txt").write_bytes(("\n".join(lines) + "\n").encode())

    default_page = await _run(tmp_path, {"path": "many.txt"})
    assert "Read file many.txt, lines 1-2000" in _text(default_page)
    assert "[Output truncated: showing 11999 of 14999 bytes. Continue reading with offset: 2001]" in _text(default_page)

    # limit > MAX_READ_LINES is capped to MAX_READ_LINES, not honored verbatim.
    capped_page = await _run(tmp_path, {"path": "many.txt", "limit": 2500})
    assert "Read file many.txt, lines 1-2000" in _text(capped_page)


async def test_read_offset_out_of_range_is_validation_error(tmp_path: Path) -> None:
    (tmp_path / "ten.txt").write_bytes(b"".join(f"line-{n}\n".encode() for n in range(1, 11)))

    outcome = await _run(tmp_path, {"path": "ten.txt", "offset": 99})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Offset 99 is out of range for this file (10 lines)" in _text(outcome)


# ---------------------------------------------------------------------------
# §5.3 L272-273 path containment (R0-B verified scheme)
# ---------------------------------------------------------------------------


async def test_read_missing_file_is_not_found(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, {"path": "missing.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert "File not found: missing.txt" in _text(outcome)


async def test_read_directory_is_unsupported(tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()

    outcome = await _run(tmp_path, {"path": "adir"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "Path is not a file: adir" in _text(outcome)


async def test_read_nul_in_path_is_validation_error(tmp_path: Path) -> None:
    """T-B-017 ③-9: JSON can legally carry a NUL in the path string; the
    parse-point rejection (nul_byte_in_path) is a VALIDATION_ERROR -- a
    different checkpoint from the read-point binary-window NUL
    (TOOL_FAILED, pinned by test_read_binary_file_is_tool_failed)."""
    outcome = await _run(tmp_path, {"path": "a\x00b"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Path contains NUL bytes" in _text(outcome)


async def test_read_absolute_path_outside_root_is_denied(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")

    outcome = await _run(tmp_path, {"path": str(outside / "secret.txt")})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(outcome)


async def test_read_traversal_outside_root_is_denied(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")

    outcome = await _run(tmp_path, {"path": "../outside/secret.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(outcome)


def _symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink, skipping when the OS privilege is unavailable (R0-B)."""
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("symlink creation privilege not held (winerror 1314)")
        raise


async def test_read_symlink_inside_root_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_bytes(b"via link\n")
    _symlink_or_skip(tmp_path / "real.txt", tmp_path / "link.txt")

    outcome = await _run(tmp_path, {"path": "link.txt"})

    assert outcome.state == "succeeded"
    assert _text(outcome).endswith("\n1: via link")


async def test_read_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")
    _symlink_or_skip(outside / "secret.txt", tmp_path / "escape.txt")

    outcome = await _run(tmp_path, {"path": "escape.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


@pytest.mark.skipif(_PLATFORM != "win32", reason="junctions are Windows-only")
async def test_read_junction_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")
    try:
        _winapi.CreateJunction(str(outside), str(tmp_path / "escape-junction"))
    except OSError as error:
        pytest.skip(f"junction creation unavailable: {type(error).__name__}")

    outcome = await _run(tmp_path, {"path": "escape-junction" + "\\secret.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED


# ---------------------------------------------------------------------------
# binary / encoding / §5.3 L276 window and output caps
# ---------------------------------------------------------------------------


async def test_read_binary_file_is_tool_failed(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"ok\x00data")

    outcome = await _run(tmp_path, {"path": "blob.bin"})

    assert (outcome.state, outcome.effect_status) == ("failed", "not_started")
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "Cannot read binary file: blob.bin" in _text(outcome)


async def test_read_invalid_utf8_is_tool_failed(tmp_path: Path) -> None:
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9\n")

    outcome = await _run(tmp_path, {"path": "latin1.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "not valid UTF-8 text" in _text(outcome)


async def test_read_window_is_capped_at_one_mib(tmp_path: Path) -> None:
    payload = b"a" * 600_000 + b"\n" + b"b" * 600_000 + b"\n" + b"c" * 100  # 1,200,102 bytes
    (tmp_path / "big.txt").write_bytes(payload)

    outcome = await _run(tmp_path, {"path": "big.txt"})

    assert outcome.state == "succeeded"
    text = _text(outcome)
    # The window cut lands mid-line-2: both visible lines are clamped per line.
    assert "Read file big.txt, lines 1-2" in text
    assert MAX_LINE_SUFFIX in text
    assert f"[File truncated: showing the first 1048576 of {len(payload)} bytes]" in text
    # Both clamped lines fit the 64 KiB output budget: no output marker.
    assert "Continue reading with offset:" not in text

    # Offset beyond the window's line count reports the window explicitly.
    beyond = await _run(tmp_path, {"path": "big.txt", "offset": 3})
    assert beyond.state == "failed"
    assert beyond.error is not None
    assert beyond.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Offset 3 is out of range for this file (2 lines)" in _text(beyond)
    assert f"(reading only the first 1048576 of {len(payload)} bytes)" in _text(beyond)


async def test_read_output_is_capped_at_64_kib_with_marker(tmp_path: Path) -> None:
    lines = [f"{n:04d}{'x' * 96}" for n in range(1, 1501)]  # 1500 lines x 100 chars
    (tmp_path / "many.txt").write_bytes(("\n".join(lines) + "\n").encode())

    outcome = await _run(tmp_path, {"path": "many.txt"})

    assert outcome.state == "succeeded"
    text = _text(outcome)
    assert "Read file many.txt, lines 1-648" in text
    assert "[Output truncated: showing 65447 of 151499 bytes. Continue reading with offset: 649]" in text

    continued = await _run(tmp_path, {"path": "many.txt", "offset": 649})
    assert "Read file many.txt, lines 649-1296" in _text(continued)
    assert "Continue reading with offset: 1297" in _text(continued)


# ---------------------------------------------------------------------------
# event sequence + serial execution (§5.1 L257, PHASE1-SLIM L85)
# ---------------------------------------------------------------------------


async def test_read_event_sequence_and_hashes(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes(b"alpha\n")
    sink = _RecordingSink()
    registry = _registry_with_read(sink)
    input: JsonObject = {"path": "notes.txt", "limit": 5}
    context = _context(tmp_path)

    outcome = await registry.invoke(context, "fs__read", input, "exec_001")

    assert outcome.state == "succeeded"
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED, PY_TOOL_FINISHED]
    assert all(draft.type.startswith("py.tool.") for draft in sink.drafts)  # no py.permission.* in R2
    for draft in sink.drafts:
        assert draft.version == 1
        assert draft.session_id == _SESSION_ID
        assert draft.metadata == {"run_id": _RUN_ID}
        assert draft.created_at == _FIXED_TIME

    requested = sink.drafts[0].data
    assert isinstance(requested, ToolRequestedData)
    assert requested.message_id == _MESSAGE_ID
    assert requested.tool_call_id == _TOOL_CALL_ID
    assert requested.execution_id == "exec_001"
    assert requested.requested_name == "fs__read"
    assert requested.tool_ref == ToolRef(namespace="fs", name="read", version=1)
    assert requested.input == input
    assert requested.input_hash == _input_hash(input)

    started = sink.drafts[1].data
    assert isinstance(started, ToolStartedData)
    assert started.execution_id == "exec_001"
    assert started.permission_decision == "allow"
    assert started.input_hash == _input_hash(input)

    finished = sink.drafts[2].data
    assert isinstance(finished, ToolFinishedData)
    assert finished.execution_id == "exec_001"
    assert (finished.state, finished.effect_status) == ("succeeded", "completed")
    assert finished.structured_output is None
    assert finished.error is None
    assert [part.type for part in finished.content] == ["text"]


class _GatedAdapter:
    """Adapter that blocks on a gate, recording exactly when execution starts."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.executions: list[str] = []
        self._spec = ToolSpec(
            namespace="app",
            name="slow",
            version=1,
            description="gated test tool",
            input_schema={"type": "object", "properties": {"tag": {"type": "string"}}},
            supported_content_types=["text"],
            side_effects=False,
            idempotency_support=True,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        tag = str(input.get("tag", ""))
        self.executions.append(tag)
        await self.gate.wait()
        return ToolResult(content=[ToolTextContent(type="text", text=f"done {tag}")])


async def test_invoke_executes_serially_in_call_start_order(tmp_path: Path) -> None:
    sink = _RecordingSink()
    registry = ToolRegistry(sink=sink, clock=_clock, ids=_FixedIds())
    gated = _GatedAdapter()
    registry.register(gated)
    registry.register(FsReadAdapter())
    registry.snapshot(_SESSION_ID)
    context = _context(tmp_path)

    first = asyncio.create_task(registry.invoke(context, "app__slow", {"tag": "A"}, "exec_a"))
    await asyncio.sleep(0)  # A starts: requested + started committed, adapter waiting on the gate
    assert gated.executions == ["A"]
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED]

    second = asyncio.create_task(registry.invoke(context, "app__slow", {"tag": "B"}, "exec_b"))
    for _ in range(5):
        await asyncio.sleep(0)
    # B is parked on the serial lock: its pipeline must not have started.
    assert gated.executions == ["A"]
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED]

    gated.gate.set()
    outcome_first = await first
    outcome_second = await second

    assert outcome_first.state == "succeeded"
    assert outcome_second.state == "succeeded"
    assert gated.executions == ["A", "B"]
    # No interleaving: A's full requested->started->finished precedes B's.
    assert [draft.type for draft in sink.drafts] == [
        PY_TOOL_REQUESTED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
        PY_TOOL_REQUESTED,
        PY_TOOL_STARTED,
        PY_TOOL_FINISHED,
    ]
    b_requested = cast(ToolRequestedData, sink.drafts[3].data)
    assert b_requested.execution_id == "exec_b"
    assert b_requested.input == {"tag": "B"}
