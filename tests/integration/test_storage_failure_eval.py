"""T-C3-008 evaluation harness: db storage-failure classification + batching.

A measurement face, not an assertion face (the test_automation_eval.py
precedent): every probe records a metric and passes, whether or not the
capability exists. Running this module on pre-implementation code captures
the BEFORE baseline; the identical inputs afterwards give the AFTER column.
The delta is the ticket's evidence against doc/GOAL.md's determinism-first
gate (doc/plans/port-232-increments.md T-C3-008, decisions 1-5):

* P0 capability -- the banked seam contract: module
  ``opencode.storage.failures`` (``storage_failure_message`` /
  ``rolled_back`` / ``finalize_transaction``) and module
  ``opencode.storage.batch`` (``MAX_BOUND_PARAMETERS`` / ``batches``).
* M1 classification matrix -- the eight upstream primary result codes map
  to engine-neutral messages (5/6 in use, 8 read-only, 10 ioerr, 11/26
  damaged, 13 full, 14 cantopen); a constraint (19) and a plain error are
  NOT storage failures.
* M2 extended + wrapped -- an extended code classifies by its primary low
  byte (517 BUSY_SNAPSHOT -> "in use"); a SQLAlchemy-wrapped error is
  classified through its ``.orig``.
* M3 rolled-back set -- the auto-rollback codes {5, 7, 9, 10, 13} (and an
  extended IOERR) say the transaction may already be rolled back; 6/19
  and non-sqlite errors say not.
* M4 batching math -- size = max(1, (cap - fixed) // per_item), exact ceil
  slicing, fixed-parameter subtraction, per-item above the cap degrades to
  one item per batch, empty input yields no batches.
* M5 live full disk -- a real engine driven to SQLITE_FULL via
  ``PRAGMA max_page_count = 1``: the failed insert classifies as "Storage
  is full", and after the PRAGMA is restored the same engine takes the
  next transaction (nothing is left dangling).
* M6 finalize seam -- a rollback that itself fails is swallowed when the
  body error is an auto-rollback code (the body error is what surfaces)
  and replaces the body error otherwise.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The upstream live-probe payload (60 KiB into a one-page database).
_LARGE = "x" * 60_000

_MESSAGES = {
    5: "Storage is in use by another process",
    6: "Storage is in use by another process",
    8: "Storage is read-only",
    10: "Storage couldn't be read or written",
    11: "Storage is damaged",
    13: "Storage is full",
    14: "Storage couldn't be opened",
    26: "Storage is damaged",
}


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _module(name: str) -> Any | None:
    try:
        if importlib.util.find_spec(name) is None:
            return None
    except ModuleNotFoundError:
        return None
    return importlib.import_module(name)


def _sql_error(code: int, message: str = "fixture") -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


# ---------------------------------------------------------------------------
# P0 -- the capability seam
# ---------------------------------------------------------------------------


def test_p0_capability_probe() -> None:
    failures = _module("opencode.storage.failures")
    batch = _module("opencode.storage.batch")
    faces = {
        "failures_module": callable(getattr(failures, "storage_failure_message", None)) if failures else False,
        "rolled_back": callable(getattr(failures, "rolled_back", None)) if failures else False,
        "finalize_transaction": callable(getattr(failures, "finalize_transaction", None)) if failures else False,
        "batch_module": batch is not None and hasattr(batch, "MAX_BOUND_PARAMETERS"),
        "batches_fn": callable(getattr(batch, "batches", None)) if batch else False,
    }
    status = "capability-absent" if not any(faces.values()) else ("probed" if all(faces.values()) else "partial")
    _report({"scenario": "P-capability", "status": status, **faces})
    assert status in ("capability-absent", "partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- the classification matrix (pure face)
# ---------------------------------------------------------------------------


def test_m1_classification_matrix() -> None:
    failures = _module("opencode.storage.failures")
    status = "capability-absent"
    passed = total = 0
    if failures is not None and callable(getattr(failures, "storage_failure_message", None)):
        cases: list[tuple[int, str | None]] = [(code, text) for code, text in _MESSAGES.items()]
        cases += [(19, None), (1, None)]  # constraint / generic sqlite error: not storage failures
        for code, expected in cases:
            total += 1
            try:
                passed += failures.storage_failure_message(_sql_error(code)) == expected
            except Exception:
                pass
        total += 1
        try:
            passed += failures.storage_failure_message(ValueError("plain")) is None
        except Exception:
            pass
        status = "classified" if passed == total else f"misclassified:{passed}/{total}"
    _report({"scenario": "M1-classification-matrix", "status": status, "passed": passed, "total": total})
    assert status.startswith(("capability-absent", "classified", "misclassified"))


# ---------------------------------------------------------------------------
# M2 -- extended codes classify by the primary byte; wrappers are walked
# ---------------------------------------------------------------------------


def test_m2_extended_and_wrapped() -> None:
    failures = _module("opencode.storage.failures")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if failures is not None and callable(getattr(failures, "storage_failure_message", None)):
        try:
            extended = failures.storage_failure_message(_sql_error(517))  # BUSY_SNAPSHOT
            ioerr_read = failures.storage_failure_message(_sql_error(266))  # IOERR_READ
            wrapped = None
            try:
                from sqlalchemy.exc import OperationalError as SaOperationalError

                wrapped = failures.storage_failure_message(
                    SaOperationalError("INSERT ...", None, _sql_error(13, "disk full"))
                )
            except ImportError:
                wrapped = "sqlalchemy-missing"
            metrics = {
                "extended_517": extended == "Storage is in use by another process",
                "extended_266": ioerr_read == "Storage couldn't be read or written",
                "wrapped_orig": wrapped == "Storage is full",
            }
            status = "primary-byte-walked" if all(metrics.values()) else "drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M2-extended-wrapped", "status": status, **metrics})
    assert status.startswith(("capability-absent", "primary-byte-walked", "drifting", "api-mismatch"))


# ---------------------------------------------------------------------------
# M3 -- the auto-rollback set (pure face)
# ---------------------------------------------------------------------------


def test_m3_rolled_back_set() -> None:
    failures = _module("opencode.storage.failures")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if failures is not None and callable(getattr(failures, "rolled_back", None)):
        try:
            truthy = {5: True, 7: True, 9: True, 10: True, 13: True, 266: True}
            falsy = {6: False, 19: False, 8: False, 11: False}
            metrics = {
                f"code_{code}": failures.rolled_back(_sql_error(code)) is expected for code, expected in truthy.items()
            }
            metrics.update(
                {f"code_{code}": failures.rolled_back(_sql_error(code)) is expected for code, expected in falsy.items()}
            )
            metrics["plain_error_false"] = failures.rolled_back(ValueError("x")) is False
            status = "set-verified" if all(metrics.values()) else "set-drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M3-rolled-back-set", "status": status, **metrics})
    assert status.startswith(("capability-absent", "set-verified", "set-drifting", "api-mismatch"))


# ---------------------------------------------------------------------------
# M4 -- the batching math (pure face)
# ---------------------------------------------------------------------------


def test_m4_batching_math() -> None:
    batch = _module("opencode.storage.batch")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if batch is not None and callable(getattr(batch, "batches", None)):
        try:
            cap = getattr(batch, "MAX_BOUND_PARAMETERS", 100)

            def chunks(items: list[int], per_item: int, fixed: int = 0) -> list[list[int]]:
                return [list(chunk) for chunk in batch.batches(items, per_item, fixed)]

            metrics = {
                "cap_is_100": cap == 100,
                "split_10x30": chunks(list(range(10)), 30) == [list(range(3 * i, 3 * i + 3)) for i in range(3)] + [[9]],
                "fixed_subtracted": len(chunks(list(range(10)), 30, 10)[0]) == 3,
                "oversized_per_item": chunks([1, 2], 250) == [[1], [2]],
                "empty": chunks([], 1) == [],
                "exact_ceiling": len(chunks(list(range(250)), 25)) == 63 and len(chunks(list(range(250)), 25)[0]) == 4,
            }
            status = "math-verified" if all(metrics.values()) else "math-drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M4-batching-math", "status": status, **metrics})
    assert status.startswith(("capability-absent", "math-verified", "math-drifting", "api-mismatch"))


# ---------------------------------------------------------------------------
# M5 -- a live full disk (real engine)
# ---------------------------------------------------------------------------


def test_m5_live_full_disk(tmp_path: Path) -> None:
    failures = _module("opencode.storage.failures")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if failures is not None and callable(getattr(failures, "storage_failure_message", None)):

        async def run() -> dict[str, Any]:
            import sqlalchemy as sa

            from opencode.storage import open_database

            database = open_database(tmp_path / "probe.db")
            out: dict[str, Any] = {}
            try:
                async with database.engine.begin() as connection:
                    await connection.execute(sa.text("CREATE TABLE probe (value TEXT)"))
                async with database.engine.connect() as connection:
                    await connection.execute(sa.text("PRAGMA max_page_count = 1"))
                caught: BaseException | None = None
                try:
                    async with database.engine.begin() as connection:
                        await connection.execute(sa.text("INSERT INTO probe (value) VALUES (:v)"), {"v": _LARGE})
                except BaseException as error:  # the probe face measures the failure itself
                    caught = error
                out["failed"] = caught is not None
                if caught is not None:
                    out["message_full"] = failures.storage_failure_message(caught) == "Storage is full"
                    out["rolled_back"] = failures.rolled_back(caught) is True
                async with database.engine.connect() as connection:
                    await connection.execute(sa.text("PRAGMA max_page_count = 1073741823"))
                async with database.engine.begin() as connection:
                    await connection.execute(sa.text("INSERT INTO probe (value) VALUES ('ok')"))
                async with database.engine.connect() as connection:
                    rows = (await connection.execute(sa.text("SELECT count(*) FROM probe"))).scalar_one()
                out["next_transaction_ok"] = rows == 1
            finally:
                await database.close()
            return out

        try:
            metrics = asyncio.run(run())
            status = "live-verified" if all(metrics.values()) else "live-drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M5-live-full-disk", "status": status, **metrics})
    assert status.startswith(("capability-absent", "live-verified", "live-drifting", "api-mismatch"))


# ---------------------------------------------------------------------------
# M6 -- the finalize seam (rollback failure vs auto-rollback body)
# ---------------------------------------------------------------------------


class _RollbackExplodes:
    """Structural fake: the rollback itself fails."""

    async def rollback(self) -> None:
        raise RuntimeError("rollback exploded")


def test_m6_finalize_seam() -> None:
    failures = _module("opencode.storage.failures")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if failures is not None and callable(getattr(failures, "finalize_transaction", None)):

        async def run() -> dict[str, Any]:
            out: dict[str, Any] = {}
            # auto-rollback body: the rollback error is swallowed
            out["full_disk_body_surfaces"] = (
                await failures.finalize_transaction(_RollbackExplodes(), _sql_error(13, "full")) is None
            )
            # any other body: the rollback error replaces it
            replaced = None
            try:
                await failures.finalize_transaction(_RollbackExplodes(), _sql_error(19, "constraint"))
            except RuntimeError as error:
                replaced = str(error)
            out["constraint_body_replaced"] = replaced == "rollback exploded"
            return out

        try:
            metrics = asyncio.run(run())
            status = "seam-verified" if all(metrics.values()) else "seam-drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M6-finalize-seam", "status": status, **metrics})
    assert status.startswith(("capability-absent", "seam-verified", "seam-drifting", "api-mismatch"))
