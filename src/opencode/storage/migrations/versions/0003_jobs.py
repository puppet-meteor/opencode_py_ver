"""Migration 0003: the jobs read model (PHASE3-ORCHESTRATOR §5.4; T-C1-076).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12

Adds the orchestrator's job registry read model without touching 0001/0002
or the events table (§3.2 L183 spirit; the jobs-domain writer in
``opencode.storage.jobs_store`` commits to the same ``events`` table on
synthesized ``ses_job_{job_id}`` stream keys):

- ``jobs``: job_id PK, stream_id (unique -- one stream per job),
  pipeline_id, params/params_hash/session_anchor (the §5.4 spec binding),
  status (created/running/stalled/gated/done/failed/cancelled), depth
  (workflow nesting), budget, checkpoint_ref, retry_count, attempt_id,
  stalled_reason, lineage, trace_id, result, error, applied_seq (the jobs
  projection watermark A), created/updated epoch-ms columns.

Deliberate deviation from the 0002_runs precedent, disclosed in progress ①:
NO foreign key to ``sessions`` -- a synthetic ``ses_job_*`` stream has no
sessions row, so an FK would make every jobs projection write fail. Events
stay the authority: the row set is fully reconstructible by the §3.3-style
rebuild (``JobsStore.rebuild``) over the stream's log.

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified backup
and a revision tag cannot replace data rollback, so this migration refuses
to drop the table (dropping it would orphan the log-derived job history).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Text(), nullable=False),
        sa.Column("pipeline_id", sa.Text(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("params_hash", sa.Text(), nullable=True),
        sa.Column("session_anchor", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=True),
        sa.Column("checkpoint_ref", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Text(), nullable=True),
        sa.Column("stalled_reason", sa.Text(), nullable=True),
        sa.Column("lineage", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("applied_seq", sa.Integer(), nullable=False),
        sa.Column("created_epoch_ms", sa.Integer(), nullable=False),
        sa.Column("updated_epoch_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("jobs_stream_id_uq", "jobs", ["stream_id"], unique=True)
    op.create_index("jobs_status_idx", "jobs", ["status"])


def downgrade() -> None:
    raise RuntimeError(
        "0003 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping the jobs read model would orphan the log-derived job history"
    )
