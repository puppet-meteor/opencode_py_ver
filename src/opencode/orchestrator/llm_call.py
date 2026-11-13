"""The real ``llm_call`` node body over the ported MAAS provider (P4-S1, T-C1-082).

The S2 engine left ``llm_call`` an injection seam (``LlmCallFn``) that only
tests ever filled; this module is the production body. It composes three
decisions the dispatcher pinned (PHASE3 §11 S4 wiring):

* **Host-only discipline** (the ``HOST_ONLY_KEYS`` philosophy): base_url /
  api_key / the resolved model id live in the *composition* (the CLI / server
  assembly passes the already-built provider here) and never enter the
  config snapshot, an event payload, or the graph state. The node's result
  carries the spec's *tier* name (spec data, the S2 fake's pinned shape),
  never the resolved model id behind it.
* **Tier resolution**: ``node.model`` is a tier *name*, not a model id. With
  a tier table the name resolves through it (an unknown tier is a structured
  ``ProviderUnavailable`` node failure); without one -- the production
  composition today -- every tier falls back to the default model, the same
  ``ModelRef`` the CLI's ``--model`` resolved to. A config-ized multi-model
  tier table is an explicit later ticket; no config key was added here.
* **Prompt rendering**: ``node.prompt_template`` renders ``str.format``
  style against the job's submitted params. String params insert verbatim;
  every other JSON value inserts as canonical JSON (deterministic, no Python
  ``repr`` artifacts); a missing key is a structured ``ValidationError``
  node failure, never a bare ``KeyError`` traceback.

The provider seam is the R2 contract face (``Provider.stream``:
``GenerationRequest`` in, ``ProviderChunk`` stream out). The stream is
aggregated to its full text (the adapter already reassembles SSE deltas into
logical chunks); the terminal ``error`` chunk maps onto the node failure
with its own CORE-CONTRACTS §8 code (a scripted HTTP 500 therefore surfaces
as ``ProviderUnavailable``), and a stream that ends without any terminal
chunk fails closed as ``ProviderStreamInterrupted``.

This module also carries the production assembly's two remaining faces: the
honest fail-closed ``tool`` node body (the real ``run_command``-backed
implementation is a later slice -- refusing explicitly beats a silent
no-op) and the advisory-cursor path derivation (the checkpointer lives in
its own sqlite file derived from the main DB path -- the M-7/§14
dual-database boundary, never a table in the main DB).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from opencode.ai.maas import MaasAdapter
from opencode.ai.provider import Provider
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.events import canonical_json
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMMessage,
    LLMTextPart,
    ModelRef,
    TextDeltaChunk,
)
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineState
from opencode.orchestrator.workers import WorkerError

#: One pipeline node completion's output budget. The chat face sizes
#: ``max_output_tokens`` from the session's context math; a pipeline's
#: summarize/critique node has no session history, so this is a plain
#: fixed budget until a tier table carries per-tier values.
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: One pipeline node completion's total request budget (the adapter derives
#: its header/chunk/total deadline ladder from it, provider.md §3).
DEFAULT_TIMEOUT_S = 120.0

#: The honest ``tool`` node refusal message (the production assembly wires
#: :func:`unsupported_tool_body` until the real tool body lands).
TOOL_BODY_UNSUPPORTED_MESSAGE = (
    "the real 'tool' node body is not wired in this composition: node '{node_id}' refuses to execute "
    "(the run_command-backed tool body lands with a later slice; this is the honest fail-closed face, "
    "never a silent no-op)"
)


def checkpointer_path_for(db_path: str | Path) -> Path:
    """The advisory-cursor sqlite path derived from the main DB path (M-7).

    The engine's checkpointer file is a sibling of the main database
    (``app.db`` -> ``app-checkpointer.sqlite3``): an independent file, never
    a table inside the main DB, so the dual-database boundary (PHASE3 §14 /
    M-7) holds by construction -- the main DB's alembic/census surface never
    learns about it and the engine's cursor store never touches job rows.
    """
    resolved = Path(db_path)
    return resolved.with_name(f"{resolved.stem}-checkpointer.sqlite3")


def render_prompt(template: str, params: JsonObject) -> str:
    """Render ``prompt_template`` ``str.format`` style against the job params.

    ``{key}`` inserts the submitted param: strings verbatim, every other
    JSON value as canonical JSON (``3``, ``true``, ``null``, compact
    objects) so the rendering is deterministic. ``{{``/``}}`` keep their
    ``str.format`` escape meaning; format specs and conversions operate on
    the inserted string. A template referencing a key the job did not
    submit, or one the format machinery cannot render, is a structured
    ``ValidationError`` (CORE-CONTRACTS §8 face) -- the job fails with a
    message naming the key, never a bare ``KeyError`` traceback.
    """
    scalars: dict[str, str] = {
        key: value if isinstance(value, str) else canonical_json(value) for key, value in params.items()
    }
    try:
        return template.format(**scalars)
    except KeyError as error:
        key = str(error.args[0]) if error.args else ""
        raise WorkerError(
            f"prompt template references missing param key '{key}'",
            code=CoreErrorCode.VALIDATION_ERROR,
        ) from error
    except (IndexError, AttributeError) as error:
        raise WorkerError(
            f"prompt template uses positional, attribute or index access this face does not support: {error}",
            code=CoreErrorCode.VALIDATION_ERROR,
        ) from error
    except ValueError as error:
        raise WorkerError(
            f"prompt template is not a valid format template: {error}",
            code=CoreErrorCode.VALIDATION_ERROR,
        ) from error


async def unsupported_tool_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The honest fail-closed ``tool`` node body (a ``ToolFn`` face).

    The registration gate still accepts ``tool`` nodes (the closed set's
    S2 face), but the real ``run_command``-backed body is a later slice --
    so the production assembly wires this body instead of a fake: a tool
    node that actually executes fails the job with the explicit
    ``UnsupportedCapability`` §8 face naming the node, never a silent
    no-op and never a made-up result.
    """
    raise WorkerError(
        TOOL_BODY_UNSUPPORTED_MESSAGE.format(node_id=node.node_id),
        code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
    )


