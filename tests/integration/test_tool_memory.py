"""Builtin ``memory__*`` integration tests (T-C1-PT-012; plan §4).

The fail-soft contract faces through the public adapter ``execute``,
against a local fake gateway (a real httpx2 transport on loopback -- zero
external network, the fake_openai_server precedent at plain-JSON scale):

* the ok faces -- results render, empty renders, ``next_cursor`` rides the
  structured payload, ``read_scene`` returns the scene document;
* the unavailable faces -- HTTP 500, HTTP 401, a malformed body and a
  refused connection all answer the structured service-unavailable payload
  with the reason token, the NOT-an-empty-result sentence and
  ``retryable=True``, and NEVER raise (plan §4.3-1/2: the empty and
  outage faces stay categorically distinct);
* the unconfigured face -- a None client (no ``memory`` block in config)
  answers unavailable/"unconfigured" (the §4.2 headless state, an
  expected condition, not a wiring defect);
* the wire faces -- the Bearer + X-Service-Id headers arrive, ``top_k`` /
  ``cursor`` ride the request body, and the conversation tool hits its own
  path;
* the pins -- the ``_DEFAULT_EFFECTS`` ask rows (§4.1) and the spec faces
  (the read-only set -- trio + the U-6a enumeration pair; the scene read
  honestly idempotent, the time-varying faces honestly not). The deeper
  loader battery (host-tier/shape) lives in test_config_loader.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject
from opencode.contracts.tools import ToolResult
from opencode.memory import MemoryGatewayClient, MemoryGatewayConfig, memory_config_from_values
from opencode.tools import ToolContext, ToolLimits, builtin
from opencode.tools.policy import _DEFAULT_EFFECTS

# ---------------------------------------------------------------------------
# The fake gateway (loopback, scripted).
# ---------------------------------------------------------------------------


@dataclass
class _Recorded:
    """One served request (headers lower-cased; body raw bytes)."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _FakeGateway:
    """A minimal scripted HTTP/1.1 JSON gateway on 127.0.0.1.

    Responses come off a scripted queue; an empty queue answers the
    default ``200 {"results": []}`` (the empty face). One request per
    connection (``Connection: close``) -- the httpx2 client's per-request
    discipline matches.
    """

    requests: list[_Recorded] = field(default_factory=list)
    _responses: list[tuple[int, bytes]] = field(default_factory=list)
    _server: asyncio.AbstractServer | None = None
    base_url: str = ""

    def script(self, status: int, body: bytes | str | dict[str, Any]) -> None:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self._responses.append((status, body))

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("utf-8", "replace")
            parts = request_line.split(" ")
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            headers: dict[str, str] = {}
            while True:
                line = (await reader.readline()).decode("utf-8", "replace")
                if line in ("\r\n", "\n", ""):
                    break
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            self.requests.append(_Recorded(method, path, headers, body))
            if self._responses:
                status, payload = self._responses.pop(0)
            else:
                status, payload = 200, b'{"results": []}'
            reason = "OK" if status < 400 else "Error"
            head = (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n"
                f"Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


@pytest.fixture
async def gateway() -> AsyncGenerator[_FakeGateway, None]:
    server = _FakeGateway()
    await server.start()
    yield server
    await server.stop()


def _client(server: _FakeGateway) -> MemoryGatewayClient:
    return MemoryGatewayClient(MemoryGatewayConfig(base_url=server.base_url, service_id="svc-test", api_key="key-test"))


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(
        session_id="ses_memtest",
        run_id="run_memtest",
        message_id="msg_memtest",
        tool_call_id="call_memtest",
        working_dir=str(tmp_path),
        limits=ToolLimits(),
        progress=None,
    )


def _payload(result: ToolResult) -> JsonObject:
    assert result.structured_output is not None and isinstance(result.structured_output, dict)
    return result.structured_output


async def _search(server: _FakeGateway, tmp_path: Path, payload: JsonObject) -> ToolResult:
    adapter = builtin.MemorySearchAdapter(_client(server))
    return await adapter.execute(_context(tmp_path), payload)


# ---------------------------------------------------------------------------
# The ok faces.
# ---------------------------------------------------------------------------


async def test_search_ok_renders_results_and_cursor(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(
        200,
        {
            "results": [
                {"id": "m-1", "text": "the user prefers tabs"},
                {"id": "m-2", "text": "project x uses py3.12"},
            ],
            "next_cursor": "cur-2",
        },
    )
    result = await _search(gateway, tmp_path, {"query": "editor preferences"})
    assert _payload(result) == {
        "status": "ok",
        "results": [
            {"id": "m-1", "text": "the user prefers tabs"},
            {"id": "m-2", "text": "project x uses py3.12"},
        ],
        "next_cursor": "cur-2",
    }
    assert result.metadata == {"availability": "ok"}
    assert result.error is None
    text = result.content[0].text if hasattr(result.content[0], "text") else ""
    assert "2 memory result(s)" in text
    assert "the user prefers tabs" in text


async def test_search_ok_empty_is_not_unavailable(gateway: _FakeGateway, tmp_path: Path) -> None:
    result = await _search(gateway, tmp_path, {"query": "nothing matches"})
    assert _payload(result) == {"status": "ok", "results": []}
    assert result.metadata == {"availability": "ok"}
    assert result.error is None
    text = result.content[0].text if hasattr(result.content[0], "text") else ""
    assert builtin.MEMORY_NO_RESULTS in text
    assert "unavailable" not in text.lower()


async def test_conversation_search_hits_its_own_path(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"results": [{"scene_id": "sc-9", "summary": "the deploy talk"}]})
    adapter = builtin.MemoryConversationSearchAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"query": "deploy"})
    assert _payload(result)["status"] == "ok"
    assert gateway.requests[0].path == "/v3/memory/conversation/search"
    assert gateway.requests[0].method == "POST"


