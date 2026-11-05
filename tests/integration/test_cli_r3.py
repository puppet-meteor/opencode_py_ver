"""R3-S7 B-group: the CLI approval face, real-SIGINT subprocess twins, closures.

Everything here drives the *public* faces added by R3-S7: the REPL approval
commands (``/approve <id> [always]`` / ``/reject <id>``) completing a pending
approval mid-run through the scripted line seam, the ``permission.rules``
engine wiring (config-file rules as the project tier, never outranking the
host tier), and the closure batch — P-2 (/history warning), P-3 (the
``[run] failed error:`` message face), non-UTF-8 console degradation, exit 3
(cancel cleanup failure) and exit 70 (migration failure) injections.

The two subprocess twins (P-1) are the only tests that deliver a *real*
signal: ``python -m opencode`` runs as a child in its own process group and
receives a genuine CTRL_C_EVENT on win32 (SIGINT elsewhere) — (a) mid-run
maps to one durable cancel intent and exit 2, (b) at the prompt face maps to
exit 130 with the process genuinely terminating (the parked ``input()``
reader is a daemon thread that is abandoned, never joined — the R2-S8 n-1
hang face). The in-process KeyboardInterrupt seams of test_cli.py remain the
race-free pins for the routing logic itself; the boundary between the two
files is documented in both module docstrings.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import pytest

from opencode.cli import _permission_error_line, main
from opencode.contracts.messages import JsonObject
from opencode.session.service import ApprovalExpiredError, RunStatus, SessionService
from opencode.storage import (
    ConflictError,
    LifecycleCleanupFailedError,
    NotFoundError,
    SessionReadOnlyError,
)
from opencode.tools.policy import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    permission_rules_from_snapshot,
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

#: CTRL_C_EVENT only exists on win32; the twin tests skip (not fail) where the
#: OS cannot deliver a real console interrupt (1314-family disclosure).
_CTRL_C_EVENT: int = getattr(signal, "CTRL_C_EVENT", signal.SIGINT)


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


def _write_tool_round(target: Path, content: str = "written") -> ScriptedResponse:
    """One scripted generation asking for an ``fs__write`` on ``target`` (the e2e tool idiom)."""
    return ScriptedResponse(
        frames=[
            Frame(tool_call_start_frame(0, "call_write", "fs__write")),
            Frame(tool_call_args_frame(0, json.dumps({"path": str(target), "content": content}))),
            Frame(finish_frame("tool_calls")),
        ]
    )


def _argv(db_path: Path, base_url: str, *prompt: str, context_limit: str = "100000") -> list[str]:
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
        context_limit,
    ]
    argv.extend(prompt)
    return argv


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _event_count(db_path: Path, event_type: str) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    return int(row[0]) if row is not None else 0


def _run_status_value(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as connection:
        row = connection.execute("SELECT status FROM runs").fetchone()
    assert row is not None
    return str(row[0])


def _latest_permission_id(db_path: Path) -> str | None:
    """The newest ``py.permission.requested`` permission_id, if the DB has one yet."""
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            row = connection.execute(
                "SELECT data FROM events WHERE type = 'py.permission.requested' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0]) if isinstance(row[0], str | bytes) else None
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    permission_id = payload.get("permission_id")
    return permission_id if isinstance(permission_id, str) else None


class _ApprovalLines:
    """Scripted REPL lines whose approval command targets the CLI's own pending permission.

    Stage 1 yields ``first`` (the run prompt). Stage 2 polls the CLI's
    database until the run's ``py.permission.requested`` event is durably
    visible, waits the commit-before-register grace (the Invoker commits the
    event *before* ``approvals.register()``, so a reply racing registration
    would classify as unknown), then yields ``reply.format(pid=...)`` — this
    is the §7 L316 mid-run approval-completes face. Every later call drains
    ``then`` (``{pid}`` formats against the same permission). A poll timeout
    raises StopIteration (EOF) with ``timed_out`` set, so a broken approval
    flow fails on assertions instead of hanging the suite.
    """

    _POLL_TIMEOUT_S = 10.0
    _REGISTER_GRACE_S = 0.1

    def __init__(self, db_path: Path, first: str, reply: str, then: Sequence[str] = ()) -> None:
        self._db_path = db_path
        self._first = first
        self._reply = reply
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
# The approval host face (§7 L316): pending line, /approve, /reject, mid-run.
# ---------------------------------------------------------------------------


def test_approve_once_completes_mid_run_and_tool_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """①once: the pending notice prints (id/action/resources + reply guidance),
    /approve completes while the run is still waiting, the tool runs, and the
    durable counts say exactly one requested + one replied (no saved decision)."""
    host = _ServerHost([_write_tool_round(tmp_path / "target.txt"), _ok_response("done")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)  # the App working_dir is the process cwd: keep the tool target inside
        db_path = tmp_path / "cli-approve-once.db"
        target = tmp_path / "target.txt"
        lines = _ApprovalLines(db_path, "write the file", "/approve {pid}", then=["/exit"])
        code = main(_argv(db_path, f"{base_url}/v1"), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "py.permission.requested never became durably visible"
    assert code == 0
    out = capsys.readouterr().out
    assert "[permission] prm_" in out
    assert "awaiting approval: /approve <id> [always] or /reject <id>" in out
    assert f"[permission] resources: {target}" in out
    assert "[approve] prm_" in out and "approved" in out
    assert "[tool]" in out and "started" in out and "succeeded" in out
    assert "[run] succeeded" in out
    assert target.read_text(encoding="utf-8") == "written"
    assert _event_count(db_path, "py.permission.requested") == 1
    assert _event_count(db_path, "py.permission.replied") == 1
    assert _event_count(db_path, "py.permission.saved") == 0


def test_approve_always_saves_decision_and_second_call_does_not_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """①always: two identical fs__write calls across two runner steps produce
    exactly one requested + one replied + one saved — the second call matches
    the saved decision (§5.2 L268) and never re-asks."""
    target = tmp_path / "target.txt"
    host = _ServerHost([_write_tool_round(target), _write_tool_round(target), _ok_response("done")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)  # the App working_dir is the process cwd: keep the tool target inside
        db_path = tmp_path / "cli-approve-always.db"
        lines = _ApprovalLines(db_path, "write the file twice", "/approve {pid} always", then=["/exit"])
        code = main(_argv(db_path, f"{base_url}/v1"), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "py.permission.requested never became durably visible"
    assert code == 0
    assert target.read_text(encoding="utf-8") == "written"
    out = capsys.readouterr().out
    assert "[approve] prm_" in out and "approved" in out
    assert _event_count(db_path, "py.permission.requested") == 1
    assert _event_count(db_path, "py.permission.replied") == 1
    assert _event_count(db_path, "py.permission.saved") == 1


def test_reject_denies_tool_and_run_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """①reject: /reject mid-run denies the tool (state ``denied``, no file), the
    run itself folds to succeeded, and the durable counts show the pair."""
    target = tmp_path / "target.txt"
    host = _ServerHost([_write_tool_round(target), _ok_response("done")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)  # the App working_dir is the process cwd: keep the tool target inside
        db_path = tmp_path / "cli-reject.db"
        lines = _ApprovalLines(db_path, "write the file", "/reject {pid}", then=["/exit"])
        code = main(_argv(db_path, f"{base_url}/v1"), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "py.permission.requested never became durably visible"
    assert code == 0
    out = capsys.readouterr().out
    assert "[approve] prm_" in out and "rejected" in out
    assert "[tool]" in out and "denied" in out
    assert "[run] succeeded" in out
    assert not target.exists()
    assert _event_count(db_path, "py.permission.requested") == 1
    assert _event_count(db_path, "py.permission.replied") == 1


def test_second_reply_is_already_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """①error mapping: a second reply to the same permission is one
    user-readable ``already resolved`` line (§7 L173 first transition wins)."""
    target = tmp_path / "target.txt"
    host = _ServerHost([_write_tool_round(target), _ok_response("done")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)  # the App working_dir is the process cwd: keep the tool target inside
        db_path = tmp_path / "cli-reject-twice.db"
        lines = _ApprovalLines(db_path, "write the file", "/reject {pid}", then=["/reject {pid}", "/exit"])
        code = main(_argv(db_path, f"{base_url}/v1"), lines=lines)
    finally:
        host.stop()
    assert not lines.timed_out, "py.permission.requested never became durably visible"
    assert code == 0
    out = capsys.readouterr().out
    assert "[approve] already resolved:" in out
    assert _event_count(db_path, "py.permission.replied") == 1


def test_unknown_permission_id_is_one_readable_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """①error mapping: an id this boot never minted is ``unknown permission id``."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-unknown-id.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["/reject prm_nope", "/exit"])
    finally:
        host.stop()
    assert code == 0
    assert "[approve] unknown permission id:" in capsys.readouterr().out


