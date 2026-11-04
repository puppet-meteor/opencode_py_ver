"""Permission policy engine + approval protocol (R3-S2; CORE-CONTRACTS §5.2, §7 L173/L316).

Single-file choice (①): the engine and the approval channel live together
because they share one vocabulary (:class:`PermissionDecision` §5.2 L265,
:class:`SavedDecision` L268/L270, :class:`ApprovalOutcome`) and one wiring
point (the App composes one :class:`ApprovalCoordinator` and injects it into
one :class:`PermissionPolicy`); no contracts module is needed because every
wire shape it touches is already versioned in ``contracts.events`` (the six
``py.permission.*`` data models). Import discipline: contracts-only imports —
this module sits *under* ``tools.invoker``/``session.service`` in the import
graph, so importing either from here would create a cycle (service imports
this module under TYPE_CHECKING only).

- :func:`PermissionPolicy.evaluate` implements the §5.2 L266 order: host hard
  deny -> root containment (both host tier; project rules can never relax
  them) -> saved decisions (current boot, this session, not expired/revoked,
  unknown-retry not blocked) -> explicit rules (source priority + order) ->
  the default table (in-root read/glob/grep/ls allow; write/edit/bash/MCP
  ask) -> the tool's declared ``side_effects`` for actions outside the
  enumeration (True -> ask, False -> allow). An unhandled ask is never
  turned into an allow (§5.2 L266): the caller maps it to
  ApprovalUnavailable/cancelled.
- :class:`ApprovalCoordinator` is the in-process approval channel (§7 L316:
  independent of the run locks). One short asyncio.Lock is the sole §7 L173
  race arbiter: reply / expiry / run-cancel / close-all resolve a pending
  approval under it, the first legal transition wins, losers surface via
  :class:`ReplyResolution`. Each registration owns one expiry watchdog task
  sleeping until ``expires_at`` (the §5.2 L268 300 s default; a short
  ``timeout_s`` is the test seam), resolutions cancel their watchdog, and
  :meth:`ApprovalCoordinator.close_all` cancels every remaining one (§7
  L337: close leaves no background tasks behind).

TS anchor (recorded fingerprint #102): ``packages/core/src/permission/
saved.ts`` once/always/ConflictError semantics; the TS approval UI faces are
deliberately not ported (r3-slices §4 tables 3/4), and the durable producer
of ``py.permission.*`` is the Invoker (T-B-027 ④-1), never this module.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from opencode.contracts.messages import JsonObject

#: §5.2 L268: the default approval deadline (300 seconds).
DEFAULT_APPROVAL_TIMEOUT_S = 300.0

#: Sanitized coordinator close reasons (surface in ``py.permission.closed.reason``).
REASON_APPROVAL_UNCONNECTED = "approval channel not connected"
REASON_APPROVAL_TIMED_OUT = "approval timed out"
REASON_APPROVAL_RUN_CANCELLED = "run cancelled while awaiting approval"
REASON_APPROVAL_SHUTDOWN = "service close while awaiting approval"

#: Sanitized policy reasons for the saved-decision lifecycle events (§5.2 L270).
REASON_DECISION_REVOKED = "revoked by host"
REASON_DECISION_EXPIRED = "saved decision expired"

#: §5.2 L266 default table: in-root read/glob/grep/ls allow, write/edit ask.
#: R3-S5 added ``bash__run`` (scheduler ledger row #28 nit-1, T-C1-030 item 11):
#: the explicit key documents the shell tool in the table with the same
#: "ask" the ``_ASK_NAMESPACES`` bash row already produced -- a zero-behavior
#: closure (the existing bash approval/contract tests stay green either way).
#: Actions outside the enumeration fall back to the tool's declared
#: ``side_effects`` (True -> ask, False -> allow) -- the R2-era tools that
#: declare no side effects (app.wait/app.slow class) stay allow. This
#: Python-side conservative default is the sanctioned fallback (CORE-CONTRACTS
#: §5.2 L266: the enumeration is the authority, side_effects is the residual
#: rule), recorded here per ledger row #28.
_DEFAULT_EFFECTS: dict[str, Literal["allow", "ask"]] = {
    "fs__read": "allow",
    "fs__glob": "allow",
    "fs__grep": "allow",
    "fs__ls": "allow",
    "fs__write": "ask",
    "fs__edit": "ask",
    "bash__run": "ask",
}

#: Namespace-level default (§5.2 L266): every bash/MCP action asks.
_ASK_NAMESPACES = frozenset({"bash", "mcp"})

#: §5.2 L266 rule source priority: host-configured rules outrank project rules.
_RULE_SOURCE_PRIORITY: dict[str, int] = {"host": 0, "project": 1}

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _epoch_ms(value: datetime) -> int:
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


class _IdMinter:
    """Process-local ascending ids for the permission/decision faces.

    The §3.1 patterned ids (evt_/msg_/ses_/run_) do not cover permission_id/
    decision_id/boot_id (plain non-empty strings), and the shared
    :class:`~opencode.session.service.IdFactory` protocol must not gain
    methods (R2 test doubles implement exactly five), so this local minter
    mirrors the AscendingIds ``<13-digit ms><10-digit counter>`` format:
    time-ordered, lexicographically sortable, unique per process.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else _utc_now
        self._last_ms = -1
        self._counter = 0

    def next(self, prefix: str) -> str:
        ms = _epoch_ms(self._clock())
        if ms > self._last_ms:
            self._last_ms = ms
            self._counter = 0
        else:
            self._counter += 1
        return f"{prefix}{ms:013d}{self._counter:010d}"


