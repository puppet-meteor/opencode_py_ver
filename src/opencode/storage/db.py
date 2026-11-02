"""SQLite storage engine open/close entry (R1-S2, CORE-CONTRACTS §5 + §3.2).

TS mapping (``packages/core/src/database/sqlite.bun.ts``): ``nativeLayer``
L83-96 -- open flags (readonly / create / readwrite), close finalizer,
``PRAGMA journal_mode = WAL``; ``make`` L23-81 -- statement execution on one
connection. Python differences: a SQLAlchemy 2.0 async engine over aiosqlite
instead of bun:sqlite; an explicit ``Database`` handle replaces the Effect
Layer finalizer (async ``close()`` / async context manager); every pooled
connection additionally sets ``foreign_keys=ON`` and ``busy_timeout=5000``
(CORE-CONTRACTS §5; the TS layer relies on its client stack for those), and
writer opens take the data-directory OS file lock BEFORE the database is
opened -- the exact msvcrt/fcntl scheme verified by the R0-B probe
(``tools/r0/platform_probe.py``, evidence ``doc/evidence/r0/platform.json``):
a second writer fails cleanly without touching the database, and process
death releases the lock. The second-writer failure behavior is reused from
R0-B and deliberately not retested here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from sqlalchemy import event
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

#: Open semantics for :func:`open_database` (TS ``nativeLayer`` open flags).
#: ``readonly`` opens read-only (file must exist); ``create`` opens
#: read-write and creates the database file if missing (TS ``create ?? true``);
#: ``readwrite`` opens read-write over an existing file (TS ``readwrite ?? true``).
OpenMode = Literal["readonly", "create", "readwrite"]

_OPEN_MODES: frozenset[str] = frozenset({"readonly", "create", "readwrite"})

#: CORE-CONTRACTS §5: SQLite runs with WAL, foreign_keys and a 5s busy timeout.
BUSY_TIMEOUT_MS = 5000

#: Data-directory writer-lock file name (same as the R0-B probe).
WRITER_LOCK_NAME = "writer.lock"


class StorageError(RuntimeError):
    """Storage engine failure; upper slices map these onto CoreError codes."""


class WriterLockHeldError(StorageError):
    """Another writer process holds the data-directory lock (R0-B conclusion)."""


class DatabaseNotFoundError(StorageError):
    """The database file does not exist (readonly / readwrite opens)."""


def _lock_region(fd: int) -> None:
    """Raise OSError when another writer holds the region (R0-B scheme)."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class WriterLock:
    """Exclusive OS file lock over one data directory (single writer, CORE-CONTRACTS §3.2).

    Byte-range lock on a lock file next to the database, mirroring the R0-B
    probe exactly: ``msvcrt.locking(LK_NBLCK)`` over 1 byte at offset 0 on
    Windows, ``fcntl.flock(LOCK_EX | LOCK_NB)`` on POSIX. The fd stays open
    for the engine's lifetime; releasing is best-effort because process death
    always releases the OS lock (R0-B verified, not retested here).
    """

    __slots__ = ("_fd", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Lock BEFORE the database opens; a held lock raises without any DB access."""
        if self._fd is not None:
            raise StorageError(f"writer lock already held: {self._path}")
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_region(fd)
        except OSError as error:
            os.close(fd)
            raise WriterLockHeldError(f"another writer holds the data-directory lock: {self._path}") from error
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_region(fd)
        except OSError:
            pass  # best-effort; process exit always releases the OS lock
        finally:
            os.close(fd)


def _engine_url(path: Path, mode: OpenMode) -> URL:
    """Absolute POSIX path; readonly goes through a sqlite URI (mode=ro)."""
    absolute = path.expanduser().resolve()
    if mode == "readonly":
        return URL.create(
            "sqlite+aiosqlite",
            database=f"file:{absolute.as_posix()}",
            query={"mode": "ro", "uri": "true"},
        )
    return URL.create("sqlite+aiosqlite", database=absolute.as_posix())


def _install_connection_pragmas(engine: AsyncEngine, *, readonly: bool) -> None:
    """CORE-CONTRACTS §5 on every pooled connection: FK on, busy timeout, WAL for writers.

    ``foreign_keys`` and ``busy_timeout`` are per-connection settings;
    ``journal_mode`` persists in the database file, so re-setting it on each
    writer connection is idempotent. A readonly connection cannot write the
    journal mode, so it is skipped there (the file is already WAL once a
    writer has opened it).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if not readonly:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


class Database:
    """Opened storage engine handle: one async engine + the data-directory writer lock.

    Mirrors the TS ``nativeLayer`` close finalizer (L92): ``close()`` disposes
    the engine and releases the writer lock; the async context manager does
    the same on exit. Repeated ``close()`` is a no-op.
    """

    def __init__(self, *, path: Path, mode: OpenMode, engine: AsyncEngine, lock: WriterLock | None) -> None:
        self.path = path
        self.mode = mode
        self.engine = engine
        self._lock = lock
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Dispose the engine, then release the writer lock (TS close finalizer)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.engine.dispose()
        finally:
            if self._lock is not None:
                self._lock.release()

    async def __aenter__(self) -> Database:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


def open_database(path: str | Path, *, mode: OpenMode = "create") -> Database:
    """Open the storage engine; the writer lock is taken BEFORE the database opens.

    ``mode`` (TS ``nativeLayer`` open flags, L87-91):

    - ``"create"`` (default): read-write, creates the database file if missing;
    - ``"readwrite"``: read-write over an existing file;
    - ``"readonly"``: read-only over an existing file; takes no writer lock.

    Writer opens lock the data directory first, so a second writer process
    fails with :class:`WriterLockHeldError` before touching the database
    (R0-B conclusion, reused, not retested).
    """
    if mode not in _OPEN_MODES:
        raise ValueError(f"unknown open mode: {mode!r} (expected 'readonly' | 'create' | 'readwrite')")
    db_path = Path(path)
    if mode != "create" and not db_path.exists():
        raise DatabaseNotFoundError(f"database file does not exist: {db_path}")
    lock: WriterLock | None = None
    if mode != "readonly":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = WriterLock(db_path.parent / WRITER_LOCK_NAME)
        lock.acquire()
    try:
        engine = create_async_engine(_engine_url(db_path, mode))
        _install_connection_pragmas(engine, readonly=mode == "readonly")
    except BaseException:
        if lock is not None:
            lock.release()
        raise
    return Database(path=db_path, mode=mode, engine=engine, lock=lock)
