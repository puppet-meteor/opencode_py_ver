"""T-C3-005 evaluation harness: always-background subagent + per-round idle receipts.

A measurement face, not an assertion face (the test_automation_eval.py form):
every probe records a metric and passes, whether or not the capability exists.
Running this module on pre-implementation code captures the BEFORE baseline;
the identical inputs on post-implementation code give the AFTER column. The
delta is the ticket's evidence against doc/GOAL.md's unattended/token gates
(长任务委派不阻塞主线; 回执按需唤醒 parent 免轮询探测——token 直接节约):

* P0 capability -- the ``subagent__run`` spec is the always-background form
  (version 2: no ``background`` input key, the always-background description
  sentence, the running-only output face);
* M1 return-immediately -- a spawn whose child parks on a gate returns in
  well under the child's remaining runtime with ``status="running"`` (the
  pre-implementation foreground face blocks: the honest reading is a
  timeout at the return deadline);
* M2 idle receipt -- the child settling delivers ONE synthetic inbox entry
  under ``subagent:{run_id}`` with the ``<subagent ... state="completed">``
  wrapper and the child's final text;
* M3 outstanding tail -- a running background shell row under the child at
  settle time keeps the receipt alive: ``state="running"`` in the wrapper
  (metadata carries the true state) plus the "Background work still
  running:" tail;
* M4 follow-up steering -- a follow-up call on a RUNNING child joins the
  current run (steer), never a SessionBusy rejection;
* M5 depth rejection -- a spawn from a session that is itself a child is
  refused (the standing v1 face, kept as the regression row);
* M6 restart replay -- a settled subagent row replays exactly once through
  ``replay_pending``; a deleted parent clears the row.

Capability probes are pinned to the banked T-C3-005 seam contract (see
doc/plans/port-232-increments.md). A post-implementation ``api-mismatch``
status means the harness and the implementation drifted: fix the alignment,
never the reading. Timing parameters are injected short per the repo
discipline (时序参数必须可配置, 测试注短值防抖).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel

from opencode.app import App
from opencode.contracts import ConfigSnapshot
from opencode.contracts.errors import CoreError
from opencode.contracts.events import PY_INBOX_ENQUEUED
from opencode.contracts.llm import (
    ErrorChunk,
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    ReasoningDeltaChunk,
    TextDeltaChunk,
    Usage,
    UsageChunk,
)
from opencode.tools.builtin.subagent import (
    SUBAGENT_INPUT_SCHEMA,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_VERSION,
    SubagentRunAdapter,
)
from opencode.tools.invoker import ToolContext

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="test-model")
_TERMINAL = {"succeeded", "failed", "interrupted"}
_PROVIDER_CHUNKS = (
    TextDeltaChunk,
    ReasoningDeltaChunk,
    UsageChunk,
    FinishChunk,
    ErrorChunk,
)

#: The always-background invocation: NO ``background`` key (the T-C3-005 face).
_INPUT: dict[str, Any] = {
    "agent": "task",
    "description": "summarize the note",
    "prompt": "Read nothing; just answer.",
}

#: How long M1 waits for the spawn call itself before reporting an honest
#: timeout (the foreground face blocks past this; the always-background face
#: returns in milliseconds).
_RETURN_DEADLINE_S = 1.5
#: The gated child parks at least this long: any return under the deadline
#: proves the call did NOT wait for the child.
_CHILD_PARK_S = 6.0
#: Poll deadlines for durable observation faces.
_SETTLE_DEADLINE_S = 15.0
_INBOX_DEADLINE_S = 15.0
_REPLAY_SLEEP_S = 0.05


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# migration + sqlite probe helpers (the test_tool_subagent.py forms)
# ---------------------------------------------------------------------------


def _migrated_db(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    migrated = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if migrated.returncode != 0:
        raise RuntimeError(f"alembic failed\nstdout:{migrated.stdout}\nstderr:{migrated.stderr}")
    return db_path


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _events_of(db_path: Path, session_id: str, event_type: str) -> list[dict[str, Any]]:
    rows = _fetch_all(db_path, "SELECT session_id, data FROM events WHERE type = ?", (event_type,))
    out: list[dict[str, Any]] = []
    for row_session, data in rows:
        if row_session != session_id:
            continue
        try:
            decoded = json.loads(str(data))
        except ValueError:
            continue
        if isinstance(decoded, dict):
            out.append(decoded)
    return out


async def _await_predicate(predicate: Any, *, timeout_s: float, what: str) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{what} did not arrive within {timeout_s}s")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# scripted provider + gates (the test_tool_subagent.py shape, copied)
# ---------------------------------------------------------------------------


class _Gate:
    """Script marker: the stream parks here until the test opens the gate."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.entered = asyncio.Event()

    def open(self) -> None:
        self.event.set()


