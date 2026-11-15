"""Builtin ``websearch__search`` tool (P7-S4; T-C1-103, ticket §1/§3).

Search the web through the user's selected search integration, adapted
from TS ``packages/core/src/tool/plugin/websearch.ts``: permission gate ->
query -> (ProviderRequired -> the consent flow) -> the results markdown
face, over the :class:`~opencode.websearch.WebSearchService` seam.

TS mapping / deviations, quantified (progress file):

- Wire name: the TS tool is ``name = "websearch"``; PY keeps the webfetch
  precedent (TS ``"webfetch"`` -> ``webfetch__fetch``) and lands
  ``websearch__search`` (namespace ``websearch``, name ``search``). The
  description is verbatim two paragraphs including the dynamic-year
  sentence (``The current year is ${new Date().getFullYear()}. ...`` --
  computed at import like the TS module scope).
- The TS permission face is one ``permission.assert`` call
  (``action = name, resources = [input.query], save = ["*"],
  metadata = input``) before any provider contact; PY maps it onto the
  existing Invoker gate (it already runs before every adapter) with the
  query bound through
  :func:`opencode.tools.policy.extract_resources` -- the webfetch mapping
  exactly.
- The consent flow is the TS ``Form.ask`` pair rebuilt as a HOST-INJECTABLE
  CONSENT CHANNEL (ticket §3-3): PY has no Form service, and the two
  existing ask surfaces cannot carry it -- the approval channel is a closed
  once/always/reject vocabulary and the question tool returns immediately
  (its answer channel is the next user message, which cannot unblock a
  tool call already in flight). The channel receives question-tool-shaped
  forms with the TS texts verbatim (``Web Search`` / the three
  allow/choose/disable options / ``Choose a web search provider``) and
  answers with ``{status, answers}``; the flow runs under one
  :class:`asyncio.Lock` (the TS ``Semaphore(1)`` providerSelectionLock)
  wrapped in ``asyncio.wait_for`` with the injectable 60 s default (the TS
  ``timeoutOrElse("1 minute")`` face). A timeout or a channel cancellation
  raises the cancellation identity (``Web search cancelled``, the TS error
  text); ``disable`` persists ``select(False)``. With NO channel wired (the
  PY production default) the flow fails closed as ProviderRequired --
  never a silent search and never a bare raise. DIFFERENT-BY-DESIGN
  (progress file ③).
- The TS consent re-check order is kept: under the lock, a resolved
  ``default()`` (possibly a concurrent call's completed consent) returns
  and re-searches without a provider hint; an empty registry raises
  ProviderRequired before any form.
- The error face is the TS ``mapError`` table verbatim: a RequestError
  carrying status 429/401 answers the two fixed sentences, any other
  status the generic ``HTTP {status}`` sentence (with
  ``metadata={"provider": id}``); a statusless RequestError and EVERY
  other error (ProviderRequired/ProviderNotFound/Disabled/cancelled, and
  any KV/consent defect) answer the fallback ``Unable to search the web
  for {query}`` wrapper -- fail-closed, ticket §8.
- The result face: ``structured_output`` = ``{provider, results}`` (each
  result carrying ``url`` always; ``title``/``content``/``time.published``
  only when set -- ``None`` is the TS absent key), ``metadata`` =
  ``{"provider": id}``, and the model-visible content is the TS markdown
  face verbatim: ``## [title](url)`` + the ``Published:`` line (the
  ``toISOString`` face: UTC, milliseconds, ``Z``) + the content block,
  joined with blank lines; zero results answer the verbatim
  ``NO_RESULTS`` sentence.
- The TS ``onProvider`` progress callback maps onto the ToolContext's
  ``progress`` seam (the Invoker bounds it before the adapter runs): each
  provider attempt publishes ``{"provider": id}``, the TS
  ``context.progress({provider: provider.id})`` payload verbatim.
- The three TS session hooks (disable-removal of the tool from
  context/compaction/generate) have no PY hook mechanism; nothing is
  created for them (ticket §3-6: no new mechanism). N/A.
- ``idempotency_support`` is False (the webfetch inversion): search results
  are time-varying, so an identical retry is honestly non-convergent;
  ``side_effects`` is True (the query ships to an external provider).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _failure
from opencode.tools.invoker import ToolInvokeError
from opencode.websearch import (
    WebSearchDisabledError,
    WebSearchProviderInfo,
    WebSearchProviderNotFoundError,
    WebSearchProviderRequiredError,
    WebSearchRequestError,
    WebSearchResult,
    WebSearchService,
    http_status,
)

if TYPE_CHECKING:
    # Annotation-only (the question.py house rule): the runtime context
    # flows in through the Invoker.
    from opencode.tools.invoker import ToolContext

WEBSEARCH_NAMESPACE = "websearch"
WEBSEARCH_NAME = "search"
WEBSEARCH_VERSION = 1

#: tool/plugin/websearch.ts ``NO_RESULTS``, verbatim.
WEBSEARCH_NO_RESULTS = "No search results found. Please try a different query."

#: tool/plugin/websearch.ts ``description``, verbatim (two paragraphs; the
#: dynamic year is computed at import, the TS module-scope face).
WEBSEARCH_DESCRIPTION = (
    "Search the web using the user's selected search integration. "
    "Use this for current information beyond knowledge cutoff.\n"
    "\n"
    f"The current year is {datetime.now().year}. Use this year when searching for recent information or current events."
)

#: The TS ``timeoutOrElse("1 minute")`` face: the consent flow's deadline.
WEBSEARCH_CONSENT_TIMEOUT_S = 60.0

#: Input field verbatim (tool/plugin/websearch.ts ``Input``): the query
#: string. The core service's optional ``providerID`` stays internal -- the
#: model never pins a provider directly (only the consent flow does).
WEBSEARCH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Search the web using the user's selected search integration.",
    "properties": {
        "query": {
            "type": "string",
            "description": "Websearch query",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

WEBSEARCH_SPEC = ToolSpec(
    namespace=WEBSEARCH_NAMESPACE,
    name=WEBSEARCH_NAME,
    version=WEBSEARCH_VERSION,
    description=WEBSEARCH_DESCRIPTION,
    input_schema=WEBSEARCH_INPUT_SCHEMA,
    supported_content_types=["text"],
    # The query ships to the selected external search provider -- a real
    # off-host side effect (ticket §3-5: the ask default documents it).
    side_effects=True,
    # The webfetch inversion: search results are time-varying, so an
    # identical retry is honestly non-convergent.
    idempotency_support=False,
)

#: The two fixed sentences of the TS ``httpErrors`` map, verbatim.
_HTTP_ERROR_SENTENCES: dict[int, str] = {
    429: "Web search rate limited (HTTP 429)",
    401: "Web search authentication failed (HTTP 401)",
}


# ---------------------------------------------------------------------------
# The consent channel protocol (ticket §3-3: the Form.ask pair, PY shape).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsentOption:
    """One choice of a consent field (the TS form option: value + label)."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ConsentField:
    """One consent question (the TS form field: key + description + options;
    ``question`` is the question-tool vocabulary for the same face)."""

    key: str
    question: str
    options: tuple[ConsentOption, ...]