class MaasLlmCall:
    """The real ``llm_call`` node body: tier-resolved provider completion.

    Construction takes either an already-assembled provider (the CLI/server
    composition's own ``MaasAdapter`` -- the App then owns its lifecycle) or
    a ``(base_url, api_key)`` pair (this body builds and owns its own
    adapter; :meth:`close` releases it). ``default_model`` is the fallback
    model every tier resolves to when no tier table is given (the production
    composition: the same ``ModelRef`` the CLI's ``--model`` produced); with
    a ``tiers`` table, tier names resolve through it and an unknown tier is
    a structured ``ProviderUnavailable`` node failure.
    """

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: ModelRef,
        tiers: Mapping[str, str] | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        temperature: float | None = None,
    ) -> None:
        owned: MaasAdapter | None = None
        if provider is not None:
            if base_url is not None or api_key is not None:
                raise ValueError("pass either an assembled provider or (base_url, api_key), not both")
            resolved: Provider = provider
        elif base_url is not None and api_key is not None:
            owned = MaasAdapter(base_url, api_key)
            resolved = owned
        else:
            raise ValueError("either an assembled provider or both base_url and api_key are required")
        self._provider = resolved
        self._owned = owned
        self._default_model = default_model
        self._tiers: Mapping[str, str] | None = None if tiers is None else dict(tiers)
        self._max_output_tokens = max_output_tokens
        self._timeout_s = timeout_s
        self._temperature = temperature

    async def close(self) -> None:
        """Close the underlying provider, but only when this body owns it.

        The composition form (``provider=``) shares the caller's adapter --
        the App's §7 L337 close order already releases it -- so this close is
        a no-op there; only the ``(base_url, api_key)`` form owns a client.
        """
        if self._owned is not None:
            await self._owned.close()

    def resolve_model(self, tier: str) -> ModelRef:
        """Tier name -> ``ModelRef`` (table hit / default fallback / fail closed).

        Without a tier table every tier falls back to ``default_model`` (the
        CLI ``--model`` source). With a table, the tier must be in it: an
        unknown tier is a structured ``ProviderUnavailable`` failure -- the
        engine's §8 mapping then surfaces exactly that face.
        """
        if self._tiers is None:
            return self._default_model
        model_id = self._tiers.get(tier)
        if model_id is None:
            raise WorkerError(
                f"llm_call node names model tier '{tier}' which the configured tier table does not define",
                code=CoreErrorCode.PROVIDER_UNAVAILABLE,
            )
        return ModelRef(provider=self._default_model.provider, model=model_id)

    async def __call__(self, node: PipelineNode, state: EngineState) -> JsonValue:
        """The ``LlmCallFn`` face: render the prompt, resolve the tier, aggregate the stream.

        The result value is the S2-pinned shape (a JSON object in
        ``node_results``): the aggregated completion text, the node's tier
        name (spec data -- the resolved model id stays host-only) and the
        logical finish reason. The single request is a no-tools, one-user-
        message generation over the provider seam; the adapter's terminal
        ``error`` chunk becomes the node failure with its own §8 code, and a
        stream without any terminal chunk fails closed.
        """
        assert node.model is not None  # the semantic gate proved the tier present
        assert node.prompt_template is not None  # ... and the template
        params = state.get("params") or {}
        prompt = render_prompt(node.prompt_template, params)
        request = GenerationRequest(
            request_id=f"llm_{uuid4().hex}",
            model=self.resolve_model(node.model),
            messages=[LLMMessage(role="user", content=[LLMTextPart(type="text", text=prompt)])],
            tools=[],
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            timeout_s=self._timeout_s,
        )
        text_parts: list[str] = []
        finish_reason: FinishReason | None = None
        terminal_error: CoreError | None = None
        async for chunk in self._provider.stream(request):
            if isinstance(chunk, TextDeltaChunk):
                text_parts.append(chunk.delta)
            elif isinstance(chunk, ErrorChunk):
                # Contractually terminal (§2.3 L98/L102): stop reading; the
                # first terminal error is the failure face, whatever a
                # misbehaving provider sends after it is ignored.
                terminal_error = chunk.error
                break
            elif isinstance(chunk, FinishChunk) and finish_reason is None:
                finish_reason = chunk.reason
        if terminal_error is not None:
            raise WorkerError(terminal_error.message, code=terminal_error.code)
        if finish_reason is None:
            # A provider face that ended its stream without finish or error
            # violated §2.3 L102: nothing about the completion is provable.
            raise WorkerError(
                "the provider stream ended without a terminal chunk",
                code=CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
            )
        if finish_reason == FinishReason.TOOL_CALLS:
            # The request carried no tools; a tool-call answer is the provider
            # breaking the request shape, and silently returning its (empty)
            # text would hide it.
            raise WorkerError(
                "the provider answered tool calls for a no-tools pipeline llm_call request",
                code=CoreErrorCode.VALIDATION_ERROR,
            )
        return {"text": "".join(text_parts), "model": node.model, "finish_reason": finish_reason.value}
