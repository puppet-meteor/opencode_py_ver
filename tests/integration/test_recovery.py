"""R4-S4 integration tests: the startup recovery coordinator (T-C1-037).

Real SQLite + ``alembic upgrade head`` + the real store/projector/repos, with
the crash windows built by direct draft commits -- the honest cross-process
crash simulation: a killed process leaves exactly a log that stops mid-run,
nothing else. Contract anchors: CORE-CONTRACTS §3.3 L210-221 (the recovery
recipe, the three windows, closed-first order), §2.1 L53 (row#27① orphan
pending cleanup), §2.1 L59-61 (run outcome legality), §6.1 L302 (the three
stop faces), §7 L333 (never auto-resume), §7 L339 (cleanup_failed kept); the
``_drive_run`` row clause is double-sided: in-process crash -> cancel reports
LifecycleCleanupFailed (zero regression), cross-process crash (no
``_schedule_errors`` account) -> the scan closes the run from the log alone.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opencode.app import App
from opencode.bus import VolatileBus
from opencode.config.loader import mcp_startup_fingerprint
from opencode.contracts import ConfigSnapshot
from opencode.contracts.events import (
    EVENT_VERSION,
    ConfigSnapshotV3,
    EventDraft,
    McpServerRef,
    PermissionRequestedData,
    PromptAcceptedData,
    RunCancelRequestedData,
    RunStartedData,
    SessionCreatedData,
    SessionCreatedDataV3,
    SnapshotLimits,
    StepGeneratedData,
    StepStartedData,
    TextFinalizedData,
    ToolFinishedData,
    ToolRequestedData,
    ToolStartedData,
    compute_snapshot_hash,
)
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import (
    AssistantMessage,
    AssistantTextPart,
    AssistantToolPart,
    JsonObject,
    ToolTextContent,
    UserMessage,
)
from opencode.session.recovery import RecoveryCoordinator, sweep_crash_temp_files
from opencode.session.runner import EventCommitter
from opencode.session.service import IdFactory
from opencode.session.state import compute_input_hash
from opencode.storage import LifecycleCleanupFailedError, ReadRepositories, SessionStore, open_database

REPO_ROOT = Path(__file__).resolve().parents[2]

_SNAPSHOT = ConfigSnapshot.create()
_FIXED_AT = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
_MODEL = ModelRef(provider="maas", model="test-model")


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


def _clock() -> datetime:
    return _FIXED_AT


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real alembic CLI against a tmp database (sync migration engine)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_path={db_path}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, f"alembic failed\nstdout:{result.stdout}\nstderr:{result.stderr}"


def _fetch_scalar(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchone()


def _event_types(db_path: Path, session_id: str) -> list[str]:
    rows = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
    return [str(row[0]) for row in rows]


def _fetch_all(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement, params).fetchall()


def _run_status_value(db_path: Path, run_id: str) -> str | None:
    row = _fetch_scalar(db_path, "SELECT status FROM runs WHERE run_id = ?", (run_id,))
    return None if row is None else str(row[0])


def _draft[DataT: Any](
    ids: IdFactory,
    session_id: str,
    run_id: str | None,
    event_type: str,
    data: DataT,
    *,
    version: int = EVENT_VERSION,
) -> EventDraft[DataT]:
    """One run-scoped durable draft (runner/invoker conventions, fixed clock)."""
    return EventDraft[DataT](
        id=ids.event_id(),
        type=event_type,
        version=version,
        session_id=session_id,
        created_at=_clock(),
        data=data,
        metadata={"run_id": run_id} if run_id is not None else {},
    )


async def _commit(committer: EventCommitter, session_id: str, drafts: list[Any]) -> None:
    """Commit one fixture batch: exactly what a crashed process leaves behind."""
    await committer.commit(session_id, drafts)


class _Fixture:
    """Direct-draft crash-window builder over one migrated database."""

    def __init__(self, db_path: Path, *, working_dir: str | None = None) -> None:
        self.db_path = db_path
        self.working_dir = working_dir if working_dir is not None else str(db_path.parent)
        self.ids = _FixedIds()
        self.session_id = self.ids.session_id()
        self.run_id = self.ids.run_id()
        self.message_id = self.ids.message_id()
        self.step_id = "step_000001"
        self.step_message_id = self.ids.message_id()

    async def build(self, database: Any, window: str) -> None:
        """Build one crash window's durable log: accepted | running | cancelling."""
        engine = database.engine
        store = SessionStore(engine, projector=_projector())
        repos = ReadRepositories(engine)
        committer = EventCommitter(repos, store, VolatileBus(), ids=self.ids, clock=_clock)
        sid, rid = self.session_id, self.run_id
        drafts = [
            _draft(
                self.ids,
                sid,
                None,
                "py.session.created",
                SessionCreatedData(
                    working_dir=self.working_dir,
                    config_snapshot=_SNAPSHOT,
                    snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
                ),
            ),
            _draft(
                self.ids,
                sid,
                rid,
                "py.prompt.accepted",
                PromptAcceptedData(
                    request_id="req_000001",
                    run_id=rid,
                    message=UserMessage(
                        id=self.message_id, type="user", created_at=_clock(), metadata={}, text="hello"
                    ),
                ),
            ),
        ]
        if window == "accepted":
            await _commit(committer, sid, drafts)
            return
        drafts.append(_draft(self.ids, sid, rid, "py.run.started", RunStartedData(run_id=rid)))
        drafts.append(
            _draft(
                self.ids,
                sid,
                rid,
                "py.step.started",
                StepStartedData(
                    run_id=rid, step_id=self.step_id, message_id=self.step_message_id, model=_MODEL, attempt=1
                ),
            )
        )
        drafts.append(
            _draft(
                self.ids,
                sid,
                rid,
                "py.text.finalized",
                TextFinalizedData(message_id=self.step_message_id, part_id="p1", ordinal=0, text="hello world"),
            )
        )
        if window == "running":
            await _commit(committer, sid, drafts)
            return
        if window == "cancelling":
            tool_a = "exec_000001"
            tool_b = "exec_000002"
            input_b: JsonObject = {"path": "b.txt"}
            generated = AssistantMessage(
                id=self.step_message_id,
                type="assistant",
                created_at=_clock(),
                metadata={},
                model=_MODEL,
                parts=[
                    AssistantTextPart(type="text", part_id="p1", text="hello world"),
                    AssistantToolPart(
                        type="tool",
                        tool_call_id="call_a",
                        execution_id=tool_a,
                        name="fs__read",
                        input={"path": "a.txt"},
                        state="pending",
                        effect_status="not_started",
                        content=[],
                    ),
                    AssistantToolPart(
                        type="tool",
                        tool_call_id="call_b",
                        execution_id=tool_b,
                        name="fs__read",
                        input=input_b,
                        state="pending",
                        effect_status="not_started",
                        content=[],
                    ),
                ],
                status="running",
            )
            drafts.append(
                _draft(
                    self.ids, sid, rid, "py.step.generated", StepGeneratedData(step_id=self.step_id, message=generated)
                )
            )
            drafts.append(
                _draft(
                    self.ids,
                    sid,
                    rid,
                    "py.tool.requested",
                    ToolRequestedData(
                        message_id=self.step_message_id,
                        tool_call_id="call_b",
                        execution_id=tool_b,
                        requested_name="fs__read",
                        input=input_b,
                        input_hash=compute_input_hash(input_b),
                    ),
                )
            )
            drafts.append(
                _draft(
                    self.ids,
                    sid,
                    rid,
                    "py.tool.started",
                    ToolStartedData(
                        execution_id=tool_b, permission_decision="allow", input_hash=compute_input_hash(input_b)
                    ),
                )
            )
            drafts.append(
                _draft(
                    self.ids,
                    sid,
                    rid,
                    "py.tool.finished",
                    ToolFinishedData(
                        execution_id=tool_b,
                        state="succeeded",
                        effect_status="completed",
                        content=[ToolTextContent(type="text", text="b done")],
                    ),
                )
            )
            drafts.append(
                _draft(
                    self.ids,
                    sid,
                    rid,
                    "py.tool.requested",
                    ToolRequestedData(
                        message_id=self.step_message_id,
                        tool_call_id="call_a",
                        execution_id=tool_a,
                        requested_name="fs__read",
                        input={"path": "a.txt"},
                        input_hash=compute_input_hash({"path": "a.txt"}),
                    ),
                )
            )
            drafts.append(
                _draft(
                    self.ids,
                    sid,
                    rid,
                    "py.permission.requested",
                    PermissionRequestedData(
                        permission_id="perm_000001",
                        execution_id=tool_a,
                        action="fs.read",
                        resources=["a.txt"],
                        input_hash=compute_input_hash({"path": "a.txt"}),
                        expires_at=_FIXED_AT,
                    ),
                )
            )
            drafts.append(
                _draft(self.ids, sid, rid, "py.run.cancel_requested", RunCancelRequestedData(run_id=rid, reason="user"))
            )
            await _commit(committer, sid, drafts)
            return
        raise ValueError(f"unknown window {window!r}")


