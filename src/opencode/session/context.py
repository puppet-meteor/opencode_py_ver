"""Session context builder (R2-S4, CORE-CONTRACTS §2.2 L78-80 / §7 L339).

:class:`ContextBuilder` is the only producer of
:class:`~opencode.contracts.llm.GenerationRequest` (the S1 face pins it): it
turns projected session history plus injected runtime settings into the
request. §2.2 L80 red line -- history arrives as contract-internal messages
(the §2.1 union aliased below) and tools as ``ToolSpec``; the output is
contract types. ``SessionMessage``-shaped storage rows, openai/SDK types or
provider wire formats never cross this boundary (the MAAS adapter owns the
OpenAI translation).

TS behaviour mapping (fingerprints in ``doc/source-baseline.json``):

- ``packages/core/src/session/model-request.ts``: ``outputLimit`` L89 (the
  output-budget derivation counterpart), ``baseTranscript`` L99 (history ->
  request assembly incl. the separate system face), ``unsupportedParts`` L183
  (only provider-supported content is sent -- the Python content union has no
  media face, so every part the assembly emits is supported by construction);
- ``packages/core/src/session/context.ts``: ``Selection`` L28 / ``Loaded``
  L35 (context selection/loading -- narrowed here to injected settings plus
  the in-memory history sequence; this module touches no DB or registry);
- ``packages/core/src/session/runner/to-llm-message.ts``: ``toLLMMessages``
  L318 (role/content conversion): ``idle`` contributes nothing (L243-244),
  empty user text yields no message, empty text/reasoning parts are dropped,
  and each tool result rides its own tool-role message after the assistant
  message that issued the calls. Two TS branches in that anchor are
  deliberately not ported (precision note, T-C1-020 n-1): L206 keeps an
  *empty* reasoning part when it carries ``providerMetadata`` -- the Python
  §2.1 ``AssistantReasoningPart`` has no metadata field, so that case is
  structurally unreachable here; and the L173-185 reasoning->text demotion
  family (error/interrupt/model change) has no Python counterpart either --
  replay reasoning never reaches the wire (the MAAS adapter drops it,
  maas.py replay note / R2-S3-review ③-9), so demoting it for display is
  the TUI layer's concern, not this request face.

Runtime settings are the OP-1 injection face (r2-slices §4): model, context
limit, output reserve, safety margin, temperature and timeout are
constructor-injected; ``ConfigSnapshot`` v1 is never read (it is a storage
shape, §6.1) and R3's ConfigLoader replaces this single injection point. The
model context limit is host-configured -- §7 L339: unknown means the host must
configure it, so ``RuntimeSettings.context_limit`` is a required field with no
default value to guess from. The optional system prompt rides the same
injection face (TS ``baseTranscript`` builds the system face separately from
history; Python R2 has no agent/instructions source yet -- R3 replaces this
point too); ``None``/empty produces no system message.

Token budget (§7 L339): ``available = context_limit - output_reserve -
safety_margin``; the assembled request (messages plus catalog) is estimated
with the deterministic ~4 chars/token heuristic (TS model-request.ts L51
comment; the injected safety margin absorbs the estimator's error, TS's
ESTIMATE_ERROR/WINDOW_HEADROOM counterpart folded into the one §7 L339 knob)
and must fit inside ``available``; ``max_output_tokens`` is exactly
``output_reserve`` -- never shrunk to fit a larger history. Overflow raises
:class:`ContextLimitExceeded`: history is preserved for a new session; Phase 1
never silently trims and never summarizes.

**Reasoning counts against the output budget** (§8.1 L107 hard input, r2-slices
§3 difference table #9): the measured endpoint spent a whole
``max_output_tokens=64`` on ``reasoning=61`` -- zero body text, finish=length.
Generated reasoning therefore rides *inside* the output reserve: the input
side may never borrow from it (the formula above), so a tight history cannot
squeeze reasoning out of the budget, and hosts must size ``output_reserve``
to cover reasoning plus visible text with the safety margin as the explicit
slack. TS records no such endpoint behaviour; this is a Python-side hard
input.

Catalog snapshot (§5.1 L253): every provider request carries a fixed catalog.
``build()`` consumes the injected ``ToolSpec`` sequence as that snapshot --
immutable consumption, no registry re-query; R2-S6 passes
``registry.snapshot()`` here once S5 lands. Specs are deep-copied into the
request so later mutation of the caller's sequence cannot reach an already
built request.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, isfinite
from typing import Any

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.llm import (
    GenerationRequest,
    LLMMessage,
    LLMPart,
    LLMReasoningPart,
    LLMTextPart,
    LLMToolCallPart,
    LLMToolResultPart,
    ModelRef,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolTextContent,
    UserMessage,
)
from opencode.contracts.tools import ToolSpec
from opencode.storage import StoreError, StoreValidationError

#: §2.1 history union (S1 face) as the builder consumes it: user / assistant /
#: idle. This is an alias over the S1 models, not a new wire model.
type SessionMessage = UserMessage | AssistantMessage | IdleMessage

#: Deterministic input estimate: ~4 characters per token (TS model-request.ts
#: L51: "Prompt text is estimated at about 4 characters per token, which can
#: run low on dense text such as code"). The injected safety margin absorbs
#: the estimator's error.
#: Host guidance (m-3, R2-S4-review §2): chars/4 is calibrated for
#: Latin-script text; CJK-dominant history underestimates the real token
#: count by roughly 2-4x (actual ~1-2 chars/token), so CJK-session hosts
#: must size ``safety_margin`` accordingly (or budget with a lower usable
#: context_limit) -- otherwise the overflow surfaces from the provider, not
#: this local gate. A configurable multiplier or a real tokenizer is an R3+
#: decision (recorded with the A seat, T-A-008).
_CHARS_PER_TOKEN = 4

#: §2.1 L35 assistant tool-part states that already carry a result for
#: replay. ``pending``/``running`` have no result yet -- they must not reach
#: request assembly (§3.3 L212: every legal tool_call_id carries exactly one
#: tool_result before the next request).
_TERMINAL_TOOL_STATES = frozenset({"succeeded", "failed", "denied", "cancelled", "unknown"})


def _require_int_setting(name: str, value: Any, *, minimum: int) -> None:
    """Runtime-settings guard: integer fields reject bools and undersized values.

    The §1 integer-bool rule is a wire-model rule; the runtime dataclass keeps
    the same spirit because ``output_reserve`` feeds the wire
    ``max_output_tokens`` and the budget fields feed wire-visible error
    details.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, not {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """OP-1 injection face: the per-request runtime settings (§7 L339).

    All fields are host-injected (App/CLI construction; R3's ConfigLoader
    replaces this single point). ``ConfigSnapshot`` v1 is never read. The
    model context limit has no default -- an unknown limit is a host
    configuration error, not a value this module guesses (§7 L339).
    """

    model: ModelRef
    #: Model context window limit in tokens (host-configured; >= 1).
    context_limit: int
    #: Output reserve in tokens: becomes ``max_output_tokens`` verbatim (>= 1).
    #: Must cover generated reasoning plus visible text (§8.1 L107 note).
    output_reserve: int
    #: Request total timeout in seconds (> 0); the adapter derives its total
    #: deadline from it (provider.md §3).
    timeout_s: float
    #: Explicit slack in tokens carved out with the reserve (>= 0); absorbs
    #: estimator error and chat-template/tool-schema overhead.
    safety_margin: int = 0
    #: Optional sampling temperature; the contract §2.2 L78 sets no bounds for
    #: it, so this face invents none -- out-of-range values are rejected by
    #: the provider side, not here (a numeric bound would have no contract
    #: basis; that ruling belongs to the A seat).
    temperature: float | None = None
    #: Optional system prompt text; ``None``/empty produces no system message
    #: (TS baseTranscript L99 anchor; see module docstring).
    system_prompt: str | None = None

    def __post_init__(self) -> None:
        _require_int_setting("context_limit", self.context_limit, minimum=1)
        _require_int_setting("output_reserve", self.output_reserve, minimum=1)
        _require_int_setting("safety_margin", self.safety_margin, minimum=0)
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, int | float)
            or not isfinite(self.timeout_s)
            or not self.timeout_s > 0
        ):
            raise ValueError(f"timeout_s must be a finite positive number, got {self.timeout_s!r}")
        if self.temperature is not None and (
            isinstance(self.temperature, bool) or not isinstance(self.temperature, int | float)
        ):
            raise ValueError(f"temperature must be a number or None, got {self.temperature!r}")
        if self.context_limit <= self.output_reserve + self.safety_margin:
            raise ValueError(
                "context_limit must exceed output_reserve + safety_margin "
                f"(got {self.context_limit} <= {self.output_reserve} + {self.safety_margin})"
            )


