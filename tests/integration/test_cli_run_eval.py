"""T-C3-014 evaluation set: the single-shot run's piped-context merge and
background-job wait.

Upstream ``opencode run`` (cli/src/run) reads stdin on a third branch --
an argument plus a non-TTY stdin takes ``readStdinIfReady`` (a 1 s grace
read) and ``mergeInput`` joins the two -- and, after the first execution
ends, keeps the process alive while the prompt's own background jobs are
in flight (``background()`` reads job -> inbox -> active in that order,
``render.waiting`` announces a changed count, ``finalError`` folds every
waited round into the exit code). The py port (see
doc/plans/port-232-increments.md T-C3-014) lands:

* ``cli.merge_input`` + ``cli._PipedStdin.read_if_ready`` (the third
  branch only -- a TTY stdin is never read, the no-argument pipe keeps
  its one-round-per-line REPL reading) behind the ``main(stdin=)`` seam;
* ``invoker.pending_background`` / ``cancel_background`` (the in-flight
  shell settlement handles, replacing the anonymous task set) and
  ``SessionService.deliver_background_settlement`` (the upstream
  ``completeBackground`` delivery: inbox admission + idle wake, the row
  deleted on success) behind the ``ToolRegistry(background_delivery=)``
  port, switched on by ``App(deliver_background_jobs=True)`` -- the
  single-shot host only;
* the ``_run_single_shot`` wait loop (both kinds: shell settlement
  handles + unfolded ``py.subagent.backgrounded`` markers), the
  exit-code law over every waited round, SIGINT-as-cancel, and the
  opt-in ``--background-after SECONDS`` flag (shell backgrounding stays
  off by default: the production bash adapter still terminates on its
  own timeout).

BEFORE the faces land every scenario reads its baseline; each must reach
its goal AFTER -- anything else is drifting: fix the alignment, never the
reading.

Instrument notes: (1) the ``--background-after`` flag is probed first and
the shell scenarios run WITHOUT it while the parser rejects it (the
identical prompt and script, the enabling flag dropped -- the reading
carries the ``flag`` column); (2) a waited round that is not
``succeeded`` is reached through the EOF-without-terminal provider stream
(the ``interrupted`` terminal, exit 2 -- a scripted HTTP 500 maps to the
same conservative terminal, test_cli.py), so the exit-code law reads 2 on
the woken round; (3) the subagent scenario routes the fake server by
request content (the child session's calls take a delayed script), so
the parent's first round is always idle before the child settles.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from opencode import cli
from opencode.app import App
from opencode.session.service import SessionService
from opencode.tools import ToolRegistry, invoker
from tests.integration.test_background_jobs import _sleep_command
from tests.integration.test_cli import _argv, _ok_response, _ServerHost
from tests.integration.test_tool_bash import _interpreter_or_skip
from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
)

_THRESHOLD_S = "1.0"
_JOB_SLEEP_S = 3
_CHILD_DELAY_S = 3.0
_PIPED_MARKER = "PIPED-CONTEXT-MARKER"
_JOB_MARKER = "BG-DONE-MARKER"
_PARENT_MARKER = "PARENT-PROMPT-MARKER"
_CHILD_MARKER = "CHILD-TASK-MARKER"
_CHILD_ANSWER = "CHILD-ANSWER-MARKER"
_TERMINAL = frozenset({"succeeded", "failed", "interrupted"})


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ScriptedResponse:
    """One scripted generation issuing exactly one tool call (the e2e idiom)."""
    return ScriptedResponse(
        frames=[
            Frame(tool_call_start_frame(0, call_id, name)),
            Frame(tool_call_args_frame(0, json.dumps(arguments))),
            Frame(finish_frame("tool_calls")),
        ]
    )


def _interrupted_stream() -> ScriptedResponse:
    """EOF without the SSE terminal: the run lands on the ``interrupted`` terminal."""
    return ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)


def _flagged(argv: list[str]) -> tuple[list[str], str]:
    """``argv`` plus ``--background-after`` when the parser knows the flag."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            cli._parse_arguments([*argv, "--background-after", _THRESHOLD_S])
    except SystemExit:
        return list(argv), "rejected"
    return [*argv, "--background-after", _THRESHOLD_S], "accepted"


def _first_user_text(body: dict[str, Any] | None) -> str:
    for message in (body or {}).get("messages") or []:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))
    return ""


def _run_statuses(db_path: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        return [str(row[0]) for row in connection.execute("SELECT status FROM runs ORDER BY rowid").fetchall()]


def _background_rows(db_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(db_path)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM background_job").fetchone()[0])


def _terminal_with_pending_job(db_path: Path) -> bool:
    """Every run terminal while a ``background_job`` row still stands (the wait phase)."""
    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=0.2)) as connection:
            statuses = [str(row[0]) for row in connection.execute("SELECT status FROM runs").fetchall()]
            rows = int(connection.execute("SELECT COUNT(*) FROM background_job").fetchone()[0])
    except sqlite3.Error:
        return False
    return bool(statuses) and all(status in _TERMINAL for status in statuses) and rows > 0


