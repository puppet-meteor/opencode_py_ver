"""Migration 0004: the steering-inbox read model (T-C1-100, P7-S1).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15

Adds the durable inbox's pending-only read model without touching the
existing tables or the events table (the 0002_runs precedent, §3.2 L183
spirit; the family addition regenerates the schema-lock artifacts):

- ``inbox``: the pending steering items, keyed (session_id, inbox_id) --
  kind (user/synthetic; compaction/move are NOT ported, ticket §3-2),
  delivery (steer/queue), request_id (NULL for synthetic items), the
  parked user turn verbatim as ``message`` JSON, enqueued_seq (the seq of
  the ``py.inbox.enqueued`` event -- the enqueue-order key), FK cascade
  from ``sessions`` (a read-model row, like messages/runs/permissions).

Rows exist only while an item is pending: ``py.inbox.delivered`` and
``py.inbox.cancelled`` delete the row in the same commit transaction as
the consumption/revocation; ``py.inbox.delivery_changed`` re-arms the
delivery column in place. Events stay the authority: the row set is fully
reconstructible by the rebuild from the final fold state
(``SessionState.inbox``).

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified backup
and a revision tag cannot replace data rollback, so this migration refuses
to drop the table (dropping it would orphan the log-derived pending inbox).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "inbox",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("inbox_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("delivery", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("enqueued_seq", sa.Integer(), nullable=False),
        sa.Column("message", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "inbox_id"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index("inbox_session_seq_idx", "inbox", ["session_id", "enqueued_seq"])


def downgrade() -> None:
    raise RuntimeError(
        "0004 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping the inbox read model would orphan the log-derived pending inbox"
    )
