"""T-C3-013 evaluation set: the default-low text verbosity face.

Upstream ships a session plugin (core/plugin/verbosity.ts, 58 lines) that
injects ``options.textVerbosity = "low"`` on four hooks (context /
compaction / generate / title) whenever the model id passes the
``supportsVerbosity`` gate and nothing else already set it. The py port
(see doc/plans/port-232-increments.md T-C3-013) folds that into three
faces:

* the ``GenerationRequest`` contract grows ``text_verbosity`` (the T-C1-084
  response_format field-growth precedent);
* the maas adapter passes it through as the Chat Completions top-level
  ``verbosity`` key (the openai-chat.ts wire shape -- py speaks Chat
  Completions, maas.py L632);
* a ``default_text_verbosity(model)`` gate function (the supportsVerbosity
  rules verbatim) is applied at the request-assembly points -- py has no
  plugin host (the T-C3-011 cut precedent).

BEFORE the faces land every scenario reads absent; each must reach its
goal AFTER -- anything else is drifting: fix the alignment, never the
reading.
"""

from __future__ import annotations

import importlib
import inspect
import json
from typing import Any, cast

from opencode.contracts.llm import GenerationRequest, ModelRef

# The upstream matrix (verbosity.test.ts): ids that MUST default low, and
# ids that MUST stay unset (gate rejects / unknown minors stay untouched).
SUPPORTED = ["gpt-5.5", "gpt-5.6-luna-fast", "gpt-5.2-codex", "gpt-5-mini-fast", "gpt-6-astra-pro"]
REJECTED = ["gpt-4o", "gpt-7", "gpt-5.7", "gpt-5.5-chat", "gpt-5.4-image-2", "claude-5-sonnet"]

_GATE_CANDIDATES = (
    "opencode.ai.verbosity",
    "opencode.ai.provider",
    "opencode.session.context",
    "opencode.orchestrator.llm_call",
)


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _find_gate() -> Any | None:
    """The gate function's home module (T-C3-011 _find precedent)."""
    for name in _GATE_CANDIDATES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "default_text_verbosity"):
            return module
    return None


def _request(model_id: str = "gpt-5.5", **extra: Any) -> GenerationRequest:
    return GenerationRequest(
        request_id="req_verbosity_probe",
        model=ModelRef(provider="maas", model=model_id),
        messages=[],
        tools=[],
        max_output_tokens=16,
        timeout_s=30.0,
        **extra,
    )


# ---------------------------------------------------------------------------
# P0 -- the capability surface
# ---------------------------------------------------------------------------


def test_p0_capability() -> None:
    metrics: dict[str, Any] = {
        "request_field": False,
        "request_default_none": False,
        "request_rejects_non_enum": False,
        "maas_passthrough_face": False,
        "gate_function": False,
    }
    # The contract face: the field exists, defaults to None, and is typed
    # to the upstream three-value enum.
    try:
        request = _request(text_verbosity="low")
        metrics["request_field"] = getattr(request, "text_verbosity", None) == "low"
    except (TypeError, ValueError):
        metrics["request_field"] = False
    try:
        plain = _request()
        metrics["request_default_none"] = getattr(plain, "text_verbosity", "missing") is None
    except (TypeError, ValueError):
        metrics["request_default_none"] = False
    try:
        _request(text_verbosity="chatty")
        metrics["request_rejects_non_enum"] = False
    except ValueError:
        metrics["request_rejects_non_enum"] = True
    # The transport face: the maas params assembly carries the branch
    # (source-level probe -- _attempt is a generator needing a live client).
    try:
        from opencode.ai.maas import MaasAdapter

        source = inspect.getsource(MaasAdapter._attempt)
        metrics["maas_passthrough_face"] = "verbosity" in source
    except (ImportError, OSError):
        metrics["maas_passthrough_face"] = False
    metrics["gate_function"] = _find_gate() is not None
    status = "probed" if all(metrics.values()) else "absent-partial"
    _report({"scenario": "P0-capability", "status": status, **metrics})
    assert status in ("absent-partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- the gate matrix (supportsVerbosity verbatim)
# ---------------------------------------------------------------------------


def test_m1_gate_matrix() -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    module = _find_gate()
    try:
        if module is None:
            raise KeyError("gate function missing")
        gate = module.default_text_verbosity
        metrics["supported_default_low"] = all(gate(model) == "low" for model in SUPPORTED)
        metrics["rejected_stay_none"] = all(gate(model) is None for model in REJECTED)
        status = "gated" if all(metrics.values()) else "drifting"
    except KeyError:
        pass
    _report({"scenario": "M1-gate-matrix", "status": status, **metrics})
    assert status in ("capability-absent", "gated", "drifting")


# ---------------------------------------------------------------------------
# M2 -- the override-preserved contract face
# ---------------------------------------------------------------------------


def test_m2_override_preserved() -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    module = _find_gate()
    try:
        if module is None:
            raise KeyError("gate function missing")
        # The explicit value survives the contract round trip: the gate
        # only fills the None hole (verbosity.ts L28 -- upstream returns
        # early when options.textVerbosity !== undefined).
        request = _request(model_id="gpt-5.5", text_verbosity="high")
        # cast seam (T-C3-010 precedent): the field exists only AFTER the
        # delta lands; BEFORE, mypy must still clear this file.
        request_any = cast(Any, request)
        metrics["explicit_value_round_trips"] = request_any.text_verbosity == "high"
        gate = module.default_text_verbosity
        resolved = request_any.text_verbosity or gate(request.model.model)
        metrics["explicit_not_overwritten"] = resolved == "high"
        unset = cast(Any, _request(model_id="gpt-5.5"))
        metrics["unset_filled_by_gate"] = (unset.text_verbosity or gate(unset.model.model)) == "low"
        status = "preserved" if all(metrics.values()) else "drifting"
    except (KeyError, ValueError):
        pass
    _report({"scenario": "M2-override-preserved", "status": status, **metrics})
    assert status in ("capability-absent", "preserved", "drifting")


# ---------------------------------------------------------------------------
# M3 -- the wire face (Chat Completions top-level verbosity)
# ---------------------------------------------------------------------------


def test_m3_wire_face() -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        from opencode.ai.maas import MaasAdapter

        source = inspect.getsource(MaasAdapter._attempt)
        if '"verbosity"' not in source and "'verbosity'" not in source:
            raise KeyError("wire key missing")
        metrics["top_level_key"] = 'params["verbosity"]' in source.replace("'", '"')
        metrics["gated_on_not_none"] = "text_verbosity is not None" in source
        status = "wired" if all(metrics.values()) else "drifting"
    except (ImportError, OSError, KeyError):
        pass
    _report({"scenario": "M3-wire-face", "status": status, **metrics})
    assert status in ("capability-absent", "wired", "drifting")
