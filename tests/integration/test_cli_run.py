"""T-C3-014 red set: the single-shot run's piped-context merge and background wait.

Upstream anchors (2.3.2): cli/src/run/run.ts ``mergeInput`` (neither input
-> undefined, one -> that one, both -> joined by a newline) and
``readStdinIfReady`` (a TTY is never read; data arriving inside the grace
window is read to EOF, silence past it yields undefined); cli/src/run/
noninteractive.ts (``background()`` reads job -> inbox -> active,
``render.waiting`` announces a changed count, the JSON ``background_wait``
line carries the job list, ``finalError`` folds every waited round into the
exit code, Ctrl-C cancels the in-flight jobs).

The py faces under test (doc/plans/port-232-increments.md T-C3-014):
``cli.merge_input`` / ``cli._PipedStdin`` behind ``main(stdin=)``, the
``_run_single_shot`` wait loop, the exit-code law, SIGINT-as-cancel and the
opt-in ``--background-after``. The scenarios share the evaluation set's
drivers (tests/integration/test_cli_run_eval.py) and assert the AFTER goals
the evaluation set only measures.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from opencode import cli
from opencode.app import App
from tests.integration import test_cli_run_eval as eval_module
from tests.integration.test_cli import _argv, _ok_response, _ServerHost
from tests.integration.test_cli_run_eval import (
    _CHILD_ANSWER,
    _CHILD_MARKER,
    _PARENT_MARKER,
    _PIPED_MARKER,
    _drive_shell_job,
    _first_user_text,
    _RoutedHost,
    _RoutedServer,
    _run_statuses,
    _terminal_with_pending_job,
    _tool_call,
)
from tests.integration.test_tool_bash import _interpreter_or_skip
from tests.r0.fake_openai_server import Frame, ScriptedResponse, finish_frame, text_delta_frame

_WAITING_LINE = "waiting for 1 background job(s)"


# ---------------------------------------------------------------------------
# mergeInput -- the four branches
# ---------------------------------------------------------------------------


def test_merge_input_joins_the_argument_and_the_piped_context() -> None:
    assert cli.merge_input(None, None) is None
    assert cli.merge_input("", "") is None
    assert cli.merge_input("say hello", None) == "say hello"
    assert cli.merge_input(None, _PIPED_MARKER) == _PIPED_MARKER
    assert cli.merge_input("say hello", _PIPED_MARKER) == f"say hello\n{_PIPED_MARKER}"


# ---------------------------------------------------------------------------
# readStdinIfReady -- the piped-stdin reader
# ---------------------------------------------------------------------------


class _TtyStream:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise AssertionError("a TTY is decided before any descriptor is queried")

    def read(self) -> str:
        raise AssertionError("a TTY stdin is never read")


class _BrokenStream:
    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def read(self) -> str:
        raise OSError("stream closed underneath the reader")


def _pipe() -> tuple[io.TextIOWrapper, int]:
    """A text stream over a real pipe's read end, plus the write end's descriptor."""
    read_fd, write_fd = os.pipe()
    return io.TextIOWrapper(io.FileIO(read_fd, "r"), encoding="utf-8"), write_fd


def _write_then_close(write_fd: int, payload: bytes) -> None:
    os.write(write_fd, payload)
    os.close(write_fd)


def test_piped_stdin_reads_a_ready_pipe_to_eof() -> None:
    stream, write_fd = _pipe()
    writer = threading.Thread(target=_write_then_close, args=(write_fd, b"piped-context"), daemon=True)
    writer.start()
    try:
        assert cli._PipedStdin(stream).read_if_ready(grace=1.0) == "piped-context"
    finally:
        writer.join(timeout=5.0)
        stream.close()


def test_piped_stdin_yields_none_past_the_grace_window() -> None:
    stream, write_fd = _pipe()
    started = time.monotonic()
    try:
        assert cli._PipedStdin(stream).read_if_ready(grace=0.3) is None
        assert time.monotonic() - started < 3.0
    finally:
        # Release the abandoned reader (a daemon by design) instead of closing
        # a stream another thread is parked on.
        os.write(write_fd, b"release")
        os.close(write_fd)


def test_piped_stdin_reads_a_redirected_file_completely(tmp_path: Path) -> None:
    context = tmp_path / "context.txt"
    context.write_text("first line\nsecond line\n", encoding="utf-8")
    with context.open("r", encoding="utf-8") as stream:
        assert cli._PipedStdin(stream).read_if_ready(grace=0.3) == "first line\nsecond line\n"


def test_piped_stdin_never_reads_a_tty() -> None:
    assert cli._PipedStdin(_TtyStream()).read_if_ready(grace=0.3) is None


def test_piped_stdin_reads_a_stream_without_a_descriptor() -> None:
    assert cli._PipedStdin(io.StringIO("from-stringio")).read_if_ready(grace=1.0) == "from-stringio"


def test_piped_stdin_folds_a_read_failure_into_the_empty_context() -> None:
    assert cli._PipedStdin(_BrokenStream()).read_if_ready(grace=0.3) == ""


# ---------------------------------------------------------------------------
# --background-after -- opt-in, positive, single-shot only
# ---------------------------------------------------------------------------


def test_background_after_is_opt_in_and_must_be_positive(tmp_path: Path) -> None:
    base = _argv(tmp_path / "flags.db", "http://127.0.0.1:9/v1", "hello")
    assert cli._parse_arguments(base).background_after is None
    assert cli._parse_arguments([*base, "--background-after", "1.5"]).background_after == 1.5
    for rejected in ("abc", "0", "-1"):
        with pytest.raises(SystemExit) as excinfo:
            cli._parse_arguments([*base, "--background-after", rejected])
        assert excinfo.value.code == 2


def test_background_after_is_announced_ignored_in_the_repl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    host = _ServerHost([_ok_response("never requested")])
    base_url = host.start()
    try:
        code = cli.main([*_argv(tmp_path / "repl.db", f"{base_url}/v1"), "--background-after", "1"], lines=[])
    finally:
        host.stop()
    captured = capsys.readouterr()
    assert code == 0
    assert "--background-after" in captured.err


def test_app_delivery_flag_defaults_off() -> None:
    assert inspect.signature(App.__init__).parameters["deliver_background_jobs"].default is False


# ---------------------------------------------------------------------------
# M1 -- the piped context joins the argument
# ---------------------------------------------------------------------------


def test_m1_piped_context_merges_into_the_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ServerHost([_ok_response("ok")])
    base_url = host.start()
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO(_PIPED_MARKER))
        code = cli.main(_argv(tmp_path / "m1.db", f"{base_url}/v1", "say", "hello"))
    finally:
        host.stop()
    capsys.readouterr()
    requests = host.server.requests
    assert code == 0
    assert requests
    assert _first_user_text(requests[0].body) == f"say hello\n{_PIPED_MARKER}"


# ---------------------------------------------------------------------------
# M2/M2b -- the process waits for the prompt's own shell job
# ---------------------------------------------------------------------------


def test_m2_shell_job_is_waited_for(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("woke round done"), name="m2")
    assert metrics["flag"] == "accepted"
    assert metrics["exit_code"] == 0
    assert metrics["run_statuses"] == ["succeeded", "succeeded"]
    # Three requests: the tool-call round, the post-marker round, the woken
    # round. (output_request_index is NOT assertable here: the request
    # history echoes the tool-call arguments verbatim, so the command's
    # marker string reaches request 2 regardless of the wait.)
    assert metrics["llm_requests"] == 3
    assert metrics["waiting_lines"] >= 1
    assert metrics["background_job_rows"] == 0


def test_m2b_json_stream_announces_the_wait(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("woke round done"), name="m2b", json_output=True)
    assert metrics["flag"] == "accepted"
    assert metrics["exit_code"] == 0
    assert "background_wait" in metrics["json_types"]
    jobs = metrics["background_wait_jobs"]
    assert isinstance(jobs, list) and len(jobs) == 1
    assert {"id", "kind", "title"} <= set(jobs[0])
    assert jobs[0]["kind"] == "shell"


# ---------------------------------------------------------------------------
# M3 -- the exit-code law covers every waited round (finalError)
# ---------------------------------------------------------------------------


def test_m3_exit_code_covers_the_woken_round(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=eval_module._interrupted_stream(), name="m3")
    assert metrics["flag"] == "accepted"
    assert metrics["exit_code"] == 2
    assert metrics["run_statuses"] == ["succeeded", "interrupted"]
    # The interrupted woken round still consumed its provider turn (the
    # third request); see M2 for why output_request_index is not assertable.
    assert metrics["llm_requests"] == 3


# ---------------------------------------------------------------------------
# M4 -- SIGINT during the wait cancels the job
# ---------------------------------------------------------------------------


def test_m4_sigint_in_wait_cancels_the_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _interpreter_or_skip()
    # A job far longer than the interrupt leaves no doubt which phase the
    # KeyboardInterrupt lands in.
    monkeypatch.setattr(eval_module, "_JOB_SLEEP_S", 8)
    db_path = tmp_path / "m4.db"
    armed = False

    def _hook() -> None:
        # The first sight of "every run terminal + a job row standing" is the
        # first round's own last poll; the second sight can only be the wait
        # phase's poll.
        nonlocal armed
        if not _terminal_with_pending_job(db_path):
            return
        if not armed:
            armed = True
            return
        raise KeyboardInterrupt

    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("never consumed"), name="m4", wait_hook=_hook)
    assert armed
    assert metrics["flag"] == "accepted"
    assert metrics["exit_code"] == 2
    # Only the first round's two provider turns ever happened -- the woken
    # round was cancelled before its request (output_request_index is not
    # assertable: the tool-call arguments echo the marker into request 2).
    assert metrics["llm_requests"] == 2
    assert metrics["background_job_rows"] == 0


# ---------------------------------------------------------------------------
# M5 -- the subagent kind: wait for the child, then the woken round
# ---------------------------------------------------------------------------


def test_m5_subagent_child_is_waited_for(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    child_task = {"agent": "task", "description": "summarize the note", "prompt": f"{_CHILD_MARKER} just answer"}
    child_answer = ScriptedResponse(
        frames=[Frame(text_delta_frame(f"{_CHILD_ANSWER} the note is fine"), delay_s=3.0), Frame(finish_frame("stop"))]
    )
    host = _RoutedHost(
        [_tool_call("call_child", "subagent__run", child_task), _ok_response("first round done"), _ok_response("woke")],
        [child_answer],
    )
    base_url = host.start()
    try:
        db_path = tmp_path / "m5.db"
        code = cli.main(_argv(db_path, f"{base_url}/v1", "--auto", _PARENT_MARKER))
    finally:
        host.stop()
    out = capsys.readouterr().out
    server = host.server
    assert isinstance(server, _RoutedServer)
    parent_bodies = [
        json.dumps(request.body or {}) for request in server.requests if not server._is_child(request.body)
    ]
    assert code == 0
    assert _run_statuses(db_path) == ["succeeded"] * 3
    assert len(parent_bodies) == 3
    assert any(_CHILD_ANSWER in body for body in parent_bodies)
    assert _WAITING_LINE in out