def _drive_shell_job(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    woken: ScriptedResponse,
    name: str,
    json_output: bool = False,
    wait_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """One single-shot run: a 3 s ``bash__run`` call, the model's closing text,
    then the woken round's script (consumed only when a wake round happens)."""
    host = _ServerHost(
        [
            _tool_call("call_job", "bash__run", {"command": _sleep_command(_JOB_SLEEP_S, _JOB_MARKER)}),
            _ok_response("first round done"),
            woken,
        ]
    )
    base_url = host.start()
    try:
        db_path = tmp_path / f"{name}.db"
        base = _argv(db_path, f"{base_url}/v1", "--auto", "run the long job")
        if json_output:
            base = [*base, "--format", "json"]
        argv, flag = _flagged(base)
        started = time.monotonic()
        code = cli.main(argv, wait_hook=wait_hook)
        wall = time.monotonic() - started
    finally:
        host.stop()
    out = capsys.readouterr().out
    bodies = [json.dumps(request.body or {}) for request in host.server.requests]
    metrics: dict[str, Any] = {
        "flag": flag,
        "exit_code": code,
        "run_statuses": _run_statuses(db_path),
        "llm_requests": len(bodies),
        "output_request_index": next((index for index, body in enumerate(bodies) if _JOB_MARKER in body), None),
        "background_job_rows": _background_rows(db_path),
        "wall_s": round(wall, 2),
    }
    if json_output:
        events = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
        types: list[str] = []
        for event in events:
            if event.get("type") not in types:
                types.append(event.get("type"))
        waits = [event for event in events if event.get("type") == "background_wait"]
        metrics["json_types"] = types
        metrics["background_wait_jobs"] = waits[0].get("jobs") if waits else None
    else:
        metrics["waiting_lines"] = out.count("waiting for ")
    return metrics


# ---------------------------------------------------------------------------
# P0 -- the capability surface
# ---------------------------------------------------------------------------


def test_p0_capability(tmp_path: Path) -> None:
    """Nine pinned seams, all absent BEFORE the faces land."""
    probes: dict[str, bool] = {
        "merge_input": callable(getattr(cli, "merge_input", None)),
        "piped_stdin_reader": inspect.isclass(getattr(cli, "_PipedStdin", None)),
        "stdin_seam": "stdin" in inspect.signature(cli.main).parameters,
        "invoker_pending_background": callable(getattr(invoker, "pending_background", None)),
        "invoker_cancel_background": callable(getattr(invoker, "cancel_background", None)),
        "service_delivery": callable(getattr(SessionService, "deliver_background_settlement", None)),
        "registry_delivery_port": "background_delivery" in inspect.signature(ToolRegistry.__init__).parameters,
        "app_flag": "deliver_background_jobs" in inspect.signature(App.__init__).parameters,
    }
    probes["cli_flag"] = _flagged(_argv(tmp_path / "p0.db", "http://127.0.0.1:9/v1", "hello"))[1] == "accepted"
    status = "probed" if all(probes.values()) else "absent-partial"
    _report({"scenario": "P0-capability", "status": status, **probes})
    assert status in ("absent-partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- the piped context joins the argument (mergeInput)
# ---------------------------------------------------------------------------


def test_m1_piped_context_merges_into_the_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An argument plus a non-TTY stdin: the first user message carries both."""
    host = _ServerHost([_ok_response("ok")])
    base_url = host.start()
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO(_PIPED_MARKER))
        code = cli.main(_argv(tmp_path / "m1.db", f"{base_url}/v1", "say", "hello"))
    finally:
        host.stop()
    capsys.readouterr()
    requests = host.server.requests
    text = _first_user_text(requests[0].body) if requests else ""
    metrics = {
        "exit_code": code,
        "llm_requests": len(requests),
        "first_user_text": text,
        "piped_visible": _PIPED_MARKER in text,
        "merged": text == f"say hello\n{_PIPED_MARKER}",
    }
    _report({"scenario": "M1-merge", **metrics})
    assert code in (0, 1, 2)


# ---------------------------------------------------------------------------
# M2 -- the process waits for the prompt's own shell job
# ---------------------------------------------------------------------------


def test_m2_waits_for_the_shell_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Backgrounded call -> first round ends -> waiting -> settlement delivered ->
    woken round carries the job output. Rounds 1 -> 2."""
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("woke round done"), name="m2")
    _report({"scenario": "M2-shell-wait", **metrics})
    # The only always-true invariant: the model's turn reached the provider.
    assert metrics["llm_requests"] >= 2


def test_m2b_json_stream_announces_the_wait(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--format json``: the TS seventh event type ``background_wait`` with its job list."""
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("woke round done"), name="m2b", json_output=True)
    _report({"scenario": "M2b-json-wait", **metrics})
    assert metrics["llm_requests"] >= 2


# ---------------------------------------------------------------------------
# M3 -- the exit-code law covers every waited round (finalError)
# ---------------------------------------------------------------------------


def test_m3_exit_code_covers_the_woken_round(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A woken round that lands on ``interrupted`` moves the exit code off 0."""
    _interpreter_or_skip()
    metrics = _drive_shell_job(tmp_path, capsys, woken=_interrupted_stream(), name="m3")
    _report({"scenario": "M3-exit-code-law", **metrics})
    assert metrics["llm_requests"] >= 2


# ---------------------------------------------------------------------------
# M4 -- SIGINT during the wait cancels the job (the cancel face)
# ---------------------------------------------------------------------------


def test_m4_sigint_in_wait_cancels_the_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ctrl-C while waiting: the in-flight job is cancelled, nothing is woken."""
    _interpreter_or_skip()
    db_path = tmp_path / "m4.db"
    armed = False

    def _hook() -> None:
        # The first sight of "every run terminal + a job row standing" is the
        # first round's own last poll (the hook runs before the status read);
        # the second sight can only be the wait phase's poll.
        nonlocal armed
        if not _terminal_with_pending_job(db_path):
            return
        if not armed:
            armed = True
            return
        raise KeyboardInterrupt

    metrics = _drive_shell_job(tmp_path, capsys, woken=_ok_response("never consumed"), name="m4", wait_hook=_hook)
    _report({"scenario": "M4-sigint-cancel", "interrupt_armed": armed, **metrics})
    assert metrics["llm_requests"] >= 2


# ---------------------------------------------------------------------------
# M5 -- the subagent kind: wait for the child, then the woken round
# ---------------------------------------------------------------------------


class _RoutedServer(FakeOpenAIServer):
    """The scripted server routed by request content.

    The parent and its background child share one provider, so arrival
    order alone cannot tell their calls apart: the child's requests carry
    the task marker without the parent's prompt and take the ``child``
    queue (a delayed script -- the child settles only after the parent's
    first round is over); every other request takes the ``parent`` queue.
    """

    def __init__(self, parent: list[ScriptedResponse], child: list[ScriptedResponse]) -> None:
        super().__init__([])
        self._parent = list(parent)
        self._child = list(child)

    def _is_child(self, body: dict[str, Any] | None) -> bool:
        text = json.dumps(body or {})
        return _CHILD_MARKER in text and _PARENT_MARKER not in text

    def _next_response(self) -> ScriptedResponse:
        queue = self._child if self._is_child(self.requests[-1].body if self.requests else None) else self._parent
        return queue.pop(0) if queue else super()._next_response()

    def child_requests(self) -> int:
        return sum(1 for request in self.requests if self._is_child(request.body))


class _RoutedHost(_ServerHost):
    """The thread-owned event loop hosting a :class:`_RoutedServer`."""

    def __init__(self, parent: list[ScriptedResponse], child: list[ScriptedResponse]) -> None:
        super().__init__([])
        self.server = _RoutedServer(parent, child)


def test_m5_waits_for_the_subagent_child(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``subagent__run`` is always background: the parent's first round ends
    before the child settles; the wait loop holds the process for the woken
    round that carries the child's completion."""
    parent_script = [
        _tool_call(
            "call_child",
            "subagent__run",
            {
                "agent": "task",
                "description": "summarize the note",
                "prompt": f"{_CHILD_MARKER} Read nothing; just answer.",
            },
        ),
        _ok_response("first round done"),
        _ok_response("woke round done"),
    ]
    child_script = [
        ScriptedResponse(
            frames=[
                Frame(text_delta_frame(f"{_CHILD_ANSWER} the note is fine"), delay_s=_CHILD_DELAY_S),
                Frame(finish_frame("stop")),
            ]
        )
    ]
    host = _RoutedHost(parent_script, child_script)
    base_url = host.start()
    try:
        db_path = tmp_path / "m5.db"
        started = time.monotonic()
        code = cli.main(_argv(db_path, f"{base_url}/v1", "--auto", _PARENT_MARKER))
        wall = time.monotonic() - started
    finally:
        host.stop()
    out = capsys.readouterr().out
    server = host.server
    assert isinstance(server, _RoutedServer)
    parent_bodies = [
        json.dumps(request.body or {}) for request in server.requests if not server._is_child(request.body)
    ]
    metrics = {
        "exit_code": code,
        "run_statuses": _run_statuses(db_path),
        "parent_requests": len(parent_bodies),
        "child_requests": server.child_requests(),
        "completion_request_index": next(
            (index for index, body in enumerate(parent_bodies) if _CHILD_ANSWER in body), None
        ),
        "waiting_lines": out.count("waiting for "),
        "terminal_lines": out.count("[run] "),
        "wall_s": round(wall, 2),
    }
    _report({"scenario": "M5-subagent-wait", **metrics})
    assert len(parent_bodies) >= 2