# ---------------------------------------------------------------------------
# §5.2 value shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """§5.2 L265: ``{effect, reason, action, resources, input_hash, source, expires_at?, audit_id}``.

    ``source`` names the deciding tier (host_deny / root_restriction /
    saved_decision:<id> / rule:<source> / default / side_effects) for audit
    display; ``expires_at``/``audit_id`` are set only for saved-decision
    allows (the saved decision's own expiry and id).
    """

    effect: Literal["allow", "ask", "deny"]
    reason: str
    action: str
    resources: tuple[str, ...]
    input_hash: str
    source: str
    expires_at: datetime | None = None
    audit_id: str | None = None


@dataclass(frozen=True, slots=True)
class SavedDecision:
    """One registered always-decision (§5.2 L268/L270): current boot + one session.

    The activation binding is exactly ``(session_id, action, resources)`` with
    the current ``boot_id`` -- a concrete, displayable rule with no wildcard
    expansion possible (lookup is exact-match only). ``input_hash`` is audit
    provenance of the approving call, not part of the always binding; an
    identical retry after an unknown-effect terminal is re-asked instead
    (§5.2 L270). ``state`` is the in-memory lifecycle; the durable terminal
    is the py.permission.revoked/expired event.
    """

    decision_id: str
    session_id: str
    action: str
    resources: tuple[str, ...]
    input_hash: str
    boot_id: str
    expires_at: datetime | None
    state: Literal["saved", "expired", "revoked"] = "saved"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """One pending approval handed to the approval channel (§5.2 L268).

    Carries everything the host channel (``reply_permission``) and the
    expiry/cancel paths need: the pinned execution identity, the concrete
    action/resources, the input hash and the approval deadline.
    """

    permission_id: str
    session_id: str
    run_id: str
    execution_id: str
    action: str
    resources: tuple[str, ...]
    input_hash: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """The coordinator's terminal answer for one approval (first transition wins).

    ``outcome`` "allow"/"deny" come only from a host reply (``reply`` carries
    the once/always/reject vocabulary); "expired"/"cancelled" are the system
    closes (``reason`` feeds ``py.permission.closed.reason``).
    """

    outcome: Literal["allow", "deny", "expired", "cancelled"]
    reply: Literal["once", "always", "reject"] | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReplyResolution:
    """The CAS result of a host reply attempt (§7 L173: first transition wins).

    ``replied``: this reply won (``outcome`` is its terminal). ``late``: a
    terminal already stands (``outcome`` is the earlier one; an expired
    earlier terminal maps to ApprovalExpired, everything else to Conflict).
    ``unknown``: the coordinator never held this permission_id.
    """

    status: Literal["replied", "late", "unknown"]
    outcome: ApprovalOutcome | None = None


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """One explicit rule (§5.2 L266): matched after saved decisions, before defaults.

    ``resources`` pins the rule to one concrete resource set (None matches
    any); ``source`` drives the priority order (host before project, then
    insertion order -- later/higher-priority rules override the default).
    """

    action: str
    effect: Literal["allow", "ask", "deny"]
    source: Literal["host", "project"] = "host"
    resources: tuple[str, ...] | None = None
    reason: str = ""


#: Known fields of one snapshot ``permission_rules`` entry (R3-S7). ``source``
#: is deliberately absent: the tier of a config-file rule is host authority
#: (§5.2 L266), so an entry trying to declare its own source fails closed.
_SNAPSHOT_RULE_KEYS: frozenset[str] = frozenset({"action", "effect", "resources", "reason"})


def permission_rules_from_snapshot(rules: Sequence[JsonObject]) -> tuple[PermissionRule, ...]:
    """Convert snapshot v2 ``permission_rules`` entries into engine rules (R3-S7).

    The wiring seam of the CLI assembly: the loader validated each entry's
    shape at load time (the exit-2 face), so the host normally reaches this
    helper with legal entries. It stays fail-closed anyway -- a snapshot
    recovered from the store (or any non-loader source) was never
    load-validated, and a malformed entry raises ValueError (the host's
    exit-70 face) instead of silently dropping a rule. Every rule assembles
    with ``source="project"``: config-file rules extend the project tier and
    can never outrank the host tier (§5.2 L266 source priority).
    """
    converted: list[PermissionRule] = []
    for index, entry in enumerate(rules):
        if not isinstance(entry, dict):
            raise ValueError(f"permission rule #{index} must be a JSON object")
        for key in entry:
            if key not in _SNAPSHOT_RULE_KEYS:
                raise ValueError(f"permission rule #{index} has unknown field {key!r}")
        action = entry["action"]
        if not isinstance(action, str) or not action:
            raise ValueError(f"permission rule #{index} needs a non-empty string 'action'")
        effect = entry["effect"]
        if effect not in ("allow", "ask", "deny"):
            raise ValueError(f"permission rule #{index} effect must be allow/ask/deny, got {effect!r}")
        resources = entry.get("resources")
        if resources is not None and (
            not isinstance(resources, list) or not all(isinstance(item, str) and item for item in resources)
        ):
            raise ValueError(f"permission rule #{index} 'resources' must be a list of non-empty strings")
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(f"permission rule #{index} 'reason' must be a string")
        converted.append(
            PermissionRule(
                action=action,
                effect=cast('Literal["allow", "ask", "deny"]', effect),
                source="project",
                resources=tuple(cast("list[str]", resources)) if resources is not None else None,
                reason=reason,
            )
        )
    return tuple(converted)


def extract_resources(action: str, raw_input: JsonObject) -> tuple[str, ...]:
    """Extract the concrete resource binding of one call (§5.2 L268).

    Policy-owned convention mirroring the TS ``Permission.Request`` anchor
    (action string + resources array): fs-family tools bind their ``path``
    target; shell-family tools bind the full command/cwd pair (the shell tool
    itself is R3-S5 -- the binding face is fixed here). Tools with neither
    input carry no resources.
    """
    path = raw_input.get("path")
    if isinstance(path, str) and path:
        return (path,)
    command = raw_input.get("command")
    if isinstance(command, str) and command:
        cwd = raw_input.get("cwd")
        return (command, cwd) if isinstance(cwd, str) and cwd else (command,)
    return ()


def _escaping_resource(action: str, root: str, resources: tuple[str, ...]) -> str | None:
    """The first fs-family resource escaping ``root`` (host tier, §5.2 L266/L274).

    A local trusted-workspace misuse guard, not an adversarial defense: both
    sides are resolved so case/symlink normalization cannot fake containment,
    and an unresolvable path is conservatively treated as an escape.
    """
    if not action.startswith("fs__"):
        return None
    base = Path(root).resolve()
    for resource in resources:
        candidate = Path(resource)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return resource
        if not resolved.is_relative_to(base):
            return resource
    return None


# ---------------------------------------------------------------------------
# §7 L316/L173 approval channel
# ---------------------------------------------------------------------------


@dataclass
class _PendingEntry:
    """One registered approval: the request plus its resolve-once future."""

    request: PermissionRequest
    future: asyncio.Future[ApprovalOutcome]


class ApprovalCoordinator:
    """The in-process approval channel: the pending table + the §7 L173 CAS.

    The pending table is keyed by permission_id; terminal outcomes are kept
    (in-memory, process lifetime) so late replies can be classified. All
    resolution paths (reply, expiry, run-cancel, close-all) run under one
    short lock -- held never across model/adapter waits (§7 L173) -- and the
    first transition wins: the loser's reply surfaces ``late`` with the
    standing terminal, never a second resolution of the same future.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
    ) -> None:
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not timeout_s > 0
            or not math.isfinite(timeout_s)
        ):
            raise ValueError("timeout_s must be a positive finite number of seconds")
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._timeout_s = float(timeout_s)
        self._lock = asyncio.Lock()
        self._pending: dict[str, _PendingEntry] = {}
        self._terminal: dict[str, ApprovalOutcome] = {}
        self._watchdogs: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def timeout_s(self) -> float:
        """The approval deadline duration (§5.2 L268 default: 300 s)."""
        return self._timeout_s

    def expires_at(self, now: datetime) -> datetime:
        """The approval deadline for a request starting at ``now``."""
        return now + timedelta(seconds=self._timeout_s)

    async def register(self, request: PermissionRequest) -> asyncio.Future[ApprovalOutcome]:
        """Register one pending approval; the returned future resolves exactly once.

        The Invoker commits ``py.permission.requested`` *before* calling
        this (durable record first: a crash between the two leaves the
        permission for restart recovery, never an unregistered wait).
        """
        future: asyncio.Future[ApprovalOutcome] = asyncio.get_running_loop().create_future()
        async with self._lock:
            if self._closed:
                raise RuntimeError("the approval coordinator is closed")
            if request.permission_id in self._pending or request.permission_id in self._terminal:
                raise ValueError(f"permission {request.permission_id!r} is already registered")
            self._pending[request.permission_id] = _PendingEntry(request, future)
            self._watchdogs[request.permission_id] = asyncio.create_task(
                self._expiry_watchdog(request.permission_id, request.expires_at)
            )
        return future

    async def pending_request(self, permission_id: str) -> PermissionRequest | None:
        """The still-pending request for ``permission_id`` (None otherwise)."""
        async with self._lock:
            entry = self._pending.get(permission_id)
            return None if entry is None else entry.request

    async def resolve_reply(self, permission_id: str, reply: Literal["once", "always", "reject"]) -> ReplyResolution:
        """Resolve a host reply under the CAS (§7 L173 first transition wins)."""
        terminal = (
            ApprovalOutcome(outcome="deny", reply="reject", reason="rejected by host")
            if reply == "reject"
            else ApprovalOutcome(outcome="allow", reply=reply, reason=f"approved by host ({reply})")
        )
        async with self._lock:
            if permission_id in self._pending:
                self._resolve_locked(permission_id, terminal)
                return ReplyResolution(status="replied", outcome=terminal)
            standing = self._terminal.get(permission_id)
            if standing is None:
                return ReplyResolution(status="unknown")
            return ReplyResolution(status="late", outcome=standing)

    async def close_run(self, run_id: str, reason: str) -> int:
        """Resolve every pending approval of ``run_id`` as cancelled; returns the count."""
        closed = 0
        async with self._lock:
            doomed = [pid for pid, entry in self._pending.items() if entry.request.run_id == run_id]
            for permission_id in doomed:
                if self._resolve_locked(permission_id, ApprovalOutcome(outcome="cancelled", reason=reason)):
                    closed += 1
        return closed

    async def close_all(self, reason: str) -> int:
        """Close every pending approval and cancel every watchdog (§7 L337/OP-9).

        Idempotent; after it the coordinator refuses new registrations (the
        service closing gate already refuses the writes that would produce
        them -- the raise is a defensive backstop, ④ untested defense).
        """
        closed = 0
        async with self._lock:
            self._closed = True
            for permission_id in list(self._pending):
                if self._resolve_locked(permission_id, ApprovalOutcome(outcome="cancelled", reason=reason)):
                    closed += 1
            for watchdog in list(self._watchdogs.values()):
                watchdog.cancel()
            self._watchdogs.clear()
        return closed

    async def _expiry_watchdog(self, permission_id: str, expires_at: datetime) -> None:
        """Sleep until ``expires_at``, then resolve the approval as expired."""
        delay = (expires_at - self._clock()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        await self._resolve(permission_id, ApprovalOutcome(outcome="expired", reason=REASON_APPROVAL_TIMED_OUT))

    async def _resolve(self, permission_id: str, terminal: ApprovalOutcome) -> bool:
        async with self._lock:
            return self._resolve_locked(permission_id, terminal)

    def _resolve_locked(self, permission_id: str, terminal: ApprovalOutcome) -> bool:
        """CAS body: resolve the pending entry, cancel its watchdog, record the terminal."""
        entry = self._pending.pop(permission_id, None)
        if entry is None:
            return False
        self._terminal[permission_id] = terminal
        watchdog = self._watchdogs.pop(permission_id, None)
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        if not entry.future.done():
            entry.future.set_result(terminal)
        return True


# ---------------------------------------------------------------------------
# §5.2 L265-270 evaluation engine
# ---------------------------------------------------------------------------


class PermissionPolicy:
    """The §5.2 evaluation engine + the current-boot saved-decision registry.

    The policy is deliberately in-memory: only decisions minted by *this*
    process (this ``boot_id``) are ever registered, so boot isolation holds
    by construction (a recovered session's audit rows stay in the store and
    never activate, §5.2 L270). Expiry and revocation flip the in-memory
    state only *after* the Invoker committed the matching durable event --
    except ``deactivate``, which flips first (fail-closed direction: a
    revoked rule stops matching even if the durable commit later fails).
    """

    def __init__(
        self,
        *,
        boot_id: str,
        approvals: ApprovalCoordinator | None = None,
        host_deny_actions: Iterable[str] = (),
        rules: Sequence[PermissionRule] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("boot_id must be a non-empty string")
        self._boot_id = boot_id
        self._approvals = approvals
        self._host_deny = frozenset(host_deny_actions)
        self._rules = tuple(sorted(rules, key=lambda rule: _RULE_SOURCE_PRIORITY[rule.source]))
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._minter = _IdMinter(self._clock)
        self._decisions: dict[str, SavedDecision] = {}
        self._unknown_retries: set[tuple[str, str, str]] = set()

    @property
    def boot_id(self) -> str:
        """The process boot identity (§5.2 L270: minted once per App start)."""
        return self._boot_id

    @property
    def approvals(self) -> ApprovalCoordinator | None:
        """The wired approval channel (None = ask is unhandled, §5.2 L268)."""
        return self._approvals

    def mint_permission_id(self) -> str:
        """A fresh permission_id for one approval request."""
        return self._minter.next("prm_")

    def mint_decision_id(self) -> str:
        """A fresh decision_id for one saved always-decision."""
        return self._minter.next("dec_")

    def evaluate(
        self,
        *,
        session_id: str,
        action: str,
        resources: tuple[str, ...],
        input_hash: str,
        working_dir: str,
        side_effects: bool,
    ) -> PermissionDecision:
        """Evaluate one call to its §5.2 L265 decision (order is semantics).

        Host hard deny -> root containment -> saved decisions (current boot,
        this session, not expired/revoked, unknown-retry not blocked) ->
        explicit rules (source priority, then order) -> the L266 default
        table -> the declared ``side_effects`` fallback. A caller that
        cannot handle the returned "ask" must surface ApprovalUnavailable,
        never convert it to allow.
        """
        now = self._clock()
        if action in self._host_deny:
            return PermissionDecision(
                effect="deny",
                reason=f"action {action!r} is denied by host policy",
                action=action,
                resources=resources,
                input_hash=input_hash,
                source="host_deny",
            )
        escape = _escaping_resource(action, working_dir, resources)
        if escape is not None:
            return PermissionDecision(
                effect="deny",
                reason=f"resource {escape!r} escapes the workspace root",
                action=action,
                resources=resources,
                input_hash=input_hash,
                source="root_restriction",
            )
        saved = self._match_saved(session_id, action, resources, input_hash, now)
        if saved is not None:
            return PermissionDecision(
                effect="allow",
                reason=f"saved decision {saved.decision_id!r} approves this call",
                action=action,
                resources=resources,
                input_hash=input_hash,
                source=f"saved_decision:{saved.decision_id}",
                expires_at=saved.expires_at,
                audit_id=saved.decision_id,
            )
        for rule in self._rules:
            if rule.action == action and (rule.resources is None or rule.resources == resources):
                return PermissionDecision(
                    effect=rule.effect,
                    reason=rule.reason or f"rule for {action!r} -> {rule.effect}",
                    action=action,
                    resources=resources,
                    input_hash=input_hash,
                    source=f"rule:{rule.source}",
                )
        default: Literal["allow", "ask"] | None = _DEFAULT_EFFECTS.get(action)
        if default is None and action.partition("__")[0] in _ASK_NAMESPACES:
            default = "ask"
        if default is not None:
            return PermissionDecision(
                effect=default,
                reason=f"default permission for {action!r}",
                action=action,
                resources=resources,
                input_hash=input_hash,
                source="default",
            )
        if side_effects:
            return PermissionDecision(
                effect="ask",
                reason=f"{action!r} declares side effects",
                action=action,
                resources=resources,
                input_hash=input_hash,
                source="side_effects",
            )
        return PermissionDecision(
            effect="allow",
            reason=f"{action!r} declares no side effects",
            action=action,
            resources=resources,
            input_hash=input_hash,
            source="side_effects",
        )

    # -- saved-decision registry faces (§5.2 L268/L270) -----------------------

    def register_saved(self, decision: SavedDecision) -> None:
        """Register one saved always-decision of the current boot (§5.2 L270).

        Called by the Invoker *after* the durable ``py.permission.saved``
        commit; a foreign-boot or duplicate registration is a producer
        defect and fails closed.
        """
        if decision.boot_id != self._boot_id:
            raise ValueError(f"decision {decision.decision_id!r} belongs to boot {decision.boot_id!r}")
        if decision.decision_id in self._decisions:
            raise ValueError(f"decision {decision.decision_id!r} is already registered")
        self._decisions[decision.decision_id] = decision

    def due_expired(self, session_id: str, now: datetime | None = None) -> tuple[SavedDecision, ...]:
        """Saved decisions of ``session_id`` whose ``expires_at`` has passed (§5.2 L270).

        The Invoker commits ``py.permission.expired`` for these through the
        commit-lock sink *before* re-evaluating, so the expired decision
        stops matching only after its durable terminal exists.
        """
        moment = now if now is not None else self._clock()
        return tuple(
            decision
            for decision in self._decisions.values()
            if decision.state == "saved"
            and decision.session_id == session_id
            and decision.expires_at is not None
            and decision.expires_at <= moment
        )

    def mark_expired(self, decision_ids: Iterable[str]) -> None:
        """Flip the given saved decisions to expired (after their durable event)."""
        for decision_id in decision_ids:
            decision = self._decisions.get(decision_id)
            if decision is not None and decision.state == "saved":
                self._decisions[decision_id] = replace(decision, state="expired")

    def decision(self, decision_id: str) -> SavedDecision | None:
        """One registered decision (None = unknown to this boot, §5.2 L270)."""
        return self._decisions.get(decision_id)

    def deactivate(self, decision_id: str) -> bool:
        """Flip saved -> revoked in memory (fail-closed first, §5.2 L270).

        The service calls this *before* committing ``py.permission.revoked``:
        a failed durable commit then leaves the rule deactivated (more
        restrictive), never a revoked row that still activates.
        """
        decision = self._decisions.get(decision_id)
        if decision is None or decision.state != "saved":
            return False
        self._decisions[decision_id] = replace(decision, state="revoked")
        return True

    def record_unknown(self, session_id: str, action: str, input_hash: str) -> None:
        """Block saved-decision reuse for an identical retry after an unknown effect (§5.2 L270).

        In-memory and boot-scoped: a restart forgets the block together with
        the decisions themselves (a new boot never activates old decisions).
        """
        self._unknown_retries.add((session_id, action, input_hash))

    def _match_saved(
        self,
        session_id: str,
        action: str,
        resources: tuple[str, ...],
        input_hash: str,
        now: datetime,
    ) -> SavedDecision | None:
        """The activating saved decision for this call, if any (exact binding)."""
        for decision in self._decisions.values():
            if (
                decision.state == "saved"
                and decision.session_id == session_id
                and decision.action == action
                and decision.resources == resources
                and (decision.expires_at is None or decision.expires_at > now)
                and (session_id, action, input_hash) not in self._unknown_retries
            ):
                return decision
        return None
