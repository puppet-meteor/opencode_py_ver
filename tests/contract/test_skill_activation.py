"""M6-skills P2-S2 contract tests (CORE-CONTRACTS §6.3): skill activation.

Covers the T-C1-052 faces:

* 正常 -- the §6.3 audit: ``SkillCatalog.activate`` returns the receipt
  five-tuple (skill_id/source/path/hash/size) plus the checked body; the §7
  host command (``SessionService.activate``) commits the durable
  ``py.skill.activated`` event under the session lock, the fold registers
  the record keyed by declared path, and recovery replays the saved
  content from the state fold -- the body file may be gone, the disk is
  never re-read (§6.3 L345/L354).
* 失败 -- the fixed 大小→路径→信任→hash order: every segment rejects with a
  named :class:`SkillActivationError` (over-cap size, size mismatch, stat
  failure, traversal/absolute escape, directory, non-UTF-8, empty body,
  digest mismatch, entry-hash mismatch); the order is fail-fast (an earlier
  segment fires when a later one would also fail); source mismatch and
  unknown ids reject before the segments; a manifest with duplicate JSON
  keys answers :class:`SkillDiscoveryError` (parse rejection, never
  last-wins); the §7 face refuses without the catalog/engine wiring, on
  unknown/closed sessions, and the §6.3 L347-50 re-activation gate refuses
  a changed hash (same skill id OR same declared path) without the explicit
  ``confirm_hash_change=True``.
* 触发面封闭 -- the closed set: only the host command activates; discovery
  (``list()``), prompts and one skill's own body text never produce a
  ``py.skill.activated`` event (§6.3 L351-353: content grants no
  permissions), and the durable record is exactly the six contract fields
  -- no permission or tool face rides along.
* 平台 -- declared paths are carried verbatim ("/" everywhere; "\\" resolves
  only where the OS treats it as a separator, and case folds only on a
  case-insensitive filesystem -- the receipt still carries the declared
  string); an unreadable body rejects in the trust segment (simulated on
  every OS, real POSIX permission bits where they are enforceable); the
  exactly-64-KiB body sits inside the cap.
* fixture -- the files under ``tests/contract/skills_activation_fixtures/``
  lock the positive R5 envelope ``events_skill_activated_valid.json``
  (closing the T-B-059 25↔26 registry/fixture gap) and the canonical
  78-byte body; the manifest+body fixture pair round-trips through
  activation. The frozen ``tests/contract/fixtures/`` census stays
  untouched (test_schema_lock.py pins it at 92).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.bus import VolatileBus
from opencode.bus.eventlog import MAX_READ_LIMIT, EventCursor, EventLog
from opencode.contracts import default_event_registry
from opencode.contracts.events import PY_SKILL_ACTIVATED, SKILL_BODY_MAX_BYTES, SkillActivatedData
from opencode.session import SessionProjector, SessionService
from opencode.session.service import REASON_SKILL_HASH_CHANGED
from opencode.session.state import SessionState, SkillInfo, apply
from opencode.skills import (
    SKILLS_MANIFEST_NAME,
    SkillActivationError,
    SkillActivationReceipt,
    SkillCatalog,
    SkillDiscoveryError,
)
from opencode.storage import (
    NotFoundError,
    ReadRepositories,
    SessionClosedError,
    SessionStore,
    StoreValidationError,
    open_database,
)
from opencode.storage.txn import ConflictError

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_FIXTURES_DIR = Path(__file__).parent / "skills_fixtures"
ACTIVATION_FIXTURES_DIR = Path(__file__).parent / "skills_activation_fixtures"

#: The locked 78-byte body and its sha256 (same pair as the skills fixtures).
VALID_BODY = "Review staged changes for correctness, tests, and doc drift before committing."
VALID_DIGEST = "70152fa897ce94831325a87efae0039c6af197ef1e70527e5551e14da3731ef8"
EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_SNAPSHOT: dict[str, Any] = {"schema_version": 1, "permission_rules": []}

#: POSIX-only root check for the real permission-bits case (root reads
#: through mode 0o000, so the case would false-fail).
_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _entry(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """One legal manifest entry for the canonical code-review skill."""
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


def _write_body(skills_dir: Path, rel: str, body: bytes) -> Path:
    """Write one skill body under the discovery root."""
    path = skills_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fixture_skills_dir(parent: Path) -> Path:
    """The canonical activation dir: the locked manifest + the locked 78-byte body."""
    skills_dir = parent / "skills"
    body = (ACTIVATION_FIXTURES_DIR / "code-review" / "SKILL.md").read_bytes()
    _write_body(skills_dir, "code-review/SKILL.md", body)
    _write_manifest(skills_dir, {"skills": [_entry()]})
    return skills_dir


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout: {result.stdout}\nstderr: {result.stderr}"


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


async def _fold_state(engine: AsyncEngine, session_id: str) -> SessionState:
    """Fold the session's whole committed log through the §6.3 state fold."""
    log = EventLog(engine)
    state = SessionState()
    after: EventCursor | None = None
    while True:
        page = await log.read(session_id, after, MAX_READ_LIMIT)
        for event in page.items:
            state = apply(state, event)
        if not page.has_more:
            break
        after = page.next_cursor
    return state


