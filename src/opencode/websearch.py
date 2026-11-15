"""The websearch service (P7-S4; T-C1-103, ticket §3-2/§3-4/§3-6).

The provider seam behind the ``websearch__search`` tool, adapted from TS
``packages/core/src/websearch.ts``: a provider registry, the KV-persisted
selection (``websearch:provider``; a provider ID | ``"random"`` | ``False``),
per-session rotation affinity, the 429 cooldown ring and the tagged error
quad. The consent form itself lives in the tool adapter
(:mod:`opencode.tools.builtin.websearch`) exactly like the TS split (the
core ``websearch.ts`` service vs the ``tool/plugin/websearch.ts`` form,
render and error map).

TS mapping / deviations, quantified (progress file):

- KV landing: the TS selection rides ``KV.Service`` (a sqlite ``kv`` table);
  PY lands it as ONE JSON document in the App's data directory -- the same
  single-writer domain the sqlite database lives in (the OS writer lock
  already serializes writers) -- behind a get/set/remove-by-key face with an
  atomic temp-file + ``os.replace`` store. The sqlite-table shape was
  rejected: the migration head and table-set census are pinned
  (``test_migrations.py`` pins version ``0004``) and the ticket's §3-8
  contract expected zero schema faces. DIFFERENT-BY-DESIGN.
- Zero builtin providers: the TS providers are plugin-ecosystem faces, not
  core; the PY delivery is the SEAM (registry + selection + rotation +
  cooldown) with hosts and tests registering their own.
  DIFFERENT-BY-DESIGN (the TS plugin ecosystem is not a porting face).
- The TS config face (``config/plugin/websearch.ts``: a ConfigEntryObserver
  hot-reloading a ``websearch`` config entry onto an in-memory selection
  tier that shadows the KV) has no PY counterpart -- the config snapshot
  schema is pinned -- so the shadow tier is absent and ``selection()``
  reads the KV only. N/A (progress file ⑤-adjacent).
- The TS bus subscription (Session Deleted/Moved -> drop the session's
  affinity cell) has no PY wiring owner; the cleanup face is
  :meth:`WebSearchService.forget_session` and the host wiring is deferred
  (the map is boot-scoped in-memory state; ``py.session.deleted`` exists but
  subscribing the service would add a lifecycle the ticket's §3-8 zero-event
  face does not ask for). PY also has no session-moved event, so the Moved
  leg is N/A by absence. Recorded in the progress file.
- The TS ``websearch.updated`` bus event (the State notify face) is zero by
  the ticket §3-8 contract; ``providers()`` recomputes on every call.
- ``query`` is the TS loop shape-for-shape: require/default resolution, the
  affinity cell taken (and re-inserted only under ``random``) BEFORE
  rotation, the attempted-set ring with 429-only rotation under ``random``
  selection, and the exhausted-ring fallback to the last recorded cooldown
  error. ``cooldownMillis`` keeps the TS semantics verbatim (missing/blank
  or unparsable ``retry-after`` = 60 s; non-negative seconds scale to ms;
  negative seconds and unparsable dates fall back to 60 s; a parseable date
  clamps to the remaining delta).
- An HTTP-carrying provider failure is recognized structurally (an
  exception with an httpx2-shaped ``response`` attribute), the PY analog of
  ``HttpClientError.isHttpClientError``: :func:`http_status` feeds both the
  cooldown's 429 test and the tool face's error-message table, and
  :func:`retry_after` feeds the cooldown window. Providers raising anything
  else stay statusless (the fallback sentence face).
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal, Protocol, TypeGuard, cast

from opencode.contracts.messages import JsonValue

#: websearch.ts ``ProviderKey``: the KV key of the persisted selection.
PROVIDER_KEY = "websearch:provider"

#: The KV document's file name in the App's data directory (the sqlite
#: ``kv`` table's PY landing, see the module docstring).
KV_FILE_NAME = "kv.json"

#: websearch.ts ``Selection``: a provider ID, the literal ``"random"``, or
#: ``False`` (web search disabled). ``"random"`` is itself a string, so the
#: static face is ``str | False``; the three-value domain stays documented.
WebSearchSelection = str | Literal[False]


class WebSearchError(Exception):
    """Base of the four tagged service faces (websearch.ts ``Error`` union)."""


class WebSearchProviderRequiredError(WebSearchError):
    """``WebSearch.ProviderRequired``: no provider resolves for the query."""

    def __init__(self) -> None:
        super().__init__("WebSearch.ProviderRequired")


class WebSearchProviderNotFoundError(WebSearchError):
    """``WebSearch.ProviderNotFound``: an explicitly requested provider id is
    not registered (its own tagged face, not a ProviderRequired subclass --
    the TS union keeps the two tags apart)."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"WebSearch.ProviderNotFound: {provider_id}")
        self.provider_id = provider_id


