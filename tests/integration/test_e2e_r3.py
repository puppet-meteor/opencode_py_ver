"""R3-S8 closeout: the six-tool full-chain e2e over the real stack (T-C1-032 ①).

Three pins over the genuine full stack -- scripted OpenAI server on real
loopback HTTP -> MaasAdapter SSE -> App -> SessionService/runner/step engine
-> Invoker -> the seven real builtins operating a real file tree in the
session working directory, with the R3 approval face driven live:

① ``test_r3_six_tool_chain_with_live_approvals`` -- ONE run, three scripted
   generations. Generation one fans out the three *ask* tools of the default
   tier table (``policy.py`` ``_DEFAULT_EFFECTS``: fs write/edit + bash ask):
   ``fs__write`` creates the real note, ``fs__edit`` atomically rewrites
   alpha->gamma, ``bash__run`` reads it back through the real interpreter
   (PowerShell on win32, cat elsewhere -- the probe chain's documented
   fallback is exercised, pwsh being absent here). Each parks on a real
   ``py.permission.requested``; the test replies ``once`` mid-run through
   ``service.reply_permission`` (§7 L316: the approval channel is independent
   of the run locks), and the tool proceeds. Generation two fans out the four
   *allow* tools (``fs__grep`` through the real bundled ripgrep binary,
   ``fs__glob``, ``fs__ls``, plus the R2 ``fs__read`` as the control face).
   Generation three closes the run with plain text. Wire tool-result
   round-trips, store projection, the real file's final bytes, and the
   durable approval arithmetic (requested/replied/saved/closed) are all
   asserted.
② ``test_event_log_ledger_per_tool_cross_validation`` -- the same chain
   through a fresh App; the durable EventLog is cross-validated per tool:
   every ``py.tool.requested`` execution_id joins its ``py.tool.finished``
   terminal (7 tools, one execution each, all succeeded/completed), the
   permission ledger reconciles (3 requested + 3 replied "once", 0 saved,
   0 closed), and the run/step/tool totals close (1 run, 3 steps, 7 tools).
③ ``test_unanswered_approval_expires_tool_not_started_run_succeeds`` -- a
   short real approval deadline (``approval_timeout_s=0.05``, the
   test_approval.py seam) with NO host reply: the watchdog resolves the
   pending permission expired, the tool folds cancelled/not_started with
   ApprovalUnavailable (never a silent allow), the run still succeeds, and
   the closed pair reconciles (requested=1, replied=0, closed(expired)=1)
   -- the acceptance-B "no approval means no execution" face end to end.
④ ``test_post_start_graceful_failure_reaches_run_terminal`` -- the
   T-C1-033 row#34 regression nail: a scripted ``fs__read`` on a missing
   path fails gracefully after ``py.tool.started`` and folds
   ``failed/unknown`` (§2.1 L50), the diagnostic text feeds back on the
   wire, and the run closes succeeded (never the pre-fix
   failed/not_started fold-rejection wedge).

Always/reject approval branches, the policy-deny face, and the multiroot fs
faces are deliberately NOT re-woven here: they are already carried by
``test_cli_r3.py`` (approve once/always, reject, config deny end to end),
``test_approval.py`` (once/reject/expiry/late-reply CAS through the service)
and ``test_fs_multiroot.py`` (roots) -- this file pins the *chain*.

Fixture reuse is read-only: the FakeOpenAIServer/Frame/ScriptedResponse
helpers come from ``tests/r0``, the alembic ``_upgrade_head`` /
``_await_terminal`` idioms are reconstructed in the shape of
``tests/integration/test_e2e.py``, the live permission polling and the
NotFound poll-retry reply come from ``tests/integration/test_approval.py``,
and the ripgrep discovery seam (PATH first, then known bundled copies --
never a fake) comes from ``tests/integration/test_tool_static_trio.py``; no
existing file is modified. Live sqlite reads happen only in the sanctioned
mid-run permission polling; every counting/cross-validation read happens
strictly after the durable terminal and app close (the S7 discipline).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import opencode.tools.builtin.grep as grep_module
from opencode.ai.maas import MaasAdapter
from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    JsonObject,
    UserMessage,
)
from opencode.session.context import RuntimeSettings
from opencode.session.service import RunStatus, SessionService
from opencode.storage import NotFoundError
from opencode.tools.builtin import (
    BashAdapter,
    FsEditAdapter,
    FsGlobAdapter,
    FsGrepAdapter,
    FsLsAdapter,
    FsReadAdapter,
    FsWriteAdapter,
)
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The full R3 tool face (six builtins) plus the R2 ``fs__read`` control face.
_TOOLS = (
    FsWriteAdapter(),
    FsEditAdapter(),
    BashAdapter(),
    FsGrepAdapter(),
    FsGlobAdapter(),
    FsLsAdapter(),
    FsReadAdapter(),
)

#: The rendered results asserted verbatim on the wire (the toModelContent faces).
_WRITE_RESULT = "Created file successfully: note.txt"
_EDIT_RESULT = "Edited note.txt (1 replacement)"
_GREP_RESULT = "Found 1 matches\nnote.txt:\n  Line 2: beta marker line"
_GLOB_RESULT = "note.txt"
_READ_RESULT = "Read file note.txt, lines 1-2\n1: gamma line\n2: beta marker line"


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
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model="fake-model"),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


async def _await_terminal(service: SessionService, run_id: str, *, timeout_s: float = 10.0) -> RunStatus:
    """Poll the durable run status (§7 L314) until it reaches a terminal."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status.is_terminal:
            return status
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} did not reach a terminal status within {timeout_s}s")
        await asyncio.sleep(0.02)


