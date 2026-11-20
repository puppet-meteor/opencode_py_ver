"""T-C3-008 contract set -- RED first: the experimental automation/job routes.

Upstream: ``server/src/handlers/automation.ts`` (106 lines, six routes) +
``handlers/job.ts`` (26 lines, one route) over ``protocol/src/groups/
automation.ts`` + ``groups/job.ts``; test families ``server/test/
automation.test.ts`` (3 it.live) + ``job.test.ts`` (1). The five banked
decisions live in doc/plans/port-232-increments.md T-C3-009.

Banked wire deviations (py house style, snake_case keys; upstream enum
vocabularies kept verbatim):

- automation info passes the service ``_info`` dict through (snake_case);
- job items map row status ``backgrounded``/``succeeded`` to the upstream
  ``running``/``completed`` literals (the report word maps at the wire, the
  row keeps the shell-word vocabulary);
- shell items expose ``shell_id`` = the tool call id (py bash runs have no
  persistent shell handle) and ``command`` = the row title (the closest the
  row carries);
- ``descendants`` walks the ``py.session.created`` ``parent_id`` seam with a
  seen-set cycle guard.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opencode.app import App
from opencode.automation.service import AutomationService
from opencode.tools.background import BackgroundJobRow, upsert_row
from tests.integration.test_automation_eval import _SESSION_ID, _RecordingSessions
from tests.integration.test_automation_http_eval import (
    _CREATE_APP_ANY,
    _close,
    _create_payload,
    _request,
)
from tests.integration.test_httpapi_server import _TOKEN
from tests.integration.test_store_commit import _require_success, _upgrade_head

_MISSING = object()


@contextmanager
def _flag(value: bool) -> Iterator[None]:
    """Flip the process-level automations flag, restoring whatever was there."""
    import opencode.httpapi.app as app_module

    original = getattr(app_module, "EVENT_AUTOMATIONS_ENABLED", _MISSING)
    if original is not _MISSING:
        app_module.EVENT_AUTOMATIONS_ENABLED = value
    try:
        yield
    finally:
        if original is not _MISSING:
            app_module.EVENT_AUTOMATIONS_ENABLED = original


class _SteppedClock:
    """Deterministic ``now_ms``: every read advances one second."""

    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        self.now += 1_000
        return self.now


class _CrossProjectSessions(_RecordingSessions):
    """The recording port plus one session that lives in another project."""

    def resolve_session(self, session_id: str) -> Any:
        if session_id == "ses_other":
            return SimpleNamespace(id="ses_other", project_id="proj_elsewhere")
        return super().resolve_session(session_id)


async def _make_api(tmp_path: Path, *, sessions: Any = None, clock: Any = None) -> tuple[Any, Any]:
    db_path = tmp_path / "red.db"
    _require_success(_upgrade_head(db_path))
    app = App(db_path, working_dir="C:/work")
    port = sessions if sessions is not None else _RecordingSessions()
    kwargs: dict[str, Any] = {"database": app._database, "sessions": port}
    if clock is not None:
        kwargs["now_ms"] = clock
    service = AutomationService(**kwargs)
    api = _CREATE_APP_ANY(app, token=_TOKEN, automation=service)
    return api, app


async def _create(api: Any, directory: str | Path, **overrides: Any) -> str:
    status, body = await _request(
        api, "POST", "/api/experimental/automation", body=_create_payload(str(directory), **overrides)
    )
    assert status == 200, f"create failed: {status} {body}"
    return body["data"]["id"]


def _rewrite_row_directory(db_path: Path, automation_id: str, directory: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("update automation set directory = ? where id = ?", (directory, automation_id))
        conn.commit()
    finally:
        conn.close()


def _seed_session(db_path: Path, session_id: str, *, parent_id: str | None = None) -> None:
    """One sessions row + its ``py.session.created`` event (the parent seam)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "insert into sessions (session_id, status, applied_seq) values (?, 'active', 0)",
            (session_id,),
        )
        conn.execute(
            "insert into events (id, session_id, seq, type, version, created, data, metadata,"
            " batch_id, batch_ordinal) values (?, ?, 1, 'py.session.created', 1, 1, ?, '{}', 'seed', 0)",
            (f"evt_{session_id}", session_id, json.dumps({"working_dir": "C:/work", "parent_id": parent_id})),
        )
        conn.commit()
    finally:
        conn.close()