class ContextLimitExceeded(StoreError):
    """The assembled request does not fit the model's context budget (§7 L339).

    Ownership note (float-reported, T-C1-017): the wire code lives in contracts
    (``CoreErrorCode.CONTEXT_LIMIT_EXCEEDED``) but no exception class existed;
    this follows the SessionBusyError precedent -- a session-layer rejection
    carrying the §8 ``.core`` wire shape via the ``StoreError`` base. The
    history is never trimmed or summarized; the caller keeps it and starts a
    new session.
    """

    code = CoreErrorCode.CONTEXT_LIMIT_EXCEEDED
    retryable = False


def _estimate_input_tokens(messages: Sequence[LLMMessage], tools: Sequence[ToolSpec]) -> int:
    """Deterministic request-size estimate in tokens (~4 chars/token, module docstring).

    Counts every character the provider sees: message part payloads (text,
    reasoning, tool-call names + JSON inputs, tool-result content -- file
    references counted at their stored size, conservative) and the catalog
    (names, descriptions, input schemas -- providers count tool schemas too,
    TS model-request.ts WINDOW_HEADROOM rationale). Pure function: no clock,
    no randomness; the safety margin absorbs its error.
    """
    chars = 0
    for message in messages:
        for part in message.content:
            if isinstance(part, LLMTextPart | LLMReasoningPart):
                chars += len(part.text)
            elif isinstance(part, LLMToolCallPart):
                chars += len(part.name) + len(json.dumps(part.input))
            else:
                chars += len(part.name)
                for item in part.content:
                    if isinstance(item, ToolTextContent):
                        chars += len(item.text)
                    else:
                        chars += len(item.uri) + len(item.mime) + (len(item.name) if item.name else 0)
    for tool in tools:
        chars += len(tool.namespace) + len(tool.name) + len(tool.description) + len(json.dumps(tool.input_schema))
    return ceil(chars / _CHARS_PER_TOKEN)