class _Script:
    """One scripted provider stream: chunks in order, auto-ascending indexes."""

    def __init__(self) -> None:
        self.items: list[BaseModel | _Gate] = []
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            TextDeltaChunk(type="text_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta)
        )
        return self

    def reasoning(self, part_id: str, delta: str) -> _Script:
        self.items.append(
            ReasoningDeltaChunk(
                type="reasoning_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta
            )
        )
        return self

    def usage(self, input_tokens: int = 5, output_tokens: int = 7) -> _Script:
        self.items.append(
            UsageChunk(
                type="usage",
                request_id="req_script",
                index=self._next(),
                usage=Usage(input=input_tokens, output=output_tokens),
                cost_usd="0.0012",
            )
        )
        return self

    def finish(self, reason: FinishReason = FinishReason.STOP) -> _Script:
        self.items.append(FinishChunk(type="finish", request_id="req_script", index=self._next(), reason=reason))
        return self

    def error(self, error: CoreError) -> _Script:
        self.items.append(ErrorChunk(type="error", request_id="req_script", index=self._next(), error=error))
        return self

    def gate(self) -> _Gate:
        gate = _Gate()
        self.items.append(gate)
        return gate


def _is_chunk(item: object) -> TypeGuard[ProviderChunk]:
    return isinstance(item, _PROVIDER_CHUNKS)


class _ScriptedProvider:
    """Fake Provider: one scripted stream per request; every request recorded."""

    def __init__(self, scripts: Sequence[Sequence[BaseModel | _Gate | _Script]]) -> None:
        self.scripts = [
            [item for entry in script for item in (entry.items if isinstance(entry, _Script) else (entry,))]
            for script in scripts
        ]
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self.scripts.pop(0) if self.scripts else []
        for item in script:
            if _is_chunk(item):
                yield item
            elif isinstance(item, _Gate):
                item.entered.set()
                await item.event.wait()

    async def close(self) -> None:
        self.close_count += 1


