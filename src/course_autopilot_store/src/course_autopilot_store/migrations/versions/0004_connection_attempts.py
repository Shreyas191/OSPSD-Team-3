"""Provider connections: attempts in progress, each tied to its session (#98).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create ``connection_attempts``."""
    op.create_table(
        "connection_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index(
        op.f("ix_connection_attempts_session_id"),
        "connection_attempts",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop ``connection_attempts``."""
    op.drop_index(op.f("ix_connection_attempts_session_id"), table_name="connection_attempts")
    op.drop_table("connection_attempts")
