"""R3-S4 integration tests: ``fs__write`` / ``fs__edit`` on a real filesystem + the ask gate.

The real ``FsWriteAdapter``/``FsEditAdapter`` (with the read quartet as the
round-trip partner) through a real ``ToolRegistry`` assembled with the
host's default six (the cli.py assembly face), each over a real temporary
workspace (``tmp_path`` is redirected to the approved temp root by
``tests/integration/conftest.py``). The approval-driven face goes through
the full App composition (real SQLite + ``alembic upgrade head`` + runner +
policy + approval channel) exactly like ``tests/integration/
test_approval.py``, with the real fs tools in place of the ``tst.write``
double. Contract anchors: plan §2 R3-S4, CORE-CONTRACTS §5.1 L253/L255/
L257/L259, §5.2 L266, §5.3 L272-276, §7 L316, PHASE1-SLIM §4 L91
(acceptance A: exact conflict detection, atomic replacement; verification
line: real temporary workspaces).

④ disclosures: the interrupt-injection tests replace ``os.replace`` at the
single seam between the temp-file write and the swap — the only in-process
way to hit that failure path deterministically (a hard kill inside the
window is not testable in-process; the target itself stays intact either
way). Adapter-reported write/edit failures surface as ``failed/unknown``:
the tools declare ``side_effects=True``, and the frozen §2.1 invoker
mapping conservatively marks a failed started mutation unknown — the disk
assertions in the interrupt/zero-match tests prove the actual no-change
under that terminal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot, CoreErrorCode
from opencode.contracts.events import (
    PY_PERMISSION_REPLIED,
    PY_PERMISSION_REQUESTED,
    PY_TOOL_FINISHED,
    PY_TOOL_REQUESTED,
    PY_TOOL_STARTED,
    EventDraft,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
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
from opencode.session import RunRef, RunStatusValue, SessionService
from opencode.session.context import RuntimeSettings
from opencode.session.service import RunStatus
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import (
    EDIT_MAX_SOURCE_BYTES,
    WRITE_MAX_BYTES,
    FsEditAdapter,
    FsGlobAdapter,
    FsGrepAdapter,
    FsLsAdapter,
    FsReadAdapter,
    FsWriteAdapter,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_FIXED_TIME = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_itwr"
_RUN_ID = "run_itwr"
_MESSAGE_ID = "msg_itwr"
_TOOL_CALL_ID = "call_itwr"

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")


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


def _registry_with_six(sink: _RecordingSink) -> ToolRegistry:
    """The host's default assembly (cli.py): all six fs builtin tools."""
    registry = ToolRegistry(sink=sink, clock=_clock, ids=cast("Any", _FixedIds()))
    for adapter in (
        FsReadAdapter(),
        FsLsAdapter(),
        FsGlobAdapter(),
        FsGrepAdapter(),
        FsWriteAdapter(),
        FsEditAdapter(),
    ):
        registry.register(adapter)
    registry.snapshot(_SESSION_ID)
    return registry


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


async def _run(working_dir: Path, wire_name: str, input: JsonObject) -> ToolOutcome:
    """One invoke through the six-tool registry; outcome only."""
    registry = _registry_with_six(_RecordingSink())
    return await registry.invoke(_context(working_dir), wire_name, input, "exec_it")


def _text(outcome: ToolOutcome) -> str:
    """The single text content of a tool outcome."""
    parts = [part for part in outcome.content if isinstance(part, ToolTextContent)]
    assert len(parts) == 1
    return parts[0].text


def _symlink_or_skip(target: Path, link: Path) -> None:
    """Create a symlink, skipping when the OS privilege is unavailable (R0-B)."""
    try:
        link.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            pytest.skip("symlink creation privilege not held (winerror 1314)")
        raise


def _temp_leftovers(directory: Path) -> list[str]:
    """Stray ``.opencode-fs-*`` temp files left in ``directory`` (must be none)."""
    return [entry.name for entry in directory.iterdir() if entry.name.startswith(".opencode-fs-")]


