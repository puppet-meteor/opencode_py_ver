"""Builtin ``fs__write`` tool (R3-S4; CORE-CONTRACTS §5.3 L272/L274-275, plan §2.4, PHASE1-SLIM §4 L91).

Writes a UTF-8 text file inside the working directory with the §5.3 L274
atomic-replace discipline: the payload goes to a same-directory temporary
file (``tempfile.mkstemp`` — O_CREAT|O_EXCL, unique name, same volume so
the final rename never crosses devices), is flushed and fsynced, then
swapped in with ``os.replace`` (POSIX ``rename(2)``; Windows
``MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)`` — the documented atomic
overwrite of an existing file). Any failure before the swap unlinks the
temporary: the target only ever holds its old bytes or its new bytes,
never a partial write. Missing parent directories are created inside the
root first (write.ts L52 semantics).

Path containment extends the R0-B verified scheme (read.py
``_resolve_within_root``) to creation targets: an existing target resolves
through the full strict-realpath scheme (NUL check → lexical resolve →
containment → ``realpath(strict)`` → containment of the realpathed root,
with the symlink/junction escape denials); a not-yet-existing target
strict-realpaths its deepest existing ancestor, re-checks that ancestor's
containment, and appends the remaining plain components (which cannot be
symlinks yet). §5.3 L275: the checks are a local trusted-workspace misuse
guard, not a defense against adversarial concurrent link replacement.

TS mapping (``packages/core/src/tool/plugin/write.ts``, 101L): the
``path``/``content`` input face (L20-24), missing-parent creation and the
"Writes a file ..., overwriting if one exists" description (L51-53), the
``existed`` distinction in ``toModelContent`` ("Wrote"/"Created file
successfully", L33/L57). Deviations, quantified (float-reported, progress
file): no BOM management (the TS ``Bom.split``/``join`` pair belongs to
the TS read face; the Python ``fs__read`` decodes raw UTF-8 without BOM
stripping, so write/edit treat ``\\ufeff`` as ordinary text and
round-trips stay consistent); no formatter hook (TS ``formatter.file`` —
no Python formatter runtime exists in Phase 1); no file-diff preview and
no structured output (the read quartet precedent is text-only;
file-diff.ts is not ported — progress file ①); ``WRITE_MAX_BYTES`` = 1 MiB
is a Python-side safety cap (write.ts has no size check; the §5.3 L276
argument-stream cap already bounds the normal path — the trio's
hard-cap precedent).

Filesystem access stays synchronous like the read quartet: Phase-1 tool
calls execute serially under the registry lock (PHASE1-SLIM L85), and the
byte cap bounds each write's cost.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _contains, _failure, _resolve_within_root

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (read.py does the same).
    from opencode.tools.invoker import ToolContext

WRITE_NAMESPACE = "fs"
WRITE_NAME = "write"
WRITE_VERSION = 1

#: Python-side write cap: the encoded content must fit. write.ts carries no
#: size check (no TS number exists to align with); the 64 KiB §5.3 L276
#: argument-stream cap already bounds the normal path, so this is the
#: defensive backstop, sized at the §5.3 L272 1 MiB read window — a file
#: written by ``fs__write`` is always fully readable back (float-reported).
WRITE_MAX_BYTES = 1024 * 1024

WRITE_DESCRIPTION = (
    "Writes a file inside the working directory, overwriting if one exists.\n\n"
    "Missing parent directories are created automatically.\n\n"
    "Use this tool to create new files or overwrite existing files. For partial changes, "
    "use the edit tool instead."
)

WRITE_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Write a UTF-8 text file resolved inside the working directory.",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to write, relative to the working directory "
            "(an absolute path must resolve inside it)",
        },
        "content": {
            "type": "string",
            "description": "Content to write to the file",
        },
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

WRITE_SPEC = ToolSpec(
    namespace=WRITE_NAMESPACE,
    name=WRITE_NAME,
    version=WRITE_VERSION,
    description=WRITE_DESCRIPTION,
    input_schema=WRITE_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=True,
    idempotency_support=True,
)


# ---------------------------------------------------------------------------
# Target resolution: the R0-B scheme, extended for creation targets.
# ---------------------------------------------------------------------------


def _resolve_write_target(root: str, user_path: str) -> tuple[str | None, str | None]:
    """Resolve a write target under ``root`` (§5.3 L274-275).

    Existing targets go through the full R0-B strict-realpath scheme
    (read.py ``_resolve_within_root``). Not-yet-existing targets
    strict-realpath the deepest existing ancestor, re-check its containment
    against the realpathed root, and append the remaining plain components
    — a component that does not exist yet cannot be a symlink, and §5.3
    L275 scopes the residual races (concurrent link replacement) outside
    the guarantee. Returns ``(real_path, None)`` on success or
    ``(None, reason)`` with the R0-B reason string on rejection.
    """
    if "\x00" in user_path:
        return None, "nul_byte_in_path"
    absolute = os.path.normpath(os.path.join(root, user_path))
    if not _contains(root, absolute):
        return None, "escapes_workspace_before_realpath"
    if os.path.lexists(absolute):
        # An existing target (including a dangling symlink, whose strict
        # realpath fails -> not found) uses the unmodified R0-B scheme.
        return _resolve_within_root(root, user_path)
    try:
        real_root = os.path.realpath(root, strict=True)
    except OSError:
        return None, "realpath_failed_or_missing"
    ancestor = absolute
    tail: list[str] = []
    while not os.path.lexists(ancestor):
        parent, name = os.path.split(ancestor)
        if parent == ancestor:
            # Unreachable while the root exists (absolute is contained in
            # it); kept as the loop's structural stop, fail closed.
            return None, "realpath_failed_or_missing"
        tail.append(name)
        ancestor = parent
    try:
        real_ancestor = os.path.realpath(ancestor, strict=True)
    except OSError:
        return None, "realpath_failed_or_missing"
    if not _contains(real_root, real_ancestor):
        return None, "escapes_workspace_after_realpath"
    tail.reverse()
    return os.path.join(real_ancestor, *tail), None


# ---------------------------------------------------------------------------
# The §5.3 L274 atomic replace (shared with fs__edit).
# ---------------------------------------------------------------------------


def _atomic_replace(real: str, data: bytes) -> None:
    """Write ``data`` to ``real`` via a same-directory temp file + ``os.replace``.

    ``tempfile.mkstemp`` creates the temp file with O_CREAT|O_EXCL in the
    target's own directory (same volume — the swap never crosses devices;
    a unique name that cannot clobber an existing file or symlink). The
    payload is flushed and fsynced before the swap, and any failure before
    ``os.replace`` unlinks the temp: the target keeps its old bytes until
    the single atomic rename lands the new ones. On Windows ``os.replace``
    is ``MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)`` — a target held open
    by another process without FILE_SHARE_DELETE surfaces as
    ``PermissionError`` (an honest failure, never a half-written file).
    """
    handle_fd, temp = tempfile.mkstemp(prefix=".opencode-fs-", suffix=".tmp", dir=os.path.dirname(real))
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, real)
    except BaseException:
        # No intermediate state on any failure path: the target is
        # untouched and the temporary is removed (a hard kill between the
        # temp write and the swap can still strand a temp file — the
        # target itself stays intact; disclosed in the progress file).
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _reject(path: str, reason: str) -> ToolResult:
    """Map a containment rejection to its §8 error code (read.py ``_rejection`` family)."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"File not found: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against WRITE_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts below are
    # static narrows, not conversions. A caller bypassing the Invoker gets
    # a TypeError here that the Invoker's exception path turns into a
    # model-readable failed result -- fail closed either way.
    path = cast(str, input["path"])
    content = cast(str, input["content"])

    real, reason = _resolve_write_target(context.working_dir, path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _reject(path, reason)
    if os.path.isdir(real):
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, f"Path is a directory, not a file: {path}")

    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        # Defensive: lone surrogates cannot pass the Invoker's canonical
        # input hash (frozen face), so this branch is protocol-unreachable
        # — kept fail-closed for direct callers (④ disclosure).
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Cannot write {path}: content is not encodable as UTF-8")
    if len(data) > WRITE_MAX_BYTES:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Cannot write {path}: content is {len(data)} bytes, over the {WRITE_MAX_BYTES} byte write limit",
        )

    existed = os.path.isfile(real)
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        _atomic_replace(real, data)
    except OSError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot write {path}: {error}")

    verb = "Wrote" if existed else "Created"
    return ToolResult(content=[ToolTextContent(type="text", text=f"{verb} file successfully: {path}")])


class FsWriteAdapter:
    """The ``fs__write`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the write itself is synchronous
    by design (module docstring: serial Phase-1 execution + the byte cap
    bound each write's cost).
    """

    @property
    def spec(self) -> ToolSpec:
        return WRITE_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input)
