"""M6-compaction engine core (CORE-CONTRACTS §2.4 + §3.1 L176; P2-S8 slice one, T-C1-060).

The §2.4 L126 execution order, engine-core half:

1. **In-lock capture** -- read the settled history and fix the boundary under
   the session lock: the covered interval's boundary seq (``covered_seq``),
   the retained tail's start (``retained_from``), the covered message count,
   the covered history's hash, and the durable watermark (``expected_seq``,
   the CAS basis).
2. **Unlocked summary** -- generate the replacement body through the injected
   Protocol OUTSIDE the lock (it makes a model call; §2.4 L126 "释放锁后生成
   摘要"), getting back text + model + usage.
3. **Short commit lock + CAS** -- re-enter the lock only for the commit:
   ``SessionStore.commit`` (the unique §3.2 interface) with the captured
   ``expected_seq``. A moved boundary means a stale watermark, the store's CAS
   answers ``ConflictError``, and the summary is DISCARDED: zero commits, zero
   replacement, zero partial writes (§3.2 is all-or-nothing), while the extra
   model call's usage still rides the outcome (§2.4 L132). On success exactly
   one durable ``py.context.compacted`` event with the seven §2.4 fields is
   appended -- the original events and tool results are never rewritten
   (§2.4 L126/L133: the engine only records the replacement; the effective
   view lives in the ContextBuilder).

Slice boundary (p2-slices §2.4 L122, T-C1-060): the ContextBuilder
replacement generation, the retained-history wiring, the full
failure/cancel/crash matrix and the projector/fold faces land in slice two.
This module is deliberately wiring-free: constructor-injected seams only, no
bus, no service imports, and no tokenization anywhere (the threshold is a
settled-message count proxy; a token-face refinement is a separate
adjudication, not pre-written here).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

from pydantic import JsonValue

from opencode.contracts.events import (
    EVENT_VERSION,
    PY_CONTEXT_COMPACTED,
    ContextCompactedData,
    DurableEvent,
    EventDraft,
    canonical_json,
)
from opencode.contracts.llm import ModelRef, Usage
from opencode.contracts.messages import SessionMessage
from opencode.storage import ConflictError, EventBatch

#: §2.4 L128: compaction triggers on an explicit threshold. The concrete value
#: was the contract's deferred face ("阈值具体值=契约待定面,实现切片定", CORE-CONTRACTS
#: §2.4 L128) and is pinned here by T-C1-060: a settled-message count -- a
#: context-pressure proxy that keeps the zero-tokenizer red line (any
#: token-face refinement is a separate adjudication, not pre-written here).
COMPACT_TRIGGER_MESSAGE_COUNT = 40

#: §2.4 L119/L128: the retained tail -- the most recent settled messages the
#: replacement keeps verbatim; ``retained_from`` (the retained history's start
#: boundary) is computed from this tail. Pinned by T-C1-060 under the same
#: slice ruling as the trigger value.
COMPACT_RETAINED_TAIL_MESSAGES = 8


@dataclass(frozen=True, slots=True)
class SettledMessage:
    """One settled session message plus the seq of its settling durable event
    (§2.4 L118): the linkage the boundary math (covered_seq/retained_from)
    runs over. Views must carry seqs >= 1 in strictly ascending history order.
    """

    message: SessionMessage
    seq: int


@dataclass(frozen=True, slots=True)
class SettledHistoryView:
    """The provider's answer for one session: the settled messages (history
    order) plus the current durable watermark (§3.2 H) that the commit-phase
    CAS compares against.
    """

    messages: tuple[SettledMessage, ...]
    log_high: int


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """What the summary generator returns (§2.4 L120/L122-123): the
    replacement body, the generating model, and the extra model call's usage."""

    text: str
    model: ModelRef
    usage: Usage


@dataclass(frozen=True, slots=True)
class CompactionCapture:
    """The §2.4 L118-124 in-lock capture: the settled boundary seq, the
    retained start boundary, the covered message count, the covered history's
    hash, and the watermark the commit-phase expected_seq/CAS check uses."""

    covered_seq: int
    retained_from: int
    message_count: int
    history_hash: str
    expected_seq: int


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    """One engine run's result. ``usage`` is set whenever a summary call
    happened -- including the discarded path, where §2.4 L132 requires the
    extra call's usage to be recorded even though nothing was committed."""

    status: Literal["compacted", "below_threshold", "discarded"]
    capture: CompactionCapture | None
    event: DurableEvent[ContextCompactedData] | None
    usage: Usage | None


