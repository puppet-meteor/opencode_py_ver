"""Builtin ``websearch__search`` integration tests (P7-S4; T-C1-103, ticket §4).

The full ticket §4 face map through the public faces
(adapter.bind -> registry register -> snapshot -> invoke by wire name,
plus the direct adapter execute where metadata is only carried), all
against local fixture providers (ticket §4-7: zero network):

* §4-1 -- the consent flow: the allow leg (the verbatim three-option form,
  ``random`` persisted to the KV document, the search runs), the choose leg
  (the verbatim provider form, the chosen id persisted + restart-stable),
  the disable leg (``false`` persisted + restart-stable, zero provider
  calls), the 1-minute deadline leg (the cancellation identity), the
  cancelled-answer leg, the unwired-channel production default
  (fail-closed), the empty-registry leg (ProviderRequired before any
  form), and the resolved-selection re-check (no form at all).
* §4-2 -- the selection faces: ID/``random``/``false``/dangling-id
  persistence across service restarts, the invalid-stored-value hygiene
  removal, session affinity (the cell wins without a draw;
  ``forget_session`` drops it), the per-attempt progress publication, the
  429 rotation ring (retry-after window, explicit-selection fail-closed on
  429, exhausted ring returning the last cooldown error without reopening
  consent, cooldown expiry retried after the window), the non-429
  no-rotation face, and the ``cooldownMillis`` parsing table.
* §4-3 -- the result faces: the TS markdown shape (``## [title](url)`` +
  the ``Published:`` ISO line + the content block, blank-line joined), the
  TS truthiness collapses (``title ?? url``, falsy published/content), the
  ``NO_RESULTS`` verbatim sentence, and the structured payload's
  only-when-set fields.
* §4-4 -- the error mapping: the two fixed HTTP sentences, the generic
  ``HTTP {status}`` sentence, the statusless fallback (all with the
  provider metadata), the tagged errors' fallback (no metadata), and the
  service-level ProviderNotFound face.
* §4-5 -- the permission order: a denied policy rule means zero provider
  executions (the Invoker gate runs before the adapter).
* §4-6 -- the policy/Spec/registration pins: the ``_DEFAULT_EFFECTS`` row,
  the untouched ``_ASK_NAMESPACES``, the ``extract_resources`` query
  binding, the Spec faces, the wire-name resolve, the export surface, the
  unbound-adapter wiring defect, and the input double face.
* §4-8 -- the fail-closed discipline: every error surfaces as the
  structured fallback/sentence wrapper (never a bare raise), and the KV
  selection is restart-stable (woven through the consent legs).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import BaseModel

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult
from opencode.session.service import IdFactory
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry, builtin
from opencode.tools.builtin.websearch import (
    WEBSEARCH_CONSENT_TIMEOUT_S,
    WEBSEARCH_DESCRIPTION,
    WEBSEARCH_INPUT_SCHEMA,
    WEBSEARCH_NO_RESULTS,
    WEBSEARCH_SPEC,
    ConsentChannel,
    ConsentForm,
    ConsentReply,
    WebSearchAdapter,
)
from opencode.tools.invoker import ToolInvokeError
from opencode.tools.policy import (
    _ASK_NAMESPACES,
    _DEFAULT_EFFECTS,
    PermissionPolicy,
    PermissionRule,
    extract_resources,
)
from opencode.websearch import (
    PROVIDER_KEY,
    WebSearchDisabledError,
    WebSearchProviderInfo,
    WebSearchProviderNotFoundError,
    WebSearchRequestError,
    WebSearchResult,
    WebSearchService,
    cooldown_millis,
    http_status,
)

_FIXED_TIME = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_tws"
_RUN_ID = "run_tws"
_MESSAGE_ID = "msg_tws"
_TOOL_CALL_ID = "call_tws"

#: A published timestamp with a known ``toISOString`` face.
_PUBLISHED_MS = 1731672000000  # -> 2024-11-15T12:00:00.000Z
_PUBLISHED_LINE = "\nPublished: 2024-11-15T12:00:00.000Z"


# ---------------------------------------------------------------------------
# harness (the test_tool_webfetch plain-registry recipe)
# ---------------------------------------------------------------------------


def _fixed_clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    """EventSink stand-in: records every committed batch verbatim."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []
        self.drafts: list[EventDraft[BaseModel]] = []

    async def __call__(self, drafts: Sequence[EventDraft[BaseModel]]) -> None:
        self.batches.append(tuple(drafts))
        self.drafts.extend(drafts)


