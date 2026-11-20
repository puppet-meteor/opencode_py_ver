"""Migration 0006: automations (T-C3-003, P16-S1).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20

Adds the upstream 2.3.2 ``automation`` table face plus the py-side lease
table (see doc/plans/port-232-increments.md T-C3-003):

- ``automation`` -- columns mirror upstream ``core/src/automation/sql.ts``
  (trigger/action as JSON text with room for the not-yet-ported event/code
  variants); ``session_id`` carries NO foreign key: deleting the creating
  session keeps the automation (upstream keeps the same independence, the
  row outlives session lifecycle). Indexes: project, session, and a partial
  ``automation_active_idx`` on ``next_due_at`` where ``status='active'``
  (the scheduler's eligible scan);
- ``automation_lease`` -- the single-flight leader lease upstream keeps in
  the KV table (py has no KV face): a TTL row claimed per run with
  ``INSERT ... ON CONFLICT DO UPDATE ... WHERE owner=me OR until<=now``.
  A lease, not a lock: ownership decays with time so a crashed scheduler
  never wedges the table.

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified
backup, so this migration refuses to drop the tables (dropping them would
silently drop scheduled unattended work).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "automation",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("directory", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("tz", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("next_due_at", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_run_at", sa.Integer(), nullable=True),
        sa.Column("last_message_id", sa.Text(), nullable=True),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column("last_output", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("event_position", sa.Text(), nullable=True),
        sa.Column("time_created", sa.Integer(), nullable=False),
        sa.Column("time_updated", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("automation_project_idx", "automation", ["project_id"])
    op.create_index("automation_session_idx", "automation", ["session_id"])
    op.create_index(
        "automation_active_idx",
        "automation",
        ["next_due_at"],
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "automation_lease",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("until_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "0006 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping automation/automation_lease would silently drop scheduled unattended work"
    )