class ContextBuilder:
    """Builds ``GenerationRequest`` from history + injected runtime settings.

    The §2.2 L80 red line is structural: ``build()`` accepts contract messages
    and ``ToolSpec`` and returns contract types only. Assembly rules (TS
    ``to-llm-message.ts`` anchors): user -> one text part; assistant ->
    text/reasoning parts + tool_call parts (one per terminal tool part, name
    carried verbatim -- the dotted ``namespace.name`` domain is the S3/S6
    invariant, this builder neither invents nor normalizes it); each tool
    result rides its own tool-role message right after its assistant message;
    idle contributes nothing; empty texts contribute nothing.
    """

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings

    def build(
        self,
        *,
        request_id: str,
        history: Sequence[SessionMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> GenerationRequest:
        """Assemble the request for ``request_id`` from ``history``.

        Raises :class:`StoreValidationError` for illegal input faces (unknown
        history entry, non-terminal tool part = missing tool_result, duplicate
        tool_call_id) and :class:`ContextLimitExceeded` when the assembled
        request does not fit the budget. Never mutates ``history`` or
        ``tools``.
        """
        if not isinstance(request_id, str) or not request_id:
            raise StoreValidationError("request_id must be a non-empty string")
        messages = self._assemble(history)
        # §5.1 L253 catalog snapshot: the injected sequence is the fixed
        # catalog for this request; deep-copied so caller mutation cannot
        # reach the built request.
        snapshot = [tool.model_copy(deep=True) for tool in tools]
        self._enforce_budget(messages, snapshot)
        return GenerationRequest(
            request_id=request_id,
            model=self._settings.model,
            messages=messages,
            tools=snapshot,
            # §8.1 L107: the full reserve, never shrunk to fit the history --
            # generated reasoning rides inside it (module docstring).
            max_output_tokens=self._settings.output_reserve,
            temperature=self._settings.temperature,
            timeout_s=self._settings.timeout_s,
        )

    # -- assembly -------------------------------------------------------------

    def _assemble(self, history: Sequence[SessionMessage]) -> list[LLMMessage]:
        settings = self._settings
        out: list[LLMMessage] = []
        if settings.system_prompt:
            out.append(LLMMessage(role="system", content=[LLMTextPart(type="text", text=settings.system_prompt)]))
        emitted_calls: dict[str, None] = {}  # insertion-ordered set of tool_call ids
        for message in history:
            if isinstance(message, UserMessage):
                if message.text == "":
                    continue  # TS anchor: empty user text yields no message
                out.append(LLMMessage(role="user", content=[LLMTextPart(type="text", text=message.text)]))
            elif isinstance(message, AssistantMessage):
                self._append_assistant(out, message, emitted_calls)
            elif isinstance(message, IdleMessage):
                continue  # TS anchor to-llm-message.ts L243-244: idle -> []
            else:
                raise StoreValidationError(
                    f"history entries must be §2.1 contract messages, got {type(message).__name__}",
                    details={"violation": "history_entry_type", "type": type(message).__name__},
                )
        return out

    def _append_assistant(
        self, out: list[LLMMessage], message: AssistantMessage, emitted_calls: dict[str, None]
    ) -> None:
        """One assistant history row -> one assistant message + its tool-role results.

        Terminal tool parts emit the call into the assistant message and the
        paired result as its own tool-role message right after it (TS
        ``assistant()`` anchor). Non-terminal parts are rejected: their result
        is still missing, and every legal tool_call_id must carry exactly one
        tool_result before the next request (§3.3 L212; acceptance C).
        """
        content: list[LLMPart] = []
        results: list[LLMToolResultPart] = []
        for part in message.parts:
            if isinstance(part, AssistantTextPart):
                if part.text == "":
                    continue  # TS anchor: empty text parts are dropped
                content.append(LLMTextPart(type="text", text=part.text))
            elif isinstance(part, AssistantReasoningPart):
                if part.text == "":
                    continue  # TS anchor: empty reasoning contributes nothing
                content.append(LLMReasoningPart(type="reasoning", text=part.text))
            elif isinstance(part, AssistantToolPart):
                if part.state not in _TERMINAL_TOOL_STATES:
                    raise StoreValidationError(
                        f"assistant tool part {part.tool_call_id!r} is {part.state!r}; a non-terminal tool "
                        "call has no tool_result yet and must not reach request assembly",
                        details={
                            "violation": "tool_result_missing",
                            "tool_call_id": part.tool_call_id,
                            "state": part.state,
                        },
                    )
                if part.tool_call_id in emitted_calls:
                    raise StoreValidationError(
                        f"duplicate tool_call_id {part.tool_call_id!r} in history",
                        details={"violation": "duplicate_tool_call_id", "tool_call_id": part.tool_call_id},
                    )
                emitted_calls[part.tool_call_id] = None
                content.append(
                    LLMToolCallPart(
                        type="tool_call",
                        tool_call_id=part.tool_call_id,
                        name=part.name,  # dotted namespace.name, carried verbatim (④-3)
                        input=part.input,
                    )
                )
                results.append(
                    LLMToolResultPart(
                        type="tool_result",
                        tool_call_id=part.tool_call_id,
                        name=part.name,
                        content=list(part.content),  # carried verbatim, never synthesized
                    )
                )
            else:
                raise StoreValidationError(
                    f"assistant parts must be §2.1 AssistantPart members, got {type(part).__name__}",
                    details={"violation": "assistant_part_type", "type": type(part).__name__},
                )
        if content:
            out.append(LLMMessage(role="assistant", content=content))
        for result in results:
            out.append(LLMMessage(role="tool", content=[result]))

    # -- budget ---------------------------------------------------------------

    def _enforce_budget(self, messages: Sequence[LLMMessage], tools: Sequence[ToolSpec]) -> None:
        settings = self._settings
        available = settings.context_limit - settings.output_reserve - settings.safety_margin
        estimated = _estimate_input_tokens(messages, tools)
        if estimated > available:
            raise ContextLimitExceeded(
                f"estimated input tokens {estimated} exceed the available context budget {available} "
                f"(context_limit {settings.context_limit} - output_reserve {settings.output_reserve} "
                f"- safety_margin {settings.safety_margin}); history is preserved -- start a new session",
                details={
                    "context_limit": settings.context_limit,
                    "output_reserve": settings.output_reserve,
                    "safety_margin": settings.safety_margin,
                    "estimated_input_tokens": estimated,
                    "available_input_tokens": available,
                },
            )
