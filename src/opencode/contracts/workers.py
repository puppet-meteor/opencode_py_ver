"""The worker structured-return wire contract (PHASE3 S3; CORE-CONTRACTS §5.4, T-C1-078).

An ``opencode_agent`` node executes its work in a detached git worktree via a
real CLI subprocess (the *worker*). The worker's answer is not free text: the
last ``[final] `` marker on its stdout must carry a single-line JSON object of
this shape (CORE-CONTRACTS §5.4 worker protocol). Parsing/validating that
object is the manager's job (``opencode.orchestrator.workers``); this module
pins only the wire shape:

* ``schema_version`` (int, bool rejected before coercion per CORE-CONTRACTS §1);
* ``summary`` (non-empty str) -- the worker's own one-paragraph report;
* ``files_changed`` / ``artifacts`` (lists of *worktree-relative* paths; the
  validators reject absolute, backslash-separated, empty, or ``..``-escaping
  entries, because the acceptance gate matches them against the node's
  authorized-files whitelist);
* ``tests_passed`` (bool) -- the worker's self-reported gate outcome.

Path *semantics* beyond shape (whitelist matching, existence, containment) are
manager-layer rules -- the layered-gate split mirrors the pipeline-spec
contract (structure here, semantics in the engine/manager).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The path-list fields validated by ``_validate_relative_paths`` below.
_RELATIVE_PATH_FIELDS = ("files_changed", "artifacts")


def _reject_bool(field: str, value: Any) -> Any:
    """CORE-CONTRACTS §1 repo rule: integer-semantic fields reject bools pre-coercion.

    Local twin of the events/messages helpers (each contracts module keeps the
    repo rule self-contained; the behavior is identical and pinned by tests).
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    return value


def _validate_relative_paths(field: str, value: list[str]) -> list[str]:
    """Enforce worktree-relative POSIX-style paths for a path-list field.

    The acceptance gate matches these entries against the node's authorized
    whitelist using pure relative-path prefix logic, so anything that could
    escape that match -- an absolute path (POSIX ``/`` root or a Windows drive
    ``C:``), a backslash separator (Windows-only, ambiguous under the POSIX
    join), an empty entry, or a ``..`` segment -- is a structural rejection
    here rather than a surprise at gate time.
    """
    for entry in value:
        if not entry:
            raise ValueError(f"{field} entries must be non-empty")
        if entry.startswith("/"):
            raise ValueError(f"{field} entries must be worktree-relative, got absolute path: {entry!r}")
        if len(entry) >= 2 and entry[1] == ":":
            raise ValueError(f"{field} entries must be worktree-relative, got drive path: {entry!r}")
        if "\\" in entry:
            raise ValueError(f"{field} entries must use '/' separators, got backslash: {entry!r}")
        parts = entry.split("/")
        if any(part == ".." for part in parts):
            raise ValueError(f"{field} entries must not escape the worktree, got '..' segment: {entry!r}")
    return value


class WorkerReturn(BaseModel):
    """The structured return a worker must emit as its final stdout marker.

    The manager parses the *last* ``[final] `` line on worker stdout, strips
    the display-escaping the CLI applies, ``json.loads`` the single line, and
    validates it here. Any failure on that chain is a ``WorkerError`` -- the
    node fails closed; there is no best-effort fallback to raw text.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    summary: str = Field(min_length=1)
    files_changed: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    tests_passed: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_not_bool(cls, value: Any) -> Any:
        return _reject_bool("schema_version", value)

    @field_validator(*_RELATIVE_PATH_FIELDS)
    @classmethod
    def _paths_relative(cls, value: list[str], info: Any) -> list[str]:
        return _validate_relative_paths(info.field_name or "", value)