def _projector() -> Any:
    from opencode.session import SessionProjector

    return SessionProjector()


async def _scan(db_path: Path, fixture: _Fixture, *, current_mcp_servers: Any = None) -> Any:
    """Open the migrated db and run one coordinator scan (fresh 'process')."""
    async with open_database(db_path, mode="create") as database:
        engine = database.engine
        store = SessionStore(engine, projector=_projector())
        repos = ReadRepositories(engine)
        committer = EventCommitter(repos, store, VolatileBus(), ids=fixture.ids, clock=_clock)
        coordinator = RecoveryCoordinator(
            engine=engine,
            committer=committer,
            current_mcp_servers=current_mcp_servers,
            repos=repos,
            ids=fixture.ids,
            clock=_clock,
        )
        return await coordinator.scan()


# ---------------------------------------------------------------------------
# The three §3.3 L216-221 crash windows
# ---------------------------------------------------------------------------


async def test_accepted_window_closes_interrupted_and_allows_a_new_prompt(tmp_path: Path) -> None:
    """Window 1 (§3.3 L216-221): a run accepted but never started is closed
    interrupted; the half-open session then accepts a new prompt (the active
    run slot is released by the recovery commit)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "accepted")
    assert _run_status_value(db_path, fixture.run_id) == "accepted"

    report = await _scan(db_path, fixture)
    assert report.sessions[0].outcome == "recovered"
    assert report.sessions[0].closed_runs == ((fixture.run_id, "accepted"),)
    assert _run_status_value(db_path, fixture.run_id) == "interrupted"
    types = _event_types(db_path, fixture.session_id)
    assert types[-1] == "py.run.finished"

    # A fresh App over the recovered store (its own startup scan is idempotent)
    # accepts a new prompt: the one-active-run gate is open again.
    async with App(db_path, working_dir=str(tmp_path), sweep_roots=[tmp_path / "sweep"]) as app:
        report2 = await app.recovery_gate.wait()
        assert report2.sessions == ()
        ref = await app.service.prompt(fixture.session_id, "again", "req_000002")
        assert _run_status_value(db_path, ref.run_id) in {"accepted", "running", "interrupted"}


async def test_running_window_rebuilds_the_interrupted_step_from_finalized_text(tmp_path: Path) -> None:
    """Window 2: a run crashed mid-stream (step started, text finalized, no
    generated plan) is closed with the §3.3 L212 non-generated terminal: the
    interrupted step message carries exactly the committed finalized text."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "running")

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "recovered"
    assert entry.closed_runs == ((fixture.run_id, "running"),)
    assert entry.step_terminals == (fixture.step_id,)
    types = _event_types(db_path, fixture.session_id)
    assert types[-2:] == ["py.step.finished", "py.run.finished"]
    messages = _fetch_all(
        db_path,
        "SELECT data FROM messages WHERE session_id = ? AND message_id = ?",
        (fixture.session_id, fixture.step_message_id),
    )
    assert messages, "the interrupted terminal message is projected"
    data = json.loads(str(messages[0][0]))
    assert data["status"] == "interrupted"
    assert data["parts"] == [{"type": "text", "part_id": "p1", "text": "hello world"}]


