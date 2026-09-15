from __future__ import annotations

import time
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select

from ai_agent.errors import AuthorizationError, ConflictError, ResourceNotFoundError
from ai_agent.identity.sessions import RedisSessionStore
from ai_agent.persistence.models import AuditLog, OrganizationMember, User
from tests.p1_conftest import PlatformRuntime


async def test_logout_invalidates_session_and_writes_audit(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime

    response = await runtime.client.post("/api/v1/auth/logout", headers=runtime.headers)

    assert response.status_code == 204
    assert (await runtime.client.get("/api/v1/me")).status_code == 401
    async with runtime.services.database.session_factory() as session:
        record = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "auth.logout",
                AuditLog.actor_user_id == runtime.user.id,
            )
        )
    assert record is not None
    assert record.resource_id is None
    assert record.details == {"source": "web"}


async def test_inactive_user_session_is_rejected_and_revoked(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    async with runtime.services.database.session_factory() as session, session.begin():
        user = await session.get(User, runtime.user.id)
        assert user is not None
        user.is_active = False

    response = await runtime.client.get("/api/v1/me")

    assert response.status_code == 401
    assert await runtime.services.sessions.get_session(runtime.session_id) is None


async def test_member_can_be_removed_and_loses_organization_access(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    identities = runtime.services.identities
    member_user = await identities.upsert_oidc_user(
        issuer=runtime.settings.oidc.issuer,
        subject="removable-member",
        display_name="Removable Member",
        email=None,
    )
    member = await identities.add_member(
        runtime.user.id,
        runtime.organization.id,
        member_user.id,
    )

    response = await runtime.client.delete(
        f"/api/v1/organizations/current/members/{member.id}",
        headers=runtime.headers,
    )

    assert response.status_code == 204
    with pytest.raises(AuthorizationError):
        await identities.access(member_user.id, runtime.organization.id)
    async with runtime.services.database.session_factory() as session:
        assert await session.get(OrganizationMember, member.id) is None


async def test_last_admin_cannot_be_removed(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    identities = runtime.services.identities
    members = await identities.list_members(runtime.user.id, runtime.organization.id)
    owner_member = next(member for member, user, _ in members if user.id == runtime.user.id)

    with pytest.raises(ConflictError, match="at least one administrator"):
        await identities.remove_member(
            runtime.user.id,
            runtime.organization.id,
            owner_member.id,
        )


async def test_inactive_user_cannot_be_added_to_organization(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    identities = runtime.services.identities
    inactive_user = await identities.upsert_oidc_user(
        issuer=runtime.settings.oidc.issuer,
        subject="inactive-member",
        display_name="Inactive Member",
        email=None,
    )
    async with runtime.services.database.session_factory() as session, session.begin():
        user = await session.get(User, inactive_user.id)
        assert user is not None
        user.is_active = False

    with pytest.raises(ResourceNotFoundError, match="not active"):
        await identities.add_member(
            runtime.user.id,
            runtime.organization.id,
            inactive_user.id,
        )


async def test_session_store_can_revoke_all_user_sessions(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    first_id, _ = await runtime.services.sessions.create_session(
        runtime.user.id, runtime.settings.platform.session_ttl_seconds
    )
    second_id, _ = await runtime.services.sessions.create_session(
        runtime.user.id, runtime.settings.platform.session_ttl_seconds
    )

    deleted = await runtime.services.sessions.delete_user_sessions(runtime.user.id)

    assert deleted == 3
    assert await runtime.services.sessions.get_session(first_id) is None
    assert await runtime.services.sessions.get_session(second_id) is None


async def test_redis_session_index_removes_expired_entries() -> None:
    redis = FakeRedis()
    store = RedisSessionStore(redis)  # type: ignore[arg-type]
    user_id = uuid4()
    first_id, _ = await store.create_session(user_id, 300)
    index_key = f"user-sessions:{user_id}"
    await redis.zadd(index_key, {"expired-session": time.time() - 1})

    second_id, _ = await store.create_session(user_id, 300)

    assert await redis.zscore(index_key, "expired-session") is None
    assert await store.delete_user_sessions(user_id) == 2
    assert await store.get_session(first_id) is None
    assert await store.get_session(second_id) is None
    await redis.aclose()
