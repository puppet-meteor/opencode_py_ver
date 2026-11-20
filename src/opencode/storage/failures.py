"""Storage-failure classification (upstream ``core/src/database/storage-failure.ts``).

The messages never name the database engine: callers only know about
"storage". BUSY and LOCKED only surface once a write has already waited out
``busy_timeout``, so the write is lost just like on a full disk.

Banked deviations (doc/plans/port-232-increments.md T-C3-008):

- The upstream Effect ``Cause`` walk (recursing ``EffectDrizzleQueryError``
  causes, skipping interrupts) maps to the py exception chain: SQLAlchemy
  wraps the DBAPI error as ``.orig`` and arbitrary re-wraps keep the original
  as ``__cause__``; extended codes classify by their primary low byte.
- ``finalize_transaction`` is the py stand-in for the rollback leg of the
  upstream drizzle effect-sqlite session's transaction exit (L198-205),
  factored so the four same-shaped transaction seams (``txn.py`` and
  ``jobs_store.py``) can share it.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

#: SQLite primary result codes that mean storage itself can't be used, as
#: opposed to a bad statement or a constraint.
STORAGE_MESSAGES: dict[int, str] = {
    5: "Storage is in use by another process",  # SQLITE_BUSY
    6: "Storage is in use by another process",  # SQLITE_LOCKED
    8: "Storage is read-only",  # SQLITE_READONLY
    10: "Storage couldn't be read or written",  # SQLITE_IOERR
    11: "Storage is damaged",  # SQLITE_CORRUPT
    13: "Storage is full",  # SQLITE_FULL
    14: "Storage couldn't be opened",  # SQLITE_CANTOPEN
    26: "Storage is damaged",  # SQLITE_NOTADB
}

#: Primary result codes after which SQLite may already have rolled the whole
#: transaction back itself (the sqlite.org transaction error responses).
AUTO_ROLLBACK_CODES = frozenset({5, 7, 9, 10, 13})

#: How many wrappers the cause walk crosses before giving up (a cycle guard;
#: real chains are one or two deep).
_MAX_CAUSE_DEPTH = 16


class TransactionalConnection(Protocol):
    """The rollback face the finalize seam needs from a connection."""

    async def rollback(self) -> None: ...


def _primary_result_code(error: BaseException | None) -> int | None:
    """First ``sqlite_errorcode`` found along the wrapper chain, primary low byte."""
    seen: set[int] = set()
    current = error
    while current is not None and len(seen) < _MAX_CAUSE_DEPTH:
        if id(current) in seen:
            return None
        seen.add(id(current))
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(code, int):
            return code & 0xFF
        nxt = getattr(current, "orig", None)
        if not isinstance(nxt, BaseException):
            nxt = current.__cause__
        current = nxt if isinstance(nxt, BaseException) else None
    return None


def storage_failure_message(error: BaseException | None) -> str | None:
    """Engine-neutral message for a storage failure, or ``None`` if it isn't one."""
    code = _primary_result_code(error)
    if code is None:
        return None
    return STORAGE_MESSAGES.get(code)


def rolled_back(error: BaseException | None) -> bool:
    """Whether SQLite may have already rolled the transaction back itself."""
    code = _primary_result_code(error)
    return code is not None and code in AUTO_ROLLBACK_CODES


async def finalize_transaction(connection: TransactionalConnection, body_error: BaseException) -> None:
    """Roll back after a failed body (upstream session.ts L198-205 semantics).

    A rollback that itself fails is noise when the body error says SQLite may
    have already rolled the transaction back (the auto-rollback codes):
    swallow it, so the caller's ``raise`` surfaces the body error. Otherwise
    the rollback failure replaces the body error -- the transaction state is
    now unknown, which is the louder fact. A cancelled rollback always
    propagates.
    """
    try:
        await connection.rollback()
    except asyncio.CancelledError:
        raise
    except BaseException:
        if not rolled_back(body_error):
            raise  # the rollback failure replaces the body error