# ---------------------------------------------------------------------------
# App-level harness (the test_approval.py idioms, real fs tools in place)
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


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout:{result.stdout}\nstderr:{result.stderr}"


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    rows = _fetch_all(
        db_path,
        "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
        (session_id, event_type),
    )
    return [json.loads(str(row[0])) for row in rows]


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

    def call_start(self, call_index: int = 0, *, tool_call_id: str | None = None, name: str | None = None) -> _Script:
        self.items.append(
            ToolCallStartChunk(
                type="tool_call_start",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                tool_call_id=tool_call_id,
                name=name,
            )
        )
        return self

    def call_args(self, delta: str, call_index: int = 0) -> _Script:
        self.items.append(
            ToolCallArgsDeltaChunk(
                type="tool_call_args_delta",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                delta=delta,
            )
        )
        return self

    def call_end(self, tool_call_id: str, name: str, raw_arguments: str, *, call_index: int = 0) -> _Script:
        self.items.append(
            ToolCallEndChunk(
                type="tool_call_end",
                request_id="req_script",
                index=self._next(),
                call_index=call_index,
                tool_call_id=tool_call_id,
                name=name,
                raw_arguments=raw_arguments,
            )
        )
        return self

    def usage(self, input_tokens: int = 5, output_tokens: int = 7) -> _Script:
        self.items.append(
            UsageChunk(
                type="usage",
                request_id="req_script",
                index=self._next(),
                usage=Usage(input=input_tokens, output=output_tokens),
                cost_usd="0.0012",
            )
        )
        return self

    def finish(self, reason: FinishReason = FinishReason.STOP) -> _Script:
        self.items.append(FinishChunk(type="finish", request_id="req_script", index=self._next(), reason=reason))
        return self


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request; every request and close recorded."""

    def __init__(self, scripts: Sequence[Sequence[_Script]]) -> None:
        self.scripts = [[item for script in script_list for item in script.items] for script_list in scripts]
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            yield cast(ProviderChunk, item)

    async def close(self) -> None:
        self.close_count += 1


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=_MODEL,
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


async def _await_run_status(
    service: SessionService, run_id: str, wanted: frozenset[RunStatusValue], *, timeout_s: float = 5.0
) -> RunStatus:
    """Poll run_status until the run reaches one of ``wanted`` (bounded)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status in wanted:
            return status
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"run {run_id} did not reach {wanted} (stuck at {status.status}: {status.error})")
        await asyncio.sleep(0.01)


async def _await_permission_requested(
    db_path: Path, session_id: str, *, count: int = 1, timeout_s: float = 5.0
) -> list[dict[str, Any]]:
    """Poll the events table until ``count`` py.permission.requested rows exist (live sqlite read)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            rows = _fetch_all(
                db_path,
                "SELECT data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
                (session_id, PY_PERMISSION_REQUESTED),
            )
        except sqlite3.OperationalError:
            rows = None  # a concurrent writer holds the lock; retry after a tick
        if rows is not None and len(rows) >= count:
            return [json.loads(str(row[0])) for row in rows[:count]]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no py.permission.requested row appeared for {session_id}")
        await asyncio.sleep(0.01)


async def _reply_registered(service: SessionService, permission_id: str, reply: str) -> Any:
    """Reply once the coordinator holds the permission pending (poll-retry on NotFound)."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        try:
            return await service.reply_permission(permission_id, reply)
        except Exception as error:
            if type(error).__name__ == "NotFoundError" and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
                continue
            raise


def _fs_call_script(arguments: dict[str, Any], *, name: str, call_id: str = "call_1") -> _Script:
    """One scripted round: text, a single tool call carrying ``arguments``, finish."""
    raw = json.dumps(arguments)
    return (
        _Script()
        .text("p1", "let me do it")
        .call_start(0, tool_call_id=call_id, name=name)
        .call_args(raw, 0)
        .call_end(call_id, name, raw, call_index=0)
        .usage()
        .finish()
    )


def _final_script() -> _Script:
    return _Script().text("p2", "all done").usage().finish()


# ---------------------------------------------------------------------------
# fs__write: creation, overwrite, round trip, boundaries
# ---------------------------------------------------------------------------


async def test_write_creates_new_file_with_exact_bytes(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__write", {"path": "new.txt", "content": "héllo wörld\nsecond line\n"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.requested_name == "fs__write"
    assert _text(outcome) == "Created file successfully: new.txt"
    assert (tmp_path / "new.txt").read_bytes() == "héllo wörld\nsecond line\n".encode()


async def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__write", {"path": "sub/dir/file.txt", "content": "nested\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "sub" / "dir" / "file.txt").read_bytes() == b"nested\n"