def test_permission_error_line_maps_every_store_error_to_a_label() -> None:
    """①error mapping (pure face): the four reply_permission error kinds plus
    the non-mapping fallback, each one sanitized user-readable line."""
    assert _permission_error_line(NotFoundError("permission 'prm_x' does not exist")) == (
        "[approve] unknown permission id: permission 'prm_x' does not exist"
    )
    assert _permission_error_line(ApprovalExpiredError("permission 'prm_x' already expired")) == (
        "[approve] approval expired: permission 'prm_x' already expired"
    )
    assert _permission_error_line(ConflictError("permission 'prm_x' is already deny")) == (
        "[approve] already resolved: permission 'prm_x' is already deny"
    )
    assert _permission_error_line(SessionReadOnlyError("session is read-only")) == (
        "[approve] read-only session: session is read-only"
    )
    assert _permission_error_line(RuntimeError("boom")) == "[approve] failed: boom"


# ---------------------------------------------------------------------------
# permission.rules wiring: engine invariants through the snapshot helper.
# ---------------------------------------------------------------------------


def test_permission_rules_from_snapshot_conversion_faces() -> None:
    """②assembly: every snapshot rule converts with source="project" (the tier
    is host authority, a self-declared source fails closed), unknown fields,
    non-object entries, and bad effects raise ValueError (the exit-70 backstop
    for snapshots that never passed a layer load)."""
    assert permission_rules_from_snapshot([]) == ()
    (rule,) = permission_rules_from_snapshot(
        [{"action": "fs__write", "effect": "deny", "resources": ["a.txt"], "reason": "sealed"}]
    )
    assert rule == PermissionRule(
        action="fs__write", effect="deny", source="project", resources=("a.txt",), reason="sealed"
    )
    with pytest.raises(ValueError, match="unknown field 'source'"):
        permission_rules_from_snapshot([cast("JsonObject", {"action": "x", "effect": "deny", "source": "host"})])
    with pytest.raises(ValueError, match="must be a JSON object"):
        permission_rules_from_snapshot([cast("JsonObject", "not-an-object")])
    with pytest.raises(ValueError, match="effect must be allow/ask/deny"):
        permission_rules_from_snapshot([cast("JsonObject", {"action": "x", "effect": "maybe"})])
    with pytest.raises(ValueError, match="'resources' must be a list of non-empty strings"):
        permission_rules_from_snapshot([cast("JsonObject", {"action": "x", "effect": "deny", "resources": [7]})])