async def test_cancelling_window_full_terminal_batch_closed_first(tmp_path: Path) -> None:
    """Window 3: a run crashed while cancelling (row#27①: a still-pending
    approval orphaned) is closed with the full §3.3 L212 sequence in one
    atomic batch: permission.closed(cancelled) FIRST, then the not-started
    tool terminal, then the interrupted step (the already-succeeded tool
    keeps its folded facts), then run.finished(interrupted). Re-scanning is
    idempotent (the terminal scan gate finds nothing)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "cancelling")
    assert _run_status_value(db_path, fixture.run_id) == "cancelling"

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "recovered"
    assert entry.closed_runs == ((fixture.run_id, "cancelling"),)
    assert entry.closed_permissions == ("perm_000001",)
    assert entry.tool_terminals == ("exec_000001",)
    types = _event_types(db_path, fixture.session_id)
    assert types[-4:] == [
        "py.permission.closed",
        "py.tool.finished",
        "py.step.finished",
        "py.run.finished",
    ]
    closed = _fetch_all(
        db_path,
        "SELECT data FROM events WHERE session_id = ? AND type = 'py.permission.closed'",
        (fixture.session_id,),
    )
    assert json.loads(str(closed[0][0]))["outcome"] == "cancelled"
    assert _run_status_value(db_path, fixture.run_id) == "interrupted"

    before = _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (fixture.session_id,))
    report2 = await _scan(db_path, fixture)
    assert report2.sessions == ()  # fully terminal sessions leave the scan shortlist
    after = _fetch_scalar(db_path, "SELECT COUNT(*) FROM events WHERE session_id = ?", (fixture.session_id,))
    assert after == before


async def test_window2_started_tool_without_finished_synthesizes_unknown(tmp_path: Path) -> None:
    """Window 2 (§3.3 L219, T-C1-039): a process exiting between py.tool.started
    and py.tool.finished leaves a RUNNING tool with no result; the recovery scan
    synthesizes the honest unknown terminal -- ToolFinishedData(state="unknown",
    effect_status="unknown") -- even though the command may already have
    succeeded, and lands it on the report, the event batch and the projected
    interrupted message's tool part."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    tool_id = "exec_000001"
    tool_input: JsonObject = {"path": "c.txt"}
    sid, rid = fixture.session_id, fixture.run_id
    async with open_database(db_path, mode="create") as database:
        engine = database.engine
        store = SessionStore(engine, projector=_projector())
        repos = ReadRepositories(engine)
        committer = EventCommitter(repos, store, VolatileBus(), ids=fixture.ids, clock=_clock)
        generated = AssistantMessage(
            id=fixture.step_message_id,
            type="assistant",
            created_at=_clock(),
            metadata={},
            model=_MODEL,
            parts=[
                AssistantTextPart(type="text", part_id="p1", text="hello world"),
                AssistantToolPart(
                    type="tool",
                    tool_call_id="call_c",
                    execution_id=tool_id,
                    name="fs__read",
                    input=tool_input,
                    state="pending",
                    effect_status="not_started",
                    content=[],
                ),
            ],
            status="running",
        )
        await committer.commit(
            sid,
            [
                _draft(
                    fixture.ids,
                    sid,
                    None,
                    "py.session.created",
                    SessionCreatedData(
                        working_dir=fixture.working_dir,
                        config_snapshot=_SNAPSHOT,
                        snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
                    ),
                ),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.prompt.accepted",
                    PromptAcceptedData(
                        request_id="req_000001",
                        run_id=rid,
                        message=UserMessage(
                            id=fixture.message_id, type="user", created_at=_clock(), metadata={}, text="hello"
                        ),
                    ),
                ),
                _draft(fixture.ids, sid, rid, "py.run.started", RunStartedData(run_id=rid)),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.step.started",
                    StepStartedData(
                        run_id=rid, step_id=fixture.step_id, message_id=fixture.step_message_id, model=_MODEL, attempt=1
                    ),
                ),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.text.finalized",
                    TextFinalizedData(message_id=fixture.step_message_id, part_id="p1", ordinal=0, text="hello world"),
                ),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.step.generated",
                    StepGeneratedData(step_id=fixture.step_id, message=generated),
                ),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.tool.requested",
                    ToolRequestedData(
                        message_id=fixture.step_message_id,
                        tool_call_id="call_c",
                        execution_id=tool_id,
                        requested_name="fs__read",
                        input=tool_input,
                        input_hash=compute_input_hash(tool_input),
                    ),
                ),
                _draft(
                    fixture.ids,
                    sid,
                    rid,
                    "py.tool.started",
                    ToolStartedData(
                        execution_id=tool_id, permission_decision="allow", input_hash=compute_input_hash(tool_input)
                    ),
                ),
                # the crash point: no py.tool.finished ever commits (§3.3 L219)
            ],
        )
    assert _run_status_value(db_path, rid) == "running"

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "recovered"
    assert entry.closed_runs == ((rid, "running"),)
    assert entry.tool_terminals == (tool_id,)
    assert entry.step_terminals == (fixture.step_id,)

    # The synthesized unknown terminal, verbatim (§3.3 L219): unknown/unknown plus
    # the model-readable "could not be confirmed" text.
    finished = _fetch_all(
        db_path,
        "SELECT data FROM events WHERE session_id = ? AND type = 'py.tool.finished'",
        (sid,),
    )
    assert len(finished) == 1
    data = json.loads(str(finished[0][0]))
    assert data["execution_id"] == tool_id
    assert data["state"] == "unknown"
    assert data["effect_status"] == "unknown"
    assert data["content"] == [
        {
            "type": "text",
            "text": f"Tool execution {tool_id} was cancelled while running; its result could not be confirmed.",
        }
    ]

    # Batch position: the tool terminal precedes the interrupted step/run
    # terminals in the same single recovery batch (§3.3 L212 order).
    types = _event_types(db_path, sid)
    assert types[-3:] == ["py.tool.finished", "py.step.finished", "py.run.finished"]
    assert _run_status_value(db_path, rid) == "interrupted"

    # The landing: the projected interrupted message keeps the committed text
    # part verbatim and carries the synthesized unknown terminal on its tool part.
    messages = _fetch_all(
        db_path,
        "SELECT data FROM messages WHERE session_id = ? AND message_id = ?",
        (sid, fixture.step_message_id),
    )
    assert messages, "the interrupted terminal message is projected"
    message = json.loads(str(messages[0][0]))
    assert message["status"] == "interrupted"
    assert message["parts"][0] == {"type": "text", "part_id": "p1", "text": "hello world"}
    assert message["parts"][1]["state"] == "unknown"
    assert message["parts"][1]["effect_status"] == "unknown"
    assert message["parts"][1]["content"] == data["content"]