@dataclass(frozen=True, slots=True)
class ConsentForm:
    """One consent ask (the TS ``forms.ask`` payload: session, title, the
    ``websearch.provider`` metadata kind, and the field list)."""

    session_id: str
    header: str
    kind: str
    fields: tuple[ConsentField, ...]


@dataclass(frozen=True, slots=True)
class ConsentReply:
    """One consent answer: ``cancelled`` (the TS ``response.status`` face) or
    the chosen option value per field key (``answers``)."""

    status: Literal["answered", "cancelled"]
    answers: dict[str, str] = field(default_factory=dict)


type ConsentChannel = Callable[[ConsentForm], Awaitable[ConsentReply]]


def _allow_form(session_id: str, providers: Sequence[WebSearchProviderInfo]) -> ConsentForm:
    """The first TS form, verbatim texts: the three-option consent question
    with the name-sorted provider list in the allow label."""
    names = ", ".join(provider.name for provider in providers)
    return ConsentForm(
        session_id=session_id,
        header="Web Search",
        kind="websearch.provider",
        fields=(
            ConsentField(
                key="choice",
                question="Allow OpenCode to search the web for up-to-date information?",
                options=(
                    ConsentOption(value="allow", label=f"Allow search via {names}"),
                    ConsentOption(value="choose", label="Choose another provider"),
                    ConsentOption(value="disable", label="Disable web search"),
                ),
            ),
        ),
    )