def _text_response(*deltas: str) -> ScriptedResponse:
    """One text stream: a delta per fragment, then a STOP finish (the test_cli idiom)."""
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


def _tool_calls_response(preamble: str, *calls: tuple[str, str, JsonObject]) -> ScriptedResponse:
    """One scripted tool_calls generation: preamble text, the calls with
    ascending wire call indexes, then the tool_calls finish."""
    frames = [Frame(text_delta_frame(preamble))]
    for index, (call_id, name, arguments) in enumerate(calls):
        frames.append(Frame(tool_call_start_frame(index, call_id, name)))
        frames.append(Frame(tool_call_args_frame(index, json.dumps(arguments))))
    frames.append(Frame(finish_frame("tool_calls")))
    return ScriptedResponse(frames=frames)


def _read_back_command() -> str:
    """The bash__run read-back through the real interpreter: PowerShell on
    win32 (the probe chain's documented fallback -- pwsh is absent on this
    host, so powershell.exe carries the leg), cat on every other platform."""
    return "Get-Content note.txt" if sys.platform == "win32" else "cat note.txt"


def _chain_responses() -> list[ScriptedResponse]:
    """The one-run, three-generation chain script.

    Generation one: the three ask tools (write -> edit -> bash read-back).
    Generation two: the four allow tools (grep -> glob -> ls -> the R2 read
    control). Generation three: the closing text.
    """
    return [
        _tool_calls_response(
            "let me build",
            ("call_write", "fs__write", {"path": "note.txt", "content": "alpha line\nbeta marker line\n"}),
            ("call_edit", "fs__edit", {"path": "note.txt", "oldString": "alpha", "newString": "gamma"}),
            ("call_bash", "bash__run", {"command": _read_back_command()}),
        ),
        _tool_calls_response(
            "let me observe",
            ("call_grep", "fs__grep", {"pattern": "beta"}),
            ("call_glob", "fs__glob", {"pattern": "*.txt"}),
            ("call_ls", "fs__ls", {}),
            ("call_read", "fs__read", {"path": "note.txt"}),
        ),
        _text_response("the chain is complete"),
    ]


# ---------------------------------------------------------------------------
# ripgrep location: PATH first, then known bundled copies (a real binary,
# never a fake) -- reconstructed in the test_tool_static_trio.py shape.
# ---------------------------------------------------------------------------


