"""SkillCatalog (CORE-CONTRACTS §6.3, M6-skills P2-S1): skills discovery + ``list()``.

§6.3 is the planning contract this module implements the discovery half of:
``list()`` returns the currently discovered skill entries as a per-call
snapshot and *observes only* -- it never triggers activation, never reads a
skill body, and produces no tool or permission side effect. ``activate``
(the size->path->trust->hash check order, the audit receipt, the
instructions snapshot write, durable activation, recovery semantics) is the
P2-S2 slice and is deliberately absent here.

Discovery shape (seat discretion, locked by the JSON fixtures under
``tests/contract/skills_fixtures/`` -- no TS blueprint is claimed or
implied):

* root: the skills directory the catalog is constructed with; the
  conventional path face comes from
  :meth:`opencode.config.loader.ConfigLoader.skills_directory`.
* manifest: exactly one ``skills.json`` inside that directory -- a JSON
  object whose single key ``skills`` holds a list of entry objects.
* entry: exactly the six §6.3 SkillEntry fields -- ``id``, ``name``,
  ``path``, ``size``, ``hash``, ``source`` -- all required, no extras. The
  entry maps onto :class:`SkillEntry` field by field, carried verbatim
  (``path`` is the declared string; resolution and the path check belong to
  activation, §6.3 "激活前须通过路径检查").

Failure is explicit, never repaired (p2-slices §2.1 L96): a missing
directory, a missing manifest, corrupt JSON, a wrong manifest shape, or any
entry violation raises the one named :class:`SkillDiscoveryError` --
nothing is skipped, defaulted, or guessed. ``list()`` re-reads the manifest
on every call, so the returned list is a point-in-time snapshot of the
current discovery state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The manifest file name inside the skills directory (conventional form,
#: seat discretion; the wire shape is locked by the skills fixtures).
SKILLS_MANIFEST_NAME = "skills.json"

#: §6.3 SkillEntry.hash: the §6.2 digest output form -- exactly 64 lowercase
#: hex characters, no ``sha256:`` prefix.
_SKILL_HASH = re.compile(r"[0-9a-f]{64}")

#: The closed field set of one manifest entry (the §6.3 six-field table).
_ENTRY_KEYS: frozenset[str] = frozenset({"id", "name", "path", "size", "hash", "source"})


class SkillDiscoveryError(Exception):
    """Skills discovery is broken (§6.3): missing root/manifest, corrupt JSON, or an entry violation."""


class SkillEntry(BaseModel):
    """One discovered skill entry -- the §6.3 six-field table, field for field.

    A strict carrier with no defaults and no extra fields: the manifest
    validation in :class:`SkillCatalog` is the single discovery gate, and the
    model restates the same constraints (strict types, so the §1 bool rule
    holds and no string ever coerces into ``size``) so a directly constructed
    entry cannot bypass them either.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    hash: str = Field(min_length=1)
    source: str = Field(min_length=1)

    @field_validator("hash")
    @classmethod
    def _hash_digest_form(cls, value: str) -> str:
        """§6.3 (digest form along §6.2): exactly 64 lowercase hex characters."""
        if not _SKILL_HASH.fullmatch(value):
            raise ValueError("hash must be exactly 64 lowercase hex characters (CORE-CONTRACTS §6.2 digest form)")
        return value