def _settings() -> Any:
    from opencode.session.context import RuntimeSettings

    return RuntimeSettings(
        model=_MODEL,
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


def _subagent_app(
    db_path: Path,
    provider: _ScriptedProvider,
    tmp_path: Path,
    subagent: SubagentRunAdapter,
    *,
    close_timeout_s: float = 5.0,
) -> App:
    from opencode.tools.policy import PermissionRule

    return App(
        db_path,
        mode="create",
        working_dir=str(tmp_path),
        provider=provider,
        tools=(subagent,),
        settings=_settings(),
        rules=(PermissionRule(action="subagent__run", effect="allow"),),
        close_timeout_s=close_timeout_s,
    )


def _ctx(session_id: str, tmp_path: Path, *, tool_call_id: str = "call_eval_1") -> ToolContext:
    return ToolContext(
        session_id=session_id,
        run_id="run_eval",
        message_id="msg_eval",
        tool_call_id=tool_call_id,
        working_dir=str(tmp_path),
    )


# ---------------------------------------------------------------------------
# shared evaluation drives
# ---------------------------------------------------------------------------


def _inbox_type() -> str:
    return str(PY_INBOX_ENQUEUED)


async def _child_run_id(db_path: Path, child_id: str) -> str:
    def read() -> list[tuple[Any, ...]]:
        try:
            return _fetch_all(db_path, "SELECT run_id FROM runs WHERE session_id = ?", (child_id,))
        except sqlite3.OperationalError:
            return []

    rows = await _await_predicate(lambda: read(), timeout_s=_SETTLE_DEADLINE_S, what=f"runs row for {child_id}")
    return str(rows[0][0])


async def _spawn_gated_child(
    parent_id: str,
    subagent: SubagentRunAdapter,
    tmp_path: Path,
    gate: Any,
) -> dict[str, Any]:
    """M1/M3 shared drive: spawn (no background key) against a gated child.

    The spawn call itself is bounded by ``_RETURN_DEADLINE_S``: a face that
    waits for the child answers ``returned_s=None`` (honest timeout), an
    always-background face returns immediately with the running output.
    """
    started = time.perf_counter()
    returned: dict[str, Any] | None = None
    try:
        result = await asyncio.wait_for(
            subagent.execute(_ctx(parent_id, tmp_path), dict(_INPUT)), timeout=_RETURN_DEADLINE_S
        )
    except TimeoutError:
        returned = {"returned_s": None, "status": None, "sessionID": None, "output": None}
    else:
        elapsed = time.perf_counter() - started
        payload = result.structured_output if isinstance(result.structured_output, dict) else {}
        error = None if result.error is None else str(result.error.message)
        returned = {
            "returned_s": round(elapsed, 3),
            "status": payload.get("status"),
            "sessionID": payload.get("sessionID"),
            "output": payload.get("output"),
            "error": error,
        }
    # Let the parked child settle so the app teardown and the receipt face
    # can be observed deterministically (the gate opens regardless).
    await _await_predicate(gate.entered.is_set, timeout_s=_SETTLE_DEADLINE_S, what="the gated child to park")
    gate.open()
    return returned


async def _child_settled(db_path: Path, child_id: str) -> bool:
    def settled() -> bool:
        try:
            rows = _fetch_all(db_path, "SELECT status FROM runs WHERE session_id = ?", (child_id,))
        except sqlite3.OperationalError:
            return False
        return bool(rows) and all(str(row[0]) in _TERMINAL for row in rows)

    return await _await_predicate(settled, timeout_s=_SETTLE_DEADLINE_S, what=f"the child {child_id} to settle")


async def _first_receipt(db_path: Path, parent_id: str, inbox_id: str) -> dict[str, Any] | None:
    """Poll the synthetic inbox entry under ``subagent:{run_id}``; None = absent."""

    def read() -> dict[str, Any] | None:
        found = _events_of(db_path, parent_id, _inbox_type())
        for data in found:
            if data.get("inbox_id") == inbox_id:
                return data
        return None

    deadline = asyncio.get_running_loop().time() + _INBOX_DEADLINE_S
    while asyncio.get_running_loop().time() < deadline:
        entry = read()
        if entry is not None:
            return entry
        await asyncio.sleep(_REPLAY_SLEEP_S)
    return None


def _receipt_metrics(entry: dict[str, Any] | None) -> dict[str, Any]:
    if entry is None:
        return {"delivered": False}
    raw_message = entry.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    text = str(message.get("text", ""))
    raw_metadata = message.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    return {
        "delivered": True,
        "kind": entry.get("kind"),
        "wrapped": text.startswith("<subagent "),
        "state_attr": _attr(text, "state"),
        "metadata_state": metadata.get("state"),
        "still_running_tail": "Background work still running:" in text,
        "body_has_final_text": "all done" in text,
        "body_has_no_text_sentence": "Subagent completed without a text response." in text,
    }


def _attr(text: str, name: str) -> str | None:
    marker = f'{name}="'
    at = text.find(marker)
    if at < 0:
        return None
    start = at + len(marker)
    end = text.find('"', start)
    return None if end < 0 else text[start:end]


# ---------------------------------------------------------------------------
# P0 capability
# ---------------------------------------------------------------------------


async def test_p0_capability_probe() -> None:
    """The always-background spec face: v2, no background key, running-only."""
    from opencode.tools.builtin import subagent as module

    raw_properties = SUBAGENT_INPUT_SCHEMA.get("properties")
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    has_background = "background" in properties
    description = str(getattr(module, "SUBAGENT_DESCRIPTION", ""))
    always_bg_sentence = "always run in the background" in description
    capability = (
        "probed" if (SUBAGENT_VERSION >= 2 and not has_background and always_bg_sentence) else "capability-absent"
    )
    _report(
        {
            "probe": "P0",
            "version": SUBAGENT_VERSION,
            "background_key": has_background,
            "always_bg_sentence": always_bg_sentence,
            "max_depth": SUBAGENT_MAX_DEPTH,
            "capability": capability,
        }
    )


# ---------------------------------------------------------------------------
# M1 return-immediately
# ---------------------------------------------------------------------------


async def test_m1_return_immediately(tmp_path: Path) -> None:
    """A spawn against a gated child returns in well under the child runtime."""
    script = _Script()
    gate = script.gate()
    child_script = script.text("p2", "all done").usage().finish()
    provider = _ScriptedProvider([[child_script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "m1_return.db")
    metrics: dict[str, Any]
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        metrics = await _spawn_gated_child(parent.session_id, subagent, tmp_path, gate)
        child_id = metrics.get("sessionID")
        if isinstance(child_id, str) and child_id.startswith("ses_"):
            await _child_settled(db_path, child_id)
    metrics["probe"] = "M1"
    metrics["parked_child_s"] = _CHILD_PARK_S
    metrics["verdict"] = (
        "immediate"
        if metrics.get("returned_s") is not None and metrics.get("status") == "running"
        else "blocking-or-absent"
    )
    _report(metrics)


# ---------------------------------------------------------------------------
# M2 idle receipt
# ---------------------------------------------------------------------------


async def test_m2_idle_receipt(tmp_path: Path) -> None:
    """The child settling delivers ONE synthetic receipt under subagent:{run}."""
    script = _Script()
    gate = script.gate()
    child_script = script.text("p2", "all done").usage().finish()
    provider = _ScriptedProvider([[child_script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "m2_receipt.db")
    metrics: dict[str, Any] = {"delivered": False}
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        spawn = await _spawn_gated_child(parent.session_id, subagent, tmp_path, gate)
        child_id = spawn.get("sessionID")
        if isinstance(child_id, str) and child_id.startswith("ses_"):
            await _child_settled(db_path, child_id)
            run_id = await _child_run_id(db_path, child_id)
            entry = await _first_receipt(db_path, parent.session_id, f"subagent:{run_id}")
            metrics = _receipt_metrics(entry)
            metrics["inbox_id"] = f"subagent:{run_id}"
            metrics["child_id"] = child_id
        metrics["spawn_status"] = spawn.get("status")
    metrics["probe"] = "M2"
    metrics["verdict"] = "receipted" if metrics.get("delivered") and metrics.get("wrapped") else "no-receipt"
    _report(metrics)


# ---------------------------------------------------------------------------
# M3 outstanding tail + keep-alive state
# ---------------------------------------------------------------------------


async def test_m3_outstanding_tail(tmp_path: Path) -> None:
    """A running shell row under the child keeps the receipt alive (state=running)."""
    from opencode.storage import open_database
    from opencode.tools.background import BackgroundJobRow, upsert_row

    script = _Script()
    gate = script.gate()
    child_script = script.text("p2", "all done").usage().finish()
    provider = _ScriptedProvider([[child_script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "m3_tail.db")
    metrics: dict[str, Any] = {"delivered": False}
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        spawn = await _spawn_gated_child(parent.session_id, subagent, tmp_path, gate)
        child_id = spawn.get("sessionID")
        if isinstance(child_id, str) and child_id.startswith("ses_"):
            await _await_predicate(gate.entered.is_set, timeout_s=_SETTLE_DEADLINE_S, what="the gated child to park")
            # The outstanding row must exist BEFORE the child settles: a shell
            # command still running under the child at receipt time.
            async with open_database(db_path) as database:
                await upsert_row(
                    database,
                    BackgroundJobRow(
                        notification_id="ntf_m3_shell",
                        session_id=child_id,
                        tool_call_id="call_m3_shell",
                        kind="shell",
                        status="running",
                        title="long shell",
                        payload={"command": "ping -t localhost"},
                    ),
                )
            gate.open()
            await _child_settled(db_path, child_id)
            run_id = await _child_run_id(db_path, child_id)
            entry = await _first_receipt(db_path, parent.session_id, f"subagent:{run_id}")
            metrics = _receipt_metrics(entry)
            metrics["child_id"] = child_id
        metrics["spawn_status"] = spawn.get("status")
    metrics["probe"] = "M3"
    metrics["verdict"] = (
        "keep-alive"
        if metrics.get("still_running_tail") and metrics.get("state_attr") == "running"
        else "no-tail-or-flat"
    )
    _report(metrics)


# ---------------------------------------------------------------------------
# M4 follow-up steering
# ---------------------------------------------------------------------------


async def test_m4_followup_steering(tmp_path: Path) -> None:
    """A follow-up on a RUNNING child joins the current run (steer, not busy)."""
    script = _Script()
    gate = script.gate()
    child_first = script.text("p2", "partial").usage().finish()
    child_second = _Script().text("p2", " resumed answer").usage().finish()
    provider = _ScriptedProvider([[child_first], [child_second]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "m4_steer.db")
    metrics: dict[str, Any] = {}
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        parent = await app.service.create(_SNAPSHOT)
        spawn = await _spawn_gated_child(parent.session_id, subagent, tmp_path, gate)
        child_id = spawn.get("sessionID")
        if not (isinstance(child_id, str) and child_id.startswith("ses_")):
            metrics = {"followup": "spawn-absent", "spawn_status": spawn.get("status")}
        else:
            await _await_predicate(gate.entered.is_set, timeout_s=_SETTLE_DEADLINE_S, what="the gated child to park")
            # The child is mid-run here: the follow-up must not reject busy.
            outcome = "delivered"
            detail: Any = None
            try:
                result = await asyncio.wait_for(
                    subagent.execute(
                        _ctx(parent.session_id, tmp_path, tool_call_id="call_eval_followup"),
                        {**_INPUT, "sessionID": child_id, "prompt": "Now also cover the appendix."},
                    ),
                    timeout=_RETURN_DEADLINE_S,
                )
                payload = result.structured_output if isinstance(result.structured_output, dict) else {}
                detail = {
                    "status": payload.get("status"),
                    "error": None if result.error is None else str(result.error.message),
                }
                if result.error is not None and "busy" in str(result.error.message).lower():
                    outcome = "busy"
                elif result.error is not None:
                    outcome = "error"
            except TimeoutError:
                outcome = "blocked"
            gate.open()
            await _child_settled(db_path, child_id)
            metrics = {"followup": outcome, "detail": detail, "child_id": child_id}
    metrics["probe"] = "M4"
    metrics["verdict"] = "steered" if metrics.get("followup") == "delivered" else str(metrics.get("followup"))
    _report(metrics)


# ---------------------------------------------------------------------------
# M5 depth rejection (the standing regression row)
# ---------------------------------------------------------------------------


async def test_m5_depth_rejection(tmp_path: Path) -> None:
    """A spawn from a child session is refused at the depth limit."""
    child_script = _Script().text("p2", "all done").usage().finish()
    provider = _ScriptedProvider([[child_script]])
    subagent = SubagentRunAdapter()
    db_path = _migrated_db(tmp_path, "m5_depth.db")
    async with _subagent_app(db_path, provider, tmp_path, subagent) as app:
        await app.recovery_gate.wait()
        subagent.bind(app.service)
        root = await app.service.create(_SNAPSHOT)
        mid = await app.service.create(_SNAPSHOT, parent_id=root.session_id)
        result = await subagent.execute(_ctx(mid.session_id, tmp_path), dict(_INPUT))
        rejected = result.error is not None and "depth" in str(result.error.message).lower()
    _report(
        {
            "probe": "M5",
            "rejected": rejected,
            "limit": SUBAGENT_MAX_DEPTH,
            "message": None if result.error is None else str(result.error.message),
            "verdict": "rejected" if rejected else "not-rejected",
        }
    )


# ---------------------------------------------------------------------------
# M6 restart replay classification
# ---------------------------------------------------------------------------


async def test_m6_restart_replay(tmp_path: Path) -> None:
    """A settled subagent row replays once as the wrapped <subagent> report.

    The pre-implementation replay is kind-agnostic: a settled row replays
    ``payload["text"]`` verbatim (empty for a recovery-shaped payload). The
    T-C3-005 face builds the ``<subagent sessionID state description>`` body
    from the row's recovery snapshot, the upstream deliverSubagentSafe form.
    """
    from opencode.storage import open_database
    from opencode.tools.background import BackgroundJobRow, replay_pending, upsert_row

    db_path = _migrated_db(tmp_path, "m6_replay.db")

    def _row_count(notification_id: str) -> int:
        rows = _fetch_all(db_path, "SELECT COUNT(*) FROM background_job WHERE notification_id = ?", (notification_id,))
        return int(rows[0][0] or 0)

    async with open_database(db_path) as database:
        metrics: dict[str, Any] = {}
        recovery: dict[str, Any] = {
            "kind": "subagent",
            "parentSessionID": "ses_m6_parent",
            "childSessionID": "ses_m6_child",
            "agent": "task",
            "description": "summarize the note",
        }
        await upsert_row(
            database,
            BackgroundJobRow(
                notification_id="subagent:run_m6",
                session_id="ses_m6_parent",
                tool_call_id="call_m6",
                kind="subagent",
                status="completed",
                title="summarize the note",
                payload={**recovery, "output": "all done"},
            ),
        )
        replayed = await replay_pending(database)
        subagent_replays = [item for item in replayed if item.kind == "subagent"]
        metrics["replayed_subagent_rows"] = len(subagent_replays)
        replay_text = subagent_replays[0].text if subagent_replays else None
        metrics["replay_text"] = replay_text
        metrics["wrapped"] = bool(
            replay_text
            and replay_text.startswith('<subagent sessionID="ses_m6_child"')
            and 'state="completed"' in replay_text
            and "all done" in replay_text
        )
        second = await replay_pending(database)
        metrics["second_pass_subagent_rows"] = len([item for item in second if item.kind == "subagent"])
        metrics["row_cleared_after_replay"] = _row_count("subagent:run_m6") == 0
    metrics["probe"] = "M6"
    metrics["verdict"] = (
        "wrapped-replay"
        if metrics["replayed_subagent_rows"] == 1 and metrics["wrapped"] and metrics["second_pass_subagent_rows"] == 0
        else "plain-or-absent"
    )
    _report(metrics)
