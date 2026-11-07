"""R4-S6 B-group: the CLI session-selection + restart-approval face.

Everything here drives the public faces added by R4-S6 (§6.2 L107):
``--continue`` selects the most recent session created in the working
directory (no match starts a fresh one), ``--session <id>`` selects one
exact session, both are mutually exclusive, and every selection refusal
is one stderr line + exit 4 (unknown id, unreadable creation row, storage
stop, or recovery refusing to resume — v1 snapshot, vanished working
directory, MCP fingerprint drift, §6.1 L302).

The restart-approval chain (T7) is the only test that needs a REAL
crash: ``python -m opencode`` runs as a REPL child in its own right, is
killed hard while a permission is pending, and a second process
(``--continue``) proves the whole §3.3/§5.2 restart story — the startup
scan closes the orphan pending permission (``permission.closed
cancelled``), the same action re-asks in the new boot (boot_id isolation:
a saved decision from the dead boot never auto-activates), and a reply
aimed at the OLD permission id is explicitly refused. The MCP composition
test (T8) proves the composition-level wiring: definitions carry the declared
env keys resolved against ``os.environ`` (plus the win32 minimal set), the
session-start spawn attempt surfaces its failure as one stderr line, and the
App lifecycle closes the manager. The full production spawn/list/call
contract lives in ``test_mcp_production.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path
from typing import IO, Any

import pytest

from opencode.app import App
from opencode.cli import main
from opencode.contracts.events import ConfigSnapshot
from opencode.mcp import McpManager, StdioServerDefinition
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


def _write_tool_round(target: Path, content: str = "written", call_id: str = "call_write") -> ScriptedResponse:
    """One scripted generation asking for an ``fs__write`` on ``target`` (the e2e tool idiom).

    ``call_id`` must stay unique across one session's whole history (§2.1
    L37) — restart tests replay several tool rounds into the SAME session.
    """
    return ScriptedResponse(
        frames=[
            Frame(tool_call_start_frame(0, call_id, "fs__write")),
            Frame(tool_call_args_frame(0, json.dumps({"path": str(target), "content": content}))),
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


def _query_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """One read query, connect-per-call, with a short retry: right after a hard
    child kill, the WAL shared-memory teardown can transiently surface as
    ``disk I/O error`` on Windows (it clears once the OS releases the dead
    process's mappings — the app's own engine copes the same way, and B's
    process re-opens the crashed database without trouble)."""
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


def _scalar(db_path: Path, statement: str) -> Any:
    rows = _query_all(db_path, statement)
    return rows[0][0] if rows else None


def _created_rows(db_path: Path) -> list[tuple[str, int, int]]:
    """Every ``py.session.created`` row as (session_id, version, created), chronological."""
    rows = _query_all(
        db_path,
        "SELECT session_id, version, created FROM events "
        "WHERE type = 'py.session.created' ORDER BY created, session_id",
    )
    return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]


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


def _wait_for_permission_count(db_path: Path, wanted: int, timeout_s: float = 60.0) -> str:
    """Poll the DB until ``wanted`` requested permissions exist; returns the newest id."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if db_path.exists():
            try:
                if _event_count(db_path, "py.permission.requested") >= wanted:
                    permission_id = _latest_permission_id(db_path)
                    if permission_id is not None:
                        return permission_id
            except sqlite3.Error:
                pass  # transiently locked by the child
        time.sleep(0.1)
    pytest.fail(f"never saw {wanted} py.permission.requested events within {timeout_s}s")


def _wait_for_output(path: Path, needle: str, timeout_s: float = 60.0) -> None:
    """Poll the child's redirected stdout file until ``needle`` appears."""
    deadline = time.monotonic() + timeout_s
    content = ""
    while time.monotonic() < deadline:
        content = path.read_text(encoding="utf-8", errors="replace")
        if needle in content:
            return
        time.sleep(0.2)
    pytest.fail(f"{needle!r} never appeared in the CLI output within {timeout_s}s; got so far:\n{content}")


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


