"""Execution: which run holds a running proposal, and when it last showed it was alive (#90).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add ``proposals.heartbeat_at`` and ``proposals.run_id``."""
    op.add_column(
        "proposals",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("proposals", sa.Column("run_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Drop ``proposals.run_id`` and ``proposals.heartbeat_at``."""
    op.drop_column("proposals", "run_id")
    op.drop_column("proposals", "heartbeat_at")