# ---------------------------------------------------------------------------
# The §6.1 L302 stop faces
# ---------------------------------------------------------------------------


async def test_halt_on_working_dir_gone(tmp_path: Path) -> None:
    """L302: a session whose recorded working directory vanished halts -- no
    recovery writes at all; the run keeps its honest stored state."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path, working_dir=str(tmp_path / "gone" / "nowhere"))
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "running")

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "halted"
    assert "no longer exists" in (entry.reason or "")
    assert entry.closed_runs == ()
    assert _run_status_value(db_path, fixture.run_id) == "running"


async def test_halt_on_v3_fingerprint_drift_but_v1_ignores(tmp_path: Path) -> None:
    """L302 (T-B-039 ②): a v3 snapshot whose recorded MCP fingerprints drifted
    from the current host refs halts; matching refs recover; a v1 snapshot has
    no MCP face, so even drifted refs cannot halt its terminal hygiene."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    recorded = McpServerRef(
        server="srv",
        command=["bin"],
        env_refs=[],
        fingerprint=mcp_startup_fingerprint("srv", ["bin"], []),
    )
    drifted = McpServerRef(
        server="srv",
        command=["bin", "--changed"],
        env_refs=[],
        fingerprint=mcp_startup_fingerprint("srv", ["bin", "--changed"], []),
    )
    snapshot_v3 = ConfigSnapshotV3(
        schema_version=3,
        model=_MODEL,
        endpoint_ref="https://example.test/api",
        roots=[str(tmp_path)],
        permission_rules=[],
        instructions=[],
        limits=SnapshotLimits(context_limit=1000, output_reserve=100, timeout_s=30.0, safety_margin=0, max_steps=20),
        mcp_servers=[recorded],
    )

    async with open_database(db_path, mode="create") as database:
        engine = database.engine
        store = SessionStore(engine, projector=_projector())
        repos = ReadRepositories(engine)
        ids = _FixedIds()
        committer = EventCommitter(repos, store, VolatileBus(), ids=ids, clock=_clock)
        session_id = ids.session_id()
        run_id = ids.run_id()
        created = SessionCreatedDataV3(
            working_dir=str(tmp_path),
            config_snapshot=snapshot_v3,
            snapshot_hash=compute_snapshot_hash(snapshot_v3),
        )
        prompt = PromptAcceptedData(
            request_id="req_000001",
            run_id=run_id,
            message=UserMessage(id=ids.message_id(), type="user", created_at=_clock(), metadata={}, text="hi"),
        )
        await committer.commit(
            session_id,
            [
                _draft(ids, session_id, None, "py.session.created", created, version=3),
                _draft(ids, session_id, run_id, "py.prompt.accepted", prompt),
            ],
        )

    scan_fixture = _Fixture(db_path)
    scan_fixture.ids = ids  # continue the build's sequence; fresh counters would collide with committed ids
    halted = await _scan(db_path, scan_fixture, current_mcp_servers=[drifted])
    assert halted.sessions[0].outcome == "halted"
    assert "fingerprint" in (halted.sessions[0].reason or "")
    assert _run_status_value(db_path, run_id) == "accepted"

    recovered = await _scan(db_path, scan_fixture, current_mcp_servers=[recorded])
    assert recovered.sessions[0].outcome == "recovered"
    assert _run_status_value(db_path, run_id) == "interrupted"


