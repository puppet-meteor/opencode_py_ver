"""Host composition tests for the P5-S1 registration face (T-C1-085): the two production boots.

The glue wired the way production wires it, one judge set per host (the
T-C1-082 frozen-face discipline: two isomorphic assemblies, deliberately
no shared factory):

* the CLI: ``main()`` in-process over the r0 fake provider (the
  test_cli_r4.py pattern) with a tmp project root carrying a real
  ``.opencode-py/skills`` tree -- the registration must run inside the
  ``asyncio.run(_drive(...))`` loop, the same loop that later drives the
  engine (the checkpointer's aiosqlite connection is loop-bound);
* the HTTP host: the real ASGI lifespan startup (driven by hand, the
  test_httpapi_server.py pattern) with the App carrying the engine and
  the skills root -- the registration must run inside the serving loop,
  the startup twin of the shutdown reclaim face.

The disclosure policy is asserted per host: a valid manifest binds with
zero stderr; a broken manifest is exactly one stderr line and the boot
continues; a missing skills tree is the silent fresh-project zero state.
The T-C1-088 rider 2 adds the per-entry form (T-B-096 finding 2): a valid
manifest carrying one bad entry binds the healthy entries, discloses the
bad one as exactly one stderr line naming the skill id, the pipeline
string and the error class, and never writes stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.cli import main
from opencode.httpapi import create_app
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.registration import NATIVE_SPEC_NAME
from tests.contract.test_pipelines import _spec_raw
from tests.integration.test_cli_r4 import _argv, _ok_response, _ServerHost
from tests.integration.test_engine import _FakeLlm, _FakeTool
from tests.integration.test_httpapi_server import _drive_lifespan, _require_success, _upgrade_head
from tests.integration.test_registration_engine import _write_langgraph_skills

# ---------------------------------------------------------------------------
# The rider-2 manifest: one healthy entry + one bad entry (the missing-native-
# spec form) -- the minimal valid-manifest-with-a-bad-entry face.
# ---------------------------------------------------------------------------

#: The locked skills-fixtures entry hash (the contract suite's shape).
_HOST_ENTRY_HASH = "70152fa897ce94831325a87efae0039c6af197ef1e70527e5551e14da3731ef8"


def _host_entry(skill_id: str) -> dict[str, Any]:
    """One locked-shape langgraph manifest entry, spliced per id."""
    return {
        "id": skill_id,
        "name": f"{skill_id} skill",
        "path": f"{skill_id}/SKILL.md",
        "size": 78,
        "hash": _HOST_ENTRY_HASH,
        "source": "project",
        "execution": "langgraph",
        "pipeline": f"{skill_id}/graph.json",
    }


def _write_two_entry_skills(skills: Path) -> Path:
    """The rider-2 manifest: ``healthy`` publishes its native spec and binds;
    ``legacy-broken`` declares a pipeline whose native spec was never
    published (the minimal bad-entry form)."""
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "skills.json").write_text(
        json.dumps({"skills": [_host_entry("healthy"), _host_entry("legacy-broken")]}),
        encoding="utf-8",
    )
    native = skills / "healthy" / NATIVE_SPEC_NAME
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_text(json.dumps(_spec_raw(pipeline_id="healthy-pipeline")), encoding="utf-8")
    return skills


# ---------------------------------------------------------------------------
# The CLI host: main() in-process, the r0 fake provider, a tmp project root.
# ---------------------------------------------------------------------------


class TestCliHost:
    def test_cli_boots_with_the_manifest_registered_inside_the_drive_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        host = _ServerHost([_ok_response("glued")])
        base_url = host.start()
        try:
            monkeypatch.chdir(tmp_path)
            skills = _write_langgraph_skills(tmp_path / ".opencode-py" / "skills", _spec_raw(pipeline_id="digest"))
            assert (skills / "skills.json").is_file()
            # The load-bearing spy (the McpManager.add_server precedent): the
            # registration must run inside the driving loop's lifetime -- the
            # fresh binding is read back through engine.bound in the same
            # coroutine turn, which only the driving loop can serve.
            seen: list[tuple[str, bool]] = []
            original_register = PipelineEngine.register

            async def _spy_register(engine: PipelineEngine, raw: Any) -> Any:
                bound = await original_register(engine, raw)
                seen.append((bound.spec.pipeline_id, engine.bound(bound.spec.pipeline_id) is bound))
                return bound

            monkeypatch.setattr(PipelineEngine, "register", _spy_register)
            db_path = tmp_path / "cli-p5-register.db"
            code = main(_argv(db_path, f"{base_url}/v1", "--project-root", str(tmp_path), "boot the host"))
        finally:
            host.stop()
        assert code == 0
        assert seen == [("digest", True)]
        captured = capsys.readouterr()
        assert "[final] glued" in captured.out
        assert captured.err == ""  # a clean manifest discloses nothing

    def test_cli_boot_continues_past_a_broken_manifest_with_one_stderr_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        host = _ServerHost([_ok_response("booted")])
        base_url = host.start()
        try:
            monkeypatch.chdir(tmp_path)
            skills = tmp_path / ".opencode-py" / "skills"
            skills.mkdir(parents=True)
            (skills / "skills.json").write_text("{corrupt", encoding="utf-8")
            seen: list[str] = []
            original_register = PipelineEngine.register

            async def _spy_register(engine: PipelineEngine, raw: Any) -> Any:
                seen.append(str(raw.get("pipeline_id")))
                return await original_register(engine, raw)

            monkeypatch.setattr(PipelineEngine, "register", _spy_register)
            db_path = tmp_path / "cli-p5-broken-manifest.db"
            code = main(_argv(db_path, f"{base_url}/v1", "--project-root", str(tmp_path), "boot anyway"))
        finally:
            host.stop()
        assert code == 0
        assert seen == []  # a broken manifest registers nothing
        captured = capsys.readouterr()
        assert "[final] booted" in captured.out
        lines = captured.err.strip().splitlines()
        assert len(lines) == 1
        assert "skills" in lines[0] and "manifest" in lines[0]

    def test_cli_boot_without_a_skills_tree_stays_silent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        host = _ServerHost([_ok_response("fresh")])
        base_url = host.start()
        try:
            monkeypatch.chdir(tmp_path)  # a fresh project root: no .opencode-py at all
            seen: list[str] = []
            original_register = PipelineEngine.register

            async def _spy_register(engine: PipelineEngine, raw: Any) -> Any:
                seen.append(str(raw.get("pipeline_id")))
                return await original_register(engine, raw)

            monkeypatch.setattr(PipelineEngine, "register", _spy_register)
            db_path = tmp_path / "cli-p5-zero.db"
            code = main(_argv(db_path, f"{base_url}/v1", "--project-root", str(tmp_path), "fresh project"))
        finally:
            host.stop()
        assert code == 0
        assert seen == []
        captured = capsys.readouterr()
        assert "[final] fresh" in captured.out
        assert captured.err == ""  # the fresh-project zero state is silent, not an error

    def test_cli_boot_discloses_one_bad_entry_and_continues_with_the_healthy_ones_bound(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The rider-2 host form (T-B-096 finding 2): a VALID manifest with one
        bad entry (the missing-native-spec form) boots on -- the healthy
        entry binds inside the drive loop -- and the bad entry is disclosed
        as exactly one stderr line naming the skill id, the declared
        pipeline string and the error class; stdout stays the answer stream."""
        host = _ServerHost([_ok_response("booted-past-the-bad-entry")])
        base_url = host.start()
        try:
            monkeypatch.chdir(tmp_path)
            skills = _write_two_entry_skills(tmp_path / ".opencode-py" / "skills")
            assert (skills / "skills.json").is_file()
            seen: list[str] = []
            original_register = PipelineEngine.register

            async def _spy_register(engine: PipelineEngine, raw: Any) -> Any:
                seen.append(str(raw.get("pipeline_id")))
                return await original_register(engine, raw)

            monkeypatch.setattr(PipelineEngine, "register", _spy_register)
            db_path = tmp_path / "cli-p5-bad-entry.db"
            code = main(_argv(db_path, f"{base_url}/v1", "--project-root", str(tmp_path), "boot anyway"))
        finally:
            host.stop()
        assert code == 0
        assert seen == ["healthy-pipeline"]  # the boot continued: the healthy entry bound in the loop
        captured = capsys.readouterr()
        assert "[final] booted-past-the-bad-entry" in captured.out
        lines = captured.err.strip().splitlines()
        assert len(lines) == 1  # exactly one stderr line: the bad entry, nothing else
        assert "legacy-broken" in lines[0]  # the skill id is named
        assert "legacy-broken/graph.json" in lines[0]  # the declared pipeline string is named
        assert "NativeSpecMissing" in lines[0]  # the error class is named
        # stdout stays the answer stream: the disclosure face never writes it.
        assert "NativeSpecMissing" not in captured.out
        assert "rejected" not in captured.out


