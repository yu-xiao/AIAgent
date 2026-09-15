"""Add managed Agent definitions, versions, deployments, and Run binding.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT_PERMISSIONS = {
    "agent:view": "View the organization's managed Agents.",
    "agent:draft:write": "Create and update Agent drafts.",
    "agent:version:create": "Create immutable Agent versions.",
    "agent:release": "Release and roll back evaluated Agent versions.",
    "agent:release:bypass": "Bypass an Agent release gate with an audited reason.",
}


def upgrade() -> None:
    bind = op.get_bind()
    for code, description in _AGENT_PERMISSIONS.items():
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
        "agent_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1_000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "code", name="uq_agent_definitions_org_code"
        ),
    )
    op.create_index(
        "ix_agent_definitions_organization_id",
        "agent_definitions",
        ["organization_id"],
    )
    op.create_index(
        "ix_agent_definitions_org_status",
        "agent_definitions",
        ["organization_id", "status"],
    )
    op.create_index(
        "uq_agent_definitions_one_default_org",
        "agent_definitions",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE"),
        sqlite_where=sa.text("is_default = 1"),
    )
    op.create_table(
        "agent_drafts",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index(
        "ix_agent_drafts_organization_id", "agent_drafts", ["organization_id"]
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_number"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])
    op.create_index(
        "ix_agent_versions_organization_id", "agent_versions", ["organization_id"]
    )
    op.create_index(
        "ix_agent_versions_org_agent",
        "agent_versions",
        ["organization_id", "agent_id", "version_number"],
    )
    op.create_table(
        "agent_deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=30), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("deployed_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["deployed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id", "environment", name="uq_agent_deployments_environment"
        ),
    )
    op.create_index("ix_agent_deployments_agent_id", "agent_deployments", ["agent_id"])
    op.create_index(
        "ix_agent_deployments_organization_id", "agent_deployments", ["organization_id"]
    )
    op.create_index(
        "ix_agent_deployments_org_environment",
        "agent_deployments",
        ["organization_id", "environment"],
    )
    op.create_table(
        "agent_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("previous_version_id", sa.Uuid(), nullable=True),
        sa.Column("environment", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("bypassed_gate", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agent_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"], ["agent_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_agent_releases_org_idempotency",
        ),
    )
    op.create_index("ix_agent_releases_agent_id", "agent_releases", ["agent_id"])
    op.create_index(
        "ix_agent_releases_organization_id", "agent_releases", ["organization_id"]
    )
    op.create_index(
        "ix_agent_releases_org_agent",
        "agent_releases",
        ["organization_id", "agent_id", "created_at"],
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("agent_version_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("agent_config_digest", sa.String(length=64), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_runs_agent_id",
            "agent_definitions",
            ["agent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_runs_agent_version_id",
            "agent_versions",
            ["agent_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index("ix_runs_agent_id", ["agent_id"])
        batch_op.create_index("ix_runs_agent_version_id", ["agent_version_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_agent_version_id")
        batch_op.drop_index("ix_runs_agent_id")
        batch_op.drop_constraint("fk_runs_agent_version_id", type_="foreignkey")
        batch_op.drop_constraint("fk_runs_agent_id", type_="foreignkey")
        batch_op.drop_column("agent_config_digest")
        batch_op.drop_column("agent_version_id")
        batch_op.drop_column("agent_id")
    op.drop_index("ix_agent_releases_org_agent", table_name="agent_releases")
    op.drop_index("ix_agent_releases_organization_id", table_name="agent_releases")
    op.drop_index("ix_agent_releases_agent_id", table_name="agent_releases")
    op.drop_table("agent_releases")
    op.drop_index("ix_agent_deployments_org_environment", table_name="agent_deployments")
    op.drop_index("ix_agent_deployments_organization_id", table_name="agent_deployments")
    op.drop_index("ix_agent_deployments_agent_id", table_name="agent_deployments")
    op.drop_table("agent_deployments")
    op.drop_index("ix_agent_versions_org_agent", table_name="agent_versions")
    op.drop_index("ix_agent_versions_organization_id", table_name="agent_versions")
    op.drop_index("ix_agent_versions_agent_id", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_index("ix_agent_drafts_organization_id", table_name="agent_drafts")
    op.drop_table("agent_drafts")
    op.drop_index(
        "uq_agent_definitions_one_default_org", table_name="agent_definitions"
    )
    op.drop_index("ix_agent_definitions_org_status", table_name="agent_definitions")
    op.drop_index("ix_agent_definitions_organization_id", table_name="agent_definitions")
    op.drop_table("agent_definitions")
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", expanding=True)
        ),
        {"codes": list(_AGENT_PERMISSIONS)},
    )
