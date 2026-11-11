"""The host explicit-reload orchestrator (P2-S20 M2 watcher W2; CORE-CONTRACTS 6.4 rulings 2/3/4).

Dependency direction (the W1 ``__init__.py`` argument extended to this
module): the reload orchestration is a WATCHER face that imports only the
frozen read-only public faces of core — ``opencode.config.loader``,
``opencode.contracts.*`` and ``opencode.storage`` — and nothing imports it
back: core never imports the watcher package, and the W1 service module
stays byte-identical with its zero-opencode-import structural pin. The
orchestrator holds no write face at all: its only storage reference is a
:class:`~opencode.storage.repos.ReadRepositories`, so it is structurally
incapable of writing an event, a projection row or a session record — the
stored ``py.session.created`` and every ``py.permission.*`` row are
untouched by construction (ruling 2: the old snapshot's meaning is never
silently rewritten; ruling 4: reload writes no permission events).

The capability gate rides the W1 package import (the httpapi
deferred-optional precedent): importing ``opencode.watcher.reload`` first
imports the package, whose guard raises
:class:`~opencode.watcher.WatcherExtrasMissingError` (code
``UnsupportedCapability``) when the watcher extras are absent — the
explicit reload command is an extras capability on the same gate.

Ruling 2 — the reload command, never a hot reload. The only path to a
new snapshot is :meth:`ReloadOrchestrator.reload`, which re-resolves
through the host-injected callable over the ``ConfigLoader`` public face,
recomputes the 6.2 canonical hash from the NEW snapshot's bytes (a digest
is never carried over), re-verifies the (snapshot, hash) pair through the
contracts created-data model (the 6.2.3 decode-time recheck — an input
that does not match is ``ValidationError``) and swaps the in-process
regime the host reads for sessions created AFTER the reload
(:attr:`~ReloadOrchestrator.current_runtime` — the injection path; the
recorded snapshot of an existing session is its frozen behavioral
baseline). File events alone never reach this path: the watcher only
hints and the host decides (6.4/L340: stop and re-approve, never silently
swap a different permission set).

Ruling 3 — the idle gate. The reload threshold is the conjunction of
(1) no accepted/running/cancelling run of the target session (the
``non_terminal_runs`` face, the 2.1 state vocabulary), (2) no pending
``py.permission.requested`` anywhere in the process (the
``pending_permissions`` face — the contract names the condition without a
target-session qualifier), and (3) a quiet window elapsed since the last
activity, where every file change or host command the host notes restarts
the window. The observation is read-only (nothing is cancelled or
interrupted); at the application point the precondition is re-verified
under the orchestrator lock per the 3.2 commit-lock discipline — fresh
state read immediately before the act, never a stale pre-check — and a
failed recheck rejects the reload with a structured reason the host can
retry.

Ruling 4 — boot_id and saved decisions. The reload is not a boot: the
boot_id never changes across reloads. A saved decision recorded under an
older snapshot regime stays in the durable log as audit and does NOT
auto-activate under the new regime; the carrier is the evaluation-time
regime gate :meth:`ReloadOrchestrator.saved_decision_is_active` (the
only carrier inside "retain audit + never auto-activate + zero
approval-face writes": the reload cannot append revocation events and
the log is append-only), which the host wires into its 5.2 activation
policy.

The module is passive by design: no background task is ever spawned, so
the 6.4 red line 3 (7: close must reclaim, zero leftover tasks) needs
only the idempotent :meth:`~ReloadOrchestrator.close` gate.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from opencode.config.loader import ResolvedRuntime
from opencode.contracts.events import (
    ConfigSnapshotV2,
    ConfigSnapshotV3,
    SessionCreatedDataV2,
    SessionCreatedDataV3,
    compute_snapshot_hash,
)
from opencode.storage import ReadRepositories

__all__ = [
    "DEFAULT_QUIET_WINDOW_S",
    "IdleObservation",
    "ReloadOrchestrator",
    "ReloadResult",
    "SnapshotRegime",
    "verify_snapshot_hash",
]

#: Default quiet window in seconds (ruling 3: the threshold is this slice's
#: to set). Comfortably above the W1 merge window (0.5 s) so a jitter burst
#: settles before the reload opportunity opens, small enough not to stall
#: an idle host.
DEFAULT_QUIET_WINDOW_S = 2.0


@dataclass(frozen=True, slots=True)
class SnapshotRegime:
    """One snapshot regime the host runs on (the ruling-4 audit timeline).

    ``since`` is the first instant the regime was current; ``None`` for the
    initial regime the orchestrator was constructed on — it extends to the
    unbounded past, so decisions recorded in this boot before the face
    existed (but before any reload) stay active under it. A reload regime
    starts at its application instant, boundary included: the instant
    itself belongs to the NEW regime.
    """

    boot_id: str
    snapshot_hash: str
    since: datetime | None


@dataclass(frozen=True, slots=True)
class IdleObservation:
    """The read-only three-condition idle observation (ruling 3).

    Violations are enumerated, not just booleans: which runs of the target
    session are in flight, which permissions are pending anywhere, and how
    much quiet window remains — the structured inputs of a rejection.
    """

    no_in_flight_runs: bool
    no_pending_approvals: bool
    quiet_window_elapsed: bool
    in_flight_run_ids: tuple[str, ...]
    pending_permission_ids: tuple[str, ...]
    quiet_seconds_remaining: float
    observed_at: datetime

    @property
    def idle(self) -> bool:
        """The conjunction: all three conditions hold."""
        return self.no_in_flight_runs and self.no_pending_approvals and self.quiet_window_elapsed


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """The structured outcome of the host explicit reload command.

    A rejected reload carries the failing :class:`IdleObservation` and a
    human-readable ``rejection_reason`` (structured: which runs, which
    approvals, how much window remains) — the host retries later; an
    applied reload carries the new runtime, the new regime and the
    superseded regime's hash. ``boot_id`` is carried on both faces because
    it never changes across a reload (ruling 4).
    """

    applied: bool
    observation: IdleObservation
    boot_id: str
    runtime: ResolvedRuntime | None
    regime: SnapshotRegime | None
    previous_snapshot_hash: str | None
    rejection_reason: str | None


def verify_snapshot_hash(
    snapshot: ConfigSnapshotV2 | ConfigSnapshotV3,
    *,
    working_dir: str,
    snapshot_hash: str,
) -> SessionCreatedDataV2 | SessionCreatedDataV3:
    """The 6.2.3 decode-time re-verification of one (snapshot, hash) pair.

    Validates the pair through the contracts ``py.session.created`` data
    model — the same model validator that recomputes the canonical digest
    on every committed and read event — so an input that does not match is
    ``ValidationError`` (6.2: the digest is recomputed from the
    re-normalized bytes; a stale or foreign hash never passes).
    """
    payload: dict[str, Any] = {
        "working_dir": working_dir,
        "config_snapshot": snapshot.model_dump(mode="json"),
        "snapshot_hash": snapshot_hash,
    }
    if isinstance(snapshot, ConfigSnapshotV3):
        return SessionCreatedDataV3.model_validate(payload)
    return SessionCreatedDataV2.model_validate(payload)


def _default_clock() -> datetime:
    return datetime.now(tz=UTC)


def _rejection_reason(observation: IdleObservation) -> str:
    """The structured human-readable summary of a failed idle gate."""
    parts: list[str] = []
    if observation.in_flight_run_ids:
        parts.append("in-flight run(s): " + ", ".join(observation.in_flight_run_ids))
    if observation.pending_permission_ids:
        parts.append("pending approval(s): " + ", ".join(observation.pending_permission_ids))
    if not observation.quiet_window_elapsed:
        parts.append(f"quiet window not elapsed: {observation.quiet_seconds_remaining:.3f}s remaining")
    return "; ".join(parts)


class ReloadOrchestrator:
    """The host explicit-reload face: the idle-gated swap of the snapshot regime.

    The orchestrator is constructed on the host's current runtime (the
    initial regime); the host injects the resolve callable (the
    ``ConfigLoader`` public face it runs), the read repositories of the
    process, the reload target session and its working directory. The
    host — the sole consumer of the W1 notification queue per ruling 1 —
    notes every file change it consumes and every host command through
    :meth:`note_file_activity` / :meth:`note_command_activity`; the
    orchestrator never touches the queue itself. All clocks are UTC.
    """

    def __init__(
        self,
        *,
        runtime: ResolvedRuntime,
        resolve: Callable[[], ResolvedRuntime | Awaitable[ResolvedRuntime]],
        read: ReadRepositories,
        session_id: str,
        working_dir: str,
        quiet_window_s: float = DEFAULT_QUIET_WINDOW_S,
        boot_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if quiet_window_s <= 0:
            raise ValueError("quiet_window_s must be > 0")
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        if not working_dir:
            raise ValueError("working_dir must be a non-empty string")
        if boot_id is not None and not boot_id:
            raise ValueError("boot_id must be a non-empty string when given")
        self._resolve = resolve
        self._read = read
        self._session_id = session_id
        self._working_dir = working_dir
        self._quiet_window_s = quiet_window_s
        self._boot_id = boot_id if boot_id is not None else uuid4().hex
        self._clock = clock if clock is not None else _default_clock
        self._lock = asyncio.Lock()
        self._current_runtime = runtime
        self._regimes: list[SnapshotRegime] = [
            SnapshotRegime(boot_id=self._boot_id, snapshot_hash=compute_snapshot_hash(runtime.snapshot), since=None)
        ]
        self._quiet_since = self._clock()
        self._last_occupied = False
        self._closed = False

    # -- public faces --------------------------------------------------------

    @property
    def boot_id(self) -> str:
        """The process boot identity — unchanged across every reload (ruling 4)."""
        return self._boot_id

    @property
    def current_runtime(self) -> ResolvedRuntime:
        """The runtime the host creates post-reload sessions with (the injection path)."""
        return self._current_runtime

    @property
    def active_regime(self) -> SnapshotRegime:
        """The regime runs are created under right now."""
        return self._regimes[-1]

    @property
    def regimes(self) -> tuple[SnapshotRegime, ...]:
        """The append-only regime timeline (initial first, one entry per applied reload)."""
        return tuple(self._regimes)

    @property
    def is_closed(self) -> bool:
        """True after :meth:`close` — command faces refuse, audit data stays readable."""
        return self._closed

    async def __aenter__(self) -> ReloadOrchestrator:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Gate the command faces; idempotent.

        The module spawns no background task, so closing is exactly the
        refusal gate — reclamation is structural (7: zero leftover tasks).
        """
        self._closed = True

    def note_file_activity(self) -> None:
        """A file change the host consumed as a notification: restart the quiet window.

        Ruling 1 keeps the host the sole consumer of the notification
        queue, so the host notes what it consumed. Await-free by design:
        the stamp is atomic in the event loop.
        """
        if self._closed:
            return
        self._quiet_since = self._clock()

    def note_command_activity(self) -> None:
        """A host command happened (prompt/cancel/reply/...): restart the quiet window."""
        if self._closed:
            return
        self._quiet_since = self._clock()

    def saved_decision_is_active(self, *, boot_id: str, recorded_at: datetime) -> bool:
        """Ruling 4 evaluation-time gate: is this saved decision active under the CURRENT regime?

        Active iff the decision is of the current boot (the 5.2 axis) and
        was recorded under the current snapshot regime: the initial regime
        extends to the unbounded past, each reload regime starts at its
        application instant (boundary included). Prior-regime decisions
        stay in the durable log as audit and never auto-activate here —
        the host's new always-reply under the new regime does.
        """
        regime = self._regimes[-1]
        if boot_id != regime.boot_id:
            return False
        return regime.since is None or recorded_at >= regime.since

    async def check_idle(self) -> IdleObservation:
        """The read-only three-condition observation (ruling 3): nothing is applied.

        The host calls this to decide whether to offer a reload; the
        observation never cancels or interrupts any in-flight activity —
        its only state effect is the orchestrator's own quiet-window
        bookkeeping (an occupied-to-clear transition restarts the window
        from the observation, never earlier).
        """
        self._require_open()
        async with self._lock:
            return await self._observe()

    async def reload(self) -> ReloadResult:
        """The host explicit reload command (ruling 2): the only path to a new snapshot.

        Under the orchestrator lock: the idle gate is observed (fail fast —
        no resolve is spent on a busy face), the new runtime is resolved
        through the host-injected callable, and the precondition is
        re-verified on fresh state immediately before the swap (the 3.2
        commit-lock discipline at the application point — state that
        changed during the resolve is caught and the reload is rejected).
        The fresh canonical hash is computed from the new snapshot's bytes
        and the pair is re-verified through the contracts model; only then
        does the regime swap and does the new runtime become the one the
        host creates subsequent sessions with. A resolve that fails raises
        the loader's named ``ConfigError`` with the old regime intact
        (6.4/L340: stop, never silently swap).
        """
        self._require_open()
        async with self._lock:
            pre = await self._observe()
            if not pre.idle:
                return self._rejected(pre)
            candidate: object = self._resolve()
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, ResolvedRuntime):
                raise TypeError(f"the resolve callable must return a ResolvedRuntime, got {type(candidate).__name__}")
            post = await self._observe()
            if not post.idle:
                return self._rejected(post)
            fresh_hash = compute_snapshot_hash(candidate.snapshot)
            verify_snapshot_hash(candidate.snapshot, working_dir=self._working_dir, snapshot_hash=fresh_hash)
            regime = SnapshotRegime(boot_id=self._boot_id, snapshot_hash=fresh_hash, since=self._clock())
            self._regimes.append(regime)
            self._current_runtime = candidate
            return ReloadResult(
                applied=True,
                observation=post,
                boot_id=self._boot_id,
                runtime=candidate,
                regime=regime,
                previous_snapshot_hash=self._regimes[-2].snapshot_hash,
                rejection_reason=None,
            )

    # -- internals -----------------------------------------------------------

    async def _observe(self) -> IdleObservation:
        """One fresh three-condition observation (the shared read-only core)."""
        now = self._clock()
        non_terminal = await self._read.non_terminal_runs()
        in_flight = tuple(sorted(row.run_id for row in non_terminal if row.session_id == self._session_id))
        pending = tuple(sorted(row.permission_id for row in await self._read.pending_permissions()))
        occupied = bool(in_flight or pending)
        if self._last_occupied and not occupied:
            # 1/2 just cleared, observed now: the quiet window starts no
            # earlier than this observation (conservative, never under-waits).
            self._quiet_since = max(self._quiet_since, now)
        self._last_occupied = occupied
        elapsed = (now - self._quiet_since).total_seconds()
        return IdleObservation(
            no_in_flight_runs=not in_flight,
            no_pending_approvals=not pending,
            quiet_window_elapsed=elapsed >= self._quiet_window_s,
            in_flight_run_ids=in_flight,
            pending_permission_ids=pending,
            quiet_seconds_remaining=max(0.0, self._quiet_window_s - elapsed),
            observed_at=now,
        )

    def _rejected(self, observation: IdleObservation) -> ReloadResult:
        """The structured rejection: the host may retry after resolving the reason."""
        return ReloadResult(
            applied=False,
            observation=observation,
            boot_id=self._boot_id,
            runtime=None,
            regime=None,
            previous_snapshot_hash=self._regimes[-1].snapshot_hash,
            rejection_reason=_rejection_reason(observation),
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("the reload orchestrator is closed; commands are refused after close")
