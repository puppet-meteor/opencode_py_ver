"""Integration tests for the S5a human-gate chain (T-C1-080): pause, card, reply, resume.

Honest-label note: the injected ``llm_call``/``tool`` bodies below are fakes
(the S1 fake-executor discipline) -- they record calls and return fixed JSON
values. Everything around them is real: a real migrated main jobs database,
the real ``JobService`` commit spine and fold, the real compiled LangGraph
state machine under the official sqlite checkpointer, the real
``human_gate`` node body (the gate mint, the ``EnginePause`` suspend), the
real reply chain (an approve/revise resumes through ``py.job.started``, a
reject resolves ``finished(failed)`` with ``PermissionDenied`` naming the
gate) and the real HTTP route face.

Coverage (the S5a acceptance judges):

* the gate cycle: a job with a ``human_gate`` node parks ``gated`` with the
  cursor holding the node position; the gate card rides the existing
  ``py.job.gated`` event over the job's control channel with the
  conversation ref on the envelope metadata -- never the brain's stream;
* approve resumes the graph from the cursor and the job settles ``done``
  with the reply as the gate node's result; a revise carries the revised
  params into the node result while the submit-time ``params_hash`` binding
  is never re-bound; a reject resolves the job terminally;
* the reply-face validation order: every rejection is a structured conflict
  (chain not wired, job not gated, gate mismatch, gate expired);
* the HTTP route: body enum first (422, params only on a revise), then the
  service chain (409 Conflict carrying the job id and gate id), success
  answers the four-field receipt;
* a second gate cycle on the same job mints a fresh gate id (unique per
  job), the resume drive re-entering through the gated cursor;
* the advisory self-check (the S3 leftover ②) records its dispatch-liveness
  report for a spawned resume.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from opencode.app import App
from opencode.contracts.messages import JsonObject
from opencode.httpapi import create_app
from opencode.orchestrator.engine import EngineCheckpointer, PipelineEngine
from opencode.orchestrator.gates import GateBroker
from opencode.orchestrator.jobs import JobService, JobsProjector, JobTransitionError, job_stream_key
from opencode.session.service import AscendingIds
from opencode.storage import open_database
from opencode.storage.jobs_store import JobsReadRepository, JobsStore

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "test-token-1234"


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


# ---------------------------------------------------------------------------
# The honest fakes (recording, deterministic) + the controllable clock.
# ---------------------------------------------------------------------------


class _Clock:
    """The fixed-time test clock, mutable so an expiry can be simulated."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class _FakeLlm:
    """Recording fake (honestly labeled); the gate specs never reach it."""

    async def __call__(self, node: Any, state: Any) -> Any:
        return {"summary": "ok", "model": node.model}


class _FakeTool:
    """Recording fake (honestly labeled): returns a fixed JSON value."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, node: Any, state: Any) -> Any:
        self.calls.append(node.node_id)
        return {"collected": True}


def _gate_spec(*, pipeline_id: str = "gated_flow", two_gates: bool = False) -> JsonObject:
    """One tool -> human_gate chain (the gate node terminal; two gates chains a second)."""
    nodes: list[dict[str, Any]] = [
        {"node_id": "collect", "node_type": "tool", "tool_ref": "fs.read_text"},
        {"node_id": "approval", "node_type": "human_gate"},
    ]
    edges: list[dict[str, Any]] = [{"from_node": "collect", "to_node": "approval"}]
    if two_gates:
        nodes.append({"node_id": "final_approval", "node_type": "human_gate"})
        edges.append({"from_node": "approval", "to_node": "final_approval"})
    spec: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_id": pipeline_id,
        "entry": "collect",
        "nodes": nodes,
        "edges": edges,
    }
    return cast(JsonObject, spec)


class _Stack:
    """One migrated main DB + one gate-broker-wired engine over its own cursor file."""

    def __init__(self, tmp_path: Path, *, selfcheck_delay_s: float = -1.0) -> None:
        self.db_path = tmp_path / "main.db"
        result = _upgrade_head(self.db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        self.clock = _Clock()
        self.broker = GateBroker()
        self.tool = _FakeTool()
        self.engine = PipelineEngine(
            checkpointer=EngineCheckpointer(tmp_path / "cursor.sqlite3"),
            llm_call=_FakeLlm(),
            tool=self.tool,
            gate_broker=self.broker,
        )
        self.database = open_database(self.db_path, mode="create")
        self.service = JobService(
            store=JobsStore(self.database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(self.database.engine),
            executor=self.engine,
            ids=AscendingIds(self.clock),
            clock=self.clock,
            gate_broker=self.broker,
            selfcheck_delay_s=selfcheck_delay_s,
        )

    async def register(self, raw: JsonObject) -> None:
        await self.engine.register(raw)

    async def close(self) -> None:
        await self.service.close()
        await self.database.close()
        await self.engine.close()


async def _await_status(stack: _Stack, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> Any:
    """Poll the read model until a spawned run settles the job."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await stack.service.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