async def test_read_scene_returns_the_document(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"scene_id": "sc-1", "messages": [{"role": "user", "text": "hi"}]})
    adapter = builtin.MemoryReadSceneAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"scene_id": "sc-1"})
    assert _payload(result) == {
        "status": "ok",
        "scene": {"scene_id": "sc-1", "messages": [{"role": "user", "text": "hi"}]},
    }
    recorded = gateway.requests[0]
    assert recorded.method == "GET"
    assert recorded.path == "/v3/memory/scene/sc-1"


async def test_list_spaces_enumerates_and_rides_the_cursor(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"results": [{"space_id": "sp-1", "title": "Work"}], "next_cursor": "pg-2"})
    adapter = builtin.MemoryListSpacesAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"cursor": "pg-1"})
    assert _payload(result) == {
        "status": "ok",
        "results": [{"space_id": "sp-1", "title": "Work"}],
        "next_cursor": "pg-2",
    }
    assert result.metadata == {"availability": "ok"}
    recorded = gateway.requests[0]
    assert recorded.method == "GET"
    assert recorded.path == "/v3/memory/spaces?cursor=pg-1"


async def test_list_scenes_scopes_to_one_space(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"results": [{"scene_id": "sc-1", "title": "the deploy talk"}]})
    adapter = builtin.MemoryListScenesAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"space_id": "sp-1"})
    assert _payload(result)["status"] == "ok"
    text = result.content[0].text if hasattr(result.content[0], "text") else ""
    assert "1 memory scene(s)" in text
    recorded = gateway.requests[0]
    assert recorded.method == "GET"
    assert recorded.path == "/v3/memory/scenes?space_id=sp-1"


async def test_the_enumeration_empty_faces_are_distinct(gateway: _FakeGateway, tmp_path: Path) -> None:
    # The empty-queue default answers 200 {"results": []}: both enumeration
    # faces render their OWN noun-explicit sentence, never the unavailable
    # face (§4.3-2: no folding, one sentence per face).
    spaces = await builtin.MemoryListSpacesAdapter(_client(gateway)).execute(_context(tmp_path), {})
    scenes = await builtin.MemoryListScenesAdapter(_client(gateway)).execute(_context(tmp_path), {})
    for result, sentence in ((spaces, builtin.MEMORY_NO_SPACES), (scenes, builtin.MEMORY_NO_SCENES)):
        assert _payload(result) == {"status": "ok", "results": []}
        assert result.metadata == {"availability": "ok"}
        assert result.error is None
        text = result.content[0].text if hasattr(result.content[0], "text") else ""
        assert sentence in text
        assert "unavailable" not in text.lower()


