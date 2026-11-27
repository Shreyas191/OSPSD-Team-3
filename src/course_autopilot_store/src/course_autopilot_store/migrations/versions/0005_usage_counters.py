"""Model-call counters shared by every agent instance (#102).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``usage_counters``."""
    op.create_table(
        "usage_counters",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start"),
    )


def downgrade() -> None:
    """Drop ``usage_counters``."""
    op.drop_table("usage_counters")
