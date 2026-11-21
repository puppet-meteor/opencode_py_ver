"""The four artifact tools (upstream ``core/src/tool/plugin/artifact.ts``).

Upstream registers ``opencode.artifact_create/read/update/delete`` in the
Code Mode catalog when the ``experimental.artifacts`` flag is on; the py
face is :data:`TOOL_SPECS` plus :func:`adapters` -- hosts pass the service
in and get the four adapters, or an empty list when the flag constant is
off (the TS plugin simply never registers, the tools stay hidden).

The create description is rendered from the live provider registry (the TS
``prompts()``): the default provider line plus the other-providers line,
exactly what :meth:`ArtifactService.provider_prompt_text` returns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from opencode.artifact import service as _artifact_service
from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _failure

if TYPE_CHECKING:
    from opencode.tools.invoker import ToolContext

NAMESPACE = "opencode"

#: The TS Code Mode tool paths, verbatim (opencode.artifact_*).
TOOL_SPECS = [
    "opencode.artifact_create",
    "opencode.artifact_read",
    "opencode.artifact_update",
    "opencode.artifact_delete",
]

_FILES_SCHEMA = cast(
    "JsonValue",
    {
        "type": "object",
        "description": "UTF-8 file map keyed by relative path (for example index.html and assets/app.js).",
    },
)

_CREATE_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Create a multi-file artifact (an HTML report plus its assets).",
    "properties": {
        "title": {"type": "string", "description": "Human-readable artifact title."},
        "files": _FILES_SCHEMA,
        "entry": {
            "type": "string",
            "description": "Entry file path relative to the artifact root (default: index.html, index.md, or the sole file).",  # noqa: E501
        },
        "provider": {
            "type": "string",
            "description": "Artifact provider ID to override the default provider for this artifact.",
        },
    },
    "required": ["title", "files"],
    "additionalProperties": False,
}

_ID_SCHEMA = cast(
    "JsonValue",
    {
        "type": "string",
        "description": "The artifact ID (art_ prefix).",
    },
)

_READ_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Read one artifact's info and files.",
    "properties": {"id": _ID_SCHEMA},
    "required": ["id"],
    "additionalProperties": False,
}

_UPDATE_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Replace one artifact's files at the same ID and URL.",
    "properties": {
        "id": _ID_SCHEMA,
        "title": {"type": "string", "description": "New title (kept when omitted)."},
        "files": _FILES_SCHEMA,
        "entry": {"type": "string", "description": "New entry path (kept when omitted)."},
    },
    "required": ["id"],
    "additionalProperties": False,
}

_DELETE_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Delete one artifact.",
    "properties": {"id": _ID_SCHEMA},
    "required": ["id"],
    "additionalProperties": False,
}


def _text(payload: Any) -> ToolResult:
    return ToolResult(
        content=[ToolTextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_output=cast("JsonValue", payload),
    )


def _fail(error: Exception) -> ToolResult:
    code = CoreErrorCode.VALIDATION_ERROR
    if isinstance(error, LookupError):
        code = CoreErrorCode.NOT_FOUND
    return _failure(code, str(error))


def _make_adapter(name: str, description: str, schema: JsonObject, run: Any) -> type:
    spec = ToolSpec(
        namespace=NAMESPACE,
        name=name,
        version=1,
        description=description,
        input_schema=schema,
        supported_content_types=["text"],
        side_effects=name != "artifact_read",
        # Reads are pure; the mutating three honestly declare no replay
        # support (a repeated create would mint another artifact).
        idempotency_support=name == "artifact_read",
    )

    class _Adapter:
        @property
        def spec(self) -> ToolSpec:
            return spec

        async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
            try:
                return _text(await run(input))
            except Exception as error:
                return _fail(error)

    return _Adapter


def adapters(*, service: Any) -> list[Any]:
    """The four adapters bound to ``service``; empty when the flag is off.

    The gate reads the module constant at call time, so a host (or a test)
    flipping :data:`ARTIFACTS_ENABLED` changes the answer immediately.
    """
    if not getattr(_artifact_service, "ARTIFACTS_ENABLED", False):
        return []
    description_create = "Create a multi-file artifact (an HTML report plus its assets).\n\n" + cast(
        str, service.provider_prompt_text()
    )
    create = _make_adapter("artifact_create", description_create, _CREATE_SCHEMA, service.create)
    read = _make_adapter("artifact_read", "Read one artifact's info and files.", _READ_SCHEMA, service.get)
    update = _make_adapter(
        "artifact_update", "Replace one artifact's files at the same ID and URL.", _UPDATE_SCHEMA, service.update
    )
    delete = _make_adapter("artifact_delete", "Delete one artifact.", _DELETE_SCHEMA, service.delete)
    return [create(), read(), update(), delete()]