async def test_read_scene_accepts_a_continuation_cursor(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"scene_id": "sc-1", "messages": [], "next_cursor": "cur-9"})
    adapter = builtin.MemoryReadSceneAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"scene_id": "sc-1", "cursor": "cur-8"})
    assert _payload(result)["status"] == "ok"
    assert gateway.requests[0].path == "/v3/memory/scene/sc-1?cursor=cur-8"


async def test_list_spaces_500_answers_the_outage_payload(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(500, "down")
    adapter = builtin.MemoryListSpacesAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {})
    _assert_unavailable(result, "http_5xx")


# ---------------------------------------------------------------------------
# The wire faces.
# ---------------------------------------------------------------------------


async def test_the_auth_headers_and_body_ride_the_wire(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, {"results": []})
    await _search(gateway, tmp_path, {"query": "q", "top_k": 5, "cursor": "cur-1"})
    recorded = gateway.requests[0]
    assert recorded.headers["authorization"] == "Bearer key-test"
    assert recorded.headers["x-service-id"] == "svc-test"
    body = json.loads(recorded.body)
    assert body == {"query": "q", "top_k": 5, "cursor": "cur-1"}


# ---------------------------------------------------------------------------
# The unavailable faces (§4.3-1/2: never a raise, never the empty face).
# ---------------------------------------------------------------------------


def _assert_unavailable(result: ToolResult, reason: str) -> None:
    assert _payload(result) == {"status": "unavailable", "reason": reason}
    assert result.metadata == {"availability": "unavailable"}
    assert result.error is not None
    assert result.error.retryable is True
    text = result.content[0].text if hasattr(result.content[0], "text") else ""
    assert "NOT an empty result" in text
    assert builtin.MEMORY_NO_RESULTS not in text


