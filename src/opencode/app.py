"""Explicit composition root (R1-S7): engine -> bus -> SessionService.

Non-Effect replacement of the TS Layer composition (``packages/core/src/
session/projector.ts`` L537+ deps [Bus.node, Database.node] and
``packages/core/src/database/sqlite.bun.ts`` L100-103): the wiring is plain
constructors -- ``open_database`` -> ``VolatileBus`` -> ``SessionService``
(with the real ``SessionStore``/``SessionProjector``/``ReadRepositories``).
No global singleton and no implicit resources: every component is created
here, exposed here, and released here in order (§7 L312: ``close()`` releases
own tasks and closes the DB, leaving no background tasks behind).

The R1 surface is storage + bus + session only (no provider/MCP/tools). The
schema is NOT migrated here -- migrations run through the Alembic CLI
(``python -m alembic upgrade head``, CORE-CONTRACTS §3.2/§5) before the app
opens the database; ``App`` composes an already-migrated data directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

from opencode.bus import VolatileBus
from opencode.session import SessionProjector, SessionService
from opencode.session.service import Clock, IdFactory
from opencode.storage import Database, OpenMode, ReadRepositories, SessionStore, open_database


class App:
    """The explicit R1 composition: owns the Database, the bus and the service.

    ``close()`` releases in fixed order (task ②): :meth:`SessionService.close`
    (the service gate + its own resources) first, then the
    :class:`~opencode.storage.Database` handle (engine dispose + writer-lock
    release; both idempotent). The async context manager does the same on
    exit. Repeated ``close()`` is safe.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        mode: OpenMode = "create",
        working_dir: str | None = None,
        bus: VolatileBus | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._database = open_database(db_path, mode=mode)
        self._bus = bus if bus is not None else VolatileBus()
        self._service = SessionService(
            store=SessionStore(self._database.engine, projector=SessionProjector()),
            repos=ReadRepositories(self._database.engine),
            bus=self._bus,
            working_dir=working_dir if working_dir is not None else os.getcwd(),
            clock=clock,
            ids=ids,
        )

    @property
    def database(self) -> Database:
        """The owned storage handle (engine + data-directory writer lock)."""
        return self._database

    @property
    def bus(self) -> VolatileBus:
        """The owned live-notification channel."""
        return self._bus

    @property
    def service(self) -> SessionService:
        """The §7 command surface."""
        return self._service

    async def close(self) -> None:
        """Release in order: the service gate first, then the engine/lock."""
        await self._service.close()
        await self._database.close()

    async def __aenter__(self) -> App:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
