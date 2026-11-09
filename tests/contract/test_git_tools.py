"""P2-S6 contract tests: the read-only git pair ``git__status``/``git__diff`` (T-C1-053).

PHASE2-MODULES L67 authority sentence, first half (translated): "status/diff
first; record cwd/roots" -- this file pins exactly the two
read-only subcommands through the real public faces: adapter -> registry
register -> snapshot -> invoke (wire names), plus the policy default table
and the spec/schema contract surfaces. Every behavioral test builds a
**throwaway repository under the pytest temp root** (``git init`` in a
``tmp_path`` directory; the worktree's own git face is never touched).

Face map (per the ticket's test discipline):

- normal: status clean/dirty (both with the branch header line), diff
  unstaged/staged, pathspec narrowing, the catalog snapshot pinning both
  wire names, the policy evaluate allow for both keys;
- failure: out-of-root and traversal rejection, git missing (monkeypatched
  ``_which_git``) -> TOOL_FAILED install hint, non-repository directory ->
  exit 128/129 carried as data (never a tool error), the empty-path string
  boundary (treated as omitted = whole working directory), NUL-byte path,
  missing in-root path;
- platform: forward-slash relative input (all platforms), absolute
  native-separator input, and the Windows drive-letter case face
  (win32-only honest skip).

(4) disclosures: the timeout/cancel/kill-tree branches stay untested here --
the fixed 120 s deadline and the cooperative-cancel race are timing faces
the ticket's discipline discloses rather than constructs (bash.py owns the
same branches with dedicated two-generation probes); the not-reaped
defensive branch is untestable without breaking the runner contract; the
Windows execution-alias git stub face is disclosed (the ``git --version``
identification check follows the grep.py ``_rg_version_ok`` precedent,
which that module pins instead).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.session.service import IdFactory
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import (
    GIT_DEFAULT_TIMEOUT_S,
    GIT_DIFF_INPUT_SCHEMA,
    GIT_DIFF_SPEC,
    GIT_MAX_OUTPUT_BYTES,
    GIT_STATUS_INPUT_SCHEMA,
    GIT_STATUS_SPEC,
    GitDiffAdapter,
    GitStatusAdapter,
)
from opencode.tools.builtin import git as git_module
from opencode.tools.policy import PermissionPolicy

_FIXED_TIME = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_cgit"
_RUN_ID = "run_cgit"
_MESSAGE_ID = "msg_cgit"
_TOOL_CALL_ID = "call_cgit"


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


def _registry() -> ToolRegistry:
    """A registry with just the git pair, one pinned snapshot."""
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast(IdFactory, _FixedIds()))
    registry.register(GitStatusAdapter())
    registry.register(GitDiffAdapter())
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


async def _run(working_dir: Path, name: str, input: JsonObject) -> ToolOutcome:
    return await _registry().invoke(_context(working_dir), name, input, _TOOL_CALL_ID)


def _texts(outcome: ToolOutcome) -> list[str]:
    return [part.text for part in outcome.content if isinstance(part, ToolTextContent)]


def _one_text(outcome: ToolOutcome) -> str:
    texts = _texts(outcome)
    assert len(texts) == 1
    return texts[0]


def _structured(outcome: ToolOutcome) -> dict[str, Any]:
    assert outcome.structured_output is not None
    return cast("dict[str, Any]", outcome.structured_output)


def _structured_exit(outcome: ToolOutcome) -> Any:
    return _structured(outcome)["exit"]


# ---------------------------------------------------------------------------
# throwaway repositories (temproot ``tmp_path`` only; never the worktree face)
# ---------------------------------------------------------------------------


def _git_or_skip() -> None:
    """Honest skip when no usable git binary (CI runners always have one)."""
    executable, problem = git_module._resolve_git()
    if executable is None:
        assert problem is not None
        pytest.skip(f"git unavailable: {problem}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(path: Path) -> Path:
    """A committed clean repository with one tracked ``hello.txt``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "contract@example.com")
    _git(path, "config", "user.name", "Contract Test")
    (path / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return path


# ---------------------------------------------------------------------------
# normal: status clean/dirty + branch header
# ---------------------------------------------------------------------------


async def test_git_status_clean_repo_reports_branch_header_only(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__status", {})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    lines = _one_text(outcome).splitlines()
    assert len(lines) == 1  # a clean tree: the branch header is the only line
    assert lines[0].startswith("## ")  # the branch line (name not pinned: init default varies)
    assert _structured_exit(outcome) == 0
    structured = _structured(outcome)
    assert structured["cwd"] == str(repo)  # L67: cwd recorded
    assert structured["roots"] == [str(repo)]  # L67: roots recorded
    assert structured["timeout"] is False
    assert structured["truncated"] is False


async def test_git_status_dirty_repo_lists_changes(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello2\n", encoding="utf-8")

    outcome = await _run(repo, "git__status", {})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    lines = _one_text(outcome).splitlines()
    assert lines[0].startswith("## ")  # the branch header stays first
    assert lines[1] == " M hello.txt"  # porcelain v1 unstaged-modified record
    assert _structured_exit(outcome) == 0


async def test_git_status_pathspec_narrows_the_report(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    (repo / "sub" / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "sub")  # sub/c.txt is now tracked
    (repo / "sub" / "c.txt").write_text("c2\n", encoding="utf-8")  # modified, tracked
    (repo / "untracked.txt").write_text("u\n", encoding="utf-8")  # untracked at the root

    outcome = await _run(repo, "git__status", {"path": "sub"})

    assert outcome.state == "succeeded"
    text = _one_text(outcome)
    assert " M sub/c.txt" in text  # the pathspec-filtered modified record, repo-relative
    assert "untracked.txt" not in text  # outside the pathspec -> filtered out


# ---------------------------------------------------------------------------
# normal: diff unstaged/staged
# ---------------------------------------------------------------------------


async def test_git_diff_unstaged_shows_working_tree_changes(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello2\n", encoding="utf-8")

    outcome = await _run(repo, "git__diff", {})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    text = _one_text(outcome)
    assert "diff --git" in text
    assert "-hello" in text
    assert "+hello2" in text
    assert _structured_exit(outcome) == 0
    assert _structured(outcome)["cwd"] == str(repo)


async def test_git_diff_staged_shows_index_changes_and_unstaged_goes_empty(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello2\n", encoding="utf-8")
    _git(repo, "add", "hello.txt")

    staged = await _run(repo, "git__diff", {"staged": True})
    assert staged.state == "succeeded"
    assert "+hello2" in _one_text(staged)  # index vs HEAD shows the staged change

    unstaged = await _run(repo, "git__diff", {})
    assert unstaged.state == "succeeded"
    # working tree == index: an empty diff is empty data. The frozen Invoker
    # regenerates the canonical structured JSON as the model text for a
    # content-less terminal (invoker.py _normalize_result), so the empty face
    # is pinned as "no diff markers anywhere in the outcome text".
    joined = "\n".join(_texts(unstaged))
    assert "-hello" not in joined and "+hello2" not in joined and "diff --git" not in joined
    assert _structured_exit(unstaged) == 0


async def test_git_diff_pathspec_narrows_to_one_file(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "two more")  # both tracked...
    (repo / "a.txt").write_text("a2\n", encoding="utf-8")  # ...then both modified
    (repo / "b.txt").write_text("b2\n", encoding="utf-8")

    outcome = await _run(repo, "git__diff", {"path": "a.txt"})

    assert outcome.state == "succeeded"
    text = "\n".join(_texts(outcome))
    assert "diff --git a/a.txt b/a.txt" in text
    assert "b.txt" not in text  # only a.txt in the diff


# ---------------------------------------------------------------------------
# normal: the contract surfaces (catalog snapshot, policy table, spec/schema)
# ---------------------------------------------------------------------------


def test_git_pair_registers_and_catalog_pins_wire_names() -> None:
    """The wire names resolve back to the pinned specs through the public face."""
    registry = _registry()
    catalog = registry.snapshot(_SESSION_ID)

    assert [spec.name for spec in catalog.specs] == ["diff", "status"]  # sorted by (namespace, name)
    assert {spec.namespace for spec in catalog.specs} == {"git"}
    assert catalog.resolve("git__status") is GIT_STATUS_SPEC
    assert catalog.resolve("git__diff") is GIT_DIFF_SPEC
    assert catalog.resolve("git__missing") is None


def test_git_pair_specs_declare_readonly_semantics() -> None:
    """Both specs: version 1, no side effects, idempotent, "Requires the git binary"."""
    for spec in (GIT_STATUS_SPEC, GIT_DIFF_SPEC):
        assert spec.namespace == "git"
        assert spec.version == 1
        assert spec.side_effects is False
        assert spec.idempotency_support is True
        assert spec.supported_content_types == ["text"]
        assert "Requires the git binary" in spec.description
    assert {GIT_STATUS_SPEC.name, GIT_DIFF_SPEC.name} == {"status", "diff"}


def test_git_input_schemas_stay_in_the_validator_subset() -> None:
    for schema in (GIT_STATUS_INPUT_SCHEMA, GIT_DIFF_INPUT_SCHEMA):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        allowed = {"type", "description", "properties", "required", "additionalProperties"}
        assert set(schema) <= allowed
        property_allowed = {"type", "description", "minimum", "maximum"}
        for property_schema in cast("dict[str, Any]", schema["properties"]).values():
            assert set(property_schema) <= property_allowed
            assert property_schema["type"] in ("string", "boolean")
    status_properties = cast("dict[str, Any]", GIT_STATUS_INPUT_SCHEMA["properties"])
    diff_properties = cast("dict[str, Any]", GIT_DIFF_INPUT_SCHEMA["properties"])
    assert set(status_properties) == {"path"}
    assert set(diff_properties) == {"path", "staged"}
    assert diff_properties["staged"]["type"] == "boolean"
    assert "required" not in GIT_STATUS_INPUT_SCHEMA  # both inputs are optional-only
    assert "required" not in GIT_DIFF_INPUT_SCHEMA


def test_git_defaults_match_the_pinned_values() -> None:
    """Ruling f: fixed 120 s deadline + 64 KiB cap (bash.py values, independent constants)."""
    assert GIT_DEFAULT_TIMEOUT_S == 120.0
    assert GIT_MAX_OUTPUT_BYTES == 64 * 1024


def test_git_pair_allows_by_default_in_the_permission_table(tmp_path: Path) -> None:
    """The two explicit ``_DEFAULT_EFFECTS`` keys evaluate to allow/default
    (the zero-behavior closure: out-of-table + side_effects=False allowed
    both before the keys existed; the table entry only documents them)."""
    policy = PermissionPolicy(boot_id="boot_git")
    for action in ("git__status", "git__diff"):
        decision = policy.evaluate(
            session_id="ses_000001",
            action=action,
            resources=(str(tmp_path / "hello.txt"),),
            input_hash="a" * 64,
            working_dir=str(tmp_path),
            side_effects=False,
        )
        assert (decision.effect, decision.source) == ("allow", "default"), action


# ---------------------------------------------------------------------------
# failure: rejections, missing git, non-repository data face, path boundaries
# ---------------------------------------------------------------------------


async def test_git_rejects_out_of_root_absolute_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    outcome = await _run(repo, "git__status", {"path": str(outside)})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED


async def test_git_rejects_traversal_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__diff", {"path": "../outside.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED


async def test_git_rejects_nul_byte_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__status", {"path": "a\x00b"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.VALIDATION_ERROR


async def test_git_missing_in_root_path_is_not_found(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__status", {"path": "no-such.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.NOT_FOUND


async def test_git_missing_binary_fails_with_install_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_module, "_GIT_CACHE", {})
    monkeypatch.setattr(git_module, "_which_git", lambda: None)

    for name in ("git__status", "git__diff"):
        outcome = await _run(tmp_path, name, {})

        assert outcome.state == "failed", name
        assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_FAILED, name
        assert "not found on PATH" in outcome.error.message
        assert "Install a working git" in outcome.error.message
        assert "no silent fallback" in outcome.error.message


async def test_git_status_outside_repository_exit_128_is_data(tmp_path: Path) -> None:
    _git_or_skip()
    plain = tmp_path / "plain"  # a directory inside NO repository
    plain.mkdir()

    outcome = await _run(plain, "git__status", {})

    # Ruling e: a completed command with any exit code is data, not a tool failure.
    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 128
    joined = "\n".join(_texts(outcome))
    assert "not a git repository" in joined  # git's own fatal text, carried as content


async def test_git_diff_outside_repository_nonzero_exit_is_data(tmp_path: Path) -> None:
    _git_or_skip()
    plain = tmp_path / "plain"
    plain.mkdir()

    outcome = await _run(plain, "git__diff", {})

    assert outcome.state == "succeeded"
    assert outcome.error is None
    assert cast("int", _structured_exit(outcome)) > 0  # exit 129 (no-index usage face), nonzero data
    joined = "\n".join(_texts(outcome))
    assert "Not a git repository" in joined  # git's warning text, carried as content


async def test_git_empty_path_string_means_the_whole_working_directory(tmp_path: Path) -> None:
    """Empty-path boundary: ``""`` is treated as omitted -- git 2.53 itself tolerates
    an empty pathspec element, so the two faces agree (whole working dir)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello2\n", encoding="utf-8")

    outcome = await _run(repo, "git__status", {"path": ""})

    assert outcome.state == "succeeded"
    assert " M hello.txt" in _one_text(outcome)  # the whole tree was inspected


# ---------------------------------------------------------------------------
# platform: separators, drive-letter case, forward-slash input
# ---------------------------------------------------------------------------


async def test_git_accepts_forward_slash_relative_path(tmp_path: Path) -> None:
    """A forward-slash relative path resolves on every platform (native on
    POSIX; a first-class relative form on Windows)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    (repo / "sub" / "c.txt").write_text("c\n", encoding="utf-8")

    outcome = await _run(repo, "git__status", {"path": "sub/c.txt"})

    assert outcome.state == "succeeded"
    assert "c.txt" in _one_text(outcome)


async def test_git_accepts_absolute_native_separator_path(tmp_path: Path) -> None:
    """``str(Path(...))`` input (backslashes on Windows) resolves and filters."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    target = repo / "hello.txt"
    target.write_text("hello2\n", encoding="utf-8")
    other = repo / "other.txt"
    other.write_text("other\n", encoding="utf-8")

    outcome = await _run(repo, "git__diff", {"path": str(target)})

    assert outcome.state == "succeeded"
    text = "\n".join(_texts(outcome))
    assert "hello.txt" in text
    assert "other.txt" not in text


@pytest.mark.skipif(_PLATFORM != "win32", reason="drive-letter case folding is a Windows filesystem face")
async def test_git_windows_drive_letter_case_resolves(tmp_path: Path) -> None:
    """A path differing only in drive-letter case resolves (normcase folding)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello2\n", encoding="utf-8")
    raw = str(repo / "hello.txt")
    swapped = raw[0].lower() + raw[1:]  # C:\... -> c:\... (or c -> C on a lowercased drive)

    outcome = await _run(repo, "git__status", {"path": swapped})

    assert outcome.state == "succeeded"
    assert " M hello.txt" in _one_text(outcome)