def _real_rg_or_skip() -> str:
    found = shutil.which("rg")
    if found is None:
        bundled = sorted(glob.glob(os.path.expanduser(r"~/.vscode/extensions/*/bin/windows-x86_64/rg.exe")))
        if bundled:
            found = bundled[-1]
    if found is None:
        pytest.skip("no ripgrep binary found on PATH or in known bundled locations")
    return found


def _patch_rg_path(monkeypatch: pytest.MonkeyPatch, executable: str) -> None:
    """Point grep's rg discovery at ``executable`` for one test (cache cleared:
    a stale not-found discovery must not mask the bundled binary)."""
    monkeypatch.setattr(grep_module, "_RG_CACHE", {})
    monkeypatch.setattr(grep_module, "_which_rg", lambda: executable)


# ---------------------------------------------------------------------------
# sqlite helpers: live reads only for the sanctioned mid-run permission
# polling (connect-per-poll, OperationalError retried); every counting read
# happens strictly post-terminal + post-close (the S7 discipline).
# ---------------------------------------------------------------------------


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _event_rows(db_path: Path, session_id: str) -> list[tuple[str, str]]:
    """Durable EventLog (type, data) rows in seq order -- post-close reads only."""
    return [
        (str(row[0]), str(row[1]))
        for row in _fetch_all(db_path, "SELECT type, data FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
    ]


def _event_types(db_path: Path, session_id: str) -> list[str]:
    return [kind for kind, _ in _event_rows(db_path, session_id)]


def _event_data(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    return [json.loads(data) for kind, data in _event_rows(db_path, session_id) if kind == event_type]


async def _await_permission_ids(db_path: Path, session_id: str, count: int, *, timeout_s: float = 30.0) -> list[str]:
    """Live-poll the durable ``py.permission.requested`` rows until ``count``
    exist (the test_approval.py idiom: connect-per-poll; a concurrent writer
    lock surfaces as OperationalError and is retried after a tick)."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            rows = _fetch_all(
                db_path,
                "SELECT data FROM events WHERE session_id = ? AND type = 'py.permission.requested' ORDER BY seq",
                (session_id,),
            )
        except sqlite3.OperationalError:
            rows = None
        if rows is not None and len(rows) >= count:
            return [str(json.loads(str(row[0]))["permission_id"]) for row in rows[:count]]
        if time.monotonic() > deadline:
            seen = 0 if rows is None else len(rows)
            pytest.fail(f"only {seen}/{count} py.permission.requested rows appeared for {session_id}")
        await asyncio.sleep(0.02)


async def _reply_registered(service: SessionService, permission_id: str, reply: str) -> None:
    """Reply once the coordinator holds the permission pending.

    ④ race disclosure (the test_approval.py idiom): the durable
    ``py.permission.requested`` commit (aiosqlite worker thread) becomes
    reader-visible strictly before the Invoker's in-process registration --
    a reply in that window legitimately answers NotFound, so poll-retry
    instead of assuming registration.
    """
    deadline = time.monotonic() + 5.0
    while True:
        try:
            await service.reply_permission(permission_id, reply)
            return
        except NotFoundError:
            if time.monotonic() > deadline:
                raise AssertionError(f"permission {permission_id} was never registered") from None
            await asyncio.sleep(0.01)


async def _approve_as_requested(service: SessionService, db_path: Path, session_id: str, *, expected: int) -> list[str]:
    """Reply ``once`` to each ask tool's permission as it appears mid-run
    (§7 L316: the parked Invoker holds no run lock, so the reply completes
    while the run waits). The ask tools park serially, so this polls one new
    permission_id at a time; returns the replied ids in request order."""
    replied: list[str] = []
    while len(replied) < expected:
        fresh = await _await_permission_ids(db_path, session_id, len(replied) + 1)
        for permission_id in fresh:
            if permission_id in replied:
                continue
            await _reply_registered(service, permission_id, "once")
            replied.append(permission_id)
    return replied


async def _drive_full_chain(app: App, session_id: str, db_path: Path) -> None:
    """Prompt the chain, reply to the three ask approvals concurrently, and
    wait for the durable succeeded terminal."""
    run = await app.service.prompt(session_id, "build and inspect the note", "req_1")
    approver = asyncio.create_task(_approve_as_requested(app.service, db_path, session_id, expected=3))
    try:
        terminal = await _await_terminal(app.service, run.run_id, timeout_s=60.0)
    finally:
        await approver
    assert terminal.status.value == "succeeded", f"{terminal.status.value}: {terminal.error}"


def _wire_messages(body: object) -> list[dict[str, object]]:
    """The request body's message list, narrowed for tolerant wire assertions."""
    assert isinstance(body, dict)
    messages = body.get("messages")
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, dict)]


