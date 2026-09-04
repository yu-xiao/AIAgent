"""Add P2 MCP registry and external connection tables.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String(length=100)),
        sa.column("description", sa.String(length=300)),
    )
    p2_permissions = [
        ("connection:personal:create", "Create personal external connections."),
        ("connection:personal:disconnect", "Disconnect personal external connections."),
        ("connection:organization:manage", "Manage organization shared connections."),
        ("mcp-server:view", "View registered MCP servers."),
        ("mcp-server:manage", "Register and manage MCP servers."),
        ("tool:permission:use", "Use PermissionSystem MCP tools."),
        ("tool:erp:use", "Use ERP MCP tools."),
        ("tool:mes:use", "Use MES MCP tools."),
        ("tool:bi:use", "Use BI MCP tools."),
    ]
    op.bulk_insert(
        permission_table,
        [dict(code=code, description=description) for code, description in p2_permissions],
    )
    op.create_table(
        "mcp_server_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("system_code", sa.String(length=100), nullable=False),
        sa.Column("mcp_url", sa.String(length=2000), nullable=False),
        sa.Column("transport", sa.String(length=40), nullable=False),
        sa.Column("auth_mode", sa.String(length=50), nullable=False),
        sa.Column("credential_header", sa.String(length=100), nullable=False),
        sa.Column("authorization_server", sa.String(length=2000), nullable=True),
        sa.Column("authorization_endpoint", sa.String(length=2000), nullable=True),
        sa.Column("token_endpoint", sa.String(length=2000), nullable=True),
        sa.Column("oauth_client_id", sa.String(length=300), nullable=True),
        sa.Column("oauth_client_secret_reference", sa.String(length=300), nullable=True),
        sa.Column("token_endpoint_auth_method", sa.String(length=40), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2000), nullable=True),
        sa.Column("required_scope", sa.String(length=500), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("tool_sets", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("circuit_breaker_threshold", sa.Integer(), nullable=False),
        sa.Column("circuit_breaker_recovery_seconds", sa.Float(), nullable=False),
        sa.Column("response_size_limit", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "code", name="uq_mcp_servers_org_code"),
    )
    op.create_index(
        "ix_mcp_server_definitions_organization_id", "mcp_server_definitions", ["organization_id"]
    )
    op.create_index(
        "ix_mcp_servers_org_enabled", "mcp_server_definitions", ["organization_id", "enabled"]
    )
    op.create_table(
        "credential_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(length=300), nullable=False),
        sa.Column("vault_kind", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reference"),
    )
    op.create_index(
        "ix_credential_references_organization_id", "credential_references", ["organization_id"]
    )
    op.create_table(
        "external_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("ownership", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("external_issuer", sa.String(length=500), nullable=True),
        sa.Column("external_subject", sa.String(length=500), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("credential_reference", sa.String(length=300), nullable=False),
        sa.Column("refresh_credential_reference", sa.String(length=300), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("allowed_tool_sets", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_server_definitions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "server_id",
            "owner_user_id",
            "ownership",
            name="uq_external_connections_owner",
        ),
    )
    op.create_index(
        "ix_external_connections_organization_id", "external_connections", ["organization_id"]
    )
    op.create_index(
        "ix_external_connections_owner_user_id", "external_connections", ["owner_user_id"]
    )
    op.create_index("ix_external_connections_server", "external_connections", ["server_id"])
    op.create_index(
        "ix_external_connections_org_user",
        "external_connections",
        ["organization_id", "owner_user_id"],
    )
    op.create_table(
        "external_authorization_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=128), nullable=False),
        sa.Column("nonce_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_server_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_external_auth_grants_state"),
    )
    op.create_table(
        "connection_tool_grants",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_set", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["external_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("connection_id", "tool_name"),
        sa.UniqueConstraint("connection_id", "tool_name", name="uq_connection_tool_grant"),
    )
    role_rows = list(op.get_bind().execute(sa.text("SELECT id, code FROM roles")))
    role_permission_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Uuid()),
        sa.column("permission_code", sa.String(length=100)),
    )
    standard_permissions = {
        "connection:personal:create",
        "connection:personal:disconnect",
        "mcp-server:view",
        "tool:permission:use",
        "tool:erp:use",
        "tool:mes:use",
        "tool:bi:use",
    }
    op.bulk_insert(
        role_permission_table,
        [
            {"role_id": role_id, "permission_code": code}
            for role_id, role_code in role_rows
            for code, _ in p2_permissions
            if role_code == "organization_admin" or code in standard_permissions
        ],
    )


def downgrade() -> None:
    p2_codes = [
        "connection:personal:create",
        "connection:personal:disconnect",
        "connection:organization:manage",
        "mcp-server:view",
        "mcp-server:manage",
        "tool:permission:use",
        "tool:erp:use",
        "tool:mes:use",
        "tool:bi:use",
    ]
    op.execute(
        sa.delete(sa.table("role_permissions", sa.column("permission_code"))).where(
            sa.column("permission_code").in_(p2_codes)
        )
    )
    op.execute(
        sa.delete(sa.table("permissions", sa.column("code"))).where(sa.column("code").in_(p2_codes))
    )
    op.drop_table("connection_tool_grants")
    op.drop_table("external_authorization_grants")
    op.drop_index("ix_external_connections_org_user", table_name="external_connections")
    op.drop_index("ix_external_connections_server", table_name="external_connections")
    op.drop_index("ix_external_connections_owner_user_id", table_name="external_connections")
    op.drop_index("ix_external_connections_organization_id", table_name="external_connections")
    op.drop_table("external_connections")
    op.drop_index("ix_credential_references_organization_id", table_name="credential_references")
    op.drop_table("credential_references")
    op.drop_index("ix_mcp_servers_org_enabled", table_name="mcp_server_definitions")
    op.drop_index("ix_mcp_server_definitions_organization_id", table_name="mcp_server_definitions")
    op.drop_table("mcp_server_definitions")
