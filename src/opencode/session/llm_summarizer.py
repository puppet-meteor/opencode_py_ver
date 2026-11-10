"""Real LLM-backed ``SummaryGenerator`` adapter (CORE-CONTRACTS §2.4; M6 slice three, T-C1-063).

The composition-facing half of the §2.4 L126 sentence 「释放锁后生成摘要」:
``CompactionEngine`` calls the injected Protocol OUTSIDE its locks, and this
module is the production implementation the App composition root injects
(T-B-072 recommendation 1: the compaction chain must not stay in a
test-stub-driven half-wired state). Placement ruling: the adapter serves a
session-layer Protocol against the ``ai.provider`` seam, so it lives beside
the seam it serves -- ``session.runner`` already imports
``opencode.ai.provider.Provider`` (L56 precedent) -- while ``compaction.py``
stays the dependency-free engine core (frozen) and ``ai/**`` stays the pure
provider face (frozen).

Model resolution (⑥ same-source ruling): every R2 generation in a
composition resolves its model from the host-injected ``RuntimeSettings.model``
(``ContextBuilder.build`` pins ``model=self._settings.model``), so the summary
request takes its ``ModelRef`` -- and its ``max_output_tokens`` /
``temperature`` / ``timeout_s`` -- from the SAME settings object the App
already requires when a provider is present. No second config face exists to
drift from, and no request knob is invented here.

Request shape (the contract pins only the ``SummaryResult`` semantics): one
user-role message = a fixed summarization instruction followed by a plain
transcript rendering of the covered settled messages (role-labelled lines;
tool calls by name + input + state, tool results by their text/file-reference
content -- file URIs are never read, §2.1 L72). A transcript is a display,
not a replay: it needs no call/result pairing and never fails on an
unresolved tool part the way a replayed history would. No local token
budgeting: the threshold is deliberately a message-count proxy (the
compaction.py module ruling); an over-large summary prompt surfaces from the
provider and fails closed below.

Aggregation (the runner ``_consume`` L701-804 precedent): text deltas
aggregate per ``part_id`` in first-seen order; reasoning deltas are not
replacement-body material; the LAST ``UsageChunk`` wins and may arrive after
finish (§2.3 L96); a terminal ``ErrorChunk`` ends the call; EOF without a
terminal chunk is an interruption.

Failure semantics (§2.4 L132): every failure RAISES -- the engine and the
service's ``_post_run_compaction`` own the fallout (the error is recorded per
session in ``_compaction_errors``, the original context is never replaced);
``CancelledError`` propagates untouched (a close-time cancel aborts the pass).
Raised shapes: the provider's terminal ``ErrorChunk`` error rides verbatim; a
terminal-less stream raises ``PROVIDER_STREAM_INTERRUPTED`` (retryable, the
runner L795-799 shape); tool-call chunks on the no-tools summary request, or
a finish with an empty body (§2.4 L120 pins ``summary_text`` non-empty),
raise ``VALIDATION_ERROR`` (the runner's ``_GenerationViolation`` mapping).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import uuid4

from opencode.ai.provider import Provider
from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    GenerationRequest,
    LLMMessage,
    LLMTextPart,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    ToolCallArgsDeltaChunk,
    ToolCallEndChunk,
    ToolCallStartChunk,
    Usage,
    UsageChunk,
)
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantReasoningPart,
    AssistantTextPart,
    AssistantToolPart,
    IdleMessage,
    ToolFileContent,
    ToolTextContent,
    UserMessage,
)
from opencode.session.compaction import SettledMessage, SummaryResult
from opencode.session.context import RuntimeSettings

_SUMMARY_INSTRUCTION = (
    "Summarize the session transcript below for the follow-up turns. The summary "
    "replaces the transcript as the session's retained context: keep the goals, the "
    "decisions taken, the tool results worth remembering and the open threads; do "
    "not invent facts. Reply with the summary body only."
)


class SummaryGenerationError(Exception):
    """A failed summary call (§2.4 L132: no replacement happens).

    Carries the §8 wire shape on ``.core`` so the service's per-session
    ``_compaction_errors`` record keeps diagnostic parity with every other
    error face: the provider's terminal ``ErrorChunk`` error rides verbatim;
    the remaining failures map onto the runner's precedents
    (``PROVIDER_STREAM_INTERRUPTED`` for a terminal-less stream,
    ``VALIDATION_ERROR`` for a malformed one). Deliberately NOT a
    ``StoreError`` subclass: the summarizer failure never crosses the
    store/§7 rejection seam -- it is an internal diagnostic record -- and the
    base pins one ClassVar code per exception class, which a verbatim
    provider error cannot honor.
    """

    def __init__(self, core: CoreError) -> None:
        super().__init__(core.message)
        self.core = core


def _violation(violation: str, message: str) -> SummaryGenerationError:
    """The runner's ``_GenerationViolation`` mapping shape: VALIDATION_ERROR."""
    return SummaryGenerationError(
        CoreError(
            code=CoreErrorCode.VALIDATION_ERROR,
            message=message,
            retryable=False,
            details={"violation": violation},
        )
    )


def _interrupted(request_id: str, detail: str) -> SummaryGenerationError:
    """The runner L795-799 shape: EOF without a terminal chunk is an interruption."""
    return SummaryGenerationError(
        CoreError(
            code=CoreErrorCode.PROVIDER_STREAM_INTERRUPTED,
            message=(
                f"provider stream for summary request {request_id} ended without a finish or error chunk ({detail})"
            ),
            retryable=True,
        )
    )


