"""Migration 0007: artifacts (T-C3-011).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21

Adds the ``artifact`` table: the py face of upstream 2.3.2
``core/src/artifact.ts``'s KV records (ItemPrefix ``artifact:item:<id>``
folds into the primary key; the StoredRecord fields map verbatim):

- ``id`` -- ``art_`` + ascending (the upstream Artifact.ID face);
- ``info`` -- the serialized Artifact.Info JSON (title/entry/url/sessionID/
  provider/timestamps), kept whole because the HTTP face returns it as-is;
- ``directory`` -- the creating location's directory (upstream keeps it to
  resolve /view without a location query; py also uses it for the
  cross-project isolation the upstream projectID filter provides);
- ``project_id`` -- reserved for the upstream projectID semantics; py has no
  Project service so it stays nullable and the directory is the filter face.

``session_id`` carries NO foreign key: a deleted session keeps its
artifacts (upstream checkSession only validates at create/update time).

Downgrade: CORE-CONTRACTS §3.3 -- downgrades restore from a verified
backup, so this migration refuses to drop the table (dropping it would
silently lose every artifact record while the files stay orphaned on
disk).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "artifact",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("info", sa.Text(), nullable=False),
        sa.Column("directory", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("artifact_directory_idx", "artifact", ["directory"])


def downgrade() -> None:
    raise RuntimeError(
        "0007 downgrade is restore-from-verified-backup only (CORE-CONTRACTS §3.3); "
        "dropping artifact would orphan every artifact directory on disk"
    )