def _seed_v1_session(db_path: Path, working_dir: str) -> str:
    """One v1-snapshot session through the real App face (the R1 minimal baseline)."""

    async def seed() -> str:
        async with App(db_path, working_dir=working_dir) as app:
            session = await app.service.create(ConfigSnapshot.create())
            return session.session_id

    return asyncio.run(seed())


def _spawn_repl(argv: list[str], stdout: IO[str], cwd: Path) -> subprocess.Popen[str]:
    """``python -m opencode`` as a real REPL child in ``cwd`` (OP-R4-3: the recorded
    working directory is this exact string, byte for byte).

    ``PYTHONUNBUFFERED=1`` because the REPL's plain prints are block-buffered
    when redirected; without it the stdout file polls below would starve.
    """
    return subprocess.Popen(
        [sys.executable, "-m", "opencode", *argv],
        cwd=str(cwd),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONUNBUFFERED": "1"},
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _reap(proc: subprocess.Popen[str]) -> None:
    """Never leave a child alive behind a failure: kill, wait, close the pipe."""
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    if proc.stdin is not None:
        proc.stdin.close()


def _single_stderr_line(captured_err: str) -> str:
    lines = [line for line in captured_err.strip().splitlines() if line.strip()]
    assert len(lines) == 1, f"the selection stop face is exactly one stderr line, got: {lines!r}"
    assert "Traceback" not in captured_err
    return lines[0]


class _ApprovalLines:
    """Scripted REPL lines whose approval command targets the CLI's own pending permission.

    Stage 1 yields ``first`` (the run prompt). Stage 2 polls the CLI's
    database until a NEW ``py.permission.requested`` event beyond
    ``after_count`` is durably visible (the restart tests replay into a
    session that already carries earlier boots' requests — a plain
    latest-id poll would grab the stale one), waits the
    commit-before-register grace, then yields ``reply.format(pid=...)``.
    Every later call drains ``then`` (``{pid}`` formats against the same
    permission). A poll timeout raises StopIteration (EOF) with ``timed_out``
    set, so a broken approval flow fails on assertions instead of hanging.
    """

    _POLL_TIMEOUT_S = 10.0
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
# T1/T2: --continue selection (§6.2 L107 first sentence + 没有则新建).
# ---------------------------------------------------------------------------


def test_continue_selects_the_latest_session_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T1 (L107 首句): two sessions created in the same working directory, then
    ``--continue`` drives the one with the greatest ``created`` (the session_id
    assertion), creating nothing new on the way."""
    host = _ServerHost([_ok_response("one"), _ok_response("two"), _ok_response("again")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        db_path = tmp_path / "cli-continue-latest.db"
        assert main(_argv(db_path, f"{base_url}/v1", "first session")) == 0
        assert main(_argv(db_path, f"{base_url}/v1", "second session")) == 0
        code = main(_argv(db_path, f"{base_url}/v1", "--continue", "hello again"))
    finally:
        host.stop()
    assert code == 0
    assert "[final] again" in capsys.readouterr().out
    rows = _created_rows(db_path)
    assert len(rows) == 2, "--continue must not create a session when one matches"
    (_session_one, _, created_one), (session_two, _, created_two) = rows
    assert created_one < created_two, "the ordering keys must be real (latest = greatest created)"
    latest_prompt_session = _scalar(
        db_path, "SELECT session_id FROM events WHERE type = 'py.prompt.accepted' ORDER BY seq DESC LIMIT 1"
    )
    assert latest_prompt_session == session_two


def test_continue_with_no_prior_session_creates_a_fresh_v3_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T2 (L107「没有则新建」): an empty database + ``--continue`` falls through to
    the fresh-session branch — a new session whose creation row is version 3
    with the explicit empty ``mcp_servers`` face (the v3 adoption pin)."""
    host = _ServerHost([_ok_response("fresh")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        db_path = tmp_path / "cli-continue-fresh.db"
        code = main(_argv(db_path, f"{base_url}/v1", "--continue", "hello"))
    finally:
        host.stop()
    assert code == 0
    assert "[final] fresh" in capsys.readouterr().out
    rows = _created_rows(db_path)
    assert len(rows) == 1
    session_id, version, _ = rows[0]
    assert version == 3
    (payload,) = _event_payloads(db_path, "py.session.created")
    assert payload["working_dir"] == str(tmp_path)
    assert session_id
    snapshot = payload["config_snapshot"]
    assert isinstance(snapshot, dict) and snapshot["mcp_servers"] == []


# ---------------------------------------------------------------------------
# T3/T4: --session selection (§4 未建 session 报 NotFound / 复用同一会话).
# ---------------------------------------------------------------------------


def test_session_unknown_id_exits_4_with_one_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3 (L107「不存在报 NotFound」): an id no creation row carries is the
    selection stop face — exit 4, one readable stderr line, no traceback, and
    no session side effects at all."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "cli-session-unknown.db"
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--session", "ses_was_never_created_01"))
    assert code == 4
    line = _single_stderr_line(capsys.readouterr().err)
    assert "ses_was_never_created_01" in line
    assert "not found" in line
    assert _scalar(db_path, "SELECT COUNT(*) FROM sessions") == 0
    assert _event_count(db_path, "py.session.created") == 0


def test_session_with_a_valid_id_resumes_without_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T4 (复用同一 session_id): ``--session <id>`` drives the existing session —
    zero new creation events, and the new prompt lands on exactly that session."""
    host = _ServerHost([_ok_response("one"), _ok_response("two")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        db_path = tmp_path / "cli-session-resume.db"
        assert main(_argv(db_path, f"{base_url}/v1", "first session")) == 0
        session_id = _created_rows(db_path)[0][0]
        code = main(_argv(db_path, f"{base_url}/v1", "--session", session_id, "second session"))
    finally:
        host.stop()
    assert code == 0
    assert "[final] two" in capsys.readouterr().out
    assert _event_count(db_path, "py.session.created") == 1
    latest_prompt_session = _scalar(
        db_path, "SELECT session_id FROM events WHERE type = 'py.prompt.accepted' ORDER BY seq DESC LIMIT 1"
    )
    assert latest_prompt_session == session_id


# ---------------------------------------------------------------------------
# T5: the three recovery-stop faces (§6.1 L302) — all exit 4, all one line.
# ---------------------------------------------------------------------------


def test_continue_stops_on_a_v1_snapshot_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T5① (v1 快照): a session whose creation row carries the R1 minimal
    snapshot has no runtime face — recovery refuses to backfill it, so the
    process stops with the selection diagnostic, never re-roots the session
    and never adds a sessions row."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "cli-v1-stop.db"
    _require_success(_upgrade_head(db_path))
    _seed_v1_session(db_path, str(tmp_path))
    assert _scalar(db_path, "SELECT COUNT(*) FROM sessions") == 1
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--continue"))
    assert code == 4
    line = _single_stderr_line(capsys.readouterr().err)
    assert "cannot resume" in line
    assert _scalar(db_path, "SELECT COUNT(*) FROM sessions") == 1
    assert _event_count(db_path, "py.session.created") == 1


def test_continue_stops_on_mcp_fingerprint_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T5② (MCP 漂移): the session was created with one MCP server declared,
    the current host declares none — the recorded v3 fingerprints drift from
    the host's ``mcp_refs`` wiring, and recovery stops instead of resuming
    under a different launch identity (§6.1 L302)."""
    host = _ServerHost([_ok_response("one")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        db_path = tmp_path / "cli-drift-stop.db"
        with_server = _write_json(
            tmp_path / "with-mcp.json",
            {
                "model": "fake-model",
                "context_limit": 100000,
                "mcp": {
                    "servers": {
                        "probe": {
                            "type": "local",
                            "command": ["definitely-not-a-real-binary"],
                            "environment": {"OPENCODE_R4_FLAG": "1"},
                        }
                    }
                },
            },
        )
        without_server = _write_json(tmp_path / "without-mcp.json", {"model": "fake-model", "context_limit": 100000})
        assert main(_argv(db_path, f"{base_url}/v1", "--config", str(with_server), "hello")) == 0
        capsys.readouterr()  # drain boot 1's own one-line MCP spawn failure (session-start spawn, P2-S0)
        assert _event_count(db_path, "py.session.created") == 1
        code = main(_argv(db_path, f"{base_url}/v1", "--config", str(without_server), "--continue"))
    finally:
        host.stop()
    assert code == 4
    _single_stderr_line(capsys.readouterr().err)
    assert _event_count(db_path, "py.session.created") == 1


def test_session_stops_when_the_recorded_working_dir_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T5③ (记录目录已删): the session's recorded working directory no longer
    exists — recovery stops (the host must reconfigure), no fresh session is
    silently created somewhere else."""
    host = _ServerHost([_ok_response("one")])
    base_url = host.start()
    try:
        gone = tmp_path / "gone"
        gone.mkdir()
        monkeypatch.chdir(gone)
        db_path = tmp_path / "cli-gone-dir.db"
        assert main(_argv(db_path, f"{base_url}/v1", "hello")) == 0
        session_id = _created_rows(db_path)[0][0]
        monkeypatch.chdir(tmp_path)
        shutil.rmtree(gone)
        code = main(_argv(db_path, f"{base_url}/v1", "--session", session_id))
    finally:
        host.stop()
    assert code == 4
    line = _single_stderr_line(capsys.readouterr().err)
    assert "working directory" in line
    assert _event_count(db_path, "py.session.created") == 1


# ---------------------------------------------------------------------------
# T6: the mutual-exclusion usage face.
# ---------------------------------------------------------------------------


def test_continue_and_session_are_mutually_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T6: ``--continue`` and ``--session`` in one command line is an argparse
    usage error (exit 2) — one resume intent per process."""
    monkeypatch.chdir(tmp_path)
    argv = [
        "--db",
        str(tmp_path / "cli-exclusive.db"),
        "--base-url",
        "http://127.0.0.1:9/v1",
        "--api-key",
        "k",
        "--model",
        "m",
        "--context-limit",
        "1000",
        "--continue",
        "--session",
        "ses_x1",
    ]
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# T7: the restart-approval chain (real crash in a child, --continue in this process).
# ---------------------------------------------------------------------------


def test_restart_chain_closes_orphan_reasks_and_refuses_the_old_permission_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T7 (验收 C 首句 + 处方 2b): a real child crashes with a permission
    pending; ``--continue`` then proves the whole restart story — the startup
    scan closes the orphan pending permission (``permission.closed
    cancelled``), the same action re-asks in the new boot (boot_id isolation:
    the dead boot's saved decision never auto-activates), and a reply aimed
    at the OLD permission id is explicitly refused as unknown."""
    alpha = tmp_path / "alpha.txt"
    beta = tmp_path / "beta.txt"
    host = _ServerHost(
        [
            _write_tool_round(alpha, call_id="call_alpha_a"),  # A round 1: asks for alpha
            _ok_response("alpha done"),  # A round 1: finish after the always-approval
            _write_tool_round(beta, call_id="call_beta_a"),  # A round 2: asks for beta, crashes pending
            _write_tool_round(alpha, call_id="call_alpha_b"),  # B: same action re-asks (boot isolation)
            _ok_response("done"),  # B: finish after the once-approval
        ]
    )
    base_url = host.start()
    db_path = tmp_path / "cli-restart.db"
    out_path = tmp_path / "cli-restart.out"
    try:
        with out_path.open("w", encoding="utf-8", errors="replace") as out_file:
            proc = _spawn_repl(_argv(db_path, f"{base_url}/v1"), out_file, tmp_path)
            try:
                assert proc.stdin is not None
                proc.stdin.write("write alpha\n")
                proc.stdin.flush()
                _wait_for_output(out_path, "awaiting approval")
                alpha_pid = _wait_for_permission_count(db_path, 1)
                proc.stdin.write(f"/approve {alpha_pid} always\n")
                proc.stdin.flush()
                _wait_for_output(out_path, "[run] succeeded")
                proc.stdin.write("write beta\n")
                proc.stdin.flush()
                beta_pid = _wait_for_permission_count(db_path, 2)
                proc.kill()  # the crash: beta pending, never resolved, no cleanup
                proc.wait(timeout=10)
            finally:
                _reap(proc)
        assert _event_count(db_path, "py.permission.requested") == 2
        assert _event_count(db_path, "py.permission.saved") == 1
        # Process B: same working directory (OP-R4-3 exact string), --continue.
        monkeypatch.chdir(tmp_path)
        lines = _ApprovalLines(
            db_path,
            "write alpha again",
            "/approve {pid}",
            after_count=2,
            then=[f"/approve {beta_pid}", "/exit"],
        )
        code = main(_argv(db_path, f"{base_url}/v1", "--continue"), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "the re-ask permission never became durably visible"
    assert code == 0
    out = capsys.readouterr().out
    assert "[approve] unknown permission id:" in out, "the old boot's permission id must be refused explicitly"
    assert alpha.read_text(encoding="utf-8") == "written"
    # The restart chain's durable truth: re-ask (3rd request), no second save,
    # two real replies, and the scan's cancelled closure for the orphan id.
    assert _event_count(db_path, "py.permission.requested") == 3
    assert _event_count(db_path, "py.permission.saved") == 1
    assert _event_count(db_path, "py.permission.replied") == 2
    cancelled = [
        payload
        for payload in _event_payloads(db_path, "py.permission.closed")
        if payload.get("permission_id") == beta_pid
    ]
    assert cancelled and cancelled[0].get("outcome") == "cancelled"


# ---------------------------------------------------------------------------
# T8: the MCP composition (definition registration + session-start spawn, lifecycle close).
# ---------------------------------------------------------------------------


def test_mcp_composition_registers_and_spawns_definitions_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T8 (§6 L304 first sentence): one declared stdio server becomes one
    registered definition — env keys resolved against ``os.environ`` with the
    config value as fallback, plus the win32 minimal set (skipped assertions
    elsewhere) — the session-start spawn runs through the production path
    (the marker binary executes, its failure is one stderr line, not a
    traceback), and the App lifecycle closes the manager."""
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "spawned.marker"
    spawn_command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path(r'{marker}').write_text('spawned')",
    ]
    config = _write_json(
        tmp_path / "mcp-config.json",
        {
            "model": "fake-model",
            "context_limit": 100000,
            "mcp": {
                "servers": {
                    "probe": {
                        "type": "local",
                        "command": spawn_command,
                        "environment": {
                            "OPENCODE_R4_PRESENT": "from-config",
                            "OPENCODE_R4_ABSENT": "from-config",
                        },
                    }
                }
            },
        },
    )
    monkeypatch.setenv("OPENCODE_R4_PRESENT", "from-env")
    captured: list[tuple[McpManager, StdioServerDefinition]] = []
    original_add_server = McpManager.add_server

    def spy_add_server(manager: McpManager, definition: StdioServerDefinition) -> None:
        captured.append((manager, definition))
        original_add_server(manager, definition)

    monkeypatch.setattr(McpManager, "add_server", spy_add_server)
    db_path = tmp_path / "cli-mcp-compose.db"
    # The dead base URL is honest: a /exit-only REPL never touches the provider.
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "--config", str(config)), lines=["/exit"])
    assert code == 0
    assert len(captured) == 1
    manager, definition = captured[0]
    assert definition.name == "probe"
    assert definition.command == spawn_command[0]
    assert definition.args == tuple(spawn_command[1:])
    assert definition.cwd is None
    assert definition.env["OPENCODE_R4_PRESENT"] == "from-env", "os.environ wins over the declared value"
    assert definition.env["OPENCODE_R4_ABSENT"] == "from-config", "an unset key falls back to the declared value"
    if sys.platform == "win32":
        assert "SystemRoot" in definition.env
        assert "PATH" in definition.env
    assert marker.exists(), "the session-start spawn must have run the marker binary"
    # The spawn-failure face after the lifecycle close: the marker command
    # exits before speaking MCP (no handshake), so the entry is marked
    # failed, no tool face is registered, and the boot continued past it.
    captured_err = capsys.readouterr().err
    assert "MCP server 'probe' failed to start" in captured_err
    assert "Traceback" not in captured_err
    assert manager.status("probe") == "stopped"
    assert manager.tools("probe") is None
    assert manager.server_error("probe") is not None
    assert manager.closed, "the App lifecycle must have closed the injected manager"
    assert _event_count(db_path, "py.session.created") == 1
