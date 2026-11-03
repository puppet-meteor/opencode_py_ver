"""Migration 0002: the runs read model (CORE-CONTRACTS §3.2 L183-188; OP-2, T-A-005).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03

Adds the fifth read model without touching 0001, the existing events, or any
wire/version (§3.2 L183):

- ``runs``: run_id PK, session_id, request_id, status, optional error JSON;
  unique(session_id, request_id), FK cascade from ``sessions`` (a read-model
  row, like messages/tool_executions/permissions -- the events log stays
  independent of it).

Status values are the §2.1 L59-61 run states (accepted/running/cancelling/
succeeded/failed/interrupted). ``py.prompt.accepted`` projects the accepted
row; ``py.run.started``/``cancel_requested``/``finished`` update it through
the legal transitions inside the same commit transaction as every other
projection (the fold in ``opencode.session.state`` is the single source).
Events remain the authority: the row set is fully reconstructible by the
rebuild, which is also the §3.3 backfill path -- before runs queries are
served, every committed ``py.prompt.accepted`` is replayed so all accepted
runs exist as rows (R1 multi-accepted logs keep every accepted row; no
terminal gate is added, §3.2 L187).

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified backup
and a revision tag cannot replace data rollback, so this migration refuses
to drop the table (dropping it would orphan the log-derived run history).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index("runs_session_request_uq", "runs", ["session_id", "request_id"], unique=True)


def downgrade() -> None:
    raise RuntimeError(
        "0002 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping the runs read model would orphan the log-derived run history"
    )
