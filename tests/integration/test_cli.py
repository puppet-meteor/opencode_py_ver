"""S8 B-group: the stdlib-only CLI over the real fake-MAAS full stack.

``main`` runs in-process with two seams: ``lines`` (a scripted line source
for the REPL) and ``wait_hook`` (called on every terminal poll -- a single
KeyboardInterrupt from it is the mid-run Ctrl-C injection). The fake server
rides a dedicated thread-owned event loop so its URL is known before main()
starts and it outlives main()'s asyncio.run. Every test here is a sync
function on purpose: main() owns the event loop.

R3-S7 boundary note: the KeyboardInterrupt injections here (line-seam,
wait_hook) are *in-process* seams — they pin the routing logic race-free but
never exercise the OS signal machinery. The real-signal faces (a genuine
CTRL_C_EVENT/SIGINT delivered to a ``python -m opencode`` subprocess, both
mid-run and at the prompt) live in test_cli_r3.py; the two files split the
axis (in-process determinism vs subprocess realism) on purpose.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from opencode.cli import escape_for_display, main
from opencode.storage import open_database
from opencode.storage.repos import ReadRepositories
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class _ServerHost:
    """Dedicated-thread event loop hosting the fake server across main() runs."""

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


class _InterruptingLines:
    """Scripted line source that simulates Ctrl-C at the very first prompt."""

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        raise KeyboardInterrupt


def _ok_response(*deltas: str) -> ScriptedResponse:
    return ScriptedResponse(frames=[Frame(text_delta_frame(delta)) for delta in deltas] + [Frame(finish_frame("stop"))])


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


async def _assert_union_projection(db_path: Path, session_id: str) -> None:
    """Reader-side verification of the projected rows through ReadRepositories."""
    async with open_database(db_path, mode="create") as database:
        page = await ReadRepositories(database.engine).messages(session_id, limit=10)
    assert [message.type for message in page.items] == ["user", "assistant", "idle"]


def test_single_shot_e2e_exit_0_and_projection_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """①: full stack e2e -- fake server over real HTTP -> MaasAdapter -> App ->
    CLI argv; exit 0, escaped text on stdout, assistant+idle rows in the DB."""
    host = _ServerHost([_ok_response("hel", "lo")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-e2e.db"
        code = main(_argv(db_path, f"{base_url}/v1", "say", "hello"))
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "hello" in out
    assert "[final] hello" in out
    assert "[run] succeeded" in out
    with sqlite3.connect(db_path) as connection:
        session_row = connection.execute("SELECT session_id FROM sessions LIMIT 1").fetchone()
    assert session_row is not None
    asyncio.run(_assert_union_projection(db_path, session_row[0]))


def test_failed_run_maps_to_exit_1(tmp_path: Path) -> None:
    """②a: a context-budget failure (§7 L339 host-configured:
    ``--context-limit 4097`` leaves 1 available token after the default 4096
    output reserve, so the assembled request cannot fit) fails the run
    durably; the single-shot exit code is 1. A scripted HTTP 500 instead maps
    to the §8 conservative INTERRUPTED terminal, not FAILED (progress ③)."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-failed.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello", context_limit="4097"))
    finally:
        host.stop()
    assert code == 1