def _validate_entry(manifest: Path, index: int, raw: Any, seen_ids: set[str]) -> SkillEntry:
    """Validate one manifest entry (the §6.3 six-field table) into a SkillEntry.

    The single discovery gate: an unknown or missing field, an empty string,
    a negative or non-integer size, a hash outside the §6.2 digest form, or a
    duplicate id each fails with the manifest path and the entry index in the
    message -- nothing is skipped or repaired (p2-slices §2.1 L96).
    """
    where = f"skills entry [{index}]"
    if not isinstance(raw, dict):
        raise SkillDiscoveryError(f"{where} in {manifest} must be a JSON object")
    entry: dict[str, Any] = raw
    for key in entry:
        if key not in _ENTRY_KEYS:
            raise SkillDiscoveryError(
                f"unknown field {key!r} in {where} of {manifest} "
                "(CORE-CONTRACTS §6.3: exactly the six SkillEntry fields)"
            )
    missing = sorted(_ENTRY_KEYS - entry.keys())
    if missing:
        raise SkillDiscoveryError(f"{where} in {manifest} is missing required field(s): {', '.join(missing)}")
    for field in ("id", "name", "path", "source"):
        value = entry[field]
        if not isinstance(value, str) or not value:
            raise SkillDiscoveryError(f"{where}.{field} in {manifest} must be a non-empty string")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise SkillDiscoveryError(f"{where}.size in {manifest} must be an integer, got {size!r}")
    if size < 0:
        raise SkillDiscoveryError(f"{where}.size in {manifest} must be non-negative, got {size}")
    digest = entry["hash"]
    if not isinstance(digest, str) or not _SKILL_HASH.fullmatch(digest):
        raise SkillDiscoveryError(
            f"{where}.hash in {manifest} must be exactly 64 lowercase hex characters (CORE-CONTRACTS §6.2 digest form)"
        )
    entry_id = entry["id"]
    if entry_id in seen_ids:
        raise SkillDiscoveryError(
            f"duplicate skill id {entry_id!r} in {manifest} (CORE-CONTRACTS §6.3: unique within one manifest)"
        )
    seen_ids.add(entry_id)
    try:
        return SkillEntry(
            id=entry_id,
            name=entry["name"],
            path=entry["path"],
            size=size,
            hash=digest,
            source=entry["source"],
        )
    except ValidationError as error:
        raise SkillDiscoveryError(f"{where} in {manifest} is not a legal skill entry: {error}") from error


class SkillCatalog:
    """The §6.3 discovery face (P2-S1): ``list()`` observes, never executes."""

    def __init__(self, skills_dir: str | Path) -> None:
        self._skills_dir = Path(skills_dir)

    def list(self) -> list[SkillEntry]:
        """Return the currently discovered entries (§6.3): a per-call snapshot.

        Observation only: the catalog reads exactly one file -- the manifest
        inside its skills directory -- and never a skill body. No activation
        can happen (``activate`` is the P2-S2 face), no body is read, and no
        tool or permission side effect is produced: the skills module wires
        into nothing outside this file. Each call re-reads the manifest, so
        the returned list is a point-in-time snapshot of the current
        discovery state; any discovery failure raises the one named
        :class:`SkillDiscoveryError` (explicit, never repaired).
        """
        if not self._skills_dir.is_dir():
            raise SkillDiscoveryError(
                f"skills directory {self._skills_dir} does not exist (CORE-CONTRACTS §6.3 skills discovery)"
            )
        manifest = self._skills_dir / SKILLS_MANIFEST_NAME
        if not manifest.is_file():
            raise SkillDiscoveryError(
                f"skills manifest {manifest} does not exist (CORE-CONTRACTS §6.3 skills discovery)"
            )
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError as error:
            raise SkillDiscoveryError(f"cannot read skills manifest {manifest}: {error}") from error
        except UnicodeDecodeError as error:
            raise SkillDiscoveryError(f"skills manifest {manifest} is not valid UTF-8: {error}") from error
        try:
            parsed = json.loads(text)
        except ValueError as error:
            raise SkillDiscoveryError(f"skills manifest {manifest} is not valid JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise SkillDiscoveryError(
                f"skills manifest {manifest} must contain a JSON object, got {type(parsed).__name__}"
            )
        document: dict[str, Any] = parsed
        for key in document:
            if key != "skills":
                raise SkillDiscoveryError(
                    f"unknown skills manifest field {key!r} in {manifest} "
                    "(CORE-CONTRACTS §6.3: the manifest carries exactly the 'skills' list)"
                )
        if "skills" not in document:
            raise SkillDiscoveryError(f"skills manifest {manifest} is missing the required 'skills' list")
        raw_entries = document["skills"]
        if not isinstance(raw_entries, list):
            raise SkillDiscoveryError(f"'skills' in {manifest} must be a list of entry objects")
        entries: list[SkillEntry] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_entries):
            entries.append(_validate_entry(manifest, index, raw, seen_ids))
        return entries
