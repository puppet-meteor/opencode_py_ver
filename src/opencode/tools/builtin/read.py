"""Builtin ``fs__read`` tool (R2-S5; CORE-CONTRACTS §5.3 L272-273/L276, plan §2 R2-S5 ③).

Text file reads over the R0-B verified path-containment scheme
(``tools/r0/platform_probe.py`` L513-539, itself derived from
``filesystem.ts:133-146``): NUL check → lexical resolve → containment →
``realpath(strict)`` → containment of the realpathed root. R2 has exactly
one allowed root, the session's ``working_dir`` (§5.1 L255); R3 adds the
config-declared roots (float report).

TS mapping (``packages/core/src/tool/plugin/read.ts`` / ``read-filesystem.ts``):
``name`` L15, the offset/limit ``PageInput`` semantics, the per-line clamp
at 2000 chars (``MAX_LINE_LENGTH``/``MAX_LINE_SUFFIX`` L15-17), the
``Offset {n} is out of range`` wording (L33-38), ``Cannot read binary
file`` (L20-25), ``Path is not a file`` (L42-47), and the model rendering
``Read file {path}, lines a-b`` / ``{n}: {line}`` / the
``Continue reading with offset: N`` marker (``toModelContent`` L169).
Deviations from TS (plan §5.3 numbers, float-reported): the read window is
1 MiB from file start (not TS's 50 KiB page budget + rope), the output
budget is 64 KiB, the output-truncation marker reports the shown/total
byte sizes, invalid UTF-8 fails the read (TS's TextDecoder replaces with
U+FFFD), and directories are rejected as UnsupportedCapability (no
list-page support in R2).

Filesystem access stays in synchronous helpers: Phase-1 tool calls execute
serially under the registry lock (PHASE1-SLIM L85), so blocking reads
cannot starve another tool call, and the 1 MiB window bounds each read's
cost. Moving reads to a thread executor is deferred (float report).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from opencode.contracts.errors import CoreError, CoreErrorCode
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec

if TYPE_CHECKING:
    # Annotation-only: importing the runtime package here would create an
    # import cycle through the registry (invoker.py does the same).
    from opencode.tools.invoker import ToolContext

READ_NAMESPACE = "fs"
READ_NAME = "read"
READ_VERSION = 1

#: read-filesystem.ts L11/L15: page length and per-line clamp.
MAX_READ_LINES = 2_000
MAX_LINE_LENGTH = 2_000
#: read-filesystem.ts L16-17: the suffix marking a clamped line.
MAX_LINE_SUFFIX = f"... (line truncated to {MAX_LINE_LENGTH} chars)"

READ_DESCRIPTION = (
    "Read the contents of a text file inside the working directory. Each line is prefixed by its "
    "1-based line number as <line>: <content>; the prefix is for reference and is not part of the "
    "file content. Use offset and limit to read large files in sections, and prefer one larger "
    "read over many small slices."
)

READ_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": "Read a UTF-8 text file resolved inside the working directory.",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to read, relative to the working directory (an absolute path must resolve inside it)",
        },
        "offset": {
            "type": "integer",
            "minimum": 1,
            "description": "The line to start reading from (1-based)",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "The maximum number of lines to read (defaults to and capped at 2000)",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

READ_SPEC = ToolSpec(
    namespace=READ_NAMESPACE,
    name=READ_NAME,
    version=READ_VERSION,
    description=READ_DESCRIPTION,
    input_schema=READ_INPUT_SCHEMA,
    supported_content_types=["text"],
    side_effects=False,
    idempotency_support=True,
)


# ---------------------------------------------------------------------------
# Path resolution: the R0-B verified scheme, copied verbatim.
# ---------------------------------------------------------------------------


def _contains(base: str, target: str) -> bool:
    """R0-B verified containment (platform_probe.py L513-517)."""
    base_norm = os.path.normcase(os.path.normpath(base))
    target_norm = os.path.normcase(os.path.normpath(target))
    base_prefix = base_norm if base_norm.endswith(os.sep) else base_norm + os.sep
    return target_norm == base_norm or target_norm.startswith(base_prefix)


def _resolve_within_root(root: str, user_path: str) -> tuple[str | None, str | None]:
    """R0-B verified order (platform_probe.py ``resolve_user_path`` L520-539).

    NUL bytes → lexical resolve → containment → ``realpath(strict)`` →
    containment of the realpathed root. Returns ``(real_path, None)`` on
    success or ``(None, reason)`` with the R0-B reason string on rejection.
    """
    if "\x00" in user_path:
        return None, "nul_byte_in_path"
    absolute = os.path.normpath(os.path.join(root, user_path))
    if not _contains(root, absolute):
        return None, "escapes_workspace_before_realpath"
    try:
        real = os.path.realpath(absolute, strict=True)
    except OSError:
        return None, "realpath_failed_or_missing"
    real_root = os.path.realpath(root, strict=True)
    if not _contains(real_root, real):
        return None, "escapes_workspace_after_realpath"
    return real, None


# ---------------------------------------------------------------------------
# Windowed read and page rendering (read-filesystem.ts textPage/toModelContent).
# ---------------------------------------------------------------------------


def _read_window(path: str, max_bytes: int) -> tuple[bytes, int, bool]:
    """First ``max_bytes`` of the file plus the real size and truncation flag.

    Reading ``max_bytes + 1`` makes "larger than the window" a local decision
    (no stat/reopen race); the size comes from the same open handle.
    """
    with open(path, "rb") as handle:
        file_size = os.fstat(handle.fileno()).st_size
        window = handle.read(max_bytes + 1)
    if len(window) > max_bytes:
        return window[:max_bytes], file_size, True
    return window, file_size, False


def _clamp_line(line: str) -> str:
    """read-filesystem.ts ``clampLine`` L330: cap each rendered line's content."""
    return line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH] + MAX_LINE_SUFFIX


