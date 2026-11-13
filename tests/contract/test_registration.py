"""Unit tests for the P5-S1 registration face (T-C1-085): the manifest -> engine glue.

Honest-label note: the engine below is a recording fake (the ticket's
"fake/记录型 engine" pin) -- it records every ``register`` argument
verbatim and either binds a synthetic bound object or raises an injected
failure. The manifest side is real: a tmp skills tree driven through the
real ``SkillCatalog`` discovery gate (the single manifest parser -- no
second face is exercised or built here). The real-engine cases live in
tests/integration/test_registration_engine.py; the two host composition
cases in tests/integration/test_registration_hosts.py.

Coverage (the T-C1-085 acceptance judges; the T-C1-087 native-spec faces):

* filtering: only ``execution == "langgraph"`` entries open their native
  spec file -- default/None entries are counted and skipped, never opened
  (proven by nonexistent declared files: any open would surface as a
  rejection row, and none does);
* the native-spec consumption (T-C1-087, ruling-18): the consumed file is
  the declared pipeline's ``pipeline.opencode.json`` sibling -- the
  declared graph.json is routing metadata and is never opened (a garbage
  declared file proves it); a missing sibling is the named
  ``NativeSpecMissing`` rejection row carrying the skill id, the missing
  fact and the framework-emitter migration pointer (the legacy 2 SPEC + 6
  missing batch is the mirror);
* containment: the derived sibling path escaping the skills root
  (traversal, absolute, or the reparse forms -- a win32 junction, a
  symlink) is rejected per-entry without touching the outside file, and
  the ELOOP-shaped self-symlink never registers either (zero reads, one
  structured row -- the reachable face of the unresolvable form);
* the per-entry rejection face: ``OSError`` / ``json.JSONDecodeError`` /
  ``UnicodeDecodeError`` (a non-UTF-8 native spec) / pydantic
  ``ValidationError`` / ``NativeSpecMissing`` /
  ``PipelineSpecRejected`` each land in the report by class name without
  interrupting the remaining entries;
* manifest_error: a corrupt manifest is reported, never raised; a missing
  directory or manifest is the silent fresh-project zero state;
* the success sequence and the report shape, including the identity
  semantics (binding by the spec's own ``pipeline_id`` -- the manifest id
  is routing metadata) and the duplicate-pipeline_id new-binding record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.pipelines import PipelineSpec
from opencode.orchestrator.engine import PipelineSpecRejected
from opencode.orchestrator.registration import (
    NATIVE_SPEC_NAME,
    RegistrationReport,
    register_pipelines,
)
from tests.contract.test_pipelines import _spec_raw

#: The §6.2 digest form carried verbatim from the locked skills fixtures.
_SKILL_HASH = "70152fa897ce94831325a87efae0039c6af197ef1e70527e5551e14da3731ef8"


def _entry(
    skill_id: str,
    *,
    execution: str | None = None,
    pipeline: str | None = None,
) -> dict[str, Any]:
    """One manifest entry body (the locked skills_fixtures shape, spliced per case)."""
    raw: dict[str, Any] = {
        "id": skill_id,
        "name": f"{skill_id} skill",
        "path": f"{skill_id}/SKILL.md",
        "size": 78,
        "hash": _SKILL_HASH,
        "source": "project",
    }
    if execution is not None:
        raw["execution"] = execution
    if pipeline is not None:
        raw["pipeline"] = pipeline
    return raw


def _write_skills(skills: Path, entries: Sequence[dict[str, Any]]) -> Path:
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "skills.json").write_text(json.dumps({"skills": list(entries)}), encoding="utf-8")
    return skills


def _write_graph(skills: Path, relative: str, payload: object) -> None:
    """Write one file under the skills root (str payloads land verbatim)."""
    target = skills / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _write_native(skills: Path, declared: str, payload: object) -> None:
    """Write the native-spec sibling for one declared pipeline (the glue's own derivation)."""
    target = (skills / declared).parent / NATIVE_SPEC_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _write_native_bytes(skills: Path, declared: str, payload: bytes) -> None:
    """Write raw bytes as the native-spec sibling (the non-UTF-8 read face)."""
    target = (skills / declared).parent / NATIVE_SPEC_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _make_junction(link: Path, target: Path) -> None:
    """A win32 junction (``mklink /J``, no elevation): the probe-borne construct
    for the reparse-point containment face."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"mklink /J failed: {result.stdout}{result.stderr}"
    assert link.is_dir()


# ---------------------------------------------------------------------------
# The recording fake (the injectable registration face -- register only;
# the glue never consumes any other engine method).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeSpec:
    pipeline_id: str


@dataclass(frozen=True, slots=True)
class _FakeBound:
    spec: _FakeSpec
    spec_hash: str


class _RecordingEngine:
    """Records each ``register`` argument; binds synthetic results or raises injected failures.

    The default body binds by the raw spec's own ``pipeline_id``
    (re-registration replaces -- the real engine's existing new-binding
    semantics); an injected failure queue raises instead, one consumed per
    call, so each rejection class is delivered on demand.
    """

    def __init__(self, failures: Sequence[Exception] = ()) -> None:
        self.raws: list[dict[str, Any]] = []
        self.bindings: dict[str, _FakeBound] = {}
        self._failures = list(failures)
        self._counter = 0

    async def register(self, raw: dict[str, Any]) -> _FakeBound:
        self.raws.append(raw)
        if self._failures:
            raise self._failures.pop(0)
        self._counter += 1
        pipeline_id = raw.get("pipeline_id")
        assert isinstance(pipeline_id, str), "the fake binds by the spec's own pipeline_id"
        bound = _FakeBound(spec=_FakeSpec(pipeline_id=pipeline_id), spec_hash=f"sha-{self._counter:03d}")
        self.bindings[pipeline_id] = bound
        return bound

    def bound(self, pipeline_id: str) -> _FakeBound | None:
        return self.bindings.get(pipeline_id)


def _make_validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        PipelineSpec.model_validate({"schema_version": 1, "pipeline_id": "digest"})
    return excinfo.value


def _make_spec_rejection() -> PipelineSpecRejected:
    return PipelineSpecRejected("entry node 'ghost' is not declared", code=CoreErrorCode.VALIDATION_ERROR)


# ---------------------------------------------------------------------------
# The acceptance judges.
# ---------------------------------------------------------------------------


class TestFiltering:
    async def test_default_entries_are_skipped_without_opening_any_file(self, tmp_path: Path) -> None:
        """Both default faces (None and "default") never open their declared files.

        The proof form: the entries' declared bodies do not exist on disk --
        any open would raise OSError into a rejection row, and none appears.
        """
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("code-review"), _entry("plain-skill", execution="default")],
        )
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.registered == ()
        assert report.rejected == ()
        assert report.non_langgraph == 2
        assert report.manifest_error is None
        assert engine.raws == []
        assert not (skills / "code-review" / "SKILL.md").exists()

    async def test_langgraph_entries_register_in_manifest_order(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [
                _entry("alpha", execution="langgraph", pipeline="alpha/graph.json"),
                _entry("beta", execution="langgraph", pipeline="beta/graph.json"),
            ],
        )
        _write_native(skills, "alpha/graph.json", _spec_raw(pipeline_id="alpha-pipeline"))
        _write_native(skills, "beta/graph.json", _spec_raw(pipeline_id="beta-pipeline"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.manifest_error is None
        assert report.non_langgraph == 0
        assert report.rejected == ()
        assert [(r.skill_id, r.pipeline_id, r.spec_hash) for r in report.registered] == [
            ("alpha", "alpha-pipeline", "sha-001"),
            ("beta", "beta-pipeline", "sha-002"),
        ]
        assert [raw["pipeline_id"] for raw in engine.raws] == ["alpha-pipeline", "beta-pipeline"]


class TestContainment:
    async def test_traversal_escape_is_rejected_before_any_read(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("escape", execution="langgraph", pipeline="../outside/graph.json")],
        )
        # The OUTSIDE native spec EXISTS with a registrable payload -- the
        # rejection must come from containment on the derived sibling path,
        # not from a read or gate failure.
        _write_graph(tmp_path / "outside", "pipeline.opencode.json", _spec_raw(pipeline_id="escapee"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []
        (rejection,) = report.rejected
        assert rejection.skill_id == "escape"
        assert rejection.pipeline == "../outside/graph.json"
        assert rejection.error_type == "PipelinePathEscape"
        assert "escapes" in rejection.message

    async def test_absolute_pipeline_path_is_rejected(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("abs", execution="langgraph", pipeline=str(elsewhere / "graph.json"))],
        )
        _write_graph(elsewhere, "pipeline.opencode.json", _spec_raw(pipeline_id="absolute"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []
        (rejection,) = report.rejected
        assert rejection.error_type == "PipelinePathEscape"
        assert report.registered == ()

    @pytest.mark.skipif(sys.platform != "win32", reason="the junction reparse form is win32-only (mklink /J)")
    async def test_a_junction_escape_is_rejected_before_any_read(self, tmp_path: Path) -> None:
        """The junction form (T-C1-088 rider 1): ``skills/jlink -> outside``
        follows the reparse point on the realpath side and lands outside the
        skills root. The OUTSIDE native spec exists with a registrable
        payload -- the rejection can only come from containment, never from
        a read or gate failure (the traversal-test shape)."""
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("jlink-skill", execution="langgraph", pipeline="jlink/graph.json")],
        )
        outside = tmp_path / "outside"
        _write_graph(outside, "pipeline.opencode.json", _spec_raw(pipeline_id="escapee"))
        _make_junction(skills / "jlink", outside)
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []
        (rejection,) = report.rejected
        assert rejection.skill_id == "jlink-skill"
        assert rejection.pipeline == "jlink/graph.json"
        assert rejection.error_type == "PipelinePathEscape"
        assert "escapes" in rejection.message

    async def test_a_symlink_escape_is_rejected_before_any_read(self, tmp_path: Path) -> None:
        """The symlink form (T-C1-088 rider 1): ``skills/slink -> outside``
        resolves through the link and is rejected before any read. Symlink
        creation needs a privilege on win32 (Developer Mode / elevation);
        the construct skips honestly where the OS refuses it."""
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("slink-skill", execution="langgraph", pipeline="slink/graph.json")],
        )
        outside = tmp_path / "outside"
        _write_graph(outside, "pipeline.opencode.json", _spec_raw(pipeline_id="escapee"))
        try:
            os.symlink(outside, skills / "slink", target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlink creation unavailable on this host: {error}")
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []
        (rejection,) = report.rejected
        assert rejection.skill_id == "slink-skill"
        assert rejection.pipeline == "slink/graph.json"
        assert rejection.error_type == "PipelinePathEscape"
        assert "escapes" in rejection.message

    async def test_a_self_referencing_symlink_never_registers_and_lands_in_one_structured_row(
        self, tmp_path: Path
    ) -> None:
        """The unresolvable form, honest shape (T-C1-088 rider 1).

        The TDD-era probe finding (progress log): Python's non-strict
        ``Path.resolve()`` raises NO OSError for any probed construct
        (invalid chars, reserved names, trailing dots, junction and symlink
        loops all return best-effort paths), so the glue's conservative
        escape-on-OSError branch stays defensive. This pins the reachable
        face of the ELOOP construct: the self-symlink keeps the derived
        path inside the root, nothing is ever read or registered, and the
        entry lands as exactly one structured rejection row -- never a
        silent registration, never a boot crash."""
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("loop-skill", execution="langgraph", pipeline="loop/graph.json")],
        )
        try:
            os.symlink(skills / "loop", skills / "loop", target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlink creation unavailable on this host: {error}")
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []  # zero reads: the loop is never opened
        assert report.registered == ()
        assert report.manifest_error is None
        (rejection,) = report.rejected
        assert rejection.skill_id == "loop-skill"
        assert rejection.pipeline == "loop/graph.json"
        assert rejection.error_type == "NativeSpecMissing"


class TestPerEntryRejection:
    async def test_a_missing_native_spec_is_a_structured_rejection(self, tmp_path: Path) -> None:
        """The missing face (ruling-19): no ``pipeline.opencode.json`` sibling -> the named row.

        The declared graph.json EXISTS with dialect-shaped content and is
        never opened: the rejection is the glue's own missing-native-spec
        face, not a read or parse failure of the declared file.
        """
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("legacy-skill", execution="langgraph", pipeline="legacy/graph.json")],
        )
        _write_graph(
            skills,
            "legacy/graph.json",
            {"schema_version": "pydag-editable-graph-v1", "skill_name": "demo", "nodes": [], "edges": []},
        )
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert engine.raws == []
        assert report.registered == ()
        (rejection,) = report.rejected
        assert rejection.skill_id == "legacy-skill"
        assert rejection.pipeline == "legacy/graph.json"
        assert rejection.error_type == "NativeSpecMissing"
        assert "legacy-skill" in rejection.message
        assert "pipeline.opencode.json" in rejection.message
        assert "agent_dsl_flow_framework" in rejection.message

    async def test_a_directory_at_the_native_spec_path_is_the_missing_rejection(self, tmp_path: Path) -> None:
        """Conservative face: anything that is not a file is the missing spec."""
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("dir-skill", execution="langgraph", pipeline="dir/graph.json")],
        )
        (skills / "dir" / "pipeline.opencode.json").mkdir(parents=True)
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        (rejection,) = report.rejected
        assert rejection.error_type == "NativeSpecMissing"
        assert report.registered == ()

    async def test_invalid_native_spec_json_is_a_jsondecodeerror_rejection(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("bad-json", execution="langgraph", pipeline="bad/graph.json")],
        )
        _write_native(skills, "bad/graph.json", "{not json")
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        (rejection,) = report.rejected
        assert rejection.error_type == "JSONDecodeError"
        assert report.registered == ()

    async def test_a_non_utf8_native_spec_is_a_unicode_decode_error_rejection(self, tmp_path: Path) -> None:
        """The non-UTF-8 read face (T-B-096 finding 3, recast post-087 by
        T-C1-088 rider 3): the consumed file is the native spec, decoded
        strictly as utf-8 -- undecodable bytes land the row by the codec's
        own class name, the remaining entries continue, and the manifest
        itself stays healthy."""
        skills = _write_skills(
            tmp_path / "skills",
            [
                _entry("mojibake", execution="langgraph", pipeline="mojibake/graph.json"),
                _entry("healthy", execution="langgraph", pipeline="healthy/graph.json"),
            ],
        )
        _write_native_bytes(skills, "mojibake/graph.json", b"\xff\xfe{not utf-8\xff}")
        _write_native(skills, "healthy/graph.json", _spec_raw(pipeline_id="healthy-pipeline"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.manifest_error is None  # the manifest itself stays healthy
        assert [r.pipeline_id for r in report.registered] == ["healthy-pipeline"]  # the remaining entries continue
        assert [raw["pipeline_id"] for raw in engine.raws] == ["healthy-pipeline"]  # never read through
        (rejection,) = report.rejected
        assert rejection.skill_id == "mojibake"
        assert rejection.pipeline == "mojibake/graph.json"
        assert rejection.error_type == "UnicodeDecodeError"
        assert rejection.message  # the codec error's first line

    @pytest.mark.parametrize(
        ("make_failure", "error_name"),
        [
            pytest.param(_make_validation_error, "ValidationError", id="pydantic-structural"),
            pytest.param(_make_spec_rejection, "PipelineSpecRejected", id="semantic-gate"),
        ],
    )
    async def test_engine_gate_failures_surface_their_class_per_entry(
        self, tmp_path: Path, make_failure: Callable[[], Exception], error_name: str
    ) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("rejected-skill", execution="langgraph", pipeline="one/graph.json")],
        )
        _write_native(skills, "one/graph.json", _spec_raw(pipeline_id="one"))
        engine = _RecordingEngine(failures=[make_failure()])
        report = await register_pipelines(engine, skills)
        assert report.registered == ()
        (rejection,) = report.rejected
        assert rejection.skill_id == "rejected-skill"
        assert rejection.pipeline == "one/graph.json"
        assert rejection.error_type == error_name
        assert rejection.message

    async def test_one_rejection_does_not_interrupt_the_remaining_entries(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [
                _entry("broken", execution="langgraph", pipeline="broken/graph.json"),
                _entry("healthy", execution="langgraph", pipeline="healthy/graph.json"),
            ],
        )
        _write_native(skills, "broken/graph.json", "{still not json")
        _write_native(skills, "healthy/graph.json", _spec_raw(pipeline_id="healthy-pipeline"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert [r.pipeline_id for r in report.registered] == ["healthy-pipeline"]
        assert [r.skill_id for r in report.rejected] == ["broken"]
        assert [raw["pipeline_id"] for raw in engine.raws] == ["healthy-pipeline"]


class TestManifestError:
    async def test_a_corrupt_manifest_is_reported_not_raised(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir(parents=True)
        (skills / "skills.json").write_text("{corrupt", encoding="utf-8")
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.manifest_error is not None
        assert "not valid JSON" in report.manifest_error
        assert report.registered == ()
        assert report.rejected == ()
        assert report.non_langgraph == 0
        assert engine.raws == []

    async def test_a_missing_skills_directory_is_the_silent_zero_state(self, tmp_path: Path) -> None:
        engine = _RecordingEngine()
        report = await register_pipelines(engine, tmp_path / "nowhere")
        assert report == RegistrationReport()

    async def test_a_missing_manifest_is_the_silent_zero_state(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir(parents=True)
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report == RegistrationReport()


class TestIdentitySemantics:
    async def test_binding_uses_the_spec_pipeline_id_not_the_manifest_id(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("code-review", execution="langgraph", pipeline="code-review/graph.json")],
        )
        _write_native(skills, "code-review/graph.json", _spec_raw(pipeline_id="digest"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        (registered,) = report.registered
        assert registered.skill_id == "code-review"  # the manifest routing id rides along
        assert registered.pipeline_id == "digest"  # the engine binds the spec's own id
        assert engine.bound("digest") is not None
        assert engine.bound("code-review") is None  # the manifest id never reaches the engine

    async def test_a_duplicate_pipeline_id_records_both_as_new_bindings(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [
                _entry("first", execution="langgraph", pipeline="first/graph.json"),
                _entry("second", execution="langgraph", pipeline="second/graph.json"),
            ],
        )
        _write_native(skills, "first/graph.json", _spec_raw(pipeline_id="digest"))
        _write_native(skills, "second/graph.json", _spec_raw(pipeline_id="digest"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert [(r.skill_id, r.pipeline_id) for r in report.registered] == [
            ("first", "digest"),
            ("second", "digest"),
        ]
        # The engine's existing new-binding semantics: the later registration
        # replaced the binding (hash lock), recorded honestly in the report.
        assert report.registered[0].spec_hash != report.registered[1].spec_hash
        bound = engine.bound("digest")
        assert bound is not None
        assert bound.spec_hash == report.registered[1].spec_hash


class TestNativeSpecPriority:
    """The native spec is the ONLY consumption form (ruling-18): the declared
    graph.json is routing metadata, never opened -- the sibling registers."""

    async def test_the_declared_graph_is_never_opened_the_native_sibling_registers(self, tmp_path: Path) -> None:
        skills = _write_skills(
            tmp_path / "skills",
            [_entry("code-review", execution="langgraph", pipeline="code-review/graph.json")],
        )
        # The declared graph.json is garbage: any read of it would be a
        # JSONDecodeError rejection; the registered row proves only the
        # native sibling was consumed (no fallback to any conversion face).
        _write_graph(skills, "code-review/graph.json", "{never opened")
        native = _spec_raw(pipeline_id="native-pipeline")
        _write_native(skills, "code-review/graph.json", native)
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.rejected == ()
        (registered,) = report.registered
        assert registered.skill_id == "code-review"
        assert registered.pipeline_id == "native-pipeline"
        assert engine.raws == [native]  # the native spec, verbatim -- never the declared body

    async def test_the_legacy_batch_mirror_two_specs_six_missing(self, tmp_path: Path) -> None:
        """The 裁决-19 mirror: the migrated legacy batch (2 SPEC + 6 missing) lands
        as 2 registered rows + 6 structured missing rows, in manifest order."""
        legacy = (
            "gocm-sop",
            "dws-disk-diagnosis",
            "efs-diagnosis",
            "dia-dlf-job",
            "lingqu-general-rca",
            "modelarts-general-rca",
            "pipeline-author",
            "resource-fault-query-capability",
        )
        entries = [_entry(skill_id, execution="langgraph", pipeline=f"{skill_id}/graph.json") for skill_id in legacy]
        skills = _write_skills(tmp_path / "skills", entries)
        for skill_id in ("gocm-sop", "pipeline-author"):  # the two the emitter batch published
            _write_native(skills, f"{skill_id}/graph.json", _spec_raw(pipeline_id=f"{skill_id}-pipeline"))
        engine = _RecordingEngine()
        report = await register_pipelines(engine, skills)
        assert report.manifest_error is None
        assert report.non_langgraph == 0
        assert [r.pipeline_id for r in report.registered] == ["gocm-sop-pipeline", "pipeline-author-pipeline"]
        assert [r.skill_id for r in report.rejected] == [
            "dws-disk-diagnosis",
            "efs-diagnosis",
            "dia-dlf-job",
            "lingqu-general-rca",
            "modelarts-general-rca",
            "resource-fault-query-capability",
        ]
        assert {r.error_type for r in report.rejected} == {"NativeSpecMissing"}
        assert all(NATIVE_SPEC_NAME in r.message for r in report.rejected)
        assert all("agent_dsl_flow_framework" in r.message for r in report.rejected)
