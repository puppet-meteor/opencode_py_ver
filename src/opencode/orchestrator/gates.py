"""The human-gate reply chain's pure hand-off (P3-S5a, PHASE3 §4; T-C1-080).

The gate chain's *authority* faces live elsewhere: the ``py.job.*`` commits
and the fold in ``orchestrator/jobs.py`` (the reply face, the gate
registry, the re-drive), and the interrupting ``human_gate`` node body in
``orchestrator/engine.py``. This module is the deliberately small, pure
bridge between them:

* the :class:`GateBroker` holds at most ONE pending resolution per job --
  the reply face stores it *after* the gateway validated the gate (the job
  is gated, the gate id matches, the expiry has not passed), and the node
  body pops it on the resume drive, so the reply travels from the HTTP
  face to the graph resumption without any new wire contract, event type
  or card kind (CORE-CONTRACTS §5.4 S5a: the existing ``py.job.gated``
  event is the gate card, with the conversation ref riding envelope
  metadata);
* :data:`GATE_EXPIRY_S` is the 300 s expiry precedent (§5.2 m-3, the
  permission-request face) reused verbatim.

No new ``py.job.*`` event type exists in this slice: approve/revise resume
through the existing ``py.job.started`` (``gated -> running``), reject
through ``py.job.finished(failed)`` with ``PermissionDenied`` naming the
gate. The broker is in-memory by design -- a restart loses a
not-yet-consumed resolution, and recovery re-mints a fresh gate through
the started -> gated cycle (disclosed, CORE-CONTRACTS §5.4 S5a).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opencode.contracts.messages import JsonObject

#: The gate expiry horizon (seconds): the §5.2 300 s precedent (m-3) reused.
GATE_EXPIRY_S = 300.0


@dataclass(frozen=True, slots=True)
class GateResolution:
    """One validated human reply handed to the engine's ``human_gate`` body.

    ``reply`` never carries ``reject`` here: a reject resolves the job
    terminally (``finished(failed)`` with ``PermissionDenied`` naming the
    gate) inside the reply face and never re-enters the graph. ``params``
    rides a revise reply -- the revised parameters land in the gate node's
    result for downstream nodes to read, while the job's ``params_hash``
    binding stays the submit-time value (CORE-CONTRACTS §5.4 S5a: revise
    continues the run, it never re-binds the spec chain).
    """

    job_id: str
    gate_id: str
    reply: Literal["approve", "revise"]
    params: JsonObject | None = None

    def node_result(self) -> JsonObject:
        """The gate node's result value: the reply, plus the revised params on revise."""
        result: JsonObject = {"reply": self.reply}
        if self.reply == "revise" and self.params is not None:
            result["params"] = self.params
        return result


class GateBroker:
    """One pending resolution per job: ``resolve`` (the reply face), ``take`` (the node body)."""

    def __init__(self) -> None:
        self._resolutions: dict[str, GateResolution] = {}

    def resolve(self, resolution: GateResolution) -> None:
        """Record the reply face's validated resolution (one slot per job)."""
        self._resolutions[resolution.job_id] = resolution

    def take(self, job_id: str) -> GateResolution | None:
        """Pop the pending resolution for the job (None = no reply to apply)."""
        return self._resolutions.pop(job_id, None)

    def pending(self, job_id: str) -> GateResolution | None:
        """The pending resolution without consuming it (the pure read face)."""
        return self._resolutions.get(job_id)
