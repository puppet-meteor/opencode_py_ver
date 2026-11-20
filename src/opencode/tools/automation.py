"""Automation tools: the ``opencode__automation_*`` face (T-C3-003).

Upstream ``tool/plugin/automation.ts`` first-period slice: five adapters
(create/list/pause/resume/delete) registered under the ``opencode``
namespace. Each call re-reads the automation through the service, which
applies the same validation and optimistic settlement as the scheduler.

Model-facing inputs mirror the service payload (``trigger``/``action``
objects, ``every`` in seconds); the create tool derives ``directory``/
``session_id`` from the calling context and the service derives the
project (owner session, else a stable digest of the directory -- py has
no projects registry face this period, recorded deviation).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec

if TYPE_CHECKING:
    from opencode.automation.service import AutomationService
    from opencode.tools.invoker import ToolContext

AUTOMATION_NAMESPACE = "opencode"
AUTOMATION_VERSION = 1

_TOOLS = ("create", "list", "pause", "resume", "delete")

_CREATE_DESCRIPTION = (
    "Create an automation that sends a prompt on a timer: an at (one shot), an "
    "every interval (seconds) or a cron expression, each with an optional IANA tz. "
    "The prompt targets this session by default; pass to: {session: 'ses_...'} for "
    "another session in this project or to: {subagent: 'agent'} for a fresh child "
    "session of this one on every run."
)
_LIST_DESCRIPTION = "List this project's automations with their status and next run, plus the current time."
_PAUSE_DESCRIPTION = "Pause an automation. It keeps its settings and does not run until resumed."
_RESUME_DESCRIPTION = "Resume a paused automation. A missed at runs once; repeating timers resume at their next slot."
_DELETE_DESCRIPTION = "Delete an automation for good."

_ID_SCHEMA: JsonObject = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Automation id from automation_list."},
    },
    "required": ["id"],
    "additionalProperties": False,
}

_CREATE_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Create a scheduled prompt automation.",
    "properties": {
        "name": {
            "type": "string",
            "description": "A short label shown in lists (non-empty; the service validates).",
        },
        "trigger": {
            "type": "object",
            "description": (
                'When it runs: {"type": "at", "at": "in 20 minutes" | ISO time}, '
                '{"type": "every", "every": 3600} (seconds) or '
                '{"type": "cron", "cron": "0 9 * * 1-5"}, each with an optional "tz".'
            ),
        },
        "action": {
            "type": "object",
            "description": (
                'What it does: {"type": "prompt", "prompt": "...", "to": {"session": '
                '"ses_..."} | {"subagent": "agent"}}. "to" defaults to this session.'
            ),
        },
        "expires": {
            "type": "string",
            "description": 'When it stops for good: an instant, a wall-clock time in tz, or "in 8 hours".',
        },
    },
    "required": ["name", "trigger", "action"],
    "additionalProperties": False,
}

_LIST_SCHEMA: JsonObject = {
    "type": "object",
    "description": "List this project's automations.",
    "properties": {},
    "additionalProperties": False,
}


def _spec(tool: str) -> ToolSpec:
    descriptions = {
        "create": _CREATE_DESCRIPTION,
        "list": _LIST_DESCRIPTION,
        "pause": _PAUSE_DESCRIPTION,
        "resume": _RESUME_DESCRIPTION,
        "delete": _DELETE_DESCRIPTION,
    }
    schemas = {"create": _CREATE_SCHEMA, "list": _LIST_SCHEMA} | {
        tool: _ID_SCHEMA for tool in ("pause", "resume", "delete")
    }
    return ToolSpec(
        namespace=AUTOMATION_NAMESPACE,
        name=f"automation_{tool}",
        version=AUTOMATION_VERSION,
        description=descriptions[tool],
        input_schema=schemas[tool],
        supported_content_types=["text"],
        side_effects=True,
        idempotency_support=False,
    )


def _invalid(result: Exception) -> ToolResult:
    message = getattr(result, "reason", None) or str(result)
    return ToolResult(
        content=[ToolTextContent(type="text", text=f"automation error: {message}")],
        error=CoreError(code=CoreErrorCode.EXECUTION_UNKNOWN, message=message, retryable=False),
    )


def _summary(info: dict[str, Any]) -> str:
    return f'{info["id"]} "{info["name"]}": {info["status"]}, next {info["next_due_at"]}'


class AutomationAdapter:
    """One ``opencode__automation_<tool>`` builtin (spec + execute face)."""

    def __init__(self, service: AutomationService, *, tool: str) -> None:
        if tool not in _TOOLS:
            raise ValueError(f"unknown automation tool: {tool}")
        self._service = service
        self._tool = tool
        self._spec = _spec(tool)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        service = self._service
        try:
            if self._tool == "create":
                payload: dict[str, Any] = {
                    "name": input.get("name"),
                    "trigger": input.get("trigger"),
                    "action": input.get("action"),
                }
                if input.get("expires") is not None:
                    payload["expires"] = input.get("expires")
                payload["session_id"] = context.session_id
                payload["directory"] = context.working_dir
                info = await service.create(payload)
                return ToolResult(
                    content=[ToolTextContent(type="text", text=f"Created: {_summary(info)}")],
                    structured_output=cast("JsonValue", dict(info)),
                )
            if self._tool == "list":
                project = service._project_for(str(context.working_dir), None)
                if context.session_id:
                    resolved = None
                    sessions = service._sessions
                    if sessions is not None:
                        resolved = sessions.resolve_session(context.session_id)
                    if resolved is not None:
                        project = resolved.project_id
                automations = await service.list(project_id=project)
                return ToolResult(
                    content=[ToolTextContent(type="text", text="\n".join(_summary(item) for item in automations))],
                    structured_output=cast("JsonValue", {"automations": automations}),
                )
            automation_id = str(input.get("id", ""))
            if self._tool == "pause":
                info = await service.pause(automation_id)
            elif self._tool == "resume":
                info = await service.resume(automation_id)
            else:
                await service.remove(automation_id)
                return ToolResult(
                    content=[ToolTextContent(type="text", text=f"Deleted automation {automation_id}.")],
                    structured_output=cast("JsonValue", {"id": automation_id, "removed": True}),
                )
            return ToolResult(
                content=[ToolTextContent(type="text", text=_summary(info))],
                structured_output=dict(info),
            )
        except Exception as error:
            return _invalid(error)
