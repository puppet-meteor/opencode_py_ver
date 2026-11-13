"""Production pipeline registration (P5-S1, T-C1-085): the manifest -> engine glue.

Who calls ``engine.register`` in production: this module, once per host
boot. The skills manifest is the single routing source (PHASE3 §5.1 m-1 /
§13 L240): the real :class:`~opencode.skills.catalog.SkillCatalog` is the
only discovery gate (zero second parsing -- no drift), and every entry
declaring ``execution == "langgraph"`` names a skill-directory-relative
graph.json that is opened, parsed and handed to the real engine's
three-layer registration gate (structure -> semantic -> rehearsal).
Default/None entries are counted and skipped -- their pipeline face is
never opened (the catalog.py pinned discipline).

The report face (host-only; this module writes no stdout -- the CLI's
stdout is the answer stream -- and no stderr either: the hosts own the
disclosure, the report carries everything):

* ``registered``: one row per accepted binding -- the manifest's skill id
  for correlation plus the SPEC's own ``pipeline_id`` and the engine's
  ``spec_hash`` (identity semantics: the engine binds by
  ``spec.pipeline_id``; the manifest entry id is routing metadata and is
  never forced equal -- S5b routing was cut by the user ruling of
  2026-11-13; a repeated ``pipeline_id`` keeps the engine's existing
  new-binding semantics, recorded honestly);
* ``rejected``: one row per failed entry -- skill id, the declared
  pipeline string, the exception class name and the message's first line
  (the single-line disclosure discipline). The rejection face is
  ``OSError`` (the graph read) plus the ``ValueError`` family -- exactly
  the ticket's five classes (``json.JSONDecodeError``, pydantic
  ``ValidationError``, ``PydagImportError``, ``PipelineSpecRejected``)
  plus the read/decode face (``UnicodeDecodeError``) and this module's
  containment rejection (:class:`PipelinePathEscape`) -- and one entry's
  rejection never interrupts the remaining entries;
* ``manifest_error``: a broken manifest (the catalog's one named
  :class:`~opencode.skills.catalog.SkillDiscoveryError`) is reported,
  never raised -- while a missing skills directory or manifest is the
  SILENT fresh-project zero state (an empty report, no error face: the
  disclosure policy reads the absence as "nothing to register", not a
  failure);
* ``non_langgraph``: the count of default/None entries skipped.

Containment is a hard constraint (the §5.3 face, the ``_escaping_resource``
precedent): the declared pipeline path resolves (lexical -> realpath)
against the skills root, and an escape -- traversal, an absolute path, or
an unresolvable path treated conservatively as an escape -- is that
entry's rejection, never a read of anything outside the root.

The loop constraint: ``engine.register`` opens the checkpointer's
aiosqlite connection through ``EngineCheckpointer.saver()``, which binds
to the running loop. This coroutine must therefore be awaited inside the
SAME loop that later drives the engine -- the CLI awaits it at the start
of ``asyncio.run(_drive(...))`` and the HTTP host in the ASGI lifespan
startup; a composition root that wrapped registration in a temporary
``asyncio.run`` and then drove the engine on another loop would hold a
cross-loop connection (red line).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from opencode.contracts.messages import JsonObject
from opencode.skills.catalog import SKILLS_MANIFEST_NAME, SkillCatalog, SkillDiscoveryError


class PipelinePathEscape(ValueError):
    """A declared pipeline path resolves outside the skills root (the §5.3 face).

    Named, not a bare ``ValueError``: the escape is the glue's own
    rejection, discovered before any read, and the report's error rows
    carry class names the host discloses as-is.
    """


@dataclass(frozen=True, slots=True)
class RegisteredPipeline:
    """One accepted binding: the manifest skill id + the spec's identity + the engine hash."""

    skill_id: str
    pipeline_id: str
    spec_hash: str


@dataclass(frozen=True, slots=True)
class RejectedPipeline:
    """One rejected entry: the routing faces plus the failure's class name and first line."""

    skill_id: str
    pipeline: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class RegistrationReport:
    """The registration face's whole outcome (the hosts' disclosure reads only this)."""

    registered: tuple[RegisteredPipeline, ...] = ()
    rejected: tuple[RejectedPipeline, ...] = ()
    manifest_error: str | None = None
    non_langgraph: int = 0


