"""P6-S4 toolkit A integration tests (T-C1-097): patch / todo / question.

The ticket §4 face map, all through the public faces (adapter ->
registry register -> snapshot -> invoke by wire name), plus the App
census, the policy default table and the three Specs' eight catalog
fields:

* patch §4-1/2 -- the parser/verify matrix (envelope missing/mismatch,
  Add body without ``+``, Add on an existing on-disk file and the
  in-patch duplicate, Delete of a missing file + body lines + the
  delete-then-delete chain, Update of a missing file / unmatched anchor
  / @@ context disambiguation / ``*** End of File`` anchoring (both
  arms) / the blank-line retry / the typographic seek tier
  (later-exact-beats-earlier-normalized, the minus/quote/space
  families, the ellipsis non-normalization), the Move-to rename chain,
  the multi-file mix, the empty envelope / empty patchText, the
  heredoc wrapper, CRLF patch lines, roots escape vs the roots
  extension, the 1 MiB source cap + the non-UTF-8 read gate) and the
  apply e2e (Add+Update+Delete+Move on tmp_path, the honest
  already-applied list under an injected write failure, the
  rejected-patch-touches-nothing discipline).
* todo §4-3 -- the four-value enum summary (exact TS ``summarize``
  text), the empty array ``Todos cleared`` face, the stateless
  full-replacement semantics, and the fail-closed validation faces.
* question §4-4 -- the two-question render + count text, the
  structured_output/metadata faces, and the fail-closed validation
  faces (empty array, unknown/missing keys, bad options, bad
  ``multiple``); the immediate/non-blocking terminal is the design pin
  (no ``answers`` key anywhere: the replies ride the next user turn).
* §4-5/6 -- the App census (the frozen host composition carries the
  trio: fifteen base names, sixteen on the pty platforms), the policy
  default rows (patch ask, todo and question allow), the eight-field
  Specs, and the builtin export surface (constants AND Adapter classes
  re-exported -- the bijective census constraint).

(4) disclosures: the pty platform branch is covered through the
composition-derived expected set (the census derives its baseline from
``_builtin_adapters`` itself, so win32/linux/macOS all assert the same
arithmetic); the patch apply-phase ``makedirs`` failure branch and the
delete-phase remove race stay untested (timing faces, the injected
write failure covers the honest-list shape); the Invoker-level input
schema rejections (a non-string todos/questions value) are the
registry's own face, pinned by the registration/Invoker tests, not
re-tested here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.llm import GenerationRequest, ModelRef, ProviderChunk
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.httpapi.serving import _builtin_adapters
from opencode.session.context import RuntimeSettings
from opencode.session.service import IdFactory
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry, builtin
from opencode.tools.builtin import (
    PATCH_MAX_SOURCE_BYTES,
    PATCH_SPEC,
    QUESTION_SPEC,
    TODO_SPEC,
    TODO_STATUSES,
)
from opencode.tools.builtin import patch as patch_module
from opencode.tools.builtin.patch import PatchApplyAdapter
from opencode.tools.builtin.question import QuestionAskAdapter
from opencode.tools.builtin.todo import TodoWriteAdapter
from opencode.tools.invoker import validate_schema_supported
from opencode.tools.policy import PermissionPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]

_FIXED_TIME = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_tka"
_RUN_ID = "run_tka"
_MESSAGE_ID = "msg_tka"
_TOOL_CALL_ID = "call_tka"
_MODEL = ModelRef(provider="maas", model="glm-4.6")


# ---------------------------------------------------------------------------
# harness (the test_git_worktree_tools plain-registry recipe)
# ---------------------------------------------------------------------------


def _clock() -> datetime:
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


def _registry(roots: Sequence[str] = ()) -> ToolRegistry:
    """A plain registry with the toolkit A trio, one pinned snapshot."""
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast(IdFactory, _FixedIds()))
    registry.register(PatchApplyAdapter(roots=roots))
    registry.register(TodoWriteAdapter())
    registry.register(QuestionAskAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


async def _run(working_dir: Path, name: str, input: JsonObject, *, roots: Sequence[str] = ()) -> ToolOutcome:
    return await _registry(roots).invoke(_context(working_dir), name, input, _TOOL_CALL_ID)


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


def _patch(*lines: str) -> str:
    """A patch envelope around the given section lines."""
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def _posix(path: Path) -> str:
    """The forward-slash absolute form for in-patch absolute paths."""
    return str(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# patch §4-1: the parser/verify matrix (fail-closed)
# ---------------------------------------------------------------------------


async def test_patch_rejects_a_missing_envelope_and_a_missing_end_boundary(tmp_path: Path) -> None:
    no_begin = await _run(tmp_path, "patch__apply", {"patchText": "This is not a valid patch"})
    assert "The first line of the patch must be '*** Begin Patch'" in _failed(no_begin, CoreErrorCode.VALIDATION_ERROR)

    no_end = await _run(tmp_path, "patch__apply", {"patchText": "*** Begin Patch\n*** Add File: add.txt\n+added"})
    assert "The last line of the patch must be '*** End Patch'" in _failed(no_end, CoreErrorCode.VALIDATION_ERROR)

    leading = await _run(tmp_path, "patch__apply", {"patchText": "extra\n*** Begin Patch\n*** End Patch"})
    assert "The first line of the patch must be '*** Begin Patch'" in _failed(leading, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_rejects_an_empty_envelope_and_an_empty_patch_text(tmp_path: Path) -> None:
    empty_envelope = await _run(tmp_path, "patch__apply", {"patchText": _patch()})
    assert "patch rejected: empty patch" in _failed(empty_envelope, CoreErrorCode.VALIDATION_ERROR)

    empty_text = await _run(tmp_path, "patch__apply", {"patchText": ""})
    assert "patchText is required" in _failed(empty_text, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_rejects_add_body_lines_without_plus_and_delete_body_lines(tmp_path: Path) -> None:
    add_body = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Add File: file.txt", "bad")})
    assert "Invalid Add File line for 'file.txt': expected a line starting with '+', got 'bad'" in _failed(
        add_body, CoreErrorCode.VALIDATION_ERROR
    )

    delete_body = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Delete File: file.txt", "bad")})
    assert "Unexpected line after Delete File 'file.txt': 'bad'. Delete hunks do not contain body lines" in _failed(
        delete_body, CoreErrorCode.VALIDATION_ERROR
    )


async def test_patch_rejects_adding_an_existing_or_in_patch_duplicate_file(tmp_path: Path) -> None:
    (tmp_path / "exists.txt").write_text("here\n", encoding="utf-8")

    on_disk = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Add File: exists.txt", "+x")})
    assert "Failed to add exists.txt: file already exists" in _failed(on_disk, CoreErrorCode.VALIDATION_ERROR)

    in_patch = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch("*** Add File: dup.txt", "+a", "*** Add File: dup.txt", "+b")},
    )
    assert "Failed to add dup.txt: file already exists" in _failed(in_patch, CoreErrorCode.VALIDATION_ERROR)
    assert not (tmp_path / "dup.txt").exists()  # a rejected patch touches nothing


async def test_patch_rejects_deleting_a_missing_file_and_the_in_patch_delete_chain(tmp_path: Path) -> None:
    missing = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Delete File: missing.txt")})
    assert "Failed to delete missing.txt: file does not exist" in _failed(missing, CoreErrorCode.VALIDATION_ERROR)

    (tmp_path / "gone.txt").write_text("bye\n", encoding="utf-8")
    chain = await _run(
        tmp_path, "patch__apply", {"patchText": _patch("*** Delete File: gone.txt", "*** Delete File: gone.txt")}
    )
    assert "Failed to delete gone.txt: file does not exist" in _failed(chain, CoreErrorCode.VALIDATION_ERROR)
    assert (tmp_path / "gone.txt").exists()  # prepare-everything-then-apply: nothing was applied


async def test_patch_rejects_updating_a_missing_file_and_an_unmatched_anchor(tmp_path: Path) -> None:
    missing = await _run(
        tmp_path, "patch__apply", {"patchText": _patch("*** Update File: missing.txt", "@@", "-a", "+b")}
    )
    assert "Failed to read file to update missing.txt" in _failed(missing, CoreErrorCode.VALIDATION_ERROR)

    (tmp_path / "update.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    no_context = await _run(
        tmp_path, "patch__apply", {"patchText": _patch("*** Update File: update.txt", "@@ absent", "-a", "+b")}
    )
    assert "Failed to find context 'absent' in update.txt" in _failed(no_context, CoreErrorCode.VALIDATION_ERROR)

    no_lines = await _run(
        tmp_path, "patch__apply", {"patchText": _patch("*** Update File: update.txt", "@@", "-gamma", "+delta")}
    )
    assert "Failed to find expected lines in update.txt" in _failed(no_lines, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_rejects_paths_that_escape_the_working_directory(tmp_path: Path) -> None:
    traversal = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Add File: ../outside.txt", "+data")})
    assert "Path escapes the working directory" in _failed(traversal, CoreErrorCode.PERMISSION_DENIED)

    absolute = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch(f"*** Add File: {_posix(tmp_path.parent / 'outside.txt')}", "+data")},
    )
    assert "Path escapes the working directory" in _failed(absolute, CoreErrorCode.PERMISSION_DENIED)


async def test_patch_rejects_sources_over_the_cap_or_not_valid_utf8(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (PATCH_MAX_SOURCE_BYTES + 1))
    over = await _run(tmp_path, "patch__apply", {"patchText": _patch("*** Update File: big.txt", "@@", "-x", "+y")})
    assert f"over the {PATCH_MAX_SOURCE_BYTES} byte patch limit" in _failed(over, CoreErrorCode.TOOL_FAILED)

    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    bad_utf8 = await _run(
        tmp_path, "patch__apply", {"patchText": _patch("*** Update File: binary.bin", "@@", "-x", "+y")}
    )
    assert "file is not valid UTF-8 text" in _failed(bad_utf8, CoreErrorCode.TOOL_FAILED)


# ---------------------------------------------------------------------------
# patch §4-2: apply e2e (the real file system)
# ---------------------------------------------------------------------------


async def test_patch_applies_a_mixed_add_update_delete_move_patch(tmp_path: Path) -> None:
    (tmp_path / "update.txt").write_text("fn a\nx=10\nfn b\nx=10\n", encoding="utf-8")
    (tmp_path / "moved.txt").write_text("old content\n", encoding="utf-8")
    (tmp_path / "removed.txt").write_text("bye\n", encoding="utf-8")

    text = _patch(
        "*** Add File: created.txt",
        "+first",
        "+second",
        "*** Update File: update.txt",
        "@@ fn b",
        "-x=10",
        "+x=11",
        "*** Update File: moved.txt",
        "*** Move to: renamed.txt",
        "@@",
        "-old content",
        "+new content",
        "*** Delete File: removed.txt",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": text})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _one_text(outcome) == (
        "Success. Updated the following files:\nA created.txt\nM update.txt\nM renamed.txt\nD removed.txt"
    )
    # @@ anchor disambiguation: the SECOND x=10 (below "fn b") is the one replaced
    assert (tmp_path / "update.txt").read_text(encoding="utf-8") == "fn a\nx=10\nfn b\nx=11\n"
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "first\nsecond\n"
    assert (tmp_path / "renamed.txt").read_text(encoding="utf-8") == "new content\n"
    assert not (tmp_path / "moved.txt").exists()
    assert not (tmp_path / "removed.txt").exists()


async def test_patch_applies_the_in_patch_state_chains_and_rejects_the_update_after_delete(
    tmp_path: Path,
) -> None:
    add_then_update = _patch(
        "*** Add File: chain.txt",
        "+head",
        "+line one",
        "*** Update File: chain.txt",
        "@@ head",
        "-line one",
        "+line one changed",
        "*** Update File: chain.txt",
        "@@",
        "+tail",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": add_then_update})
    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed"), _one_text(outcome)
    # the later hunks saw the earlier hunks' result (the in-patch state map)
    assert (tmp_path / "chain.txt").read_text(encoding="utf-8") == "head\nline one changed\ntail\n"

    delete_then_update = _patch(
        "*** Delete File: victim.txt",
        "*** Update File: victim.txt",
        "@@",
        "-x",
        "+y",
    )
    (tmp_path / "victim.txt").write_text("x\n", encoding="utf-8")
    rejected = await _run(tmp_path, "patch__apply", {"patchText": delete_then_update})
    assert "Failed to read file to update victim.txt: file does not exist" in _failed(
        rejected, CoreErrorCode.VALIDATION_ERROR
    )
    assert (tmp_path / "victim.txt").exists()  # nothing applied


async def test_patch_reports_the_already_applied_list_when_a_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = patch_module._atomic_replace
    calls: list[str] = []

    def flaky_replace(real: str, data: bytes) -> None:
        calls.append(real)
        if len(calls) == 2:
            raise OSError("simulated disk failure")
        real_replace(real, data)

    monkeypatch.setattr(patch_module, "_atomic_replace", flaky_replace)

    text = _patch("*** Add File: first.txt", "+one", "*** Add File: second.txt", "+two")
    outcome = await _run(tmp_path, "patch__apply", {"patchText": text})

    message = _failed(outcome, CoreErrorCode.TOOL_FAILED)
    assert "Failed to write second.txt" in message
    assert ". Completed before failure: first.txt" in message  # the honest applied list
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "one\n"
    assert not (tmp_path / "second.txt").exists()


async def test_patch_strips_a_heredoc_wrapper_and_crlf_line_endings(tmp_path: Path) -> None:
    inner = _patch("*** Add File: heredoc.txt", "+payload")
    wrapped = f"cat <<'EOF'\n{inner}\nEOF"
    outcome = await _run(tmp_path, "patch__apply", {"patchText": wrapped})
    assert outcome.state == "succeeded"
    assert (tmp_path / "heredoc.txt").read_text(encoding="utf-8") == "payload\n"

    crlf = _patch("*** Add File: crlf.txt", "+line").replace("\n", "\r\n")
    outcome = await _run(tmp_path, "patch__apply", {"patchText": crlf})
    assert outcome.state == "succeeded"
    assert (tmp_path / "crlf.txt").read_text(encoding="utf-8") == "line\n"


async def test_patch_anchors_end_of_file_chunks_and_refuses_non_eof_matches(tmp_path: Path) -> None:
    (tmp_path / "tail.txt").write_text("a\nlast\n", encoding="utf-8")
    anchored = _patch(
        "*** Update File: tail.txt",
        "@@",
        "-last",
        "+final",
        "*** End of File",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": anchored})
    assert outcome.state == "succeeded"
    assert (tmp_path / "tail.txt").read_text(encoding="utf-8") == "a\nfinal\n"

    # the load-bearing arm: a non-EOF match exists at offset 0, but the
    # EOF-anchored seek only looks at the tail -- fail closed (TS parity)
    (tmp_path / "eof.txt").write_text("marker\nend\nmiddle\n", encoding="utf-8")
    refused = _patch(
        "*** Update File: eof.txt",
        "@@",
        "-marker",
        "-end",
        "+changed",
        "+end",
        "*** End of File",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": refused})
    assert "Failed to find expected lines in eof.txt" in _failed(outcome, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_retries_a_trailing_blank_line_on_a_file_without_a_final_newline(tmp_path: Path) -> None:
    # "a\nb" has no trailing newline: the ["b", ""] seek fails first, then the
    # retry trims the trailing blank from BOTH sides (util/patch.ts
    # computeReplacements) and the bare ["b"] matches
    (tmp_path / "no-newline.txt").write_text("a\nb", encoding="utf-8")
    text = _patch(
        "*** Update File: no-newline.txt",
        "@@",
        "-b",
        "+replacement",
        "",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": text})
    assert outcome.state == "succeeded"
    assert (tmp_path / "no-newline.txt").read_text(encoding="utf-8") == "a\nreplacement\n"

    (tmp_path / "no-blank.txt").write_text("content\n", encoding="utf-8")
    missing = _patch(
        "*** Update File: no-blank.txt",
        "@@",
        "",
        "+added",
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": missing})
    assert "Failed to find an expected blank line in no-blank.txt" in _failed(outcome, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_seek_tiers_match_typographic_variants_and_prefer_later_exact_matches(
    tmp_path: Path,
) -> None:
    # a later EXACT match beats an earlier normalized match (per-tier scan)
    quotes = tmp_path / "quotes.txt"
    quotes.write_text('He said \u201chello\u201d\nmiddle\nHe said "hello"\n', encoding="utf-8")
    later_exact = _patch(
        "*** Update File: quotes.txt",
        "@@",
        '-He said "hello"',
        '+He said "goodbye"',
    )
    outcome = await _run(tmp_path, "patch__apply", {"patchText": later_exact})
    assert outcome.state == "succeeded"
    assert quotes.read_text(encoding="utf-8") == 'He said \u201chello\u201d\nmiddle\nHe said "goodbye"\n'

    # the typographic families: U+2212 minus, NBSP space
    minus = tmp_path / "minus.txt"
    minus.write_text("value \u2212 1\n", encoding="utf-8")
    outcome = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch("*** Update File: minus.txt", "@@", "-value - 1", "+value - 2")},
    )
    assert outcome.state == "succeeded"
    assert minus.read_text(encoding="utf-8") == "value - 2\n"

    nbsp = tmp_path / "nbsp.txt"
    nbsp.write_text("hello\u00a0world\n", encoding="utf-8")
    outcome = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch("*** Update File: nbsp.txt", "@@", "-hello world", "+hello there")},
    )
    assert outcome.state == "succeeded"
    assert nbsp.read_text(encoding="utf-8") == "hello there\n"

    # the normalization table is closed: an ellipsis is NOT three dots
    ellipsis = tmp_path / "ellipsis.txt"
    ellipsis.write_text("wait\u2026\n", encoding="utf-8")
    outcome = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch("*** Update File: ellipsis.txt", "@@", "-wait...", "+done")},
    )
    assert "Failed to find expected lines in ellipsis.txt" in _failed(outcome, CoreErrorCode.VALIDATION_ERROR)


async def test_patch_preserves_a_leading_bom_through_an_update(tmp_path: Path) -> None:
    # the TS Bom.split/joinBom pair rides derive: the source BOM survives
    bom = tmp_path / "bom.txt"
    bom.write_bytes("\ufeffold\n".encode("utf-8"))
    outcome = await _run(
        tmp_path,
        "patch__apply",
        {"patchText": _patch("*** Update File: bom.txt", "@@", "-old", "+new")},
    )
    assert outcome.state == "succeeded"
    assert bom.read_bytes() == "\ufeffnew\n".encode("utf-8")


async def test_the_roots_extension_admits_a_secondary_root_the_default_rejects(tmp_path: Path) -> None:
    # a sibling of the pytest tmp dir: inside the default roots' parent but
    # outside the working directory itself, so the default policy must reject
    extra = tmp_path.parent / f"{tmp_path.name}-extra"
    extra.mkdir()
    (extra / "shared.txt").write_text("old\n", encoding="utf-8")
    text = _patch(
        f"*** Update File: {_posix(extra / 'shared.txt')}",
        "@@",
        "-old",
        "+new",
    )

    denied = await _run(tmp_path, "patch__apply", {"patchText": text})
    assert "Path escapes the working directory" in _failed(denied, CoreErrorCode.PERMISSION_DENIED)

    admitted = await _run(tmp_path, "patch__apply", {"patchText": text}, roots=(str(extra),))
    assert (admitted.state, admitted.effect_status) == ("succeeded", "completed")
    assert (extra / "shared.txt").read_text(encoding="utf-8") == "new\n"


# ---------------------------------------------------------------------------
# todo §4-3: the stateless echo
# ---------------------------------------------------------------------------


async def test_todo_write_echoes_the_four_value_enum_and_the_count_summary(tmp_path: Path) -> None:
    todos = [
        {"content": "Write the parser", "status": "completed"},
        {"content": "Wire the adapters", "status": "in_progress"},
        {"content": "Run the gates", "status": "pending"},
        {"content": "Drop the old plan", "status": "cancelled"},
    ]
    outcome = await _run(tmp_path, "todo__write", {"todos": json.dumps(todos)})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    # the TS summarize text verbatim: fraction first, zero-count statuses
    # suppressed, the in-progress list JSON-quoted
    assert _one_text(outcome) == (
        'Todos updated: 1/4 completed, 1 in progress, 1 pending, 1 cancelled. In progress: "Wire the adapters"'
    )
    assert outcome.structured_output == {"todos": todos}

    # the metadata face (the TS execute face rides metadata too; the
    # registry outcome drops it, so the direct adapter call pins it)
    direct = await TodoWriteAdapter().execute(_context(tmp_path), {"todos": json.dumps(todos)})
    assert direct.error is None
    assert direct.structured_output == {"todos": todos}
    assert direct.metadata == {"todos": todos}


async def test_todo_write_clears_the_list_on_an_empty_array(tmp_path: Path) -> None:
    outcome = await _run(tmp_path, "todo__write", {"todos": "[]"})
    assert outcome.state == "succeeded"
    assert _one_text(outcome) == "Todos cleared"
    assert outcome.structured_output == {"todos": []}


async def test_todo_write_replaces_the_previous_list_statelessly(tmp_path: Path) -> None:
    first = await _run(
        tmp_path,
        "todo__write",
        {
            "todos": json.dumps(
                [
                    {"content": "First task", "status": "completed"},
                    {"content": "Second task", "status": "pending"},
                ]
            )
        },
    )
    assert "1/2 completed" in _one_text(first)

    second = await _run(
        tmp_path, "todo__write", {"todos": json.dumps([{"content": "Only task", "status": "in_progress"}])}
    )
    text = _one_text(second)
    # zero residue: the array IS the new list (full-replacement semantics)
    assert text == 'Todos updated: 0/1 completed, 1 in progress. In progress: "Only task"'
    assert "First task" not in text
    assert "Second task" not in text


async def test_todo_write_rejects_invalid_payloads_fail_closed(tmp_path: Path) -> None:
    bad_payloads = [
        ("{not json", "todos is not valid JSON"),
        ('{"content": "a", "status": "pending"}', "todos must be a JSON-encoded array of {content, status} entries"),
        ('["a"]', "todos[0] must be an object with exactly the keys 'content' and 'status'"),
        ('[{"content": "a"}]', "todos[0] must be an object with exactly the keys 'content' and 'status'"),
        (
            '[{"content": "a", "status": "pending", "note": "x"}]',
            "todos[0] must be an object with exactly the keys 'content' and 'status'",
        ),
        (
            '[{"content": "a", "status": "done"}]',
            "todos[0].status must be one of: pending, in_progress, completed, cancelled",
        ),
        ('[{"content": 7, "status": "pending"}]', "todos[0].content must be a string"),
    ]
    for raw, fragment in bad_payloads:
        outcome = await _run(tmp_path, "todo__write", {"todos": raw})
        assert fragment in _failed(outcome, CoreErrorCode.VALIDATION_ERROR), raw


# ---------------------------------------------------------------------------
# question §4-4: the structured question face
# ---------------------------------------------------------------------------


async def test_question_ask_renders_the_questions_and_returns_immediately(tmp_path: Path) -> None:
    questions = [
        {
            "question": "Which database engine should we use?",
            "header": "Database",
            "options": [
                {"label": "PostgreSQL", "description": "The relational default"},
                {"label": "SQLite", "description": "The embedded option"},
            ],
        },
        {
            "question": "Which language for the CLI?",
            "header": "Language",
            "options": [{"label": "Python", "description": "The current core"}],
            "multiple": True,
        },
    ]
    outcome = await _run(tmp_path, "question__ask", {"questions": json.dumps(questions)})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    lines = _one_text(outcome).splitlines()
    assert lines[0] == "Questions asked: 2."
    assert lines[1] == "1. Database: Which database engine should we use? (options: PostgreSQL, SQLite)"
    assert lines[2] == "2. Language: Which language for the CLI? (choose any) (options: Python)"

    # the canonical face: multiple defaulted to False on the first entry
    expected_payload = {
        "questions": [
            {
                "question": "Which database engine should we use?",
                "header": "Database",
                "options": [
                    {"label": "PostgreSQL", "description": "The relational default"},
                    {"label": "SQLite", "description": "The embedded option"},
                ],
                "multiple": False,
            },
            {
                "question": "Which language for the CLI?",
                "header": "Language",
                "options": [{"label": "Python", "description": "The current core"}],
                "multiple": True,
            },
        ]
    }
    assert outcome.structured_output == expected_payload

    # the immediate/non-blocking terminal (the DIFFERENT-BY-DESIGN pin):
    # no answers key anywhere -- the replies ride the next user turn
    assert "answers" not in json.dumps(outcome.structured_output)
    direct = await QuestionAskAdapter().execute(_context(tmp_path), {"questions": json.dumps(questions)})
    assert direct.error is None
    assert direct.structured_output == expected_payload
    assert direct.metadata == expected_payload


async def test_question_ask_rejects_invalid_payloads_fail_closed(tmp_path: Path) -> None:
    bad_payloads = [
        ("{not json", "questions is not valid JSON"),
        ("[]", "questions must be a JSON-encoded non-empty array of question entries"),
        ('{"question": "q"}', "questions must be a JSON-encoded non-empty array of question entries"),
        ("[42]", "questions[0] must be an object with the keys 'question', 'header' and 'options'"),
        (
            '[{"question": "q", "header": "h", "options": [], "priority": 1}]',
            "questions[0] has unknown key(s): priority",
        ),
        ('[{"question": "q", "options": []}]', "questions[0] is missing required key(s): header"),
        ('[{"question": "q", "header": "h", "options": "nope"}]', "questions[0].options must be an array"),
        (
            '[{"question": "q", "header": "h", "options": [{}]}]',
            "questions[0].options[0] must be an object with exactly the keys 'label' and 'description'",
        ),
        (
            '[{"question": "q", "header": "h", "options": [{"label": "a", "description": "d"}], "multiple": "yes"}]',
            "questions[0].multiple must be a boolean",
        ),
        ('[{"question": 1, "header": "h", "options": []}]', "questions[0].question must be a string"),
    ]
    for raw, fragment in bad_payloads:
        outcome = await _run(tmp_path, "question__ask", {"questions": raw})
        assert fragment in _failed(outcome, CoreErrorCode.VALIDATION_ERROR), raw


# ---------------------------------------------------------------------------
# §4-5/6: census, policy defaults, the Specs, the export surface
# ---------------------------------------------------------------------------


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _migrated(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"
    return db_path


def _settings() -> RuntimeSettings:
    return RuntimeSettings(model=_MODEL, context_limit=100_000, output_reserve=4096, timeout_s=30.0)


class _ScriptedProvider:
    """Fake Provider (the test_app_compaction._ScriptedProvider precedent)."""

    def __init__(self, scripts: Sequence[Sequence[ProviderChunk]] | None = None) -> None:
        self._scripts = [list(script) for script in scripts] if scripts is not None else []
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self._scripts.pop(0) if self._scripts else []
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        self.close_count += 1


async def test_app_census_registers_the_full_host_composition_with_the_toolkit_a_trio(
    tmp_path: Path,
) -> None:
    """The host composition face (ticket §4-5): the trio rides both
    production host assemblies (the CLI drive list and
    ``serving._builtin_adapters``), so an App composed with the frozen face
    registers the fifteen base adapters (sixteen on the pty platforms) --
    the trio wires resolve, nothing else joins, and a tool-less App
    composition stays tool-less (the app.py ``tools`` loop is host-param
    plumbing, never a builtin registry)."""
    db_path = _migrated(tmp_path, "toolkit_a_census.db")
    provider = _ScriptedProvider()
    composed = _builtin_adapters((str(tmp_path),))
    async with App(
        db_path,
        mode="create",
        working_dir=str(tmp_path),
        provider=provider,
        settings=_settings(),
        tools=composed,
    ) as app:
        await app.recovery_gate.wait()
        registry = app.registry
        assert registry is not None
        catalog = registry.snapshot("ses_toolkit_a_census")
        expected = {f"{adapter.spec.namespace}__{adapter.spec.name}" for adapter in composed}
        wire_names = {f"{spec.namespace}__{spec.name}" for spec in catalog.specs}
        assert wire_names == expected  # the composed set exactly, nothing else
        assert len(catalog.specs) == len(composed)  # 15 -> 16 host adapters on the pty platforms
        for wire_name in ("patch__apply", "todo__write", "question__ask"):
            assert catalog.resolve(wire_name) is not None
        # the registration-isolation face is unchanged: no workflow namespace
        assert "workflow" not in {spec.namespace for spec in catalog.specs}
    assert provider.close_count == 1


def test_the_policy_defaults_table_carries_the_three_new_rows(tmp_path: Path) -> None:
    policy = PermissionPolicy(boot_id="boot_toolkit_a")
    ask = policy.evaluate(
        session_id="ses_000001",
        action="patch__apply",
        resources=(str(tmp_path / "file.txt"),),
        input_hash="a" * 64,
        working_dir=str(tmp_path),
        side_effects=True,
    )
    assert (ask.effect, ask.source) == ("ask", "default")

    for action in ("todo__write", "question__ask"):
        decision = policy.evaluate(
            session_id="ses_000001",
            action=action,
            resources=("*",),
            input_hash="a" * 64,
            working_dir=str(tmp_path),
            side_effects=False,
        )
        assert (decision.effect, decision.source) == ("allow", "default"), action


def test_the_three_specs_pin_the_eight_catalog_fields() -> None:
    assert (PATCH_SPEC.namespace, PATCH_SPEC.name, PATCH_SPEC.version) == ("patch", "apply", 1)
    assert (PATCH_SPEC.side_effects, PATCH_SPEC.idempotency_support) == (True, False)
    assert (TODO_SPEC.namespace, TODO_SPEC.name, TODO_SPEC.version) == ("todo", "write", 1)
    assert (TODO_SPEC.side_effects, TODO_SPEC.idempotency_support) == (False, True)
    assert (QUESTION_SPEC.namespace, QUESTION_SPEC.name, QUESTION_SPEC.version) == ("question", "ask", 1)
    assert (QUESTION_SPEC.side_effects, QUESTION_SPEC.idempotency_support) == (False, True)
    for spec in (PATCH_SPEC, TODO_SPEC, QUESTION_SPEC):
        assert spec.supported_content_types == ["text"]
        assert isinstance(spec.description, str) and spec.description
        validate_schema_supported(spec.input_schema, root=True)  # the registration face accepts each
    # todowrite.txt verbatim (ticket §3-9)
    assert TODO_SPEC.description.startswith("Create and update a todo list for the current session.")
    assert "Send the full list on every call; it replaces the previous one." in TODO_SPEC.description
    assert TODO_STATUSES == ("pending", "in_progress", "completed", "cancelled")
    # patch.txt verbatim
    assert "Your patch language is a stripped" in PATCH_SPEC.description
    assert "*** Begin Patch" in PATCH_SPEC.description
    # seat-written question description (§3-9: TS ships no question .txt)
    assert QUESTION_SPEC.description.startswith("Use this tool when you need to ask the user questions")


def test_builtin_reexports_the_trio_constants_and_adapter_classes() -> None:
    """The trio rides both host compositions, so the export surface carries
    the constants (their SPEC pins the catalog identity) AND the Adapter
    classes -- the bijective census guard (test_workflow_tools: every
    exported Adapter class appears exactly once in the host composition,
    and the composition carries nothing undeclared) stays green with the
    trio composed."""
    exported = (
        "PATCH_DESCRIPTION",
        "PATCH_INPUT_SCHEMA",
        "PATCH_MAX_SOURCE_BYTES",
        "PATCH_NAME",
        "PATCH_NAMESPACE",
        "PATCH_SPEC",
        "PATCH_VERSION",
        "QUESTION_DESCRIPTION",
        "QUESTION_INPUT_SCHEMA",
        "QUESTION_NAME",
        "QUESTION_NAMESPACE",
        "QUESTION_SPEC",
        "QUESTION_VERSION",
        "TODO_DESCRIPTION",
        "TODO_INPUT_SCHEMA",
        "TODO_NAME",
        "TODO_NAMESPACE",
        "TODO_SPEC",
        "TODO_STATUSES",
        "TODO_VERSION",
    )
    for name in exported:
        assert name in builtin.__all__, name
        assert getattr(builtin, name) is not None, name
    for class_name in ("PatchApplyAdapter", "TodoWriteAdapter", "QuestionAskAdapter"):
        assert class_name in builtin.__all__, class_name
        assert getattr(builtin, class_name) is not None, class_name