async def _seed_row(app: Any, notification_id: str, session_id: str, **overrides: Any) -> None:
    fields: dict[str, Any] = {
        "notification_id": notification_id,
        "session_id": session_id,
        "tool_call_id": f"call_{notification_id}",
        "kind": "shell",
        "status": "backgrounded",
        "title": "sleep 30",
        "payload": None,
    }
    fields.update(overrides)
    await upsert_row(app._database, BackgroundJobRow(**fields))


# ---------------------------------------------------------------------------
# structural: the five paths exist
# ---------------------------------------------------------------------------


async def test_routes_present(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        paths = {getattr(route, "path", "") for route in api.routes}
        assert {
            "/api/experimental/automation",
            "/api/experimental/automation/{automation_id}",
            "/api/experimental/automation/{automation_id}/pause",
            "/api/experimental/automation/{automation_id}/resume",
            "/api/experimental/job",
        } <= paths
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# family 1: CRUD round trip + the flag gate + the gone folder
# ---------------------------------------------------------------------------


async def test_crud_round_trip(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path, clock=_SteppedClock())
    try:
        automation_id = await _create(api, tmp_path)
        status, body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
        assert status == 200 and body["data"]["id"] == automation_id
        assert body["data"]["status"] == "active"
        status, body = await _request(api, "POST", f"/api/experimental/automation/{automation_id}/pause")
        assert status == 200 and body["data"]["status"] == "paused"
        status, body = await _request(api, "POST", f"/api/experimental/automation/{automation_id}/resume")
        assert status == 200 and body["data"]["status"] == "active"
        for method, path in (
            ("GET", "/api/experimental/automation/no-such-id"),
            ("DELETE", "/api/experimental/automation/no-such-id"),
            ("POST", "/api/experimental/automation/no-such-id/pause"),
            ("POST", "/api/experimental/automation/no-such-id/resume"),
        ):
            status, body = await _request(api, method, path)
            assert status == 404, f"{method} {path}: {status}"
            assert body["code"] == "NotFound"
        status, _body = await _request(api, "DELETE", f"/api/experimental/automation/{automation_id}")
        assert status == 204
        status, _body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
        assert status == 404
    finally:
        await _close(app)


async def test_flag_off_gates_and_hides(tmp_path: Path) -> None:
    """Off: every mutating/managing route 403s after its 404 check, the bare
    list stays 200 with an empty visible set (upstream: visibility filter)."""
    api, app = await _make_api(tmp_path)
    try:
        with _flag(False):
            status, body = await _request(
                api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path))
            )
            assert status == 403 and body["code"] == "PermissionDenied"
            status, _body = await _request(api, "GET", "/api/experimental/automation")
            assert status == 200  # no query params: no gate, nothing visible
            status, _body = await _request(
                api,
                "GET",
                "/api/experimental/automation",
                query=f"session_id={_SESSION_ID}".encode(),
            )
            assert status == 403  # the session's directory gate
            status, _body = await _request(api, "GET", "/api/experimental/automation/no-such-id")
            assert status == 404  # find resolves the row before the gate
    finally:
        await _close(app)
    api, app = await _make_api(tmp_path)  # flag on: the managing routes answer
    try:
        automation_id = await _create(api, tmp_path)
        with _flag(False):
            for method, suffix in (("GET", ""), ("POST", "/pause"), ("POST", "/resume"), ("DELETE", "")):
                status, _body = await _request(api, method, f"/api/experimental/automation/{automation_id}{suffix}")
                assert status == 403, f"{method}{suffix}: {status}"
    finally:
        await _close(app)


