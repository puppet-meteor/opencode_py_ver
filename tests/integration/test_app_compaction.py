"""M6-compaction slice-three composition-root tests (CORE-CONTRACTS §2.4; T-C1-063).

The App-composition pin of the real summary face (T-B-072 recommendation 1):
the half-wired state -- a service seam complete and injectable but only ever
driven by test stubs -- closes here. Three faces:

* 二态 (R1/R2): a provider composition injects a real ``ProviderSummarizer``
  into ``SessionService._compaction_summarizer`` (the engine then builds over
  the real faces); a provider-less composition keeps ``None`` -- the pre-M6
  dormant face, strictly unchanged (R1 pinned).
* 端到端: under the App composition the post-settlement trigger runs the REAL
  adapter path -- the provider's second request IS the summary request, and
  exactly one durable ``py.context.compacted`` event lands in the database
  with the §2.4 fields over the aggregated stream.

The wiring matrix below the composition root (trigger/boundary/CAS/failure/
cancel/crash) is the slice-two face, pinned by test_session_compaction.py;
this file pins only what the composition adds: the injected adapter instance,
its request shape, and the event it produces end to end.

Test-only private access note (the test_session_compaction precedent):
``service._compaction_summarizer`` / ``service._compaction()`` /
``service._compaction_errors`` / ``service._tasks`` are read to pin the
injection face, the dormant face and to await the scheduled run task
deterministically (the task reference is read synchronously before the loop
can start it).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode.app import App
from opencode.contracts import (
    PY_PROMPT_ACCEPTED,
    PY_SESSION_CREATED,
    ConfigSnapshot,
    PromptAcceptedData,
    SessionCreatedData,
    UserMessage,
    compute_snapshot_hash,
)
from opencode.contracts.events import PY_RUN_FINISHED, EventDraft, RunFinishedData
from opencode.contracts.llm import (
    FinishChunk,
    FinishReason,
    GenerationRequest,
    LLMTextPart,
    ModelRef,
    ProviderChunk,
    TextDeltaChunk,
    Usage,
    UsageChunk,
)
from opencode.session import SessionProjector
from opencode.session.context import RuntimeSettings
from opencode.session.llm_summarizer import ProviderSummarizer
from opencode.storage import EventBatch, SessionStore
from opencode.storage.repos import ReadRepositories

REPO_ROOT = Path(__file__).resolve().parents[2]

_SESSION_ID = "ses_00000000000000000000000000a1"
_T0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
_SNAPSHOT = ConfigSnapshot.create()
_MODEL = ModelRef(provider="maas", model="glm-4.6")


# ---------------------------------------------------------------------------
# harness (mirrors tests/integration/test_session_compaction.py)
# ---------------------------------------------------------------------------


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


def _migrated(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic failed\nstdout={result.stdout}\nstderr={result.stderr}"
    return db_path


def _settings() -> RuntimeSettings:
    return RuntimeSettings(model=_MODEL, context_limit=100_000, output_reserve=4096, timeout_s=30.0)


class _SeedIds:
    """Counter ids for store-side seeding (distinct from the service's factory)."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self) -> str:
        self._n += 1
        return f"{self._n:05d}"


def _finished_draft(session_id: str, run_id: str, event_id: str, at: datetime) -> EventDraft[RunFinishedData]:
    return EventDraft[RunFinishedData](
        id=event_id,
        type=PY_RUN_FINISHED,
        version=1,
        session_id=session_id,
        created_at=at,
        data=RunFinishedData(run_id=run_id, outcome="interrupted", error=None),
        metadata={"run_id": run_id},
    )


async def _seed_round(
    store: SessionStore, repos: ReadRepositories, ids: _SeedIds, prefix: str, *, text: str | None = None
) -> None:
    """One settled round through the REAL store: prompt.accepted + run.finished.

    The multi-accepted R1-baseline log shape (§0.2): a user message settles at
    ``py.prompt.accepted``, the idle row settles at ``py.run.finished``, and
    the active-run slot is released -- so two settled messages land per round.
    """
    high = await repos.log_high(_SESSION_ID)
    ordinal = ids._n + 1
    run_id = f"run_{prefix}_{ids._next()}"
    user = UserMessage(
        id=f"msg_{prefix}_{ids._next()}",
        type="user",
        created_at=_T0,
        metadata={},
        text=text if text is not None else f"seed turn {ordinal}",
    )
    prompt = EventDraft[PromptAcceptedData](
        id=f"evt_{prefix}p{ids._next()}",
        type=PY_PROMPT_ACCEPTED,
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=PromptAcceptedData(request_id=f"req_{prefix}_{ids._next()}", run_id=run_id, message=user),
        metadata={"run_id": run_id},
    )
    finished = _finished_draft(_SESSION_ID, run_id, f"evt_{prefix}f{ids._next()}", _T0)
    await store.commit(_SESSION_ID, EventBatch(f"batch_{prefix}_{ids._next()}", (prompt, finished)), high)


def _created_draft() -> EventDraft[SessionCreatedData]:
    return EventDraft[SessionCreatedData](
        id="evt_created_00000",
        type=PY_SESSION_CREATED,
        version=1,
        session_id=_SESSION_ID,
        created_at=_T0,
        data=SessionCreatedData(
            working_dir="C:/work",
            config_snapshot=_SNAPSHOT,
            snapshot_hash=compute_snapshot_hash(_SNAPSHOT),
        ),
    )


async def _seed(store: SessionStore, repos: ReadRepositories, rounds: int) -> None:
    ids = _SeedIds()
    await store.commit(_SESSION_ID, EventBatch("batch_seed_00000", (_created_draft(),)), 0)
    for ordinal in range(1, rounds + 1):
        await _seed_round(store, repos, ids, "seed", text=f"seed turn {ordinal}")


class _Chunks:
    """Chunk factory with auto-ascending stream indexes (the _Script precedent)."""

    def __init__(self) -> None:
        self._index = 0

    def _next(self) -> int:
        self._index += 1
        return self._index

    def text(self, part_id: str, delta: str) -> TextDeltaChunk:
        return TextDeltaChunk(
            type="text_delta", request_id="req_script", index=self._next(), part_id=part_id, delta=delta
        )

    def usage(self, *, input_tokens: int = 5, output_tokens: int = 7) -> UsageChunk:
        return UsageChunk(
            type="usage",
            request_id="req_script",
            index=self._next(),
            usage=Usage(input=input_tokens, output=output_tokens),
        )

    def finish(self) -> FinishChunk:
        return FinishChunk(type="finish", request_id="req_script", index=self._next(), reason=FinishReason.STOP)


class _ScriptedProvider:
    """Fake Provider (the test_httpapi_server._ScriptedProvider precedent).

    One scripted chunk stream per request; every request and close recorded.
    """

    def __init__(self, scripts: Sequence[Sequence[ProviderChunk]] | None = None) -> None:
        self._scripts = [list(script) for script in scripts] if scripts is not None else []
        self.requests: list[GenerationRequest] = []
        self.close_count = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]:
        self.requests.append(request)
        script = self._scripts.pop(0) if self._scripts else []
        for chunk in script:
            yield chunk

    async def close(self) -> None:
        self.close_count += 1