async def test_v1_session_recovers_despite_drifted_refs(tmp_path: Path) -> None:
    """The v1-no-runtime-face stop belongs to ConfigLoader.recover alone: a v1
    session (R1 legacy, §7 L333 accepted wording) still gets terminal hygiene
    even when the host declares drifted current refs."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "accepted")

    drifted = McpServerRef(
        server="srv",
        command=["other"],
        env_refs=[],
        fingerprint=mcp_startup_fingerprint("srv", ["other"], []),
    )
    report = await _scan(db_path, fixture, current_mcp_servers=[drifted])
    assert report.sessions[0].outcome == "recovered"
    assert _run_status_value(db_path, fixture.run_id) == "interrupted"


# ---------------------------------------------------------------------------
# Write-gate boundaries and the integrity gate
# ---------------------------------------------------------------------------


async def test_non_active_sessions_are_scanned_but_never_written(tmp_path: Path) -> None:
    """§7 L312-313/L339: closed and cleanup_failed sessions keep their runs;
    the scan counts them, writes nothing (the txn write gate owns them)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "accepted")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE sessions SET status = 'closed' WHERE session_id = ?", (fixture.session_id,))

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "skipped"
    assert "write gate" in (entry.reason or "")
    assert entry.closed_runs == ()
    assert _run_status_value(db_path, fixture.run_id) == "accepted"
    assert report.closed_sessions == 1


