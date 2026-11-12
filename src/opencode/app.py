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
the provider's ``close`` (§7 L337), plus -- M6 slice three, T-C1-063 -- the
real LLM-backed summary face (``ProviderSummarizer`` over the same provider
and ``RuntimeSettings``) as the service's ``compaction_summarizer``, so the
§2.4 compaction chain runs on real model calls in every R2 composition.
Without one, the composition is the R1
surface unchanged (storage + bus + session; the runs read model stays wired
so ``run_status``/``cancel`` keep their §3.2 L187 face over stored runs).

The schema is NOT migrated here -- migrations run through the Alembic CLI
(``python -m alembic upgrade head``, CORE-CONTRACTS §3.2/§5) before the app
opens the database; ``App`` composes an already-migrated data directory.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol
from uuid import uuid4

from opencode.bus import VolatileBus
from opencode.bus.eventlog import EventLog
from opencode.contracts.events import DurableEvent, McpServerRef
from opencode.contracts.llm import GenerationRequest, ProviderChunk
from opencode.contracts.pipelines import PipelineSpec
from opencode.mcp import McpConnectError, McpManager, MCPRemoteTool, MCPToolAdapter
from opencode.orchestrator.brain_driver import BrainDriver
from opencode.orchestrator.engine import PipelineEngine
from opencode.orchestrator.jobs import FakeJobExecutor, JobService, JobsProjector, SpecParamsValidator
from opencode.orchestrator.workers import WorkerManager
from opencode.session import SessionProjector, SessionService
from opencode.session.context import ContextBuilder, RuntimeSettings
from opencode.session.llm_summarizer import ProviderSummarizer
from opencode.session.recovery import RecoveryCoordinator, RecoveryGate, RecoveryReport, sweep_crash_temp_files
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
from opencode.storage.jobs_store import JobsReadRepository, JobsStore
from opencode.storage.store import EventBatch
from opencode.tools import ToolLimits, ToolRegistry
from opencode.tools.policy import (
    DEFAULT_APPROVAL_TIMEOUT_S,
    ApprovalCoordinator,
    McpToolBinding,
    PermissionPolicy,
    PermissionRule,
)
from opencode.tools.registry import ToolAdapter, mcp_tool_spec
from opencode.tools.workflow import WorkflowRunAdapter, WorkflowStatusAdapter


class _ProviderHandle(Protocol):
    """A Provider whose client the App owns closing (§7 L337).

    Structural: the :class:`opencode.ai.provider.Provider` protocol pins only
    ``stream``; the composition root additionally needs the §7 L337 shutdown
    face, so the App's parameter is this wider protocol (the concrete MAAS
    adapter satisfies it structurally).
    """

    def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]: ...

    async def close(self) -> None: ...


async def _startup_scan(coordinator: RecoveryCoordinator, roots: tuple[str, ...]) -> RecoveryReport:
    """One App-startup pass: the row#27③ crash-temp sweep, then the recovery scan.

    The sweep is host-operational hygiene over the declared roots (T-C1-028
    ④-1 ruling: the composition-root path, never the session service layer);
    its counters ride the recovery report. Sweep failures never block the
    scan -- they are reported, not raised.
    """
    removed, errors = sweep_crash_temp_files(roots)
    report = await coordinator.scan()
    return replace(report, tmp_files_removed=removed, tmp_roots=roots, tmp_errors=errors)


class _RecoveryGatedStore(SessionStore):
    """The R4-S4 write gate (T-C1-037, OP-R4-6): SessionStore + startup recovery.

    Wraps the composition root's real store, sharing its single
    TransactionWriter (no second writer and no second lock set): the write
    faces ``commit``/``rebuild``/``cleanup_failed`` first await the bound
    startup recovery scan, then delegate unchanged. Reads are NOT gated
    (§3.2.1: queries never trigger recovery) and the recovery coordinator
    itself commits through the ungated inner store -- only the service-facing
    writes wait. A scan failure propagates to every gated caller
    (fail-closed: the composition root stays write-unavailable instead of
    proceeding past a failed recovery).
    """

    def __init__(self, inner: SessionStore, gate: RecoveryGate) -> None:
        self._writer = inner._writer
        self._gate = gate

    async def commit(self, session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]:
        await self._gate.wait()
        return await super().commit(session_id, batch, expected_seq)

    async def rebuild(self, session_id: str) -> None:
        await self._gate.wait()
        await super().rebuild(session_id)

    async def cleanup_failed(self, session_id: str, run_id: str, reason: str) -> None:
        await self._gate.wait()
        await super().cleanup_failed(session_id, run_id, reason)