def _content_str(content: object) -> str:
    """Wire content as text: a plain string or OpenAI content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "".join(texts)
    return ""


def _assistant_text(message: AssistantMessage) -> str:
    """Concatenated text parts of an assistant message."""
    return "".join(part.text for part in message.parts if isinstance(part, AssistantTextPart))


def _tool_parts(message: AssistantMessage) -> list[AssistantToolPart]:
    return [part for part in message.parts if isinstance(part, AssistantToolPart)]


def _wire_tool_messages(messages: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [message for message in messages if message.get("role") == "tool"]


# ---------------------------------------------------------------------------
# ① the chain: six R3 tools + live approvals, wire/store/file/ledger faces.
# ---------------------------------------------------------------------------


async def test_r3_six_tool_chain_with_live_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """①: one run, three generations, the seven real tools on a real tree --
    the three ask tools each park on a real approval and proceed on a mid-run
    ``once`` reply; the rendered results feed back verbatim on the wire."""
    # The DB (and its live writer.lock) live OUTSIDE working_dir as a
    # deterministic-environment choice: fs__grep scans "." with rg and a live
    # writer.lock in the tree makes rg exit 2 -- a shape tolerated since R4-S1
    # (answered with the already-collected rows), so this is not a workaround
    # for a wedged run (that defect is fixed; T-C1-032 float-report).
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    _patch_rg_path(monkeypatch, _real_rg_or_skip())

    server = FakeOpenAIServer(_chain_responses())
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=_TOOLS,
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            await _drive_full_chain(app, session_id, db_path)

            # store: user -> three assistant generations -> idle(succeeded)
            page = await app.service.messages(session_id, limit=20)
            assert [message.type for message in page.items] == [
                "user",
                "assistant",
                "assistant",
                "assistant",
                "idle",
            ]
            assert page.has_more is False
            users = [message for message in page.items if isinstance(message, UserMessage)]
            assert [message.text for message in users] == ["build and inspect the note"]
            assistants = [message for message in page.items if isinstance(message, AssistantMessage)]
            idles = [message for message in page.items if isinstance(message, IdleMessage)]
            assert [message.status for message in assistants] == ["completed", "completed", "completed"]
            assert [message.outcome for message in idles] == ["succeeded"]

            # generation one: the three ask tools, all succeeded on the once replies
            assert _assistant_text(assistants[0]) == "let me build"
            first_parts = _tool_parts(assistants[0])
            assert [part.name for part in first_parts] == ["fs.write", "fs.edit", "bash.run"]
            assert [part.state for part in first_parts] == ["succeeded"] * 3
            assert [part.effect_status for part in first_parts] == ["completed"] * 3

            # generation two: the four allow tools, no approval anywhere near them
            assert _assistant_text(assistants[1]) == "let me observe"
            second_parts = _tool_parts(assistants[1])
            assert [part.name for part in second_parts] == ["fs.grep", "fs.glob", "fs.ls", "fs.read"]
            assert [part.state for part in second_parts] == ["succeeded"] * 4

            # generation three closes the run
            assert _assistant_text(assistants[2]) == "the chain is complete"

            # wire: three provider requests; the second carries the ask leg's
            # three tool results, the third adds the allow leg's four
            assert len(server.requests) == 3
            second_request = _wire_messages(server.requests[1].body)
            ask_results = _wire_tool_messages(second_request)
            assert [message.get("tool_call_id") for message in ask_results] == ["call_write", "call_edit", "call_bash"]
            assert _content_str(ask_results[0].get("content")) == _WRITE_RESULT
            assert _content_str(ask_results[1].get("content")) == _EDIT_RESULT
            # the real interpreter read the edited file back
            assert "beta marker line" in _content_str(ask_results[2].get("content"))

            third_request = _wire_messages(server.requests[2].body)
            all_results = _wire_tool_messages(third_request)
            assert len(all_results) == 7
            allow_results = all_results[3:]
            assert [message.get("tool_call_id") for message in allow_results] == [
                "call_grep",
                "call_glob",
                "call_ls",
                "call_read",
            ]
            assert _content_str(allow_results[0].get("content")) == _GREP_RESULT
            assert _content_str(allow_results[1].get("content")) == _GLOB_RESULT
            # ls renders the working dir; the db (and its writer.lock) lives
            # outside it in the db_dir sibling, so tmp holds exactly note.txt:
            # equality (T-C1-033 nit-1)
            assert _content_str(allow_results[2].get("content")) == "note.txt"
            assert _content_str(allow_results[3].get("content")) == _READ_RESULT
    finally:
        await server.aclose()

    # the real file: created by the write, rewritten by the edit, read back
    # by bash -- the chain's net effect on disk
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "gamma line\nbeta marker line\n"

    # ③ durable approval arithmetic (必答①): the three ask tools asked, the
    # host replied once to each, nothing saved, nothing closed
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.permission.requested") == 3
    assert event_types.count("py.permission.replied") == 3
    assert event_types.count("py.permission.saved") == 0
    assert event_types.count("py.permission.closed") == 0
    requested_actions = [str(row["action"]) for row in _event_data(db_path, session_id, "py.permission.requested")]
    assert requested_actions == ["fs__write", "fs__edit", "bash__run"]
    replies = [str(row["reply"]) for row in _event_data(db_path, session_id, "py.permission.replied")]
    assert replies == ["once", "once", "once"]
    # run/step/tool totals: one run, three generations, seven executions
    assert event_types.count("py.run.finished") == 1
    assert event_types.count("py.step.generated") == 3
    assert event_types.count("py.tool.finished") == 7


# ---------------------------------------------------------------------------
# ② the durable ledger, cross-validated per tool.
# ---------------------------------------------------------------------------


async def test_event_log_ledger_per_tool_cross_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """②: the same chain through a fresh App; every ``py.tool.requested``
    execution joins its ``py.tool.finished`` terminal by execution_id, the
    per-tool counts say exactly one execution per builtin, and the permission
    ledger reconciles with the default tier table."""
    # The DB (and its live writer.lock) live OUTSIDE working_dir as a
    # deterministic-environment choice: fs__grep scans "." with rg and a live
    # writer.lock in the tree makes rg exit 2 -- a shape tolerated since R4-S1
    # (answered with the already-collected rows), so this is not a workaround
    # for a wedged run (that defect is fixed; T-C1-032 float-report).
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))
    _patch_rg_path(monkeypatch, _real_rg_or_skip())

    server = FakeOpenAIServer(_chain_responses())
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=_TOOLS,
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            await _drive_full_chain(app, session_id, db_path)
    finally:
        await server.aclose()

    requested = _event_data(db_path, session_id, "py.tool.requested")
    assert len(requested) == 7
    names_by_execution = {str(row["execution_id"]): str(row["requested_name"]) for row in requested}
    assert sorted(names_by_execution.values()) == [
        "bash__run",
        "fs__edit",
        "fs__glob",
        "fs__grep",
        "fs__ls",
        "fs__read",
        "fs__write",
    ]

    # every execution finished exactly once, succeeded, with a completed effect
    finished = _event_data(db_path, session_id, "py.tool.finished")
    assert len(finished) == 7
    per_tool = Counter(names_by_execution[str(row["execution_id"])] for row in finished)
    assert dict(per_tool) == {
        "fs__write": 1,
        "fs__edit": 1,
        "bash__run": 1,
        "fs__grep": 1,
        "fs__glob": 1,
        "fs__ls": 1,
        "fs__read": 1,
    }
    assert {str(row["state"]) for row in finished} == {"succeeded"}
    assert {str(row["effect_status"]) for row in finished} == {"completed"}

    # every execution started exactly once; the once-replied ask tools record
    # the effective "allow" decision, as do the default-allow reads
    started = _event_data(db_path, session_id, "py.tool.started")
    assert len(started) == 7
    assert {str(row["permission_decision"]) for row in started} == {"allow"}

    # the permission ledger closes against the default tier table: the three
    # ask actions asked and were replied "once"; nothing saved, nothing closed
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.permission.requested") == 3
    assert event_types.count("py.permission.replied") == 3
    assert event_types.count("py.permission.saved") == 0
    assert event_types.count("py.permission.closed") == 0

    # run/step totals
    assert event_types.count("py.run.finished") == 1
    assert event_types.count("py.step.generated") == 3


# ---------------------------------------------------------------------------
# ③ the unanswered approval: expiry refuses execution end to end.
# ---------------------------------------------------------------------------


async def test_unanswered_approval_expires_tool_not_started_run_succeeds(tmp_path: Path) -> None:
    """③: a short real approval deadline and NO host reply -- the watchdog
    resolves the permission expired, the pending tool folds cancelled /
    not_started (never a silent allow; the file is never written), the run
    still succeeds, and the closed pair reconciles on the durable ledger."""
    # The DB (and its live writer.lock) live OUTSIDE working_dir as a
    # deterministic-environment choice: fs__grep scans "." with rg and a live
    # writer.lock in the tree makes rg exit 2 -- a shape tolerated since R4-S1
    # (answered with the already-collected rows), so this is not a workaround
    # for a wedged run (that defect is fixed; T-C1-032 float-report).
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))

    server = FakeOpenAIServer(
        [
            _tool_calls_response(
                "let me write", ("call_write", "fs__write", {"path": "note.txt", "content": "nope\n"})
            ),
            _text_response("gave up on the write"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsWriteAdapter(),),
            settings=_settings(),
            approval_timeout_s=0.05,
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            run = await app.service.prompt(session_id, "write the note", "req_1")
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=10.0)
            assert terminal.status.value == "succeeded"

            # store: the tool part folded cancelled / not_started, the second
            # generation still produced its closing text
            page = await app.service.messages(session_id, limit=10)
            assert [message.type for message in page.items] == ["user", "assistant", "assistant", "idle"]
            assistants = [message for message in page.items if isinstance(message, AssistantMessage)]
            idles = [message for message in page.items if isinstance(message, IdleMessage)]
            assert _assistant_text(assistants[0]) == "let me write"
            expired_parts = _tool_parts(assistants[0])
            assert [part.name for part in expired_parts] == ["fs.write"]
            assert [part.state for part in expired_parts] == ["cancelled"]
            assert [part.effect_status for part in expired_parts] == ["not_started"]
            assert _assistant_text(assistants[1]) == "gave up on the write"
            assert [message.outcome for message in idles] == ["succeeded"]

            # wire: the cancelled result fed back to the second generation
            assert len(server.requests) == 2
            second_request = _wire_messages(server.requests[1].body)
            results = _wire_tool_messages(second_request)
            assert [message.get("tool_call_id") for message in results] == ["call_write"]
            assert "the approval timed out" in _content_str(results[0].get("content"))
    finally:
        await server.aclose()

    # no approval means no execution: the side effect never happened
    assert not (tmp_path / "note.txt").exists()

    # the ledger: asked once, never replied, closed expired; the tool terminal
    # is cancelled / not_started
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.permission.requested") == 1
    assert event_types.count("py.permission.replied") == 0
    assert event_types.count("py.permission.closed") == 1
    closed = _event_data(db_path, session_id, "py.permission.closed")[0]
    assert closed["outcome"] == "expired"
    finished = _event_data(db_path, session_id, "py.tool.finished")
    assert [(str(row["state"]), str(row["effect_status"])) for row in finished] == [("cancelled", "not_started")]


# ---------------------------------------------------------------------------
# ④ the post-start graceful failure: the run must reach a terminal.
# ---------------------------------------------------------------------------


async def test_post_start_graceful_failure_reaches_run_terminal(tmp_path: Path) -> None:
    """④ (T-C1-033 regression nail, the row#34 defect end to end): a scripted
    tool call that fails gracefully *after* ``py.tool.started`` (``fs__read``
    on a missing path -- the adapter's own NOT_FOUND face) folds
    ``failed/unknown`` (§2.1 L50: a started call cannot claim not_started;
    pre-fix the invoker's failed/not_started pair was rejected by the fold
    and the run wedged permanently running with no user-visible error), the
    failure text feeds back to the closing generation on the wire, and the
    run closes ``py.run.finished`` succeeded -- the
    succeeded-with-failed-tool form (§2.1: a failed tool is model-visible
    feedback, not a failed run; the test ③ cancelled/not_started run-still-
    succeeds precedent, runner.py L620-667 fold)."""
    # Same shape as ①②③: the DB and its live writer.lock live OUTSIDE
    # working_dir (deterministic-environment uniformity, not a workaround --
    # a locked sibling's rg exit 2 is tolerated since R4-S1); this nail's
    # tool is fs__read (no directory scan), so the placement is uniformity,
    # not necessity.
    db_dir = tmp_path.parent / f"{tmp_path.name}-db"
    db_dir.mkdir()
    db_path = db_dir / "e2e.db"
    _require_success(_upgrade_head(db_path))

    server = FakeOpenAIServer(
        [
            _tool_calls_response("let me read", ("call_read", "fs__read", {"path": "missing.txt"})),
            _text_response("the read failed, closing out"),
        ]
    )
    base_url = await server.start()
    provider = MaasAdapter(f"{base_url}/v1", "test-key")
    try:
        async with App(
            db_path,
            working_dir=str(tmp_path),
            provider=provider,
            tools=(FsReadAdapter(),),
            settings=_settings(),
        ) as app:
            session = await app.service.create(ConfigSnapshot.create())
            session_id = session.session_id
            run = await app.service.prompt(session_id, "read the missing note", "req_1")
            # the wedge face: pre-fix this poll never reaches a terminal
            terminal = await _await_terminal(app.service, run.run_id, timeout_s=10.0)
            assert terminal.status.value == "succeeded"

            # store: the tool part folded failed/unknown; the closing text
            # and idle(succeeded) landed
            page = await app.service.messages(session_id, limit=10)
            assert [message.type for message in page.items] == ["user", "assistant", "assistant", "idle"]
            assistants = [message for message in page.items if isinstance(message, AssistantMessage)]
            idles = [message for message in page.items if isinstance(message, IdleMessage)]
            failed_parts = _tool_parts(assistants[0])
            assert [part.name for part in failed_parts] == ["fs.read"]
            assert [part.state for part in failed_parts] == ["failed"]
            assert [part.effect_status for part in failed_parts] == ["unknown"]
            assert _assistant_text(assistants[1]) == "the read failed, closing out"
            assert [message.outcome for message in idles] == ["succeeded"]

            # wire: the failure is user-visible -- the tool result fed back
            # to the closing generation carries the diagnostic text
            assert len(server.requests) == 2
            second_request = _wire_messages(server.requests[1].body)
            results = _wire_tool_messages(second_request)
            assert [message.get("tool_call_id") for message in results] == ["call_read"]
            assert "File not found: missing.txt" in _content_str(results[0].get("content"))
    finally:
        await server.aclose()

    # durable: the (failed, unknown) terminal is on the EventLog, the call
    # really started (the fold accepted a post-start terminal), and the run
    # closed -- never the pre-fix permanently-running wedge
    finished = _event_data(db_path, session_id, "py.tool.finished")
    assert [(str(row["state"]), str(row["effect_status"])) for row in finished] == [("failed", "unknown")]
    event_types = _event_types(db_path, session_id)
    assert event_types.count("py.tool.started") == 1
    assert event_types.count("py.run.finished") == 1
