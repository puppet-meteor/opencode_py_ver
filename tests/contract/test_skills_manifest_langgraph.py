"""The S5a skills-manifest extension contract tests (T-C1-080, CORE-CONTRACTS §6.3).

The manifest's only field-level change in the P3 line (PHASE3 §5.1): the
optional execution pair -- ``execution`` ∈ {default, langgraph} and the
conditional ``pipeline`` (skill-directory-relative path, required on
langgraph entries, rejected on default entries). The ticket's four
acceptance paths (valid / bad / unknown / missing-pipeline) are pinned
here over the fixture files under ``tests/contract/skills_fixtures/``
(the two negative fixtures ride the existing verbatim-rejection harness
in test_skill_catalog.py; this module owns the four-path behavior face).

Honest scope note: discovery reads exactly the manifest -- a declared
``pipeline`` file is never opened here (same observation discipline as
the body ``path``). The graph.json registration-time validation + §5.1
hash lock are the engine ``register`` face (already pinned by
test_pipelines.py); the manifest -> engine glue is the S5b slice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencode.config.loader import ConfigLoader
from opencode.skills import SKILLS_MANIFEST_NAME, SkillCatalog, SkillDiscoveryError, SkillEntry
from tests.contract.test_skill_catalog import (
    LANGGRAPH_POSITIVE_FIXTURE,
    SKILLS_FIXTURES_DIR,
    VALID_DIGEST,
    _entry,
    _write_manifest,
)

_MISSING_PIPELINE_FIXTURE = "skills_manifest_entry_langgraph_missing_pipeline.json"
_UNKNOWN_EXECUTION_FIXTURE = "skills_manifest_entry_execution_unknown.json"


def _fixture_bytes(name: str) -> bytes:
    return (SKILLS_FIXTURES_DIR / name).read_bytes()


class TestLanggraphManifestPaths:
    """The four ticket paths: valid / bad / unknown / missing-pipeline."""

    def test_valid_langgraph_entry_round_trips_the_pair(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes(_fixture_bytes(LANGGRAPH_POSITIVE_FIXTURE))
        entries = SkillCatalog(tmp_path).list()
        assert len(entries) == 1
        first = entries[0]
        assert isinstance(first, SkillEntry)
        assert (first.id, first.name, first.path, first.size, first.hash, first.source) == (
            "code-review",
            "Code Review",
            "code-review/SKILL.md",
            78,
            VALID_DIGEST,
            "project",
        )
        assert first.execution == "langgraph"
        assert first.pipeline == "code-review/graph.json"

    def test_missing_pipeline_is_rejected_with_the_pair_rule(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes(_fixture_bytes(_MISSING_PIPELINE_FIXTURE))
        with pytest.raises(SkillDiscoveryError, match="required 'pipeline'"):
            SkillCatalog(tmp_path).list()

    def test_unknown_execution_value_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes(_fixture_bytes(_UNKNOWN_EXECUTION_FIXTURE))
        with pytest.raises(SkillDiscoveryError, match="must be one of: default, langgraph"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.parametrize("execution", ["default", None])
    def test_pipeline_without_langgraph_execution_is_rejected(self, tmp_path: Path, execution: object) -> None:
        """The pair is conditional: ``pipeline`` on a default entry (explicit
        or the implicit None reading) is a discovery rejection, never a
        silently ignored extra."""
        overrides: dict[str, object] = {"pipeline": "code-review/graph.json"}
        if execution is not None:
            overrides["execution"] = execution
        _write_manifest(tmp_path, {"skills": [_entry(overrides)]})
        with pytest.raises(SkillDiscoveryError, match="legal only on langgraph"):
            SkillCatalog(tmp_path).list()

    def test_explicit_default_execution_without_pipeline_stays_legal(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({"execution": "default"})]})
        entries = SkillCatalog(tmp_path).list()
        assert entries[0].execution == "default"
        assert entries[0].pipeline is None

    def test_plain_six_field_entries_stay_legal_and_carry_the_pair_as_none(self, tmp_path: Path) -> None:
        """The S1/S2 discovery face is unchanged: an entry without the pair
        discovers exactly as before (the pair reads back as None)."""
        _write_manifest(tmp_path, {"skills": [_entry()]})
        first = SkillCatalog(tmp_path).list()[0]
        assert first.execution is None
        assert first.pipeline is None

    def test_discovery_never_opens_the_declared_pipeline_file(self, tmp_path: Path) -> None:
        """The observation discipline carries over: the pipeline path is
        declared verbatim, and the file it names does not exist at all."""
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes(_fixture_bytes(LANGGRAPH_POSITIVE_FIXTURE))
        assert not (tmp_path / "code-review" / "graph.json").exists()
        assert not (tmp_path / "code-review" / "SKILL.md").exists()
        assert SkillCatalog(tmp_path).list()[0].pipeline == "code-review/graph.json"

    def test_the_declared_pipeline_path_is_carried_verbatim(self, tmp_path: Path) -> None:
        """Neither separator is normalized and neither is resolved (the
        platform face the body ``path`` already pins)."""
        _write_manifest(
            tmp_path,
            {"skills": [_entry({"execution": "langgraph", "pipeline": "deep\\nest graph.json"})]},
        )
        assert SkillCatalog(tmp_path).list()[0].pipeline == "deep\\nest graph.json"

    def test_langgraph_pair_survives_the_loader_seam_composition(self, tmp_path: Path) -> None:
        """The P2-S1 composition roundtrip (the loader names the conventional
        root; the catalog discovers inside it) carries the pair end to end."""
        skills_dir = tmp_path / ".opencode-py" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / SKILLS_MANIFEST_NAME).write_bytes(_fixture_bytes(LANGGRAPH_POSITIVE_FIXTURE))
        entries = SkillCatalog(ConfigLoader(working_dir=tmp_path).skills_directory()).list()
        assert [entry.execution for entry in entries] == ["langgraph"]
        assert [entry.pipeline for entry in entries] == ["code-review/graph.json"]

    def test_the_pair_rides_the_canonical_manifest_json_shape(self) -> None:
        """The fixture bytes are the locked wire shape: a JSON object whose
        single top-level key is ``skills`` and whose entries carry exactly
        the closed field set."""
        parsed = json.loads(_fixture_bytes(LANGGRAPH_POSITIVE_FIXTURE))
        assert list(parsed) == ["skills"]
        entry = parsed["skills"][0]
        assert sorted(entry) == [
            "execution",
            "hash",
            "id",
            "name",
            "path",
            "pipeline",
            "size",
            "source",
        ]
