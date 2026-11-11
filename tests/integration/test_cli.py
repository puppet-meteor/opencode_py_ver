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
import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from opencode.cli import _parse_arguments, escape_for_display, main
from opencode.config import ConfigLoader
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


# ---------------------------------------------------------------------------
# T-C1-049: the product config bridge CLI face — no explicit
# --base-url/--api-key/--model needed when the TS product config exists.
# Every test injects a tmp product config via XDG_CONFIG_HOME (the production
# directory resolution) — the real ~/.config/opencode is never read.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _hermetic_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the ambient credential/provider env so the bridge tests are deterministic."""
    monkeypatch.delenv("OPENCODE_PY_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_PY_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_PY_PROVIDER", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _inject_product_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, base_url: str, api_key: str, model: str
) -> Path:
    """Inject a tmp TS product config (nested gateway shape) under a tmp XDG home."""
    xdg_home = tmp_path / "xdg-home"
    config_dir = xdg_home / "opencode"
    config_dir.mkdir(parents=True)
    (config_dir / "opencode.json").write_text(
        json.dumps(
            {"provider": {"gateway": {"options": {"baseURL": base_url, "apiKey": api_key}, "models": {model: {}}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))
    return config_dir


def _global_with(tmp_path: Path, values: dict[str, object]) -> Path:
    """A tmp -py global config supplying whatever the product tier cannot (context_limit)."""
    global_path = tmp_path / "global.json"
    global_path.write_text(json.dumps(values), encoding="utf-8")
    return global_path


def _no_product_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the product dir at an empty tmp XDG home — the real ~/.config/opencode is never read."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg-home"))


def test_parse_arguments_runs_three_flag_free_on_product_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _hermetic_cli_env: None
) -> None:
    """T-C1-049 CLI face: with a product gateway config present, none of
    --base-url/--api-key/--model is required — credentials backfill onto args
    and the model rides the merged values (args.model stays None; the resolve
    face reads it from the layer)."""
    monkeypatch.chdir(tmp_path)
    _inject_product_config(
        monkeypatch, tmp_path, base_url="https://product-gw.test/v1", api_key="r0-product-key", model="product-model"
    )
    global_path = _global_with(tmp_path, {"context_limit": 100000})
    args = _parse_arguments(["--db", str(tmp_path / "x.db"), "--config", str(global_path)])
    assert args.base_url == "https://product-gw.test/v1"
    assert args.api_key == "r0-product-key"
    assert args.model is None  # the model arrives via the merged values, not args
    values = ConfigLoader(global_config=global_path).load().values  # same env: the product tier carries the model
    assert values["model"] == "product-model"


def test_explicit_flags_beat_the_product_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _hermetic_cli_env: None
) -> None:
    """Precedence 1/3: the CLI flag overrides the product tier values."""
    monkeypatch.chdir(tmp_path)
    _inject_product_config(
        monkeypatch, tmp_path, base_url="https://product-gw.test/v1", api_key="r0-product-key", model="product-model"
    )
    global_path = _global_with(tmp_path, {"context_limit": 100000})
    args = _parse_arguments(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(global_path),
            "--base-url",
            "https://flag-url.test/v1",
            "--api-key",
            "r0-flag-key",
            "--model",
            "flag-model",
        ]
    )
    assert args.base_url == "https://flag-url.test/v1"
    assert args.api_key == "r0-flag-key"
    assert args.model == "flag-model"


def test_env_beats_the_product_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _hermetic_cli_env: None) -> None:
    """Precedence 2/3: the OPENCODE_PY_* env vars override the product tier values."""
    monkeypatch.chdir(tmp_path)
    _inject_product_config(
        monkeypatch, tmp_path, base_url="https://product-gw.test/v1", api_key="r0-product-key", model="product-model"
    )
    global_path = _global_with(tmp_path, {"context_limit": 100000})
    monkeypatch.setenv("OPENCODE_PY_BASE_URL", "https://env-url.test/v1")
    monkeypatch.setenv("OPENCODE_PY_API_KEY", "r0-env-key")
    args = _parse_arguments(["--db", str(tmp_path / "x.db"), "--config", str(global_path)])
    assert args.base_url == "https://env-url.test/v1"
    assert args.api_key == "r0-env-key"


def test_global_endpoint_without_api_key_names_api_key_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_cli_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The partial-values face: a global config carrying 'endpoint' only (the
    product bridge itself is all-or-nothing) resolves base_url and then fails
    naming exactly the missing credential — api_key, never a value read."""
    monkeypatch.chdir(tmp_path)
    _no_product_config(monkeypatch, tmp_path)  # no product tier: only the global endpoint is present
    global_path = _global_with(tmp_path, {"endpoint": "https://from-global.test/v1"})
    with pytest.raises(SystemExit) as excinfo:
        _parse_arguments(["--config", str(global_path)])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--api-key is required" in err
    assert "OPENCODE_PY_API_KEY" in err
    assert "--base-url is required" not in err  # the endpoint resolved; only api_key is missing
    assert "https://from-global.test/v1" not in err  # zero value echo


def test_product_credentials_never_echo_on_the_error_face(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_cli_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The zero-echo pin: the bridge READ the credentials and a later
    completeness error fires — nothing read may appear anywhere in the output."""
    monkeypatch.chdir(tmp_path)
    _inject_product_config(
        monkeypatch, tmp_path, base_url="https://product-gw.test/v1", api_key="r0-never-echo", model="product-model"
    )
    # No context_limit anywhere: the context-limit completeness error fires AFTER
    # the credentials resolved from the product tier.
    with pytest.raises(SystemExit) as excinfo:
        _parse_arguments(["--db", str(tmp_path / "x.db")])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "r0-never-echo" not in captured.out
    assert "r0-never-echo" not in captured.err


def test_cli_runs_e2e_three_flag_free_on_product_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_cli_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() unchanged: the product tier flows through BOTH loader calls
    (parse-time completeness and the resolve face) — a full single-shot round
    with no --base-url/--api-key/--model flags, and the product model reaching
    the durable v3 snapshot."""
    host = _ServerHost([_ok_response("hello")])
    server_url = host.start()
    try:
        monkeypatch.chdir(tmp_path)
        _inject_product_config(
            monkeypatch, tmp_path, base_url=f"{server_url}/v1", api_key="r0-e2e-product-key", model="fake-model"
        )
        global_path = _global_with(tmp_path, {"context_limit": 100000})
        code = main(["--db", str(tmp_path / "flag-free.db"), "--config", str(global_path), "say", "hello"])
    finally:
        host.stop()
    assert code == 0
    out = capsys.readouterr().out
    assert "[final] hello" in out
    assert "[run] succeeded" in out
    assert "r0-e2e-product-key" not in out  # credentials never reach stdout
    with sqlite3.connect(tmp_path / "flag-free.db") as connection:
        row = connection.execute("SELECT data FROM events WHERE type = 'py.session.created'").fetchone()
    assert row is not None
    raw = row[0]
    payload = json.loads(raw) if isinstance(raw, str | bytes) else raw
    assert payload["config_snapshot"]["model"]["model"] == "fake-model"  # the product tier's model, durably
    assert "r0-e2e-product-key" not in json.dumps(payload)  # zero-echo on the durable face too
