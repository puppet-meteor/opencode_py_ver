"""T-C1-096 (P6-S3) integration tests: the manual compaction HTTP/SDK face.

The wiring ticket over the §2.4 chain (CORE-CONTRACTS §2.4; the engine,
the ``py.context.compacted`` event, the projection and the post-settlement
auto trigger are already in the library — T-C1-060/062/063): the manual
route ``POST /session/{session_id}/compact``, its service face
:meth:`opencode.session.service.SessionService.compact_session`, and the
SDK twin :meth:`opencode.sdk.client.OpencodeClient.compact_session`.
Contract anchors: ticket ``doc/orchestration/tasks/T-C1-096.md`` §3
decisions 1-8 (the route path, the flat per-status wire, the S1-delete
guard order, the fail-closed 422 engine gate, notify-on-committed-only,
the no-swallow error policy split from the auto trigger, the census pin,
zero touch on the auto-trigger wiring).

④ disclosures: the raw-ASGI driver, scripted provider, gate and app
harness are the test_httpapi_server.py forms and the SDK boot harness is
the test_sdk_client.py form (imported per the cross-file test-import
precedent — test_httpapi_session_resources.py). The fixture insight every
test rests on: a provider-wired App auto-compacts after EVERY settled run
(the T-C1-062 wiring), so a settled-history fixture built from runs would
be compacted before the manual call ever fires — the ≥40 settled histories
below are built through ``POST /session/import`` instead (imported
messages settle at their own seq; no run ever fired, so no trigger fired).
The summary call then flows through the REAL ``ProviderSummarizer`` — the
scripted provider answers it from the queue like any other request, so the
deterministic text/usage/model ride the durable event without any
white-box summarizer injection. ``discarded`` is deliberately not probed
here: constructing it needs a concurrent commit landing under the summary
call (the engine's contract tests own that matrix; ticket §5 disclosure 3).

§4 checklist mapping (item -> test):

1. compacted full chain   -> test_manual_compact_answers_the_compacted_wire_and_the_durable_event
2. below_threshold        -> test_below_threshold_answers_status_only_and_lands_no_events
3. 404 faces              -> test_missing_and_deleted_sessions_answer_404
4. 409 busy               -> test_an_in_flight_run_answers_409_until_the_run_settles
5. engine unavailable     -> test_a_dormant_composition_answers_422_and_the_guard_order_holds
6. auto-trigger e2e       -> test_the_post_run_auto_trigger_compacts_without_a_manual_call
7. SDK round trip         -> test_sdk_compact_session_answers_both_outcome_faces
8. zero regression        -> no existing route's semantics is touched here; the
   census pin (20->21) lives in test_sdk_client.py and the full-suite gate
   run (test_httpapi_server / test_httpapi_sse / test_sdk_client /
   test_httpapi_session_resources / test_compaction_engine /
   test_compaction_projection green) is the double proof.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from opencode.contracts import CoreErrorCode, compute_snapshot_hash
from opencode.contracts.events import PY_CONTEXT_COMPACTED, PY_SESSION_CREATED
from opencode.contracts.llm import LLMTextPart, Usage
from opencode.contracts.messages import AssistantMessage, AssistantTextPart, IdleMessage, UserMessage
from opencode.httpapi import create_app
from opencode.sdk import OpencodeApiError, OpencodeClient
from opencode.sdk.client import CompactionReceipt
from opencode.session.compaction import COMPACT_RETAINED_TAIL_MESSAGES, COMPACT_TRIGGER_MESSAGE_COUNT
from tests.integration.test_httpapi_server import (
    _MODEL,
    _SNAPSHOT,
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
    _plain_app,
    _require_success,
    _run_app,
    _Script,
    _ScriptedProvider,
    _upgrade_head,
)
from tests.integration.test_sdk_client import (
    _boot_server,
    _KeyedProvider,
    _Running,
    _stop_server,
)

#: One shared timestamp for the synthesized import history (the fold keys on
#: seqs, never on created_at; the ordering key rides created_at_ms verbatim).
_STAMP = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)

#: The deterministic summary bodies the scripted provider answers the
#: summarizer's request with (one text delta + the default usage 5/7).
_SUMMARY_TEXT = "the replacement summary"
_AUTO_SUMMARY_TEXT = "auto compaction summary"

#: The wire form of ``Usage(input=5, output=7)`` — all five §2.1 keys, the
#: unknowns as nulls (the model dumps its whole field set).
_USAGE_WIRE = {"input": 5, "output": 7, "reasoning": None, "cache_read": None, "cache_write": None}


def _summary_script(text: str) -> _Script:
    """The scripted summary stream: one text part, default usage, terminal finish."""
    return _Script().text("p1", text).usage().finish()


def _import_payload(session_id: str, *, triplets: int) -> dict[str, Any]:
    """One import payload with ``triplets`` user/assistant/idle triplets.

    The export wire exactly (T-C1-094 §3 decision 5): the seven-key shape
    with the §6.2-canonical snapshot hash, and a message history that
    settles wholesale on import — no run ever fired, so the T-C1-062
    post-settlement trigger never fires (the fixture insight above).
    """
    messages: list[dict[str, Any]] = []
    for index in range(triplets):
        messages.append(
            UserMessage(
                id=f"msg_import_u_{index:04d}",
                type="user",
                created_at=_STAMP,
                metadata={},
                text=f"imported question {index}",
            ).model_dump(mode="json")
        )
        messages.append(
            AssistantMessage(
                id=f"msg_import_a_{index:04d}",
                type="assistant",
                created_at=_STAMP,
                metadata={},
                model=_MODEL,
                parts=[AssistantTextPart(type="text", part_id=f"part_{index:04d}", text=f"imported answer {index}")],
                status="completed",
            ).model_dump(mode="json")
        )
        messages.append(
            IdleMessage(
                id=f"msg_import_i_{index:04d}",
                type="idle",
                created_at=_STAMP,
                metadata={},
                run_id=f"run_import_{index:04d}",
                outcome="succeeded",
            ).model_dump(mode="json")
        )
    return {
        "session_id": session_id,
        "status": "active",
        "working_dir": "C:/work",
        "config_snapshot": _SNAPSHOT_BODY,
        "snapshot_hash": compute_snapshot_hash(_SNAPSHOT),
        "created_at_ms": int(_STAMP.timestamp() * 1000),
        "messages": messages,
    }


def _compacted_rows(db_path: Path, session_id: str) -> list[tuple[str, int, dict[str, Any]]]:
    """Every durable ``py.context.compacted`` row (id, seq, data) in commit order."""
    return [
        (str(row[0]), int(row[1]), json.loads(row[2]) if isinstance(row[2], str) else dict(row[2]))
        for row in _fetch_all(
            db_path,
            "SELECT id, seq, data FROM events WHERE session_id = ? AND type = ? ORDER BY seq",
            (session_id, PY_CONTEXT_COMPACTED),
        )
    ]


async def _await_compacted_row(db_path: Path, session_id: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    """Poll the events table until one compaction row lands (live sqlite read).

    The auto-trigger pass runs inside the scheduled run task AFTER the
    terminal status is reader-visible, so the sqlite probe (the
    ``_await_permission_requested`` form, incl. the locked-database retry)
    is the deterministic wait for it.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            rows = _compacted_rows(db_path, session_id)
        except sqlite3.OperationalError:
            rows = []  # a concurrent writer holds the lock; retry after a tick
        if rows:
            return rows[0][2]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"no py.context.compacted event landed for session {session_id}")
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# §4-1: the compacted full chain (import-built history -> manual pass -> durable event)
# ---------------------------------------------------------------------------


