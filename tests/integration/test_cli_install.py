"""R5-S1 (T-C1-043): the installable command face — console entry + offline clean-env install smoke.

The wheel built from this checkout must carry the ``opencode`` console entry
(``[project.scripts]`` -> ``opencode.cli:main``), install into a fresh venv, and
yield a working ``opencode`` command. The single-shot and REPL faces here ride
the same fake-MAAS server and the same assertion strings as the in-process CLI
tests — the helpers are imported from ``test_cli.py`` verbatim (no new
protocol; the exit-code surface itself is already per-code tested across
test_cli.py / test_cli_r3.py / test_cli_r4.py, see the progress file mapping).

Install strategy (seat decision, progress ①, after two probes):

* Everything runs with ``--offline`` — the smoke never touches a network; the
  uv commands fail loudly instead of silently falling back to one.
* Dependencies land in the fresh venv lock-exact via
  ``UV_PROJECT_ENVIRONMENT=<venv> uv sync --offline --frozen --no-install-project
  --no-dev`` (the same lock+cache machinery ``uv run --offline`` uses), then the
  wheel installs with ``uv pip install --no-deps`` — so ``opencode`` itself
  comes *only* from the freshly built wheel (the probe verified the imported
  package resolves inside the venv, never the source tree).
* The task-book's first sketch (``--no-deps`` + a ``--system-site-packages``
  child of the project venv) was probed and rejected: stdlib venv chaining does
  not inherit a parent *venv's* site-packages, so the installed package could
  not import its dependencies. A from-network "clean machine" install is not
  constructible under the zero-network constraint (drill + ④ in the progress
  file).

Run-face note: the single-shot/REPL children run with ``cwd=REPO_ROOT`` so the
alembic step finds ``alembic.ini`` (discovery walks cwd upward; the wheel does
not ship the ini — ④ / S3 README material). If ``uv`` is missing the install
tests skip explicitly (counted in ④); the two PATH-isolation tests below need
no uv and pin the real-environment flavor of the missing-rg /
missing-interpreter prompt faces (the message faces themselves are pinned by
test_tool_static_trio.py / test_tool_bash.py seam tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencode.process import interpreter as interpreter_module
from opencode.tools.builtin import grep as grep_module
from tests.integration.test_cli import _argv, _ok_response, _ServerHost

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Windows/console vs POSIX venv layout for the installed console script.
_BIN_DIR = "Scripts" if sys.platform == "win32" else "bin"
_EXE_NAME = "opencode.exe" if sys.platform == "win32" else "opencode"


@dataclass(frozen=True)
class _InstalledFace:
    """One built wheel plus the ``opencode`` console script installed from it."""

    wheel: Path
    exe: Path


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> _InstalledFace:
    """Build the wheel offline, materialize a fresh dependency-only venv, install."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is unavailable: the offline install smoke cannot run (T-C1-043 ④)")
    base = tmp_path_factory.mktemp("s1-install")
    dist = base / "dist"
    dist.mkdir()
    build = subprocess.run(
        [uv, "build", "--offline", "--wheel", "--out-dir", str(dist)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert build.returncode == 0, f"uv build failed\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel in {dist}, got {[wheel.name for wheel in wheels]}"
    venv_dir = base / "venv"
    sync = subprocess.run(
        [uv, "sync", "--offline", "--frozen", "--no-install-project", "--no-dev"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert sync.returncode == 0, f"uv sync failed\nstdout:\n{sync.stdout}\nstderr:\n{sync.stderr}"
    venv_python = venv_dir / _BIN_DIR / ("python.exe" if sys.platform == "win32" else "python")
    install = subprocess.run(
        [uv, "pip", "install", "--offline", "--no-deps", "--python", str(venv_python), str(wheels[0])],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert install.returncode == 0, f"uv pip install failed\nstdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    exe = venv_dir / _BIN_DIR / _EXE_NAME
    assert exe.is_file(), f"the console script {exe} was not installed; is [project.scripts] in the wheel?"
    return _InstalledFace(wheel=wheels[0], exe=exe)


def _wait_for(path: Path, needle: str, timeout_s: float) -> None:
    """Poll the child's redirected stdout file until ``needle`` appears (r3 twin precedent)."""
    deadline = time.monotonic() + timeout_s
    content = ""
    while time.monotonic() < deadline:
        content = path.read_text(encoding="utf-8", errors="replace")
        if needle in content:
            return
        time.sleep(0.2)
    pytest.fail(f"{needle!r} never appeared in the child output within {timeout_s}s; got so far:\n{content}")


def test_wheel_carries_the_package_and_the_console_entry_point(installed: _InstalledFace) -> None:
    """构建面断言 (2b): the wheel lists the opencode package face and the entry point."""
    with zipfile.ZipFile(installed.wheel) as archive:
        names = archive.namelist()
        entry_points = [name for name in names if name.endswith("entry_points.txt")]
        assert len(entry_points) == 1, f"expected one entry_points.txt, got {entry_points}"
        metadata = archive.read(entry_points[0]).decode("utf-8")
    assert "opencode/__init__.py" in names
    assert "opencode/cli.py" in names
    assert "[console_scripts]" in metadata
    assert "opencode = opencode.cli:main" in metadata


def test_installed_command_help_is_zero_side_effect_exit_0(installed: _InstalledFace, tmp_path: Path) -> None:
    """命令可用 (2d): the installed ``opencode`` runs; ``--help`` exits 0, usage on stdout,
    and touches nothing in the working directory."""
    result = subprocess.run(
        [str(installed.exe), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "usage: opencode" in result.stdout
    assert not (tmp_path / "opencode.db").exists()


def test_installed_single_shot_run_succeeds_exit_0(installed: _InstalledFace, tmp_path: Path) -> None:
    """单发面 smoke (2e): mirrors test_cli.py ⑦ on the installed command — exit 0, the
    durable final line, and the succeeded terminal on stdout."""
    host = _ServerHost([_ok_response("hel", "lo")])
    base_url = host.start()
    try:
        db_path = tmp_path / "install-single-shot.db"
        result = subprocess.run(
            [str(installed.exe), *_argv(db_path, f"{base_url}/v1", "say", "hello")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        host.stop()
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "[final] hello" in result.stdout
    assert "[run] succeeded" in result.stdout


def test_installed_repl_round_trip_exits_0(installed: _InstalledFace, tmp_path: Path) -> None:
    """REPL 面 smoke (2e): prompt face -> one scripted run -> ``/exit`` — exit 0, the
    final line and the prompt both visible (r3 twin's file-redirect + polled-needle face)."""
    host = _ServerHost([_ok_response("hello")])
    base_url = host.start()
    db_path = tmp_path / "install-repl.db"
    out_path = tmp_path / "install-repl.out"
    try:
        with out_path.open("w", encoding="utf-8", errors="replace") as out:
            proc = subprocess.Popen(
                [str(installed.exe), *_argv(db_path, f"{base_url}/v1")],
                cwd=str(REPO_ROOT),
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                stdin = proc.stdin
                assert stdin is not None
                _wait_for(out_path, "opencode> ", timeout_s=60.0)
                stdin.write("hello\n")
                stdin.flush()
                _wait_for(out_path, "[run] succeeded", timeout_s=60.0)
                stdin.write("/exit\n")
                stdin.flush()
                code = proc.wait(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdin is not None:
                    proc.stdin.close()
    finally:
        host.stop()
    assert code == 0
    content = out_path.read_text(encoding="utf-8", errors="replace")
    assert "opencode> " in content
    assert "[final] hello" in content


def test_installed_cli_missing_maas_config_is_an_argparse_exit_2(installed: _InstalledFace, tmp_path: Path) -> None:
    """缺 MAAS 配置提示面 (4b): flags and both env fallbacks missing -> the explicit
    argparse diagnostic (naming the env var), exit 2, and no database side effect."""
    env = {
        key: value for key, value in os.environ.items() if key not in ("OPENCODE_PY_BASE_URL", "OPENCODE_PY_API_KEY")
    }
    # T-C1-067 fixup: pin XDG_CONFIG_HOME at a fresh empty dir so the product
    # bridge tier stays silent even on machines with a real ~/.config/opencode
    # (the product dir is always the empty tmp dir - machine-independent face).
    xdg_config_home = tmp_path / "xdg"
    xdg_config_home.mkdir()
    env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    result = subprocess.run(
        [str(installed.exe), "hello"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "--base-url is required" in result.stderr
    assert "OPENCODE_PY_BASE_URL" in result.stderr
    assert not (tmp_path / "opencode.db").exists()


def test_grep_missing_rg_under_real_path_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 rg 提示面 (4a): a real PATH carrying no rg -> the explicit discovery problem
    (the TOOL_FAILED wrapper wording is pinned by test_tool_static_trio.py)."""
    monkeypatch.setenv("PATH", str(tmp_path))
    executable, problem = grep_module._discover_rg()
    assert executable is None
    assert problem == "it was not found on PATH"


def test_bash_missing_interpreter_under_real_path_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺解释器提示面 (4c): a real PATH carrying no shell -> the explicit discovery
    problem. Unix leg not constructible: the ``/bin/bash`` absolute fallback survives
    PATH isolation (T-C1-043 ④); the wrapper wording is pinned by test_tool_bash.py."""
    if sys.platform != "win32":
        pytest.skip("unix: /bin/bash absolute fallback survives real PATH isolation (T-C1-043 ④)")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(interpreter_module, "_INTERPRETER_CACHE", {})
    resolved, problem = interpreter_module.resolve_interpreter()
    assert resolved is None
    assert problem is not None
    assert "no PowerShell interpreter was found" in problem
