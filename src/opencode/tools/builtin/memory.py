"""The memory builtin tools (T-C1-PT-012; plan §4.1 read-only trio).

Three adapters over :mod:`opencode.memory`'s gateway client, one module
(the git.py one-module-many-adapters precedent):

- ``memory__search`` -- cross-tier semantic recall (L1-forward fusion);
- ``memory__conversation_search`` -- session/scene-scoped recall (L0);
- ``memory__read_scene`` -- one scene's full context (L2/L3 by reference;
  granularity/pagination stays the plan's open U-6 slot, base face only).

Fail-soft is the PINNED contract (plan §4.3), and it inverts the websearch
fail-closed precedent on purpose: an unbound websearch adapter is a host
wiring defect (raise), but an UNCONFIGURED memory tool is an expected
headless state -- the plan's §4.2 headless-ask contradiction stays
registered, and until it is ruled on, "no memory block in config" simply
means the service is unavailable. So every failure face returns a
structured payload, never a bare raise, never a hung main chain:

- ok + results -> ``{"status": "ok", "results": [...], "next_cursor"?}``
- ok + empty  -> ``{"status": "ok", "results": []}`` + the empty sentence
- unavailable -> ``{"status": "unavailable", "reason": token}`` + the
  NOT-an-empty-result sentence (§4.3-2: folding forbidden)

The audit distinction (§4.3-3) rides ``metadata.availability``
("ok" | "unavailable") on every terminal payload. Retry policy is S-7
spike territory (§4.3-4): the ``error.retryable=True`` flag is honest, but
the adapter itself retries nothing. Approval defaults to ask
(:data:`opencode.tools.policy._DEFAULT_EFFECTS`), the §4.1 default.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.memory import MemoryGatewayClient, MemoryUnavailableError
from opencode.tools.builtin.read import _failure

if TYPE_CHECKING:  # pragma: no cover - annotation-only (the question.py house rule)
    from opencode.tools.invoker import ToolContext

MEMORY_NAMESPACE = "memory"
MEMORY_VERSION = 1
MEMORY_SEARCH_NAME = "search"
MEMORY_CONVERSATION_SEARCH_NAME = "conversation_search"
MEMORY_READ_SCENE_NAME = "read_scene"

#: The empty face's model-visible sentence (distinct from the unavailable
#: sentence below by design -- §4.3-2 forbids any folding of the two).
MEMORY_NO_RESULTS = "No relevant memories found for this query."

#: The unavailable face's model-visible sentence: states the outage AND the
#: distinction explicitly, so a model cannot read it as an empty result.
MEMORY_UNAVAILABLE = (
    "Memory service is currently unavailable ({reason}). No memory data was "
    "consulted for this query -- this is a service outage, NOT an empty result."
)


def _search_schema(description: str) -> JsonObject:
    """The shared search input schema (query required; topK/cursor optional)."""
    return {
        "type": "object",
        "description": description,
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of results to return",
            },
            "cursor": {
                "type": "string",
                "description": "Pagination cursor from a previous response",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }


MEMORY_SEARCH_SPEC = ToolSpec(
    namespace=MEMORY_NAMESPACE,
    name=MEMORY_SEARCH_NAME,
    version=MEMORY_VERSION,
    description=(
        "Search the user's long-term memory: cross-tier semantic recall "
        "(distilled memory tier forward, fused retrieval). Use this to recall "
        "facts, decisions, and prior work the user or past sessions stored."
    ),
    input_schema=_search_schema("Search the user's long-term memory."),
    supported_content_types=["text"],
    # Read-only against the recall service; the ask default is the §4.1
    # approval face, not a side-effect claim.
    side_effects=False,
    # Recall is time-varying (memories land continuously): an identical
    # retry is honestly non-convergent, the websearch inversion.
    idempotency_support=False,
)

MEMORY_CONVERSATION_SEARCH_SPEC = ToolSpec(
    namespace=MEMORY_NAMESPACE,
    name=MEMORY_CONVERSATION_SEARCH_NAME,
    version=MEMORY_VERSION,
    description=(
        "Search past conversations and scenes (session-scoped recall): the "
        "verbatim conversation tier. Use this to find what was actually said "
        "in earlier sessions, as opposed to distilled memory facts."
    ),
    input_schema=_search_schema("Search past conversations and scenes."),
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=False,
)

MEMORY_READ_SCENE_SPEC = ToolSpec(
    namespace=MEMORY_NAMESPACE,
    name=MEMORY_READ_SCENE_NAME,
    version=MEMORY_VERSION,
    description=(
        "Read one memory scene's full context document by its id (obtained "
        "from memory_search or conversation_search results)."
    ),
    input_schema={
        "type": "object",
        "description": "Read one memory scene's full context.",
        "properties": {
            "scene_id": {
                "type": "string",
                "description": "The scene id to read",
            },
        },
        "required": ["scene_id"],
        "additionalProperties": False,
    },
    supported_content_types=["text"],
    side_effects=False,
    # A stable scene document under a stable id: the honest idempotent read.
    idempotency_support=True,
)


def _ok_search(page_results: tuple[JsonObject, ...], next_cursor: str | None) -> ToolResult:
    """The ok face for both search tools (results possibly empty)."""
    structured: JsonObject = {"status": "ok", "results": list(page_results)}
    if next_cursor is not None:
        structured["next_cursor"] = next_cursor
    if page_results:
        lines = "\n".join(f"- {json.dumps(item, ensure_ascii=False, separators=(',', ':'))}" for item in page_results)
        text = f"Found {len(page_results)} memory result(s):\n{lines}"
    else:
        text = MEMORY_NO_RESULTS
    return ToolResult(
        content=[ToolTextContent(type="text", text=text)],
        structured_output=structured,
        metadata={"availability": "ok"},
    )


def _ok_scene(scene: JsonObject) -> ToolResult:
    return ToolResult(
        content=[ToolTextContent(type="text", text=json.dumps(scene, ensure_ascii=False))],
        structured_output={"status": "ok", "scene": scene},
        metadata={"availability": "ok"},
    )


def _unavailable(reason: str) -> ToolResult:
    """The §4.3-1/2/3 face: structured outage payload, never a raise."""
    return ToolResult(
        content=[ToolTextContent(type="text", text=MEMORY_UNAVAILABLE.format(reason=reason))],
        structured_output={"status": "unavailable", "reason": reason},
        metadata={"availability": "unavailable"},
        error=CoreError(
            code=CoreErrorCode.TOOL_FAILED,
            message=f"memory service unavailable: {reason}",
            # Honest flag (retry MIGHT succeed); the retry POLICY itself is
            # the S-7 spike's, not this adapter's (§4.3-4).
            retryable=True,
        ),
    )


def _optional_int(input: JsonObject, key: str) -> int | None:
    value = input.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _optional_str(input: JsonObject, key: str) -> str | None:
    value = input.get(key)
    return value if isinstance(value, str) and value else None


def _required_str(input: JsonObject, key: str) -> str | None:
    """The fail-closed double face (the Invoker already validated the
    schema; None here means the re-check failed)."""
    value = input.get(key)
    return value if isinstance(value, str) and value else None


async def _run_search(
    client: MemoryGatewayClient | None,
    input: JsonObject,
    *,
    conversation: bool,
) -> ToolResult:
    """The shared body of both search adapters (one gateway method each)."""
    if client is None:
        return _unavailable("unconfigured")
    query = _required_str(input, "query")
    if query is None:
        return _failure(CoreErrorCode.VALIDATION_ERROR, "query is required and must be a non-empty string")
    top_k = _optional_int(input, "top_k")
    cursor = _optional_str(input, "cursor")
    try:
        if conversation:
            page = await client.conversation_search(query, top_k=top_k, cursor=cursor)
        else:
            page = await client.search(query, top_k=top_k, cursor=cursor)
    except MemoryUnavailableError as error:
        return _unavailable(error.reason)
    except Exception:
        # §4.3-1: the main chain continues -- an unexpected defect in this
        # adapter's own plumbing still renders as the outage payload.
        return _unavailable("unexpected")
    return _ok_search(page.results, page.next_cursor)


class MemorySearchAdapter:
    """The ``memory__search`` builtin (§5.1 face: ``spec`` + ``execute``)."""

    def __init__(self, client: MemoryGatewayClient | None = None) -> None:
        self._client = client

    def bind(self, client: MemoryGatewayClient) -> None:
        """Bind the host-constructed gateway client (test/host seam)."""
        self._client = client

    @property
    def spec(self) -> ToolSpec:
        return MEMORY_SEARCH_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await _run_search(self._client, input, conversation=False)


class MemoryConversationSearchAdapter:
    """The ``memory__conversation_search`` builtin (the L0 twin)."""

    def __init__(self, client: MemoryGatewayClient | None = None) -> None:
        self._client = client

    def bind(self, client: MemoryGatewayClient) -> None:
        self._client = client

    @property
    def spec(self) -> ToolSpec:
        return MEMORY_CONVERSATION_SEARCH_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return await _run_search(self._client, input, conversation=True)


class MemoryReadSceneAdapter:
    """The ``memory__read_scene`` builtin (the L2/L3 document read)."""

    def __init__(self, client: MemoryGatewayClient | None = None) -> None:
        self._client = client

    def bind(self, client: MemoryGatewayClient) -> None:
        self._client = client

    @property
    def spec(self) -> ToolSpec:
        return MEMORY_READ_SCENE_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        client = self._client
        if client is None:
            return _unavailable("unconfigured")
        scene_id = _required_str(input, "scene_id")
        if scene_id is None:
            return _failure(CoreErrorCode.VALIDATION_ERROR, "scene_id is required and must be a non-empty string")
        try:
            scene = await client.read_scene(scene_id)
        except MemoryUnavailableError as error:
            return _unavailable(error.reason)
        except Exception:
            return _unavailable("unexpected")
        return _ok_scene(scene)