class RegistrationEngine(Protocol):
    """The engine face the glue consumes: the three-layer registration gate.

    Only ``register`` -- the glue never queries ``bound`` (the engine binds
    by its own dict). The structural protocol keeps the face fake-friendly
    (the recording fake in the unit tests); the real
    :class:`~opencode.orchestrator.engine.PipelineEngine` satisfies it.
    The return is the engine's bound pipeline (spec + spec_hash + compiled
    graph), typed ``Any`` because the glue reads exactly two fields off it
    and the compiled-graph type stays engine-side.
    """

    async def register(self, raw: JsonObject) -> Any: ...


def _manifest_present(root: Path) -> bool:
    """The silent-zero-state pre-check: is there anything to discover at all?

    A fresh project carries no skills directory (or no manifest yet) --
    "nothing to register", never an error. The catalog below still owns
    every *broken* discovery face as its one named SkillDiscoveryError.
    """
    return root.is_dir() and (root / SKILLS_MANIFEST_NAME).is_file()


def _load_graph(skills_root: Path, pipeline: str) -> Any:
    """Containment + open + parse for one declared pipeline file (the sync read face).

    The path base is the skill-directory-relative string (PHASE3 §5.1
    m-1); both sides resolve (lexical -> realpath) and an escape --
    traversal, an absolute path, or an unresolvable path treated
    conservatively as an escape -- rejects before any read (the §5.3
    face, the ``_escaping_resource`` precedent). The parse hands back the
    raw JSON value untyped: the engine's structural layer owns what a
    graph.json must be. Raises ``PipelinePathEscape`` (containment),
    ``OSError`` (the read) and the ValueError family (UTF-8/JSON decode)
    -- together with the engine's gate exceptions, exactly the per-entry
    rejection face ``register_pipelines`` records.
    """
    resolved_root = skills_root.resolve()
    try:
        resolved = (skills_root / pipeline).resolve()
    except OSError as error:
        raise PipelinePathEscape(f"pipeline path {pipeline!r} under {skills_root} is unresolvable: {error}") from error
    if not resolved.is_relative_to(resolved_root):
        raise PipelinePathEscape(
            f"pipeline path {pipeline!r} escapes the skills root {skills_root} "
            "(CORE-CONTRACTS §5.3: traversal and out-of-root symlinks/junctions are rejected)"
        )
    return json.loads(resolved.read_text(encoding="utf-8"))


async def register_pipelines(engine: RegistrationEngine, skills_dir: str | Path) -> RegistrationReport:
    """Register every langgraph skill's pipeline into ``engine``; report the whole outcome.

    Pure registration face: host-only, no stdout and no stderr -- the
    callers (the CLI drive loop and the ASGI lifespan startup) disclose
    the report per the pinned policy, each staying on its own loop. Never
    raises past a per-entry rejection: a broken manifest or a failed entry
    lands in the report and the remaining entries continue.
    """
    root = Path(skills_dir)
    if not _manifest_present(root):
        return RegistrationReport()
    try:
        entries = SkillCatalog(root).list()
    except SkillDiscoveryError as error:
        return RegistrationReport(manifest_error=str(error))
    registered: list[RegisteredPipeline] = []
    rejected: list[RejectedPipeline] = []
    non_langgraph = 0
    for entry in entries:
        if entry.execution != "langgraph":
            # The default/None face: counted and skipped -- the entry's
            # pipeline file is never opened (the catalog.py discipline).
            non_langgraph += 1
            continue
        # The S5a pair's conditional rule guarantees a non-None pipeline on a
        # langgraph entry (catalog-side validation); ``!=`` does not narrow,
        # so cast for the declared string (the catalog.py precedent).
        pipeline = cast(str, entry.pipeline)
        try:
            bound = await engine.register(_load_graph(root, pipeline))
            registered.append(
                RegisteredPipeline(
                    skill_id=entry.id,
                    pipeline_id=bound.spec.pipeline_id,
                    spec_hash=bound.spec_hash,
                )
            )
        except (OSError, ValueError) as error:
            # The per-entry rejection face: the read's OSError plus the
            # ValueError family -- PipelinePathEscape / json.JSONDecodeError /
            # UnicodeDecodeError from the sync read face, pydantic
            # ValidationError / PydagImportError / PipelineSpecRejected from
            # the engine's three-layer gate. Recorded by class name with the
            # message's first line; the remaining entries continue.
            message_lines = str(error).splitlines()
            rejected.append(
                RejectedPipeline(
                    skill_id=entry.id,
                    pipeline=pipeline,
                    error_type=type(error).__name__,
                    message=message_lines[0] if message_lines else "",
                )
            )
    return RegistrationReport(
        registered=tuple(registered),
        rejected=tuple(rejected),
        non_langgraph=non_langgraph,
    )