_PIN_CLOCK = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _pin_policy(rules: Sequence[PermissionRule], *, host_deny: Sequence[str] = ()) -> PermissionPolicy:
    return PermissionPolicy(boot_id="boot_r3s7pins", host_deny_actions=host_deny, rules=rules, clock=lambda: _PIN_CLOCK)


def _pin_evaluate(
    policy: PermissionPolicy, action: str, resources: tuple[str, ...], working_dir: str
) -> PermissionDecision:
    return policy.evaluate(
        session_id="ses_pin00000000000001",
        action=action,
        resources=resources,
        input_hash="0" * 64,
        working_dir=working_dir,
        side_effects=True,
    )


def test_engine_project_deny_tightens_the_default_allow(tmp_path: Path) -> None:
    """③chain pin: a config-file deny beats the L266 default allow for fs__read."""
    rules = permission_rules_from_snapshot([{"action": "fs__read", "effect": "deny", "reason": "sealed"}])
    decision = _pin_evaluate(_pin_policy(rules), "fs__read", ("a.txt",), str(tmp_path))
    assert (decision.effect, decision.source) == ("deny", "rule:project")


def test_engine_host_ask_outranks_project_allow_for_bash(tmp_path: Path) -> None:
    """③chain pin (§5.2 L266): a host-tier ask survives a project allow — the
    project tier never loosens the host tier, through the same assembled rules."""
    project = permission_rules_from_snapshot([{"action": "bash__run", "effect": "allow"}])
    host = PermissionRule(action="bash__run", effect="ask", source="host")
    decision = _pin_evaluate(_pin_policy((host, *project)), "bash__run", ("ls",), str(tmp_path))
    assert (decision.effect, decision.source) == ("ask", "rule:host")


