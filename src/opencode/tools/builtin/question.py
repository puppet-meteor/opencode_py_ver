"""Builtin ``question__ask`` tool (P6-S4; T-C1-097 toolkit A, ticket §3-4).

Structured question asking, adapted from TS
``packages/core/src/tool/plugin/question.ts`` (entry shape from
``packages/schema/src/question.ts`` ``Prompt``): the model sends a
validated, non-empty array of ``{question, header, options, multiple?}``
entries; the tool renders them and RETURNS IMMEDIATELY. It never blocks,
never collects answers and never writes anything — the user's replies
arrive as the next user turn of the conversation and the model continues
from them (ticket §3-4: the answer channel is the conversation itself).

TS mapping / deviations, quantified (progress file):

- The TS tool blocks on ``Form.Service.ask`` (an interactive form with
  per-question field widgets), back-fills ``answers`` into the tool
  result and tunnels a dismissal through ``CancelledError`` (a typed
  defect tunnel). None of that surface exists on the Python side: no
  Form/TUI service, and the approval channel is a closed three-value
  decision enum (``ApprovalCoordinator.resolve_reply``:
  once/always/reject) that cannot carry free-text answers — forcing it
  to would be a protocol extension, out of ticket. The Python shape is
  therefore the ticket's structured question form: questions in, echo +
  ``structured_output`` out, tool terminal. DIFFERENT-BY-DESIGN
  (progress file ③), pinned by tests.
- The TS input is ``{questions: Schema.Array(Prompt).check(isNonEmpty)}``
  — an effect-Schema array. The Invoker's strict schema subset carries
  no array type, so the Python wire shape is a JSON-encoded
  ``questions`` string (the todo.py / pty.py precedent); the adapter
  decodes and validates it fail-closed (ticket §8).
  DIFFERENT-BY-DESIGN.
- An entry carrying keys beyond ``question``/``header``/``options``/
  ``multiple`` — or an option beyond ``label``/``description`` — is
  rejected. The TS effect Structs silently strip unknown keys; the
  Python side keeps the fail-closed reading (ticket §8).
- The description text is seat-written (ticket §3-9: TS ships no
  question ``.txt``) — the TS description adapted to the non-blocking
  JSON-string form (progress file ⑦).
- The result face: content = the count line + the per-question echo
  (the tool text IS the display surface — the questions reach the user
  through the model-visible tool result); structured_output =
  ``{"questions": [...]}`` (the validated entries, canonicalized with
  ``multiple`` defaulted to ``false``); metadata = ``{"questions":
  [...]}`` (the TS metadata face carried the form answers; the Python
  result payload is the questions themselves).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _failure

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (edit.py does the same).
    from opencode.tools.invoker import ToolContext

QUESTION_NAMESPACE = "question"
QUESTION_NAME = "ask"
QUESTION_VERSION = 1

#: Seat-written description (ticket §3-9: TS ships no question .txt): the
#: TS question.ts description adapted to the non-blocking JSON-string form.
QUESTION_DESCRIPTION = (
    "Use this tool when you need to ask the user questions during execution. This allows you to:\n"
    "1. Gather user preferences or requirements\n"
    "2. Clarify ambiguous instructions\n"
    "3. Get decisions on implementation choices as you work\n"
    "4. Offer choices to the user about what direction to take.\n"
    "\n"
    "Usage notes:\n"
    "- The questions are shown to the user as part of your message; the tool returns immediately and "
    "does not block for answers\n"
    "- Wait for the user's reply in the next user message, then continue from it\n"
    "- Offer concise options per question: a short label (1-5 words) plus a longer description of the "
    "choice\n"
    "- Set `multiple: true` to allow selecting more than one option\n"
    "- If you recommend a specific option, make that the first option in the list and add "
    '"(Recommended)" at the end of the label'
)

QUESTION_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Ask the user structured questions; the tool returns immediately and the answers arrive "
        "as the next user message."
    ),
    "properties": {
        "questions": {
            "type": "string",
            "description": (
                "The questions to ask, as a JSON-encoded non-empty array of entries: "
                "{question, header, options: [{label, description}], multiple?}"
            ),
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}

QUESTION_SPEC = ToolSpec(
    namespace=QUESTION_NAMESPACE,
    name=QUESTION_NAME,
    version=QUESTION_VERSION,
    description=QUESTION_DESCRIPTION,
    input_schema=QUESTION_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    # Asking is a pure echo face: the same questions re-asked produce the
    # same result, so the adapter honestly declares idempotency.
    idempotency_support=True,
)

_QUESTION_KEYS = frozenset({"question", "header", "options", "multiple"})
_OPTION_KEYS = frozenset({"label", "description"})
_REQUIRED_QUESTION_KEYS = frozenset({"question", "header", "options"})


class _QuestionValidationError(ValueError):
    """One questions payload rejection (ticket §8: the fail-closed face)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class _Option:
    """One validated choice (schema/question.ts ``Option``: label + description)."""

    label: str
    description: str


