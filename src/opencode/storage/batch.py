"""Batch splitting under the bound-parameter cap (upstream ``core/src/database/batch.ts``).

Banked deviation (doc/plans/port-232-increments.md T-C3-008): the upstream
``insertBatches`` derives ``per_item`` from a drizzle ``Table``'s column
count; py has no table objects, so callers compute the column count and pass
``per_item`` to :func:`batches` themselves (``insertBatches`` is therefore
not mirrored). The cap keeps the upstream Durable Object motivation even
though py sqlite3's real limit is far higher (32766): one constant,
deterministic behaviour across runtimes, injectable where reality differs.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Durable Object SQLite rejects a statement that binds more than 100
#: parameters, so every runtime stays under it.
MAX_BOUND_PARAMETERS = 100


def batches[T](
    items: Sequence[T],
    per_item: int,
    fixed: int = 0,
    *,
    max_bound_parameters: int = MAX_BOUND_PARAMETERS,
) -> list[list[T]]:
    """Split ``items`` so each statement binds at most the cap.

    ``per_item`` parameters for each item (a multi-row insert binds one per
    column) plus ``fixed`` for the rest of the statement; the batch size is
    ``max(1, (cap - fixed) // per_item)`` with an exact ceiling split.
    """
    size = max(1, (max_bound_parameters - fixed) // per_item)
    count = (len(items) + size - 1) // size
    return [list(items[index * size : (index + 1) * size]) for index in range(count)]