def test_engine_project_allow_cannot_loosen_root_restriction(tmp_path: Path) -> None:
    """③chain pin: an escaping fs resource is denied before any rule matches."""
    working = tmp_path / "wd"
    working.mkdir()
    rules = permission_rules_from_snapshot([{"action": "fs__write", "effect": "allow"}])
    decision = _pin_evaluate(_pin_policy(rules), "fs__write", (str(tmp_path / "outside.txt"),), str(working))
    assert (decision.effect, decision.source) == ("deny", "root_restriction")


def test_engine_project_allow_cannot_loosen_host_hard_deny(tmp_path: Path) -> None:
    """③chain pin: the host hard-deny action set wins over a project allow."""
    rules = permission_rules_from_snapshot([{"action": "fs__write", "effect": "allow"}])
    decision = _pin_evaluate(_pin_policy(rules, host_deny=("fs__write",)), "fs__write", ("a.txt",), str(tmp_path))
    assert (decision.effect, decision.source) == ("deny", "host_deny")


def test_config_deny_rule_blocks_fs_write_without_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """②end to end: a config-file ``permission.rules`` deny reaches the live
    engine through the CLI assembly — the tool is denied with *no* approval
    request (policy deny never asks), the run still succeeds, exit 0."""
    target = tmp_path / "target.txt"
    host = _ServerHost([_write_tool_round(target), _ok_response("done")])
    base_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        config = _write_json(
            tmp_path / "global.json",
            {
                "model": "fake-model",
                "context_limit": 100000,
                "permission": {"rules": [{"action": "fs__write", "effect": "deny", "reason": "no writes here"}]},
            },
        )
        db_path = tmp_path / "cli-rules-deny.db"
        code = main(
            [
                "--db",
                str(db_path),
                "--base-url",
                f"{base_url}/v1",
                "--api-key",
                "r0-test-key",
                "--config",
                str(config),
                "write",
                "the",
                "file",
            ]
        )
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "[tool]" in out and "denied" in out
    assert "[run] succeeded" in out
    assert not target.exists()
    assert _event_count(db_path, "py.permission.requested") == 0
    assert _run_status_value(db_path) == "succeeded"


@pytest.mark.parametrize(
    "rule",
    [
        {"effect": "deny"},  # action missing
        {"action": "fs__write"},  # effect missing
        {"action": "fs__write", "effect": "maybe"},  # effect outside the vocabulary
        {"action": "", "effect": "deny"},  # empty action
        {"action": "fs__write", "effect": "deny", "source": "host"},  # self-declared tier
        {"action": "fs__write", "effect": "deny", "bogus": 1},  # unknown field
        {"action": "fs__write", "effect": "deny", "resources": "a.txt"},  # resources not a list
        {"action": "fs__write", "effect": "deny", "resources": [""]},  # empty resource entry
        {"action": "fs__write", "effect": "deny", "reason": 5},  # reason not a string
        "not-an-object",  # entry not a JSON object
    ],
)
def test_bad_rule_shape_fails_at_load_time_exit_2(
    rule: dict[str, Any] | str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """②load-time face (必答②): a malformed rule entry is config content —
    the layer load fails with the file path in the message on the argparse
    exit-2 face, exactly like every other unknown config field."""
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / "opencode.json", {"permission": {"rules": [rule]}})
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "hello",
                "--db",
                str(tmp_path / "x.db"),
                "--base-url",
                "http://127.0.0.1:9/v1",
                "--api-key",
                "k",
                "--model",
                "m",
                "--context-limit",
                "100000",
            ]
        )
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# P-1 twins: real SIGINT in a real subprocess.
# ---------------------------------------------------------------------------