@asynccontextmanager
async def _service(db_path: Path, skills_dir: Path | None = None) -> AsyncIterator[tuple[SessionService, AsyncEngine]]:
    """One storage-backed service over an already-migrated db_path."""
    async with open_database(db_path, mode="create") as database:
        yield (
            SessionService(
                store=SessionStore(database.engine, projector=SessionProjector()),
                repos=ReadRepositories(database.engine),
                bus=VolatileBus(),
                working_dir="C:/work",
                engine=database.engine,
                catalog=SkillCatalog(skills_dir) if skills_dir is not None else None,
            ),
            database.engine,
        )


# -- 正常: the §6.3 audit face ------------------------------------------------


class TestActivationAudit:
    """正常: the receipt, the durable commit, the fold, and the no-disk restore."""

    def test_receipt_carries_the_audit_five_tuple_and_the_checked_body(self, tmp_path: Path) -> None:
        receipt = SkillCatalog(_fixture_skills_dir(tmp_path)).activate("code-review", "project", VALID_DIGEST)
        assert isinstance(receipt, SkillActivationReceipt)
        assert receipt.skill_id == "code-review"
        assert receipt.source == "project"
        assert receipt.path == "code-review/SKILL.md"
        assert receipt.hash == VALID_DIGEST
        assert receipt.size == 78
        assert receipt.body == VALID_BODY
        with pytest.raises(FrozenInstanceError):
            receipt.hash = "0" * 64  # type: ignore[misc]

    def test_activation_grants_nothing_beyond_the_audit_record(self, tmp_path: Path) -> None:
        """§6.3 L351-353: the receipt's durable face is exactly the six
        contract fields -- no permission, tool or run face rides along."""
        receipt = SkillCatalog(_fixture_skills_dir(tmp_path)).activate("code-review", "project", VALID_DIGEST)
        record = receipt.durable_record()
        assert isinstance(record, SkillActivatedData)
        assert set(SkillActivatedData.model_fields) == {"skill_id", "source", "path", "hash", "size", "body"}
        assert record.model_dump() == {
            "skill_id": "code-review",
            "source": "project",
            "path": "code-review/SKILL.md",
            "hash": VALID_DIGEST,
            "size": 78,
            "body": VALID_BODY,
        }

    async def test_activate_commits_the_durable_event_and_folds_the_record(self, tmp_path: Path) -> None:
        """The §7 face: check order outside the lock, gates + commit inside;
        the projection advances (the no-row dispatch branch) and the fold
        registers the record keyed by the declared path."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, engine):
            session = await service.create(_SNAPSHOT)
            receipt = await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            assert receipt.body == VALID_BODY
            rows = _fetch_all(
                db_path,
                "SELECT type, seq FROM events WHERE session_id = ? ORDER BY seq",
                (session.session_id,),
            )
            assert rows == [("py.session.created", 1), ("py.skill.activated", 2)]
            applied = _fetch_scalar(
                db_path, "SELECT applied_seq FROM sessions WHERE session_id = ?", (session.session_id,)
            )
            assert applied is not None and applied[0] == 2
            state = await _fold_state(engine, session.session_id)
            info = state.skills["code-review/SKILL.md"]
            assert isinstance(info, SkillInfo)
            assert (info.skill_id, info.source, info.path, info.hash, info.size, info.body) == (
                "code-review",
                "project",
                "code-review/SKILL.md",
                VALID_DIGEST,
                78,
                VALID_BODY,
            )

    async def test_activation_is_session_scoped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, engine):
            session_a = await service.create(_SNAPSHOT)
            session_b = await service.create(_SNAPSHOT)
            await service.activate(session_a.session_id, "code-review", "project", VALID_DIGEST)
            rows_b = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ?", (session_b.session_id,))
            assert rows_b == [("py.session.created",)]
            assert (await _fold_state(engine, session_b.session_id)).skills == {}

    async def test_recovery_restores_the_saved_content_without_reading_disk(self, tmp_path: Path) -> None:
        """§6.3 L345/L354: the state fold carries the saved body -- after the
        skills directory is deleted entirely, the committed log alone
        restores the record (any disk re-read would fail here)."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        work = tmp_path / "work"
        work.mkdir()
        skills_dir = _fixture_skills_dir(work)
        async with _service(db_path, skills_dir) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
        shutil.rmtree(work)
        async with open_database(db_path, mode="create") as database:
            state = await _fold_state(database.engine, session.session_id)
        info = state.skills["code-review/SKILL.md"]
        assert isinstance(info, SkillInfo)
        assert info.body == VALID_BODY
        assert info.hash == VALID_DIGEST
        assert info.size == 78
        assert state.applied_seq == 2

    async def test_same_hash_reactivation_is_idempotent_and_commits_a_second_audit_event(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, engine):
            session = await service.create(_SNAPSHOT)
            first = await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            second = await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            assert first == second
            rows = _fetch_all(
                db_path,
                "SELECT type, seq FROM events WHERE session_id = ? ORDER BY seq",
                (session.session_id,),
            )
            assert [row[0] for row in rows] == [
                "py.session.created",
                "py.skill.activated",
                "py.skill.activated",
            ]
            state = await _fold_state(engine, session.session_id)
            assert list(state.skills) == ["code-review/SKILL.md"]
            assert state.skills["code-review/SKILL.md"].hash == VALID_DIGEST

    async def test_confirmed_hash_change_commits_the_new_record(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, engine):
            session = await service.create(_SNAPSHOT)
            await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            new_body = b"Review staged changes for correctness, tests, and doc drift before merging."
            _write_body(skills_dir, "code-review/SKILL.md", new_body)
            _write_manifest(skills_dir, {"skills": [_entry({"size": len(new_body), "hash": _digest(new_body)})]})
            new_hash = _digest(new_body)
            receipt = await service.activate(
                session.session_id, "code-review", "project", new_hash, confirm_hash_change=True
            )
            assert receipt.hash == new_hash
            assert receipt.size == len(new_body)
            state = await _fold_state(engine, session.session_id)
            assert state.skills["code-review/SKILL.md"].hash == new_hash