def _provider_form(session_id: str, providers: Sequence[WebSearchProviderInfo]) -> ConsentForm:
    """The second TS form (the ``choose`` arm), verbatim texts."""
    return ConsentForm(
        session_id=session_id,
        header="Choose a web search provider",
        kind="websearch.provider",
        fields=(
            ConsentField(
                key="provider",
                question="Choose a provider for web search.",
                options=tuple(ConsentOption(value=provider.id, label=provider.name) for provider in providers),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The result faces (the TS markdown render + the structured payload).
# ---------------------------------------------------------------------------


def _published_line(published_ms: float) -> str:
    """The JS ``toISOString`` face: UTC, millisecond precision, ``Z`` suffix."""
    stamp = datetime.fromtimestamp(published_ms / 1000, tz=UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def _render_result(result: WebSearchResult) -> str:
    """One result's markdown block (tool/plugin/websearch.ts, verbatim shape):
    the ``## [title](url)`` heading (``title ?? url``), the ``Published:``
    line only when the timestamp is truthy, the content block only when the
    content is truthy (the TS truthiness faces -- ``0`` and ``""`` render
    nothing)."""
    title = result.title if result.title is not None else result.url
    published = f"\nPublished: {_published_line(result.published_ms)}" if result.published_ms else ""
    content = f"\n\n{result.content}" if result.content else ""
    return f"## [{title}]({result.url}){published}{content}"


def _render_results(results: Sequence[WebSearchResult]) -> str:
    """The model-visible content: the result blocks joined with a blank line,
    or the verbatim ``NO_RESULTS`` sentence."""
    if not results:
        return WEBSEARCH_NO_RESULTS
    return "\n\n".join(_render_result(result) for result in results)


def _result_payload(result: WebSearchResult) -> dict[str, JsonValue]:
    """One result's structured face (schema/websearch.ts ``Result``): ``url``
    always; ``title``/``content``/``time.published`` only when carried
    (``None`` = the TS absent key, dropped at serialization)."""
    payload: dict[str, JsonValue] = {"url": result.url}
    if result.title is not None:
        payload["title"] = result.title
    if result.content is not None:
        payload["content"] = result.content
    if result.published_ms is not None:
        payload["time"] = {"published": result.published_ms}
    return payload


def _request_failure(query: str, error: WebSearchRequestError) -> ToolResult:
    """The TS ``mapError`` RequestError arm: the status-keyed sentence (the
    two fixed ``httpErrors`` entries, the generic ``HTTP {status}``
    sentence on a map miss, or the fallback when statusless) plus the
    provider metadata."""
    fallback = f"Unable to search the web for {query}"
    status = http_status(error.cause)
    if status is None:
        message = fallback
    else:
        message = _HTTP_ERROR_SENTENCES.get(status, f"Web search request failed (HTTP {status})")
    return ToolResult(
        content=[ToolTextContent(type="text", text=message)],
        error=CoreError(code=CoreErrorCode.TOOL_FAILED, message=message, retryable=False),
        metadata={"provider": error.provider_id},
    )


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


class WebSearchCancelledError(Exception):
    """The tool-face cancellation identity (tool/plugin/websearch.ts
    ``new Error("Web search cancelled")``): raised on a channel cancellation
    and on the consent deadline; the model-visible message stays the TS
    fallback sentence (the mapError table sends every non-Request error
    there)."""

    def __init__(self) -> None:
        super().__init__("Web search cancelled")


class WebSearchAdapter:
    """The ``websearch__search`` builtin (§5.1 L253 face: ``spec`` +
    ``execute``).

    Stateful in exactly one field (the subagent/webfetch bind precedent):
    the host-constructed :class:`~opencode.websearch.WebSearchService`
    (provider registry + KV selection; hosts build it over the App's data
    directory). The consent channel and the consent deadline are
    constructor-injected test/host seams; with no channel the consent flow
    fails closed as ProviderRequired (the fallback sentence face). An
    unbound adapter is a host wiring defect, fail-closed as
    :class:`ToolInvokeError` (§7 L341). No ``roots`` constructor argument:
    the tool touches no files.
    """

    def __init__(
        self,
        *,
        consent_channel: ConsentChannel | None = None,
        consent_timeout_s: float = WEBSEARCH_CONSENT_TIMEOUT_S,
    ) -> None:
        self._service: WebSearchService | None = None
        self._consent_channel = consent_channel
        self._consent_timeout_s = consent_timeout_s
        # The TS module-level Semaphore(1) providerSelectionLock: the
        # consent flow is serialized per adapter (each host composition
        # binds exactly one adapter to its App-scoped service).
        self._selection_lock = asyncio.Lock()

    def bind(self, service: WebSearchService) -> None:
        """Bind the host's web search service (each host calls this once, after construction)."""
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return WEBSEARCH_SPEC

    async def _select_provider(self, service: WebSearchService, session_id: str) -> str | None:
        """The consent flow body (the TS lock-protected Effect): returns the
        provider id for the re-search, or None to re-search without a hint
        (the allow/random legs); raises the tagged/cancellation identities
        for every other outcome."""
        # TS order: a resolved default -- possibly a concurrent call's
        # completed consent -- re-searches without asking again.
        if await service.default() is not None:
            return None
        providers = service.providers()
        if not providers:
            raise WebSearchProviderRequiredError()
        channel = self._consent_channel
        if channel is None:
            # No host consent surface is wired (the PY production default):
            # fail closed as ProviderRequired -- the adapter's fallback
            # sentence, never a silent search.
            raise WebSearchProviderRequiredError()
        response = await channel(_allow_form(session_id, providers))
        if response.status == "cancelled":
            raise WebSearchCancelledError()
        choice = response.answers.get("choice")
        if choice == "disable":
            await service.select(False)
            raise WebSearchDisabledError()
        selection: ConsentReply | None = None
        if choice == "choose":
            selection = await channel(_provider_form(session_id, providers))
            if selection.status == "cancelled":
                raise WebSearchCancelledError()
        provider_id = selection.answers.get("provider", "random") if selection is not None else "random"
        if provider_id == "random":
            await service.select("random")
            return None
        if not any(provider.id == provider_id for provider in providers):
            raise WebSearchProviderRequiredError()
        await service.select(provider_id)
        return provider_id

    async def _consent_flow(self, service: WebSearchService, session_id: str) -> str | None:
        """The TS ``providerSelectionLock.withPermit`` face: the consent flow
        runs serialized on the adapter's selection lock."""
        async with self._selection_lock:
            return await self._select_provider(service, session_id)

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        service = self._service
        if service is None:
            raise ToolInvokeError(
                "websearch__search is not bound to a web search service "
                "(host wiring defect: call bind(WebSearchService(...)) after construction)"
            )
        # The Invoker strictly validated this input against
        # WEBSEARCH_INPUT_SCHEMA before execute (adapter contract, §5.1
        # L257); the re-check below is the fail-closed double face.
        query = input.get("query")
        if not isinstance(query, str) or not query:
            return _failure(CoreErrorCode.VALIDATION_ERROR, "query is required and must be a non-empty string")
        fallback = f"Unable to search the web for {query}"
        # The TS onProvider face: each provider attempt publishes the
        # provider id through the Invoker-bounded progress seam.
        on_provider: Callable[[WebSearchProviderInfo], Awaitable[None]] | None = None
        progress = context.progress
        if progress is not None:

            async def on_provider(info: WebSearchProviderInfo) -> None:
                await progress({"provider": info.id})

        try:
            try:
                response = await service.query(query, session_id=context.session_id, on_provider=on_provider)
            except WebSearchProviderRequiredError:
                # The TS catchTag arm: the consent flow under the lock and
                # the deadline, then the re-search.
                try:
                    provider_id = await asyncio.wait_for(
                        self._consent_flow(service, context.session_id), timeout=self._consent_timeout_s
                    )
                except TimeoutError:
                    # The TS timeoutOrElse arm: the deadline answers with
                    # the cancellation identity.
                    raise WebSearchCancelledError() from None
                response = await service.query(
                    query, session_id=context.session_id, provider_id=provider_id, on_provider=on_provider
                )
        except WebSearchRequestError as error:
            return _request_failure(query, error)
        except (
            WebSearchCancelledError,
            WebSearchDisabledError,
            WebSearchProviderNotFoundError,
            WebSearchProviderRequiredError,
        ):
            # The TS mapError non-Request arm: the fallback sentence, no
            # metadata.
            return _failure(CoreErrorCode.TOOL_FAILED, fallback)
        except Exception:
            # Fail-closed (ticket §8): every other face -- a KV/consent
            # defect, an unexpected provider-shape error -- surfaces as the
            # structured fallback wrapper, never a bare raise, never a
            # silent failure.
            return _failure(CoreErrorCode.TOOL_FAILED, fallback)
        content = _render_results(response.results)
        payload: dict[str, JsonValue] = {
            "provider": response.provider_id,
            # The cast is the git.py house restatement: the list-of-dicts is
            # a JsonValue by construction.
            "results": cast("JsonValue", [_result_payload(result) for result in response.results]),
        }
        return ToolResult(
            content=[ToolTextContent(type="text", text=content)],
            structured_output=payload,
            metadata={"provider": response.provider_id},
        )