async def test_http_500_answers_the_outage_payload(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(500, {"error": "boom"})
    result = await _search(gateway, tmp_path, {"query": "q"})
    _assert_unavailable(result, "http_5xx")


async def test_http_401_answers_the_auth_reason(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(401, "unauthorized")
    result = await _search(gateway, tmp_path, {"query": "q"})
    _assert_unavailable(result, "auth")


async def test_a_malformed_body_answers_malformed(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(200, "not-json")
    result = await _search(gateway, tmp_path, {"query": "q"})
    _assert_unavailable(result, "malformed")


async def test_a_dead_endpoint_answers_the_transport_bucket(tmp_path: Path) -> None:
    # A client pointed at a dead port (nothing listens): the transport
    # bucket -- httpx2 maps a refused connect to its timeout identity on
    # this platform -- never a raise; the §4.3 contract only pins the
    # empty-vs-outage distinction, not the token split.
    dead = MemoryGatewayClient(
        MemoryGatewayConfig(base_url="http://127.0.0.1:9", service_id="svc", api_key="key"),
        timeout_s=2.0,
    )
    adapter = builtin.MemorySearchAdapter(dead)
    result = await adapter.execute(_context(tmp_path), {"query": "q"})
    assert isinstance(result.structured_output, dict)
    assert result.structured_output["status"] == "unavailable"
    assert result.structured_output["reason"] in {"network", "timeout"}
    assert result.metadata == {"availability": "unavailable"}


@pytest.mark.parametrize(
    "adapter_factory",
    [
        builtin.MemorySearchAdapter,
        builtin.MemoryConversationSearchAdapter,
        builtin.MemoryReadSceneAdapter,
        builtin.MemoryListSpacesAdapter,
        builtin.MemoryListScenesAdapter,
    ],
)
async def test_an_unconfigured_client_is_the_fail_soft_face(adapter_factory: type, tmp_path: Path) -> None:
    adapter = adapter_factory(None)
    if adapter_factory is builtin.MemoryReadSceneAdapter:
        payload: JsonObject = {"scene_id": "s"}
    elif adapter_factory in (builtin.MemoryListSpacesAdapter, builtin.MemoryListScenesAdapter):
        payload = {}
    else:
        payload = {"query": "q"}
    result = await adapter.execute(_context(tmp_path), payload)
    _assert_unavailable(result, "unconfigured")


async def test_read_scene_500_answers_the_outage_payload(gateway: _FakeGateway, tmp_path: Path) -> None:
    gateway.script(500, "down")
    adapter = builtin.MemoryReadSceneAdapter(_client(gateway))
    result = await adapter.execute(_context(tmp_path), {"scene_id": "sc-1"})
    _assert_unavailable(result, "http_5xx")


async def test_a_missing_query_is_the_validation_double_face(gateway: _FakeGateway, tmp_path: Path) -> None:
    result = await _search(gateway, tmp_path, {"query": ""})
    assert result.error is not None
    assert result.error.code is CoreErrorCode.VALIDATION_ERROR
    assert gateway.requests == []


# ---------------------------------------------------------------------------
# The pins (§4.1 ask default; the read-only spec faces).
# ---------------------------------------------------------------------------


def test_policy_defaults_pin_the_memory_tools_to_ask() -> None:
    assert _DEFAULT_EFFECTS["memory__search"] == "ask"
    assert _DEFAULT_EFFECTS["memory__conversation_search"] == "ask"
    assert _DEFAULT_EFFECTS["memory__read_scene"] == "ask"
    assert _DEFAULT_EFFECTS["memory__list_spaces"] == "ask"
    assert _DEFAULT_EFFECTS["memory__list_scenes"] == "ask"


def test_the_specs_pin_the_readonly_memory_tools() -> None:
    for spec in (
        builtin.MEMORY_SEARCH_SPEC,
        builtin.MEMORY_CONVERSATION_SEARCH_SPEC,
        builtin.MEMORY_READ_SCENE_SPEC,
        builtin.MEMORY_LIST_SPACES_SPEC,
        builtin.MEMORY_LIST_SCENES_SPEC,
    ):
        assert spec.namespace == "memory"
        assert spec.supported_content_types == ["text"]
        assert spec.side_effects is False
    assert builtin.MEMORY_SEARCH_SPEC.name == "search"
    assert builtin.MEMORY_CONVERSATION_SEARCH_SPEC.name == "conversation_search"
    assert builtin.MEMORY_READ_SCENE_SPEC.name == "read_scene"
    assert builtin.MEMORY_LIST_SPACES_SPEC.name == "list_spaces"
    assert builtin.MEMORY_LIST_SCENES_SPEC.name == "list_scenes"
    # Honest idempotency: a stable scene document vs time-varying faces
    # (recall and the enumeration pair alike).
    assert builtin.MEMORY_READ_SCENE_SPEC.idempotency_support is True
    assert builtin.MEMORY_SEARCH_SPEC.idempotency_support is False
    assert builtin.MEMORY_CONVERSATION_SEARCH_SPEC.idempotency_support is False
    assert builtin.MEMORY_LIST_SPACES_SPEC.idempotency_support is False
    assert builtin.MEMORY_LIST_SCENES_SPEC.idempotency_support is False


# ---------------------------------------------------------------------------
# The config resolution face (the loader battery lives in test_config_loader).
# ---------------------------------------------------------------------------


def test_memory_config_from_values_resolves_the_complete_block() -> None:
    values: dict[str, Any] = {"memory": {"base_url": "https://mem.example", "service_id": "svc", "api_key": "k"}}
    config = memory_config_from_values(values)
    assert config is not None
    assert config.base_url == "https://mem.example"
    assert config.service_id == "svc"
    assert config.api_key == "k"


@pytest.mark.parametrize(
    "block",
    [
        None,  # absent
        {},  # empty
        {"base_url": "https://mem.example"},  # incomplete
        {"base_url": "https://mem.example", "service_id": "", "api_key": "k"},  # blank value
        {"base_url": 7, "service_id": "svc", "api_key": "k"},  # wrong type
    ],
)
def test_memory_config_from_reads_incomplete_blocks_as_unconfigured(block: dict[str, Any] | None) -> None:
    values: dict[str, Any] = {} if block is None else {"memory": block}
    assert memory_config_from_values(values) is None