class _RecordingProgress:
    """The ToolContext progress seam double: records every payload."""

    def __init__(self) -> None:
        self.payloads: list[JsonObject] = []

    async def __call__(self, payload: JsonObject) -> None:
        self.payloads.append(payload)


def _context(working_dir: Path, progress: _RecordingProgress | None = None) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
        progress=progress,
    )


def _registry(adapter: WebSearchAdapter, policy: PermissionPolicy | None = None) -> ToolRegistry:
    """A plain registry with the websearch adapter, one pinned snapshot.

    ``policy`` is the Invoker's gate policy: absent (the default) the gate
    is skipped -- the consent-flow tests must use this arm, since a policy
    whose evaluation lands on ``ask`` without an approval channel would
    cancel instead of searching; the §4-5 deny test passes the rule-bearing
    policy.
    """
    registry = ToolRegistry(
        sink=cast("Any", _RecordingSink()),
        clock=_fixed_clock,
        ids=cast(IdFactory, _FixedIds()),
        policy=policy,
    )
    registry.register(adapter)
    registry.snapshot(_SESSION_ID)
    return registry


async def _invoke(registry: ToolRegistry, working_dir: Path, input: JsonObject) -> ToolOutcome:
    return await registry.invoke(_context(working_dir), "websearch__search", input, _TOOL_CALL_ID)


def _texts(outcome: ToolOutcome) -> list[str]:
    return [part.text for part in outcome.content if isinstance(part, ToolTextContent)]


def _one_text(outcome: ToolOutcome) -> str:
    texts = _texts(outcome)
    assert len(texts) == 1
    return texts[0]


def _failed(outcome: ToolOutcome, code: CoreErrorCode) -> str:
    """Assert the fail-closed terminal and return its model-readable text."""
    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code is code, _one_text(outcome)
    return _one_text(outcome)


def _texts_direct(result: ToolResult) -> str:
    """The single text of a direct adapter ToolResult (the only surface
    that carries metadata)."""
    texts = [part.text for part in result.content if isinstance(part, ToolTextContent)]
    assert len(texts) == 1
    return texts[0]


# ---------------------------------------------------------------------------
# the local fixture faces (ticket §4-7: providers + channel, zero network)
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """A fixture provider: scripted results or a settable error, recording
    every query (the seam contract: any raised exception becomes the
    service's RequestError wrapper)."""

    def __init__(
        self,
        provider_id: str,
        name: str,
        *,
        results: Sequence[WebSearchResult] = (),
        error: Exception | None = None,
    ) -> None:
        self.id = provider_id
        self.name = name
        self.results = tuple(results)
        self.error = error
        self.calls: list[str] = []

    async def execute(self, query: str) -> Sequence[WebSearchResult]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.results


def _http_error(status: int, headers: dict[str, str] | None = None) -> httpx2.HTTPStatusError:
    """An HTTP-carrying provider failure: the status feeds the cooldown ring
    and the error table through the httpx2-shaped ``response`` attribute."""
    request = httpx2.Request("GET", "https://search.invalid/search")
    response = httpx2.Response(status, headers=headers, request=request)
    return httpx2.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _answered(answers: dict[str, str]) -> ConsentReply:
    return ConsentReply(status="answered", answers=dict(answers))


def _cancelled() -> ConsentReply:
    return ConsentReply(status="cancelled")


class _ScriptedChannel:
    """ConsentChannel double: records every form verbatim, answers from a
    scripted reply queue (an empty queue makes any unexpected ask fail the
    test loudly)."""

    def __init__(self, *replies: ConsentReply, delay_s: float = 0.0) -> None:
        self.forms: list[ConsentForm] = []
        self._replies = list(replies)
        self._delay_s = delay_s

    async def __call__(self, form: ConsentForm) -> ConsentReply:
        self.forms.append(form)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._replies.pop(0)


class _MsClock:
    """Injectable epoch-ms clock (the TS Clock.currentTimeMillis seam)."""

    def __init__(self, ms: int = 1_000_000) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class _Rng:
    """Deterministic Math.random seam: pops scripted values, then draws 0.0."""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0) if self._values else 0.0


def _service(tmp_path: Path, *, clock: _MsClock | None = None, rng: _Rng | None = None) -> WebSearchService:
    return WebSearchService(tmp_path / "kv.json", clock=clock, rng=rng)