async def test_write_overwrites_existing_file_completely(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_bytes(b"old content that is much longer than the new one\n")

    outcome = await _run(tmp_path, "fs__write", {"path": "keep.txt", "content": "short\n"})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "Wrote file successfully: keep.txt"
    # No residue of the longer previous content: the swap replaces the file.
    assert (tmp_path / "keep.txt").read_bytes() == b"short\n"


async def test_write_then_read_round_trip_is_verbatim(tmp_path: Path) -> None:
    """plan §2.4 test plan: real workspace round trip (write -> read, byte-exact)."""
    await _run(tmp_path, "fs__write", {"path": "rt.txt", "content": "line one\nline two\n"})

    outcome = await _run(tmp_path, "fs__read", {"path": "rt.txt"})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "Read file rt.txt, lines 1-2\n1: line one\n2: line two"


async def test_write_absolute_path_inside_root_is_accepted(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__write", {"path": str(tmp_path / "abs.txt"), "content": "absolute\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "abs.txt").read_bytes() == b"absolute\n"


async def test_write_traversal_escape_is_denied(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")

    outcome = await _run(tmp_path, "fs__write", {"path": "../outside/x.txt", "content": "no\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(outcome)
    assert not list(outside.glob("*"))


async def test_write_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")
    _symlink_or_skip(outside / "secret.txt", tmp_path / "escape-link")

    outcome = await _run(tmp_path, "fs__write", {"path": "escape-link", "content": "no\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert (outside / "secret.txt").read_bytes() == b"secret\n"  # untouched


@pytest.mark.skipif(_PLATFORM != "win32", reason="junctions are Windows-only")
async def test_write_junction_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    # mypy narrowing: typeshed types _winapi as win32-only; the skipif above
    # already keeps the runtime off this body on non-Windows hosts.
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").mkdir()
    try:
        _winapi.CreateJunction(str(outside), str(tmp_path / "escape-junction"))
    except OSError as error:
        pytest.skip(f"junction creation unavailable: {type(error).__name__}")

    outcome = await _run(tmp_path, "fs__write", {"path": "escape-junction/file.txt", "content": "no\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert not list((outside / "secret").glob("*"))


async def test_write_directory_target_is_unsupported(tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()

    outcome = await _run(tmp_path, "fs__write", {"path": "adir", "content": "no\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "Path is a directory, not a file: adir" in _text(outcome)


async def test_write_nul_in_path_is_validation_error(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__write", {"path": "a\x00b.txt", "content": "no\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Path contains NUL bytes" in _text(outcome)


async def test_write_over_limit_content_is_rejected(tmp_path: Path) -> None:
    """PHASE1-SLIM L91 (oversized diagnostic): the quantified write cap, verbatim."""
    content = "x" * (WRITE_MAX_BYTES + 1)

    outcome = await _run(tmp_path, "fs__write", {"path": "big.txt", "content": content})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert f"content is {WRITE_MAX_BYTES + 1} bytes, over the {WRITE_MAX_BYTES} byte write limit" in _text(outcome)
    assert not (tmp_path / "big.txt").exists()


async def test_write_interrupt_before_replace_leaves_existing_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plan §2.4 test plan: write interruption injection -> no half-written file."""
    (tmp_path / "keep.txt").write_bytes(b"original\n")

    def _replace_disabled(src: str, dst: str) -> None:
        raise OSError("os.replace disabled by test")

    monkeypatch.setattr(os, "replace", _replace_disabled)
    outcome = await _run(tmp_path, "fs__write", {"path": "keep.txt", "content": "replacement\n"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "Cannot write keep.txt" in _text(outcome)
    # Atomicity: the target keeps its old bytes and no temp file is left behind.
    assert (tmp_path / "keep.txt").read_bytes() == b"original\n"
    assert _temp_leftovers(tmp_path) == []


async def test_write_interrupt_on_create_leaves_no_target_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _replace_disabled(src: str, dst: str) -> None:
        raise OSError("os.replace disabled by test")

    monkeypatch.setattr(os, "replace", _replace_disabled)
    outcome = await _run(tmp_path, "fs__write", {"path": "fresh.txt", "content": "nope\n"})

    assert outcome.state == "failed"
    assert not (tmp_path / "fresh.txt").exists()
    assert _temp_leftovers(tmp_path) == []


async def test_write_event_sequence_default_allow(tmp_path: Path) -> None:
    """§5.1 L257 requested->started->finished through the six-tool registry
    (no injected policy: the R2 default-allow face; §5.2 L266 turns the
    write/edit keys to ask only under an injected policy)."""
    sink = _RecordingSink()
    registry = _registry_with_six(sink)
    context = _context(tmp_path)

    outcome = await registry.invoke(context, "fs__write", {"path": "seq.txt", "content": "seq\n"}, "exec_001")

    assert outcome.state == "succeeded"
    assert [draft.type for draft in sink.drafts] == [PY_TOOL_REQUESTED, PY_TOOL_STARTED, PY_TOOL_FINISHED]
    requested = sink.drafts[0].data
    assert isinstance(requested, ToolRequestedData)
    assert requested.requested_name == "fs__write"
    started = sink.drafts[1].data
    assert isinstance(started, ToolStartedData)
    assert started.permission_decision == "allow"
    finished = sink.drafts[2].data
    assert isinstance(finished, ToolFinishedData)
    assert (finished.state, finished.effect_status) == ("succeeded", "completed")


# ---------------------------------------------------------------------------
# fs__edit: the edit.ts conflict-detection tiers
# ---------------------------------------------------------------------------


async def test_edit_replaces_unique_occurrence_preserving_rest(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_bytes(b"before target after\nsecond line\nend\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "one.txt", "oldString": "target", "newString": "renamed"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert _text(outcome) == "Edited one.txt (1 replacement)"
    assert (tmp_path / "one.txt").read_bytes() == b"before renamed after\nsecond line\nend\n"


async def test_edit_multiple_matches_fail_without_replace_all(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_bytes(b"v = 1\nv = 2\nv = 3\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "many.txt", "oldString": "v = ", "newString": "w = "})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Found 3 matches for oldString, but expected exactly one" in _text(outcome)
    assert (tmp_path / "many.txt").read_bytes() == b"v = 1\nv = 2\nv = 3\n"  # untouched


async def test_edit_replace_all_applies_every_occurrence(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_bytes(b"v = 1\nv = 2\nv = 3\n")

    outcome = await _run(
        tmp_path,
        "fs__edit",
        {"path": "many.txt", "oldString": "v = ", "newString": "w = ", "replaceAll": True},
    )

    assert outcome.state == "succeeded"
    assert _text(outcome) == "Edited many.txt (3 replacements)"
    assert (tmp_path / "many.txt").read_bytes() == b"w = 1\nw = 2\nw = 3\n"


async def test_edit_zero_matches_fails_without_touching_file(tmp_path: Path) -> None:
    (tmp_path / "miss.txt").write_bytes(b"nothing to see here\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "miss.txt", "oldString": "absent", "newString": "x"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "Could not find oldString in miss.txt" in _text(outcome)
    assert (tmp_path / "miss.txt").read_bytes() == b"nothing to see here\n"


async def test_edit_empty_old_string_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_bytes(b"data\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "e.txt", "oldString": "", "newString": "x"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "oldString must not be empty. Use write to create or overwrite a file." in _text(outcome)


async def test_edit_identical_strings_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_bytes(b"data\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "e.txt", "oldString": "data", "newString": "data"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "No changes to apply: oldString and newString are identical." in _text(outcome)


async def test_edit_unicode_variant_fallback_matches_typographic_file(tmp_path: Path) -> None:
    """edit.ts tier 2 (normalizeForMatch): typographic variants still match."""
    (tmp_path / "curly.txt").write_bytes("it\u2019s here\n".encode())

    outcome = await _run(tmp_path, "fs__edit", {"path": "curly.txt", "oldString": "it's", "newString": "IT'S"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "curly.txt").read_bytes() == b"IT'S here\n"


async def test_edit_unicode_noop_reports_unchanged(tmp_path: Path) -> None:
    """The reachable unchanged branch: a fallback match whose replacement
    reproduces the file's bytes reports "no changes" instead of writing."""
    (tmp_path / "noop.txt").write_bytes(b"it's fine\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "noop.txt", "oldString": "it\u2019s", "newString": "it's"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.VALIDATION_ERROR
    assert "No changes to apply: the edit leaves noop.txt unchanged" in _text(outcome)
    assert (tmp_path / "noop.txt").read_bytes() == b"it's fine\n"


async def test_edit_trailing_whitespace_tolerant_match(tmp_path: Path) -> None:
    """edit.ts tier 3 (findLineOccurrences): whole-line match ignoring
    trailing whitespace; the replacement's clean line replaces the padded one."""
    (tmp_path / "trail.txt").write_bytes(b"alpha   \nbeta\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "trail.txt", "oldString": "alpha\n", "newString": "ALPHA\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "trail.txt").read_bytes() == b"ALPHA\nbeta\n"


async def test_edit_line_candidates_dedupe_overlaps(tmp_path: Path) -> None:
    """edit.ts findLineOccurrences overlap dedup: adjacent candidate windows
    over the same lines collapse to one replacement."""
    (tmp_path / "dup.txt").write_bytes(b"a\na\na\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "dup.txt", "oldString": "a \na\n", "newString": "X\n"})

    assert outcome.state == "succeeded"
    assert _text(outcome) == "Edited dup.txt (1 replacement)"
    assert (tmp_path / "dup.txt").read_bytes() == b"X\na\n"


async def test_edit_matches_lf_view_and_preserves_crlf_endings(tmp_path: Path) -> None:
    """edit.ts matchView core: the model's LF oldString matches a CRLF file
    and the file's CRLF endings survive the replacement."""
    (tmp_path / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "crlf.txt", "oldString": "beta\n", "newString": "BETA\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "crlf.txt").read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


async def test_edit_mixed_region_endings_follow_matched_lines(tmp_path: Path) -> None:
    """edit.ts withRegionEndings: inside one match, each line keeps the
    ending of the region line it replaces (mixed-ending file stays mixed)."""
    (tmp_path / "mixed.txt").write_bytes(b"x\r\ny\nz\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "mixed.txt", "oldString": "x\ny\n", "newString": "X\nY\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "mixed.txt").read_bytes() == b"X\r\nY\nz\n"


async def test_edit_line_endings_only_change_applies_exact_bytes(tmp_path: Path) -> None:
    """edit.ts endingsOnly: old/new differing only in \\r rewrite the exact
    bytes -- the matched CRLF becomes LF, the rest of the file keeps CRLF."""
    (tmp_path / "eol.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")

    outcome = await _run(tmp_path, "fs__edit", {"path": "eol.txt", "oldString": "two\r\n", "newString": "two\n"})

    assert outcome.state == "succeeded"
    assert (tmp_path / "eol.txt").read_bytes() == b"one\r\ntwo\nthree\r\n"


async def test_edit_missing_file_is_not_found(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__edit", {"path": "nope.txt", "oldString": "a", "newString": "b"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.NOT_FOUND
    assert "File not found: nope.txt" in _text(outcome)


async def test_edit_directory_target_is_unsupported(tmp_path: Path) -> None:
    (tmp_path / "adir").mkdir()

    outcome = await _run(tmp_path, "fs__edit", {"path": "adir", "oldString": "a", "newString": "b"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "Path is a directory, not a file: adir" in _text(outcome)


async def test_edit_traversal_escape_is_denied(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "fs__edit", {"path": "../outside/x.txt", "oldString": "a", "newString": "b"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert "Path escapes the working directory" in _text(outcome)


async def test_edit_symlink_escape_is_denied_after_realpath(tmp_path: Path, tmp_path_factory: Any) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_bytes(b"secret\n")
    _symlink_or_skip(outside / "secret.txt", tmp_path / "escape-link")

    outcome = await _run(tmp_path, "fs__edit", {"path": "escape-link", "oldString": "secret", "newString": "nope"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.PERMISSION_DENIED
    assert (outside / "secret.txt").read_bytes() == b"secret\n"


async def test_edit_invalid_utf8_file_is_tool_failed(tmp_path: Path) -> None:
    (tmp_path / "bin.txt").write_bytes(b"ok\xff\xfebad")

    outcome = await _run(tmp_path, "fs__edit", {"path": "bin.txt", "oldString": "bad", "newString": "good"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert "file is not valid UTF-8 text" in _text(outcome)


async def test_edit_source_over_limit_is_tool_failed(tmp_path: Path) -> None:
    """PHASE1-SLIM L91 (oversized diagnostic): the quantified edit source cap."""
    (tmp_path / "big.txt").write_bytes(b"x" * (EDIT_MAX_SOURCE_BYTES + 1))

    outcome = await _run(tmp_path, "fs__edit", {"path": "big.txt", "oldString": "x", "newString": "y"})

    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code == CoreErrorCode.TOOL_FAILED
    assert f"the file is {EDIT_MAX_SOURCE_BYTES + 1} bytes, over the {EDIT_MAX_SOURCE_BYTES} byte edit limit" in _text(
        outcome
    )


async def test_edit_interrupt_leaves_file_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "keep.txt").write_bytes(b"original\n")

    def _replace_disabled(src: str, dst: str) -> None:
        raise OSError("os.replace disabled by test")

    monkeypatch.setattr(os, "replace", _replace_disabled)
    outcome = await _run(tmp_path, "fs__edit", {"path": "keep.txt", "oldString": "original", "newString": "edited"})

    assert outcome.state == "failed"
    assert "Cannot edit keep.txt" in _text(outcome)
    assert (tmp_path / "keep.txt").read_bytes() == b"original\n"
    assert _temp_leftovers(tmp_path) == []


# ---------------------------------------------------------------------------
# The approval-driven face (full App composition, §5.2 L266 ask + §7 L316)
# ---------------------------------------------------------------------------


async def test_write_asks_and_once_approval_writes_the_file(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider(
        [[_fs_call_script({"path": "out.txt", "content": "approved bytes\n"}, name="fs.write")], [_final_script()]]
    )

    async with App(
        db_path,
        working_dir=str(tmp_path),
        provider=provider,
        tools=[FsWriteAdapter()],
        settings=_settings(),
    ) as app:
        session = await app.service.create(_SNAPSHOT)
        run_ref: RunRef = await app.service.prompt(session.session_id, "write out.txt please", "req_1")

        requested = await _await_permission_requested(db_path, session.session_id)
        # §5.2 L266 default table: root-internal fs__write asks; the policy
        # binds the concrete path resource.
        assert requested[0]["action"] == "fs__write"
        assert requested[0]["resources"] == ["out.txt"]

        await _reply_registered(app.service, str(requested[0]["permission_id"]), "once")
        final = await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))
        assert final.error is None

        assert (tmp_path / "out.txt").read_bytes() == b"approved bytes\n"
        started = _event_data(db_path, session.session_id, PY_TOOL_STARTED)[0]
        assert started["permission_decision"] == "allow"
        # ④-2 pairing discipline: replied and started share one batch.
        pair = _fetch_all(
            db_path,
            "SELECT type, batch_id FROM events WHERE session_id = ? AND type IN (?, ?)",
            (session.session_id, PY_PERMISSION_REPLIED, PY_TOOL_STARTED),
        )
        assert [row[0] for row in pair] == [PY_PERMISSION_REPLIED, PY_TOOL_STARTED]
        assert pair[0][1] == pair[1][1]


async def test_edit_ask_rejected_leaves_file_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    (tmp_path / "target.txt").write_bytes(b"alpha beta\n")
    provider = _ScriptedProvider(
        [
            [_fs_call_script({"path": "target.txt", "oldString": "alpha", "newString": "omega"}, name="fs.edit")],
            [_final_script()],
        ]
    )

    async with App(
        db_path,
        working_dir=str(tmp_path),
        provider=provider,
        tools=[FsEditAdapter()],
        settings=_settings(),
    ) as app:
        session = await app.service.create(_SNAPSHOT)
        run_ref = await app.service.prompt(session.session_id, "edit target.txt please", "req_1")

        requested = await _await_permission_requested(db_path, session.session_id)
        assert requested[0]["action"] == "fs__edit"
        assert requested[0]["resources"] == ["target.txt"]

        await _reply_registered(app.service, str(requested[0]["permission_id"]), "reject")
        await _await_run_status(app.service, run_ref.run_id, frozenset({RunStatusValue.SUCCEEDED}))

        # §2.1 L47 via the S2 gate: denied / not_started, no started event,
        # and the file on disk is untouched.
        assert (tmp_path / "target.txt").read_bytes() == b"alpha beta\n"
        finished = _event_data(db_path, session.session_id, PY_TOOL_FINISHED)[0]
        assert (finished["state"], finished["effect_status"]) == ("denied", "not_started")
        assert finished["error"]["code"] == CoreErrorCode.PERMISSION_DENIED.value
        assert _event_data(db_path, session.session_id, PY_TOOL_STARTED) == []
