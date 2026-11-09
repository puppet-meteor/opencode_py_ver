"""The SSE consumption face of the SDK (P2-S5 / M5-C, §7 L105 client side).

``GET /session/{session_id}/events`` streams the durable log as SSE: event
frames carry ``id: {session_id}:{seq}`` (the client resume cursor) plus the
eight-field DurableEvent envelope as one compact JSON ``data:`` line; the
catch-up marker renders as the comment ``: synced {H}`` and an idle stream
emits ``: keepalive`` comments. :func:`stream_durable_events` turns that byte
stream into typed :class:`~opencode.contracts.events.DurableEvent` objects:

* the four wire forms (``id:``/``event:``/``data:``/``: comment``) are parsed
  per the SSE rules — one optional leading space after the field colon is
  stripped (the 056 harness rule, a spec behavior not a server quirk);
* comments (``: keepalive``, ``: synced {H}``) are skipped: the minimal face
  yields events only, and the Synced marker never advances the resume cursor
  anyway (it carries no ``id:`` line);
* event frames decode through the default contracts registry (unknown
  type/version fail closed, never silently skipped);
* envelope ``evt_`` ids are deduplicated within one stream's lifecycle: the
  server replays strictly after the cursor, so a duplicate is a wire breach
  the SDK absorbs rather than propagating — reconnection stays the caller's
  business;
* every event frame's ``id:`` value updates the caller's cursor callback
  *before* dedup, so the exposed resume cursor never lags a frame already on
  the wire;
* the terminal ``event: error`` frame raises :class:`OpencodeStreamError` and
  ends the stream (the retryable=false faces should not be blindly
  reconnected — the 056 note carries over).

The first version is a single-connection generator: no auto-reconnect
helper (reconnection is exercised by the tests holding the cursor
explicitly), no ephemeral replay, and the underlying socket honors the
per-request timeout (a healthy stream is paced by the server's keepalives,
so any timeout above the keepalive interval never fires on a live server).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from pydantic import ValidationError

from opencode.contracts.errors import CoreError
from opencode.contracts.events import DurableEvent, EventDecodeError, EventRegistry
from opencode.sdk.errors import OpencodeClientError, OpencodeStreamError

__all__ = ["stream_durable_events"]


def _decode_line(raw: bytes) -> bytes:
    """One wire line with its terminator removed (``\\n``, optional ``\\r``)."""
    line = raw[:-1] if raw.endswith(b"\n") else raw
    return line[:-1] if line.endswith(b"\r") else line


def _stream_error(data_lines: list[str]) -> OpencodeStreamError:
    """The terminal ``event: error`` frame as :class:`OpencodeStreamError`."""
    try:
        core = CoreError.model_validate(json.loads("\n".join(data_lines)))
    except (ValueError, ValidationError) as error:
        raise OpencodeClientError("the SSE error frame did not carry the §8 CoreError shape") from error
    return OpencodeStreamError(core)


def _decode_frame(
    frame_id: str | None,
    frame_event: str | None,
    data_lines: list[str],
    *,
    registry: EventRegistry,
) -> DurableEvent[Any] | None:
    """One complete SSE block as a typed event, or ``None`` when it is not one.

    The server emits exactly three block forms: the event frame (``id:`` +
    ``event:`` + one ``data:`` JSON line) and the two comment blocks
    (``: synced {H}``, ``: keepalive``) — the comments skip, everything else
    is a wire contract breach and raises instead of being silently dropped.
    """
    if frame_event == "error":
        raise _stream_error(data_lines)
    if frame_id is None and frame_event is None and not data_lines:
        return None  # a comment-only block (keepalive / synced): skipped
    if frame_id is None or not data_lines:
        raise OpencodeClientError(
            f"an SSE event frame needs its id and data lines "
            f"(id={frame_id!r}, event={frame_event!r}, data={data_lines!r})"
        )
    try:
        event = registry.decode_durable("\n".join(data_lines))
    except EventDecodeError as error:
        raise OpencodeClientError(f"an SSE event frame did not decode: {error}") from error
    return event


def stream_durable_events(
    lines: Iterable[bytes],
    *,
    registry: EventRegistry,
    on_cursor: Callable[[str], None],
) -> Iterator[DurableEvent[Any]]:
    """Yield typed durable events from one SSE byte stream (see module docstring).

    ``lines`` is the open response body iterated line by line (urllib's
    ``HTTPResponse`` iterates by line); ``on_cursor`` receives every event
    frame's ``id:`` value — the Last-Event-ID resume cursor — as frames are
    parsed, before dedup. The generator is pull-shaped: one line is read per
    ``next()``, and the caller owns closing the underlying response.
    """
    seen: set[str] = set()
    frame_id: str | None = None
    frame_event: str | None = None
    data_lines: list[str] = []
    for raw in lines:
        line = _decode_line(raw)
        if not line:
            # blank line: one complete frame is on hand — dispatch it
            event = _decode_frame(frame_id, frame_event, data_lines, registry=registry)
            if event is not None:
                if frame_id is not None:
                    on_cursor(frame_id)
                if event.id not in seen:
                    seen.add(event.id)
                    yield event
            frame_id = None
            frame_event = None
            data_lines = []
            continue
        if line.startswith(b":"):
            continue  # comment (keepalive / synced): skipped, the minimal face
        field, separator, value = line.partition(b":")
        if not separator:
            raise OpencodeClientError(f"malformed SSE line (no field colon): {line!r}")
        if value.startswith(b" "):
            value = value[1:]  # one optional leading space after the colon
        if field == b"id":
            frame_id = value.decode("utf-8")
        elif field == b"event":
            frame_event = value.decode("utf-8")
        elif field == b"data":
            data_lines.append(value.decode("utf-8"))
        else:
            raise OpencodeClientError(f"unknown SSE field {field!r} (the wire emits id/event/data/comments only)")