async def _compacted_rows(engine: AsyncEngine) -> list[tuple[str, dict[str, Any]]]:
    """Every durable ``py.context.compacted`` row (id, data) in commit order."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa_text(
                    "SELECT id, data FROM events WHERE session_id = :sid AND type = 'py.context.compacted' ORDER BY seq"
                ),
                {"sid": _SESSION_ID},
            )
        ).all()
    return [(row.id, json.loads(row.data) if isinstance(row.data, str) else row.data) for row in rows]


# ---------------------------------------------------------------------------
# 二态: R2 注入真实适配器 / R1 休眠面钉死
# ---------------------------------------------------------------------------


async def test_provider_composition_injects_the_real_summarizer(tmp_path: Path) -> None:
    """R2: App(provider=...) wires a real ``ProviderSummarizer`` into the service seam."""
    db_path = _migrated(tmp_path, "app_compaction_r2.db")
    provider = _ScriptedProvider([])
    async with App(db_path, mode="create", working_dir="C:/work", provider=provider, settings=_settings()) as app:
        await app.recovery_gate.wait()
        assert isinstance(app.service._compaction_summarizer, ProviderSummarizer)
        assert app.service._compaction() is not None  # the engine builds over the real faces
    assert provider.close_count == 1  # §7 L337: the App-owned provider close is unchanged


async def test_providerless_composition_keeps_the_dormant_face(tmp_path: Path) -> None:
    """R1: App() without a provider pins the pre-M6 dormant face (no summarizer, no engine)."""
    db_path = _migrated(tmp_path, "app_compaction_r1.db")
    async with App(db_path, mode="create", working_dir="C:/work") as app:
        await app.recovery_gate.wait()
        assert app.service._compaction_summarizer is None
        assert app.service._compaction() is None


# ---------------------------------------------------------------------------
# 端到端: App 组合下阈值触发 -> 真实适配器路径 -> py.context.compacted 落库
# ---------------------------------------------------------------------------


async def test_app_composition_compacts_through_the_real_adapter(tmp_path: Path) -> None:
    """19 seeded rounds (38 settled) + the run's own user/assistant/idle (41
    total) cross the threshold; the post-settlement trigger runs the engine
    whose summary call goes through the REAL adapter: the provider's second
    request is the summary request, and exactly one durable
    ``py.context.compacted`` event lands with the §2.4 fields."""
    db_path = _migrated(tmp_path, "app_compaction_e2e.db")
    run_chunks = _Chunks()
    summary_chunks = _Chunks()
    provider = _ScriptedProvider(
        [
            # request 1: the run's own generation (a plain terminal answer)
            [run_chunks.text("p1", "all done"), run_chunks.usage(), run_chunks.finish()],
            # request 2: the compaction summary stream (the real adapter's call)
            [
                summary_chunks.text("p1", "Seeded rounds summarized "),
                summary_chunks.text("p1", "by the real adapter."),
                summary_chunks.usage(input_tokens=31, output_tokens=17),
                summary_chunks.finish(),
            ],
        ]
    )
    settings = _settings()
    async with App(db_path, mode="create", working_dir="C:/work", provider=provider, settings=settings) as app:
        await app.recovery_gate.wait()
        engine = app.database.engine
        store = SessionStore(engine, projector=SessionProjector())
        repos = ReadRepositories(engine)
        await _seed(store, repos, rounds=19)
        ref = await app.service.prompt(_SESSION_ID, "final turn", "req_final")
        task = app.service._tasks[ref.run_id][1]
        await task
        assert app.service._compaction_errors == {}
        # exactly two provider requests: the run's generation, then the summary
        assert len(provider.requests) == 2
        run_request, summary_request = provider.requests
        assert run_request is not summary_request
        assert run_request.request_id != summary_request.request_id
        assert summary_request.model == settings.model
        assert summary_request.tools == []
        assert summary_request.request_id.startswith("compaction_")
        (message,) = summary_request.messages
        assert message.role == "user"
        (part,) = message.content
        assert isinstance(part, LLMTextPart)
        # the transcript is exactly the covered interval: through the 17th
        # seeded user message, never the retained tail (the 18th round onward,
        # the final turn, the run's own assistant text and idle row)
        assert "seed turn 1" in part.text
        assert "seed turn 17" in part.text
        assert "seed turn 18" not in part.text
        assert "final turn" not in part.text
        assert "all done" not in part.text
        rows = await _compacted_rows(engine)
        (row,) = rows  # exactly one durable compaction event
        event_id, data = row
        assert isinstance(event_id, str) and event_id
        assert data["summary_text"] == "Seeded rounds summarized by the real adapter."
        assert data["model"] == {"provider": "maas", "model": "glm-4.6"}
        assert data["usage"]["input"] == 31
        assert data["usage"]["output"] == 17
        # 41 settled (19 rounds x 2 + user + assistant + idle) - 8 retained
        assert data["message_count"] == 33
        assert data["covered_seq"] < data["retained_from"]
    assert provider.close_count == 1
