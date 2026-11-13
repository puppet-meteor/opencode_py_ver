"""SkillCatalog (CORE-CONTRACTS §6.3, M6-skills P2-S1+P2-S2): discovery + activation.

§6.3 is the planning contract this module implements both halves of.
``list()`` (P2-S1) returns the currently discovered skill entries as a
per-call snapshot and *observes only* -- it never triggers activation,
never reads a skill body, and produces no tool or permission side effect.
``activate()`` (P2-S2) is the auditable activation gate: the fixed
大小→路径→信任→hash check order (any failing segment rejects with a named
:class:`SkillActivationError` -- nothing is repaired, no later segment is
skipped past a failure), the source same-value check (§6.3 L330), and the
§6.3 L332 audit receipt. Committing the ``py.skill.activated`` durable
event, the instructions snapshot write and the re-activation confirmation
gate belong to the §7 host command face
(:meth:`opencode.session.service.SessionService.activate`); this module
stays pure filesystem validation with no side effect.

Discovery shape (seat discretion, locked by the JSON fixtures under
``tests/contract/skills_fixtures/`` -- no TS blueprint is claimed or
implied):

* root: the skills directory the catalog is constructed with; the
  conventional path face comes from
  :meth:`opencode.config.loader.ConfigLoader.skills_directory`.
* manifest: exactly one ``skills.json`` inside that directory -- a JSON
  object whose single key ``skills`` holds a list of entry objects.
* entry: the §6.3 SkillEntry fields -- ``id``, ``name``, ``path``,
  ``size``, ``hash``, ``source``, all six required -- plus the optional
  S5a execution pair (``execution`` ∈ {default, langgraph}; ``pipeline``
  required with ``langgraph``, rejected without it -- CORE-CONTRACTS
  §6.3 manifest extension, T-C1-080); no other extras. The entry maps onto
  :class:`SkillEntry` field by field, carried verbatim (``path`` is the
  declared string; resolution and the path check belong to activation,
  §6.3 "激活前须通过路径检查"). Discovery reads exactly the manifest -- a
  declared ``pipeline`` file is *not* opened here; the graph.json
  registration-time validation and hash lock are the engine's
  ``register`` face (the manifest -> engine glue is the P5-S1
  registration face, :mod:`opencode.orchestrator.registration`).

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
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from opencode.contracts.events import SKILL_BODY_MAX_BYTES, SkillActivatedData

#: The manifest file name inside the skills directory (conventional form,
#: seat discretion; the wire shape is locked by the skills fixtures).
SKILLS_MANIFEST_NAME = "skills.json"

#: §6 L289: the total instruction budget the activated skill body counts
#: into. The per-source cap is the imported :data:`SKILL_BODY_MAX_BYTES`
#: (64 KiB, the same per-AGENTS.md figure) enforced by the activation size
#: segment; the cross-source total is the §6 runtime instruction face,
#: whose consumption seam lands with the system_prompt wiring slice -- the
#: body must be rejected explicitly there, never silently truncated.
SKILL_INSTRUCTION_TOTAL_BUDGET = 256 * 1024

#: §6.3 SkillEntry.hash: the §6.2 digest output form -- exactly 64 lowercase
#: hex characters, no ``sha256:`` prefix.
_SKILL_HASH = re.compile(r"[0-9a-f]{64}")

#: The closed field set of one manifest entry: the §6.3 six-field table plus
#: the optional S5a execution pair (CORE-CONTRACTS §6.3 manifest extension,
#: T-C1-080). ``execution``/``pipeline`` are legal only as the conditional
#: langgraph pair -- the required/optional split and the pair's rules are
#: enforced in :func:`_validate_entry`.
_ENTRY_KEYS: frozenset[str] = frozenset({"id", "name", "path", "size", "hash", "source", "execution", "pipeline"})

#: The six unconditionally required §6.3 fields (the execution pair is
#: optional by CORE-CONTRACTS §6.3; a missing pair member is only a
#: violation under the conditional rules below).
_REQUIRED_ENTRY_KEYS: frozenset[str] = frozenset({"id", "name", "path", "size", "hash", "source"})


class SkillDiscoveryError(Exception):
    """Skills discovery is broken (§6.3): missing root/manifest, corrupt JSON, or an entry violation."""


class _DuplicateManifestKey(ValueError):
    """One repeated JSON object key inside the manifest (票内定夺 b carrier)."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Duplicate JSON object keys are ambiguous; reject instead of last-wins.

    The §6.2 L297 canonical-JSON rule (duplicate object keys are rejected,
    never silently repaired) applied to the manifest parse face -- a
    manifest carrying ``{"skills": [...], "skills": [...]}`` is corrupt,
    and the later value must not silently win (票内定夺 b; the
    ``object_pairs_hook`` precedent is ``contracts/events.py``).
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey(key)
        result[key] = value
    return result


