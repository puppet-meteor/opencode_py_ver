"""Generate the R5 unified JSON Schema artifacts (CORE-CONTRACTS §1 L17; r5-slices §2.4).

Explicit tool (NOT part of pytest or CI): regenerates ``doc/reference/schema/``
from the six frozen ``opencode.contracts`` wire modules by calling
``model_json_schema()`` on every pydantic model defined in them (read-only
consumption -- the contracts source is never modified), plus one
registry-parametrized event-envelope file derived from the public definition
tuples the default registry is built from.

Determinism is a hard requirement (the lock-diff tests byte-compare the
committed artifacts against a fresh in-process regeneration): serialization pins
``json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False,
allow_nan=False)`` plus a trailing newline -- the ``tools/r0/dump_fixtures.py``
precedent extended with key sorting, so the output never depends on dict
insertion order. No timestamps, no environment data, no network; artifacts are
written with LF newlines regardless of platform.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from opencode.contracts import cards, errors, events, llm, messages, pipelines, tools
from opencode.contracts.events import (
    COMPACTION_EVENT_DEFINITIONS,
    JOB_EVENT_DEFINITIONS,
    R1_EVENT_DEFINITIONS,
    R2_EVENT_DEFINITIONS,
    R3_CONFIG_EVENT_DEFINITIONS,
    R3_PERMISSION_EVENT_DEFINITIONS,
    R4_CONFIG_EVENT_DEFINITIONS,
    SKILL_EVENT_DEFINITIONS,
    DurableEvent,
    EphemeralEvent,
    EventDefinition,
    EventDraft,
    versioned_type,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "doc" / "reference" / "schema"
SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

#: The wire modules whose pydantic models make up the unified schema face
#: (P3-S1 adds ``opencode.contracts.cards``, the job-card rendering contract;
#: P3-S2 adds ``opencode.contracts.pipelines``, the pipeline-spec wire contract).
MODULES: tuple[ModuleType, ...] = (errors, events, llm, messages, tools, cards, pipelines)

#: The public definition tuples ``default_event_registry()`` composes
#: (events.py, re-anchored per family addition). Importing the tuples --
#: not the registry's private dicts -- keeps the generator on public surface
#: only. The sixth tuple (SKILL_EVENT_DEFINITIONS, py.skill.activated) was
#: added here by T-C1-052a, fulfilling the T-C1-045 disclosure; the seventh
#: (COMPACTION_EVENT_DEFINITIONS, py.context.compacted) by T-C1-060; the
#: eighth (JOB_EVENT_DEFINITIONS, the seven py.job.* types) by T-C1-076;
#: the registry still exposes no enumeration API.
ENVELOPE_DEFINITIONS: tuple[EventDefinition, ...] = (
    *R1_EVENT_DEFINITIONS,
    *R2_EVENT_DEFINITIONS,
    *R3_PERMISSION_EVENT_DEFINITIONS,
    *R3_CONFIG_EVENT_DEFINITIONS,
    *R4_CONFIG_EVENT_DEFINITIONS,
    *SKILL_EVENT_DEFINITIONS,
    *COMPACTION_EVENT_DEFINITIONS,
    *JOB_EVENT_DEFINITIONS,
)


def _module_models(module: ModuleType) -> dict[str, type[BaseModel]]:
    """Every pydantic model *defined in* the module (re-exports excluded)."""
    models: dict[str, type[BaseModel]] = {}
    for name, obj in vars(module).items():
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
            models[name] = obj
    return models


def _serialize(document: dict[str, Any]) -> str:
    """The one canonical serialization for every artifact (drift-test contract)."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _parametrize(envelope_model: Any, data_model: type[BaseModel]) -> Any:
    """Same Any-routing as events._parametrize: mypy cannot express a ``type[X]``
    value as a generic subscript argument, so the base class goes through ``Any``."""
    return envelope_model[data_model]


def _module_document(module: ModuleType) -> tuple[str, str]:
    """One self-contained artifact per contracts module (granularity ruling, T-C1-045)."""
    stem = module.__name__.rsplit(".", 1)[-1]
    document: dict[str, Any] = {
        "$schema": SCHEMA_URI,
        "title": f"opencode.contracts.{stem}: unified JSON Schema (R5-S4, CORE-CONTRACTS section 1 L17)",
        "module": module.__name__,
        "models": {name: model.model_json_schema() for name, model in _module_models(module).items()},
    }
    return f"{stem}.json", _serialize(document)


def _envelope_document() -> tuple[str, str]:
    """Registry-parametrized envelope schemas, one per registered (type, version)."""
    durable: dict[str, Any] = {}
    ephemeral: dict[str, Any] = {}
    for definition in ENVELOPE_DEFINITIONS:
        if definition.durability == "durable":
            key = versioned_type(definition.event_type, definition.version)
            durable[key] = _parametrize(DurableEvent, definition.data_model).model_json_schema()
        else:
            ephemeral[definition.event_type] = _parametrize(EphemeralEvent, definition.data_model).model_json_schema()
    document: dict[str, Any] = {
        "$schema": SCHEMA_URI,
        "title": "opencode.contracts.events: registry-parametrized event envelope schemas (R5-S4)",
        "durable": durable,
        "ephemeral": ephemeral,
        "generic": {
            "EventDraft": EventDraft.model_json_schema(),
            "DurableEvent": DurableEvent.model_json_schema(),
            "EphemeralEvent": EphemeralEvent.model_json_schema(),
        },
    }
    return "event-envelopes.json", _serialize(document)


def build_artifacts() -> dict[str, str]:
    """The full artifact set: filename -> canonical text (deterministic, in-memory)."""
    artifacts: dict[str, str] = {}
    for module in MODULES:
        filename, text = _module_document(module)
        artifacts[filename] = text
    filename, text = _envelope_document()
    artifacts[filename] = text
    return artifacts


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in build_artifacts().items():
        path = SCHEMA_DIR / filename
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