async def test_integrity_failure_skips_the_session(tmp_path: Path) -> None:
    """§3.3 integrity verification: applied_seq != log high watermark skips
    the session fail-closed (no repair, no writes), reported honestly."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "accepted")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE sessions SET applied_seq = applied_seq + 1 WHERE session_id = ?", (fixture.session_id,)
        )

    report = await _scan(db_path, fixture)
    entry = report.sessions[0]
    assert entry.outcome == "integrity_failure"
    assert "A != H" in (entry.reason or "")
    assert _run_status_value(db_path, fixture.run_id) == "accepted"


# ---------------------------------------------------------------------------
# The _drive_run row clause, double-sided
# ---------------------------------------------------------------------------


class _CrashingProvider:
    """A provider whose stream raises a non-CoreError mid-stream (the
    test_service_run.py m-1① idiom): the scheduled task crashes, _drive_run
    records it, the durable run stays non-terminal."""

    def __init__(self) -> None:
        self.crashed = asyncio.Event()

    async def stream(self, request: Any) -> Any:
        from opencode.contracts.llm import TextDeltaChunk

        yield TextDeltaChunk(type="text_delta", request_id="req_script", index=1, part_id="p1", delta="partial")
        self.crashed.set()
        raise RuntimeError("provider exploded")

    async def close(self) -> None:
        return None


def _runtime_settings() -> Any:
    from opencode.session.context import RuntimeSettings

    return RuntimeSettings(
        model=_MODEL,
        context_limit=1000,
        output_reserve=100,
        timeout_s=30.0,
        safety_margin=0,
        temperature=None,
        system_prompt=None,
    )


async def test_drive_run_in_process_crash_cancel_reports_lifecycle_cleanup_failed(tmp_path: Path) -> None:
    """Side ① (zero regression, the test_service_run.py m-1① face): an
    in-process crash is reported by cancel() as LifecycleCleanupFailedError;
    the session is marked cleanup_failed, which the scan then keeps verbatim
    (§7 L339) -- the run honestly stays cancelling."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _CrashingProvider()

    async with App(
        db_path,
        working_dir=str(tmp_path),
        provider=provider,
        settings=_runtime_settings(),
        sweep_roots=[tmp_path / "sweep"],
    ) as app:
        created = await app.service.create(_SNAPSHOT)
        run_ref = await app.service.prompt(created.session_id, "hello", "req_000001")
        await provider.crashed.wait()
        await asyncio.sleep(0.1)  # let the crash reach _drive_run's recorder

        with pytest.raises(LifecycleCleanupFailedError) as failed:
            await app.service.cancel(run_ref.run_id)
        assert "RuntimeError" in str(failed.value)

    report = await _scan(db_path, _Fixture(db_path))
    entry = report.sessions[0]
    assert entry.outcome == "skipped"
    assert report.cleanup_failed_sessions == 1
    assert _run_status_value(db_path, run_ref.run_id) == "cancelling"