class WebSearchDisabledError(WebSearchError):
    """``WebSearch.Disabled``: the persisted selection is ``False``."""

    def __init__(self) -> None:
        super().__init__("WebSearch.Disabled")


class WebSearchRequestError(WebSearchError):
    """``WebSearch.Request``: a provider execution failure, tagged with the
    provider id and the underlying cause (the TS ``{providerID, cause}``
    fields)."""

    def __init__(self, provider_id: str, cause: BaseException) -> None:
        super().__init__(f"WebSearch.Request: {provider_id}: {cause}")
        self.provider_id = provider_id
        self.cause = cause


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """schema/websearch.ts ``Result``: ``url`` required; ``title`` /
    ``content`` / ``published_ms`` optional (``None`` = the TS absent key;
    ``published_ms`` is the ``time.published`` milliseconds-since-epoch
    face, a ``Schema.Finite`` number)."""

    url: str
    title: str | None = None
    content: str | None = None
    published_ms: float | None = None


@dataclass(frozen=True, slots=True)
class WebSearchProviderInfo:
    """schema/websearch.ts ``Provider``: the wire shape ``{id, name}``."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    """schema/websearch.ts ``Response``: the winning provider id + results."""

    provider_id: str
    results: tuple[WebSearchResult, ...]


class WebSearchProvider(Protocol):
    """websearch.ts ``ProviderImplementation``: the host-registered seam.

    ``execute`` runs one query and returns zero or more results; ANY
    exception it raises is wrapped into
    :class:`WebSearchRequestError` by :meth:`WebSearchService.query` (the TS
    ``Effect.result`` face) and can carry an HTTP status for the cooldown /
    error-table faces through an httpx2-shaped ``response`` attribute.
    """

    id: str
    name: str

    async def execute(self, query: str) -> Sequence[WebSearchResult]: ...


@dataclass(slots=True)
class _Affinity:
    """One session's rotation cell (websearch.ts ``preferred`` value)."""

    provider: str | None = None


@dataclass(slots=True)
class _Cooldown:
    """One provider's 429 window: the expiry instant + the recorded error."""

    until: int
    error: WebSearchRequestError


