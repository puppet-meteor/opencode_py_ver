"""Builtin ``todo__write`` tool (P6-S4; T-C1-097 toolkit A, ticket §3-3).

Stateless todo-list echo, ported from TS
``packages/core/src/tool/plugin/todo.ts``: every call carries the FULL
updated list (full-replacement semantics — the array IS the new list,
it does not merge with anything), nothing is persisted, and the result
is the echo summary the user sees as the model's working plan.

TS mapping / deviations, quantified (progress file):

- The TS input is ``{todos: Schema.Array(Info)}`` — an effect-Schema
  array of ``{content, status}`` structs. The Invoker's strict schema
  subset (invoker.py ``validate_schema_supported``) carries no array
  type, so the Python wire shape is a JSON-encoded ``todos`` string
  (the pty.py argv-string precedent); the adapter decodes and
  validates it fail-closed (ticket §8). DIFFERENT-BY-DESIGN, pinned by
  tests.
- The TS ``experimental.todo`` config gate (the tool only registers
  when the config flag is on) is not ported: the Python side has no
  experimental configuration face (ticket §2: zero ``experimental``
  hits under ``src/``), so the tool is ALWAYS registered.
  DIFFERENT-BY-DESIGN (progress file ④).
- An entry carrying keys beyond ``content``/``status`` is rejected.
  The TS effect Struct silently strips unknown keys; the Python side
  keeps the fail-closed reading (ticket §8) — an entry with an
  unknown key is a model mistake worth reporting, not silently
  reshaping.
- The status enumeration is the TS literal set verbatim:
  ``pending`` / ``in_progress`` / ``completed`` / ``cancelled``.
- ``_summarize`` is the TS ``summarize`` port verbatim: the
  ``"Todos cleared"`` empty-list face, the ``completed/total``
  fraction-first count, the zero-count status suppression, the
  ``status.replace("_", " ")`` label (each TS literal carries at most
  one underscore, so the JS first-occurrence replace and Python's
  replace agree on the enumeration), and the ``. In progress: ...``
  JSON-quoted active list (``ensure_ascii=False`` keeps the JS
  ``JSON.stringify`` face — raw non-ASCII, no ``\\uXXXX`` escapes).
- The result face is the TS execute face: content = the summary text,
  structured_output = ``{"todos": [...]}`` (the TS ``Output`` schema
  shape), metadata = ``{"todos": [...]}`` (the TS metadata face).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _failure

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (edit.py does the same).
    from opencode.tools.invoker import ToolContext

TODO_NAMESPACE = "todo"
TODO_NAME = "write"
TODO_VERSION = 1

#: The TS ``Info.status`` literal set (todo.ts), ported verbatim.
TODO_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed", "cancelled")

#: todowrite.txt verbatim (ticket §3-9: the model-facing description text
#: ships in English, ported from the TS ``../todowrite.txt`` import).
TODO_DESCRIPTION = (
    "Create and update a todo list for the current session. The user sees it as your working plan.\n"
    "\n"
    "Use it proactively when a task has 3 or more distinct steps, or when the user gives several tasks "
    "or asks for a todo list. Skip it for a single simple task or a purely conversational request. "
    "When in doubt, use it.\n"
    "\n"
    "- Send the full list on every call; it replaces the previous one.\n"
    "- Mark items `in_progress` when you start them and `completed` as soon as they're actually done.\n"
    "- Only call it when the list changes."
)

TODO_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Create and update the session todo list (full replacement: every call sends the complete new list)."
    ),
    "properties": {
        "todos": {
            "type": "string",
            "description": (
                "The updated todo list as a JSON-encoded array of {content, status} entries "
                "(status: pending, in_progress, completed, cancelled). "
                "Send the full list on every call; it replaces the previous one."
            ),
        },
    },
    "required": ["todos"],
    "additionalProperties": False,
}

TODO_SPEC = ToolSpec(
    namespace=TODO_NAMESPACE,
    name=TODO_NAME,
    version=TODO_VERSION,
    description=TODO_DESCRIPTION,
    input_schema=TODO_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    # A stateless echo: the same list re-sent produces the same result
    # face, so the adapter honestly declares idempotency (the TS tool
    # has no side effects to replay either).
    idempotency_support=True,
)


class _TodoValidationError(ValueError):
    """One todos payload rejection (ticket §8: the fail-closed face)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_todos(raw: str) -> list[dict[str, str]]:
    """Decode + validate the JSON-encoded todo list (fail-closed, ticket §8).

    Raises :class:`_TodoValidationError` with a model-readable reason on
    every rejection: bad JSON, a non-array top level, a non-object entry,
    an entry without exactly the ``content``/``status`` keys, a
    non-string ``content``, or a ``status`` outside the TS four-value
    enumeration.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _TodoValidationError(f"todos is not valid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise _TodoValidationError("todos must be a JSON-encoded array of {content, status} entries")
    todos: list[dict[str, str]] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict) or set(entry) != {"content", "status"}:
            raise _TodoValidationError(f"todos[{index}] must be an object with exactly the keys 'content' and 'status'")
        content = entry["content"]
        status = entry["status"]
        if not isinstance(content, str):
            raise _TodoValidationError(f"todos[{index}].content must be a string")
        if not isinstance(status, str) or status not in TODO_STATUSES:
            raise _TodoValidationError(f"todos[{index}].status must be one of: {', '.join(TODO_STATUSES)}")
        todos.append({"content": content, "status": status})
    return todos


def _summarize(todos: Sequence[dict[str, str]]) -> str:
    """The TS todo.ts ``summarize`` port: the status-count echo text."""
    if not todos:
        return "Todos cleared"
    total = len(todos)

    def count(status: str) -> int:
        return sum(1 for todo in todos if todo["status"] == status)

    counts = ", ".join(
        [f"{count('completed')}/{total} completed"]
        + [
            f"{count(status)} {status.replace('_', ' ')}"
            for status in ("in_progress", "pending", "cancelled")
            if count(status)
        ]
    )
    active = [json.dumps(todo["content"], ensure_ascii=False) for todo in todos if todo["status"] == "in_progress"]
    summary = f"Todos updated: {counts}"
    if active:
        summary = f"{summary}. In progress: {', '.join(active)}"
    return summary


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against TODO_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the JSON-string hop is
    # re-validated here because the array face lives beyond the schema
    # subset (fail-closed, ticket §8).
    raw = input.get("todos")
    if not isinstance(raw, str):
        return _failure(CoreErrorCode.VALIDATION_ERROR, "todos is required and must be a JSON-encoded array")
    try:
        todos = _parse_todos(raw)
    except _TodoValidationError as error:
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Invalid todo list: {error.message}")
    return ToolResult(
        content=[ToolTextContent(type="text", text=_summarize(todos))],
        # The cast is the git.py house restatement (L417): a nested
        # list-of-dicts is a JsonValue by construction, the checker only
        # needs it restated through dict invariance.
        structured_output={"todos": cast("JsonValue", todos)},
        metadata={"todos": cast("JsonValue", todos)},
    )


class TodoWriteAdapter:
    """The ``todo__write`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the decode/validate/summarize
    pipeline is synchronous by design (edit.py/write.py precedent: no I/O,
    no awaits, the echo cost bounded by the decoded input size). No
    ``roots`` constructor argument: the tool touches no files.
    """

    @property
    def spec(self) -> ToolSpec:
        return TODO_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input)