# -- 失败: the fixed check order + the §7/§6.3 refusals -------------------------


class TestCheckOrderRefusals:
    """失败: 大小→路径→信任→hash, fail-fast, each segment named."""

    def test_over_cap_declared_size_is_rejected(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", b"x" * (SKILL_BODY_MAX_BYTES + 1))
        _write_manifest(skills_dir, {"skills": [_entry({"size": SKILL_BODY_MAX_BYTES + 1, "hash": "0" * 64})]})
        with pytest.raises(SkillActivationError, match="size check"):
            SkillCatalog(skills_dir).activate("code-review", "project", "0" * 64)

    def test_size_mismatch_is_rejected(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", b"short body")
        _write_manifest(skills_dir, {"skills": [_entry()]})
        with pytest.raises(SkillActivationError, match="size check"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_stat_failure_is_rejected_in_the_size_segment(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_manifest(skills_dir, {"skills": [_entry()]})  # no body file at all
        with pytest.raises(SkillActivationError, match="size check: cannot stat"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_traversal_path_is_rejected(self, tmp_path: Path) -> None:
        escape = tmp_path / "outside"
        escape.mkdir()
        (escape / "SKILL.md").write_bytes(VALID_BODY.encode("utf-8"))
        skills_dir = tmp_path / "skills"
        _write_manifest(skills_dir, {"skills": [_entry({"path": "../outside/SKILL.md"})]})
        with pytest.raises(SkillActivationError, match="path check"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_absolute_declared_path_is_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "SKILL.md").write_bytes(VALID_BODY.encode("utf-8"))
        skills_dir = tmp_path / "skills"
        _write_manifest(skills_dir, {"skills": [_entry({"path": str((outside / "SKILL.md").resolve())})]})
        with pytest.raises(SkillActivationError, match="path check"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_directory_declared_path_is_rejected(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        body_dir = skills_dir / "code-review"
        body_dir.mkdir(parents=True)
        dir_size = body_dir.stat().st_size
        _write_manifest(skills_dir, {"skills": [_entry({"path": "code-review", "size": dir_size})]})
        with pytest.raises(SkillActivationError, match="path check"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_non_utf8_body_is_rejected_in_the_trust_segment(self, tmp_path: Path) -> None:
        raw = (ACTIVATION_FIXTURES_DIR / "code-review-bad-utf8" / "SKILL.body").read_bytes()
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", raw)
        _write_manifest(skills_dir, {"skills": [_entry({"size": len(raw), "hash": _digest(raw)})]})
        with pytest.raises(SkillActivationError, match="trust check"):
            SkillCatalog(skills_dir).activate("code-review", "project", _digest(raw))

    def test_empty_body_is_rejected_in_the_trust_segment(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", b"")
        _write_manifest(skills_dir, {"skills": [_entry({"size": 0, "hash": EMPTY_DIGEST})]})
        with pytest.raises(SkillActivationError, match="trust check"):
            SkillCatalog(skills_dir).activate("code-review", "project", EMPTY_DIGEST)

    def test_digest_mismatch_is_rejected_in_the_hash_segment(self, tmp_path: Path) -> None:
        skills_dir = _fixture_skills_dir(tmp_path)
        with pytest.raises(SkillActivationError, match="hash check: the body digest"):
            SkillCatalog(skills_dir).activate("code-review", "project", "a" * 64)

    def test_entry_hash_mismatch_is_rejected_in_the_hash_segment(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", VALID_BODY.encode("utf-8"))
        _write_manifest(skills_dir, {"skills": [_entry({"hash": "b" * 64})]})
        with pytest.raises(SkillActivationError, match="hash check: the discovered entry hash"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_size_runs_before_path(self, tmp_path: Path) -> None:
        """Order discipline: an over-cap size fires even though the declared
        path is also an escaping traversal."""
        skills_dir = tmp_path / "skills"
        _write_manifest(
            skills_dir,
            {"skills": [_entry({"size": SKILL_BODY_MAX_BYTES + 1, "path": "../outside/SKILL.md"})]},
        )
        with pytest.raises(SkillActivationError, match="size check"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_path_runs_before_trust(self, tmp_path: Path) -> None:
        """Order discipline: a traversal path fires even though the file it
        names would also fail the UTF-8 trust segment."""
        escape = tmp_path / "outside"
        escape.mkdir()
        raw = (ACTIVATION_FIXTURES_DIR / "code-review-bad-utf8" / "SKILL.body").read_bytes()
        (escape / "SKILL.md").write_bytes(raw)
        skills_dir = tmp_path / "skills"
        _write_manifest(
            skills_dir,
            {"skills": [_entry({"path": "../outside/SKILL.md", "size": len(raw), "hash": _digest(raw)})]},
        )
        with pytest.raises(SkillActivationError, match="path check"):
            SkillCatalog(skills_dir).activate("code-review", "project", _digest(raw))

    def test_trust_runs_before_hash(self, tmp_path: Path) -> None:
        """Order discipline: a non-UTF-8 body fires even though the expected
        hash would also fail the hash segment."""
        raw = (ACTIVATION_FIXTURES_DIR / "code-review-bad-utf8" / "SKILL.body").read_bytes()
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", raw)
        _write_manifest(skills_dir, {"skills": [_entry({"size": len(raw), "hash": _digest(raw)})]})
        with pytest.raises(SkillActivationError, match="trust check"):
            SkillCatalog(skills_dir).activate("code-review", "project", "c" * 64)

    def test_source_mismatch_rejects_before_the_segments(self, tmp_path: Path) -> None:
        skills_dir = _fixture_skills_dir(tmp_path)
        (skills_dir / "code-review" / "SKILL.md").unlink()  # the size segment would also fail
        with pytest.raises(SkillActivationError, match="does not equal the discovered entry source"):
            SkillCatalog(skills_dir).activate("code-review", "user", VALID_DIGEST)

    def test_unknown_skill_id_is_rejected(self, tmp_path: Path) -> None:
        skills_dir = _fixture_skills_dir(tmp_path)
        with pytest.raises(SkillActivationError, match="not in the current discovery manifest"):
            SkillCatalog(skills_dir).activate("nope", "project", VALID_DIGEST)

    def test_activate_arguments_are_validated(self, tmp_path: Path) -> None:
        skills_dir = _fixture_skills_dir(tmp_path)
        catalog = SkillCatalog(skills_dir)
        with pytest.raises(SkillActivationError, match="must be a non-empty string"):
            catalog.activate(123, "project", VALID_DIGEST)  # type: ignore[arg-type]
        with pytest.raises(SkillActivationError, match="must be a non-empty string"):
            catalog.activate("code-review", "project", "")

    def test_duplicate_manifest_key_is_a_discovery_rejection(self, tmp_path: Path) -> None:
        """票内定夺 b: a repeated manifest key is corrupt, never last-wins."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / SKILLS_MANIFEST_NAME).write_bytes(
            (SKILLS_FIXTURES_DIR / "skills_manifest_duplicate_key.json").read_bytes()
        )
        with pytest.raises(SkillDiscoveryError, match="duplicate JSON object key"):
            SkillCatalog(skills_dir).list()

    def test_corrupt_manifest_answers_the_discovery_error(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        _write_manifest(skills_dir, '{"skills": [')
        with pytest.raises(SkillDiscoveryError, match="not valid JSON"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)


class TestServiceRefusals:
    """失败: the §7 host command face refuses without wiring, on damaged
    sessions, and on an unconfirmed hash change."""

    async def test_activate_without_the_catalog_fails_closed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        async with _service(db_path, None) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            with pytest.raises(StoreValidationError, match="activate requires the skill catalog"):
                await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)

    async def test_activate_without_the_engine_fails_closed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with open_database(db_path, mode="create") as database:
            service = SessionService(
                store=SessionStore(database.engine, projector=SessionProjector()),
                repos=ReadRepositories(database.engine),
                bus=VolatileBus(),
                working_dir="C:/work",
                catalog=SkillCatalog(skills_dir),
            )
            session = await service.create(_SNAPSHOT)
            with pytest.raises(StoreValidationError, match="activate requires the storage engine"):
                await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)

    async def test_activate_on_an_unknown_session_is_not_found(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, _engine):
            with pytest.raises(NotFoundError):
                await service.activate("ses_0000000000000000000000unknown1", "code-review", "project", VALID_DIGEST)

    async def test_activate_validates_its_arguments(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            with pytest.raises(StoreValidationError, match=r"\^ses_"):
                await service.activate("not-a-session-id", "code-review", "project", VALID_DIGEST)
            with pytest.raises(StoreValidationError, match="confirm_hash_change must be a bool"):
                await service.activate(
                    session.session_id,
                    "code-review",
                    "project",
                    VALID_DIGEST,
                    confirm_hash_change="yes",  # type: ignore[arg-type]
                )

    async def test_closed_service_refuses_activation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            await service.close()
            with pytest.raises(SessionClosedError, match="service is closed"):
                await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)

    async def test_hash_change_requires_explicit_confirmation(self, tmp_path: Path) -> None:
        """§6.3 L347-350: the same skill id with a different body hash is
        refused by default and names the confirmation face."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            new_body = b"Review staged changes for correctness, tests, and doc drift before merging."
            _write_body(skills_dir, "code-review/SKILL.md", new_body)
            _write_manifest(skills_dir, {"skills": [_entry({"size": len(new_body), "hash": _digest(new_body)})]})
            with pytest.raises(ConflictError, match="confirm_hash_change=True") as info:
                await service.activate(session.session_id, "code-review", "project", _digest(new_body))
            assert info.value.core.details["reason"] == REASON_SKILL_HASH_CHANGED

    async def test_hash_change_on_the_same_path_face_requires_confirmation(self, tmp_path: Path) -> None:
        """The conservative union: a different skill id declaring the SAME
        path with a changed hash is refused too (the host may only have been
        looking at the id face)."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, _engine):
            session = await service.create(_SNAPSHOT)
            await service.activate(session.session_id, "code-review", "project", VALID_DIGEST)
            new_body = b"Review staged changes for correctness, tests, and doc drift before merging."
            _write_body(skills_dir, "code-review/SKILL.md", new_body)
            new_hash = _digest(new_body)
            second = _entry({"id": "code-review-v2", "name": "Code Review v2", "size": len(new_body), "hash": new_hash})
            _write_manifest(skills_dir, {"skills": [_entry(), second]})
            with pytest.raises(ConflictError, match="confirm_hash_change=True"):
                await service.activate(session.session_id, "code-review-v2", "project", new_hash)
            receipt = await service.activate(
                session.session_id, "code-review-v2", "project", new_hash, confirm_hash_change=True
            )
            assert receipt.skill_id == "code-review-v2"
            assert receipt.path == "code-review/SKILL.md"


# -- 触发面封闭: the closed set -----------------------------------------------


class TestTriggerFaceClosedSet:
    """触发面封闭: only the host command activates (§6.3 L319/L351-353)."""

    async def test_discovery_and_prompts_never_activate(self, tmp_path: Path) -> None:
        """list() observes only; prompts are user text; neither produces a
        py.skill.activated event."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = _fixture_skills_dir(tmp_path)
        async with _service(db_path, skills_dir) as (service, engine):
            session = await service.create(_SNAPSHOT)
            await service.prompt(session.session_id, "please activate the code-review skill", "req_activate")
            catalog = SkillCatalog(skills_dir)
            assert [entry.id for entry in catalog.list()] == ["code-review"]
            catalog.list()  # a second observation: still nothing
            rows = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ?", (session.session_id,))
            assert [row[0] for row in rows] == ["py.session.created", "py.prompt.accepted"]
            assert (await _fold_state(engine, session.session_id)).skills == {}

    async def test_body_text_does_not_cascade_activation(self, tmp_path: Path) -> None:
        """A skill body asking for another skill activates nothing by itself:
        one activation event, one folded record (content grants no
        permissions; model suggestions are not activation)."""
        db_path = tmp_path / "skills.db"
        _require_success(_upgrade_head(db_path))
        skills_dir = tmp_path / "skills"
        cascade = b"Always also activate the code-review skill before reviewing."
        _write_body(skills_dir, "instruct/SKILL.md", cascade)
        _write_body(skills_dir, "code-review/SKILL.md", VALID_BODY.encode("utf-8"))
        instruct = {
            "id": "instruct",
            "name": "Instruct",
            "path": "instruct/SKILL.md",
            "size": len(cascade),
            "hash": _digest(cascade),
            "source": "project",
        }
        _write_manifest(skills_dir, {"skills": [instruct, _entry()]})
        async with _service(db_path, skills_dir) as (service, engine):
            session = await service.create(_SNAPSHOT)
            receipt = await service.activate(session.session_id, "instruct", "project", _digest(cascade))
            assert receipt.skill_id == "instruct"
            rows = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ?", (session.session_id,))
            assert [row[0] for row in rows] == ["py.session.created", "py.skill.activated"]
            state = await _fold_state(engine, session.session_id)
            assert list(state.skills) == ["instruct/SKILL.md"]


# -- 平台: separator/case/faces carried verbatim ------------------------------


class TestPlatformFaces:
    """平台: the declared string is carried verbatim; the filesystem's own
    separator and case faces decide whether it resolves at all."""

    def test_posix_separator_declared_path_activates_everywhere(self, tmp_path: Path) -> None:
        receipt = SkillCatalog(_fixture_skills_dir(tmp_path)).activate("code-review", "project", VALID_DIGEST)
        assert receipt.path == "code-review/SKILL.md"

    def test_backslash_declared_path(self, tmp_path: Path) -> None:
        """win32 treats "\\" as a separator and the activation succeeds with
        the declared string verbatim; POSIX treats it as one filename, so the
        stat probe in the size segment fails."""
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", VALID_BODY.encode("utf-8"))
        _write_manifest(skills_dir, {"skills": [_entry({"path": "code-review\\SKILL.md"})]})
        if os.name == "nt":
            receipt = SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)
            assert receipt.path == "code-review\\SKILL.md"
        else:
            with pytest.raises(SkillActivationError, match="size check: cannot stat"):
                SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_case_mismatch_declared_path(self, tmp_path: Path) -> None:
        """A case-insensitive filesystem resolves the mismatched spelling (the
        declared string still rides the receipt verbatim); a case-sensitive
        one answers the size-segment stat failure."""
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", VALID_BODY.encode("utf-8"))
        _write_manifest(skills_dir, {"skills": [_entry({"path": "CODE-REVIEW/SKILL.md"})]})
        if os.name == "nt":
            receipt = SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)
            assert receipt.path == "CODE-REVIEW/SKILL.md"
        else:
            with pytest.raises(SkillActivationError, match="size check: cannot stat"):
                SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    def test_unreadable_body_rejects_in_the_trust_segment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulated on every OS: the read itself fails after the size and
        path segments passed (the stat face is not the read face)."""
        skills_dir = _fixture_skills_dir(tmp_path)

        def _refuse(self: Path) -> bytes:
            raise OSError("permission denied (simulated)")

        monkeypatch.setattr(Path, "read_bytes", _refuse)
        with pytest.raises(SkillActivationError, match="trust check: cannot read"):
            SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)

    @pytest.mark.skipif(
        sys.platform == "win32" or _ROOT, reason="real POSIX permission bits (not enforceable on win32/root)"
    )
    def test_mode_000_body_rejects_in_the_trust_segment_on_posix(self, tmp_path: Path) -> None:
        skills_dir = _fixture_skills_dir(tmp_path)
        body = skills_dir / "code-review" / "SKILL.md"
        body.chmod(0o000)
        try:
            with pytest.raises(SkillActivationError, match="trust check: cannot read"):
                SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)
        finally:
            body.chmod(0o644)

    def test_exactly_cap_sized_body_is_inside_the_budget(self, tmp_path: Path) -> None:
        """The §6 L289 cap is inclusive: a body of exactly 64 KiB passes (an
        over-cap byte count rejects in the size segment, per 失败)."""
        body = b"x" * SKILL_BODY_MAX_BYTES
        skills_dir = tmp_path / "skills"
        _write_body(skills_dir, "code-review/SKILL.md", body)
        _write_manifest(skills_dir, {"skills": [_entry({"size": len(body), "hash": _digest(body)})]})
        receipt = SkillCatalog(skills_dir).activate("code-review", "project", _digest(body))
        assert receipt.size == SKILL_BODY_MAX_BYTES


# -- fixture: the locked companion set ----------------------------------------


class TestFixtureLocks:
    """fixture: the activation fixtures lock the R5 envelope + the bodies."""

    def test_activation_fixture_dir_is_exactly_the_declared_set(self) -> None:
        files = sorted(
            path.relative_to(ACTIVATION_FIXTURES_DIR).as_posix()
            for path in ACTIVATION_FIXTURES_DIR.rglob("*")
            if path.is_file()
        )
        assert files == [
            "code-review-bad-utf8/SKILL.body",
            "code-review/SKILL.md",
            "events_skill_activated_valid.json",
        ]

    def test_r5_positive_fixture_decodes_as_a_durable_envelope(self) -> None:
        """The T-B-059 gap closure: ``py.skill.activated`` now has its positive
        event fixture, decodable by the default registry with the exact six
        data fields and an empty (not run-scoped) metadata face."""
        raw = (ACTIVATION_FIXTURES_DIR / "events_skill_activated_valid.json").read_bytes()
        event = default_event_registry().decode_durable(raw)
        assert event.type == PY_SKILL_ACTIVATED
        assert event.version == 1
        assert event.session_id == "ses_0000000000000000000000000001"
        assert event.seq == 2
        assert event.metadata == {}
        assert isinstance(event.data, SkillActivatedData)
        assert event.data.model_dump() == json.loads(raw)["data"]
        assert event.data.body == VALID_BODY
        assert event.data.hash == VALID_DIGEST
        assert event.data.size == 78
        assert event.data.source == "project"
        assert event.data.path == "code-review/SKILL.md"

    def test_manifest_body_fixture_pair_round_trips_through_activation(self, tmp_path: Path) -> None:
        """The locked manifest declares exactly the locked 78-byte body:
        discovery reads it field by field, activation checks it end to end."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / SKILLS_MANIFEST_NAME).write_bytes(
            (SKILLS_FIXTURES_DIR / "skills_manifest_valid.json").read_bytes()
        )
        _write_body(
            skills_dir, "code-review/SKILL.md", (ACTIVATION_FIXTURES_DIR / "code-review" / "SKILL.md").read_bytes()
        )
        receipt = SkillCatalog(skills_dir).activate("code-review", "project", VALID_DIGEST)
        assert (receipt.skill_id, receipt.source, receipt.path, receipt.hash, receipt.size, receipt.body) == (
            "code-review",
            "project",
            "code-review/SKILL.md",
            VALID_DIGEST,
            78,
            VALID_BODY,
        )
