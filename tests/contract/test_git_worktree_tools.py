"""P2-S7 contract tests: the worktree trio (T-C1-055).

PHASE2-MODULES L67 authority sentence, second half (translated): "then
approved worktree operations; record cwd/roots and conflicts; never
silently overwrite uncommitted files" -- this file pins the three approved
subcommands through the real public faces: adapter -> registry register ->
snapshot -> invoke (wire names), plus the policy default table and the
spec/schema contract surfaces. Every behavioral test builds a
**throwaway repository under the pytest temp root** (``git init`` plus one
empty commit in a ``tmp_path`` directory -- ``worktree add`` needs a HEAD
to exist; the worktree's own git face is never touched).

Face map (per the ticket's test discipline):

- normal: add without ref (detached HEAD, ``.git`` file) and with ref
  (HEAD~1 checkout), list porcelain (main + new worktree + detached
  marker), remove clean (directory gone), the catalog snapshot pinning
  the three wire names, the policy evaluate faces (list allow, add/remove
  ask);
- conflict: add into a non-empty target refuses BEFORE any git spawn
  (monkeypatched discovery proves the ordering; conflicts recorded,
  contents survive), add onto an existing file, remove of a dirty
  worktree (untracked file survives), remove of an unverifiable path
  (fail-closed, probe cannot run), remove of the main working tree
  (precheck passes, git's native refusal is exit-code data with an EMPTY
  conflicts array), an option-shaped ref (``-b``) staying positional
  (exit-code data, nothing created);
- failure: out-of-root and traversal rejection, NUL-byte path, missing
  in-root path (remove), the empty-string/missing path required face,
  git missing (monkeypatched ``_which_git``) -> TOOL_FAILED install hint,
  non-repository list -> exit 128 carried as data;
- platform: forward-slash relative input, absolute native-separator
  input, and the Windows drive-letter case face (win32-only honest skip).

(4) disclosures: the timeout/cancel/kill-tree branches stay untested here
-- the fixed 120 s deadline, the cooperative-cancel race, the status-probe
timeout/cancel fail-closed branches and the not-reaped defensive branch
are timing faces the ticket's discipline discloses rather than constructs
(bash.py owns the shared run_command process face with dedicated
two-generation probes; the module-level "cancel tests" acceptance of L70
rides that shared face, the T-C1-053 argument continued); the
git-layer post-precheck race window (a target filling up between the
precheck and git's scan) is covered by the main-tree native-refusal test
as its constructible stand-in; the Windows execution-alias git stub face
is disclosed (grep.py pins the ``_rg_version_ok`` precedent).
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
    GitWorktreeAddAdapter,
    GitWorktreeListAdapter,
    GitWorktreeRemoveAdapter,
)
from opencode.tools.builtin import git as git_module
from opencode.tools.policy import PermissionPolicy

_FIXED_TIME = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_cgw"
_RUN_ID = "run_cgw"
_MESSAGE_ID = "msg_cgw"
_TOOL_CALL_ID = "call_cgw"


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
    """A registry with just the worktree trio, one pinned snapshot."""
    registry = ToolRegistry(sink=cast("Any", _RecordingSink()), clock=_clock, ids=cast(IdFactory, _FixedIds()))
    registry.register(GitWorktreeAddAdapter())
    registry.register(GitWorktreeListAdapter())
    registry.register(GitWorktreeRemoveAdapter())
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


def _conflicts(outcome: ToolOutcome) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", _structured(outcome)["conflicts"])


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
    """A repository with one empty base commit (the ticket's fixture form).

    ``git worktree add`` needs a HEAD to exist; the inline ``-c`` identity
    keeps the fixture self-contained (no repo-local config writes).
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "-c", "user.email=probe@example.com", "-c", "user.name=probe", "commit", "--allow-empty", "-m", "base")
    return path


# ---------------------------------------------------------------------------
# normal: add without/with ref, list porcelain, remove clean
# ---------------------------------------------------------------------------


