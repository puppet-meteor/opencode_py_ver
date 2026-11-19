"""The memory gateway service (T-C1-PT-012; plan ``doc/plans/memory-longterm-plan.md`` §4).

The read-only recall service behind the ``memory__*`` tools: a thin async
REST client for the Agent-Memory gateway (the CSS L0/L1 recall tiers of the
plan's §2 read path). The plan's §4.1 names the official Python SDK
``tencentdb_agent_memory``; that package does not exist on PyPI (probed
2026-11-19: ``pip index versions tencentdb-agent-memory`` -> No matching
distribution -- the referenced TencentDB-Agent-Memory ships a Node service,
not a Python package). This module is the recorded deviation: a
self-implemented lightweight client over httpx2 holding the plan's own
contract anchors -- v2/v3 REST, Bearer auth, serviceId isolation. The wire
shape below (paths, ``results``/``next_cursor`` keys) is therefore THIS
repo's contract; aligning with the real gateway later touches only this
module.

Error taxonomy (plan §4.3, the pinned fail-soft contract): every transport
failure -- timeout, network, HTTP 5xx, auth rejection, malformed body --
raises ONE tagged identity, :class:`MemoryUnavailableError`, carrying a
short reason token. An empty result set is NOT an error: the two faces stay
distinguishable end-to-end (the tool adapters render them as different
structured payloads; folding unavailable into "no memories found" is
forbidden, §4.3-2). The audit distinction (empty-result vs
service-unavailable, §4.3-3) rides the adapters' ``metadata.availability``
face. Retry policy (counts/backoff/circuit-breaker) is S-7 spike territory
and deliberately absent here (§4.3-4): one attempt per call, no hidden
retries.

The HTTP client is per-request (``async with``): memory calls are
low-frequency, and a fresh client per call keeps this service free of any
close/lifecycle burden on the host compositions (the webfetch precedent).
``trust_env=False`` pins the same no-proxy discipline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx2

from opencode.contracts.messages import JsonObject, JsonValue

#: The config block's known sub-keys (loader-enforced shape; see
#: :data:`opencode.config.loader.HOST_ONLY_KEYS` -- ``memory`` is host-tier
#: trust exactly like ``api_key``).
MEMORY_CONFIG_KEYS: frozenset[str] = frozenset({"base_url", "service_id", "api_key"})

#: One attempt's budget. Generous for a recall endpoint (the plan's S-7
#: spike will calibrate; until then this is a plain constant, not a knob).
MEMORY_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class MemoryGatewayConfig:
    """The resolved ``memory`` config block (all three keys non-empty)."""

    base_url: str
    service_id: str
    api_key: str


class MemoryUnavailableError(Exception):
    """A transport-level failure talking to the memory gateway (§4.3-1).

    ``reason`` is a short stable token -- ``timeout`` / ``network`` /
    ``auth`` / ``http_5xx`` / ``http_error`` / ``malformed`` -- never a
    raw body echo. The tool adapters translate this identity into the
    structured service-unavailable payload; nothing here decides policy.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"memory gateway unavailable: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class MemorySearchPage:
    """One search response: the result objects plus the optional cursor.

    ``results`` empty is the EMPTY face (a successful query with nothing to
    recall) -- categorically distinct from :class:`MemoryUnavailableError`.
    """

    results: tuple[JsonObject, ...]
    next_cursor: str | None


def memory_config_from_values(values: Mapping[str, object]) -> MemoryGatewayConfig | None:
    """Resolve the ``memory`` block from merged config values.

    None (the unconfigured face) when the block is absent, empty, or any of
    the three keys is missing/not a non-empty string -- an incomplete block
    reads as unconfigured, not as an error (the loader validates shape at
    load time; this lenient face only serves in-process callers like the
    test bridge). Values are never echoed by this module's error paths.
    """
    block = values.get("memory")
    if not isinstance(block, Mapping):
        return None
    base_url = block.get("base_url")
    service_id = block.get("service_id")
    api_key = block.get("api_key")
    if not (
        isinstance(base_url, str)
        and base_url
        and isinstance(service_id, str)
        and service_id
        and isinstance(api_key, str)
        and api_key
    ):
        return None
    return MemoryGatewayConfig(base_url=base_url, service_id=service_id, api_key=api_key)


def _page_from_payload(payload: JsonValue) -> MemorySearchPage:
    """Validate the search wire shape; anything unexpected is malformed."""
    if not isinstance(payload, dict):
        raise MemoryUnavailableError("malformed") from None
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise MemoryUnavailableError("malformed") from None
    results = tuple(item for item in raw_results if isinstance(item, dict))
    raw_cursor = payload.get("next_cursor")
    cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
    return MemorySearchPage(results=results, next_cursor=cursor)


