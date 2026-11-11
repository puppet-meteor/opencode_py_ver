"""Opt-in real-MAAS short-session evidence tests (T-C1-048, ROADMAP L62 core 0.1 gate).

Both scenarios are double-gated and skip by default (the default suite and CI
are untouched): ``OPENCODE_PY_MAAS_REAL=1`` (exactly ``1``) AND a resolvable
bridge from :mod:`tests.integration._maas_real_config` -- the two skip
reasons (opt-in not enabled / config unavailable) are written separately.

Budget choice (verified in source, ticket §2.3):
``RuntimeSettings.output_reserve`` becomes ``GenerationRequest.max_output_tokens``
verbatim (``src/opencode/session/context.py`` L154-156 and L282-284: "the
full reserve, never shrunk to fit the history") and the adapter sends it as
the wire ``max_tokens`` (``src/opencode/ai/maas.py`` L425). R0 §8.1
(``doc/evidence/r0/provider.md`` L103-108) measured generated reasoning
riding inside that same budget (``max_output_tokens=64`` fully consumed by
``reasoning=61``: zero visible text, ``finish=length``), so the real session
keeps the builtin ``output_reserve=4096`` (no ``--output-reserve`` flag) and
passes ``--context-limit 100000`` (repo precedent; the usable input budget
``100000 - 4096 - 0`` far exceeds a short session while the RuntimeSettings
invariant ``context_limit > output_reserve + safety_margin`` holds) plus
``--timeout-s 180``.

Permission face (verified in source, ticket §3.A2): the §5.2 L266 default
table auto-allows in-root ``fs__read`` with no approval
(``src/opencode/tools/policy.py`` ``_DEFAULT_EFFECTS``), while host-tier root
containment resolves fs-family resources against the session working dir --
for a fresh session, the process cwd (``src/opencode/app.py`` L185).
``--project-root`` alone cannot re-root the session: ConfigLoader rejects a
project root that is neither the working directory nor an ancestor of it
(``src/opencode/config/loader.py`` L494-501, the exit-70 face). The tool
scenario therefore ``monkeypatch.chdir``s into ``tmp_path`` and passes
``--project-root <tmp_path>`` (equal to the working directory,
validation-clean): the target file is in-root, the read auto-allows, and the
scripted run can never block on the approval face.

Assertion discipline: only structural facts (exit code, ``runs.status``,
event types/counts, non-empty text parts, the fixed marker word) -- never
raw endpoint/model/key strings. stdout is consumed via
``capsys.readouterr()`` immediately after ``main()`` returns so a failure
report can never echo captured output; the CLI print face itself echoes no
endpoint/model/key (only ``[final]``/``[tool]``/``[run]`` lines).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from opencode.cli import main
from opencode.contracts.messages import AssistantMessage, AssistantTextPart, AssistantToolPart
from opencode.storage import open_database
from opencode.storage.repos import ReadRepositories
from tests.integration._maas_real_config import (
    MaasRealConfig,
    MaasRealConfigUnavailable,
    resolve_maas_real_config,
)

pytestmark = pytest.mark.maas_real

#: Fixed non-sensitive marker word (ticket §3.A2): the target file content and
#: the token the final answer must contain.
_MARKER = "quokka-7f3a"

#: Budget/timeout values -- rationale in the module docstring.
_CONTEXT_LIMIT = "100000"
_TIMEOUT_S = "180"


def _gated_config() -> MaasRealConfig:
    """The double gate: strict opt-in AND a resolvable bridge, else skip."""
    if os.environ.get("OPENCODE_PY_MAAS_REAL") != "1":
        pytest.skip("real-MAAS opt-in not enabled (OPENCODE_PY_MAAS_REAL is not exactly 1)")
    try:
        return resolve_maas_real_config()
    except MaasRealConfigUnavailable as error:
        pytest.skip(f"real-MAAS config unavailable: {error}")


def _argv(config: MaasRealConfig, db_path: Path, *, project_root: Path | None = None) -> list[str]:
    """Assemble the in-process CLI argv from the resolved bridge values."""
    argv = [
        "--db",
        str(db_path),
        "--base-url",
        config.base_url,
        "--api-key",
        config.api_key,
        "--model",
        config.model,
        "--context-limit",
        _CONTEXT_LIMIT,
        "--timeout-s",
        _TIMEOUT_S,
    ]
    if project_root is not None:
        argv.extend(["--project-root", str(project_root)])
    return argv


def _run_status(db_path: Path) -> str:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT status FROM runs").fetchone()
    assert row is not None, "no run row was persisted"
    return str(row[0])


def _event_count(db_path: Path, event_type: str) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


async def _assistant_messages(db_path: Path) -> list[AssistantMessage]:
    """The projected assistant rows (reader face, test_cli.py precedent)."""
    with sqlite3.connect(db_path) as connection:
        session_row = connection.execute("SELECT session_id FROM sessions LIMIT 1").fetchone()
    assert session_row is not None, "no session row was persisted"
    async with open_database(db_path, mode="create") as database:
        page = await ReadRepositories(database.engine).messages(str(session_row[0]), limit=50)
    return [message for message in page.items if isinstance(message, AssistantMessage)]


def test_real_text_turn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Scenario 1 (text turn): one REPL round over the real gateway.

    Asserts exit 0, a succeeded run, and an assistant message carrying a
    non-empty text part (length > 0; content never pinned).
    """
    config = _gated_config()
    db_path = tmp_path / "maas-real-text.db"
    prompt = f"Reply in one short sentence and include the word {_MARKER} in your reply."
    code = main(_argv(config, db_path), lines=[prompt, "/exit"])
    capsys.readouterr()  # consume immediately: failure reports must not echo captured output
    assert code == 0
    assert _run_status(db_path) == "succeeded"
    assistants = asyncio.run(_assistant_messages(db_path))
    assert assistants, "no assistant message was persisted"
    texts = [part.text for message in assistants for part in message.parts if isinstance(part, AssistantTextPart)]
    assert any(len(text) > 0 for text in texts)


def test_real_tool_execution_round(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 2 (real tool-execution loop): read tool -> marker answer.

    The target file (fixed marker content) is written under ``tmp_path``; the
    test chdirs there and pins ``--project-root`` to it, so the read is
    in-root and auto-allowed (module docstring) -- the scripted run can never
    block on the approval face.
    """
    config = _gated_config()
    target = tmp_path / "maas-real-target.txt"
    target.write_text(f"marker word: {_MARKER}\n", encoding="utf-8")
    db_path = tmp_path / "maas-real-tool.db"
    monkeypatch.chdir(tmp_path)
    prompt = (
        f"First use the read tool to read the file at {target}. Then reply with the marker word that the file contains."
    )
    code = main(_argv(config, db_path, project_root=tmp_path), lines=[prompt, "/exit"])
    capsys.readouterr()
    assert code == 0
    assert _run_status(db_path) == "succeeded"
    assert _event_count(db_path, "py.tool.requested") >= 1
    assert _event_count(db_path, "py.tool.started") >= 1
    assert _event_count(db_path, "py.tool.finished") >= 1
    assistants = asyncio.run(_assistant_messages(db_path))
    tool_parts = [part for message in assistants for part in message.parts if isinstance(part, AssistantToolPart)]
    assert tool_parts, "no tool part reached the assistant message"
    assert any(part.name == "fs.read" and part.state == "succeeded" for part in tool_parts)
    final_text = "".join(part.text for part in assistants[-1].parts if isinstance(part, AssistantTextPart))
    assert _MARKER in final_text