def test_host_config_error_maps_to_exit_70(tmp_path: Path) -> None:
    """②c: a host-config contradiction (``--context-limit 1`` cannot exceed
    the 4096 output reserve, context.py L187) is a construction-time
    ValueError -> traceback + exit 70 (#94: non-0/1/2 failures never
    masquerade as a failed run's exit 1)."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-config-error.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello", context_limit="1"))
    finally:
        host.stop()
    assert code == 70


def test_interrupted_run_maps_to_exit_2(tmp_path: Path) -> None:
    """②b: an EOF-without-terminal provider stream (§8 conservative terminal)
    interrupts the run; the single-shot exit code is 2."""
    host = _ServerHost([ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-interrupted.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello"))
    finally:
        host.stop()
    assert code == 2


def test_repl_mid_run_interrupt_returns_to_prompt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """③: one KeyboardInterrupt during the terminal wait maps to one durable
    cancel intent; the run ends interrupted and the REPL returns to the prompt
    (/exit afterwards still exits 0)."""
    host = _ServerHost(
        [ScriptedResponse(frames=[Frame(text_delta_frame("partial")), Frame(finish_frame("stop"), delay_s=1.0)])]
    )
    base_url = host.start()
    fired: list[bool] = []

    def wait_hook() -> None:
        if not fired:  # inject Ctrl-C exactly once, at the first poll
            fired.append(True)
            raise KeyboardInterrupt

    try:
        db_path = tmp_path / "cli-repl-cancel.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["hello", "/exit"], wait_hook=wait_hook)
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    # No assertion on the streamed preview here: whether the first delta
    # prints before the runner observes the cancel is scheduler-dependent
    # (the cancel can win and abort before the provider request); the
    # preview face is pinned race-free by the delta/final override test.
    assert "[run] interrupted" in out
    with sqlite3.connect(db_path) as connection:
        cancel_row = connection.execute("SELECT COUNT(*) FROM events WHERE type = 'py.run.cancel_requested'").fetchone()
        status_row = connection.execute("SELECT status FROM runs").fetchone()
    assert cancel_row is not None
    assert cancel_row[0] == 1
    assert status_row is not None
    assert status_row[0] == "interrupted"


def test_repl_prompt_face_interrupt_maps_to_130(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ctrl-C at the input prompt face (injected through the line seam) exits 130."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-repl-ki.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=_InterruptingLines())
    finally:
        host.stop()
    assert code == 130
    assert "^C" in capsys.readouterr().out


def test_escape_replaces_control_sequences_in_stream(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """④: model text carrying ESC/CR/NUL never reaches stdout raw; every
    dangerous control became U+FFFD while the visible text survives."""
    host = _ServerHost([_ok_response("before\x1b[31mred\x1b[0m\r\x00after")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-escape.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello"))
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\r" not in out
    assert "\x00" not in out
    assert "\ufffd" in out
    assert "before" in out
    assert "after" in out


def test_escape_for_display_replaces_c0_del_and_c1() -> None:
    """④ (pure face): C0 except \\t/\\n, DEL, and C1 all map to U+FFFD."""
    assert escape_for_display("a\x1b[2Jb\rc\nd\te\x00f\x7fg\u0080h") == "a\ufffd[2Jb\ufffdc\nd\te\ufffdf\ufffdg\ufffdh"


def test_history_shows_union_order_after_one_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """⑤: /history after one run shows the user + assistant + idle full order
    through the union face."""
    host = _ServerHost([_ok_response("hello there")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-history.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["hi", "/history", "/exit"])
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "[user] hi" in out
    assert "[assistant.text] hello there" in out
    assert "[idle] succeeded" in out


def test_history_on_empty_session_notices_without_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """⑤: /history on a fresh session prints a notice, not an error."""
    host = _ServerHost([])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-history-empty.db"
        code = main(_argv(db_path, f"{base_url}/v1"), lines=["/history", "/exit"])
    finally:
        host.stop()
    assert code == 0
    assert "no messages yet" in capsys.readouterr().out


def test_durable_override_shows_delta_preview_and_final(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """⑥: the streamed delta preview fragments and the durable finalized full
    value are both visible (§4 L244/L246 preview -> override, distinguishable)."""
    host = _ServerHost([_ok_response("hel", "lo")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-override.db"
        code = main(_argv(db_path, f"{base_url}/v1", "hello"))
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "hello" in out  # the inline delta preview (hel + lo)
    assert "[final] hello" in out  # the durable full value on its own line


def test_subprocess_entry_smoke(tmp_path: Path) -> None:
    """⑦: ``python -m opencode`` in a real subprocess -- pins the __main__
    wiring and the alembic migration step actually running."""
    host = _ServerHost([_ok_response("smoke")])
    base_url = host.start()
    try:
        db_path = tmp_path / "cli-smoke.db"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "opencode",
                "hello",
                "--db",
                str(db_path),
                "--base-url",
                f"{base_url}/v1",
                "--api-key",
                "r0-test-key",
                "--model",
                "fake-model",
                "--context-limit",
                "100000",
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        host.stop()
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "[run] succeeded" in result.stdout


def test_argparse_missing_required_args_exit_2() -> None:
    """⑧: missing --model or --context-limit is a standard argparse exit 2."""
    with pytest.raises(SystemExit) as excinfo:
        main(["hello", "--db", "unused.db", "--base-url", "http://127.0.0.1:9", "--api-key", "k"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "hello",
                "--db",
                "unused.db",
                "--base-url",
                "http://127.0.0.1:9",
                "--api-key",
                "k",
                "--model",
                "m",
            ]
        )
    assert excinfo.value.code == 2