async def test_manual_compact_answers_the_compacted_wire_and_the_durable_event(tmp_path: Path) -> None:
    """The ≥40 settled history imported (no run, no auto-trigger), one manual pass,
    the flat compacted wire, the seven-field durable event, and the idempotent
    second pass answering below_threshold."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([[_summary_script(_SUMMARY_TEXT)]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = "ses_compact_import_0001"
        # 14 triplets = 42 imported messages: the smallest multiple-of-three
        # history over the 40-message threshold.
        imported = ((COMPACT_TRIGGER_MESSAGE_COUNT + 2) // 3) * 3
        assert imported == 42 and imported >= COMPACT_TRIGGER_MESSAGE_COUNT
        status, _, body, _ = await _asgi_json(
            api, "POST", "/session/import", body=_json_body(_import_payload(session_id, triplets=imported // 3))
        )
        assert status == 200 and body == {"session_id": session_id}
        assert provider.requests == []  # an import replays events; no model call ever happens

        status, _, wire, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 200
        covered = imported - COMPACT_RETAINED_TAIL_MESSAGES  # 34 covered, 8 retained
        # seq arithmetic: created at seq 1, the k-th import at seq k+1 -> the
        # 34th settled message sits at seq 35, the retained tail starts at 36.
        rows = _compacted_rows(db_path, session_id)
        assert len(rows) == 1
        event_id, seq, data = rows[0]
        assert seq == imported + 2  # 44: created + 42 imports + this event
        assert wire == {
            "status": "compacted",
            "covered_seq": covered + 1,
            "retained_from": covered + 2,
            "message_count": covered,
            "summary_hash": sha256(_SUMMARY_TEXT.encode("utf-8")).hexdigest(),
            "event_id": event_id,  # the wire id IS the durable event's id
            "usage": _USAGE_WIRE,
        }
        assert data == {
            "covered_seq": covered + 1,
            "retained_from": covered + 2,
            "summary_text": _SUMMARY_TEXT,  # durable-only on the event (seat whitelist)
            "summary_hash": sha256(_SUMMARY_TEXT.encode("utf-8")).hexdigest(),
            "model": {"provider": "maas", "model": "test-model"},  # the settings' ModelRef
            "usage": _USAGE_WIRE,
            "message_count": covered,
        }

        # the summary call was the REAL adapter's request: one user-role
        # no-tools GenerationRequest whose transcript is exactly the covered
        # interval (triplets 0..10 whole + triplet 11's user message).
        (request,) = provider.requests
        assert request.request_id.startswith("compaction_")
        assert request.model == _MODEL
        assert request.tools == []
        (message,) = request.messages
        assert message.role == "user"
        (part,) = message.content
        assert isinstance(part, LLMTextPart)
        assert "imported question 0" in part.text
        assert "imported question 11" in part.text
        assert "imported answer 11" not in part.text
        assert "imported question 12" not in part.text

        # the projection advances and nothing else: applied_seq moves, the
        # messages read model keeps every original row (§2.4 L134).
        status, _, info, _ = await _asgi_json(api, "GET", f"/session/{session_id}")
        assert status == 200
        assert info["applied_seq"] == imported + 2
        status, _, page, _ = await _asgi_json(api, "GET", f"/session/{session_id}/messages")
        assert status == 200 and len(page["items"]) == imported

        # the idempotent second pass: the folded view is now the summary
        # (None-linked, never counted) + the 8 retained -> below threshold,
        # status-only, zero new events, zero provider calls.
        status, _, wire2, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 200
        assert wire2 == {"status": "below_threshold"}
        assert len(provider.requests) == 1
        assert len(_compacted_rows(db_path, session_id)) == 1


# ---------------------------------------------------------------------------
# §4-2: below_threshold
# ---------------------------------------------------------------------------


async def test_below_threshold_answers_status_only_and_lands_no_events(tmp_path: Path) -> None:
    """A fresh session (<40 settled) answers the status-only wire: no summary call,
    no durable write; the ``{}`` JSON body is the accepted empty form too."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    provider = _ScriptedProvider([])  # any accidental model call would EOF loudly
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        status, _, wire, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=_json_body({}))
        assert status == 200
        assert wire == {"status": "below_threshold"}
        assert provider.requests == []
        rows = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
        assert [row[0] for row in rows] == [PY_SESSION_CREATED]  # zero durable writes

        # a non-empty body is refused on the §1 unknown-fields rule (the TS
        # {id, delivery?} inbox-steering face is not ported, DIFFERENT-BY-DESIGN)
        status, _, payload, _ = await _asgi_json(
            api, "POST", f"/session/{session_id}/compact", body=_json_body({"id": "msg_x"})
        )
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"[1]")
        assert status == 422  # a non-object body answers the transport face
        rows = _fetch_all(db_path, "SELECT type FROM events WHERE session_id = ? ORDER BY seq", (session_id,))
        assert [row[0] for row in rows] == [PY_SESSION_CREATED]


