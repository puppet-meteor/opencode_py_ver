"""Tool wire contracts (CORE-CONTRACTS §5.1): ToolRef/ToolSpec/ToolResult.

Only the shapes that cross seams live here: catalog entries ride
``GenerationRequest.tools`` (S1 face) and adapter results come back as
``ToolResult``. The registry/invoke runtime (register/snapshot/invoke, §5.1
L247-251) is R2-S5 and must not redeclare these wire models. TS field mapping:
``packages/schema/src/tool.ts`` / ``packages/core/src/tool.ts`` per
doc/parity.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from opencode.contracts.errors import CoreError
from opencode.contracts.messages import JsonObject, ToolContent, _reject_bool


class ToolRef(BaseModel):
    """Catalog identity of a tool: ``(namespace, name, version)``.

    ``version`` is the tool's own schema version (>= 1), not an event version;
    §8's closed-evolution rule for events applies to tool schemas by the same
    spirit — a changed input_schema ships as a new version, never a silent
    same-version edit.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)

    @field_validator("version", mode="before")
    @classmethod
    def _version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("version", value)


class ToolSpec(BaseModel):
    """§5.1 L244: the eight catalog fields shown to the provider per request.

    ``supported_content_types`` is the subset of {"text","file"} the tool's
    results may contain; R2 builtins declare ["text"] only. Empty list means a
    purely structured result (``structured_output`` carries it, §2.1 L72).
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    description: str
    input_schema: JsonObject
    supported_content_types: list[Literal["text", "file"]]
    side_effects: bool
    idempotency_support: bool

    @field_validator("version", mode="before")
    @classmethod
    def _version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("version", value)


class ToolResult(BaseModel):
    """§5.1 L245: adapter execution result — content first, never a bare string.

    ``content`` may be empty only when ``structured_output`` carries the value
    (MCP ``structuredContent``); the model-readable-text requirement for tool
    terminal states is enforced on ``py.tool.finished`` data, not here — this
    is the raw adapter face before Invoker normalization (R2-S5).
    """

    model_config = ConfigDict(extra="forbid")

    content: list[ToolContent]
    structured_output: JsonValue | None = None
    metadata: JsonObject = Field(default_factory=dict)
    error: CoreError | None = None