def http_status(cause: BaseException) -> int | None:
    """The TS ``HttpClientError.isHttpClientError(cause) ?
    cause.response?.status : undefined`` face: an exception carrying an
    httpx2-shaped ``response`` reports its status; every other cause is
    statusless."""
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def retry_after(cause: BaseException) -> str | None:
    """The ``retry-after`` header value of an HTTP-carrying cause, if any."""
    response = getattr(cause, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:  # a duck-typed carrier without a mapping face
        return None
    return value if isinstance(value, str) else None


def _epoch_ms_now() -> int:
    return int(time.time() * 1000)


def _parse_date_ms(value: str) -> float | None:
    """The JS ``Date.parse`` face: an RFC 1123 HTTP-date (the ``retry-after``
    wire form) or an ISO-8601 timestamp, in epoch milliseconds; naive
    values read as UTC."""
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000


def cooldown_millis(value: str | None, now: int) -> int:
    """websearch.ts ``cooldownMillis``, verbatim semantics: a missing/blank
    value is 60 s; a finite numeric seconds value scales to milliseconds
    when non-negative (negative falls back to 60 s); a parseable date clamps
    to the remaining delta; anything else is 60 s."""
    if value is None or not value.strip():
        return 60_000
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if math.isfinite(seconds):
        return int(seconds * 1000) if seconds >= 0 else 60_000
    delta = _parse_date_ms(value)
    if delta is None:
        return 60_000
    return max(0, int(delta - now))


def _is_valid_selection(value: JsonValue) -> TypeGuard[WebSearchSelection]:
    """The ``Selection`` decode face: any string (a provider id, or the
    literal ``"random"``) or exactly ``False``; every other stored value is
    corruption."""
    if isinstance(value, bool):
        return value is False
    return isinstance(value, str)


class _JsonFileKV:
    """The KV landing (ticket §3-2): one JSON document in the data
    directory, one key per top-level member.

    The face mirrors the TS ``KV.Service`` seam (get/set/remove by key); the
    store is an atomic same-directory temp file + ``os.replace`` (the data
    directory's OS writer lock already serializes writers). A file that
    fails to decode reads as the empty store (the corrupt-file face); the
    per-key invalid-value hygiene removal lives in the caller
    (:meth:`WebSearchService._selection`), like the TS decode-None arm. An
    unreadable file (any other OSError) propagates so the tool face can wrap
    it into the structured fail-closed sentence (ticket §8).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, JsonValue]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except ValueError:  # undecodable bytes: the corrupt-file face
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return cast("dict[str, JsonValue]", parsed)

    def get(self, key: str) -> JsonValue | None:
        """The stored value, or None when the key is absent (a stored JSON
        ``null`` also reads as None: ``select`` never stores null, so a
        null is hand corruption reading as no-selection)."""
        return self._load().get(key)

    def set(self, key: str, value: JsonValue) -> None:
        data = self._load()
        data[key] = value
        self._store(data)

    def remove(self, key: str) -> None:
        data = self._load()
        if key in data:
            del data[key]
            self._store(data)

    def _store(self, data: dict[str, JsonValue]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(f"{self._path.name}.tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, self._path)


class WebSearchService:
    """websearch.ts ``WebSearch.Service``: the provider registry, the
    KV-persisted selection, the session-affinity rotation table and the 429
    cooldown ring behind the tool's queries.

    ``clock`` (epoch milliseconds, the TS ``Clock.currentTimeMillis`` face)
    and ``rng`` (``[0, 1)``, the TS ``Math.random`` face) are injectable
    test seams. Boot-scoped in-memory state (registry, cooldowns, affinity)
    plus the restart-stable KV selection (ticket §8).
    """

    def __init__(
        self,
        kv_path: str | Path,
        *,
        clock: Callable[[], int] | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._kv = _JsonFileKV(Path(kv_path))
        self._providers: dict[str, WebSearchProvider] = {}
        self._cooldowns: dict[str, _Cooldown] = {}
        self._preferred: dict[str | None, _Affinity] = {}
        self._clock = clock if clock is not None else _epoch_ms_now
        self._rng = rng if rng is not None else random.random

    # -- registry faces -----------------------------------------------------

    def register_provider(self, provider: WebSearchProvider) -> None:
        """websearch.ts ``transform`` ``add``: registry set-by-id (a re-registered
        id overwrites, the Map.set semantics; zero builtin providers --
        hosts/tests register their own)."""
        self._providers[provider.id] = provider

    def providers(self) -> list[WebSearchProviderInfo]:
        """The ``{id, name}`` face, name-sorted (the TS ``localeCompare``
        order; PY orders by code point -- identical for ASCII names)."""
        return sorted(
            (WebSearchProviderInfo(id=provider.id, name=provider.name) for provider in self._providers.values()),
            key=lambda info: info.name,
        )

    # -- selection faces ----------------------------------------------------

    async def _selection(self) -> WebSearchSelection | None:
        """websearch.ts ``selection``: the persisted selection, with the TS
        decode-None hygiene arm -- a stored value outside the Selection
        domain is removed and reads as unselected."""
        stored = self._kv.get(PROVIDER_KEY)
        if stored is None:
            return None
        if not _is_valid_selection(stored):
            self._kv.remove(PROVIDER_KEY)
            return None
        return stored

    def _default_provider(self, choice: WebSearchSelection | None) -> WebSearchProvider | None:
        """websearch.ts ``defaultProvider``: ``False`` fails as Disabled;
        ``"random"`` inspects the global affinity cell (session id None)
        and falls back to the FIRST-INSERTED provider -- inspection must
        not select a provider or reopen consent when every provider is
        cooling down; an id resolves or is dangling (None)."""
        if choice is False:
            raise WebSearchDisabledError()
        if choice == "random":
            active = self._preferred.get(None)
            active_id = active.provider if active is not None else None
            current = self._providers.get(active_id) if active_id is not None else None
            if current is not None:
                return current
            return next(iter(self._providers.values()), None)
        if choice is not None:
            return self._providers.get(choice)
        return None

    async def default(self) -> WebSearchProviderInfo | None:
        """websearch.ts ``defaultInfo``: the resolved default's ``{id, name}``
        face, ``None`` when unresolved; raises :class:`WebSearchDisabledError`
        when the selection is ``False``."""
        provider = self._default_provider(await self._selection())
        if provider is None:
            return None
        return WebSearchProviderInfo(id=provider.id, name=provider.name)

    async def select(self, selection: WebSearchSelection) -> None:
        """websearch.ts ``select``: persist the selection
        (``kv.set(ProviderKey, selection)``)."""
        self._kv.set(PROVIDER_KEY, selection)

    def forget_session(self, session_id: str) -> None:
        """The affinity-cell cleanup face (the TS bus subscription on Session
        Deleted/Moved; the PY host wiring is deferred -- progress file)."""
        self._preferred.pop(session_id, None)

    # -- the query loop -----------------------------------------------------

    def _require_provider(self, provider_id: str) -> WebSearchProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise WebSearchProviderNotFoundError(provider_id)
        return provider

    def _random_provider(
        self,
        now: int,
        affinity: _Affinity,
        attempted: set[str] | None = None,
    ) -> WebSearchProvider | None:
        """websearch.ts ``randomProvider``: expired cooldowns and unregistered
        ids are dropped, the affinity provider wins when usable, otherwise
        one of the remaining providers is drawn and becomes the new
        affinity."""
        for provider_id in list(self._cooldowns):
            cooldown = self._cooldowns[provider_id]
            if cooldown.until <= now or provider_id not in self._providers:
                del self._cooldowns[provider_id]
        current = self._providers.get(affinity.provider) if affinity.provider is not None else None
        if (
            current is not None
            and current.id not in self._cooldowns
            and (attempted is None or current.id not in attempted)
        ):
            return current
        available = [
            provider
            for provider in self._providers.values()
            if provider.id not in self._cooldowns and (attempted is None or provider.id not in attempted)
        ]
        provider = available[math.floor(self._rng() * len(available))] if available else None
        if provider is not None:
            affinity.provider = provider.id
        return provider

    async def query(
        self,
        query: str,
        *,
        session_id: str | None = None,
        provider_id: str | None = None,
        on_provider: Callable[[WebSearchProviderInfo], Awaitable[None]] | None = None,
    ) -> WebSearchResponse:
        """websearch.ts ``query``, shape-for-shape.

        ``provider_id`` pins the query to one registry id (no rotation, no
        cooldown reads -- an explicit selection fails closed on its own
        errors); without it the persisted selection decides (``random``
        runs the rotation + cooldown ring; a resolved id runs that provider;
        nothing resolved raises ProviderRequired for the tool's consent
        flow). ``on_provider`` is the TS progress face: called with the
        ``{id, name}`` of every provider ATTEMPT (the tool wires it to the
        ToolContext progress seam, publishing ``{"provider": id}``).
        """
        choice = None if provider_id is not None else await self._selection()
        provider: WebSearchProvider | None
        if provider_id is not None:
            provider = self._require_provider(provider_id)
        else:
            provider = self._default_provider(choice)
        if provider is None:
            raise WebSearchProviderRequiredError()
        # Keep the cell for this query so deletion/movement cannot reinsert an in-flight session's entry.
        affinity = self._preferred.get(session_id)
        if affinity is None:
            affinity = _Affinity()
        if choice == "random":
            self._preferred[session_id] = affinity
            rotated = self._random_provider(self._clock(), affinity)
            if rotated is not None:
                provider = rotated
        attempted: set[str] = set()
        while True:
            if on_provider is not None:
                await on_provider(WebSearchProviderInfo(id=provider.id, name=provider.name))
            cooldown = self._cooldowns.get(provider.id) if choice == "random" else None
            if cooldown is None or cooldown.until <= self._clock():
                attempted.add(provider.id)
                try:
                    results = tuple(await provider.execute(query))
                except Exception as cause:
                    error = WebSearchRequestError(provider_id=provider.id, cause=cause)
                    if choice != "random" or http_status(cause) != 429:
                        raise error from cause
                    now = self._clock()
                    cooldown = _Cooldown(until=now + cooldown_millis(retry_after(cause), now), error=error)
                    self._cooldowns[provider.id] = cooldown
                else:
                    return WebSearchResponse(provider_id=provider.id, results=results)
            # Only reachable with choice == "random" (every non-random leg
            # returned above), where cooldown is set: either the skipped
            # live cooldown or the one just recorded for the 429.
            assert cooldown is not None
            provider = self._random_provider(self._clock(), affinity, attempted)
            if provider is None:
                raise cooldown.error
