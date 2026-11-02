"""Storage module (R1-S2): async SQLite engine open/close + Alembic-managed schema.

Single writer per data directory (R0-B verified OS file lock, acquired before
the database opens), WAL + foreign_keys + busy_timeout on every pooled
connection (CORE-CONTRACTS §5), and the migration 0001 minimal schema per
CORE-CONTRACTS §3.2. Migrations run on a sync engine (``python -m alembic
upgrade head``, see ``migrations/env.py``); the runtime uses the async engine
from :func:`open_database` -- the same database file serves both (SQLite is
compatible at the file level).
"""

from opencode.storage.db import (
    BUSY_TIMEOUT_MS,
    WRITER_LOCK_NAME,
    Database,
    DatabaseNotFoundError,
    OpenMode,
    StorageError,
    WriterLock,
    WriterLockHeldError,
    open_database,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "WRITER_LOCK_NAME",
    "Database",
    "DatabaseNotFoundError",
    "OpenMode",
    "StorageError",
    "WriterLock",
    "WriterLockHeldError",
    "open_database",
]
