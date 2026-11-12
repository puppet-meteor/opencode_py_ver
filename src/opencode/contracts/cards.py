"""Job event card protocol (frontend rendering contract; PHASE3-ORCHESTRATOR §5.4/§4).

The wire contract for rendering ``py.job.*`` events as live cards on the
``GET /jobs/{id}/events`` SSE stream (卡片直播) and for correlating them with
the session stream's trace tree (trace 树). This module is the contract face
only -- models + the closed kind mapping; the event -> card builder lands
with the gateway card chain (S5a) (it needs the folded job status, which is
gateway state, not wire shape).

Design notes (T-C1-076, progress ①):

- one card kind per ``py.job.*`` event type, closed by the ``JOB_CARD_KINDS``
  mapping (every registered jobs-family type must have exactly one kind --
  the contract test pins the bijection);
- every card carries the job id and the event's ``trace`` block so the
  frontend can join ``/session/{id}/events`` and ``/jobs/{id}/events`` on
  ``trace_id`` (PHASE3 §4 M-6 double-stream envelope);
- ``status`` is the job's folded status AFTER the event (the fold's verdict,
  supplied by the builder), so a card alone answers "what state is the job
  in now";
- ``detail`` is optional free JSON (event-specific rendering payload: result
  bodies, stalled reasons, gate deadlines);
- ``title`` is the human-renderable heading text.

TS mapping: none -- this is a Python-side orchestrator contract (PHASE3
§5.4); no TS file is claimed or referenced (zero new TS anchors per the
task-book discipline).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from opencode.contracts.events import (
    PY_JOB_CREATED,
    PY_JOB_FINISHED,
    PY_JOB_GATED,
    PY_JOB_REDISPATCHED,
    PY_JOB_SPEC_BOUND,
    PY_JOB_STALLED,
    PY_JOB_STARTED,
    JobTrace,
)

#: The card kind vocabulary: one per jobs-family event type (closed bijection).
JobCardKind = Literal[
    "job_created",
    "job_spec_bound",
    "job_started",
    "job_stalled",
    "job_redispatched",
    "job_gated",
    "job_finished",
]

#: The closed ``py.job.* -> card kind`` mapping (the rendering contract).
JOB_CARD_KINDS: dict[str, JobCardKind] = {
    PY_JOB_CREATED: "job_created",
    PY_JOB_SPEC_BOUND: "job_spec_bound",
    PY_JOB_STARTED: "job_started",
    PY_JOB_STALLED: "job_stalled",
    PY_JOB_REDISPATCHED: "job_redispatched",
    PY_JOB_GATED: "job_gated",
    PY_JOB_FINISHED: "job_finished",
}

#: The §5.4 status closed set (the card's ``status`` domain).
JobStatusLiteral = Literal["created", "running", "stalled", "gated", "done", "failed", "cancelled"]


class JobCard(BaseModel):
    """One renderable job card: the frontend wire shape for a ``py.job.*`` event.

    ``kind`` selects the renderer; ``status`` is the folded job status after
    the event; ``detail`` carries the event-specific payload (``None`` when
    the kind renders from the fixed fields alone); ``title`` is the heading
    text. Unknown fields are rejected (the §1 repo-wide closed-shape rule).
    """

    model_config = ConfigDict(extra="forbid")

    kind: JobCardKind
    job_id: str = Field(min_length=1)
    status: JobStatusLiteral
    title: str = Field(min_length=1)
    trace: JobTrace
    detail: object | None = None
