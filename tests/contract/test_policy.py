"""Contract tests for the R3-S2 permission policy engine + approval protocol.

Contract anchors: CORE-CONTRACTS §5.2 L265-270 (the decision vocabulary and
evaluation order), §5.2 L266 (default table + rule priority + workspace-root
restriction), §5.2 L268 (the approval protocol: once/always/reject, the 300 s
default deadline), §5.2 L270 (saved decisions: exact binding, boot isolation,
expiry/revocation, the unknown-effect retry block), §7 L173 (the CAS: first
transition wins, losers surface via ReplyResolution), §7 L316 (the approval
channel independent of the run locks), §7 L337 (close leaves no background
tasks behind). The durable py.permission.* producers are the Invoker's job
and are pinned by the integration tests (tests/integration/test_approval.py);
these tests pin the pure engine and the in-process channel.

④ disclosure: the approval races run on the real asyncio scheduler; expiry is
exercised with a short real timeout (0.02 s watchdog) -- the same code path
as the 300 s default, no injected clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opencode.contracts.messages import JsonObject
from opencode.tools import (
    DEFAULT_APPROVAL_TIMEOUT_S,
    ApprovalCoordinator,
    ApprovalOutcome,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
    SavedDecision,
    extract_resources,
)
from opencode.tools import policy as policy_module

_NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
_SAVED_RESOURCES = ("C:/work/out.txt",)


def _clock() -> datetime:
    return _NOW


def _evaluate(
    policy: PermissionPolicy,
    *,
    session_id: str = "ses_000001",
    action: str = "fs__write",
    resources: tuple[str, ...] = _SAVED_RESOURCES,
    input_hash: str = "a" * 64,
    working_dir: str = "C:/work",
    side_effects: bool = True,
) -> PermissionDecision:
    return policy.evaluate(
        session_id=session_id,
        action=action,
        resources=resources,
        input_hash=input_hash,
        working_dir=working_dir,
        side_effects=side_effects,
    )


def _saved_decision(
    *,
    decision_id: str = "dec_000001",
    session_id: str = "ses_000001",
    action: str = "fs__write",
    resources: tuple[str, ...] = _SAVED_RESOURCES,
    boot_id: str = "boot_contract",
    expires_at: datetime | None = None,
) -> SavedDecision:
    return SavedDecision(
        decision_id=decision_id,
        session_id=session_id,
        action=action,
        resources=resources,
        input_hash="c" * 64,
        boot_id=boot_id,
        expires_at=expires_at,
    )


def _request(
    permission_id: str, *, run_id: str = "run_000001", expires_at: datetime | None = None
) -> PermissionRequest:
    return PermissionRequest(
        permission_id=permission_id,
        session_id="ses_000001",
        run_id=run_id,
        execution_id="exec_000001",
        action="fs__write",
        resources=_SAVED_RESOURCES,
        input_hash="a" * 64,
        expires_at=expires_at if expires_at is not None else _NOW + timedelta(seconds=300),
    )


# ---------------------------------------------------------------------------
# §5.2 L266 evaluation order: default table, root restriction, rules
# ---------------------------------------------------------------------------


def test_default_table_reads_allow_writes_ask(tmp_path: Path) -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    cases = {
        "fs__read": "allow",
        "fs__glob": "allow",
        "fs__grep": "allow",
        "fs__ls": "allow",
        "fs__write": "ask",
        "fs__edit": "ask",
        "bash__exec": "ask",
        "mcp__server_call": "ask",
    }
    for action, effect in cases.items():
        decision = _evaluate(policy, action=action, resources=(str(tmp_path / "note.txt"),), working_dir=str(tmp_path))
        assert (decision.effect, decision.source) == (effect, "default"), action


def test_unknown_action_falls_back_to_declared_side_effects() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    asking = _evaluate(policy, action="net__fetch", side_effects=True)
    assert (asking.effect, asking.source) == ("ask", "side_effects")
    allowing = _evaluate(policy, action="app__wait", side_effects=False)
    assert (allowing.effect, allowing.source) == ("allow", "side_effects")


def test_root_restriction_denies_fs_escape(tmp_path: Path) -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    inside = _evaluate(policy, action="fs__read", resources=(str(tmp_path / "a.txt"),), working_dir=str(tmp_path))
    assert (inside.effect, inside.source) == ("allow", "default")
    escape = _evaluate(policy, action="fs__read", resources=("C:/Windows/win.ini",), working_dir=str(tmp_path))
    assert escape.effect == "deny"
    assert escape.source == "root_restriction"
    traversal = _evaluate(
        policy,
        action="fs__write",
        resources=(str(tmp_path / "sub" / ".." / ".." / "stash.txt"),),
        working_dir=str(tmp_path),
    )
    assert traversal.source == "root_restriction"


def test_root_restriction_denies_unresolvable_resource(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)

    class _UnresolvablePath:
        # Path stand-in whose resolve() raises for the poisoned resource only: the
        # host-tier guard treats an unresolvable path as an escape (fail closed).
        def __init__(self, value: str | Path) -> None:
            self._value = str(value)

        def is_absolute(self) -> bool:
            return True

        def resolve(self) -> Path:
            if "poison" in self._value:
                raise OSError("unresolvable")
            return Path(self._value)

        def __truediv__(self, other: object) -> _UnresolvablePath:
            return _UnresolvablePath(f"{self._value}/{other}")

    monkeypatch.setattr(policy_module, "Path", _UnresolvablePath)
    decision = _evaluate(
        policy,
        action="fs__write",
        resources=(str(tmp_path / "poison.txt"),),
        working_dir=str(tmp_path),
    )
    assert (decision.effect, decision.source) == ("deny", "root_restriction")


def test_host_deny_outranks_rules_and_saved_decisions(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        boot_id="boot_contract",
        clock=_clock,
        host_deny_actions=("fs__read",),
        rules=(PermissionRule(action="fs__read", effect="allow", source="project", reason="project allows reads"),),
    )
    resources = (str(tmp_path / "a.txt"),)
    decision = _evaluate(policy, action="fs__read", resources=resources, working_dir=str(tmp_path))
    assert (decision.effect, decision.source) == ("deny", "host_deny")
    policy.register_saved(
        _saved_decision(action="fs__read", resources=resources),
    )
    again = _evaluate(policy, action="fs__read", resources=resources, working_dir=str(tmp_path))
    assert (again.effect, again.source) == ("deny", "host_deny")


def test_rules_cannot_relax_root_restriction(tmp_path: Path) -> None:
    policy = PermissionPolicy(
        boot_id="boot_contract",
        clock=_clock,
        rules=(PermissionRule(action="fs__write", effect="allow", source="host"),),
    )
    decision = _evaluate(policy, action="fs__write", resources=("C:/elsewhere/x.txt",), working_dir=str(tmp_path))
    assert (decision.effect, decision.source) == ("deny", "root_restriction")


def test_rule_priority_and_resource_pinning() -> None:
    policy = PermissionPolicy(
        boot_id="boot_contract",
        clock=_clock,
        rules=(
            PermissionRule(action="fs__write", effect="deny", source="host", resources=_SAVED_RESOURCES),
            PermissionRule(action="fs__write", effect="allow", source="host"),
            PermissionRule(action="fs__write", effect="allow", source="project"),
        ),
    )
    pinned = _evaluate(policy, resources=_SAVED_RESOURCES)
    assert (pinned.effect, pinned.source) == ("deny", "rule:host")
    other = _evaluate(policy, resources=("C:/work/other.txt",))
    assert (other.effect, other.source) == ("allow", "rule:host")

    host_first = PermissionPolicy(
        boot_id="boot_contract",
        clock=_clock,
        rules=(
            PermissionRule(action="fs__write", effect="allow", source="project"),
            PermissionRule(action="fs__write", effect="ask", source="host"),
        ),
    )
    decision = _evaluate(host_first, resources=("C:/work/other.txt",))
    assert (decision.effect, decision.source) == ("ask", "rule:host")


# ---------------------------------------------------------------------------
# §5.2 L268/L270 saved decisions
# ---------------------------------------------------------------------------


def test_saved_decision_exact_binding_allows() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    policy.register_saved(_saved_decision(expires_at=_NOW + timedelta(seconds=3600)))
    hit = _evaluate(policy, input_hash="d" * 64)
    assert (hit.effect, hit.source) == ("allow", "saved_decision:dec_000001")
    assert hit.audit_id == "dec_000001"
    assert hit.expires_at == _NOW + timedelta(seconds=3600)
    # No wildcard expansion: another resource set, session or action falls through.
    assert _evaluate(policy, resources=("C:/work/other.txt",)).effect == "ask"
    assert _evaluate(policy, session_id="ses_000002").effect == "ask"
    assert _evaluate(policy, action="fs__edit").effect == "ask"


def test_saved_decision_expiry_lifecycle() -> None:
    mutable_now = {"value": _NOW}

    def clock() -> datetime:
        return mutable_now["value"]

    policy = PermissionPolicy(boot_id="boot_contract", clock=clock)
    policy.register_saved(_saved_decision(expires_at=_NOW + timedelta(seconds=1)))
    assert _evaluate(policy).effect == "allow"
    mutable_now["value"] = _NOW + timedelta(seconds=2)
    assert _evaluate(policy).effect == "ask"
    due = policy.due_expired("ses_000001")
    assert [decision.decision_id for decision in due] == ["dec_000001"]
    policy.mark_expired(["dec_000001"])
    assert policy.due_expired("ses_000001") == ()
    assert policy.decision("dec_000001") is not None
    assert policy.decision("dec_000001").state == "expired"  # type: ignore[union-attr]
    assert _evaluate(policy).effect == "ask"


def test_saved_decision_revocation_is_fail_closed() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    policy.register_saved(_saved_decision())
    assert _evaluate(policy).effect == "allow"
    assert policy.deactivate("dec_000001") is True
    revoked = policy.decision("dec_000001")
    assert revoked is not None and revoked.state == "revoked"
    assert _evaluate(policy).effect == "ask"
    assert policy.deactivate("dec_000001") is False
    assert policy.deactivate("dec_missing") is False


def test_unknown_effect_retry_blocks_saved_decision() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    policy.register_saved(_saved_decision())
    assert _evaluate(policy, input_hash="e" * 64).effect == "allow"
    policy.record_unknown("ses_000001", "fs__write", "e" * 64)
    assert _evaluate(policy, input_hash="e" * 64).effect == "ask"
    # The block is keyed by the input hash: a different call still auto-allows.
    assert _evaluate(policy, input_hash="f" * 64).effect == "allow"


def test_foreign_boot_and_duplicate_registration_fail_closed() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    assert policy.boot_id == "boot_contract"
    with pytest.raises(ValueError, match="boot"):
        policy.register_saved(_saved_decision(boot_id="boot_other"))
    policy.register_saved(_saved_decision())
    with pytest.raises(ValueError, match="already registered"):
        policy.register_saved(_saved_decision())


def test_id_minters_are_prefixed_and_unique() -> None:
    policy = PermissionPolicy(boot_id="boot_contract", clock=_clock)
    first = policy.mint_permission_id()
    second = policy.mint_permission_id()
    assert first.startswith("prm_")
    assert second.startswith("prm_")
    assert first != second
    assert policy.mint_decision_id().startswith("dec_")


def test_extract_resources_binds_path_and_command() -> None:
    assert extract_resources("fs__write", {"path": "out.txt"}) == ("out.txt",)
    assert extract_resources("bash__exec", {"command": "ls", "cwd": "/tmp"}) == ("ls", "/tmp")
    assert extract_resources("bash__exec", {"command": "ls"}) == ("ls",)
    assert extract_resources("app__wait", {}) == ()
    empty_path: JsonObject = {"path": ""}
    assert extract_resources("fs__write", empty_path) == ()


# ---------------------------------------------------------------------------
# §7 L316/L173 approval channel
# ---------------------------------------------------------------------------


def test_coordinator_timeout_validation_and_default_deadline() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        ApprovalCoordinator(timeout_s=0)
    with pytest.raises(ValueError, match="timeout_s"):
        ApprovalCoordinator(timeout_s=float("inf"))
    coordinator = ApprovalCoordinator(clock=_clock)
    assert coordinator.timeout_s == DEFAULT_APPROVAL_TIMEOUT_S == 300.0
    assert coordinator.expires_at(_NOW) == _NOW + timedelta(seconds=300)


async def test_reply_once_resolves_the_future_exactly_once() -> None:
    coordinator = ApprovalCoordinator(clock=_clock)
    future = await coordinator.register(_request("prm_000001"))
    assert not future.done()
    pending = await coordinator.pending_request("prm_000001")
    assert pending is not None and pending.permission_id == "prm_000001"
    resolution = await coordinator.resolve_reply("prm_000001", "once")
    assert resolution.status == "replied"
    assert resolution.outcome == ApprovalOutcome(outcome="allow", reply="once", reason="approved by host (once)")
    outcome = await future
    assert (outcome.outcome, outcome.reply) == ("allow", "once")
    assert await coordinator.pending_request("prm_000001") is None
    with pytest.raises(ValueError, match="already registered"):
        await coordinator.register(_request("prm_000001"))


async def test_reply_reject_and_always_carry_their_vocabulary() -> None:
    coordinator = ApprovalCoordinator(clock=_clock)
    reject_future = await coordinator.register(_request("prm_reject"))
    rejected = await coordinator.resolve_reply("prm_reject", "reject")
    assert rejected.status == "replied"
    assert rejected.outcome is not None and rejected.outcome.outcome == "deny"
    assert (await reject_future).outcome == "deny"

    always_future = await coordinator.register(_request("prm_always"))
    approved = await coordinator.resolve_reply("prm_always", "always")
    assert approved.status == "replied"
    assert approved.outcome is not None and approved.outcome.reply == "always"
    assert (await always_future).outcome == "allow"


async def test_late_and_unknown_replies_are_classified() -> None:
    coordinator = ApprovalCoordinator(clock=_clock)
    await coordinator.register(_request("prm_000001"))
    await coordinator.resolve_reply("prm_000001", "once")
    late = await coordinator.resolve_reply("prm_000001", "reject")
    assert late.status == "late"
    assert late.outcome is not None and late.outcome.outcome == "allow"
    unknown = await coordinator.resolve_reply("prm_missing", "once")
    assert (unknown.status, unknown.outcome) == ("unknown", None)


async def test_close_run_cancels_only_that_run() -> None:
    coordinator = ApprovalCoordinator(clock=_clock)
    future_a = await coordinator.register(_request("prm_a", run_id="run_000001"))
    future_b = await coordinator.register(_request("prm_b", run_id="run_000002"))
    closed = await coordinator.close_run("run_000001", "run cancelled while awaiting approval")
    assert closed == 1
    outcome_a = await future_a
    assert (outcome_a.outcome, outcome_a.reason) == ("cancelled", "run cancelled while awaiting approval")
    assert not future_b.done()
    assert (await coordinator.resolve_reply("prm_b", "once")).status == "replied"
    late = await coordinator.resolve_reply("prm_a", "once")
    assert late.status == "late"
    assert late.outcome is not None and late.outcome.outcome == "cancelled"


async def test_close_all_cancels_every_pending_and_refuses_new_registrations() -> None:
    coordinator = ApprovalCoordinator(clock=_clock)
    future_a = await coordinator.register(_request("prm_a", run_id="run_000001"))
    future_b = await coordinator.register(_request("prm_b", run_id="run_000002"))
    closed = await coordinator.close_all("service close while awaiting approval")
    assert closed == 2
    for future in (future_a, future_b):
        assert (await future).outcome == "cancelled"
    assert await coordinator.close_all("service close while awaiting approval") == 0
    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.register(_request("prm_c"))


async def test_expiry_watchdog_resolves_expired() -> None:
    coordinator = ApprovalCoordinator(timeout_s=0.02)
    expires_at = datetime.now(UTC) + timedelta(seconds=0.02)
    future = await coordinator.register(_request("prm_000001", expires_at=expires_at))
    outcome = await future
    assert outcome.outcome == "expired"
    assert outcome.reason == "approval timed out"
    late = await coordinator.resolve_reply("prm_000001", "once")
    assert late.status == "late"
    assert late.outcome is not None and late.outcome.outcome == "expired"