def _adapter(
    service: WebSearchService,
    *,
    channel: ConsentChannel | None = None,
    timeout_s: float = WEBSEARCH_CONSENT_TIMEOUT_S,
) -> WebSearchAdapter:
    adapter = WebSearchAdapter(consent_channel=channel, consent_timeout_s=timeout_s)
    adapter.bind(service)
    return adapter


def _two_providers(service: WebSearchService) -> tuple[_ScriptedProvider, _ScriptedProvider]:
    """The standard two-provider registry (insertion alpha, beta)."""
    alpha = _ScriptedProvider("alpha", "Alpha", results=(WebSearchResult(url="https://a.example/1"),))
    beta = _ScriptedProvider("beta", "Beta", results=(WebSearchResult(url="https://b.example/1"),))
    service.register_provider(alpha)
    service.register_provider(beta)
    return alpha, beta


def _kv_document(tmp_path: Path) -> Any:
    return json.loads((tmp_path / "kv.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# §4-1: the consent flow
# ---------------------------------------------------------------------------


async def test_an_allow_consent_persists_random_and_searches(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99))
    alpha, beta = _two_providers(service)
    channel = _ScriptedChannel(_answered({"choice": "allow"}))
    registry = _registry(_adapter(service, channel=channel))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert _one_text(outcome) == "## [https://b.example/1](https://b.example/1)"
    assert outcome.structured_output == {
        "provider": "beta",
        "results": [{"url": "https://b.example/1"}],
    }
    # The draw (rng 0.99 over [Alpha, Beta]) picked beta; alpha never ran.
    assert beta.calls == ["hello"]
    assert alpha.calls == []
    # The allow leg persists the random selection (restart-stable KV).
    assert _kv_document(tmp_path) == {PROVIDER_KEY: "random"}
    # The first form is the verbatim TS consent question.
    assert len(channel.forms) == 1
    form = channel.forms[0]
    assert form.session_id == _SESSION_ID
    assert form.header == "Web Search"
    assert form.kind == "websearch.provider"
    (field,) = form.fields
    assert field.key == "choice"
    assert field.question == "Allow OpenCode to search the web for up-to-date information?"
    assert [(option.value, option.label) for option in field.options] == [
        ("allow", "Allow search via Alpha, Beta"),
        ("choose", "Choose another provider"),
        ("disable", "Disable web search"),
    ]


async def test_a_choose_consent_persists_the_chosen_provider(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    channel = _ScriptedChannel(_answered({"choice": "choose"}), _answered({"provider": "beta"}))
    registry = _registry(_adapter(service, channel=channel))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert outcome.structured_output == {
        "provider": "beta",
        "results": [{"url": "https://b.example/1"}],
    }
    assert beta.calls == ["hello"]
    assert alpha.calls == []
    assert _kv_document(tmp_path) == {PROVIDER_KEY: "beta"}
    # The second form is the verbatim TS provider chooser.
    assert len(channel.forms) == 2
    form = channel.forms[1]
    assert form.session_id == _SESSION_ID
    assert form.header == "Choose a web search provider"
    assert form.kind == "websearch.provider"
    (field,) = form.fields
    assert field.key == "provider"
    assert field.question == "Choose a provider for web search."
    assert [(option.value, option.label) for option in field.options] == [("alpha", "Alpha"), ("beta", "Beta")]
    # Restart stability: a fresh service over the same KV resolves the
    # persisted id with no consent surface at all.
    restarted = _service(tmp_path)
    alpha2, beta2 = _two_providers(restarted)
    registry2 = _registry(_adapter(restarted))
    outcome2 = await _invoke(registry2, tmp_path, {"query": "again"})
    assert outcome2.state == "succeeded"
    assert beta2.calls == ["again"]
    assert alpha2.calls == []


async def test_a_disable_consent_persists_false_and_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    channel = _ScriptedChannel(_answered({"choice": "disable"}))
    registry = _registry(_adapter(service, channel=channel))

    text = _failed(await _invoke(registry, tmp_path, {"query": "hello"}), CoreErrorCode.TOOL_FAILED)
    assert text == "Unable to search the web for hello"
    assert alpha.calls == []
    assert beta.calls == []
    assert _kv_document(tmp_path) == {PROVIDER_KEY: False}
    # Restart stability: the false selection raises Disabled on the fresh
    # service, and a fresh adapter answers the fallback without ever
    # reopening consent (the disabled face is not a ProviderRequired).
    restarted = _service(tmp_path)
    _two_providers(restarted)
    with pytest.raises(WebSearchDisabledError):
        await restarted.default()
    channel2 = _ScriptedChannel()  # any ask would fail the test loudly
    registry2 = _registry(_adapter(restarted, channel=channel2))
    text2 = _failed(await _invoke(registry2, tmp_path, {"query": "again"}), CoreErrorCode.TOOL_FAILED)
    assert text2 == "Unable to search the web for again"
    assert channel2.forms == []


async def test_the_consent_deadline_answers_the_cancellation_identity(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    channel = _ScriptedChannel(_answered({"choice": "allow"}), delay_s=0.5)
    registry = _registry(_adapter(service, channel=channel, timeout_s=0.05))

    text = _failed(await _invoke(registry, tmp_path, {"query": "hello"}), CoreErrorCode.TOOL_FAILED)
    assert text == "Unable to search the web for hello"
    assert alpha.calls == []
    assert beta.calls == []
    # The deadline fired before any selection was persisted.
    assert not (tmp_path / "kv.json").exists()


async def test_a_cancelled_consent_answer_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    channel = _ScriptedChannel(_cancelled())
    registry = _registry(_adapter(service, channel=channel))

    text = _failed(await _invoke(registry, tmp_path, {"query": "hello"}), CoreErrorCode.TOOL_FAILED)
    assert text == "Unable to search the web for hello"
    assert alpha.calls == []
    assert beta.calls == []
    assert not (tmp_path / "kv.json").exists()
    assert len(channel.forms) == 1


async def test_an_unwired_consent_channel_fails_closed(tmp_path: Path) -> None:
    # The PY production default: no host consent surface exists, so an
    # unresolved selection can never silently search.
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    registry = _registry(_adapter(service))

    text = _failed(await _invoke(registry, tmp_path, {"query": "hello"}), CoreErrorCode.TOOL_FAILED)
    assert text == "Unable to search the web for hello"
    assert alpha.calls == []
    assert beta.calls == []


async def test_an_empty_registry_fails_closed_before_any_form(tmp_path: Path) -> None:
    service = _service(tmp_path)
    channel = _ScriptedChannel()  # any ask would fail the test loudly
    registry = _registry(_adapter(service, channel=channel))

    text = _failed(await _invoke(registry, tmp_path, {"query": "hello"}), CoreErrorCode.TOOL_FAILED)
    assert text == "Unable to search the web for hello"
    assert channel.forms == []


async def test_a_resolved_selection_skips_the_consent_flow(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    await service.select("alpha")
    channel = _ScriptedChannel()  # any ask would fail the test loudly
    registry = _registry(_adapter(service, channel=channel))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert alpha.calls == ["hello"]
    assert beta.calls == []
    assert channel.forms == []


# ---------------------------------------------------------------------------
# §4-2: the selection, affinity and cooldown faces
# ---------------------------------------------------------------------------


async def test_the_selection_persists_across_service_restarts(tmp_path: Path) -> None:
    kv = tmp_path / "kv.json"
    first = WebSearchService(kv)
    _alpha, _beta = _two_providers(first)
    await first.select("alpha")
    resolved = await first.default()
    assert resolved == WebSearchProviderInfo(id="alpha", name="Alpha")

    # Restart 1: the id selection survives (fresh in-memory registry).
    second = WebSearchService(kv)
    _two_providers(second)
    assert await second.default() == WebSearchProviderInfo(id="alpha", name="Alpha")
    await second.select("random")
    # The random inspection: the global affinity cell or the FIRST-inserted
    # provider, blind to cooldowns -- never a selection or a consent reopen.
    assert await second.default() == WebSearchProviderInfo(id="alpha", name="Alpha")

    await second.select(False)
    with pytest.raises(WebSearchDisabledError):
        await second.default()
    # Restart 2: the false selection survives.
    third = WebSearchService(kv)
    with pytest.raises(WebSearchDisabledError):
        await third.default()

    # A dangling id reads unresolved (a valid string selection naming an
    # unregistered provider): the consent flow's re-check entry face.
    await third.select("ghost")
    fourth = WebSearchService(kv)
    assert await fourth.default() is None


async def test_invalid_stored_selections_are_removed(tmp_path: Path) -> None:
    kv = tmp_path / "kv.json"
    for bad in (42, True, {"kind": "bogus"}, ["random"]):
        kv.write_text(json.dumps({PROVIDER_KEY: bad}), encoding="utf-8")
        service = WebSearchService(kv)
        assert await service.default() is None
        assert PROVIDER_KEY not in _kv_document(tmp_path)
    # A whole-file corruption reads as the empty store.
    kv.write_text("not json at all", encoding="utf-8")
    service = WebSearchService(kv)
    assert await service.default() is None


async def test_random_rotation_keeps_session_affinity(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99, 0.0, 0.0))
    alpha, beta = _two_providers(service)
    await service.select("random")

    # Session A draws beta (rng 0.99 over [alpha, beta]) and keeps the cell.
    assert (await service.query("q1", session_id="ses_a")).provider_id == "beta"
    # Session A's second query reuses the cell WITHOUT a draw (the next rng
    # value 0.0 would pick alpha).
    assert (await service.query("q2", session_id="ses_a")).provider_id == "beta"
    assert alpha.calls == []
    # Session B has no cell: it draws fresh (rng 0.0 -> alpha).
    assert (await service.query("q3", session_id="ses_b")).provider_id == "alpha"
    # The cleanup face drops session A's cell: the next query re-draws.
    service.forget_session("ses_a")
    assert (await service.query("q4", session_id="ses_a")).provider_id == "alpha"
    assert beta.calls == ["q1", "q2"]
    assert alpha.calls == ["q3", "q4"]


async def test_each_provider_attempt_publishes_progress(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99, 0.0))
    _alpha, beta = _two_providers(service)
    beta.error = _http_error(429, {"retry-after": "3600"})
    await service.select("random")
    registry = _registry(_adapter(service))
    progress = _RecordingProgress()

    outcome = await registry.invoke(
        _context(tmp_path, progress=progress), "websearch__search", {"query": "hello"}, _TOOL_CALL_ID
    )
    assert outcome.state == "succeeded"
    assert outcome.structured_output == {"provider": "alpha", "results": [{"url": "https://a.example/1"}]}
    # The TS onProvider face: one publication per ATTEMPT, including the
    # failing one (the {"provider": id} payloads verbatim).
    assert progress.payloads == [{"provider": "beta"}, {"provider": "alpha"}]


async def test_random_rotation_skips_a_429_provider(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99))
    alpha, beta = _two_providers(service)
    beta.error = _http_error(429, {"retry-after": "3600"})
    await service.select("random")
    registry = _registry(_adapter(service))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert outcome.structured_output == {"provider": "alpha", "results": [{"url": "https://a.example/1"}]}
    assert beta.calls == ["hello"]
    assert alpha.calls == ["hello"]
    # The same session's next query rides the mutated affinity cell
    # straight to alpha (beta is cooling down).
    outcome2 = await _invoke(registry, tmp_path, {"query": "again"})
    assert outcome2.structured_output == {"provider": "alpha", "results": [{"url": "https://a.example/1"}]}
    assert beta.calls == ["hello"]
    assert alpha.calls == ["hello", "again"]
    # An explicit selection ignores the cooldown ring and fails closed on
    # its own error (the non-random leg never reads cooldowns).
    with pytest.raises(WebSearchRequestError) as request_error:
        await service.query("explicit", provider_id="beta")
    assert http_status(request_error.value.cause) == 429


async def test_an_exhausted_ring_returns_the_last_cooldown_error(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99, 0.0))
    alpha, beta = _two_providers(service)
    alpha.error = _http_error(429, {"retry-after": "3600"})
    beta.error = _http_error(429, {"retry-after": "3600"})
    await service.select("random")
    channel = _ScriptedChannel()  # any ask would fail the test loudly
    adapter = _adapter(service, channel=channel)
    registry = _registry(adapter)

    # First pass: beta draws, 429s, rotates to alpha, 429s -- the ring is
    # exhausted, so the LAST recorded cooldown error answers.
    result = await adapter.execute(_context(tmp_path), {"query": "hello"})
    assert result.error is not None
    assert result.error.code is CoreErrorCode.TOOL_FAILED
    assert _texts_direct(result) == "Web search rate limited (HTTP 429)"
    assert result.metadata == {"provider": "alpha"}
    assert beta.calls == ["hello"]
    assert alpha.calls == ["hello"]
    # Every provider cooling down must NOT reopen consent: the resolved
    # default (cooldown-blind) re-searches into the same cooldown error,
    # and the live cooldown means no new provider execution at all.
    outcome = await _invoke(registry, tmp_path, {"query": "again"})
    text = _failed(outcome, CoreErrorCode.TOOL_FAILED)
    assert text == "Web search rate limited (HTTP 429)"
    assert channel.forms == []
    assert alpha.calls == ["hello"]
    assert beta.calls == ["hello"]