# ---------------------------------------------------------------------------
# §4-3: the 404 faces
# ---------------------------------------------------------------------------


async def test_missing_and_deleted_sessions_answer_404(tmp_path: Path) -> None:
    """A never-created session and a deleted tombstone answer the same 404 (the
    delete is a data-preserving hide: the log survives, the face refuses)."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _run_app(db_path, _ScriptedProvider([])) as app:
        api = create_app(app, token=_TOKEN)
        status, _, payload, _ = await _asgi_json(api, "POST", "/session/ses_compact_missing_0001/compact", body=b"")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        session_id = await _http_create_session(api)
        status, _, payload, _ = await _asgi_json(api, "DELETE", f"/session/{session_id}")
        assert status == 204 and payload is None
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)


# ---------------------------------------------------------------------------
# §4-4: the 409 busy face
# ---------------------------------------------------------------------------


async def test_an_in_flight_run_answers_409_until_the_run_settles(tmp_path: Path) -> None:
    """A parked run holds the busy slot (the S1 delete 409 probe form); once it
    settles, the same manual call passes the guard and answers below_threshold."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    script = _Script().text("p1", "working")
    gate = script.gate()  # the run parks mid-stream until the test opens the gate
    script.usage().finish()
    provider = _ScriptedProvider([[script]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "held prompt", "req_compact_busy_1")
        run_id = str(ref["run_id"])
        await _await_http_status(api, run_id, {"running"})
        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)  # parked proof (T-C1-061b form)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.SESSION_BUSY, retryable=False)
        gate.open()  # release the parked stream: usage + finish settle the run
        final = await _await_http_status(api, run_id, _TERMINAL)
        assert final["status"] == "succeeded"
        status, _, wire, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 200
        assert wire == {"status": "below_threshold"}  # 3 settled: the guard no longer holds


