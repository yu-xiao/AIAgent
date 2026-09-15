"""Add durable Run jobs and attempt history.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_run_jobs_run_id", "run_jobs", ["run_id"])
    op.create_index("ix_run_jobs_organization_id", "run_jobs", ["organization_id"])
    op.create_index("ix_run_jobs_user_id", "run_jobs", ["user_id"])
    op.create_index(
        "ix_run_jobs_available", "run_jobs", ["status", "available_at", "created_at"]
    )
    op.create_index("ix_run_jobs_lease_expiry", "run_jobs", ["lease_expires_at"])
    op.create_index(
        "ix_run_jobs_org_status", "run_jobs", ["organization_id", "status", "created_at"]
    )
    op.create_table(
        "run_job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["run_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_run_job_attempt"),
    )
    op.create_index("ix_run_job_attempts_job_id", "run_job_attempts", ["job_id"])
    op.create_index(
        "ix_run_job_attempts_job_started", "run_job_attempts", ["job_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_run_job_attempts_job_started", table_name="run_job_attempts")
    op.drop_index("ix_run_job_attempts_job_id", table_name="run_job_attempts")
    op.drop_table("run_job_attempts")
    op.drop_index("ix_run_jobs_org_status", table_name="run_jobs")
    op.drop_index("ix_run_jobs_lease_expiry", table_name="run_jobs")
    op.drop_index("ix_run_jobs_available", table_name="run_jobs")
    op.drop_index("ix_run_jobs_user_id", table_name="run_jobs")
    op.drop_index("ix_run_jobs_organization_id", table_name="run_jobs")
    op.drop_index("ix_run_jobs_run_id", table_name="run_jobs")
    op.drop_table("run_jobs")
