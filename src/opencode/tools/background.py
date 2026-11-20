"""Background jobs: persistence + restart replay (T-C3-002, upstream 2.3.2 job).

Upstream ``core/src/job.ts`` + ``job/{sql,recovery}.ts`` mechanism, first
period scope per doc/plans/port-232-increments.md T-C3-002:

- ``backgrounded 即落行`` -- the row is upserted the moment a call
  backgrounds (upstream ``start(background)`` runs before the fork), so
  a crash at any later point still leaves a replayable row;
- settle -- the background continuation upserts the terminal payload
  (model-visible text + structured output, already bounded by the
  adapter's self-managed cap);
- admit -- replay delivery deletes the row (upstream
  ``completeBackground``/inbox admit); session cancellation deletes the
  session's rows (upstream ``cancelSession``);
- restart replay (upstream ``session/execution/restart.ts`` L370-425,
  the shell slice) -- settled rows replay their payload as a synthetic
  notification; a *running* shell row cannot reattach in-process (py
  has no shell daemon), so it replays as an honest error naming the
  server restart, never a fake success.

This module owns ONLY the durable face; the runtime seam (threshold
wait, marker outcome, settlement continuation) lives in
``opencode.tools.invoker`` because it needs the finish/normalize
pipeline -- background.py must not import the invoker (layering).

Timestamps are epoch-ms integers (the ``txn.epoch_ms`` face); JSON
payloads use the repo's ``JsonObject`` contract.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from opencode.contracts.messages import JsonObject
from opencode.storage.db import Database

#: The sqlite table (migration 0005); the eval harness pins this name.
TABLE = "background_job"

#: Recovery descriptor families (upstream job kind). First period: shell.
KIND_SHELL = "shell"

#: Statuses. ``backgrounded`` = running (upserted at the backgrounding
#: moment); the rest are terminal.
STATUS_BACKGROUNDED = "backgrounded"
STATUS_SUCCEEDED = "succeeded"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
_TERMINAL = frozenset({STATUS_SUCCEEDED, STATUS_ERROR, STATUS_CANCELLED})

#: The tool namespaces that background past the threshold (first period
#: shell only; subagent arrives with T-C3-005).
BACKGROUNDABLE_NAMESPACES = frozenset({"bash"})

_RESTART_ERROR_TEXT = (
    "Command {title!r} was interrupted by a server restart and did not "
    "complete; its effect is unknown. Re-run it if the change window still "
    "needs the result."
)


def notification_id_for(tool_call_id: str) -> str:
    """The replay key for a call's notification (upstream: one per tool call)."""
    return f"ntf_{tool_call_id}"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class BackgroundJobRow:
    """One pending notification's durable state (upstream job row)."""

    notification_id: str
    session_id: str
    tool_call_id: str
    kind: str
    status: str
    title: str
    payload: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class ReplayedNotification:
    """A replayed synthetic notification (boot-time delivery descriptor)."""

    notification_id: str
    session_id: str
    tool_call_id: str
    kind: str
    status: str
    text: str


@dataclass(slots=True)
class _StoredRow:
    """Internal read model: the row plus its bookkeeping timestamps."""

    row: BackgroundJobRow
    created_at_ms: int = 0
    updated_at_ms: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


def _to_stored(fetched: Any) -> _StoredRow:
    notification_id, session_id, tool_call_id, kind, status, title, payload, created_ms, updated_ms = fetched
    payload_json: JsonObject | None = None
    if isinstance(payload, str) and payload:
        try:
            decoded = json.loads(payload)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            payload_json = decoded
    elif isinstance(payload, dict):
        payload_json = payload
    return _StoredRow(
        row=BackgroundJobRow(
            notification_id=notification_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            kind=kind,
            status=status,
            title=title,
            payload=payload_json,
        ),
        created_at_ms=created_ms,
        updated_at_ms=updated_ms,
    )


async def upsert_row(database: Database, row: BackgroundJobRow) -> None:
    """Insert or update by ``notification_id`` (upstream upsert job).

    ``created_at_ms`` is preserved on update; ``status``/``title``/
    ``payload``/``updated_at_ms`` follow the incoming row -- this is the
    backgrounding-then-settling write path, so a settle never resets the
    original creation time.
    """
    now_ms = _now_ms()
    payload = json.dumps(row.payload) if row.payload is not None else None
    statement = text(
        """
        insert into background_job (
            notification_id, session_id, tool_call_id, kind, status,
            title, payload, created_at_ms, updated_at_ms
        ) values (
            :notification_id, :session_id, :tool_call_id, :kind, :status,
            :title, :payload, :now_ms, :now_ms
        )
        on conflict(notification_id) do update set
            status = excluded.status,
            title = excluded.title,
            payload = excluded.payload,
            updated_at_ms = excluded.updated_at_ms
        """
    ).bindparams(
        notification_id=row.notification_id,
        session_id=row.session_id,
        tool_call_id=row.tool_call_id,
        kind=row.kind,
        status=row.status,
        title=row.title,
        payload=payload,
        now_ms=now_ms,
    )
    async with database.engine.begin() as connection:
        await connection.execute(statement)


async def delete_for_session(database: Database, session_id: str) -> int:
    """Cancel a session's pending notifications (upstream ``cancelSession``)."""
    async with database.engine.begin() as connection:
        result = await connection.execute(
            text("delete from background_job where session_id = :session_id").bindparams(session_id=session_id)
        )
        return int(result.rowcount or 0)


async def _load_all(database: Database) -> list[_StoredRow]:
    async with database.engine.connect() as connection:
        result = await connection.execute(
            text(
                "select notification_id, session_id, tool_call_id, kind, status, "
                "title, payload, created_at_ms, updated_at_ms from background_job "
                "order by created_at_ms, notification_id"
            )
        )
        return [_to_stored(fetched) for fetched in result.fetchall()]


async def replay_pending(database: Database) -> list[ReplayedNotification]:
    """Boot-time replay: deliver every pending row exactly once, then admit.

    Order is creation order (upstream replays in a stable sequence so the
    woken session sees causes before effects). Terminal rows replay their
    settled payload; a still-running shell row replays as an honest error
    (no daemon to reattach to). Delivery is destructive by design: the row
    is deleted in the same transaction batch as its replay, so a crash
    mid-replay redelivers rather than loses (at-least-once, upstream's
    settled-replay semantics).
    """
    stored_rows = await _load_all(database)
    replayed: list[ReplayedNotification] = []
    for stored in stored_rows:
        row = stored.row
        if row.status in _TERMINAL:
            payload_text = ""
            if row.payload is not None:
                value = row.payload.get("text")
                if isinstance(value, str):
                    payload_text = value
            replayed.append(
                ReplayedNotification(
                    notification_id=row.notification_id,
                    session_id=row.session_id,
                    tool_call_id=row.tool_call_id,
                    kind=row.kind,
                    status=row.status,
                    text=payload_text,
                )
            )
        else:
            # Running at boot: unrecoverable in-process (no shell daemon).
            # Settle honestly to error and replay the interruption.
            replayed.append(
                ReplayedNotification(
                    notification_id=row.notification_id,
                    session_id=row.session_id,
                    tool_call_id=row.tool_call_id,
                    kind=row.kind,
                    status=STATUS_ERROR,
                    text=_RESTART_ERROR_TEXT.format(title=row.title),
                )
            )
    if stored_rows:
        async with database.engine.begin() as connection:
            await connection.execute(text("delete from background_job"))
    return replayed