@dataclass(frozen=True, slots=True)
class _Question:
    """One validated entry (schema/question.ts ``Prompt``, minus the unported field faces)."""

    question: str
    header: str
    options: tuple[_Option, ...]
    multiple: bool


def _parse_questions(raw: str) -> list[_Question]:
    """Decode + validate the JSON-encoded question list (fail-closed, ticket §8).

    Raises :class:`_QuestionValidationError` with a model-readable reason
    on every rejection: bad JSON, a non-array or empty top level (the TS
    ``isNonEmpty`` check), a non-object entry, an entry with unknown or
    missing keys, a mistyped field, or an option that is not exactly a
    ``{label, description}`` pair of strings.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _QuestionValidationError(f"questions is not valid JSON: {error}") from error
    if not isinstance(parsed, list) or not parsed:
        raise _QuestionValidationError("questions must be a JSON-encoded non-empty array of question entries")
    questions: list[_Question] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise _QuestionValidationError(
                f"questions[{index}] must be an object with the keys 'question', 'header' and 'options'"
            )
        unknown = set(entry) - _QUESTION_KEYS
        if unknown:
            raise _QuestionValidationError(
                f"questions[{index}] has unknown key(s): {', '.join(sorted(unknown))}; "
                "allowed keys are 'question', 'header', 'options' and 'multiple'"
            )
        missing = _REQUIRED_QUESTION_KEYS - set(entry)
        if missing:
            raise _QuestionValidationError(
                f"questions[{index}] is missing required key(s): {', '.join(sorted(missing))}"
            )
        question = entry["question"]
        header = entry["header"]
        options = entry["options"]
        multiple = entry.get("multiple", False)
        if not isinstance(question, str):
            raise _QuestionValidationError(f"questions[{index}].question must be a string")
        if not isinstance(header, str):
            raise _QuestionValidationError(f"questions[{index}].header must be a string")
        if not isinstance(options, list):
            raise _QuestionValidationError(f"questions[{index}].options must be an array")
        if not isinstance(multiple, bool):
            raise _QuestionValidationError(f"questions[{index}].multiple must be a boolean")
        validated_options: list[_Option] = []
        for option_index, option in enumerate(options):
            if not isinstance(option, dict) or set(option) != _OPTION_KEYS:
                raise _QuestionValidationError(
                    f"questions[{index}].options[{option_index}] must be an object with exactly "
                    "the keys 'label' and 'description'"
                )
            label = option["label"]
            description = option["description"]
            if not isinstance(label, str):
                raise _QuestionValidationError(f"questions[{index}].options[{option_index}].label must be a string")
            if not isinstance(description, str):
                raise _QuestionValidationError(
                    f"questions[{index}].options[{option_index}].description must be a string"
                )
            validated_options.append(_Option(label=label, description=description))
        questions.append(
            _Question(
                question=question,
                header=header,
                options=tuple(validated_options),
                multiple=multiple,
            )
        )
    return questions


def _render(questions: Sequence[_Question]) -> str:
    """The count line + the per-question echo (the tool text IS the display surface)."""
    lines = [f"Questions asked: {len(questions)}."]
    for index, question in enumerate(questions):
        labels = ", ".join(option.label for option in question.options)
        choice = " (choose any)" if question.multiple else ""
        echo = f"{index + 1}. {question.header}: {question.question}{choice}"
        lines.append(f"{echo} (options: {labels})" if labels else echo)
    return "\n".join(lines)


def _payload(questions: Sequence[_Question]) -> dict[str, JsonValue]:
    """The canonical structured face: the validated entries with ``multiple`` defaulted."""
    return {
        "questions": [
            {
                "question": item.question,
                "header": item.header,
                # The cast is the git.py house restatement (L588): the
                # list-of-dicts is a JsonValue by construction.
                "options": cast(
                    "JsonValue",
                    [{"label": option.label, "description": option.description} for option in item.options],
                ),
                "multiple": item.multiple,
            }
            for item in questions
        ]
    }


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against QUESTION_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the JSON-string hop is
    # re-validated here because the array face lives beyond the schema
    # subset (fail-closed, ticket §8).
    raw = input.get("questions")
    if not isinstance(raw, str):
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            "questions is required and must be a JSON-encoded non-empty array",
        )
    try:
        questions = _parse_questions(raw)
    except _QuestionValidationError as error:
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Invalid questions: {error.message}")
    payload = _payload(questions)
    return ToolResult(
        content=[ToolTextContent(type="text", text=_render(questions))],
        structured_output=payload,
        metadata=payload,
    )


class QuestionAskAdapter:
    """The ``question__ask`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the decode/validate/render
    pipeline is synchronous by design (edit.py/write.py precedent: no I/O,
    no awaits, no blocking; the echo cost bounded by the decoded input
    size). No ``roots`` constructor argument: the tool touches no files.
    """

    @property
    def spec(self) -> ToolSpec:
        return QUESTION_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input)