async def test_an_expired_cooldown_is_retried_after_the_window(tmp_path: Path) -> None:
    clock = _MsClock(ms=1_000_000)
    service = _service(tmp_path, clock=clock, rng=_Rng(0.0))
    alpha = _ScriptedProvider("alpha", "Alpha", results=(WebSearchResult(url="https://a.example/1"),))
    service.register_provider(alpha)
    alpha.error = _http_error(429, {"retry-after": "1"})  # a 1 s window
    await service.select("random")

    # The single provider exhausts its own ring within one query (the
    # attempted set), answering the 429 sentence.
    with pytest.raises(WebSearchRequestError) as first_error:
        await service.query("q1", session_id="ses_a")
    assert http_status(first_error.value.cause) == 429
    assert alpha.calls == ["q1"]
    # Inside the window: the cooldown is skipped without executing.
    with pytest.raises(WebSearchRequestError):
        await service.query("q2", session_id="ses_a")
    assert alpha.calls == ["q1"]
    # After the window: the retry succeeds (the scripted 429 must be
    # cleared so the provider answers normally again).
    clock.ms += 2_000
    alpha.error = None
    response = await service.query("q3", session_id="ses_a")
    assert response.provider_id == "alpha"
    assert alpha.calls == ["q1", "q3"]


async def test_a_non_429_failure_does_not_rotate(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.99))
    alpha, beta = _two_providers(service)
    beta.error = _http_error(500)
    await service.select("random")
    adapter = _adapter(service)

    result = await adapter.execute(_context(tmp_path), {"query": "hello"})
    assert result.error is not None
    assert result.error.code is CoreErrorCode.TOOL_FAILED
    assert _texts_direct(result) == "Web search request failed (HTTP 500)"
    assert result.metadata == {"provider": "beta"}
    assert beta.calls == ["hello"]
    assert alpha.calls == []