async def test_worktree_add_creates_detached_worktree_without_ref(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    target = repo / "wt-a"

    outcome = await _run(repo, "git__worktree_add", {"path": "wt-a"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert target.is_dir()
    assert (target / ".git").is_file()  # a linked worktree carries a .git FILE, not a directory
    structured = _structured(outcome)
    assert structured["exit"] == 0
    assert structured["cwd"] == str(repo)  # L67: cwd recorded
    assert structured["roots"] == [str(repo)]  # L67: roots recorded
    assert structured["conflicts"] == []  # L67: the conflicts face, empty on success
    # --detach is pinned: the porcelain list reports the new worktree detached
    listing = await _run(repo, "git__worktree_list", {})
    joined = "\n".join(_texts(listing))
    assert "detached" in joined
    assert (repo / "wt-a").as_posix() in joined


async def test_worktree_add_with_ref_checks_out_that_commit(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=probe@example.com", "-c", "user.name=probe", "commit", "-m", "second")

    outcome = await _run(repo, "git__worktree_add", {"path": "wt-ref", "ref": "HEAD~1"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert _structured_exit(outcome) == 0
    # HEAD~1 is the empty base commit: the checked-out worktree predates hello.txt
    assert (repo / "wt-ref").is_dir()
    assert not (repo / "wt-ref" / "hello.txt").exists()
    assert (repo / "hello.txt").exists()  # the main tree keeps its own checkout


async def test_worktree_list_porcelain_includes_main_and_new_worktree(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    assert (await _run(repo, "git__worktree_add", {"path": "wt-list"})).state == "succeeded"

    outcome = await _run(repo, "git__worktree_list", {})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 0
    text = _one_text(outcome)
    # machine-readable blocks: the main tree (branch) and the new worktree (detached)
    assert f"worktree {repo.as_posix()}" in text
    assert f"worktree {(repo / 'wt-list').as_posix()}" in text
    assert "branch refs/heads/" in text
    assert "detached" in text
    structured = _structured(outcome)
    assert structured["cwd"] == str(repo)
    assert structured["roots"] == [str(repo)]
    assert structured["conflicts"] == []  # listing can never conflict, but still records the face


async def test_worktree_remove_clean_deletes_the_directory(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    target = repo / "wt-rm"
    assert (await _run(repo, "git__worktree_add", {"path": "wt-rm"})).state == "succeeded"

    outcome = await _run(repo, "git__worktree_remove", {"path": "wt-rm"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 0
    assert not target.exists()  # the directory is gone
    structured = _structured(outcome)
    assert structured["cwd"] == str(repo)
    assert structured["conflicts"] == []


async def test_worktree_add_twice_refuses_the_second_time(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    assert (await _run(repo, "git__worktree_add", {"path": "wt-once"})).state == "succeeded"

    second = await _run(repo, "git__worktree_add", {"path": "wt-once"})

    # The occupied-target precheck refuses the repeat: honest non-idempotency,
    # and the first checkout survives untouched.
    assert second.state == "failed"
    assert second.error is not None and second.error.code is CoreErrorCode.CONFLICT
    assert (repo / "wt-once" / ".git").is_file()


# ---------------------------------------------------------------------------
# normal: the contract surfaces (catalog snapshot, policy table, spec/schema)
# ---------------------------------------------------------------------------


def test_worktree_trio_registers_and_catalog_pins_wire_names() -> None:
    """The wire names resolve back to the pinned specs through the public face."""
    registry = _registry()
    catalog = registry.snapshot(_SESSION_ID)

    assert [spec.name for spec in catalog.specs] == [
        "worktree_add",
        "worktree_list",
        "worktree_remove",
    ]  # sorted by (namespace, name)
    assert {spec.namespace for spec in catalog.specs} == {"git"}
    assert catalog.resolve("git__worktree_add") is git_module.GIT_WORKTREE_ADD_SPEC
    assert catalog.resolve("git__worktree_list") is git_module.GIT_WORKTREE_LIST_SPEC
    assert catalog.resolve("git__worktree_remove") is git_module.GIT_WORKTREE_REMOVE_SPEC
    # red line: lock/prune/repair/move (and every other subcommand) never register
    assert catalog.resolve("git__worktree_lock") is None
    assert catalog.resolve("git__worktree_prune") is None


def test_worktree_specs_declare_their_effect_faces() -> None:
    """version 1, text-only, the install note; list read-only+idempotent,
    add/remove side-effecting+non-idempotent (a repeat must refuse)."""
    for spec in (
        git_module.GIT_WORKTREE_ADD_SPEC,
        git_module.GIT_WORKTREE_LIST_SPEC,
        git_module.GIT_WORKTREE_REMOVE_SPEC,
    ):
        assert spec.namespace == "git"
        assert spec.version == 1
        assert spec.supported_content_types == ["text"]
        assert "Requires the git binary" in spec.description
    assert git_module.GIT_WORKTREE_LIST_SPEC.side_effects is False
    assert git_module.GIT_WORKTREE_LIST_SPEC.idempotency_support is True
    for spec in (git_module.GIT_WORKTREE_ADD_SPEC, git_module.GIT_WORKTREE_REMOVE_SPEC):
        assert spec.side_effects is True
        assert spec.idempotency_support is False


def test_worktree_input_schemas_stay_in_the_validator_subset() -> None:
    for schema in (
        git_module.GIT_WORKTREE_ADD_INPUT_SCHEMA,
        git_module.GIT_WORKTREE_LIST_INPUT_SCHEMA,
        git_module.GIT_WORKTREE_REMOVE_INPUT_SCHEMA,
    ):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        allowed = {"type", "description", "properties", "required", "additionalProperties"}
        assert set(schema) <= allowed
        property_allowed = {"type", "description", "minimum", "maximum"}
        for property_schema in cast("dict[str, Any]", schema["properties"]).values():
            assert set(property_schema) <= property_allowed
            assert property_schema["type"] == "string"
    add_properties = cast("dict[str, Any]", git_module.GIT_WORKTREE_ADD_INPUT_SCHEMA["properties"])
    remove_properties = cast("dict[str, Any]", git_module.GIT_WORKTREE_REMOVE_INPUT_SCHEMA["properties"])
    assert set(add_properties) == {"path", "ref"}
    assert git_module.GIT_WORKTREE_ADD_INPUT_SCHEMA["required"] == ["path"]
    assert git_module.GIT_WORKTREE_LIST_INPUT_SCHEMA["properties"] == {}  # zero parameters
    assert "required" not in git_module.GIT_WORKTREE_LIST_INPUT_SCHEMA
    assert set(remove_properties) == {"path"}
    assert git_module.GIT_WORKTREE_REMOVE_INPUT_SCHEMA["required"] == ["path"]


def test_worktree_trio_defaults_in_the_permission_table(tmp_path: Path) -> None:
    """The three explicit ``_DEFAULT_EFFECTS`` keys evaluate to their faces:
    list allow (side_effects=False), add/remove ask (side_effects=True) --
    the zero-behavior closure documents the trio inside the closed
    enumeration (the out-of-table residual rule produced the same effects
    before the keys existed; T-C1-053 / bash__run row #28 precedents)."""
    policy = PermissionPolicy(boot_id="boot_git_wt")
    decision = policy.evaluate(
        session_id="ses_wt",
        action="git__worktree_list",
        resources=(),
        input_hash="a" * 64,
        working_dir=str(tmp_path),
        side_effects=False,
    )
    assert (decision.effect, decision.source) == ("allow", "default")
    for action in ("git__worktree_add", "git__worktree_remove"):
        decision = policy.evaluate(
            session_id="ses_wt",
            action=action,
            resources=(str(tmp_path / "wt"),),
            input_hash="a" * 64,
            working_dir=str(tmp_path),
            side_effects=True,
        )
        assert (decision.effect, decision.source) == ("ask", "default"), action


# ---------------------------------------------------------------------------
# conflict: precheck refusals (never silently overwrite) + native data face
# ---------------------------------------------------------------------------


async def test_worktree_add_refuses_non_empty_target_without_touching_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    target = repo / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    # Discovery is sabotaged: if the precheck ordering were wrong, git would
    # be missed and the outcome would be TOOL_FAILED, proving the precheck
    # refuses BEFORE any git process is spawned.
    monkeypatch.setattr(git_module, "_GIT_CACHE", {})
    monkeypatch.setattr(git_module, "_which_git", lambda: None)

    outcome = await _run(repo, "git__worktree_add", {"path": "occupied"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.CONFLICT
    conflicts = _conflicts(outcome)
    assert len(conflicts) == 1
    assert conflicts[0]["op"] == "add"
    assert Path(cast("str", conflicts[0]["path"])) == target.resolve()
    assert conflicts[0]["reason"] == "the target directory already exists and is not empty"
    structured = _structured(outcome)  # the three-part refusal: content + conflicts + error
    assert structured["cwd"] == str(repo)
    assert structured["roots"] == [str(repo)]
    assert "The operation was not attempted" in _one_text(outcome)
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"  # nothing overwritten


async def test_worktree_add_refuses_existing_file_target(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "file.txt").write_text("x\n", encoding="utf-8")

    outcome = await _run(repo, "git__worktree_add", {"path": "file.txt"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.CONFLICT
    conflicts = _conflicts(outcome)
    assert conflicts[0]["op"] == "add"
    assert conflicts[0]["reason"] == "the target exists and is not a directory"
    assert (repo / "file.txt").read_text(encoding="utf-8") == "x\n"  # the file survives


async def test_worktree_remove_refuses_dirty_worktree(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    assert (await _run(repo, "git__worktree_add", {"path": "wt-dirty"})).state == "succeeded"
    dirty = repo / "wt-dirty"
    (dirty / "untracked.txt").write_text("u\n", encoding="utf-8")

    outcome = await _run(repo, "git__worktree_remove", {"path": "wt-dirty"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.CONFLICT
    conflicts = _conflicts(outcome)
    assert len(conflicts) == 1
    assert conflicts[0]["op"] == "remove"
    assert Path(cast("str", conflicts[0]["path"])) == dirty.resolve()
    assert conflicts[0]["reason"] == "the worktree has modified or untracked files"
    assert dirty.is_dir() and (dirty / "untracked.txt").exists()  # never force-removed


async def test_worktree_remove_refuses_unverifiable_path_fail_closed(tmp_path: Path) -> None:
    """§5.3 L277: a path whose cleanliness cannot be verified refuses the change.

    The target lives under a working_dir that is inside NO repository, so the
    status probe itself exits 128 -- fail-closed, conflicts recorded."""
    _git_or_skip()
    plain = tmp_path / "plain"  # a directory inside NO repository
    target = plain / "maybe-wt"
    target.mkdir(parents=True)

    outcome = await _run(plain, "git__worktree_remove", {"path": "maybe-wt"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.CONFLICT
    conflicts = _conflicts(outcome)
    assert conflicts[0]["op"] == "remove"
    assert "cleanliness could not be verified" in cast("str", conflicts[0]["reason"])
    assert target.is_dir()  # untouched


async def test_worktree_remove_main_tree_native_refusal_is_data(tmp_path: Path) -> None:
    """The constructible second-layer face: the precheck passes (the main tree
    is clean) and git's own refusal is exit-code data -- never a tool error,
    never a conflicts entry (the boundary pinned by ruling e)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__worktree_remove", {"path": "."})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 128
    assert _conflicts(outcome) == []  # a git-layer refusal does not pollute conflicts
    assert "main working tree" in "\n".join(_texts(outcome))  # git's own fatal text, carried as content
    assert repo.is_dir()  # the main tree survives


async def test_worktree_add_option_shaped_ref_stays_positional_data(tmp_path: Path) -> None:
    """ref="-b" rides after the ``--`` separator: git rejects it as a
    reference (exit 128, data face), never as an option -- and nothing is
    created."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__worktree_add", {"path": "wt-opt", "ref": "-b"})

    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 128
    assert "invalid reference" in "\n".join(_texts(outcome))
    assert not (repo / "wt-opt").exists()
    assert _conflicts(outcome) == []


# ---------------------------------------------------------------------------
# failure: rejections, missing git, non-repository data face, path boundaries
# ---------------------------------------------------------------------------


async def test_worktree_add_rejects_out_of_root_absolute_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()

    outcome = await _run(repo, "git__worktree_add", {"path": str(outside)})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED


async def test_worktree_remove_rejects_traversal_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__worktree_remove", {"path": "../escape"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED


async def test_worktree_rejects_nul_byte_path(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__worktree_add", {"path": "a\x00b"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.VALIDATION_ERROR


async def test_worktree_remove_missing_in_root_path_is_not_found(tmp_path: Path) -> None:
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    outcome = await _run(repo, "git__worktree_remove", {"path": "no-such"})

    assert outcome.state == "failed"
    assert outcome.error is not None and outcome.error.code is CoreErrorCode.NOT_FOUND


async def test_worktree_empty_path_string_is_an_explicit_error(tmp_path: Path) -> None:
    """The required-path variant: ``""`` is an explicit error for add/remove,
    never the 053 omitted-means-whole-tree face (that face is pathspec-domain)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    for name in ("git__worktree_add", "git__worktree_remove"):
        outcome = await _run(repo, name, {"path": ""})
        assert outcome.state == "failed", name
        assert outcome.error is not None and outcome.error.code is CoreErrorCode.VALIDATION_ERROR, name
        assert "requires a non-empty 'path' string" in _one_text(outcome), name


async def test_worktree_required_and_unknown_fields_fail_strict_validation(tmp_path: Path) -> None:
    """The invoker's strict schema face: a missing required path and an
    unknown field on the zero-parameter list both fail before the adapter."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")

    missing = await _run(repo, "git__worktree_add", {})
    assert missing.state == "failed"
    assert missing.error is not None and missing.error.code is CoreErrorCode.VALIDATION_ERROR
    assert "required field is missing" in _one_text(missing)

    unknown = await _run(repo, "git__worktree_list", {"path": "x"})
    assert unknown.state == "failed"
    assert unknown.error is not None and unknown.error.code is CoreErrorCode.VALIDATION_ERROR
    assert "unknown field" in _one_text(unknown)


async def test_worktree_missing_binary_fails_with_install_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_module, "_GIT_CACHE", {})
    monkeypatch.setattr(git_module, "_which_git", lambda: None)
    plain = tmp_path / "plain"
    plain.mkdir()

    calls: list[tuple[str, JsonObject]] = [
        ("git__worktree_add", {"path": "fresh"}),
        ("git__worktree_list", {}),
        ("git__worktree_remove", {"path": "."}),
    ]
    for name, input in calls:
        outcome = await _run(plain, name, input)

        assert outcome.state == "failed", name
        assert outcome.error is not None and outcome.error.code is CoreErrorCode.TOOL_FAILED, name
        assert "not found on PATH" in outcome.error.message, name
        assert "Install a working git" in outcome.error.message, name
        assert "no silent fallback" in outcome.error.message, name


async def test_worktree_list_outside_repository_exit_128_is_data(tmp_path: Path) -> None:
    _git_or_skip()
    plain = tmp_path / "plain"  # a directory inside NO repository
    plain.mkdir()

    outcome = await _run(plain, "git__worktree_list", {})

    # Ruling e: a completed command with any exit code is data, not a tool failure.
    assert (outcome.state, outcome.effect_status) == ("succeeded", "completed")
    assert outcome.error is None
    assert _structured_exit(outcome) == 128
    assert "not a git repository" in "\n".join(_texts(outcome))
    structured = _structured(outcome)
    assert structured["cwd"] == str(plain)
    assert structured["roots"] == [str(plain)]
    assert structured["conflicts"] == []


# ---------------------------------------------------------------------------
# platform: separators, drive-letter case, forward-slash input
# ---------------------------------------------------------------------------


async def test_worktree_accepts_forward_slash_relative_path(tmp_path: Path) -> None:
    """A forward-slash relative path resolves on every platform (native on
    POSIX; a first-class relative form on Windows)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    (repo / "sub").mkdir()

    outcome = await _run(repo, "git__worktree_add", {"path": "sub/wt-fwd"})

    assert outcome.state == "succeeded"
    assert (repo / "sub" / "wt-fwd" / ".git").is_file()


async def test_worktree_accepts_absolute_native_separator_path(tmp_path: Path) -> None:
    """``str(Path(...))`` input (backslashes on Windows) resolves and creates."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    target = repo / "wt-abs"

    outcome = await _run(repo, "git__worktree_add", {"path": str(target)})

    assert outcome.state == "succeeded"
    assert (target / ".git").is_file()


@pytest.mark.skipif(_PLATFORM != "win32", reason="drive-letter case folding is a Windows filesystem face")
async def test_worktree_windows_drive_letter_case_resolves(tmp_path: Path) -> None:
    """A path differing only in drive-letter case resolves (normcase folding)."""
    _git_or_skip()
    repo = _init_repo(tmp_path / "repo")
    raw = str(repo / "wt-case")
    swapped = raw[0].lower() + raw[1:]  # C:\... -> c:\... (or c -> C on a lowercased drive)

    outcome = await _run(repo, "git__worktree_add", {"path": swapped})

    assert outcome.state == "succeeded"
    assert (repo / "wt-case" / ".git").is_file()