async def test_cross_process_crash_closed_by_the_app_startup_scan(tmp_path: Path) -> None:
    """Side ② (the simulated cross-process crash): the durable log simply
    stops mid-run -- no _schedule_errors account, no runner task, nothing but
    storage. A fresh App's startup scan (the gate) closes the run from the
    log alone, and the gated service then works normally."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    fixture = _Fixture(db_path)
    async with open_database(db_path, mode="create") as database:
        await fixture.build(database, "running")

    async with App(db_path, sweep_roots=[tmp_path / "sweep"]) as app:
        report = await app.recovery_gate.wait()
        assert report.sessions[0].outcome == "recovered"
        assert report.sessions[0].closed_runs == ((fixture.run_id, "running"),)
        status = await app.service.run_status(fixture.run_id)
        assert status.status.value == "interrupted"
        ref = await app.service.prompt(fixture.session_id, "after recovery", "req_000002")
        assert ref.run_id != fixture.run_id


# ---------------------------------------------------------------------------
# The composition-root gate (OP-R4-6) and the row#27③ sweep
# ---------------------------------------------------------------------------


async def test_gate_blocks_writes_until_the_scan_completes_and_fails_closed(tmp_path: Path) -> None:
    """OP-R4-6: the gated store faces await the startup scan (writes block
    until it completes) and a scan failure propagates to every gated caller
    (fail-closed) -- reads stay ungated throughout."""
    from opencode.session.recovery import RecoveryCoordinator as Coordinator

    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))

    release = asyncio.Event()
    original = Coordinator.scan

    async def slow_scan(self: Any) -> Any:
        await release.wait()
        return await original(self)

    async def broken_scan(self: Any) -> Any:
        raise RuntimeError("scan exploded")

    Coordinator.scan = slow_scan  # type: ignore[method-assign]
    try:
        app = App(db_path, working_dir=str(tmp_path), sweep_roots=[tmp_path / "sweep"])
        create_task = asyncio.create_task(app.service.create(_SNAPSHOT))
        await asyncio.sleep(0.05)
        assert not create_task.done(), "the gated write must wait for the scan"
        release.set()
        await create_task
        await app.close()
    finally:
        Coordinator.scan = original  # type: ignore[method-assign]

    Coordinator.scan = broken_scan  # type: ignore[method-assign]
    try:
        app = App(db_path, working_dir=str(tmp_path), sweep_roots=[tmp_path / "sweep"])
        # the eager scan already failed; the first gated write must surface it
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="scan exploded"):
            await app.service.create(_SNAPSHOT)
        await app.close()
    finally:
        Coordinator.scan = original  # type: ignore[method-assign]


def test_sweep_removes_only_crash_temp_files(tmp_path: Path) -> None:
    """Row #27③ (T-C1-028 ④-1): the sweep removes exactly the
    ``.opencode-fs-*.tmp`` crash leftovers; sibling files, the intact target
    and a directory with the pattern shape are untouched."""
    leftover_a = tmp_path / ".opencode-fs-abc.tmp"
    leftover_b = tmp_path / ".opencode-fs-xyz.tmp"
    target = tmp_path / "target.txt"
    sibling = tmp_path / "keep.txt"
    pattern_dir = tmp_path / ".opencode-fs-dir.tmp"
    for path in (leftover_a, leftover_b, target, sibling):
        path.write_text("x", encoding="utf-8")
    pattern_dir.mkdir()

    removed, errors = sweep_crash_temp_files([tmp_path])
    assert removed == 2
    assert errors == ()
    assert not leftover_a.exists()
    assert not leftover_b.exists()
    assert target.exists()
    assert sibling.exists()
    assert pattern_dir.exists()

    removed_again, _ = sweep_crash_temp_files([tmp_path])
    assert removed_again == 0

    removed_missing, _ = sweep_crash_temp_files([tmp_path / "missing-root"])
    assert removed_missing == 0


async def test_app_startup_sweep_reports_counts(tmp_path: Path) -> None:
    """The App startup pass rides the row#27③ sweep over the declared roots
    and reports the counters on the recovery report."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    sweep_root = tmp_path / "sweep"
    sweep_root.mkdir()
    (sweep_root / ".opencode-fs-1.tmp").write_text("x", encoding="utf-8")

    async with App(db_path, sweep_roots=[sweep_root]) as app:
        report = await app.recovery_gate.wait()
        assert report.tmp_files_removed == 1
        assert report.tmp_roots == (str(sweep_root),)
        assert not (sweep_root / ".opencode-fs-1.tmp").exists()
