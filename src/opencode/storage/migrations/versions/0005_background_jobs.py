"""Migration 0005: background jobs (T-C3-002, P15-S1).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Adds the upstream 2.3.2 ``job`` table face under the banked py name
``background_job`` (``storage/jobs_store.py`` already owns the unrelated
workflow event projection, so the upstream Job persistence lands here,
see doc/plans/port-232-increments.md T-C3-002):

- ``notification_id`` TEXT PK -- upstream's primary key, the id the
  settled synthetic notification replays under;
- ``session_id``/``tool_call_id`` -- the originating call, no FK: the
  row must outlive any session read-model rebuild (upstream keeps the
  job table standalone for the same reason -- replay is boot-time and
  must not be cascade-deleted by session lifecycle);
- ``kind`` -- recovery descriptor family, first period ``shell`` only
  (subagent lands with T-C3-005);
- ``status`` -- ``backgrounded`` (running, upserted the moment the call
  backgrounds, upstream ``start(background)`` before the fork) or a
  terminal ``succeeded``/``error``/``cancelled``;
- ``title`` -- the human face of the work (the command line);
- ``payload`` JSON -- the terminal model-visible text + structured
  output, NULL until settle;
- ``created_at_ms``/``updated_at_ms`` -- epoch-ms (txn.epoch_ms face).

Rows exist only while a notification is pending: admission (replay
delivery, upstream ``completeBackground``/inbox admit) and session
cancellation delete them.

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified
backup, so this migration refuses to drop the table (dropping it would
silently drop undelivered notifications).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "background_job",
        sa.Column("notification_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at_ms", sa.Integer(), nullable=False),
        sa.Column("updated_at_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("notification_id"),
    )
    op.create_index("background_job_session_idx", "background_job", ["session_id"])


def downgrade() -> None:
    raise RuntimeError(
        "0005 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping background_job would silently drop undelivered notifications"
    )