def test_cooldown_millis_parses_the_ts_faces() -> None:
    # The missing/blank/unparsable defaults.
    assert cooldown_millis(None, 0) == 60_000
    assert cooldown_millis("", 0) == 60_000
    assert cooldown_millis("   ", 0) == 60_000
    assert cooldown_millis("abc", 0) == 60_000
    assert cooldown_millis("nan", 0) == 60_000
    # Numeric seconds, scaled to milliseconds when non-negative.
    assert cooldown_millis("2", 0) == 2_000
    assert cooldown_millis("2.5", 0) == 2_500
    assert cooldown_millis("0", 0) == 0
    assert cooldown_millis("-1", 0) == 60_000
    # An HTTP-date: the remaining delta, clamped at zero.
    future = datetime(2038, 1, 19, 3, 14, 7, tzinfo=UTC)
    future_ms = int(future.timestamp() * 1000)
    assert cooldown_millis("Tue, 19 Jan 2038 03:14:07 GMT", 0) == future_ms
    assert cooldown_millis("Tue, 19 Jan 2038 03:14:07 GMT", future_ms + 5_000) == 0
    assert cooldown_millis("Thu, 01 Jan 1970 00:00:00 GMT", 1_000_000) == 0


# ---------------------------------------------------------------------------
# §4-3: the result faces
# ---------------------------------------------------------------------------


