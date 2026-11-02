"""Migration 0001: initial minimal schema (CORE-CONTRACTS §3.2).

Revision ID: 0001
Revises:
Create Date: 2026-11-02

Column lists follow CORE-CONTRACTS §3.2 verbatim (the scheduler cross-checks
them column by column). The §3.2 uniqueness requirements
(unique(session_id, seq) etc.) are implemented as explicitly named UNIQUE
indexes: on SQLite a UNIQUE constraint is exactly a unique index, and named
indexes stay addressable for later revisions and verification (inline
constraints would become unaddressable ``sqlite_autoindex_*`` entries).

- ``events``: id PK, session_id, seq, type, version, created (INTEGER, epoch
  ms -- TS ``EventTable.created``), data/metadata JSON, batch_id,
  batch_ordinal, optional dedupe_key; unique(session_id, seq),
  unique(session_id, batch_id, batch_ordinal), unique(session_id, dedupe_key).
  There is NO separate sequence table: seq is allocated as max(seq)+1 per
  session inside the write transaction and ``sessions.applied_seq`` records
  the projection watermark (CORE-CONTRACTS §3.2 Python difference; the TS
  ``EventSequenceTable`` is deliberately not ported). ``events`` also carries
  NO foreign key, so the durable log survives read-model deletion (rebuild,
  CORE-CONTRACTS §3.3 acceptance C; §3.2.1 explicitly contemplates absent
  sessions rows while events remain). Read-model tables below cascade from
  ``sessions`` instead -- see progress note T-C1-003 for the conservative
  reading behind this split.
- ``sessions``: session_id PK, status, active_run_id, config snapshot +
  snapshot_hash (both nullable: the §3.2.1 minimal diagnostic record stores
  them empty), applied_seq (NOT NULL, default 0), optional diagnostic.
- ``messages``: session_id, message_id, seq (order), data JSON;
  unique(session_id, message_id).
- ``tool_executions``: session_id, execution_id, run_id, step_id, message_id,
  tool_call_id, input_hash, state; unique(session_id, execution_id),
  unique(session_id, step_id, tool_call_id).
- ``permissions``: minimal projection of pending approvals and saved
  decisions (§3.2: input_hash, scope, expiry, terminal state).
  ``input_hash`` is nullable because ``py.permission.saved`` data carries no
  input_hash, only pending approvals do.

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified backup
and a revision tag cannot replace data rollback, so this migration refuses
to drop the schema (dropping it would destroy the event log).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("batch_ordinal", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("events_session_seq_uq", "events", ["session_id", "seq"], unique=True)
    op.create_index("events_session_batch_uq", "events", ["session_id", "batch_id", "batch_ordinal"], unique=True)
    op.create_index("events_session_dedupe_uq", "events", ["session_id", "dedupe_key"], unique=True)
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("active_run_id", sa.Text(), nullable=True),
        sa.Column("config_snapshot", sa.JSON(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=True),
        sa.Column("applied_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diagnostic", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_table(
        "messages",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index("messages_session_message_uq", "messages", ["session_id", "message_id"], unique=True)
    op.create_table(
        "tool_executions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("execution_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "tool_executions_session_execution_uq", "tool_executions", ["session_id", "execution_id"], unique=True
    )
    op.create_index(
        "tool_executions_session_step_tool_call_uq",
        "tool_executions",
        ["session_id", "step_id", "tool_call_id"],
        unique=True,
    )
    op.create_table(
        "permissions",
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("permission_id", sa.Text(), nullable=True),
        sa.Column("decision_id", sa.Text(), nullable=True),
        sa.Column("execution_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("resources", sa.JSON(), nullable=True),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "0001 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping the schema would destroy the event log"
    )