class App:
    """The explicit R2 composition: owns the Database, the bus, the runner and the service.

    ``close()`` releases in fixed order (§7 L337/L339): the service lifecycle first
    (closing gate -> shutdown intents for its scheduled runs -> wait/timeout
    -> MCP close -> provider close -> service closed), then the
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
        rules: Sequence[PermissionRule] = (),
        mcp_manager: McpManager | None = None,
        mcp_refs: Sequence[McpServerRef] | None = None,
        sweep_roots: Sequence[str | Path] | None = None,
        pipeline_engine: PipelineEngine | None = None,
        workflow_tools: bool = False,
    ) -> None:
        if provider is not None and settings is None:
            raise StoreValidationError("settings are required when a provider is given")
        # P3-S4 (T-C1-079): the workflow tools and the brain driver are the
        # provider composition's faces -- they register into the brain's
        # single ToolRegistry and drive its session runner. Wiring them
        # without a provider is a composition defect (fail closed, never a
        # silent no-op registry).
        if workflow_tools and provider is None:
            raise StoreValidationError("workflow_tools=True requires the provider composition (provider + settings)")
        self._database = open_database(db_path, mode=mode)
        self._bus = bus if bus is not None else VolatileBus()
        engine = self._database.engine
        store = SessionStore(engine, projector=SessionProjector())
        repos = ReadRepositories(engine)
        shared_ids = ids if ids is not None else AscendingIds(clock)
        # R4-S4 (T-C1-037): startup recovery wiring. OP-R4-6 conservative
        # default: the composition root is unavailable for writes until the
        # one startup recovery scan completes -- the service's store faces
        # pass through _RecoveryGatedStore (the same single TransactionWriter,
        # no second writer or lock set) while the recovery coordinator commits
        # through the ungated inner store; reads stay ungated (§3.2.1). The
        # scan is scheduled eagerly when a loop is already running and
        # otherwise starts at the first gated write. ``mcp_refs`` declares the
        # host's current MCP server references for the §6.1 L302 fingerprint
        # comparison (None = the host declares no MCP face: no comparison);
        # row #27③'s crash-temp sweep rides the same startup pass over
        # ``sweep_roots`` (default: the working directory, the
        # composition-root's declared root face).
        resolved_working_dir = working_dir if working_dir is not None else os.getcwd()
        recovery_gate = RecoveryGate()
        gated_store = _RecoveryGatedStore(store, recovery_gate)
        recovery_committer = EventCommitter(repos, store, self._bus, ids=shared_ids, clock=clock)
        recovery_coordinator = RecoveryCoordinator(
            engine=engine,
            committer=recovery_committer,
            current_mcp_servers=mcp_refs,
            repos=repos,
            ids=shared_ids,
            clock=clock,
        )
        recovery_roots = (
            tuple(str(root) for root in sweep_roots) if sweep_roots is not None else (resolved_working_dir,)
        )
        recovery_gate.bind(lambda: _startup_scan(recovery_coordinator, recovery_roots))
        self._recovery_gate = recovery_gate
        runner: Runner | None = None
        summarizer: ProviderSummarizer | None = None
        approvals: ApprovalCoordinator | None = None
        policy: PermissionPolicy | None = None
        registry: ToolRegistry | None = None
        if provider is not None:
            assert settings is not None  # validated above
            approvals = ApprovalCoordinator(clock=clock, timeout_s=approval_timeout_s)
            policy = PermissionPolicy(
                boot_id=f"boot_{uuid4().hex}",
                approvals=approvals,
                # R3-S7 wiring seam (B report §6 open point 1): snapshot
                # permission rules enter as the project tier -- they tighten
                # the defaults but never outrank the host tier (§5.2 L266).
                rules=rules,
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
            # M6 slice three (T-C1-063): the real LLM-backed summary face over
            # the SAME provider and runtime settings -- the §2.4 L126 model
            # call of the compaction chain, injected with the same conditional
            # shape as ``provider_close`` below (None keeps the R1 dormant
            # face strictly unchanged).
            summarizer = ProviderSummarizer(provider=provider, settings=settings)
        self._service = SessionService(
            store=gated_store,
            repos=repos,
            bus=self._bus,
            working_dir=resolved_working_dir,
            clock=clock,
            ids=shared_ids,
            engine=engine,
            runner=runner,
            compaction_summarizer=summarizer,
            provider_close=provider.close if provider is not None else None,
            mcp_close=mcp_manager.close if mcp_manager is not None else None,
            approvals=approvals,
            policy=policy,
            close_timeout_s=close_timeout_s,
        )
        # R4-S2 wiring: the MCP manager is App-owned but lifecycle-closed by
        # the service (§7 L339: MCP -> provider -> DB); the caller injects it
        # explicitly -- config loading is the S3 face. P2-S0 (§6 L304)
        # upgraded the ownership from "close only" to the full
        # spawn+bind+close lifecycle: start_mcp_servers() spawns each
        # snapshot-declared stdio server, registers its remote tools and
        # pins the policy bindings, while close() keeps the unchanged
        # §7 L339 order.
        self._mcp_manager = mcp_manager
        self._mcp_refs = tuple(mcp_refs) if mcp_refs is not None else ()
        self._registry = registry
        self._policy = policy
        # P3-S1 (T-C1-076): the jobs domain rides the same engine + bus over
        # its own writer (PHASE3 M-7 "two writer domains" -- the session fold
        # is fail-closed on py.job.* and frozen, so jobs commit through
        # JobsStore on ses_job_* stream keys, not SessionStore). P3-S2
        # (T-C1-077): the executor is the injected pipeline engine when one is
        # wired (real LangGraph execution over its own checkpointer file);
        # None keeps the S1 fake (started -> finished(done)). Recovery is
        # deliberately NOT wired into the lifespan (a semantics change beyond
        # this slice's edit list): callers drive ``jobs.recover()`` explicitly
        # -- progress ④ open item, now with the engine itself injectable as
        # the ``JobCheckpointReader`` (its advisory-cursor read face).
        # P3-S3 (T-C1-078): when the wired engine carries a real worker
        # manager, it doubles as the jobs ``RunCanceller`` (a cancel kills
        # the worker's subprocess tree before the terminal fold commits).
        # The full production wiring (credentials, model, runs root) is
        # composed by the caller that builds the manager -- the App face
        # only derives the hook.
        self._pipeline_engine = pipeline_engine

        # P3-S4 (T-C1-079): the shared gateway params validator over the
        # engine's registered specs -- zero-model strict JSON-Schema
        # validation (PHASE3 §1 condition 4) that the brain's workflow__run
        # submit and the future S5b router consume as ONE instance. Without
        # an engine the lookup is the empty face (every pipeline id is
        # "not registered" -- fail closed, same verdict the engine-bound
        # lookup gives an unknown id).
        def _spec_of(pipeline_id: str) -> PipelineSpec | None:
            bound = pipeline_engine.bound(pipeline_id) if pipeline_engine is not None else None
            return bound.spec if bound is not None else None

        params_validator: SpecParamsValidator | None = None
        if workflow_tools:
            params_validator = SpecParamsValidator(lookup=_spec_of)

        wired_agent = pipeline_engine.opencode_agent if pipeline_engine is not None else None
        self._jobs = JobService(
            store=JobsStore(engine, projector=JobsProjector()),
            repos=JobsReadRepository(engine),
            executor=pipeline_engine if pipeline_engine is not None else FakeJobExecutor(),
            bus=self._bus,
            ids=shared_ids,
            clock=clock,
            params_validator=params_validator,
            canceller=wired_agent if isinstance(wired_agent, WorkerManager) else None,
        )
        # The registration-isolation face (C#5): the workflow tools are
        # injected ONLY into the main brain's ToolRegistry (this single
        # registry); worker catalogs (S3) build their own builtin-only
        # registries and never see workflow__run. The brain driver shares
        # the same service + engine + bus.
        self._brain: BrainDriver | None = None
        if workflow_tools:
            assert registry is not None  # the provider check above guarantees it
            registry.register(WorkflowRunAdapter(jobs=self._jobs))
            registry.register(WorkflowStatusAdapter(jobs=self._jobs))
            self._brain = BrainDriver(
                service=self._service,
                log=EventLog(engine, repos=repos, bus=self._bus),
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

    @property
    def recovery_gate(self) -> RecoveryGate:
        """The startup-recovery gate (T-C1-037); ``await gate.wait()`` yields the scan report."""
        return self._recovery_gate

    @property
    def mcp_manager(self) -> McpManager | None:
        """The owned MCP server manager (None = no MCP servers in this composition)."""
        return self._mcp_manager

    @property
    def jobs(self) -> JobService:
        """The P3-S1 jobs service (submit/run/cancel + the §8 recovery scan)."""
        return self._jobs

    @property
    def pipeline_engine(self) -> PipelineEngine | None:
        """The P3-S2 pipeline engine when wired (None keeps the S1 fake executor)."""
        return self._pipeline_engine

    @property
    def registry(self) -> ToolRegistry | None:
        """The provider composition's single tool registry (None without a provider).

        P3-S4 (T-C1-079) registers the workflow tools into exactly this
        instance -- the main brain's registry; worker catalogs are separate
        builtin-only registries and never see them.
        """
        return self._registry

    @property
    def brain(self) -> BrainDriver | None:
        """The P3-S4 main-brain driver (None unless ``workflow_tools=True``)."""
        return self._brain

    async def start_mcp_servers(self) -> tuple[tuple[str, Exception], ...]:
        """P2-S0 (§6 L304): spawn, catalog and bind every configured stdio server.

        The App's spawn+bind ownership face (the close keeps §7 L339): each
        injected ``mcp_servers`` ref is started through the owned manager
        (spawn -> initialize -> tools/list), its remote tools are registered
        under the fixed ``mcp`` namespace and every ``mcp__<name>`` action is
        pinned to the server's startup fingerprint plus the registry's MCP
        catalog digest (:meth:`PermissionPolicy.bind_mcp_tools`, §5.2 L268).
        The digest is taken once after every registration, so the bound
        catalog version is the boot's final provider-visible face. A server
        that fails to start is one ``(server, error)`` pair -- the manager
        keeps its failed marker and no empty tool face is registered -- and
        the remaining servers still start; the driving host surfaces each
        failure itself. A ref without a manager registration or a
        cross-server tool-name collision is a wiring defect and propagates
        (fail closed, never silent).
        """
        if self._mcp_manager is None:
            return ()
        if self._registry is None or self._policy is None:
            raise StoreValidationError("MCP servers require the provider composition (tools and policy)")
        started: list[tuple[McpServerRef, tuple[MCPRemoteTool, ...]]] = []
        failures: list[tuple[str, Exception]] = []
        for ref in self._mcp_refs:
            try:
                tools = await self._mcp_manager.start_server(ref.server)
            except (McpConnectError, OSError) as error:
                # The manager carries its failed marker for the
                # handshake/catalog faces; an OSError here is a spawn-level
                # failure (e.g. a missing binary) that face does not mark --
                # either way nothing is registered for the server and the
                # boot continues with the rest (④ disclosed).
                failures.append((ref.server, error))
                continue
            for tool in tools:
                self._registry.register_mcp(
                    ref.server,
                    MCPToolAdapter(
                        manager=self._mcp_manager,
                        server=ref.server,
                        tool=tool,
                        spec=mcp_tool_spec(tool.name, tool.description, tool.input_schema),
                    ),
                )
            started.append((ref, tools))
        catalog_version = self._registry.mcp_catalog_version()
        for ref, tools in started:
            binding = McpToolBinding(server=ref.server, fingerprint=ref.fingerprint, catalog_version=catalog_version)
            for tool in tools:
                self._policy.bind_mcp_tools(f"mcp__{tool.name}", binding)
        return tuple(failures)

    async def close(self) -> None:
        """Release in order: the jobs tasks first (which cancels every engine
        drive), then the engine's own advisory-cursor store, then the service
        lifecycle, then the engine/lock."""
        await self._jobs.close()
        if self._pipeline_engine is not None:
            await self._pipeline_engine.close()
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