def _send_ctrl_c(pid: int) -> None:
    """Deliver a real SIGINT to the child (win32: CTRL_C_EVENT into its own
    process group; the parent ignores the event around the injection)."""
    if sys.platform == "win32":
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                os.kill(pid, _CTRL_C_EVENT)
            except (OSError, ValueError):
                pytest.skip("CTRL_C_EVENT delivery unavailable in this environment (winerror 1314 family)")
            time.sleep(0.2)
        finally:
            signal.signal(signal.SIGINT, previous)
    else:
        try:
            os.kill(pid, signal.SIGINT)
        except (OSError, ValueError):
            pytest.skip("SIGINT delivery unavailable in this environment")


def _spawn_cli(argv: list[str], stdout: IO[str]) -> subprocess.Popen[str]:
    """``python -m opencode`` as a real child: a new process group on win32 so
    the parent can target it with a real CTRL_C_EVENT (the CLI re-enables its
    own Ctrl-C processing at main() start), sharing the parent's console."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        [sys.executable, "-m", "opencode", *argv],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def _reap(proc: subprocess.Popen[str]) -> None:
    """Never leave a twin alive behind a failure: kill, wait, close the pipe."""
    if proc.poll() is None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
    if proc.stdin is not None:
        proc.stdin.close()


def _wait_for_run_status(db_path: Path, wanted: str, timeout_s: float = 60.0) -> None:
    """Poll the child's own DB until its run reaches ``wanted`` (migration and
    session creation included); connect-per-poll so the child never blocks."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if db_path.exists():
            try:
                with closing(sqlite3.connect(db_path)) as connection:
                    row = connection.execute("SELECT status FROM runs").fetchone()
                if row is not None and str(row[0]) == wanted:
                    return
            except sqlite3.Error:
                pass  # not migrated yet, or transiently locked by the child
        time.sleep(0.1)
    pytest.fail(f"no run reached status {wanted!r} within {timeout_s}s (migration/child too slow or broken)")


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


def test_subprocess_mid_run_ctrl_c_cancels_and_exits_2(tmp_path: Path) -> None:
    """P-1(a): a real console interrupt while the run waits becomes one durable
    cancel intent; the CLI keeps waiting and exits 2 with the run durably
    interrupted.

    The stream is scripted to end (EOF without [DONE]) ~3 s in, *after* the
    signal: the Windows proactor cannot abort an in-flight overlapped socket
    read, so a cancelling run folds when the stream ends (here the scripted
    EOF; in production the request timeout) — the CLI never stops waiting in
    between (④ disclosure).
    """
    host = _ServerHost([ScriptedResponse(frames=[Frame(text_delta_frame("partial"), delay_s=3.0)], omit_done=True)])
    base_url = host.start()
    db_path = tmp_path / "twin-a.db"
    out_path = tmp_path / "twin-a.out"
    try:
        with out_path.open("w", encoding="utf-8", errors="replace") as out:
            proc = _spawn_cli(_argv(db_path, f"{base_url}/v1", "hello"), out)
            try:
                _wait_for_run_status(db_path, "running")
                _send_ctrl_c(proc.pid)
                try:
                    code = proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pytest.fail("mid-run Ctrl-C twin: the CLI did not exit within 30s")
            finally:
                _reap(proc)
    finally:
        host.stop()
    assert code == 2
    assert _run_status_value(db_path) == "interrupted"
    assert _event_count(db_path, "py.run.cancel_requested") == 1


def test_subprocess_prompt_face_ctrl_c_exits_130(tmp_path: Path) -> None:
    """P-1(b): a real console interrupt at the prompt face exits 130 and the
    process *genuinely terminates* — the parked ``input()`` reader is a daemon
    thread abandoned at exit, never joined (the R2-S8 n-1 join-hang face)."""
    host = _ServerHost([])
    base_url = host.start()
    db_path = tmp_path / "twin-b.db"
    out_path = tmp_path / "twin-b.out"
    try:
        with out_path.open("w", encoding="utf-8", errors="replace") as out:
            proc = _spawn_cli(_argv(db_path, f"{base_url}/v1"), out)
            try:
                _wait_for_output(out_path, "opencode> ")
                _send_ctrl_c(proc.pid)
                try:
                    code = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    content = out_path.read_text(encoding="utf-8", errors="replace")
                    pytest.fail(f"prompt-face Ctrl-C twin: the process hung (the join face); output so far:\n{content}")
            finally:
                _reap(proc)
    finally:
        host.stop()
    assert code == 130
    assert "^C" in out_path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Closure batch: P-2, P-3, non-UTF-8 console, exit 3, exit 70.
