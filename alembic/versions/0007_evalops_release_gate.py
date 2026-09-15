"""Add EvalOps datasets, runs, policies, and release gate evidence.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVAL_PERMISSIONS = {
    "eval:view": "View evaluation datasets, policies, and results.",
    "eval:dataset:write": "Create and version evaluation datasets.",
    "eval:run:create": "Run evaluations for immutable Agent versions.",
    "eval:policy:manage": "Manage Agent evaluation gate policies.",
}


def upgrade() -> None:
    bind = op.get_bind()
    for code, description in _EVAL_PERMISSIONS.items():
        bind.execute(
            sa.text(
                "INSERT INTO permissions (code, description) "
                "SELECT :code, :description "
                "WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE code = :code)"
            ),
            {"code": code, "description": description},
        )
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_code) "
                "SELECT roles.id, :permission_code FROM roles "
                "WHERE roles.code = 'organization_admin' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM role_permissions existing "
                "WHERE existing.role_id = roles.id "
                "AND existing.permission_code = :permission_code)"
            ),
            {"permission_code": code},
        )

    op.create_table(
        "evaluation_datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1_000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "code", name="uq_eval_datasets_org_code"),
    )
    op.create_index(
        "ix_evaluation_datasets_organization_id",
        "evaluation_datasets",
        ["organization_id"],
    )
    op.create_index(
        "ix_eval_datasets_org_status",
        "evaluation_datasets",
        ["organization_id", "status"],
    )
    op.create_table(
        "evaluation_dataset_drafts",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("cases", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["evaluation_datasets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("dataset_id"),
    )
    op.create_index(
        "ix_evaluation_dataset_drafts_organization_id",
        "evaluation_dataset_drafts",
        ["organization_id"],
    )
    op.create_table(
        "evaluation_dataset_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("cases_snapshot", sa.JSON(), nullable=False),
        sa.Column("cases_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["evaluation_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id", "version_number", name="uq_eval_dataset_versions_number"
        ),
    )
    op.create_index(
        "ix_evaluation_dataset_versions_dataset_id",
        "evaluation_dataset_versions",
        ["dataset_id"],
    )
    op.create_index(
        "ix_evaluation_dataset_versions_organization_id",
        "evaluation_dataset_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_eval_dataset_versions_org_dataset",
        "evaluation_dataset_versions",
        ["organization_id", "dataset_id", "version_number"],
    )
    op.create_table(
        "agent_evaluation_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=30), nullable=False),
        sa.Column("dataset_version_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("min_pass_rate", sa.Float(), nullable=False),
        sa.Column("max_critical_failures", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["evaluation_dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id", "environment", name="uq_agent_eval_policies_environment"
        ),
    )
    op.create_index(
        "ix_agent_evaluation_policies_agent_id",
        "agent_evaluation_policies",
        ["agent_id"],
    )
    op.create_index(
        "ix_agent_evaluation_policies_organization_id",
        "agent_evaluation_policies",
        ["organization_id"],
    )
    op.create_index(
        "ix_agent_eval_policies_org_environment",
        "agent_evaluation_policies",
        ["organization_id", "environment"],
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("agent_config_digest", sa.String(length=64), nullable=False),
        sa.Column("dataset_version_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("min_pass_rate", sa.Float(), nullable=False),
        sa.Column("max_critical_failures", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("critical_failures", sa.Integer(), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=True),
        sa.Column("gate_passed", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["evaluation_dataset_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["agent_evaluation_policies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id"),
        sa.UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_eval_runs_org_idempotency"
        ),
    )
    op.create_index("ix_evaluation_runs_agent_id", "evaluation_runs", ["agent_id"])
    op.create_index(
        "ix_evaluation_runs_agent_version_id",
        "evaluation_runs",
        ["agent_version_id"],
    )
    op.create_index(
        "ix_evaluation_runs_organization_id",
        "evaluation_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_eval_runs_org_agent_created",
        "evaluation_runs",
        ["organization_id", "agent_id", "created_at"],
    )
    op.create_table(
        "evaluation_case_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("case_key", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("used_tools", sa.JSON(), nullable=False),
        sa.Column("citations_count", sa.Integer(), nullable=False),
        sa.Column("answer_digest", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"], ["evaluation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_run_id", "case_key", name="uq_eval_results_run_case"
        ),
    )
    op.create_index(
        "ix_evaluation_case_results_evaluation_run_id",
        "evaluation_case_results",
        ["evaluation_run_id"],
    )
    op.create_index(
        "ix_evaluation_case_results_organization_id",
        "evaluation_case_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_eval_results_org_run",
        "evaluation_case_results",
        ["organization_id", "evaluation_run_id"],
    )
    with op.batch_alter_table("agent_releases") as batch_op:
        batch_op.add_column(sa.Column("evaluation_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("gate_decision", sa.String(length=30), nullable=True))
        batch_op.add_column(
            sa.Column("gate_policy_digest", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_agent_releases_evaluation_run_id",
            "evaluation_runs",
            ["evaluation_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_releases") as batch_op:
        batch_op.drop_constraint(
            "fk_agent_releases_evaluation_run_id", type_="foreignkey"
        )
        batch_op.drop_column("gate_policy_digest")
        batch_op.drop_column("gate_decision")
        batch_op.drop_column("evaluation_run_id")
    op.drop_index("ix_eval_results_org_run", table_name="evaluation_case_results")
    op.drop_index(
        "ix_evaluation_case_results_organization_id",
        table_name="evaluation_case_results",
    )
    op.drop_index(
        "ix_evaluation_case_results_evaluation_run_id",
        table_name="evaluation_case_results",
    )
    op.drop_table("evaluation_case_results")
    op.drop_index("ix_eval_runs_org_agent_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_organization_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_agent_version_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_agent_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index(
        "ix_agent_eval_policies_org_environment", table_name="agent_evaluation_policies"
    )
    op.drop_index(
        "ix_agent_evaluation_policies_organization_id",
        table_name="agent_evaluation_policies",
    )
    op.drop_index(
        "ix_agent_evaluation_policies_agent_id",
        table_name="agent_evaluation_policies",
    )
    op.drop_table("agent_evaluation_policies")
    op.drop_index(
        "ix_eval_dataset_versions_org_dataset",
        table_name="evaluation_dataset_versions",
    )
    op.drop_index(
        "ix_evaluation_dataset_versions_organization_id",
        table_name="evaluation_dataset_versions",
    )
    op.drop_index(
        "ix_evaluation_dataset_versions_dataset_id",
        table_name="evaluation_dataset_versions",
    )
    op.drop_table("evaluation_dataset_versions")
    op.drop_index(
        "ix_evaluation_dataset_drafts_organization_id",
        table_name="evaluation_dataset_drafts",
    )
    op.drop_table("evaluation_dataset_drafts")
    op.drop_index("ix_eval_datasets_org_status", table_name="evaluation_datasets")
    op.drop_index(
        "ix_evaluation_datasets_organization_id", table_name="evaluation_datasets"
    )
    op.drop_table("evaluation_datasets")
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", expanding=True)
        ),
        {"codes": list(_EVAL_PERMISSIONS)},
    )