async def test_results_render_the_ts_markdown_face(tmp_path: Path) -> None:
    alpha = _ScriptedProvider(
        "alpha",
        "Alpha",
        results=(
            WebSearchResult(
                url="https://a.example/one",
                title="First",
                content="Body one",
                published_ms=_PUBLISHED_MS,
            ),
            WebSearchResult(url="https://b.example/two"),
        ),
    )
    service = _service(tmp_path)
    service.register_provider(alpha)
    await service.select("alpha")
    registry = _registry(_adapter(service))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert _one_text(outcome) == (
        "## [First](https://a.example/one)" + _PUBLISHED_LINE + "\n\nBody one"
        "\n\n"
        "## [https://b.example/two](https://b.example/two)"
    )
    assert outcome.structured_output == {
        "provider": "alpha",
        "results": [
            {
                "url": "https://a.example/one",
                "title": "First",
                "content": "Body one",
                "time": {"published": _PUBLISHED_MS},
            },
            {"url": "https://b.example/two"},
        ],
    }


async def test_missing_fields_collapse_per_the_ts_truthiness(tmp_path: Path) -> None:
    # The render face uses TS truthiness (0 and "" render nothing, a None
    # title falls back to the url); the structured payload keeps the
    # carried fields ("" and 0 serialize; None drops).
    alpha = _ScriptedProvider(
        "alpha",
        "Alpha",
        results=(WebSearchResult(url="https://x.example/1", title=None, content="", published_ms=0),),
    )
    service = _service(tmp_path)
    service.register_provider(alpha)
    await service.select("alpha")
    registry = _registry(_adapter(service))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert _one_text(outcome) == "## [https://x.example/1](https://x.example/1)"
    assert outcome.structured_output == {
        "provider": "alpha",
        "results": [{"url": "https://x.example/1", "content": "", "time": {"published": 0}}],
    }


