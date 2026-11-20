"""T-C3-002 evaluation harness: background-jobs capability, before/after levels.

A measurement face, not an assertion face: every probe records a metric
and passes, whether or not the background-jobs capability exists. Running
this module on pre-implementation code captures the BEFORE baseline; the
identical inputs on post-implementation code give the AFTER column. The
delta is the ticket's evidence against doc/GOAL.md's gates:

* M1 execution-rate gate -- wall-clock of one long ``bash__run`` call and
  of a sequential 3-call batch (how a model issues calls in a turn) at
  the STABLE seam (registry.invoke, identical inputs both sides):
  pre-implementation every call blocks its full duration; with the
  capability a call past the threshold returns a backgrounded marker and
  the wall collapses toward N x threshold;
* M2/M3 unattended-survival gate -- after a backgrounded run, does a
  ``background_job`` row exist and does ``replay_pending`` on a reopened
  database deliver the notification (pre-implementation: absent / 0%).
  The full survival matrix (crash mid-run, settled replay, orphan
  cleanup) is the contract test set shipped WITH the implementation;
* M4 token gate -- model-visible bytes vs the full payload on disk for
  the same outcome (the backgrounded marker must stay bounded; the full
  text lives in the tool-output directory, never inline).

Capability probes are pinned to the banked T-C3-002 seam contract (see
doc/plans/port-232-increments.md): module ``opencode.tools.background``
with ``replay_pending(database)``, the ``background_after_s`` field on
ToolLimits, and the ``background_job`` sqlite table. A post-implementation
``api-mismatch`` status means the harness and the implementation drifted:
fix the alignment, never the reading.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from sys import platform as _PLATFORM
from typing import Any, cast

from opencode.contracts.messages import ToolTextContent
from opencode.session.service import IdFactory
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin import BASH_MAX_OUTPUT_BYTES, BashAdapter
from tests.integration.test_tool_bash import (
    _FIXED_TIME,
    _FixedIds,
    _flood_command,
    _interpreter_or_skip,
    _RecordingSink,
)

_SESSION_ID = "ses_itbgeval"
_RUN_ID = "run_itbgeval"
_MESSAGE_ID = "msg_itbgeval"
_TOOL_CALL_ID = "call_itbgeval"
_THRESHOLD_S = 1.0
_SINGLE_SLEEP_S = 3
_BATCH_SLEEP_S = 2
_BATCH_SIZE = 3
_FULL_OUTPUT_MARKER = "full output saved to "


def _sleep_command(seconds: int, marker: str) -> str:
    if _PLATFORM == "win32":
        return f"Start-Sleep -Seconds {seconds}; Write-Output '{marker}'"
    return f"sleep {seconds} && echo '{marker}'"


def _limits_with_threshold() -> tuple[ToolLimits, bool]:
    """The threshold-injected limits when the field exists, else the default face.

    ``model_fields`` + ``**extra`` (not a literal keyword) so the probe is
    static-safe on pre-implementation code and exact on post-implementation
    code: mypy sees a dict-unpacked construction, not a phantom field.
    """
    extra: dict[str, Any] = {}
    if "background_after_s" in getattr(ToolLimits, "model_fields", {}):
        extra["background_after_s"] = _THRESHOLD_S
    return ToolLimits(**extra), bool(extra)


def _registry() -> ToolRegistry:
    registry = ToolRegistry(
        sink=cast("Any", _RecordingSink()),
        clock=lambda: _FIXED_TIME,
        ids=cast(IdFactory, _FixedIds()),
    )
    registry.register(BashAdapter())
    registry.snapshot(_SESSION_ID)
    return registry


def _context(working_dir: Path, limits: ToolLimits) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=limits,
    )


def _text(outcome: ToolOutcome) -> str:
    return "\n".join(part.text for part in outcome.content if isinstance(part, ToolTextContent))


def _module_available() -> bool:
    return importlib.util.find_spec("opencode.tools.background") is not None


def _run_calls(
    working_dir: Path,
    limits: ToolLimits,
    commands: Sequence[str],
) -> tuple[float, list[ToolOutcome]]:
    """Sequential await-per-call: the model-turn shape (M1's measured face)."""
    registry = _registry()
    started = time.monotonic()

    async def _all() -> list[ToolOutcome]:
        outcomes: list[ToolOutcome] = []
        for i, command in enumerate(commands):
            outcome = await registry.invoke(
                _context(working_dir, limits), "bash__run", {"command": command}, f"exec_itbgeval{i}"
            )
            outcomes.append(outcome)
        return outcomes

    outcomes = asyncio.run(_all())
    return time.monotonic() - started, outcomes


def _outcome_metrics(outcomes: Sequence[ToolOutcome]) -> dict[str, Any]:
    visible_bytes = 0
    disk_bytes: int | None = None
    delivered_inline = 0
    backgrounded_markers = 0
    truncation_markers = 0
    for outcome in outcomes:
        text = _text(outcome)
        visible_bytes += len(text.encode("utf-8"))
        if "done-" in text:
            delivered_inline += 1
        if "background" in text.lower() and "done-" not in text:
            backgrounded_markers += 1
        if " truncated: showing " in text:
            truncation_markers += 1
        marker_at = text.find(_FULL_OUTPUT_MARKER)
        if marker_at != -1:
            file_name = text[marker_at + len(_FULL_OUTPUT_MARKER) :].splitlines()[0].strip()
            path = Path(file_name)
            if path.is_file():
                disk_bytes = max(disk_bytes or 0, path.stat().st_size)
    return {
        "calls": len(outcomes),
        "delivered_inline": delivered_inline,
        "backgrounded_markers": backgrounded_markers,
        "truncation_markers": truncation_markers,
        "model_visible_bytes": visible_bytes,
        "disk_full_bytes": disk_bytes,
    }


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def test_m1_single_long_call_wall_clock(tmp_path: Path) -> None:
    """One long call: pre-implementation wall ~= duration; with the capability ~= threshold."""
    _interpreter_or_skip()
    limits, threshold_wired = _limits_with_threshold()
    wall, outcomes = _run_calls(tmp_path, limits, [_sleep_command(_SINGLE_SLEEP_S, "done-single")])
    _report(
        {
            "scenario": "M1-single",
            "threshold_wired": threshold_wired,
            "duration_s": _SINGLE_SLEEP_S,
            "wall_s": round(wall, 2),
            **_outcome_metrics(outcomes),
        }
    )
    # The only always-true invariant: the call terminated and answered something.
    assert outcomes[0].state in {"succeeded", "failed"}


def test_m1_sequential_batch_wall_clock(tmp_path: Path) -> None:
    """A model-style sequential turn of three long calls."""
    _interpreter_or_skip()
    limits, threshold_wired = _limits_with_threshold()
    commands = [_sleep_command(_BATCH_SLEEP_S, f"done-batch{i}") for i in range(_BATCH_SIZE)]
    wall, outcomes = _run_calls(tmp_path, limits, commands)
    _report(
        {
            "scenario": "M1-batch",
            "threshold_wired": threshold_wired,
            "per_call_s": _BATCH_SLEEP_S,
            "wall_s": round(wall, 2),
            **_outcome_metrics(outcomes),
        }
    )
    assert len(outcomes) == _BATCH_SIZE


def test_m1_concurrent_batch_context(tmp_path: Path) -> None:
    """Context column: event-loop overlap at the tool layer exists on both sides."""
    _interpreter_or_skip()
    limits, threshold_wired = _limits_with_threshold()
    commands = [_sleep_command(_BATCH_SLEEP_S, f"done-conc{i}") for i in range(_BATCH_SIZE)]
    registry = _registry()
    context = _context(tmp_path, limits)
    started = time.monotonic()

    async def _gathered() -> list[ToolOutcome]:
        return list(
            await asyncio.gather(
                *(
                    registry.invoke(context, "bash__run", {"command": command}, f"exec_itbgevalc{i}")
                    for i, command in enumerate(commands)
                )
            )
        )

    outcomes = asyncio.run(_gathered())
    wall = time.monotonic() - started
    _report(
        {
            "scenario": "M1-concurrent",
            "threshold_wired": threshold_wired,
            "per_call_s": _BATCH_SLEEP_S,
            "wall_s": round(wall, 2),
        }
    )
    assert len(outcomes) == _BATCH_SIZE


def test_m4_bounded_notification(tmp_path: Path) -> None:
    """Token gate: model-visible bytes stay bounded; the full payload is on disk.

    Works on both sides because the overflow-to-disk face (T-C3-001) is
    already landed; the AFTER column adds the bounded backgrounded marker
    for long-running calls on top of the same directory.
    """
    _interpreter_or_skip()
    limits, _ = _limits_with_threshold()
    payload = 4 * BASH_MAX_OUTPUT_BYTES
    wall, outcomes = _run_calls(tmp_path, limits, [_flood_command("x", payload)])
    metrics = _outcome_metrics(outcomes)
    _report(
        {
            "scenario": "M4-bounded",
            "payload_bytes": payload,
            "wall_s": round(wall, 2),
            **metrics,
        }
    )
    assert len(outcomes) == 1


def test_m2_m3_restart_survival(tmp_path: Path) -> None:
    """Persistence + replay: 0% pre-implementation; 100% with the capability."""
    _interpreter_or_skip()
    limits, threshold_wired = _limits_with_threshold()
    wall, outcomes = _run_calls(tmp_path, limits, [_sleep_command(_SINGLE_SLEEP_S, "done-restart")])
    metrics = _outcome_metrics(outcomes)
    status: str
    delivered: int | None = None
    row_count: int | None = None
    if not _module_available():
        status = "capability-absent"
    else:
        try:
            from opencode.storage import open_database

            background = importlib.import_module("opencode.tools.background")
            replay_pending = getattr(background, "replay_pending", None)
            if replay_pending is None:
                raise AttributeError("replay_pending")

            db_path = tmp_path / "eval.db"

            async def _probe() -> tuple[int, int]:
                async with open_database(db_path, mode="create") as database:
                    replayed = await replay_pending(database)
                conn = sqlite3.connect(db_path)
                try:
                    count = conn.execute("select count(*) from background_job").fetchone()[0]
                finally:
                    conn.close()
                return len(replayed), count

            delivered, row_count = asyncio.run(_probe())
            status = "probed"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report(
        {
            "scenario": "M2M3-restart",
            "capability_module": _module_available(),
            "threshold_wired": threshold_wired,
            "status": status,
            "notifications_delivered": delivered,
            "background_job_rows": row_count,
            "prep_wall_s": round(wall, 2),
            **metrics,
        }
    )
    assert status.startswith(("capability-absent", "probed", "api-mismatch"))
