"""M6-skills P2-S1 catalog contract tests (CORE-CONTRACTS §6.3): SkillCatalog.list discovery.

Covers the T-C1-051 faces (the T-C1-052 activation faces live in
``test_skill_activation.py``):

* 正常 -- the discovery roundtrip: a manifest entry maps onto
  :class:`~opencode.skills.catalog.SkillEntry` field by field, and ``list()``
  observes only (no body is read on this face, nothing outside the manifest
  is touched; activation is the separate audited ``activate`` face).
* 失败 -- every discovery branch is one explicit
  :class:`~opencode.skills.catalog.SkillDiscoveryError`: missing directory,
  missing manifest, corrupt JSON/UTF-8, wrong manifest shape, and each entry
  violation (unknown/missing fields, empty strings, size and hash
  constraints, duplicate ids). Nothing is guessed or repaired
  (p2-slices §2.1 L96).
* 平台 -- declared paths are carried verbatim (neither separator is
  normalized, neither is resolved: bodies are never touched on the
  filesystem), ids are case-sensitive on every platform, and an unreadable
  manifest maps to the named error (simulated on every OS; real POSIX
  permission bits where they are enforceable).
* 接缝 -- :meth:`opencode.config.loader.ConfigLoader.skills_directory`
  names the conventional root from the SAME project-root determination as
  the §6 L283 layer chain, and the loader+catalog composition round-trips.
* fixture -- the JSON files under ``tests/contract/skills_fixtures/`` lock
  the manifest/entry wire shape (positive round-trip, negative rejection,
  verbatim bytes).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from opencode.config.loader import ConfigLoader
from opencode.skills import SKILLS_MANIFEST_NAME, SkillCatalog, SkillDiscoveryError, SkillEntry

SKILLS_FIXTURES_DIR = Path(__file__).parent / "skills_fixtures"

#: sha256 of the 78-byte body "Review staged changes for correctness, tests,
#: and doc drift before committing." -- an internally honest fixture pair.
VALID_DIGEST = "70152fa897ce94831325a87efae0039c6af197ef1e70527e5551e14da3731ef8"
#: sha256 of the empty body -- the size-0 boundary entry.
EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

POSITIVE_FIXTURE = "skills_manifest_valid.json"
#: The S5a manifest-extension positive fixture (T-C1-080, CORE-CONTRACTS
#: §6.3): a langgraph entry carrying the legal execution/pipeline pair.
#: Its four-path behavior tests live in test_skills_manifest_langgraph.py;
#: it joins the directory-exactness set here so the fixture face stays one
#: declared set.
LANGGRAPH_POSITIVE_FIXTURE = "skills_manifest_entry_langgraph_valid.json"
NEGATIVE_FIXTURES = [
    "skills_manifest_invalid_json.json",
    "skills_manifest_top_level_array.json",
    "skills_manifest_unknown_field.json",
    "skills_manifest_missing_skills_key.json",
    "skills_manifest_entry_missing_field.json",
    "skills_manifest_entry_unknown_field.json",
    "skills_manifest_entry_bad_size.json",
    "skills_manifest_entry_bad_hash.json",
    "skills_manifest_entry_duplicate_id.json",
    "skills_manifest_duplicate_key.json",
    "skills_manifest_entry_langgraph_missing_pipeline.json",
    "skills_manifest_entry_execution_unknown.json",
]


def _entry(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """One legal manifest entry (the positive fixture's first entry as a dict)."""
    base: dict[str, Any] = {
        "id": "code-review",
        "name": "Code Review",
        "path": "code-review/SKILL.md",
        "size": 78,
        "hash": VALID_DIGEST,
        "source": "project",
    }
    if overrides:
        base.update(overrides)
    return base


def _write_manifest(skills_dir: Path, document: object) -> Path:
    """Materialize a manifest inside a skills directory (dict or raw text)."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    manifest = skills_dir / SKILLS_MANIFEST_NAME
    if isinstance(document, str):
        manifest.write_text(document, encoding="utf-8")
    else:
        manifest.write_text(json.dumps(document), encoding="utf-8")
    return manifest


class TestDiscoveryRoundtrip:
    """正常: discovery -> list() returns the manifest entries field by field."""

    def test_manifest_entries_map_onto_skill_entry_field_by_field(self, tmp_path: Path) -> None:
        second = _entry(
            {
                "id": "empty-skill",
                "name": "Empty Skill",
                "path": "empty-skill/SKILL.md",
                "size": 0,
                "hash": EMPTY_DIGEST,
            }
        )
        _write_manifest(tmp_path, {"skills": [_entry(), second]})
        entries = SkillCatalog(tmp_path).list()
        assert len(entries) == 2
        first = entries[0]
        assert isinstance(first, SkillEntry)
        assert first.id == "code-review"
        assert first.name == "Code Review"
        assert first.path == "code-review/SKILL.md"
        assert first.size == 78
        assert first.hash == VALID_DIGEST
        assert first.source == "project"

    def test_empty_skills_list_is_a_legal_empty_discovery(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": []})
        assert SkillCatalog(tmp_path).list() == []

    def test_list_rereads_the_manifest_each_call(self, tmp_path: Path) -> None:
        """§6.3 "当前已发现的 skill 条目快照": each call returns the current
        discovery state (a per-call snapshot, not a cached view)."""
        manifest = _write_manifest(tmp_path, {"skills": [_entry()]})
        catalog = SkillCatalog(tmp_path)
        assert len(catalog.list()) == 1
        manifest.write_text(json.dumps({"skills": [_entry(), _entry({"id": "second"})]}), encoding="utf-8")
        assert [entry.id for entry in catalog.list()] == ["code-review", "second"]

    def test_size_zero_and_large_sizes_are_legal(self, tmp_path: Path) -> None:
        """§6.3: the body upper limit is a P2-S2 contract-pending face; P2-S1
        discovery checks only the non-negative integer constraint."""
        _write_manifest(tmp_path, {"skills": [_entry({"size": 0}), _entry({"id": "big", "size": 2**40})]})
        assert [entry.size for entry in SkillCatalog(tmp_path).list()] == [0, 2**40]


class TestObservationOnly:
    """list() 只观察不执行: no activation, no body read, no side effects (§6.3)."""

    def test_bodies_are_never_read_or_required(self, tmp_path: Path) -> None:
        # The declared body files do not exist at all: discovery cannot have
        # read (or even stat'ed) them, yet list() succeeds and carries the
        # declared path verbatim.
        _write_manifest(tmp_path, {"skills": [_entry()]})
        assert not (tmp_path / "code-review" / "SKILL.md").exists()
        entries = SkillCatalog(tmp_path).list()
        assert entries[0].path == "code-review/SKILL.md"

    def test_the_manifest_is_the_only_file_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = _write_manifest(tmp_path, {"skills": [_entry()]})
        (tmp_path / "decoy.txt").write_text("decoy", encoding="utf-8")
        reads: list[str] = []
        original = Path.read_text

        def _recording_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            reads.append(str(self))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _recording_read_text)
        SkillCatalog(tmp_path).list()
        assert reads == [str(manifest)]

    def test_discovery_leaves_the_tree_unchanged(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, {"skills": [_entry()]})
        (tmp_path / "code-review").mkdir()
        (tmp_path / "code-review" / "SKILL.md").write_text("body", encoding="utf-8")
        before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
        content_before = manifest.read_text(encoding="utf-8")
        SkillCatalog(tmp_path).list()
        after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
        assert after == before
        assert manifest.read_text(encoding="utf-8") == content_before

    def test_catalog_public_face_is_list_and_activate(self) -> None:
        """The §6.3 method table, both halves landed: ``list`` observes
        (P2-S1), ``activate`` audits (P2-S2) -- the catalog exposes exactly
        these two."""
        public = [name for name in dir(SkillCatalog) if not name.startswith("_")]
        assert public == ["activate", "list"]


class TestDiscoveryFailures:
    """失败: every branch is an explicit SkillDiscoveryError -- no repair, no guessing."""

    def test_missing_skills_directory(self, tmp_path: Path) -> None:
        with pytest.raises(SkillDiscoveryError, match=r"skills directory .* does not exist"):
            SkillCatalog(tmp_path / "nope").list()

    def test_missing_manifest(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SkillDiscoveryError, match=r"skills manifest .* does not exist"):
            SkillCatalog(tmp_path).list()

    def test_manifest_path_occupied_by_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).mkdir(parents=True)
        with pytest.raises(SkillDiscoveryError, match=r"skills manifest .* does not exist"):
            SkillCatalog(tmp_path).list()

    def test_manifest_is_not_valid_json(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, '{"skills": [')
        with pytest.raises(SkillDiscoveryError, match="not valid JSON"):
            SkillCatalog(tmp_path).list()

    def test_manifest_is_not_valid_utf8(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes(b'{"skills": "\xff\xfe"}')
        with pytest.raises(SkillDiscoveryError, match="not valid UTF-8"):
            SkillCatalog(tmp_path).list()

    def test_manifest_top_level_is_not_an_object(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, "[]")
        with pytest.raises(SkillDiscoveryError, match="must contain a JSON object"):
            SkillCatalog(tmp_path).list()

    def test_manifest_unknown_top_level_field(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [], "version": 1})
        with pytest.raises(SkillDiscoveryError, match="unknown skills manifest field 'version'"):
            SkillCatalog(tmp_path).list()

    def test_manifest_missing_skills_key(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {})
        with pytest.raises(SkillDiscoveryError, match="missing the required 'skills' list"):
            SkillCatalog(tmp_path).list()

    def test_skills_value_is_not_a_list(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": {"id": "x"}})
        with pytest.raises(SkillDiscoveryError, match="must be a list of entry objects"):
            SkillCatalog(tmp_path).list()

    def test_entry_is_not_an_object(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": ["code-review"]})
        with pytest.raises(SkillDiscoveryError, match=r"skills entry \[0\].* must be a JSON object"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.parametrize("field", ["id", "name", "path", "source"])
    @pytest.mark.parametrize("bad", ["", 5, None, True])
    def test_string_fields_must_be_non_empty_strings(self, tmp_path: Path, field: str, bad: Any) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({field: bad})]})
        with pytest.raises(SkillDiscoveryError, match=rf"{field}.* must be a non-empty string"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.parametrize("field", ["hash", "id", "name", "path", "size", "source"])
    def test_missing_entry_field(self, tmp_path: Path, field: str) -> None:
        entry = _entry()
        del entry[field]
        _write_manifest(tmp_path, {"skills": [entry]})
        with pytest.raises(SkillDiscoveryError, match="missing required field"):
            SkillCatalog(tmp_path).list()

    def test_unknown_entry_field(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({"description": "extra"})]})
        with pytest.raises(SkillDiscoveryError, match="unknown field 'description'"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.parametrize("bad_size", [-1, -(2**31), 1.5, "78", True, None])
    def test_size_must_be_a_non_negative_integer(self, tmp_path: Path, bad_size: Any) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({"size": bad_size})]})
        with pytest.raises(SkillDiscoveryError, match=r"size.* must be (an integer|non-negative)"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "",
            VALID_DIGEST.upper(),  # uppercase hex
            VALID_DIGEST[:-1],  # 63 chars
            VALID_DIGEST + "8",  # 65 chars
            "z" * 64,  # non-hex
            f"sha256:{VALID_DIGEST}",  # prefixed
            42,
        ],
    )
    def test_hash_must_be_exactly_64_lowercase_hex(self, tmp_path: Path, bad_hash: Any) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({"hash": bad_hash})]})
        with pytest.raises(SkillDiscoveryError, match=r"hash.* must be exactly 64 lowercase hex"):
            SkillCatalog(tmp_path).list()

    def test_duplicate_id_within_one_manifest(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [_entry(), _entry({"name": "Same id, different name"})]})
        with pytest.raises(SkillDiscoveryError, match="duplicate skill id 'code-review'"):
            SkillCatalog(tmp_path).list()

    def test_error_names_the_manifest_and_entry_index(self, tmp_path: Path) -> None:
        manifest = _write_manifest(tmp_path, {"skills": [_entry(), _entry({"id": "broken", "size": -1})]})
        with pytest.raises(SkillDiscoveryError, match=rf"skills entry \[1\].*{re.escape(manifest.name)}"):
            SkillCatalog(tmp_path).list()


class TestPlatformFaces:
    """平台: separators/case carried verbatim; permission failures are named errors."""

    @pytest.mark.parametrize("declared_path", ["code-review/SKILL.md", "code-review\\SKILL.md", "code-review"])
    def test_declared_paths_are_carried_verbatim(self, tmp_path: Path, declared_path: str) -> None:
        """Neither separator is normalized and neither is resolved against the
        skills directory: list() never touches the filesystem for bodies, so
        the declaration survives byte for byte on Windows and POSIX alike."""
        _write_manifest(tmp_path, {"skills": [_entry({"path": declared_path})]})
        assert SkillCatalog(tmp_path).list()[0].path == declared_path

    def test_ids_are_case_sensitive_on_every_platform(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [_entry(), _entry({"id": "Code-Review", "name": "Distinct"})]})
        assert [entry.id for entry in SkillCatalog(tmp_path).list()] == ["code-review", "Code-Review"]

    def test_uppercase_hash_is_rejected_regardless_of_platform(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {"skills": [_entry({"hash": VALID_DIGEST.upper()})]})
        with pytest.raises(SkillDiscoveryError, match="64 lowercase hex"):
            SkillCatalog(tmp_path).list()

    def test_unreadable_manifest_is_a_named_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_manifest(tmp_path, {"skills": [_entry()]})

        def _denied(self: Path, *args: Any, **kwargs: Any) -> str:
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(Path, "read_text", _denied)
        with pytest.raises(SkillDiscoveryError, match="cannot read skills manifest"):
            SkillCatalog(tmp_path).list()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits are not enforceable on Windows")
    def test_manifest_without_read_permission(self, tmp_path: Path) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads through mode 000")
        manifest = _write_manifest(tmp_path, {"skills": [_entry()]})
        os.chmod(manifest, 0o000)
        try:
            with pytest.raises(SkillDiscoveryError, match="cannot read skills manifest"):
                SkillCatalog(tmp_path).list()
        finally:
            os.chmod(manifest, 0o644)


class TestLoaderSeam:
    """The S5 discovery seam: ConfigLoader.skills_directory (the conventional root)."""

    def test_conventional_root_under_the_project_root(self, tmp_path: Path) -> None:
        loader = ConfigLoader(working_dir=tmp_path)
        assert loader.skills_directory() == tmp_path.resolve() / ".opencode-py" / "skills"

    def test_root_follows_the_vcs_marker_not_the_working_dir(self, tmp_path: Path) -> None:
        # A .git FILE (the worktree form) is a root marker, same as the layer chain.
        (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        nested = tmp_path / "sub"
        nested.mkdir()
        loader = ConfigLoader(working_dir=nested)
        assert loader.skills_directory() == tmp_path.resolve() / ".opencode-py" / "skills"

    def test_pinned_project_root_wins(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub"
        nested.mkdir()
        loader = ConfigLoader(working_dir=nested, project_root=tmp_path)
        assert loader.skills_directory() == tmp_path.resolve() / ".opencode-py" / "skills"

    def test_composition_roundtrip(self, tmp_path: Path) -> None:
        """The full P2-S1 composition: the loader names the conventional root,
        the catalog discovers the manifest inside it (session/app wiring is
        outside this slice)."""
        skills_dir = tmp_path / ".opencode-py" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / SKILLS_MANIFEST_NAME).write_text(json.dumps({"skills": [_entry()]}), encoding="utf-8")
        loader = ConfigLoader(working_dir=tmp_path)
        entries = SkillCatalog(loader.skills_directory()).list()
        assert [entry.id for entry in entries] == ["code-review"]


class TestWireShapeFixtures:
    """fixture: the skills_fixtures JSON files lock the manifest/entry wire shape."""

    def test_the_fixture_directory_is_exactly_the_declared_set(self) -> None:
        assert sorted(path.name for path in SKILLS_FIXTURES_DIR.glob("*.json")) == sorted(
            [POSITIVE_FIXTURE, LANGGRAPH_POSITIVE_FIXTURE, *NEGATIVE_FIXTURES]
        )

    def test_positive_fixture_round_trips_field_by_field(self, tmp_path: Path) -> None:
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes((SKILLS_FIXTURES_DIR / POSITIVE_FIXTURE).read_bytes())
        entries = SkillCatalog(tmp_path).list()
        assert [(e.id, e.name, e.path, e.size, e.hash, e.source) for e in entries] == [
            ("code-review", "Code Review", "code-review/SKILL.md", 78, VALID_DIGEST, "project"),
            ("empty-skill", "Empty Skill", "empty-skill/SKILL.md", 0, EMPTY_DIGEST, "project"),
        ]

    @pytest.mark.parametrize("name", NEGATIVE_FIXTURES)
    def test_negative_fixture_is_rejected_verbatim(self, tmp_path: Path, name: str) -> None:
        """The fixture bytes are copied verbatim into the manifest slot; each
        must fail with the named error (no repair, no guessing)."""
        (tmp_path / SKILLS_MANIFEST_NAME).write_bytes((SKILLS_FIXTURES_DIR / name).read_bytes())
        with pytest.raises(SkillDiscoveryError):
            SkillCatalog(tmp_path).list()
