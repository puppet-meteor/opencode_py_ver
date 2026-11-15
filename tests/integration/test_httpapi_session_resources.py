"""T-C1-094 (P6-S1) integration tests: the session resource HTTP faces.

The seven routes over one :class:`~opencode.app.App` composition through the
transport in :mod:`opencode.httpapi.app` — list/get/delete/fork/export/
import/single-message — plus the seven SDK client methods over a real
uvicorn server. Contract anchors: ticket ``doc/orchestration/tasks/T-C1-094.md``
§3 decisions 1-9 (flat PY JSON, no TS ``{"data":...}`` wrapper; the delete is
an event-sourced tombstone; fork is a message-level replay under a fresh id;
export/import is the self-describing round trip) and §4 items 1-8.

④ disclosures: the raw-ASGI driver, scripted provider, gate and app harness
are the test_httpapi_server.py forms (imported per the cross-file test-import
precedent — test_registration_hosts.py); the SDK boot harness is the
test_sdk_client.py form. The fork/import replay copies projected message ids
verbatim (the fork IS a copy), so the ④-3 surviving-trace probe
``session_id_by_message_id`` can see one id under two sessions — the probe
feeds only an ``is not None`` corruption classifier, so the ambiguity never
changes a classification. Import restores only under a completely fresh id:
the payload's own id must be unoccupied (row OR log), which is what the
export→delete→verbatim-import 409 face below pins.

§4 checklist mapping (item -> test):

1. list            -> test_list_is_empty_then_two_sessions_created_descending
2. get             -> test_get_answers_the_four_field_wire_and_404_for_missing
3. delete          -> test_delete_tombstones_every_resource_face_with_404 /
                      test_delete_refuses_an_in_flight_run_with_409
4. fork            -> test_fork_truncates_copies_and_never_touches_the_source /
                      test_fork_missing_source_and_foreign_before_answer_404
5. export/import   -> test_export_import_round_trip_conflict_and_fail_closed
6. message get     -> test_message_get_answers_the_wire_and_404_faces
7. SDK methods     -> test_sdk_list_get_message_faces /
                      test_sdk_delete_fork_export_import_round_trip
8. zero regression -> no route of the existing twelve is touched here; the
   full-suite gate run (test_httpapi_server.py / test_httpapi_sse.py /
   test_sdk_client.py green) is the double proof.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import PY_SESSION_DELETED
from opencode.httpapi import create_app
from opencode.sdk import OpencodeApiError, OpencodeClient
from tests.integration.test_httpapi_server import (
    _SNAPSHOT_BODY,
    _TERMINAL,
    _TOKEN,
    _asgi_json,
    _assert_core_error,
    _await_http_status,
    _fetch_all,
    _final_script,
    _http_create_session,
    _http_prompt,
    _json_body,
    _require_success,
    _run_app,
    _Script,
    _ScriptedProvider,
    _upgrade_head,
)
from tests.integration.test_sdk_client import (
    _await_status,
    _boot_server,
    _KeyedProvider,
    _Running,
    _stop_server,
)


async def _settled_session(api: Any, provider: _ScriptedProvider, text: str, request_id: str) -> tuple[str, str]:
    """Create one session and drive its one prompt to a terminal (3 messages)."""
    session_id = await _http_create_session(api)
    assert provider.scripts, "the scripted provider must hold this prompt's script"
    ref = await _http_prompt(api, session_id, text, request_id)
    run_id = str(ref["run_id"])
    final = await _await_http_status(api, run_id, _TERMINAL)
    assert final["status"] == "succeeded" and final["error"] is None
    return session_id, run_id


async def _message_items(api: Any, session_id: str) -> list[dict[str, Any]]:
    """The full projected history of one session over the messages route."""
    status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages")
    assert status == 200
    assert isinstance(payload, dict)
    items: list[dict[str, Any]] = payload["items"]
    return items


# ---------------------------------------------------------------------------
# §4-1/2: list + get
# ---------------------------------------------------------------------------


async def test_list_is_empty_then_two_sessions_created_descending(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _run_app(db_path, _ScriptedProvider([[]])) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(api, "GET", "/session")
        assert status == 200
        assert payload == []  # the empty database answers the empty list
        first = await _http_create_session(api)
        second = await _http_create_session(api)
        status, _, payload, _ = await _asgi_json(api, "GET", "/session")
        assert status == 200
        assert isinstance(payload, list) and len(payload) == 2
        assert [row["session_id"] for row in payload] == [second, first]  # created-descending
        for row in payload:
            assert set(row) == {"session_id", "status", "applied_seq", "active_run_id", "created_at_ms"}
            assert row["status"] == "active"
            assert row["applied_seq"] == 1  # one py.session.created projected
            assert row["active_run_id"] is None
            assert isinstance(row["created_at_ms"], int)
        assert payload[0]["created_at_ms"] >= payload[1]["created_at_ms"]


async def test_get_answers_the_four_field_wire_and_404_for_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _run_app(db_path, _ScriptedProvider([[]])) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}")
        assert status == 200
        assert set(payload) == {"session_id", "status", "applied_seq", "active_run_id"}
        assert payload["session_id"] == session_id
        assert payload["status"] == "active"
        assert payload["applied_seq"] == 1
        assert payload["active_run_id"] is None
        status, _, payload, _ = await _asgi_json(api, "GET", "/session/ses_never_created_0001")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


# ---------------------------------------------------------------------------
# §4-3: delete (tombstone effects + the active-run 409)
# ---------------------------------------------------------------------------


async def test_delete_tombstones_every_resource_face_with_404(tmp_path: Path) -> None:
    """One 204 delete hides get/list/messages/events; the data itself survives."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id, _run_id = await _settled_session(api, provider, "hello", "req_1")
        other = await _http_create_session(api)  # the list face keeps its other rows
        assert len(await _message_items(api, session_id)) == 3

        # a never-created session answers 404, never a tombstone
        status, _, payload, _ = await _asgi_json(api, "DELETE", "/session/ses_never_created_0002")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)

        # the first delete is 204 No Content with an empty body (the TS form)
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{session_id}")
        assert status == 204
        assert payload is None

        # every resource face of the deleted session answers 404
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{session_id}/events")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)

        # the list face excludes the deleted session, keeps the others
        status, _, listed, _ = await _asgi_json(api, "GET", "/session")
        assert status == 200
        assert [row["session_id"] for row in listed] == [other]

        # a repeated delete is the same 404 (the TS NoContent-then-NotFound form)
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{session_id}")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)

        # the tombstone is a hide, not a purge: the log, the flipped row and
        # the projected messages all survive in place
        assert _fetch_all(db_path, "SELECT status FROM sessions WHERE session_id = ?", (session_id,)) == [("deleted",)]
        kinds = [
            str(row[0])
            for row in _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
        ]
        assert kinds[-1] == PY_SESSION_DELETED
        assert _fetch_all(db_path, "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)) == [(3,)]


async def test_delete_refuses_an_in_flight_run_with_409(tmp_path: Path) -> None:
    """A session holding an active run is never deleted under; after the run settles, it is."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "hello")
    gate = script.gate()  # the run parks mid-stream until the test opens the gate
    provider = _ScriptedProvider([[script]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "hello", "req_1")
        run_id = str(ref["run_id"])
        await _await_http_status(api, run_id, {"running"})
        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)  # parked proof (T-C1-061b form)
        status, _, info, _ = await _asgi_json(api, "GET", f"/session/{session_id}")
        assert status == 200 and info["active_run_id"] == run_id  # the busy slot is visible on the get wire
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{session_id}")
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.SESSION_BUSY, retryable=False)
        gate.open()  # release the parked stream: the run folds interrupted
        final = await _await_http_status(api, run_id, _TERMINAL)
        assert final["status"] == "interrupted"
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{session_id}")
        assert status == 204
        assert payload is None


# ---------------------------------------------------------------------------
# §4-4: fork
# ---------------------------------------------------------------------------


async def test_fork_truncates_copies_and_never_touches_the_source(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        source, _run_id = await _settled_session(api, provider, "hello", "req_1")
        items = await _message_items(api, source)
        assert len(items) == 3  # user / assistant / idle
        status, _, before_fork, _ = await _asgi_json(api, "GET", f"/session/{source}")
        assert status == 200

        # before=<assistant id>: the copy truncates to the messages before it
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{source}/fork", body=_json_body({"before": items[1]["id"]})
        )
        assert status == 200
        assert set(payload) == {"session_id"}
        truncated = str(payload["session_id"])
        assert truncated != source and truncated.startswith("ses_")
        assert (await _message_items(api, truncated)) == items[:1]

        # an absent body copies the full history
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{source}/fork", body=b"")
        assert status == 200
        full = str(payload["session_id"])
        assert full != source and full != truncated
        assert (await _message_items(api, full)) == items

        # the source is untouched: same get wire, same history (read-only copy)
        status, _, after_fork, _ = await _asgi_json(api, "GET", f"/session/{source}")
        assert status == 200
        assert after_fork == before_fork
        assert (await _message_items(api, source)) == items


async def test_fork_missing_source_and_foreign_before_answer_404(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()], [_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        source, _ = await _settled_session(api, provider, "first", "req_1")
        foreign, _ = await _settled_session(api, provider, "second", "req_2")
        foreign_user_id = (await _message_items(api, foreign))[0]["id"]

        status, _, payload, _ = await _asgi_json(api, "POST", "/session/ses_never_created_0003/fork", body=b"")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # a before id that exists nowhere
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{source}/fork", body=_json_body({"before": "msg_nowhere_0001"})
        )
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # a before id that exists — in another session (never a cross-session copy)
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{source}/fork", body=_json_body({"before": foreign_user_id})
        )
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # the body shape is strict: unknown fields and mistyped before answer 422
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{source}/fork", body=_json_body({"after": "x"})
        )
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{source}/fork", body=_json_body({"before": 5}))
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        # the source still forks after all the refusals (nothing was consumed)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{source}/fork", body=b"")
        assert status == 200


# ---------------------------------------------------------------------------
# §4-5: export -> import (round trip, conflict, fail-closed)
# ---------------------------------------------------------------------------


async def test_export_import_round_trip_conflict_and_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        source, _run_id = await _settled_session(api, provider, "hello", "req_1")
        items = await _message_items(api, source)
        assert len(items) == 3

        status, _, export, _ = await _asgi_json(api, "GET", f"/session/{source}/export")
        assert status == 200
        assert set(export) == {
            "session_id",
            "status",
            "working_dir",
            "config_snapshot",
            "snapshot_hash",
            "created_at_ms",
            "messages",
        }
        assert export["session_id"] == source
        assert export["status"] == "active"
        assert export["working_dir"] == "C:/work"
        assert isinstance(export["snapshot_hash"], str) and export["snapshot_hash"]
        assert isinstance(export["created_at_ms"], int)
        assert export["messages"] == items  # the projected history rides whole

        # the payload's own id is occupied by the living source -> 409
        status, _, payload, _ = await _asgi_json(api, "POST", "/session/import", body=_json_body(export))
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.CONFLICT, retryable=False)

        # a fresh target id restores the payload (the export -> import-new-id form)
        restored = dict(export, session_id="ses_import_restore_0001")
        status, _, payload, _ = await _asgi_json(api, "POST", "/session/import", body=_json_body(restored))
        assert status == 200
        assert set(payload) == {"session_id"}
        assert payload["session_id"] == "ses_import_restore_0001"
        # count conservation: 3 messages exported, 3 restored, wire-identical
        assert (await _message_items(api, "ses_import_restore_0001")) == items
        status, _, info, _ = await _asgi_json(api, "GET", "/session/ses_import_restore_0001")
        assert status == 200
        assert info["status"] == "active"  # the restored session is ACTIVE (disclosed)
        assert info["applied_seq"] == 4  # created + three imported messages
        assert info["active_run_id"] is None
        # the list face keeps the round trip's created stamp (the ordering key)
        status, _, listed, _ = await _asgi_json(api, "GET", "/session")
        row = next(item for item in listed if item["session_id"] == "ses_import_restore_0001")
        assert row["created_at_ms"] == export["created_at_ms"]

        # re-importing the same payload conflicts: the id lives again
        status, _, payload, _ = await _asgi_json(api, "POST", "/session/import", body=_json_body(restored))
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.CONFLICT, retryable=False)

        # fail-closed faces (ticket §8): every refusal below lands nothing
        undecodable = dict(export, session_id="ses_import_bad_message_001")
        undecodable["messages"] = [dict(item) for item in export["messages"]]
        del undecodable["messages"][0]["text"]  # a user message without its text
        tampered = dict(export, session_id="ses_import_tampered_0001", snapshot_hash="0" * 64)
        duplicate = dict(
            export, session_id="ses_import_duplicate_0001", messages=[export["messages"][0], export["messages"][0]]
        )
        unknown_key = dict(export, session_id="ses_import_unknown_key_001")
        unknown_key["unexpected"] = True
        for bad in (undecodable, tampered, duplicate, unknown_key):
            status, _, payload, _ = await _asgi_json(api, "POST", "/session/import", body=_json_body(bad))
            assert status == 422, bad["session_id"]
            _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
            status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{bad['session_id']}")
            assert status == 404  # nothing was created: no silent drops, no half imports

        # the delete does not free the id: the tombstone keeps it occupied
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{source}")
        assert status == 204
        assert payload is None
        status, _, payload, _ = await _asgi_json(api, "POST", "/session/import", body=_json_body(export))
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.CONFLICT, retryable=False)


# ---------------------------------------------------------------------------
# §4-6: the single-message get
# ---------------------------------------------------------------------------


async def test_message_get_answers_the_wire_and_404_faces(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_final_script()], [_final_script()]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        source, _ = await _settled_session(api, provider, "first", "req_1")
        foreign, _ = await _settled_session(api, provider, "second", "req_2")
        items = await _message_items(api, source)
        foreign_id = (await _message_items(api, foreign))[0]["id"]

        # an existing message answers exactly its §2.1 wire
        for item in items:
            status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{source}/message/{item['id']}")
            assert status == 200
            assert payload == item
        # a message id that exists nowhere in this session
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{source}/message/msg_nowhere_0002")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # a message id that exists — in another session (no cross-session leak)
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{source}/message/{foreign_id}")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # the deleted session's message face answers the same 404
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{source}")
        assert status == 204
        status, _, payload, _ = await _asgi_json(api, "GET", f"/session/{source}/message/{items[0]['id']}")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


# ---------------------------------------------------------------------------
# §4-7: the seven SDK methods over a real server (the test_sdk_client.py form)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sdk_running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Running]:
    root = tmp_path_factory.mktemp("sdk_session_resources_server")
    handle = _boot_server(root / "app.db", _KeyedProvider())
    try:
        yield handle
    finally:
        _stop_server(handle)


@pytest.fixture(scope="module")
def sdk_client(sdk_running: _Running) -> OpencodeClient:
    return OpencodeClient(base_url=sdk_running.base, token=_TOKEN)


def test_sdk_list_get_message_faces(sdk_client: OpencodeClient) -> None:
    ref = sdk_client.create_session(_SNAPSHOT_BODY)
    run = sdk_client.prompt(ref.session_id, "sdk session resources hello", f"req_{ref.session_id}")
    _await_status(sdk_client, run.run_id, _TERMINAL)

    summaries = sdk_client.list_sessions()
    stamps = [summary.created_at_ms for summary in summaries]
    assert stamps == sorted(stamps, reverse=True)  # created-descending on the live list
    mine = [summary for summary in summaries if summary.session_id == ref.session_id]
    assert len(mine) == 1
    assert mine[0].status == "active"
    assert mine[0].active_run_id is None
    assert mine[0].applied_seq >= 1
    assert isinstance(mine[0].created_at_ms, int)

    info = sdk_client.get_session(ref.session_id)
    assert info.session_id == ref.session_id
    assert info.status == "active"
    assert info.applied_seq >= 1
    assert info.active_run_id is None

    page = sdk_client.list_messages(ref.session_id)
    assert len(page.items) == 3
    first = sdk_client.get_message(ref.session_id, page.items[0].id)
    assert first == page.items[0]  # the single-message face equals the page's item
    with pytest.raises(OpencodeApiError) as missing:
        sdk_client.get_message(ref.session_id, "msg_sdk_nowhere_0001")
    assert missing.value.status_code == 404
    with pytest.raises(OpencodeApiError) as gone:
        sdk_client.get_session("ses_sdk_never_created_0001")
    assert gone.value.status_code == 404


def test_sdk_delete_fork_export_import_round_trip(sdk_client: OpencodeClient) -> None:
    ref = sdk_client.create_session(_SNAPSHOT_BODY)
    run = sdk_client.prompt(ref.session_id, "sdk fork export import hello", f"req_{ref.session_id}")
    _await_status(sdk_client, run.run_id, _TERMINAL)
    source_items = sdk_client.list_messages(ref.session_id).items
    assert len(source_items) == 3

    # fork: the full copy, then the truncation before the assistant message
    full = sdk_client.fork_session(ref.session_id)
    assert full.session_id != ref.session_id
    assert sdk_client.list_messages(full.session_id).items == source_items
    truncated = sdk_client.fork_session(ref.session_id, before=source_items[1].id)
    assert truncated.session_id not in {ref.session_id, full.session_id}
    assert sdk_client.list_messages(truncated.session_id).items == source_items[:1]
    # the source is untouched by both forks (read-only copies)
    assert sdk_client.list_messages(ref.session_id).items == source_items

    # export -> delete -> import under a fresh id: the history is back
    exported = sdk_client.export_session(ref.session_id)
    assert exported.session_id == ref.session_id
    assert exported.working_dir == "C:/work"
    assert exported.status == "active"
    assert [message.id for message in exported.messages] == [message.id for message in source_items]
    # 204 -> no body on the wire: the SDK face is typed -> None (and the raw
    # empty-body 204 is pinned by the httpx-level delete test), so the call is
    # a discarded statement here.
    sdk_client.delete_session(ref.session_id)
    with pytest.raises(OpencodeApiError) as gone:
        sdk_client.get_session(ref.session_id)
    assert gone.value.status_code == 404
    with pytest.raises(OpencodeApiError) as repeat:
        sdk_client.delete_session(ref.session_id)  # the repeated delete is the same 404
    assert repeat.value.status_code == 404

    restored = replace(exported, session_id="ses_sdk_import_restore_0001")
    imported = sdk_client.import_session(restored)  # the SessionExport object form
    assert imported.session_id == "ses_sdk_import_restore_0001"
    assert sdk_client.list_messages(imported.session_id).items == source_items  # count conserved, wire-identical
    with pytest.raises(OpencodeApiError) as conflict:
        sdk_client.import_session(restored.to_payload())  # the JSON-object form conflicts on the living id
    assert conflict.value.status_code == 409