async def _await_selfcheck(stack: _Stack, job_id: str) -> JsonObject:
    """Poll until the advisory self-check probe recorded its report."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while True:
        report = stack.service.last_selfcheck(job_id)
        if report is not None:
            return report
        assert asyncio.get_running_loop().time() < deadline, "the advisory self-check never ran"
        await asyncio.sleep(0.01)


def _stream_types(db_path: Path, stream: str) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT type FROM events WHERE session_id = ? ORDER BY seq", (stream,)).fetchall()
    return [row[0] for row in rows]


def _gated_metadata(db_path: Path, stream: str) -> JsonObject:
    """The first ``py.job.gated`` event's envelope metadata (the card routing face)."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT metadata FROM events WHERE session_id = ? AND type = 'py.job.gated' ORDER BY seq",
            (stream,),
        ).fetchall()
    assert rows, "no py.job.gated event on the job's control channel"
    return json.loads(rows[0][0])


async def _asgi_request(
    api: Any,
    method: str,
    path: str,
    *,
    token: str | None = _TOKEN,
    body: bytes | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_root": "",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51236),
        "server": ("127.0.0.11", 4096),
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}] if body is not None else []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await api(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    status = int(start["status"])
    response_headers = {bytes(name).lower(): bytes(value) for name, value in start["headers"]}
    response_body = b"".join(
        bytes(message.get("body", b"")) for message in sent if message["type"] == "http.response.body"
    )
    return status, response_headers, response_body


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


async def _gate_app(tmp_path: Path) -> tuple[App, Any]:
    """One App over a gate-broker-wired engine (the route face)."""
    db_path = tmp_path / "api.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\n{result.stderr}"
    engine = PipelineEngine(
        checkpointer=EngineCheckpointer(tmp_path / "api-cursor.sqlite3"),
        llm_call=_FakeLlm(),
        tool=_FakeTool(),
        gate_broker=GateBroker(),
    )
    await engine.register(_gate_spec())
    app = App(db_path, working_dir="C:/work", pipeline_engine=engine)
    return app, create_app(app, token=_TOKEN)


async def _await_app_status(app: App, job_id: str, statuses: set[str], *, timeout_s: float = 5.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        record = await app.jobs.get_job(job_id)
        if record.status in statuses:
            return record
        assert asyncio.get_running_loop().time() < deadline, f"job {job_id} never settled: {record.status}"
        await asyncio.sleep(0.01)


class TestGateCycle:
    async def test_gate_parks_the_job_and_approve_resumes_to_done(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path, selfcheck_delay_s=0.0)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {"run": "approve"}, "ses_0001")
            job_id = handle.job_id
            await stack.service.run(job_id)  # parks at the gate node
            record = await stack.service.get_job(job_id)
            assert record.status == "gated"
            gate_id = stack.service.active_gate(job_id)
            assert gate_id is not None
            assert gate_id.startswith("gat_")  # minted by the service, unique within the job
            assert stack.tool.calls == ["collect"]  # the tool node ran before the gate

            # The gate card: the existing py.job.gated event over the job's control
            # channel, the conversation ref on the envelope metadata (never the brain).
            stream = job_stream_key(job_id)
            assert _stream_types(stack.db_path, stream) == [
                "py.job.created",
                "py.job.spec_bound",
                "py.job.started",
                "py.job.gated",
            ]
            assert _gated_metadata(stack.db_path, stream) == {"conversation_ref": "ses_0001"}

            # Approve: gated -> running through the existing started event, the
            # resolution handed to the broker, the resume drive spawned.
            record = await stack.service.reply_gate(job_id, gate_id, "approve")
            assert record.status in {"running", "done"}
            record = await _await_status(stack, job_id, {"done"})
            assert record.result == {
                "pipeline_id": "gated_flow",
                "nodes": {"collect": {"collected": True}, "approval": {"reply": "approve"}},
            }
            assert stack.service.active_gate(job_id) is None  # the gate died with the cycle
            assert _stream_types(stack.db_path, stream) == [
                "py.job.created",
                "py.job.spec_bound",
                "py.job.started",
                "py.job.gated",
                "py.job.started",  # gated -> running (the approve resume)
                "py.job.finished",
            ]
            # The advisory self-check (the S3 leftover ②): the spawned resume's
            # dispatch-liveness report landed and every check passed.
            assert (await _await_selfcheck(stack, job_id))["ok"] is True
        finally:
            await stack.close()

    async def test_revise_carries_params_into_the_node_result_without_rebinding(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {"run": "revise"}, "api")
            job_id = handle.job_id
            await stack.service.run(job_id)
            gated = await stack.service.get_job(job_id)
            assert gated.status == "gated"
            bound_hash = gated.params_hash  # the submit-time spec-chain binding
            gate_id = stack.service.active_gate(job_id)
            assert gate_id is not None
            record = await stack.service.reply_gate(job_id, gate_id, "revise", params={"tone": "formal"})
            assert record.status in {"running", "done"}
            record = await _await_status(stack, job_id, {"done"})
            result = record.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            assert nodes["approval"] == {"reply": "revise", "params": {"tone": "formal"}}
            assert record.params_hash == bound_hash  # a revise continues the run, never re-binds
        finally:
            await stack.close()

    async def test_reject_resolves_terminally_with_permission_denied(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {"run": "reject"}, "api")
            job_id = handle.job_id
            await stack.service.run(job_id)
            gate_id = stack.service.active_gate(job_id)
            assert gate_id is not None
            record = await stack.service.reply_gate(job_id, gate_id, "reject")
            assert record.status == "failed"
            assert record.error is not None
            assert record.error["code"] == "PermissionDenied"
            assert gate_id in str(record.error["message"])  # the error names the gate
            assert record.result is None
            assert stack.service.active_gate(job_id) is None  # the card died with the job
            assert stack.broker.pending(job_id) is None  # a reject never re-enters the graph
            assert _stream_types(stack.db_path, job_stream_key(job_id)) == [
                "py.job.created",
                "py.job.spec_bound",
                "py.job.started",
                "py.job.gated",
                "py.job.finished",
            ]
        finally:
            await stack.close()

    async def test_two_gate_cycles_mint_unique_gate_ids_per_job(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec(two_gates=True))
            handle = await stack.service.submit("gated_flow", {"run": "double"}, "api")
            job_id = handle.job_id
            await stack.service.run(job_id)
            first_gate = stack.service.active_gate(job_id)
            assert first_gate is not None
            await stack.service.reply_gate(job_id, first_gate, "approve")
            # The resume drive runs the second human_gate node and parks again.
            record = await _await_status(stack, job_id, {"gated", "done"})
            assert record.status == "gated"
            second_gate = stack.service.active_gate(job_id)
            assert second_gate is not None
            assert second_gate != first_gate  # gate ids are unique within the job
            assert second_gate.startswith("gat_")
            await stack.service.reply_gate(job_id, second_gate, "approve")
            record = await _await_status(stack, job_id, {"done"})
            result = record.result
            assert isinstance(result, dict)
            nodes = result["nodes"]
            assert isinstance(nodes, dict)
            assert nodes["approval"] == {"reply": "approve"}
            assert nodes["final_approval"] == {"reply": "approve"}
            assert _stream_types(stack.db_path, job_stream_key(job_id)).count("py.job.gated") == 2
        finally:
            await stack.close()


class TestReplyFaceValidation:
    async def test_reply_on_a_non_gated_job_is_a_structured_conflict(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {}, "api")
            with pytest.raises(JobTransitionError, match="is not gated"):
                await stack.service.reply_gate(handle.job_id, "gat_missing", "approve")
        finally:
            await stack.close()

    async def test_gate_mismatch_names_the_active_gate_and_applies_nothing(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {}, "api")
            job_id = handle.job_id
            await stack.service.run(job_id)
            gate_id = stack.service.active_gate(job_id)
            assert gate_id is not None
            with pytest.raises(JobTransitionError, match="is not the active gate"):
                await stack.service.reply_gate(job_id, "gat_wrong", "approve")
            assert stack.service.active_gate(job_id) == gate_id  # the mismatch applied nothing
            record = await stack.service.get_job(job_id)
            assert record.status == "gated"  # ...and the job never left the gate
        finally:
            await stack.close()

    async def test_expired_gate_rejects_and_invalidates_the_gate(self, tmp_path: Path) -> None:
        stack = _Stack(tmp_path)
        try:
            await stack.register(_gate_spec())
            handle = await stack.service.submit("gated_flow", {}, "api")
            job_id = handle.job_id
            await stack.service.run(job_id)
            gate_id = stack.service.active_gate(job_id)
            assert gate_id is not None
            stack.clock.now = stack.clock.now + timedelta(seconds=301)  # past the 300 s m-3 expiry
            with pytest.raises(JobTransitionError, match="has expired"):
                await stack.service.reply_gate(job_id, gate_id, "approve")
            assert stack.service.active_gate(job_id) is None  # the expired gate is invalidated
            record = await stack.service.get_job(job_id)
            assert record.status == "gated"  # the job itself stays parked (no auto-fail in v1)
        finally:
            await stack.close()

    async def test_unwired_chain_rejects_before_any_status_check(self, tmp_path: Path) -> None:
        db_path = tmp_path / "plain.db"
        result = _upgrade_head(db_path)
        assert result.returncode == 0, f"alembic failed\n{result.stderr}"
        engine = PipelineEngine(
            checkpointer=EngineCheckpointer(tmp_path / "plain-cursor.sqlite3"),
            llm_call=_FakeLlm(),
            tool=_FakeTool(),
        )
        database = open_database(db_path, mode="create")
        service = JobService(
            store=JobsStore(database.engine, projector=JobsProjector()),
            repos=JobsReadRepository(database.engine),
            executor=engine,
            ids=AscendingIds(lambda: datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC)),
            clock=lambda: datetime(2026, 11, 13, 9, 0, 0, tzinfo=UTC),
        )
        try:
            handle = await service.submit("gated_flow", {}, "api")
            with pytest.raises(JobTransitionError, match="not wired"):
                await service.reply_gate(handle.job_id, "gat_1", "approve")
        finally:
            await service.close()
            await database.close()
            await engine.close()