async def test_zero_results_answer_the_no_results_sentence(tmp_path: Path) -> None:
    alpha = _ScriptedProvider("alpha", "Alpha", results=())
    service = _service(tmp_path)
    service.register_provider(alpha)
    await service.select("alpha")
    registry = _registry(_adapter(service))

    outcome = await _invoke(registry, tmp_path, {"query": "hello"})
    assert outcome.state == "succeeded"
    assert _one_text(outcome) == "No search results found. Please try a different query."
    assert outcome.structured_output == {"provider": "alpha", "results": []}


# ---------------------------------------------------------------------------
# §4-4: the error mapping
# ---------------------------------------------------------------------------


async def test_request_errors_map_to_the_ts_sentence_table(tmp_path: Path) -> None:
    service = _service(tmp_path)
    alpha = _ScriptedProvider("alpha", "Alpha", results=(WebSearchResult(url="https://a.example/1"),))
    service.register_provider(alpha)
    await service.select("alpha")
    adapter = _adapter(service)

    for status, expected in (
        (429, "Web search rate limited (HTTP 429)"),
        (401, "Web search authentication failed (HTTP 401)"),
        (500, "Web search request failed (HTTP 500)"),
    ):
        alpha.error = _http_error(status)
        result = await adapter.execute(_context(tmp_path), {"query": "hello"})
        assert result.error is not None
        assert result.error.code is CoreErrorCode.TOOL_FAILED
        assert _texts_direct(result) == expected
        assert result.metadata == {"provider": "alpha"}

    # A statusless RequestError: the fallback sentence, still with the
    # provider metadata (the TS RequestError arm keeps it either way).
    alpha.error = RuntimeError("boom")
    result = await adapter.execute(_context(tmp_path), {"query": "hello"})
    assert result.error is not None
    assert result.error.code is CoreErrorCode.TOOL_FAILED
    assert _texts_direct(result) == "Unable to search the web for hello"
    assert result.metadata == {"provider": "alpha"}


async def test_tagged_errors_answer_the_fallback_sentence(tmp_path: Path) -> None:
    # ProviderRequired (an empty registry, no consent surface wired).
    adapter = _adapter(_service(tmp_path))
    result = await adapter.execute(_context(tmp_path), {"query": "hello"})
    assert result.error is not None
    assert result.error.code is CoreErrorCode.TOOL_FAILED
    assert _texts_direct(result) == "Unable to search the web for hello"
    assert result.metadata == {}
    # Disabled (the persisted false selection).
    service = _service(tmp_path)
    _two_providers(service)
    await service.select(False)
    adapter2 = _adapter(service)
    result2 = await adapter2.execute(_context(tmp_path), {"query": "hello"})
    assert result2.error is not None
    assert result2.error.code is CoreErrorCode.TOOL_FAILED
    assert _texts_direct(result2) == "Unable to search the web for hello"
    assert result2.metadata == {}