def _split_lines(window: str) -> list[str]:
    """read-filesystem.ts ``textPage`` L258-261: newline handling.

    A trailing newline ends the last line instead of starting an empty one;
    a lone ``\\r`` before ``\\n`` is stripped (CRLF files read line-by-line).
    """
    lines = window.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _select_page(
    lines: list[str],
    offset: int,
    limit: int,
    max_output_bytes: int,
) -> tuple[list[str], int | None, int, int]:
    """read-filesystem.ts ``textPage`` L251-276: the limit + byte-budget page.

    Returns ``(selected, next_offset, shown_bytes, total_bytes)``. The byte
    counts cover the clamped lines joined with ``"\\n"`` -- i.e. the output
    without the cap -- so the truncation marker reports real sizes (plan
    §5.3: explicit truncation + original size). TS's redundant
    ``size >= MAX`` pre-check is subsumed by the ``size + line_size >
    max_output_bytes`` test for every budget ≥ 1 byte.
    """
    clamped = [_clamp_line(line) for line in lines[offset - 1 :]]
    total_bytes = len("\n".join(clamped).encode("utf-8")) if clamped else 0
    selected: list[str] = []
    size = 0
    next_offset: int | None = None
    for index, text in enumerate(clamped):
        line_size = len(text.encode("utf-8")) + (1 if selected else 0)
        if len(selected) >= limit or size + line_size > max_output_bytes:
            next_offset = offset + index
            break
        selected.append(text)
        size += line_size
    return selected, next_offset, size, total_bytes


def _render(
    path: str,
    offset: int,
    selected: list[str],
    next_offset: int | None,
    shown_bytes: int,
    total_bytes: int,
    *,
    window_bytes: int,
    file_size: int,
    window_truncated: bool,
) -> str:
    """read.ts ``toModelContent`` L169: numbered page + explicit truncation markers."""
    parts: list[str] = []
    if not selected:
        parts.append(f"Read file {path}, 0 lines")
    else:
        parts.append(f"Read file {path}, lines {offset}-{offset + len(selected) - 1}")
    parts.extend(f"{offset + index}: {line}" for index, line in enumerate(selected))
    if next_offset is not None:
        parts.append(
            f"[Output truncated: showing {shown_bytes} of {total_bytes} bytes. "
            f"Continue reading with offset: {next_offset}]"
        )
    if window_truncated:
        parts.append(f"[File truncated: showing the first {window_bytes} of {file_size} bytes]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


def _failure(code: CoreErrorCode, message: str) -> ToolResult:
    """A graceful adapter failure: model-readable text + §8 error (§7 L341)."""
    return ToolResult(
        content=[ToolTextContent(type="text", text=message)],
        error=CoreError(code=code, message=message, retryable=False),
    )


def _rejection(path: str, reason: str) -> ToolResult:
    """Map an R0-B containment rejection to its §8 error code."""
    if reason == "nul_byte_in_path":
        return _failure(CoreErrorCode.VALIDATION_ERROR, f"Path contains NUL bytes: {path}")
    if reason == "realpath_failed_or_missing":
        return _failure(CoreErrorCode.NOT_FOUND, f"File not found: {path}")
    # escapes_workspace_before_realpath / escapes_workspace_after_realpath
    return _failure(CoreErrorCode.PERMISSION_DENIED, f"Path escapes the working directory: {path}")


def _execute(context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against READ_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the casts below are
    # static narrows, not conversions. A caller bypassing the Invoker gets
    # a TypeError here that the Invoker's exception path turns into a
    # model-readable failed result -- fail closed either way.
    path = cast(str, input["path"])
    offset = cast(int, input.get("offset", 1))
    limit = min(cast(int, input.get("limit", MAX_READ_LINES)), MAX_READ_LINES)

    real, reason = _resolve_within_root(context.working_dir, path)
    if real is None:
        assert reason is not None  # the resolver always returns a reason on rejection
        return _rejection(path, reason)
    if not os.path.isfile(real):
        return _failure(CoreErrorCode.UNSUPPORTED_CAPABILITY, f"Path is not a file: {path}")

    window_bytes, file_size, window_truncated = _read_window(real, context.limits.max_read_bytes)
    if b"\x00" in window_bytes:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot read binary file: {path}")
    try:
        window_text = window_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Cannot read {path}: file is not valid UTF-8 text")

    lines = _split_lines(window_text)
    if offset > max(len(lines), 1):
        count = len(lines)
        window_note = f" (reading only the first {len(window_bytes)} of {file_size} bytes)" if window_truncated else ""
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"Offset {offset} is out of range for this file ({count} {'line' if count == 1 else 'lines'}){window_note}",
        )

    selected, next_offset, shown_bytes, total_bytes = _select_page(
        lines, offset, limit, context.limits.max_read_output_bytes
    )
    text = _render(
        path,
        offset,
        selected,
        next_offset,
        shown_bytes,
        total_bytes,
        window_bytes=len(window_bytes),
        file_size=file_size,
        window_truncated=window_truncated,
    )
    return ToolResult(content=[ToolTextContent(type="text", text=text)])


class FsReadAdapter:
    """The ``fs__read`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    ``execute`` is a thin async wrapper -- the read itself is synchronous
    by design (see module docstring: serial Phase-1 execution + bounded
    window make blocking reads safe).
    """

    @property
    def spec(self) -> ToolSpec:
        return READ_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        return _execute(context, input)