# ---------------------------------------------------------------------------


def test_history_non_numeric_argument_warns_and_keeps_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """P-2 (row #2): /history with a non-numeric limit warns once and keeps the
    default instead of erroring.

    Open point 6 (T-B-033 §6-6), made explicit: this injection is also the
    pump dead-lock regression pin — "/history abc" arrives *mid-run* through
    ``_pump_input``, whose ``include_pushback=False`` read + ``push_back``
    deferral is exactly the fixed face; a regression there parks the line in
    the wrong queue and this test dead-locks at the next prompt read.
    """
    host = _ServerHost([_ok_response("hello")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-p2.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["hi", "/history abc", "/exit"])
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "[history] limit must be an integer; ignoring 'abc'" in out
    assert "[assistant.text] hello" in out  # the default limit still served the history


def test_failed_run_error_line_carries_message_not_repr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """P-3 (row #3): the [run] failed line carries the CoreError message; the
    raw model repr never reaches the console."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-p3.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello", context_limit="4097"))
    finally:
        host.stop()
    assert code == 1
    out = capsys.readouterr().out
    assert "[run] failed error:" in out
    assert "CoreError(" not in out


def test_non_utf8_console_degrades_lossily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Row #17: model text a legacy console codec cannot encode degrades to
    ``?`` instead of crashing the run (main() reconfigures errors="replace")."""
    host = _ServerHost([_ok_response("héllo")])
    base_url = host.start()
    buffer = io.BytesIO()
    replacement = io.TextIOWrapper(buffer, encoding="ascii", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", replacement)
    try:
        db_path = tmp_path / "cli-utf8.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello"))
    finally:
        host.stop()
    replacement.flush()
    assert code == 0
    assert b"h?llo" in buffer.getvalue()


def test_cancel_cleanup_failure_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row #16: a LifecycleCleanupFailedError from cancel is the honest exit 3
    (_CLEANUP_EXIT_CODE), with the failure line on stderr — never a silent 0."""
    host = _ServerHost(
        [ScriptedResponse(frames=[Frame(text_delta_frame("partial")), Frame(finish_frame("stop"), delay_s=1.0)])]
    )
    base_url = host.start()

    async def _failing_cancel(self: SessionService, run_id: str) -> RunStatus:
        raise LifecycleCleanupFailedError("simulated cleanup failure")

    monkeypatch.setattr(SessionService, "cancel", _failing_cancel)
    fired: list[bool] = []

    def wait_hook() -> None:
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt

    try:
        db_path = tmp_path / "cli-exit3.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["hello"], wait_hook=wait_hook)
    finally:
        host.stop()
    assert code == 3
    assert "cleanup failed" in capsys.readouterr().err


def test_migration_failure_maps_to_exit_70(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Row #15 (merged with the exit-70 face): an un-runnable database path
    fails the alembic migration step honestly — exit 70, diagnostic on stderr."""
    db_path = tmp_path / "missing-parent" / "x.db"
    code = main(_argv(db_path, "http://127.0.0.1:9/v1", "hello"))
    assert code == 70
    assert "database migration failed" in capsys.readouterr().err


def test_resolve_phase_config_error_maps_to_exit_70(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Row #33 (nit-2): a shape-valid config whose *resolved* values are illegal
    (context_limit <= output_reserve, the RuntimeSettings invariant) fails in
    main's resolve phase — traceback + exit 70, the resolve-stage complement to
    the migration-stage injection above (never the failed-run exit 1)."""
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "cli-resolve-70.db"
    config = _write_json(
        tmp_path / "resolve.json", {"model": "fake-model", "context_limit": 100, "output_reserve": 200}
    )
    code = main(
        [
            "--db",
            str(db_path),
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--api-key",
            "r0-test-key",
            "--config",
            str(config),
        ]
    )
    assert code == 70
    assert "the resolved config is not a legal runtime face" in capsys.readouterr().err