async def test_gone_directory_stays_manageable(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        automation_id = await _create(api, tmp_path)
        _rewrite_row_directory(tmp_path / "red.db", automation_id, "C:/gone/nowhere")
        status, body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
        assert status == 200 and body["data"]["directory"] == "C:/gone/nowhere"
        status, _body = await _request(api, "DELETE", f"/api/experimental/automation/{automation_id}")
        assert status == 204
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# family 2: the create error mapping (upstream catchTags)
# ---------------------------------------------------------------------------


async def test_create_invalid_payloads_map_422(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        for overrides in (
            {"name": ""},
            {"trigger": {"type": "wat"}},
            {"trigger": {"type": "every", "every": 30.0}},
            {"trigger": {"type": "at", "at": "2020-01-01T00:00:00Z"}},
            {"trigger": {"type": "every", "every": 3600.0, "tz": "Mars/Olympus"}},
        ):
            status, body = await _request(
                api,
                "POST",
                "/api/experimental/automation",
                body=_create_payload(str(tmp_path), **overrides),
            )
            assert status == 422, f"{overrides}: {status}"
            assert body["code"] == "ValidationError"
            assert body["message"]
    finally:
        await _close(app)


async def test_create_unknown_target_or_owner_session_maps_404(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        status, body = await _request(
            api,
            "POST",
            "/api/experimental/automation",
            body=_create_payload(
                str(tmp_path),
                action={"type": "prompt", "prompt": "x", "to": {"session": "missing_session"}},
            ),
        )
        assert status == 404 and body["code"] == "NotFound"
        status, _body = await _request(
            api,
            "POST",
            "/api/experimental/automation",
            body=_create_payload(str(tmp_path), session_id="missing_session"),
        )
        assert status == 404
    finally:
        await _close(app)


async def test_create_cross_project_target_maps_422(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path, sessions=_CrossProjectSessions())
    try:
        status, body = await _request(
            api,
            "POST",
            "/api/experimental/automation",
            body=_create_payload(
                str(tmp_path),
                action={"type": "prompt", "prompt": "x", "to": {"session": "ses_other"}},
            ),
        )
        assert status == 422 and body["code"] == "ValidationError"
        assert "another project" in body["message"]
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# family 3: the list filter matrix
# ---------------------------------------------------------------------------


async def test_list_owner_session_filter_and_unknown_404(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path, clock=_SteppedClock())
    try:
        owned = await _create(api, tmp_path, name="owned", session_id=_SESSION_ID)
        unowned = await _create(api, tmp_path, name="unowned")
        status, body = await _request(api, "GET", "/api/experimental/automation")
        assert status == 200 and [row["id"] for row in body["data"]] == [owned, unowned]
        status, body = await _request(
            api,
            "GET",
            "/api/experimental/automation",
            query=f"session_id={_SESSION_ID}".encode(),
        )
        assert status == 200 and [row["id"] for row in body["data"]] == [owned]
        status, _body = await _request(api, "GET", "/api/experimental/automation", query=b"session_id=missing_session")
        assert status == 404
    finally:
        await _close(app)


async def test_list_directory_filter_absolute_check_and_match(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path, clock=_SteppedClock())
    try:
        here = await _create(api, tmp_path, name="here")
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        there = await _create(api, str(other_dir), name="there")
        status, _body = await _request(api, "GET", "/api/experimental/automation", query=b"directory=relative/path")
        assert status == 422
        # Verbatim string match (upstream row.directory === directory): the
        # query must carry the same form the row stored (str(), not as_posix()).
        status, body = await _request(
            api,
            "GET",
            "/api/experimental/automation",
            query=f"directory={other_dir!s}".encode(),
        )
        assert status == 200 and [row["id"] for row in body["data"]] == [there]
        status, body = await _request(
            api,
            "GET",
            "/api/experimental/automation",
            query=f"directory={tmp_path!s}".encode(),
        )
        assert status == 200 and [row["id"] for row in body["data"]] == [here]
    finally:
        await _close(app)


async def test_list_flag_off_empty_visible_set(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        await _create(api, tmp_path)
        with _flag(False):
            status, body = await _request(api, "GET", "/api/experimental/automation")
            assert status == 200 and body["data"] == []
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# family 4: job.list wire + filters + descendants
# ---------------------------------------------------------------------------


async def test_job_list_wire_shape_and_ordering(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        await _seed_row(app, "ntf_a", "ses_w", title="sleep 30")
        await _seed_row(
            app,
            "ntf_b",
            "ses_w",
            kind="subagent",
            status="succeeded",
            payload={
                "kind": "subagent",
                "parentSessionID": "ses_w",
                "childSessionID": "ses_w2",
                "agent": "general",
                "description": "the check",
                "output": "ok",
            },
        )
        status, body = await _request(api, "GET", "/api/experimental/job")
        assert status == 200 and len(body["data"]) == 2
        shell, subagent = body["data"]
        assert shell["kind"] == "shell" and shell["id"] == "ntf_a"
        assert shell["session_id"] == "ses_w" and shell["status"] == "running"
        assert shell["title"] == "sleep 30"
        assert shell["time"]["started_ms"] > 0 and shell["time"]["completed_ms"] is None
        assert shell["shell_id"] == "call_ntf_a" and shell["command"] == "sleep 30"
        assert subagent["kind"] == "subagent" and subagent["id"] == "ntf_b"
        assert subagent["status"] == "completed" and subagent["time"]["completed_ms"] is not None
        assert subagent["child_session_id"] == "ses_w2"
        assert subagent["agent"] == "general" and subagent["description"] == "the check"
        assert subagent["session_id"] == "ses_w"  # the subagent row is parent-owned
    finally:
        await _close(app)


async def test_job_list_kind_status_filters_and_validation(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        await _seed_row(app, "ntf_a", "ses_f", title="one")
        await _seed_row(app, "ntf_b", "ses_f", status="succeeded", title="two")
        await _seed_row(
            app,
            "ntf_c",
            "ses_f",
            kind="subagent",
            status="succeeded",
            payload={
                "kind": "subagent",
                "parentSessionID": "ses_f",
                "childSessionID": "c",
                "agent": "a",
                "description": "d",
            },
        )

        async def ids(query: bytes) -> list[str]:
            status, body = await _request(api, "GET", "/api/experimental/job", query=query)
            assert status == 200, f"{query.decode()}: {status} {body}"
            return [item["id"] for item in body["data"]]

        # The ids are alphabetical in seeding order: a same-millisecond tie
        # breaks by notification_id, so the expected orders stay deterministic.
        assert await ids(b"kind=shell") == ["ntf_a", "ntf_b"]
        assert await ids(b"kind=subagent") == ["ntf_c"]
        assert await ids(b"status=running") == ["ntf_a"]
        assert await ids(b"status=completed") == ["ntf_b", "ntf_c"]
        assert await ids(b"kind=shell&status=completed") == ["ntf_b"]
        assert await ids(b"kind=subagent&status=running") == []
        assert await ids(b"kind=shell&descendants=true") == ["ntf_a", "ntf_b"]
        status, _body = await _request(api, "GET", "/api/experimental/job", query=b"kind=bogus")
        assert status == 422
        status, _body = await _request(api, "GET", "/api/experimental/job", query=b"status=bogus")
        assert status == 422
        status, _body = await _request(api, "GET", "/api/experimental/job", query=b"descendants=maybe")
        assert status == 422
    finally:
        await _close(app)


async def test_job_list_session_filter_and_unknown_404(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        db_path = tmp_path / "red.db"
        _seed_session(db_path, "ses_a")
        _seed_session(db_path, "ses_b")
        await _seed_row(app, "ntf_a1", "ses_a")
        await _seed_row(app, "ntf_a2", "ses_a", kind="subagent", status="succeeded", payload={})
        await _seed_row(app, "ntf_b1", "ses_b")
        status, body = await _request(api, "GET", "/api/experimental/job", query=b"session_id=ses_a")
        assert status == 200 and [item["id"] for item in body["data"]] == ["ntf_a1", "ntf_a2"]
        status, _body = await _request(api, "GET", "/api/experimental/job", query=b"session_id=missing_session")
        assert status == 404
    finally:
        await _close(app)


async def test_job_list_descendants_walks_parent_chain_with_cycle_guard(tmp_path: Path) -> None:
    api, app = await _make_api(tmp_path)
    try:
        db_path = tmp_path / "red.db"
        _seed_session(db_path, "ses_parent")
        _seed_session(db_path, "ses_child", parent_id="ses_parent")
        _seed_session(db_path, "ses_cyc", parent_id="ses_cyc")  # a self-cycle row
        await _seed_row(app, "ntf_p", "ses_parent")
        await _seed_row(app, "ntf_c", "ses_child")
        await _seed_row(app, "ntf_y", "ses_cyc")
        status, body = await _request(api, "GET", "/api/experimental/job", query=b"session_id=ses_parent")
        assert status == 200 and [item["id"] for item in body["data"]] == ["ntf_p"]
        status, body = await _request(
            api, "GET", "/api/experimental/job", query=b"session_id=ses_parent&descendants=true"
        )
        assert status == 200
        assert sorted(item["id"] for item in body["data"]) == ["ntf_c", "ntf_p"]
        status, body = await _request(api, "GET", "/api/experimental/job", query=b"session_id=ses_cyc&descendants=true")
        assert status == 200 and [item["id"] for item in body["data"]] == ["ntf_y"]
    finally:
        await _close(app)
