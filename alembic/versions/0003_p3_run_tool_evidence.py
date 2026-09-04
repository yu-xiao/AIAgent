"""Persist P3 MCP invocation summaries and citations.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("server_code", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("arguments_digest", sa.String(length=128), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_invocations_run_id", "tool_invocations", ["run_id"])
    op.create_index("ix_tool_invocations_run_created", "tool_invocations", ["run_id", "created_at"])
    op.create_index("ix_tool_invocations_organization_id", "tool_invocations", ["organization_id"])
    op.create_index("ix_tool_invocations_user_id", "tool_invocations", ["user_id"])
    op.create_index("ix_tool_invocations_trace", "tool_invocations", ["trace_id"])
    op.create_table(
        "citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("server_code", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("resource_id", sa.String(length=300), nullable=True),
        sa.Column("queried_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("partial", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_citations_run_id", "citations", ["run_id"])
    op.create_index("ix_citations_run_created", "citations", ["run_id", "created_at"])
    op.create_index("ix_citations_organization_id", "citations", ["organization_id"])
    op.create_index("ix_citations_trace", "citations", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_citations_trace", table_name="citations")
    op.drop_index("ix_citations_organization_id", table_name="citations")
    op.drop_index("ix_citations_run_created", table_name="citations")
    op.drop_index("ix_citations_run_id", table_name="citations")
    op.drop_table("citations")
    op.drop_index("ix_tool_invocations_trace", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_user_id", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_organization_id", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_created", table_name="tool_invocations")
    op.drop_index("ix_tool_invocations_run_id", table_name="tool_invocations")
    op.drop_table("tool_invocations")