class MemoryGatewayClient:
    """The read-only gateway client (§4.1 trio + the U-6a enumeration pair).

    Stateful in nothing: every method opens one short-lived
    ``httpx2.AsyncClient`` pinned to the block's auth headers (Bearer token
    + the serviceId isolation header). Hosts construct it from the resolved
    config (or leave the adapters unconfigured -- the fail-soft face).
    """

    def __init__(self, config: MemoryGatewayConfig, *, timeout_s: float = MEMORY_TIMEOUT_S) -> None:
        self._config = config
        self._timeout_s = timeout_s

    @property
    def config(self) -> MemoryGatewayConfig:
        return self._config

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        params: Mapping[str, str] | None = None,
    ) -> JsonValue:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "X-Service-Id": self._config.service_id,
            "Accept": "application/json",
        }
        try:
            async with httpx2.AsyncClient(
                base_url=self._config.base_url,
                headers=headers,
                timeout=httpx2.Timeout(self._timeout_s),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, json=json_body, params=params)
        except httpx2.TimeoutException:
            raise MemoryUnavailableError("timeout") from None
        except httpx2.HTTPError:
            # Transport-level failure the status code never reached (DNS,
            # refused connection, reset, TLS) -- one bucket, §4.3-1.
            raise MemoryUnavailableError("network") from None
        status = response.status_code
        if status in (401, 403):
            raise MemoryUnavailableError("auth") from None
        if status >= 500:
            raise MemoryUnavailableError("http_5xx") from None
        if status >= 400:
            raise MemoryUnavailableError("http_error") from None
        try:
            return response.json()
        except ValueError:
            raise MemoryUnavailableError("malformed") from None

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        """``memory__search``: cross-tier semantic recall (L1-forward fusion)."""
        body: JsonObject = {"query": query}
        if top_k is not None:
            body["top_k"] = top_k
        if cursor is not None:
            body["cursor"] = cursor
        return _page_from_payload(await self._request("POST", "/v3/memory/search", json_body=body))

    async def conversation_search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        """``memory__conversation_search``: session/scene-scoped recall (L0)."""
        body: JsonObject = {"query": query}
        if top_k is not None:
            body["top_k"] = top_k
        if cursor is not None:
            body["cursor"] = cursor
        return _page_from_payload(await self._request("POST", "/v3/memory/conversation/search", json_body=body))

    async def read_scene(self, scene_id: str, *, cursor: str | None = None) -> JsonObject:
        """``memory__read_scene``: one scene's full context (L2/L3 by reference).

        The U-6a ruling's face: the full scene document, with an optional
        continuation cursor when the gateway paginates a long scene.
        """
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = cursor
        payload = await self._request(
            "GET",
            f"/v3/memory/scene/{quote(scene_id, safe='')}",
            params=params or None,
        )
        if not isinstance(payload, dict):
            raise MemoryUnavailableError("malformed") from None
        return payload

    async def list_spaces(self, *, cursor: str | None = None) -> MemorySearchPage:
        """``memory__list_spaces``: enumerate the service's memory spaces.

        The U-6a ruling's enumeration primitive: the spaces behind the
        serviceId/spaceId isolation boundary (plan §2.3).
        """
        params: dict[str, str] = {}
        if cursor is not None:
            params["cursor"] = cursor
        return _page_from_payload(await self._request("GET", "/v3/memory/spaces", params=params or None))

    async def list_scenes(
        self,
        *,
        space_id: str | None = None,
        cursor: str | None = None,
    ) -> MemorySearchPage:
        """``memory__list_scenes``: enumerate scenes (optionally one space's)."""
        params: dict[str, str] = {}
        if space_id is not None:
            params["space_id"] = space_id
        if cursor is not None:
            params["cursor"] = cursor
        return _page_from_payload(await self._request("GET", "/v3/memory/scenes", params=params or None))

    async def write(
        self,
        content: str,
        *,
        space_id: str | None = None,
        scene_id: str | None = None,
    ) -> JsonObject:
        """``memory__write``: store one explicit memory entry (U-5(a) Day-1).

        The write path's own wire (this repo's contract like the read
        faces): POST /v3/memory/write with the content plus the optional
        space/scene scoping; the response carries the stored entry's id
        when the gateway assigns one.
        """
        body: JsonObject = {"content": content}
        if space_id is not None:
            body["space_id"] = space_id
        if scene_id is not None:
            body["scene_id"] = scene_id
        payload = await self._request("POST", "/v3/memory/write", json_body=body)
        if not isinstance(payload, dict):
            raise MemoryUnavailableError("malformed") from None
        return payload
