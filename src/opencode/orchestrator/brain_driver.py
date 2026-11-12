"""The main-brain driver seam (P3-S4, T-C1-079; PHASE3 §2 main brain, §13 C#2).

v1 is the in-process form: one resident brain session over the composition's
own :class:`~opencode.session.service.SessionService` (a same-process object
call -- the message -> main brain -> submit -> poll -> reinject -> summarize
default path, PHASE3 path ②), with the event window taken through
:class:`~opencode.bus.eventlog.EventLog.follow` and the terminal verdict
through the ``run_status`` poll loop (the ``cli.py`` ``_wait_terminal``
shape: no internal deadline -- the caller owns timeouts).

The §13 C#2 boundary: the sdk's synchronous urllib transport never enters
the asyncio event loop. A v2 ``OpencodeClient`` form (the same driver face
over the HTTP API) stays an interface comment until that client exists --
nothing here may block the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from opencode.bus.eventlog import EventCursor, EventLog, LogItem, Synced
from opencode.contracts import DurableEvent
from opencode.contracts.events import ConfigSnapshot, ConfigSnapshotV2, ConfigSnapshotV3
from opencode.contracts.messages import JsonObject
from opencode.session.service import RunStatus, RunStatusValue, SessionService

#: The §7 poll cadence for the terminal verdict (the EventLog clamps its own
#: follow poll to <= 1s; this is the run_status loop's).
DEFAULT_BRAIN_POLL_INTERVAL_S = 0.05


@dataclass(frozen=True, slots=True)
class BrainTurn:
    """One completed brain turn.

    ``events`` is the turn's durable event window: every event with
    ``seq > H`` where H is the high watermark captured by the follow entry
    immediately before the prompt (backlog replayed before the prompt is
    excluded by construction). The window is closed deterministically: the
    runs read model commits in the same transaction as the terminal events,
    so a bounded page sweep after the terminal verdict cannot miss them.
    """

    session_id: str
    run_id: str
    request_id: str
    status: RunStatusValue
    error: str | None
    events: tuple[DurableEvent[Any], ...]


class BrainDriver:
    """The resident main-brain driver over the composition's own service.

    ``ensure_session`` creates (and caches) one resident brain session;
    ``send`` drives one prompt turn to its terminal and returns the turn
    (identity, terminal status, the turn's durable events). The brain's own
    tool calls (``workflow__run`` / ``workflow__status``) execute inside the
    session's runner exactly like any other tool round -- the driver adds no
    parallel channel and relays nothing (job events ride the control
    channel; the brain reads the job projection through its tools).
    """

    def __init__(
        self,
        *,
        service: SessionService,
        log: EventLog,
        poll_interval_s: float = DEFAULT_BRAIN_POLL_INTERVAL_S,
    ) -> None:
        if isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, int | float) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be a positive number of seconds (bools are rejected)")
        self._service = service
        self._log = log
        self._poll_interval_s = float(poll_interval_s)
        self._session_id: str | None = None

    async def ensure_session(
        self,
        config_snapshot: ConfigSnapshot | ConfigSnapshotV2 | ConfigSnapshotV3 | JsonObject | None = None,
    ) -> str:
        """Create the resident brain session once (cached; every call returns it)."""
        if self._session_id is None:
            snapshot = config_snapshot if config_snapshot is not None else ConfigSnapshot.create()
            reference = await self._service.create(snapshot)
            self._session_id = reference.session_id
        return self._session_id

    async def send(self, session_id: str, text: str, request_id: str | None = None) -> BrainTurn:
        """Drive one brain turn: prompt, wait for the run terminal, collect the window.

        The follow context is entered BEFORE the prompt (the four-step
        preamble registers the wakeup first and captures H -- the prompt's
        events and everything after land strictly above H); a concurrent
        drainer collects the live stream while the ``run_status`` poll loop
        (no internal deadline; the caller owns timeouts) waits for the
        terminal, and a bounded page sweep from H afterwards closes the
        window deterministically.
        """
        resolved_request_id = request_id if request_id is not None else f"brain_{uuid4().hex}"
        collected: dict[int, DurableEvent[Any]] = {}
        async with self._log.follow(session_id) as stream:
            synced_seq = 0
            async for item in stream:
                if isinstance(item, Synced):
                    # The catch-up marker: everything above this seq is this
                    # turn's window (the backlog below it belongs to history).
                    synced_seq = item.cursor.seq
                    break
            reference = await self._service.prompt(session_id, text, resolved_request_id)
            drainer = asyncio.create_task(self._drain(stream, synced_seq, collected))
            try:
                status = await self._wait_terminal(reference.run_id)
            finally:
                drainer.cancel()
                with suppress(asyncio.CancelledError):
                    await drainer
            # Deterministic close: the terminal verdict comes from the runs
            # read model, which commits in the same transaction as the final
            # events -- sweep the pages from the Synced cursor so nothing the
            # drainer had not yet yielded is lost.
            cursor = EventCursor(session_id=session_id, seq=synced_seq)
            while True:
                page = await self._log.read(session_id, cursor)
                for item in page.items:
                    collected.setdefault(item.seq, item)
                cursor = page.next_cursor
                if not page.has_more:
                    break
        return BrainTurn(
            session_id=session_id,
            run_id=reference.run_id,
            request_id=reference.request_id,
            status=status.status,
            error=status.error,
            events=tuple(collected[key] for key in sorted(collected)),
        )

    async def _drain(self, stream: AsyncIterator[LogItem], synced_seq: int, sink: dict[int, DurableEvent[Any]]) -> None:
        """Collect the live follow stream into ``sink`` (seq > the Synced cursor)."""
        async for item in stream:
            if isinstance(item, Synced):
                continue
            if item.seq > synced_seq:
                sink.setdefault(item.seq, item)

    async def _wait_terminal(self, run_id: str) -> RunStatus:
        """Poll ``run_status`` until a terminal status (the cli.py loop shape)."""
        while True:
            status = await self._service.run_status(run_id)
            if status.status.is_terminal:
                return status
            await asyncio.sleep(self._poll_interval_s)
