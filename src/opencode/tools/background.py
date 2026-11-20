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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import bindparam, text

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

#: The subagent recovery kind (T-C3-005; upstream ``kind: "subagent"``).
KIND_SUBAGENT = "subagent"

#: Row status -> the TS report ``state`` word. The ROW keeps the shell-word
#: terminal vocabulary (the ``_TERMINAL`` set -- the status-literal discovery
#: recorded in the plan: no "completed" row status exists); the REPORT the
#: parent sees carries the TS vocabulary.
SUBAGENT_REPORT_STATE = {
    STATUS_SUCCEEDED: "completed",
    STATUS_ERROR: "error",
    STATUS_CANCELLED: "cancelled",
}


def _collapse(value: str) -> str:
    """Whitespace-collapse one label (upstream ``clean``)."""
    return " ".join(value.split())


def _payload_dict(raw: Any) -> dict[str, Any]:
    """Decode a raw payload column (JSON text or already-parsed) to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def subagent_report_text(
    *,
    child_session_id: str,
    state: str,
    description: str,
    body: str,
    outstanding: Sequence[str] = (),
) -> str:
    """The TS ``SubagentCompletion`` report: XML wrapper + keep-alive tail.

    ``state`` is the TRUE terminal state (the metadata carrier keeps it
    verbatim). A COMPLETED child with outstanding background work reports
    ``running`` in the wrapper attribute instead -- the TS keep-alive: the
    parent must not treat the child as done while its background work runs
    on -- and the tail names each outstanding line. Shared verbatim by the
    in-process delivery and the restart replay so the two faces can never
    drift.
    """
    keep_alive = state == "completed" and bool(outstanding)
    report_state = "running" if keep_alive else state
    report_body = body
    if keep_alive:
        report_body = f"{body}\n\nBackground work still running:\n" + "\n".join(outstanding)
    return (
        f'<subagent sessionID="{child_session_id}" state="{report_state}" '
        f'description="{description}">\n'
        f"{report_body}\n"
        "</subagent>"
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


async def delete_row(database: Database, notification_id: str) -> int:
    """Delete one row after its successful delivery (``completeBackground``).

    The T-C3-005 subagent outbox close arm: settle upserts the row, the
    successful delivery deletes it; a FAILED delivery leaves it standing
    so the next boot's replay retries.
    """
    async with database.engine.begin() as connection:
        result = await connection.execute(
            text("delete from background_job where notification_id = :notification_id").bindparams(
                notification_id=notification_id
            )
        )
        return int(result.rowcount or 0)


async def _live_session_ids(database: Database) -> set[str]:
    """Sessions with any durable trace (parent-liveness for the subagent arm).

    PY is event-sourced -- there is no sessions table, so the upstream
    sessions-table liveness check maps onto the events log (any row under
    the session id; recorded deviation).
    """
    async with database.engine.connect() as connection:
        result = await connection.execute(text("select distinct session_id from events"))
        return {str(row[0]) for row in result.fetchall()}


async def outstanding_lines(database: Database, session_id: str) -> list[str]:
    """The keep-alive tail lines: rows still running under ``session_id``.

    TS ``SessionBackground.outstanding`` (the OutstandingItem face): shell
    and subagent kinds render the upstream line forms, any other kind the
    generic one; a row living under a DIFFERENT session (a descendant's
    work) carries the cross-session location suffix. Codemode/image kinds
    are unported tools (recorded deviation). Queried LIVE -- never
    snapshotted -- so a settle racing a still-running row reads the truth.
    """
    async with database.engine.connect() as connection:
        result = await connection.execute(
            text(
                "select notification_id, kind, title, payload, session_id from background_job "
                "where session_id = :session_id and status not in ('succeeded', 'error', 'cancelled') "
                "order by created_at_ms, notification_id"
            ).bindparams(session_id=session_id)
        )
        fetched = result.fetchall()
    lines: list[str] = []
    for notification_id, kind, title, payload, row_session in fetched:
        data = _payload_dict(payload)
        if kind == KIND_SHELL:
            command = data.get("command")
            label = command if isinstance(command, str) and command else title
            line = f"- shell ({notification_id}): {_collapse(label)}"
        elif kind == KIND_SUBAGENT:
            agent = data.get("agent")
            child = data.get("childSessionID")
            description = data.get("description")
            agent_word = agent if isinstance(agent, str) and agent else "task"
            child_word = child if isinstance(child, str) and child else notification_id
            desc_word = description if isinstance(description, str) and description else title
            line = f"- subagent ({agent_word}, sessionID: {child_word}): {_collapse(desc_word)}"
        else:
            line = f"- {kind} ({notification_id}): {_collapse(title)}"
        if row_session != session_id:
            line = f"{line}, in {row_session}"
        lines.append(line)
    return lines


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


async def list_rows(database: Database) -> list[_StoredRow]:
    """Every durable background row, oldest first (T-C3-009).

    The persisted leg of the upstream ``Job.backgroundJobs`` read; the live
    leg folds into the same rows because the invoker upserts every transition
    as it happens, so this one face serves the ``job.list`` route.
    """
    return await _load_all(database)


async def replay_pending(database: Database) -> list[ReplayedNotification]:
    """Boot-time replay: deliver every pending row exactly once, then admit.

    Order is creation order (upstream replays in a stable sequence so the
    woken session sees causes before effects). Terminal rows replay their
    settled payload; a still-running shell row replays as an honest error
    (no daemon to reattach to). Delivery is destructive by design: the row
    is deleted in the same transaction batch as its replay, so a crash
    mid-replay redelivers rather than loses (at-least-once, upstream's
    settled-replay semantics).

    T-C3-005 adds the subagent arm (upstream ``deliverSubagentSafe``): a
    settled subagent row replays as the wrapped ``<subagent>`` report built
    from its recovery payload, outstanding work queried LIVE through the
    same shared construction the in-process delivery uses; a row whose
    parent left no durable trace is cleared silently; a row whose child
    was still running at boot is KEPT -- PY has no ``execution.resume`` to
    re-drive it (recorded deviation), so the row survives for the next
    boot rather than faking a terminal receipt.
    """
    stored_rows = await _load_all(database)
    live_parents = await _live_session_ids(database)
    replayed: list[ReplayedNotification] = []
    keep: set[str] = set()
    for stored in stored_rows:
        row = stored.row
        if row.kind == KIND_SUBAGENT:
            if row.session_id not in live_parents:
                # Deleted parent: silent clear (the sweep drops the row).
                continue
            if row.status not in _TERMINAL:
                keep.add(row.notification_id)
                continue
            payload = row.payload if row.payload is not None else {}
            child = _payload_str(payload, "childSessionID") or row.tool_call_id
            description = _payload_str(payload, "description") or row.title
            body = _payload_str(payload, "output") or ""
            outstanding = await outstanding_lines(database, child)
            replayed.append(
                ReplayedNotification(
                    notification_id=row.notification_id,
                    session_id=row.session_id,
                    tool_call_id=row.tool_call_id,
                    kind=row.kind,
                    status=row.status,
                    text=subagent_report_text(
                        child_session_id=child,
                        state=SUBAGENT_REPORT_STATE[row.status],
                        description=description,
                        body=body,
                        outstanding=outstanding,
                    ),
                )
            )
            continue
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
    doomed = [stored.row.notification_id for stored in stored_rows if stored.row.notification_id not in keep]
    if doomed:
        delete_statement = text("delete from background_job where notification_id in :notification_ids").bindparams(
            bindparam("notification_ids", expanding=True)
        )
        async with database.engine.begin() as connection:
            await connection.execute(delete_statement, {"notification_ids": doomed})
    return replayed