class SettledHistorySource(Protocol):
    """Read seam for the settled history with event-seq linkage (§2.4 L118/L126).

    "Settled" = complete messages only (a user message once accepted, an
    assistant message in a terminal state, an idle row once its run finished);
    which rows count is the provider's contract -- the real read path is the
    slice-two ContextBuilder wiring, tests inject fakes. ``log_high`` is the
    current durable watermark H (§3.2) read in the same lock as the messages,
    so a legal capture can never carry a stale CAS basis.
    """

    async def settled_history(self, session_id: str) -> SettledHistoryView: ...


class SummaryGenerator(Protocol):
    """The injected summary face (§2.4 L126): runs OUTSIDE the engine locks
    (it makes a model call) and returns the replacement body, the generating
    model and the extra call's usage. The real LLM adapter wiring is a
    slice-two face; this seam keeps the engine core dependency-free.
    """

    async def summarize(self, covered: Sequence[SettledMessage]) -> SummaryResult: ...


class CompactionStore(Protocol):
    """The store face the engine needs: the unique commit interface (§3.2).

    ``SessionStore`` satisfies this structurally. The Protocol exists so the
    engine core's fast faces are testable against the §3.2 expected_seq
    semantics without a migrated database (the CAS-critical tests run over
    the real store), the store.py ``Projector``-seam precedent.
    """

    async def commit(self, session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]: ...


class CompactionIds(Protocol):
    """The id faces the engine needs; service.IdFactory/AscendingIds satisfy
    this structurally (a local protocol avoids a service<->compaction import
    cycle once the slice-two wiring lands).
    """

    def event_id(self) -> str: ...

    def batch_id(self) -> str: ...


def _history_hash(covered: Sequence[SettledMessage]) -> str:
    """The in-lock settled-history hash (§2.4 L126): sha256 over the covered
    interval's canonical JSON -- the identity of what the summary replaces.

    Not a wire field (the event carries ``summary_hash``); it rides the
    capture record for audit and boundary comparison.
    """
    payload: list[JsonValue] = [{"seq": item.seq, "message": item.message.model_dump(mode="json")} for item in covered]
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _summary_hash(text: str) -> str:
    """The summary identity digest (§2.4 L121): sha256 over the UTF-8 body.

    The wire pins ``summary_hash`` only as a non-empty string; the engine
    derives the 64-hex sha256 deterministically so commit verification and
    audit comparison are reproducible.
    """
    return sha256(text.encode("utf-8")).hexdigest()


def _validate_view(view: SettledHistoryView) -> None:
    """Fail-closed provider-seam defense: the boundary math needs settling
    seqs >= 1 in strictly ascending history order and a watermark at or above
    the last settle. A violating view is rejected before anything is read off
    it -- no capture, no summary, no commit."""
    previous = 0
    for item in view.messages:
        if isinstance(item.seq, bool) or not isinstance(item.seq, int) or item.seq < 1:
            raise ValueError(f"settled message seq must be an integer >= 1, got {item.seq!r}")
        if item.seq <= previous:
            raise ValueError("settled history must be ordered by strictly ascending settling seq")
        previous = item.seq
    if isinstance(view.log_high, bool) or not isinstance(view.log_high, int) or view.log_high < 0:
        raise ValueError(f"log_high must be an integer >= 0, got {view.log_high!r}")
    if view.messages and view.log_high < view.messages[-1].seq:
        raise ValueError("log_high cannot be below the last settled message's seq")


