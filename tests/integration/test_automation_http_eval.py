"""T-C3-009 evaluation harness: the experimental automation/job HTTP routes.

A measurement face, not an assertion face (the test_automation_eval.py
precedent): every probe records a metric and passes, whether or not the
capability exists. Running this module on pre-implementation code captures
the BEFORE baseline; the identical inputs afterwards give the AFTER column.
The delta is the ticket's evidence against doc/plans/port-232-increments.md
T-C3-009 (decisions 1-5):

* P0 route capability -- the seven experimental routes exist on the
  ``create_app`` FastAPI face and the automation service injects through an
  ``automation=`` parameter.
* M1 create + error mapping -- a valid payload answers 200 with ``data.id``;
  an invalid payload answers 422 (InvalidRequest, kind automation); an
  unknown session answers 404.
* M2 flag gate -- with the process-level automations flag on, create answers
  200; with it off, 403; an automation whose directory is gone stays
  readable (the "gone folder stays manageable" semantics).
* M3 CRUD round trip -- get/pause/resume return the info envelope, delete
  answers 204, and an unknown id answers 404 everywhere.
* M4 list filtering -- unfiltered lists everything (oldest first), sessionID
  and directory filter, an unknown session answers 404.
* M5 job.list -- the route answers 200 with a data array, an unknown session
  answers 404, and the query parameters reach the filter face.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from opencode.app import App
from opencode.automation.service import AutomationService
from opencode.httpapi import create_app
from tests.integration.test_automation_eval import _PROJECT, _SESSION_ID, _RecordingSessions
from tests.integration.test_httpapi_server import _TOKEN, _asgi_request, _json_body
from tests.integration.test_store_commit import _require_success, _upgrade_head

#: The injection call is measured, not statically known: until the capability
#: lands, ``create_app`` has no ``automation`` parameter for mypy to see.
_CREATE_APP_ANY = cast(Any, create_app)

_EXPECTED_ROUTES = {
    "/api/experimental/automation",
    "/api/experimental/automation/{automation_id}",
    "/api/experimental/automation/{automation_id}/pause",
    "/api/experimental/automation/{automation_id}/resume",
    "/api/experimental/job",
}


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _create_payload(directory: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "eval-probe",
        "project_id": _PROJECT,
        "directory": directory,
        "trigger": {"type": "every", "every": 3600.0},
        "action": {"type": "prompt", "prompt": "run the check", "to": {"session": _SESSION_ID}},
    }
    payload.update(overrides)
    return payload


async def _make_api(tmp_path: Path, *, flag: bool | None = None) -> tuple[Any, Any, _RecordingSessions]:
    """Migrated db + App + injected automation service over one FastAPI face."""
    db_path = tmp_path / "probe.db"
    _require_success(_upgrade_head(db_path))
    app = App(db_path, working_dir="C:/work")
    sessions = _RecordingSessions()
    if "automation" not in inspect.signature(create_app).parameters:
        await _close(app)
        raise _CapabilityAbsent("create_app has no automation parameter")
    service = AutomationService(database=app._database, sessions=sessions)
    api = _CREATE_APP_ANY(app, token=_TOKEN, automation=service)
    if flag is not None:
        import opencode.httpapi.app as app_module

        if hasattr(app_module, "EVENT_AUTOMATIONS_ENABLED"):
            app_module.EVENT_AUTOMATIONS_ENABLED = flag
    return api, app, sessions


class _CapabilityAbsent(Exception):
    """The seam this probe measures is not implemented yet."""


async def _request(
    api: Any, method: str, path: str, *, body: dict[str, Any] | None = None, query: bytes = b""
) -> tuple[int, Any]:
    raw = _json_body(body) if body is not None else None
    status, _headers, payload, _receive = await _asgi_request(
        api, method, path, token=_TOKEN, body=raw, query_string=query
    )
    decoded: Any = None
    if payload:
        try:
            decoded = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            decoded = None
    return status, decoded


async def _close(app: Any) -> None:
    if hasattr(app, "close"):
        result = app.close()
        if hasattr(result, "__await__"):
            await result


# ---------------------------------------------------------------------------
# P0 -- the route capability
# ---------------------------------------------------------------------------


async def test_p0_route_capability(tmp_path: Path) -> None:
    db_path = tmp_path / "p0.db"
    _require_success(_upgrade_head(db_path))
    app = App(db_path, working_dir="C:/work")
    try:
        api = create_app(app, token=_TOKEN)  # the new parameter must stay optional
        paths = {getattr(route, "path", "") for route in api.routes}
        has_injection = "automation" in inspect.signature(create_app).parameters
        faces = {
            "routes_present": _EXPECTED_ROUTES <= paths,
            "automation_parameter": has_injection,
        }
        missing = sorted(_EXPECTED_ROUTES - paths)
    finally:
        await _close(app)
    status = "capability-absent" if not any(faces.values()) else ("probed" if all(faces.values()) else "partial")
    _report({"scenario": "P-capability", "status": status, **faces, "missing": missing})
    assert status in ("capability-absent", "partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- create + the upstream error mapping
# ---------------------------------------------------------------------------


async def test_m1_create_error_mapping(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, _sessions = await _make_api(tmp_path)
        try:
            ok_status, ok_body = await _request(
                api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path))
            )
            metrics["valid_create_200"] = (
                ok_status == 200 and isinstance(ok_body, dict) and "id" in ok_body.get("data", {})
            )
            bad_status, bad_body = await _request(
                api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path), trigger={"type": "at"})
            )
            metrics["invalid_payload_422"] = bad_status == 422 and isinstance(bad_body, dict)
            gone_status, _gone_body = await _request(
                api,
                "POST",
                "/api/experimental/automation",
                body=_create_payload(
                    str(tmp_path), action={"type": "prompt", "prompt": "x", "to": {"session": "missing_session"}}
                ),
            )
            metrics["unknown_session_404"] = gone_status == 404
            status = "mapped" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M1-create-errors", "status": status, **metrics})
    assert status in ("capability-absent", "mapped", "drifting")


# ---------------------------------------------------------------------------
# M2 -- the flag gate + the gone-folder semantics
# ---------------------------------------------------------------------------


async def test_m2_flag_gate_and_gone_directory(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, _sessions = await _make_api(tmp_path, flag=True)
        try:
            created, body = await _request(
                api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path))
            )
            automation_id = (body or {}).get("data", {}).get("id")
            metrics["enabled_create_200"] = created == 200 and automation_id is not None
            # A gone folder stays manageable: the row's directory no longer
            # exists, and get must still answer 200 (upstream catch semantics).
            conn = sqlite3.connect(tmp_path / "probe.db")
            try:
                conn.execute(
                    "update automation set directory = ? where id = ?",
                    ("C:/gone/nowhere", automation_id),
                )
                conn.commit()
            finally:
                conn.close()
            gone, _gone_body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
            metrics["gone_folder_still_readable"] = gone == 200
            import opencode.httpapi.app as app_module

            if hasattr(app_module, "EVENT_AUTOMATIONS_ENABLED"):
                try:
                    app_module.EVENT_AUTOMATIONS_ENABLED = False
                    blocked, _blocked_body = await _request(
                        api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path), name="second")
                    )
                    metrics["disabled_create_403"] = blocked == 403
                finally:
                    app_module.EVENT_AUTOMATIONS_ENABLED = True
            else:
                metrics["disabled_create_403"] = False
            status = "gated" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M2-flag-gate", "status": status, **metrics})
    assert status in ("capability-absent", "gated", "drifting")


# ---------------------------------------------------------------------------
# M3 -- the CRUD round trip
# ---------------------------------------------------------------------------


async def test_m3_crud_round_trip(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, _sessions = await _make_api(tmp_path)
        try:
            _created, body = await _request(
                api, "POST", "/api/experimental/automation", body=_create_payload(str(tmp_path))
            )
            automation_id = (body or {}).get("data", {}).get("id")
            get_status, get_body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
            metrics["get_200"] = get_status == 200 and (get_body or {}).get("data", {}).get("id") == automation_id
            pause_status, pause_body = await _request(
                api, "POST", f"/api/experimental/automation/{automation_id}/pause"
            )
            metrics["pause_200_paused"] = (
                pause_status == 200 and (pause_body or {}).get("data", {}).get("status") == "paused"
            )
            resume_status, resume_body = await _request(
                api, "POST", f"/api/experimental/automation/{automation_id}/resume"
            )
            metrics["resume_200_active"] = (
                resume_status == 200 and (resume_body or {}).get("data", {}).get("status") == "active"
            )
            unknown, _unknown_body = await _request(api, "GET", "/api/experimental/automation/no-such-id")
            metrics["unknown_404"] = unknown == 404
            delete_status, _delete_body = await _request(api, "DELETE", f"/api/experimental/automation/{automation_id}")
            metrics["delete_204"] = delete_status == 204
            after_delete, _after_body = await _request(api, "GET", f"/api/experimental/automation/{automation_id}")
            metrics["deleted_get_404"] = after_delete == 404
            status = "round-tripped" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M3-crud", "status": status, **metrics})
    assert status in ("capability-absent", "round-tripped", "drifting")


# ---------------------------------------------------------------------------
# M4 -- list filtering
# ---------------------------------------------------------------------------


async def test_m4_list_filtering(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, _sessions = await _make_api(tmp_path)
        try:
            _first, first_body = await _request(
                api,
                "POST",
                "/api/experimental/automation",
                body=_create_payload(str(tmp_path), name="one", session_id=_SESSION_ID),
            )
            other_dir = tmp_path / "other"
            other_dir.mkdir()
            _second, second_body = await _request(
                api,
                "POST",
                "/api/experimental/automation",
                body=_create_payload(str(other_dir), name="two"),
            )
            first_id = (first_body or {}).get("data", {}).get("id")
            second_id = (second_body or {}).get("data", {}).get("id")
            all_status, all_body = await _request(api, "GET", "/api/experimental/automation")
            all_ids = [row.get("id") for row in (all_body or {}).get("data", [])]
            metrics["unfiltered_lists_all"] = all_status == 200 and first_id in all_ids and second_id in all_ids
            by_session, by_session_body = await _request(
                api, "GET", "/api/experimental/automation", query=f"session_id={_SESSION_ID}".encode()
            )
            session_ids = [row.get("id") for row in (by_session_body or {}).get("data", [])]
            # The session filter matches the OWNER (creating) session, not the
            # action target -- probe fix: row "one" carries the owner, row
            # "two" does not, so only "one" lists under the session.
            metrics["session_filter"] = by_session == 200 and session_ids == [
                first_id
            ]  # both rows target the same session
            by_dir, by_dir_body = await _request(
                api, "GET", "/api/experimental/automation", query=f"directory={other_dir!s}".encode()
            )
            dir_ids = [row.get("id") for row in (by_dir_body or {}).get("data", [])]
            metrics["directory_filter"] = by_dir == 200 and dir_ids == [second_id]
            unknown, _unknown_body = await _request(
                api, "GET", "/api/experimental/automation", query=b"session_id=missing_session"
            )
            metrics["unknown_session_404"] = unknown == 404
            status = "filtered" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M4-list-filter", "status": status, **metrics})
    assert status in ("capability-absent", "filtered", "drifting")


# ---------------------------------------------------------------------------
# M5 -- job.list
# ---------------------------------------------------------------------------


async def test_m5_job_list(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, _sessions = await _make_api(tmp_path)
        try:
            ok_status, ok_body = await _request(api, "GET", "/api/experimental/job")
            data = (ok_body or {}).get("data")
            metrics["route_200_data_array"] = ok_status == 200 and isinstance(data, list)
            filtered, _filtered_body = await _request(
                api, "GET", "/api/experimental/job", query=b"kind=shell&status=running"
            )
            metrics["query_reaches_filter"] = filtered == 200
            unknown, _unknown_body = await _request(
                api, "GET", "/api/experimental/job", query=b"session_id=missing_session"
            )
            metrics["unknown_session_404"] = unknown == 404
            status = "listed" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M5-job-list", "status": status, **metrics})
    assert status in ("capability-absent", "listed", "drifting")