# ---------------------------------------------------------------------------
# §4-5: the dormant composition (fail-closed 422) and the guard order
# ---------------------------------------------------------------------------


async def test_a_dormant_composition_answers_422_and_the_guard_order_holds(tmp_path: Path) -> None:
    """A provider-less App has no compaction engine: a live session answers 422
    fail-closed (never a silent 200), while existence (404) and the busy slot
    (409) both precede the engine gate — an R1-accepted prompt (no runner
    wired) holds the slot the delete/busy guard reads."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    async with _plain_app(db_path) as app:
        api = create_app(app, token=_TOKEN)
        # existence precedes engine availability: never-created answers 404, not 422
        status, _, payload, _ = await _asgi_json(
            api, "POST", "/session/ses_compact_dormant_never_0001/compact", body=b""
        )
        assert status == 404
        _assert_core_error(payload, CoreErrorCode.NOT_FOUND, retryable=False)
        # busy precedes engine availability: the accepted prompt holds the slot
        session_id = await _http_create_session(api)
        ref = await _http_prompt(api, session_id, "dormant prompt", "req_compact_dormant_1")
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 409
        _assert_core_error(payload, CoreErrorCode.SESSION_BUSY, retryable=False)
        status, _, info, _ = await _asgi_json(api, "GET", f"/session/{session_id}")
        assert status == 200 and info["active_run_id"] == str(ref["run_id"])  # the R1 slot, no runner
        # a fresh live session reaches the engine gate: no summarizer -> 422
        fresh = await _http_create_session(api)
        status, _, payload, _ = await _asgi_json(api, "POST", f"/session/{fresh}/compact", body=b"")
        assert status == 422
        _assert_core_error(payload, CoreErrorCode.VALIDATION_ERROR, retryable=False)


# ---------------------------------------------------------------------------
# §4-6: the auto-trigger end-to-end (the validation leg; zero rewiring)
# ---------------------------------------------------------------------------


async def test_the_post_run_auto_trigger_compacts_without_a_manual_call(tmp_path: Path) -> None:
    """The T-C1-062 wiring over HTTP: 14 settled runs (42 settled messages) cross
    the threshold and the post-settlement pass commits the durable event with no
    manual call anywhere; the engine's own trigger face is not touched."""
    db_path = tmp_path / "app.db"
    _require_success(_upgrade_head(db_path))
    runs = 14  # 14 runs x 3 settled messages = 42 >= COMPACT_TRIGGER_MESSAGE_COUNT
    assert runs * 3 >= COMPACT_TRIGGER_MESSAGE_COUNT > (runs - 1) * 3
    provider = _ScriptedProvider([[_final_script()] for _ in range(runs)] + [[_summary_script(_AUTO_SUMMARY_TEXT)]])
    async with _run_app(db_path, provider) as app:
        api = create_app(app, token=_TOKEN)
        session_id = await _http_create_session(api)
        for index in range(runs):
            ref = await _http_prompt(api, session_id, f"auto trigger turn {index}", f"req_auto_{index}")
            final = await _await_http_status(api, str(ref["run_id"]), _TERMINAL)
            assert final["status"] == "succeeded"
        # the pass runs after the last terminal is reader-visible: poll for it
        data = await _await_compacted_row(db_path, session_id)
        assert data["message_count"] == runs * 3 - COMPACT_RETAINED_TAIL_MESSAGES  # 34
        assert data["summary_text"] == _AUTO_SUMMARY_TEXT
        assert data["usage"] == _USAGE_WIRE
        assert len(_compacted_rows(db_path, session_id)) == 1  # exactly one durable event
        assert len(provider.requests) == runs + 1  # the runs + exactly one summary call
        # the manual face still answers honestly over the replaced view
        status, _, wire, _ = await _asgi_json(api, "POST", f"/session/{session_id}/compact", body=b"")
        assert status == 200
        assert wire == {"status": "below_threshold"}  # summary (None-linked) + 8 retained


