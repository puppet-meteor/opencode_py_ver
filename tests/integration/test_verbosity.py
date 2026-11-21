"""T-C3-013 integration tests: the default-low text verbosity faces.

Upstream ships a session plugin (core/plugin/verbosity.ts, 58 lines) that
fills ``options.textVerbosity = "low"`` on the context/compaction/generate/
title hooks whenever the resolved model id passes ``supportsVerbosity`` and
nothing else already set it. The py port (doc/plans/port-232-increments.md
T-C3-013) folds that into three faces, pinned here:

* the ``GenerationRequest`` contract field (three-value enum, None default);
* the ``default_text_verbosity`` gate (verbosity.ts L45-50 verbatim, with
  the ``toLowerCase()`` normalization from openAIModelID L53 folded in);
* the two request-assembly injections (``ContextBuilder.build`` and the
  compaction summarizer -- py has no plugin host, the T-C3-011 cut).

The Chat Completions top-level ``verbosity`` wire key is pinned in
``tests/contract/test_ai_adapter.py`` (the temperature precedent).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opencode.ai.verbosity import default_text_verbosity
from opencode.contracts.llm import GenerationRequest, ModelRef
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.llm_summarizer import ProviderSummarizer
from tests.contract.test_llm_summarizer import _Chunks, _covered, _ScriptedProvider

# verbosity.ts L46-49, one id per boundary shape the two regexes encode.
SUPPORTED_IDS = [
    "gpt-5",  # the bare form (second regex, end boundary)
    "gpt-5-mini",  # -mini at the end
    "gpt-5-nano",  # -nano at the end
    "gpt-5.6",  # minor 1-6 at the end (first regex)
    "gpt-5.5",
    "gpt-5.6-luna-fast",  # minor followed by '-'
    "gpt-5.2-codex",
    "gpt-5-mini-fast",  # -mini followed by '-'
    "gpt-6",  # 'gpt-6' anywhere (L46)
    "gpt-6-astra-pro",
    "openai/gpt-5.5",  # the '/' left boundary (a gateway-resolved shape)
]
REJECTED_IDS = [
    "gpt-4o",
    "gpt-7",
    "gpt-5.7",  # unknown minor stays unset (the L48 comment)
    "gpt-5.5-chat",  # the specialized chat face (L47) outranks the regex
    "gpt-5.4-image-2",  # the specialized image face (L47)
    "claude-5-sonnet",
    "gpt-50",  # no boundary after gpt-5 -- 0 is not [.:-] nor the end
    "gpt-5-max",  # -max is not -mini/-nano
    "xgpt-5.5",  # no left boundary (^, '/' or '.')
]


def _request(model_id: str = "glm-4.6", text_verbosity: str | None = None) -> GenerationRequest:
    return GenerationRequest(
        request_id="req_verbosity",
        model=ModelRef(provider="maas", model=model_id),
        messages=[],
        tools=[],
        max_output_tokens=64,
        timeout_s=30.0,
        text_verbosity=text_verbosity,  # type: ignore[arg-type]
    )


def _settings_for(model_id: str) -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model=model_id),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


# ---------------------------------------------------------------------------
# The contract face (the T-C1-084 response_format field-growth precedent)
# ---------------------------------------------------------------------------


def test_contract_accepts_the_three_values() -> None:
    for value in ("low", "medium", "high"):
        assert _request(text_verbosity=value).text_verbosity == value


def test_contract_defaults_to_none() -> None:
    assert _request().text_verbosity is None


def test_contract_rejects_non_enum_values() -> None:
    with pytest.raises(ValidationError):
        _request(text_verbosity="chatty")


# ---------------------------------------------------------------------------
# The gate matrix (supportsVerbosity verbatim)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", SUPPORTED_IDS)
def test_gate_defaults_supported_ids_to_low(model_id: str) -> None:
    assert default_text_verbosity(model_id) == "low"


@pytest.mark.parametrize("model_id", REJECTED_IDS)
def test_gate_leaves_rejected_ids_unset(model_id: str) -> None:
    assert default_text_verbosity(model_id) is None


def test_gate_normalizes_case_like_upstream() -> None:
    """openAIModelID lowercases before the gate (verbosity.ts L53)."""
    assert default_text_verbosity("GPT-5.5") == "low"
    assert default_text_verbosity("Gpt-6-Astra-Pro") == "low"


# ---------------------------------------------------------------------------
# The two assembly-point injections
# ---------------------------------------------------------------------------


def test_builder_injects_low_for_supported_model() -> None:
    request = ContextBuilder(settings=_settings_for("gpt-5.5")).build(request_id="req_verbose", history=())
    assert request.text_verbosity == "low"


def test_builder_leaves_other_models_unset() -> None:
    request = ContextBuilder(settings=_settings_for("glm-4.6")).build(request_id="req_plain", history=())
    assert request.text_verbosity is None


async def test_summarizer_injects_low_for_supported_model() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider(
        [[chunks.text("p1", "Summary body."), chunks.usage(input_tokens=1, output_tokens=1), chunks.finish()]]
    )

    await ProviderSummarizer(provider=provider, settings=_settings_for("gpt-5.5")).summarize(_covered(3))

    (request,) = provider.requests
    assert request.text_verbosity == "low"


async def test_summarizer_leaves_other_models_unset() -> None:
    chunks = _Chunks()
    provider = _ScriptedProvider(
        [[chunks.text("p1", "Summary body."), chunks.usage(input_tokens=1, output_tokens=1), chunks.finish()]]
    )

    await ProviderSummarizer(provider=provider, settings=_settings_for("glm-4.6")).summarize(_covered(3))

    (request,) = provider.requests
    assert request.text_verbosity is None