class TestGateReplyRoute:
    async def test_approve_over_the_route_answers_the_receipt_and_settles(self, tmp_path: Path) -> None:
        app, api = await _gate_app(tmp_path)
        try:
            handle = await app.jobs.submit("gated_flow", {"run": "route"}, "ses_0002")
            await app.jobs.run(handle.job_id)  # parks gated
            gate_id = app.jobs.active_gate(handle.job_id)
            assert gate_id is not None
            status, _headers, raw = await _asgi_request(
                api, "POST", f"/jobs/{handle.job_id}/gate/{gate_id}/reply", body=_json_body({"reply": "approve"})
            )
            assert status == 200
            payload = json.loads(raw)
            assert payload["job_id"] == handle.job_id
            assert payload["gate_id"] == gate_id
            assert payload["reply"] == "approve"
            assert payload["status"] in {"running", "done"}  # the resume drive runs in the background
            record = await _await_app_status(app, handle.job_id, {"done"})
            assert record.result is not None
        finally:
            await app.close()

    async def test_non_gated_and_mismatch_answer_409_conflict(self, tmp_path: Path) -> None:
        app, api = await _gate_app(tmp_path)
        try:
            handle = await app.jobs.submit("gated_flow", {}, "api")
            # A created (never gated) job: the service chain answers 409.
            status, _headers, raw = await _asgi_request(
                api, "POST", f"/jobs/{handle.job_id}/gate/gat_x/reply", body=_json_body({"reply": "approve"})
            )
            assert status == 409
            payload = json.loads(raw)
            assert payload["code"] == "Conflict"
            assert payload["details"]["job_id"] == handle.job_id
            assert payload["details"]["gate_id"] == "gat_x"
            # The same job now gates for real; a wrong gate id still answers 409.
            await app.jobs.run(handle.job_id)
            gate_id = app.jobs.active_gate(handle.job_id)
            assert gate_id is not None
            status, _headers, raw = await _asgi_request(
                api, "POST", f"/jobs/{handle.job_id}/gate/gat_wrong/reply", body=_json_body({"reply": "approve"})
            )
            assert status == 409
            payload = json.loads(raw)
            assert payload["code"] == "Conflict"
            assert payload["details"]["gate_id"] == "gat_wrong"
            assert gate_id in payload["message"]  # the conflict names the active gate
            record = await app.jobs.get_job(handle.job_id)
            assert record.status == "gated"  # nothing was applied
        finally:
            await app.close()

    async def test_params_are_legal_only_on_a_revise_reply(self, tmp_path: Path) -> None:
        app, api = await _gate_app(tmp_path)
        try:
            handle = await app.jobs.submit("gated_flow", {}, "api")
            # params on an approve -> 422 (the wire contract is real).
            status, _headers, raw = await _asgi_request(
                api,
                "POST",
                f"/jobs/{handle.job_id}/gate/gat_1/reply",
                body=_json_body({"reply": "approve", "params": {"x": 1}}),
            )
            assert status == 422
            assert json.loads(raw)["code"] == "ValidationError"
            # params not an object on a revise -> 422.
            status, _headers, raw = await _asgi_request(
                api,
                "POST",
                f"/jobs/{handle.job_id}/gate/gat_1/reply",
                body=_json_body({"reply": "revise", "params": [1]}),
            )
            assert status == 422
            assert json.loads(raw)["code"] == "ValidationError"
        finally:
            await app.close()