# ---------------------------------------------------------------------------
# §4-7: the SDK round trip over a real server (the test_sdk_client.py form)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sdk_running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Running]:
    root = tmp_path_factory.mktemp("sdk_compaction_server")
    handle = _boot_server(root / "app.db", _KeyedProvider())
    try:
        yield handle
    finally:
        _stop_server(handle)


@pytest.fixture(scope="module")
def sdk_client(sdk_running: _Running) -> OpencodeClient:
    return OpencodeClient(base_url=sdk_running.base, token=_TOKEN)


def test_sdk_compact_session_answers_both_outcome_faces(sdk_client: OpencodeClient) -> None:
    """The SDK twin: below_threshold on a fresh session, compacted over an
    import-built history (the shared server's keyed provider auto-answers the
    summary call with its default finishing script: text "all done", usage 5/7),
    the idempotent second pass, and the 404 error face."""
    fresh = sdk_client.create_session(_SNAPSHOT_BODY)
    receipt = sdk_client.compact_session(fresh.session_id)
    assert receipt == CompactionReceipt(status="below_threshold")

    session_id = "ses_sdk_compact_import_0001"
    sdk_client.import_session(_import_payload(session_id, triplets=14))
    receipt = sdk_client.compact_session(session_id)
    assert receipt.status == "compacted"
    assert receipt.covered_seq == 35  # created at seq 1 + the k-th import at k+1
    assert receipt.retained_from == 36
    assert receipt.message_count == 34
    assert receipt.summary_hash == sha256(b"all done").hexdigest()
    assert isinstance(receipt.event_id, str) and receipt.event_id.startswith("evt_")
    assert receipt.usage == Usage(input=5, output=7)
    # the idempotent second pass decodes the status-only face
    assert sdk_client.compact_session(session_id) == CompactionReceipt(status="below_threshold")
    # the 404 face raises the §8-carrying API error
    with pytest.raises(OpencodeApiError) as missing:
        sdk_client.compact_session("ses_sdk_compact_never_0001")
    assert missing.value.status_code == 404
