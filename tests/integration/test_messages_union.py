"""S8 A-group: the messages() union face over real projected rows.

Four compatibility pins (T-C1-023 §3): ① the pure-User legacy regression,
② user/assistant/idle interleaved projection paging through the same
MessagePage rules, ③ cursor ownership across types and sessions, ④
whole-page fail-closed decode for corrupted rows. Assistant/idle rows come
from real full-stack runs (App + scripted provider + runner); synchronous
sqlite mutations happen strictly after durable terminals and app close (the
S7 incident discipline).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opencode.app import App
from opencode.contracts import (
    ConfigSnapshot,
    EventDraft,
    PromptAcceptedData,
    SessionCreatedData,
    compute_snapshot_hash,
)
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
)
from opencode.contracts.messages import AssistantMessage, IdleMessage, UserMessage
from opencode.session import SessionProjector
from opencode.session.context import RuntimeSettings
from opencode.session.service import RunStatus, SessionService
from opencode.storage import EventBatch, ReadRepositories, SessionStore, open_database
from opencode.storage.repos import InvalidCursorError, MessagePage
from opencode.storage.txn import StorageCorruptError

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"


class _StreamProvider:
    """Minimal scripted provider: one text+finish stream per request."""

    def __init__(self, streams: list[list[ProviderChunk]]) -> None:
        self._streams = [list(stream) for stream in streams]
        self.requests: list[GenerationRequest] = []

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        stream = self._streams.pop(0) if self._streams else []
        for chunk in stream:
            yield chunk

    async def close(self) -> None:
        return None


def _text_stream(*deltas: str) -> list[ProviderChunk]:
    """One text-part stream: one delta per fragment, then a STOP finish."""
    chunks: list[ProviderChunk] = [
        TextDeltaChunk(type="text_delta", request_id="req_union", index=number + 1, part_id="p1", delta=delta)
        for number, delta in enumerate(deltas)
    ]
    chunks.append(FinishChunk(type="finish", request_id="req_union", index=len(chunks) + 1, reason=FinishReason.STOP))
    return chunks


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model=ModelRef(provider="maas", model="union-test-model"),
        context_limit=100_000,
        output_reserve=4096,
        timeout_s=30.0,
    )


async def _await_terminal(service: SessionService, run_id: str, *, timeout_s: float = 10.0) -> RunStatus:
    """Poll the durable run status (§7 L314) until it reaches a terminal."""
    deadline = time.monotonic() + timeout_s
    while True:
        status = await service.run_status(run_id)
        if status.status.is_terminal:
            return status
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} did not reach a terminal status within {timeout_s}s")
        await asyncio.sleep(0.02)


def _user_texts(page: MessagePage) -> list[str]:
    """User-text projection over a page; every item must be a UserMessage
    (the ① isinstance pin doubles as the static narrowing for ``.text``)."""
    texts: list[str] = []
    for message in page.items:
        assert isinstance(message, UserMessage)
        texts.append(message.text)
    return texts


_CREATED_AT = datetime(2025, 1, 1, tzinfo=UTC)


def _session_created_draft(session_id: str, event_id: str) -> EventDraft[SessionCreatedData]:
    """``py.session.created`` draft (the test_eventlog store-commit idiom)."""
    snapshot = ConfigSnapshot.create()
    return EventDraft[SessionCreatedData](
        id=event_id,
        type="py.session.created",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=snapshot,
            snapshot_hash=compute_snapshot_hash(snapshot),
        ),
    )


def _prompt_accepted_draft(
    session_id: str,
    event_id: str,
    *,
    text: str,
    message_id: str,
) -> EventDraft[PromptAcceptedData]:
    """``py.prompt.accepted`` draft carrying one user message row."""
    return EventDraft[PromptAcceptedData](
        id=event_id,
        type="py.prompt.accepted",
        version=1,
        session_id=session_id,
        created_at=_CREATED_AT,
        data=PromptAcceptedData(
            request_id="req_pure",
            run_id="run_pure",
            message=UserMessage(id=message_id, type="user", created_at=_CREATED_AT, metadata={}, text=text),
        ),
        metadata={"run_id": "run_pure"},
    )


async def test_pure_user_session_pages_unchanged_through_the_union_face(tmp_path: Path) -> None:
    """①: the R1 legacy shape (a pure-User session) pages through the union
    face with the exact pre-union behavior: types, order, cursors, has_more.

    Pure-user rows come from direct store commits of prompt-accepted drafts
    (the test_eventlog idiom): the R1 command surface busy-gates a second
    prompt until the first run terminates, and a provider-less run never
    does -- the store face is the only way to build this legacy shape."""
    db_path = tmp_path / "union.db"
    _require_success(_upgrade_head(db_path))
    session_id = "ses_pure"
    async with open_database(db_path, mode="create") as database:
        store = SessionStore(database.engine, projector=SessionProjector())
        await store.commit(session_id, EventBatch("batch_c", (_session_created_draft(session_id, "evt_c"),)), 0)
        for index, text in enumerate(("note 1", "note 2", "note 3")):
            await store.commit(
                session_id,
                EventBatch(
                    f"batch_p{index}",
                    (_prompt_accepted_draft(session_id, f"evt_p{index}", text=text, message_id=f"msg_u{index}"),),
                ),
                1 + index,
            )
        repos = ReadRepositories(database.engine)

        page = await repos.messages(session_id, limit=10)
        assert [type(message) for message in page.items] == [UserMessage, UserMessage, UserMessage]
        assert _user_texts(page) == ["note 1", "note 2", "note 3"]
        assert page.has_more is False
        assert page.next_after_message_id == page.items[-1].id

        head = await repos.messages(session_id, limit=2)
        assert _user_texts(head) == ["note 1", "note 2"]
        assert head.has_more is True
        assert head.next_after_message_id == head.items[-1].id
        tail = await repos.messages(session_id, after_message_id=head.next_after_message_id, limit=2)
        assert _user_texts(tail) == ["note 3"]
        assert tail.has_more is False


async def test_mixed_projection_types_page_in_projection_order(tmp_path: Path) -> None:
    """②: two real full-stack runs project user/assistant/idle interleaved;
    the same MessagePage rules page them in projection order."""
    db_path = tmp_path / "union.db"
    _require_success(_upgrade_head(db_path))
    provider = _StreamProvider([_text_stream("hello there"), _text_stream("bye now")])
    async with App(db_path, working_dir="C:/work", provider=provider, tools=(), settings=_settings()) as app:
        session = await app.service.create(ConfigSnapshot.create())
        session_id = session.session_id
        first_run = await app.service.prompt(session_id, "one", "req_1")
        assert (await _await_terminal(app.service, first_run.run_id)).status.value == "succeeded"
        second_run = await app.service.prompt(session_id, "two", "req_2")
        assert (await _await_terminal(app.service, second_run.run_id)).status.value == "succeeded"

        page = await app.service.messages(session_id, limit=10)
        assert [message.type for message in page.items] == [
            "user",
            "assistant",
            "idle",
            "user",
            "assistant",
            "idle",
        ]
        user_one, assistant_one, idle_one = page.items[0], page.items[1], page.items[2]
        assert isinstance(user_one, UserMessage) and user_one.text == "one"
        assert isinstance(assistant_one, AssistantMessage) and assistant_one.status == "completed"
        assert assistant_one.parts[0].type == "text"
        assert isinstance(idle_one, IdleMessage) and idle_one.outcome == "succeeded"

        head = await app.service.messages(session_id, limit=2)
        assert [message.type for message in head.items] == ["user", "assistant"]
        assert head.has_more is True
        assert head.next_after_message_id == assistant_one.id
        tail = await app.service.messages(session_id, after_message_id=head.next_after_message_id, limit=10)
        assert [message.type for message in tail.items] == ["idle", "user", "assistant", "idle"]
        assert tail.has_more is False


async def test_cursor_ownership_holds_for_the_union_face(tmp_path: Path) -> None:
    """③: an assistant-row cursor continues the page at the following idle
    row; a cursor belonging to another session is still InvalidCursor."""
    db_path = tmp_path / "union.db"
    _require_success(_upgrade_head(db_path))
    provider = _StreamProvider([_text_stream("hello")])
    async with App(db_path, working_dir="C:/work", provider=provider, tools=(), settings=_settings()) as app:
        session = await app.service.create(ConfigSnapshot.create())
        session_id = session.session_id
        run = await app.service.prompt(session_id, "one", "req_1")
        assert (await _await_terminal(app.service, run.run_id)).status.value == "succeeded"

        page = await app.service.messages(session_id, limit=2)  # [user, assistant]
        assert [message.type for message in page.items] == ["user", "assistant"]
        cursor = page.items[1].id  # an assistant row id drives the next page
        rest = await app.service.messages(session_id, after_message_id=cursor, limit=10)
        assert [message.type for message in rest.items] == ["idle"]

        other = await app.service.create(ConfigSnapshot.create())
        with pytest.raises(InvalidCursorError):
            await app.service.messages(other.session_id, after_message_id=page.items[0].id, limit=10)


async def test_corrupted_rows_fail_the_whole_page(tmp_path: Path) -> None:
    """④: a legal-JSON-but-bad-shape assistant payload and an unknown type tag
    each fail the whole page (StorageCorruptError, no partial items leaked)."""
    db_path = tmp_path / "union.db"
    _require_success(_upgrade_head(db_path))
    provider = _StreamProvider([_text_stream("hello")])
    async with App(db_path, working_dir="C:/work", provider=provider, tools=(), settings=_settings()) as app:
        session = await app.service.create(ConfigSnapshot.create())
        session_id = session.session_id
        run = await app.service.prompt(session_id, "one", "req_1")
        assert (await _await_terminal(app.service, run.run_id)).status.value == "succeeded"
        page = await app.service.messages(session_id, limit=10)
        assistant = next(message for message in page.items if isinstance(message, AssistantMessage))
        assistant_id = assistant.id

    # quiesced (durable terminal + app closed): the S7 discipline for direct writes
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT data FROM messages WHERE session_id = ? AND message_id = ?", (session_id, assistant_id)
        ).fetchone()
    assert row is not None
    original = row[0]

    # variant 1: legal JSON, wrong model shape (required fields missing)
    bad_shape = json.dumps({"type": "assistant", "id": assistant_id, "created_at": "not-a-timestamp"})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE messages SET data = ? WHERE session_id = ? AND message_id = ?",
            (bad_shape, session_id, assistant_id),
        )
    async with App(db_path, working_dir="C:/work") as reader:
        with pytest.raises(StorageCorruptError):
            await reader.service.messages(session_id, limit=10)

    # variant 2: an unknown type tag on the same row
    payload = json.loads(original)
    payload["type"] = "chatty"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE messages SET data = ? WHERE session_id = ? AND message_id = ?",
            (json.dumps(payload), session_id, assistant_id),
        )
    async with App(db_path, working_dir="C:/work") as reader:
        with pytest.raises(StorageCorruptError):
            await reader.service.messages(session_id, limit=10)