def _render_assistant(message: AssistantMessage, lines: list[str]) -> None:
    for part in message.parts:
        if isinstance(part, AssistantTextPart):
            lines.append(f"[assistant] {part.text}")
        elif isinstance(part, AssistantReasoningPart):
            lines.append(f"[assistant reasoning] {part.text}")
        else:
            assert isinstance(part, AssistantToolPart)
            lines.append(f"[assistant tool {part.name}] state={part.state} input={json.dumps(part.input)}")
            for content in part.content:
                if isinstance(content, ToolTextContent):
                    lines.append(f"[tool result {part.name}] {content.text}")
                else:
                    assert isinstance(content, ToolFileContent)
                    # §2.1 L72: the reference is rendered, the URI is never read.
                    lines.append(f"[tool result {part.name}] file {content.uri} ({content.mime})")


def _render_transcript(covered: Sequence[SettledMessage]) -> str:
    """Render the covered settled history as plain transcript lines.

    A display, not a replay (module docstring): every union member renders
    without pairing or validation, file references stay references, and
    nothing is truncated or guessed.
    """
    lines: list[str] = []
    for item in covered:
        message = item.message
        if isinstance(message, UserMessage):
            lines.append(f"[user] {message.text}")
        elif isinstance(message, AssistantMessage):
            _render_assistant(message, lines)
        else:
            assert isinstance(message, IdleMessage)
            line = f"[run {message.run_id}] {message.outcome}"
            if message.error is not None:
                line = f"{line} error: {message.error.message}"
            lines.append(line)
    return "\n".join(lines)


class ProviderSummarizer:
    """The production ``SummaryGenerator`` (§2.4 L126): one provider call per
    ``summarize`` invocation.

    Constructed over the composition's provider and the SAME
    ``RuntimeSettings`` every other R2 generation uses (the ⑥ same-source
    ruling -- model, output reserve, temperature and timeout all ride the one
    host-injected face; nothing is re-decided here). The call emits a
    no-tools ``GenerationRequest`` whose single user message carries the
    summarization instruction plus the covered transcript, aggregates the
    stream to its terminal chunk, and answers the three ``SummaryResult``
    fields: the aggregated text body, the settings' ``ModelRef`` and the
    last-reported ``Usage`` (all-null = unknown when the provider reported
    none, §2.1). The finish reason is not gated (the runner precedent: the
    provider's logical finish rides as received); every failure mode raises
    (module docstring) so the §2.4 L132 red line -- zero replacement on
    failure/cancel/crash -- stays with the engine/service policy.
    """

    def __init__(self, *, provider: Provider, settings: RuntimeSettings) -> None:
        self._provider = provider
        self._settings = settings

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult:
        if not covered:
            # The engine's boundary math always passes a non-empty covered
            # interval (40 - 8 = 32); an empty one is a caller bug, failed
            # closed here BEFORE any model call spends usage (④ disclosed).
            raise ValueError("covered history must not be empty")
        settings = self._settings
        request = GenerationRequest(
            request_id=f"compaction_{uuid4().hex}",
            model=settings.model,
            messages=[
                LLMMessage(
                    role="user",
                    content=[
                        LLMTextPart(
                            type="text",
                            text=f"{_SUMMARY_INSTRUCTION}\n\n{_render_transcript(covered)}",
                        )
                    ],
                )
            ],
            tools=[],
            max_output_tokens=settings.output_reserve,
            temperature=settings.temperature,
            timeout_s=settings.timeout_s,
        )
        parts: dict[str, list[str]] = {}
        usage = Usage()  # all-None = unknown until a UsageChunk arrives (§2.1)
        finish: FinishChunk | None = None
        error: CoreError | None = None
        async for chunk in self._provider.stream(request):
            if isinstance(chunk, TextDeltaChunk):
                parts.setdefault(chunk.part_id, []).append(chunk.delta)
            elif isinstance(chunk, ReasoningDeltaChunk):
                continue  # reasoning is not replacement-body material (§2.1 L33)
            elif isinstance(chunk, ToolCallStartChunk | ToolCallArgsDeltaChunk | ToolCallEndChunk):
                # The summary request carries no tools (§2.2 L78: an empty tools
                # list is a state); a provider emitting call chunks for it is
                # off-contract and its text cannot be trusted as the body.
                raise _violation(
                    "summary_tool_call_chunk",
                    "the summary stream carried tool-call chunks on a no-tools request (§2.2 L78)",
                )
            elif isinstance(chunk, UsageChunk):
                usage = chunk.usage  # the last one wins; may arrive after finish (§2.3 L96)
            elif isinstance(chunk, FinishChunk):
                if finish is None:
                    finish = chunk
            else:
                assert isinstance(chunk, ErrorChunk)
                # §2.3 L98: the terminal error rides verbatim and the call ends.
                error = chunk.error
                break
        if error is not None:
            raise SummaryGenerationError(error)
        if finish is None:
            raise _interrupted(request.request_id, "empty stream" if not parts else "stream closed")
        text = "".join("".join(deltas) for deltas in parts.values())
        if not text:
            # §2.4 L120 pins summary_text non-empty; failing here keeps the
            # wire's pydantic guard out of the commit path (④ disclosed).
            raise _violation(
                "summary_empty_body",
                "the summary stream finished without any text (§2.4 L120 requires a non-empty body)",
            )
        return SummaryResult(text=text, model=settings.model, usage=usage)
