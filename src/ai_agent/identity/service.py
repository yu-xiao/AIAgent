"""User provisioning, organization membership and deterministic RBAC."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.audit.service import AuditService
from ai_agent.errors import AuthorizationError, ConflictError, ResourceNotFoundError
from ai_agent.persistence.models import (
    MemberRole,
    Organization,
    OrganizationMember,
    Permission,
    Role,
    RolePermission,
    User,
)

AGENT_USE = "agent:use"
AGENT_MANAGE = "agent:manage"
AGENT_VIEW = "agent:view"
AGENT_DRAFT_WRITE = "agent:draft:write"
AGENT_VERSION_CREATE = "agent:version:create"
AGENT_RELEASE = "agent:release"
AGENT_RELEASE_BYPASS = "agent:release:bypass"
EVAL_VIEW = "eval:view"
EVAL_DATASET_WRITE = "eval:dataset:write"
EVAL_RUN_CREATE = "eval:run:create"
EVAL_POLICY_MANAGE = "eval:policy:manage"
AUDIT_VIEW = "audit:view"
ORGANIZATION_MEMBER_MANAGE = "organization:member:manage"
CONNECTION_PERSONAL_CREATE = "connection:personal:create"
CONNECTION_PERSONAL_DISCONNECT = "connection:personal:disconnect"
CONNECTION_ORGANIZATION_MANAGE = "connection:organization:manage"
MCP_SERVER_VIEW = "mcp-server:view"
MCP_SERVER_MANAGE = "mcp-server:manage"
TOOL_PERMISSION_USE = "tool:permission:use"
TOOL_ERP_USE = "tool:erp:use"
TOOL_MES_USE = "tool:mes:use"
TOOL_BI_USE = "tool:bi:use"
TOOL_PERMISSION_BY_SYSTEM = {
    "permission-system": TOOL_PERMISSION_USE,
    "permission": TOOL_PERMISSION_USE,
    "erp": TOOL_ERP_USE,
    "kingdee": TOOL_ERP_USE,
    "mes": TOOL_MES_USE,
    "bi": TOOL_BI_USE,
}
ADMIN_ROLE = "organization_admin"
MEMBER_ROLE = "member"

PERMISSION_DEFINITIONS = {
    AGENT_USE: "Use the organization's Agent.",
    AGENT_MANAGE: "Manage Agent definitions and runtime switches.",
    AGENT_VIEW: "View the organization's managed Agents.",
    AGENT_DRAFT_WRITE: "Create and update Agent drafts.",
    AGENT_VERSION_CREATE: "Create immutable Agent versions.",
    AGENT_RELEASE: "Release and roll back evaluated Agent versions.",
    AGENT_RELEASE_BYPASS: "Bypass an Agent release gate with an audited reason.",
    EVAL_VIEW: "View evaluation datasets, policies, and results.",
    EVAL_DATASET_WRITE: "Create and version evaluation datasets.",
    EVAL_RUN_CREATE: "Run evaluations for immutable Agent versions.",
    EVAL_POLICY_MANAGE: "Manage Agent evaluation gate policies.",
    AUDIT_VIEW: "View organization audit records.",
    ORGANIZATION_MEMBER_MANAGE: "Manage organization members and roles.",
    CONNECTION_PERSONAL_CREATE: "Create personal external connections.",
    CONNECTION_PERSONAL_DISCONNECT: "Disconnect personal external connections.",
    CONNECTION_ORGANIZATION_MANAGE: "Manage organization shared connections.",
    MCP_SERVER_VIEW: "View registered MCP servers.",
    MCP_SERVER_MANAGE: "Register and manage MCP servers.",
    TOOL_PERMISSION_USE: "Use PermissionSystem MCP tools.",
    TOOL_ERP_USE: "Use ERP MCP tools.",
    TOOL_MES_USE: "Use MES MCP tools.",
    TOOL_BI_USE: "Use BI MCP tools.",
}


@dataclass(frozen=True, slots=True)
class OrganizationAccess:
    organization: Organization
    member: OrganizationMember
    permissions: frozenset[str]


class IdentityService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit or AuditService()

    async def upsert_oidc_user(
        self, *, issuer: str, subject: str, display_name: str, email: str | None
    ) -> User:
        async with self._session_factory() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.issuer == issuer, User.subject == subject)
            )
            if user is None:
                user = User(
                    issuer=issuer,
                    subject=subject,
                    display_name=display_name,
                    email=email,
                )
                session.add(user)
                await session.flush()
            else:
                user.display_name = display_name
                user.email = email
            return user

    async def get_user(self, user_id: UUID) -> User:
        async with self._session_factory() as session:
            user = await session.get(User, user_id)
            if user is None or not user.is_active:
                raise ResourceNotFoundError("Authenticated platform user is unavailable.")
            return user

    async def record_logout(self, user_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(
                self._audit.record(
                    organization_id=None,
                    actor_user_id=user_id,
                    action="auth.logout",
                    resource_type="session",
                    resource_id=None,
                    details={"source": "web"},
                )
            )

    async def require_tool_access(
        self, user_id: UUID, organization_id: UUID, system_code: str
    ) -> None:
        permission = TOOL_PERMISSION_BY_SYSTEM.get(system_code.lower())
        if permission:
            await self.access(user_id, organization_id, permission)
        else:
            await self.access(user_id, organization_id, AGENT_USE)

    async def create_organization(self, user_id: UUID, name: str) -> Organization:
        async with self._session_factory() as session, session.begin():
            organization = Organization(name=name)
            session.add(organization)
            await session.flush()
            member = OrganizationMember(organization_id=organization.id, user_id=user_id)
            session.add(member)
            await session.flush()

            for code, description in PERMISSION_DEFINITIONS.items():
                if await session.get(Permission, code) is None:
                    session.add(Permission(code=code, description=description))
            await session.flush()

            admin = Role(
                organization_id=organization.id,
                code=ADMIN_ROLE,
                display_name="Organization administrator",
            )
            standard = Role(
                organization_id=organization.id,
                code=MEMBER_ROLE,
                display_name="Member",
            )
            session.add_all([admin, standard])
            await session.flush()
            session.add_all(
                [
                    RolePermission(role_id=admin.id, permission_code=code)
                    for code in PERMISSION_DEFINITIONS
                ]
                + [
                    RolePermission(role_id=standard.id, permission_code=code)
                    for code in (
                        AGENT_USE,
                        CONNECTION_PERSONAL_CREATE,
                        CONNECTION_PERSONAL_DISCONNECT,
                        MCP_SERVER_VIEW,
                        TOOL_PERMISSION_USE,
                        TOOL_ERP_USE,
                        TOOL_MES_USE,
                        TOOL_BI_USE,
                    )
                ]
                + [MemberRole(member_id=member.id, role_id=admin.id)]
            )
            session.add(
                self._audit.record(
                    organization_id=organization.id,
                    actor_user_id=user_id,
                    action="organization.created",
                    resource_type="organization",
                    resource_id=str(organization.id),
                    details={"name": name},
                )
            )
            return organization

    async def list_organizations(self, user_id: UUID) -> list[Organization]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(Organization)
                .join(OrganizationMember)
                .join(User, User.id == OrganizationMember.user_id)
                .where(OrganizationMember.user_id == user_id, User.is_active.is_(True))
                .order_by(Organization.name, Organization.id)
            )
            return list(result)

    async def access(
        self, user_id: UUID, organization_id: UUID, required_permission: str | None = None
    ) -> OrganizationAccess:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Organization, OrganizationMember)
                    .join(
                        OrganizationMember,
                        OrganizationMember.organization_id == Organization.id,
                    )
                    .join(User, User.id == OrganizationMember.user_id)
                    .where(
                        Organization.id == organization_id,
                        OrganizationMember.user_id == user_id,
                        User.is_active.is_(True),
                    )
                )
            ).one_or_none()
            if row is None:
                raise AuthorizationError("Organization access denied.")
            organization, member = row
            permissions = frozenset(
                await session.scalars(
                    select(RolePermission.permission_code)
                    .join(Role, Role.id == RolePermission.role_id)
                    .join(MemberRole, MemberRole.role_id == Role.id)
                    .where(
                        MemberRole.member_id == member.id,
                        Role.organization_id == organization_id,
                    )
                )
            )
            if required_permission and required_permission not in permissions:
                raise AuthorizationError("Required platform permission is missing.")
            return OrganizationAccess(organization, member, permissions)

    async def list_members(
        self, actor_id: UUID, organization_id: UUID
    ) -> list[tuple[OrganizationMember, User, list[str]]]:
        await self.access(actor_id, organization_id, ORGANIZATION_MEMBER_MANAGE)
        async with self._session_factory() as session:
            pairs = (
                await session.execute(
                    select(OrganizationMember, User)
                    .join(User, User.id == OrganizationMember.user_id)
                    .where(OrganizationMember.organization_id == organization_id)
                    .order_by(User.display_name, User.id)
                )
            ).all()
            result: list[tuple[OrganizationMember, User, list[str]]] = []
            for member, user in pairs:
                roles = list(
                    await session.scalars(
                        select(Role.code)
                        .join(MemberRole, MemberRole.role_id == Role.id)
                        .where(MemberRole.member_id == member.id)
                        .order_by(Role.code)
                    )
                )
                result.append((member, user, roles))
            return result

    async def add_member(
        self, actor_id: UUID, organization_id: UUID, user_id: UUID
    ) -> OrganizationMember:
        await self.access(actor_id, organization_id, ORGANIZATION_MEMBER_MANAGE)
        async with self._session_factory() as session, session.begin():
            user = await session.get(User, user_id)
            if user is None:
                raise ResourceNotFoundError("Platform user not found.")
            if not user.is_active:
                raise ResourceNotFoundError("Platform user is not active.")
            existing = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.user_id == user_id,
                )
            )
            if existing is not None:
                raise ConflictError("User is already an organization member.")
            role = await session.scalar(
                select(Role).where(
                    Role.organization_id == organization_id, Role.code == MEMBER_ROLE
                )
            )
            if role is None:
                raise ConflictError("Organization member role is unavailable.")
            member = OrganizationMember(organization_id=organization_id, user_id=user_id)
            session.add(member)
            await session.flush()
            session.add(MemberRole(member_id=member.id, role_id=role.id))
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="organization.member_added",
                    resource_type="organization_member",
                    resource_id=str(member.id),
                    details={"user_id": str(user_id)},
                )
            )
            return member

    async def remove_member(
        self, actor_id: UUID, organization_id: UUID, member_id: UUID
    ) -> None:
        await self.access(actor_id, organization_id, ORGANIZATION_MEMBER_MANAGE)
        async with self._session_factory() as session, session.begin():
            member = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.id == member_id,
                    OrganizationMember.organization_id == organization_id,
                )
            )
            if member is None:
                raise ResourceNotFoundError("Organization member not found.")
            admin_count = await session.scalar(
                select(func.count(func.distinct(MemberRole.member_id)))
                .select_from(MemberRole)
                .join(Role, Role.id == MemberRole.role_id)
                .where(
                    Role.organization_id == organization_id,
                    Role.code == ADMIN_ROLE,
                )
            )
            member_is_admin = await session.scalar(
                select(func.count())
                .select_from(MemberRole)
                .join(Role, Role.id == MemberRole.role_id)
                .where(MemberRole.member_id == member_id, Role.code == ADMIN_ROLE)
            )
            if member_is_admin and admin_count == 1:
                raise ConflictError("The organization must retain at least one administrator.")
            await session.execute(delete(MemberRole).where(MemberRole.member_id == member_id))
            await session.delete(member)
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="organization.member_removed",
                    resource_type="organization_member",
                    resource_id=str(member_id),
                    details={"user_id": str(member.user_id)},
                )
            )

    async def set_member_roles(
        self,
        actor_id: UUID,
        organization_id: UUID,
        member_id: UUID,
        role_codes: set[str],
    ) -> None:
        await self.access(actor_id, organization_id, ORGANIZATION_MEMBER_MANAGE)
        if not role_codes:
            raise ConflictError("At least one role is required.")
        async with self._session_factory() as session, session.begin():
            member = await session.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.id == member_id,
                    OrganizationMember.organization_id == organization_id,
                )
            )
            if member is None:
                raise ResourceNotFoundError("Organization member not found.")
            member_user = await session.get(User, member.user_id)
            if member_user is None or not member_user.is_active:
                raise ResourceNotFoundError("Platform user is not active.")
            roles = list(
                await session.scalars(
                    select(Role).where(
                        Role.organization_id == organization_id,
                        Role.code.in_(role_codes),
                    )
                )
            )
            if {role.code for role in roles} != role_codes:
                raise ResourceNotFoundError("One or more organization roles do not exist.")
            current_admin = await session.scalar(
                select(func.count())
                .select_from(MemberRole)
                .join(Role, Role.id == MemberRole.role_id)
                .where(MemberRole.member_id == member_id, Role.code == ADMIN_ROLE)
            )
            if current_admin and ADMIN_ROLE not in role_codes:
                admin_count = await session.scalar(
                    select(func.count(func.distinct(MemberRole.member_id)))
                    .select_from(MemberRole)
                    .join(Role, Role.id == MemberRole.role_id)
                    .where(Role.organization_id == organization_id, Role.code == ADMIN_ROLE)
                )
                if admin_count == 1:
                    raise ConflictError("The organization must retain at least one administrator.")
            await session.execute(delete(MemberRole).where(MemberRole.member_id == member_id))
            session.add_all([MemberRole(member_id=member_id, role_id=role.id) for role in roles])
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="organization.member_roles_updated",
                    resource_type="organization_member",
                    resource_id=str(member_id),
                    details={"roles": sorted(role_codes)},
                )
            )
