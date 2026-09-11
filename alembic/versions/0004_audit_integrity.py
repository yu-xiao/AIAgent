"""Add audit record integrity metadata.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("integrity_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_logs", sa.Column("integrity_key_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_logs", "integrity_key_id")
    op.drop_column("audit_logs", "integrity_hash")
