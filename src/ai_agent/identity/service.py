"""User provisioning, organization membership and deterministic RBAC."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.errors import AuthorizationError, ConflictError, ResourceNotFoundError
from ai_agent.persistence.models import (
    AuditLog,
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
AUDIT_VIEW = "audit:view"
ORGANIZATION_MEMBER_MANAGE = "organization:member:manage"
ADMIN_ROLE = "organization_admin"
MEMBER_ROLE = "member"

PERMISSION_DEFINITIONS = {
    AGENT_USE: "Use the organization's Agent.",
    AGENT_MANAGE: "Manage Agent definitions and runtime switches.",
    AUDIT_VIEW: "View organization audit records.",
    ORGANIZATION_MEMBER_MANAGE: "Manage organization members and roles.",
}


@dataclass(frozen=True, slots=True)
class OrganizationAccess:
    organization: Organization
    member: OrganizationMember
    permissions: frozenset[str]


class IdentityService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
                + [RolePermission(role_id=standard.id, permission_code=AGENT_USE)]
                + [MemberRole(member_id=member.id, role_id=admin.id)]
            )
            session.add(
                AuditLog(
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
                .where(OrganizationMember.user_id == user_id)
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
                    .where(
                        Organization.id == organization_id,
                        OrganizationMember.user_id == user_id,
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
            if await session.get(User, user_id) is None:
                raise ResourceNotFoundError("Platform user not found.")
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
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="organization.member_added",
                    resource_type="organization_member",
                    resource_id=str(member.id),
                    details={"user_id": str(user_id)},
                )
            )
            return member

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
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="organization.member_roles_updated",
                    resource_type="organization_member",
                    resource_id=str(member_id),
                    details={"roles": sorted(role_codes)},
                )
            )