class SkillEntry(BaseModel):
    """One discovered skill entry -- the §6.3 field table, field for field.

    A strict carrier with no extra fields: the six §6.3 fields are required
    (strict types, so the §1 bool rule holds and no string ever coerces into
    ``size``), and the S5a execution pair is optional (``execution`` None =
    the implicit "default" reading; ``pipeline`` legal only on langgraph
    entries). The manifest validation in :class:`SkillCatalog` is the single
    discovery gate, and the model restates the same constraints so a
    directly constructed entry cannot bypass them either.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    hash: str = Field(min_length=1)
    source: str = Field(min_length=1)
    #: The execution declaration (CORE-CONTRACTS §6.3 S5a): None (the
    #: implicit "default") or "langgraph"; any other value is rejected at
    #: discovery, and "langgraph" makes ``pipeline`` required.
    execution: Literal["default", "langgraph"] | None = None
    #: The langgraph pipeline path (skill-directory-relative, declared
    #: verbatim like ``path`` -- never opened by discovery). Legal only with
    #: execution "langgraph"; rejected on a default entry.
    pipeline: str | None = Field(default=None, min_length=1)

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
                "(CORE-CONTRACTS §6.3: exactly the SkillEntry fields -- six required "
                "plus the optional execution/pipeline pair)"
            )
    missing = sorted(_REQUIRED_ENTRY_KEYS - entry.keys())
    if missing:
        raise SkillDiscoveryError(f"{where} in {manifest} is missing required field(s): {', '.join(missing)}")
    for field in ("id", "name", "path", "source"):
        value = entry[field]
        if not isinstance(value, str) or not value:
            raise SkillDiscoveryError(f"{where}.{field} in {manifest} must be a non-empty string")
    # -- the S5a execution pair (CORE-CONTRACTS §6.3 manifest extension):
    # execution ∈ {default, langgraph} when present; pipeline is a
    # non-empty string legal ONLY on langgraph entries, and required there.
    # A pipeline on a default entry (explicit or implicit) is rejected --
    # the pair is conditional, never a free-form extension.
    execution = entry.get("execution")
    if execution is not None and (not isinstance(execution, str) or execution not in ("default", "langgraph")):
        raise SkillDiscoveryError(
            f"{where}.execution in {manifest} must be one of: default, langgraph "
            "(CORE-CONTRACTS §6.3 manifest extension)"
        )
    pipeline = entry.get("pipeline")
    if pipeline is not None and (not isinstance(pipeline, str) or not pipeline):
        raise SkillDiscoveryError(f"{where}.pipeline in {manifest} must be a non-empty string")
    if execution == "langgraph" and pipeline is None:
        raise SkillDiscoveryError(
            f"{where} in {manifest} declares execution 'langgraph' without the required 'pipeline' field "
            "(CORE-CONTRACTS §6.3: a langgraph skill must name its pipeline path)"
        )
    if pipeline is not None and execution != "langgraph":
        raise SkillDiscoveryError(
            f"{where} in {manifest} carries 'pipeline' without execution 'langgraph' "
            "(CORE-CONTRACTS §6.3: the pipeline field is legal only on langgraph skills)"
        )
    # the pair is validated above -- execution is exactly the enum literal or
    # None; ``in``-membership does not narrow, so cast for the frozen field.
    execution = cast(Literal["default", "langgraph"] | None, execution)
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
            execution=execution,
            pipeline=pipeline,
        )
    except ValidationError as error:
        raise SkillDiscoveryError(f"{where} in {manifest} is not a legal skill entry: {error}") from error


class SkillActivationError(Exception):
    """Skill activation is refused (§6.3): the fixed check order rejected the
    body, or the command arguments disagree with the discovered entry."""


@dataclass(frozen=True, slots=True)
class SkillActivationReceipt:
    """The §6.3 L332 audit receipt: the activation record four-tuple + skill_id.

    The five contract fields -- ``skill_id`` plus the source/path/hash/size
    record -- are the audit face (§6.3 L330: the list entry, the activation
    record and the receipt carry one source value). ``body`` rides along as
    the checked bounded body: the receipt is the only in-process carrier
    from the check-order gate (this module) to the §7 host command face
    that commits the durable record -- the body was read and verified here
    exactly once, and re-reading it elsewhere would be an unchecked read
    (§6.3 L345). :meth:`durable_record` constructs the §6.3 L334-343
    six-field event data from it.
    """

    skill_id: str
    source: str
    path: str
    hash: str
    size: int
    body: str

    def durable_record(self) -> SkillActivatedData:
        """The §6.3 L334-343 durable activation record (the ``py.skill.activated`` data)."""
        return SkillActivatedData(
            skill_id=self.skill_id,
            source=self.source,
            path=self.path,
            hash=self.hash,
            size=self.size,
            body=self.body,
        )


class SkillCatalog:
    """The §6.3 face: ``list()`` observes (P2-S1); ``activate()`` audits (P2-S2)."""

    def __init__(self, skills_dir: str | Path) -> None:
        self._skills_dir = Path(skills_dir)

    def list(self) -> list[SkillEntry]:
        """Return the currently discovered entries (§6.3): a per-call snapshot.

        Observation only: the catalog reads exactly one file -- the manifest
        inside its skills directory -- and never a skill body. No activation
        happens here, no body is read, and no tool or permission side
        effect is produced: the skills module wires into nothing outside
        this file. Each call re-reads the manifest, so the returned list is
        a point-in-time snapshot of the current discovery state; any
        discovery failure raises the one named :class:`SkillDiscoveryError`
        (explicit, never repaired).
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
            parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        except _DuplicateManifestKey as error:
            raise SkillDiscoveryError(
                f"skills manifest {manifest} carries a duplicate JSON object key {error.args[0]!r} "
                "(CORE-CONTRACTS §6.2 L297: duplicate keys are ambiguous -- rejected, not last-wins)"
            ) from error
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

    def activate(self, skill_id: str, source: str, expected_hash: str) -> SkillActivationReceipt:
        """Run the §6.3 fixed check order over one discovered skill; return the receipt.

        The auditable activation gate (§6.3 L319/L345): locate the entry by
        ``skill_id`` in the *current* discovery state, require the activate
        ``source`` to equal the entry's (§6.3 L330 三者同值), then run the
        fixed 大小→路径→信任→hash order. Any failing segment rejects with a
        :class:`SkillActivationError` that names its segment -- nothing is
        repaired, and no later segment is skipped past a failure. The body
        is read exactly once (trust segment) and carried on the receipt.

        This face grants nothing (§6.3 L351-353): no §5.2 permission, no
        execution, no declared plugin/MCP load. The durable-record commit,
        the instructions snapshot write and the re-activation hash-change
        confirmation gate belong to the §7 host command face.
        """
        for argument, name in ((skill_id, "skill_id"), (source, "source"), (expected_hash, "expected_hash")):
            if not isinstance(argument, str) or not argument:
                raise SkillActivationError(f"activate {name} must be a non-empty string, got {argument!r}")
        # Locate the entry in the current discovery state (a broken
        # discovery answers with the named SkillDiscoveryError -- no
        # guessing over a corrupt manifest).
        entry = next((item for item in self.list() if item.id == skill_id), None)
        if entry is None:
            raise SkillActivationError(
                f"skill id {skill_id!r} is not in the current discovery manifest "
                "(CORE-CONTRACTS §6.3 L325: activate locates the entry by id)"
            )
        # Identity precondition (票内定夺 c): one source value across the
        # list entry, the activation record and the receipt (§6.3 L330) --
        # placed before the body segments because locating the wrong entry
        # would make all four of them meaningless.
        if entry.source != source:
            raise SkillActivationError(
                f"activate source {source!r} does not equal the discovered entry source {entry.source!r} "
                "(CORE-CONTRACTS §6.3 L330: the list entry, the activation record and the receipt "
                "carry one source value)"
            )
        # -- segment ① 大小 (票内定夺 a+d): the declared size against the
        # bounded per-skill cap, then the same-source stat verification.
        if entry.size > SKILL_BODY_MAX_BYTES:
            raise SkillActivationError(
                f"size check: skill {skill_id!r} declares {entry.size} bytes, over the {SKILL_BODY_MAX_BYTES}-byte "
                "per-skill body cap (CORE-CONTRACTS §6 L289/§6.3 L343: rejected explicitly, never silently truncated)"
            )
        body_path = self._skills_dir / entry.path
        try:
            stat_size = body_path.stat().st_size
        except OSError as error:
            raise SkillActivationError(
                f"size check: cannot stat the declared body {entry.path!r} under {self._skills_dir}: {error}"
            ) from error
        if stat_size != entry.size:
            raise SkillActivationError(
                f"size check: skill {skill_id!r} declares {entry.size} bytes but the body at {entry.path!r} "
                f"holds {stat_size} (CORE-CONTRACTS §6.3 L328: the declared size and the body bytes are one source)"
            )
        # -- segment ② 路径 (§5.3 face): the declared path must resolve
        # inside the discovery root -- traversal, absolute escapes and
        # out-of-root symlinks/junctions are rejected.
        resolved_root = self._skills_dir.resolve()
        resolved = body_path.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise SkillActivationError(
                f"path check: {entry.path!r} resolves outside the skills root {self._skills_dir} "
                "(CORE-CONTRACTS §5.3 L276: traversal and out-of-root symlinks/junctions are rejected)"
            )
        if not resolved.is_file():
            raise SkillActivationError(f"path check: {entry.path!r} does not name a regular file under the skills root")
        # -- segment ③ 信任 (票内定夺 d): the body is read through the
        # path-segment-resolved anchor only -- a read failure, a UTF-8
        # decode failure, a stat-vs-read divergence or an empty body is
        # rejected here; the disk is never consulted past this point.
        try:
            raw = resolved.read_bytes()
        except OSError as error:
            raise SkillActivationError(f"trust check: cannot read the skill body at {entry.path!r}: {error}") from error
        if len(raw) != stat_size:
            raise SkillActivationError(
                f"trust check: the body at {entry.path!r} changed between the size check and the read "
                f"({stat_size} stat bytes vs {len(raw)} read bytes)"
            )
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SkillActivationError(
                f"trust check: the skill body at {entry.path!r} is not valid UTF-8: {error}"
            ) from error
        if not body:
            raise SkillActivationError(
                f"trust check: the skill body at {entry.path!r} is empty "
                "(CORE-CONTRACTS §6.3 L343: the durable record carries a non-empty bounded body)"
            )
        # -- segment ④ hash (§6.3 L345/L329): the body digest against
        # expected_hash, then the discovered entry hash against it.
        digest = sha256(raw).hexdigest()
        if digest != expected_hash:
            raise SkillActivationError(
                f"hash check: the body digest {digest} does not equal expected_hash "
                "(CORE-CONTRACTS §6.3 L345: the hash check is the body digest vs expected_hash)"
            )
        if entry.hash != expected_hash:
            raise SkillActivationError(
                f"hash check: the discovered entry hash {entry.hash} does not equal expected_hash "
                "(CORE-CONTRACTS §6.3 L329)"
            )
        return SkillActivationReceipt(
            skill_id=entry.id,
            source=entry.source,
            path=entry.path,
            hash=digest,
            size=len(raw),
            body=body,
        )
