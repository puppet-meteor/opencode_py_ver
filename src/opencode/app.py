"""Explicit composition root (R1-S7 + R2-S7): engine -> bus -> runner -> service.

Non-Effect replacement of the TS Layer composition (``packages/core/src/
session/projector.ts`` L537+ deps [Bus.node, Database.node] and
``packages/core/src/database/sqlite.bun.ts`` L100-103): the wiring is plain
constructors -- ``open_database`` -> ``VolatileBus`` -> ``SessionService``
(with the real ``SessionStore``/``SessionProjector``/``ReadRepositories``).
No global singleton and no implicit resources: every component is created
here, exposed here, and released here in order (§7 L312/L337: ``close()``
releases own tasks and closes the provider/DB, leaving no background tasks
behind).

With a provider this is the full R2 stack (r2-slices §2 R2-S7):
``EventLog`` -> ``EventCommitter`` -> ``ToolRegistry(sink=committer.sink)``
-> ``Runner``, injected into ``SessionService`` as the scheduler (§7 L331)
together with the engine (the runs read model + management-write seam) and
the provider's ``close`` (§7 L337). Without one, the composition is the R1
surface unchanged (storage + bus + session; the runs read model stays wired
so ``run_status``/``cancel`` keep their §3.2 L187 face over stored runs).

The schema is NOT migrated here -- migrations run through the Alembic CLI
(``python -m alembic upgrade head``, CORE-CONTRACTS §3.2/§5) before the app
opens the database; ``App`` composes an already-migrated data directory.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Protocol
from uuid import uuid4

from opencode.bus import VolatileBus
from opencode.bus.eventlog import EventLog
from opencode.contracts.llm import GenerationRequest, ProviderChunk
from opencode.session import SessionProjector, SessionService
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.runner import DEFAULT_MAX_STEPS, EventCommitter, Runner
from opencode.session.service import DEFAULT_CLOSE_TIMEOUT_S, AscendingIds, Clock, IdFactory
from opencode.storage import (
    Database,
    OpenMode,
    ReadRepositories,
    SessionStore,
    StoreValidationError,
    open_database,
)
from opencode.tools import ToolLimits, ToolRegistry
from opencode.tools.policy import DEFAULT_APPROVAL_TIMEOUT_S, ApprovalCoordinator, PermissionPolicy
from opencode.tools.registry import ToolAdapter


class _ProviderHandle(Protocol):
    """A Provider whose client the App owns closing (§7 L337).

    Structural: the :class:`opencode.ai.provider.Provider` protocol pins only
    ``stream``; the composition root additionally needs the §7 L337 shutdown
    face, so the App's parameter is this wider protocol (the concrete MAAS
    adapter satisfies it structurally).
    """

    def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]: ...

    async def close(self) -> None: ...


class App:
    """The explicit R2 composition: owns the Database, the bus, the runner and the service.

    ``close()`` releases in fixed order (§7 L337): the service lifecycle first
    (closing gate -> shutdown intents for its scheduled runs -> wait/timeout
    -> provider close -> service closed), then the
    :class:`~opencode.storage.Database` handle (engine dispose + writer-lock
    release; both idempotent). The async context manager does the same on
    exit. Repeated ``close()`` is safe. One shared ID factory and clock feed
    every component, so event/batch ids never collide across the service and
    the runner stack (the per-instance default factories could mint
    duplicates within the same millisecond).
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
        provider: _ProviderHandle | None = None,
        tools: Sequence[ToolAdapter] = (),
        settings: RuntimeSettings | None = None,
        limits: ToolLimits | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
        approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
    ) -> None:
        if provider is not None and settings is None:
            raise StoreValidationError("settings are required when a provider is given")
        self._database = open_database(db_path, mode=mode)
        self._bus = bus if bus is not None else VolatileBus()
        engine = self._database.engine
        store = SessionStore(engine, projector=SessionProjector())
        repos = ReadRepositories(engine)
        shared_ids = ids if ids is not None else AscendingIds(clock)
        runner: Runner | None = None
        approvals: ApprovalCoordinator | None = None
        policy: PermissionPolicy | None = None
        if provider is not None:
            assert settings is not None  # validated above
            approvals = ApprovalCoordinator(clock=clock, timeout_s=approval_timeout_s)
            policy = PermissionPolicy(
                boot_id=f"boot_{uuid4().hex}",
                approvals=approvals,
                clock=clock,
            )
            committer = EventCommitter(repos, store, self._bus, ids=shared_ids, clock=clock)
            registry = ToolRegistry(sink=committer.sink, clock=clock, ids=shared_ids, policy=policy)
            for adapter in tools:
                registry.register(adapter)
            runner = Runner(
                provider=provider,
                context_builder=ContextBuilder(settings),
                registry=registry,
                committer=committer,
                log=EventLog(engine, repos=repos, bus=self._bus),
                bus=self._bus,
                ids=shared_ids,
                clock=clock,
                limits=limits,
                max_steps=max_steps,
            )
        self._service = SessionService(
            store=store,
            repos=repos,
            bus=self._bus,
            working_dir=working_dir if working_dir is not None else os.getcwd(),
            clock=clock,
            ids=shared_ids,
            engine=engine,
            runner=runner,
            provider_close=provider.close if provider is not None else None,
            approvals=approvals,
            policy=policy,
            close_timeout_s=close_timeout_s,
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
        """Release in order: the service lifecycle first, then the engine/lock."""
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