class CompactionEngine:
    """The §2.4 L126 execution order over injected seams: threshold gate ->
    in-lock capture -> unlocked summary -> short commit lock with the captured
    expected_seq (CAS). The engine is stateless per call: when to invoke it is
    the caller's trigger policy (slice two), and repeated invocations simply
    capture-and-commit again -- the original log is only ever appended to.
    """

    def __init__(
        self,
        *,
        store: CompactionStore,
        history: SettledHistorySource,
        summarizer: SummaryGenerator,
        ids: CompactionIds,
        clock: Callable[[], datetime],
        lock: Callable[[str], asyncio.Lock],
    ) -> None:
        self._store = store
        self._history = history
        self._summarizer = summarizer
        self._ids = ids
        self._clock = clock
        self._lock = lock

    async def compact(self, session_id: str) -> CompactionOutcome:
        """Run one threshold-checked compaction pass for the session.

        ``below_threshold`` -- the §2.4 L128 gate held (no summary call, no
        commit). ``compacted`` -- exactly one ``py.context.compacted`` durable
        event committed. ``discarded`` -- §2.4 L126: the settled boundary
        moved under the summary call (the store CAS answered ConflictError),
        so the summary was dropped with zero commits while its usage still
        rides the outcome. Any other failure propagates: nothing is swallowed,
        nothing is half-committed (§2.4 L132; the full failure/cancel/crash
        matrix is the slice-two face).
        """
        if not isinstance(session_id, str) or not session_id or not session_id.startswith("ses_"):
            raise ValueError("session_id must be a non-empty string matching ^ses_")

        # -- §2.4 L126 phase 1: capture under the session lock ----------------
        capture, covered = await self._capture(session_id)
        if capture is None:
            return CompactionOutcome(status="below_threshold", capture=None, event=None, usage=None)

        # -- §2.4 L126 phase 2: summary generation OUTSIDE the lock -----------
        summary = await self._summarizer.summarize(covered)

        # -- §2.4 L126 phase 3: short commit lock + expected_seq/CAS (§3.2) ---
        async with self._lock(session_id):
            draft = EventDraft[ContextCompactedData](
                id=self._ids.event_id(),
                type=PY_CONTEXT_COMPACTED,
                version=EVENT_VERSION,
                session_id=session_id,
                created_at=self._clock(),
                data=ContextCompactedData(
                    covered_seq=capture.covered_seq,
                    retained_from=capture.retained_from,
                    summary_text=summary.text,
                    summary_hash=_summary_hash(summary.text),
                    model=summary.model,
                    usage=summary.usage,
                    message_count=capture.message_count,
                ),
            )
            try:
                committed = await self._store.commit(
                    session_id, EventBatch(self._ids.batch_id(), (draft,)), capture.expected_seq
                )
            except ConflictError:
                # The settled boundary moved under the summary call; the
                # §3.2 all-or-nothing CAS left zero rows written. Discard the
                # summary (zero commit, zero replacement, zero partial
                # writes); the extra model call's usage is still recorded on
                # the outcome (§2.4 L132).
                return CompactionOutcome(status="discarded", capture=capture, event=None, usage=summary.usage)
            return CompactionOutcome(status="compacted", capture=capture, event=committed[0], usage=summary.usage)

    async def _capture(self, session_id: str) -> tuple[CompactionCapture | None, tuple[SettledMessage, ...]]:
        """§2.4 L126 phase 1: fix the settled boundary under the session lock.

        Returns ``(None, ())`` when the §2.4 L128 threshold gate holds; a
        non-empty covered interval plus its capture otherwise.
        """
        async with self._lock(session_id):
            view = await self._history.settled_history(session_id)
            _validate_view(view)
            if len(view.messages) < COMPACT_TRIGGER_MESSAGE_COUNT:
                return None, ()
            covered = view.messages[:-COMPACT_RETAINED_TAIL_MESSAGES]
            retained = view.messages[-COMPACT_RETAINED_TAIL_MESSAGES:]
            if not covered or not retained:
                # Unreachable with the pinned 40/8 constants (40 - 8 = 32
                # covered, 8 retained); the guard documents the invariant for
                # any future re-tuning of the two constants.
                raise ValueError(
                    "the retained tail must leave a non-empty covered interval "
                    "(COMPACT_RETAINED_TAIL_MESSAGES < COMPACT_TRIGGER_MESSAGE_COUNT is required)"
                )
            capture = CompactionCapture(
                covered_seq=covered[-1].seq,
                retained_from=retained[0].seq,
                message_count=len(covered),
                history_hash=_history_hash(covered),
                expected_seq=view.log_high,
            )
            return capture, covered