# ---------------------------------------------------------------------------
# The HTTP host: the real ASGI lifespan startup, driven by hand.
# ---------------------------------------------------------------------------


def _serving_engine(tmp_path: Path) -> PipelineEngine:
    """The production engine composition shape, over fake node bodies (register never runs them)."""
    return PipelineEngine(
        checkpointer=EngineCheckpointer(tmp_path / "advisory-cursor.sqlite3"),
        llm_call=_FakeLlm(),
        tool=_FakeTool(),
    )


class TestAsgiLifespanHost:
    async def test_lifespan_registers_the_manifest_inside_the_serving_loop(self, tmp_path: Path) -> None:
        db_path = tmp_path / "serving-p5.db"
        _require_success(_upgrade_head(db_path))
        skills = _write_langgraph_skills(tmp_path / "skills", _spec_raw(pipeline_id="digest"))
        engine = _serving_engine(tmp_path)
        app = App(db_path, pipeline_engine=engine, skills_dir=skills)
        api = create_app(app, token="p5-registration-token")
        bound_ok: dict[str, bool] = {}

        async def after_startup() -> None:
            # Inside the serving loop: the startup face has bound the pipeline
            # (the same loop that will serve jobs through this engine).
            bound = engine.bound("digest")
            bound_ok["digest"] = bound is not None and bound.spec.pipeline_id == "digest"

        events = await _drive_lifespan(api, after_startup=after_startup)
        assert [event["type"] for event in events] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert bound_ok["digest"] is True

    async def test_lifespan_boot_continues_past_a_broken_manifest_with_one_stderr_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "serving-p5-broken.db"
        _require_success(_upgrade_head(db_path))
        skills = tmp_path / "skills"
        skills.mkdir(parents=True)
        (skills / "skills.json").write_text("{corrupt", encoding="utf-8")
        engine = _serving_engine(tmp_path)
        app = App(db_path, pipeline_engine=engine, skills_dir=skills)
        api = create_app(app, token="p5-registration-token")
        events = await _drive_lifespan(api)
        assert [event["type"] for event in events] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        lines = capsys.readouterr().err.strip().splitlines()
        assert len(lines) == 1
        assert "skills" in lines[0] and "manifest" in lines[0]
        assert engine.bound("digest") is None

    async def test_lifespan_without_a_skills_tree_stays_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path = tmp_path / "serving-p5-zero.db"
        _require_success(_upgrade_head(db_path))
        engine = _serving_engine(tmp_path)
        app = App(db_path, pipeline_engine=engine, skills_dir=tmp_path / "nowhere")
        api = create_app(app, token="p5-registration-token")
        events = await _drive_lifespan(api)
        assert [event["type"] for event in events] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        assert capsys.readouterr().err == ""
        assert engine.bound("digest") is None

    async def test_lifespan_boot_discloses_one_bad_entry_and_continues(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The rider-2 host form, the ASGI startup twin: a valid manifest with
        one bad entry completes the lifespan startup with the healthy entry
        bound on the serving loop, exactly one stderr line naming the skill
        id, the pipeline string and the error class, and stdout untouched."""
        db_path = tmp_path / "serving-p5-bad-entry.db"
        _require_success(_upgrade_head(db_path))
        skills = _write_two_entry_skills(tmp_path / "skills")
        engine = _serving_engine(tmp_path)
        app = App(db_path, pipeline_engine=engine, skills_dir=skills)
        api = create_app(app, token="p5-registration-token")
        events = await _drive_lifespan(api)
        assert [event["type"] for event in events] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
        captured = capsys.readouterr()
        lines = captured.err.strip().splitlines()
        assert len(lines) == 1  # exactly one stderr line: the bad entry, nothing else
        assert "legacy-broken" in lines[0]  # the skill id is named
        assert "legacy-broken/graph.json" in lines[0]  # the declared pipeline string is named
        assert "NativeSpecMissing" in lines[0]  # the error class is named
        assert "NativeSpecMissing" not in captured.out  # the disclosure face never writes stdout
        assert engine.bound("healthy-pipeline") is not None  # the boot continued: the healthy entry bound