async def test_an_explicit_unknown_provider_is_not_found(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _two_providers(service)
    with pytest.raises(WebSearchProviderNotFoundError):
        await service.query("hello", provider_id="ghost")


# ---------------------------------------------------------------------------
# §4-5: the permission order
# ---------------------------------------------------------------------------


async def test_a_denied_policy_makes_zero_provider_calls(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=_MsClock(), rng=_Rng(0.0))
    alpha, beta = _two_providers(service)
    # The gate policy carries the deny rule for the exact query resource;
    # only the Invoker's gate (the TS permission.assert face) can produce
    # this terminal -- the adapter never runs.
    policy = PermissionPolicy(
        boot_id="boot_websearch_deny",
        rules=(PermissionRule(action="websearch__search", effect="deny", resources=("secret query",)),),
    )
    registry = _registry(_adapter(service), policy=policy)

    outcome = await _invoke(registry, tmp_path, {"query": "secret query"})
    assert outcome.state == "denied"
    assert outcome.effect_status == "not_started"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED
    text = _one_text(outcome)
    assert 'Tool "websearch__search" was denied by the permission policy' in text
    assert "rule for 'websearch__search' -> deny" in text
    assert outcome.structured_output is None
    # The deny runs before any provider contact.
    assert alpha.calls == []
    assert beta.calls == []


# ---------------------------------------------------------------------------
# §4-6: the policy, Spec and registration pins
# ---------------------------------------------------------------------------


def test_websearch_policy_and_spec_pins() -> None:
    assert _DEFAULT_EFFECTS["websearch__search"] == "ask"
    # The git-trio precedent: a per-action default row, not an ask namespace.
    assert "websearch" not in _ASK_NAMESPACES
    # The permission.assert resource binding is the query (the TS anchor face).
    assert extract_resources("websearch__search", {"query": "hello"}) == ("hello",)
    assert extract_resources("websearch__search", {"query": ""}) == ()
    assert extract_resources("websearch__search", {}) == ()
    # The fixed priority of the earlier bindings still wins.
    assert extract_resources("fs__read", {"query": "q", "path": "C:/x"}) == ("C:/x",)

    assert WEBSEARCH_SPEC.namespace == "websearch"
    assert WEBSEARCH_SPEC.name == "search"
    assert WEBSEARCH_SPEC.version == 1
    assert WEBSEARCH_SPEC.side_effects is True
    assert WEBSEARCH_SPEC.idempotency_support is False
    assert WEBSEARCH_SPEC.supported_content_types == ["text"]
    assert WEBSEARCH_INPUT_SCHEMA == {
        "type": "object",
        "description": "Search the web using the user's selected search integration.",
        "properties": {"query": {"type": "string", "description": "Websearch query"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    # The description's two verbatim paragraphs with the dynamic year.
    prefix = (
        "Search the web using the user's selected search integration. "
        "Use this for current information beyond knowledge cutoff.\n\nThe current year is "
    )
    suffix = ". Use this year when searching for recent information or current events."
    assert WEBSEARCH_DESCRIPTION.startswith(prefix)
    assert WEBSEARCH_DESCRIPTION.endswith(suffix)
    year = WEBSEARCH_DESCRIPTION.removeprefix(prefix).removesuffix(suffix)
    assert year.isdigit() and len(year) == 4
    # The verbatim NO_RESULTS sentence.
    assert WEBSEARCH_NO_RESULTS == "No search results found. Please try a different query."
    # The consent deadline: the TS one-minute face.
    assert WEBSEARCH_CONSENT_TIMEOUT_S == 60.0


async def test_the_adapter_registers_and_resolves_by_wire_name(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _two_providers(service)
    registry = _registry(_adapter(service))

    catalog = registry.snapshot(_SESSION_ID)
    spec = catalog.resolve("websearch__search")
    assert spec is not None
    assert spec.namespace == "websearch"
    assert spec.name == "search"


def test_the_builtin_export_surface_carries_the_websearch_faces() -> None:
    for name in (
        "WebSearchAdapter",
        "WEBSEARCH_CONSENT_TIMEOUT_S",
        "WEBSEARCH_DESCRIPTION",
        "WEBSEARCH_INPUT_SCHEMA",
        "WEBSEARCH_NAME",
        "WEBSEARCH_NAMESPACE",
        "WEBSEARCH_NO_RESULTS",
        "WEBSEARCH_SPEC",
        "WEBSEARCH_VERSION",
    ):
        assert name in builtin.__all__


async def test_an_unbound_adapter_is_a_wiring_defect(tmp_path: Path) -> None:
    with pytest.raises(ToolInvokeError):
        await WebSearchAdapter().execute(_context(tmp_path), {"query": "hello"})


async def test_the_input_double_face_rejects_an_empty_query(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _two_providers(service)
    registry = _registry(_adapter(service))

    # The strict schema subset carries no minLength, so an empty string
    # passes the Invoker and the adapter's fail-closed re-check answers.
    text = _failed(await _invoke(registry, tmp_path, {"query": ""}), CoreErrorCode.VALIDATION_ERROR)
    assert text == "query is required and must be a non-empty string"
