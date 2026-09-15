from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import select

from ai_agent.config import LocalAuthSettings
from ai_agent.identity.local import LocalAuthService, hash_password, verify_password
from ai_agent.persistence.models import AuditLog, LocalCredential
from tests.p1_conftest import PlatformRuntime


@pytest.fixture
async def local_auth_runtime(
    platform_runtime: PlatformRuntime,
) -> AsyncIterator[PlatformRuntime]:
    runtime = platform_runtime
    runtime.settings.local_auth = LocalAuthSettings(enabled=True, registration_enabled=True)
    runtime.services.redis = FakeRedis()
    runtime.services.local_auth = LocalAuthService(
        runtime.services.database,
        runtime.services.identities,
        runtime.services.audit,
    )
    runtime.client.cookies.clear()
    yield runtime


def test_password_hash_is_salted_and_rejects_invalid_values() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)
    assert not verify_password("password", "invalid")
    assert not verify_password("password", "scrypt-v1$not-hex$also-not-hex")


async def test_register_login_and_logout_flow(
    local_auth_runtime: PlatformRuntime,
) -> None:
    runtime = local_auth_runtime
    headers = {"X-Requested-With": "ai-agent-web"}

    options = await runtime.client.get("/api/v1/auth/options")
    assert options.status_code == 200
    assert options.json() == {
        "local_enabled": True,
        "registration_enabled": True,
        "oidc_enabled": True,
    }

    response = await runtime.client.post(
        "/api/v1/auth/register",
        headers=headers,
        json={
            "email": " New.User@Example.Test ",
            "display_name": "测试用户",
            "password": "local-test-password-123",
        },
    )

    assert response.status_code == 204
    assert "HttpOnly" in response.headers["set-cookie"]
    me = await runtime.client.get("/api/v1/me")
    assert me.status_code == 200
    profile = me.json()
    assert profile["email"] == "new.user@example.test"
    assert profile["display_name"] == "测试用户"
    assert len(profile["organizations"]) == 1
    assert profile["organizations"][0]["name"] == "测试用户的工作区"

    access = await runtime.services.identities.access(
        UUID(profile["id"]), UUID(profile["organizations"][0]["id"])
    )
    assert "organization:member:manage" in access.permissions
    assert "agent:use" in access.permissions
    async with runtime.services.database.session_factory() as session:
        credential = await session.get(LocalCredential, "new.user@example.test")
        registered = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "auth.register",
                AuditLog.actor_user_id == UUID(profile["id"]),
            )
        )
    assert credential is not None
    assert "local-test-password-123" not in credential.password_hash
    assert registered is not None

    duplicate = await runtime.client.post(
        "/api/v1/auth/register",
        headers=headers,
        json={
            "email": "new.user@example.test",
            "display_name": "另一个用户",
            "password": "another-local-password-123",
        },
    )
    assert duplicate.status_code == 409

    logout = await runtime.client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": profile["csrf_token"]},
    )
    assert logout.status_code == 204
    assert (await runtime.client.get("/api/v1/me")).status_code == 401

    wrong_password = await runtime.client.post(
        "/api/v1/auth/local/login",
        headers=headers,
        json={"email": "new.user@example.test", "password": "wrong-password"},
    )
    assert wrong_password.status_code == 401

    login = await runtime.client.post(
        "/api/v1/auth/local/login",
        headers=headers,
        json={
            "email": "NEW.USER@example.test",
            "password": "local-test-password-123",
        },
    )
    assert login.status_code == 204
    assert (await runtime.client.get("/api/v1/me")).status_code == 200


async def test_local_auth_guards_and_tenant_isolation(
    local_auth_runtime: PlatformRuntime,
) -> None:
    runtime = local_auth_runtime
    payload = {
        "email": "first@example.test",
        "display_name": "First",
        "password": "first-local-password-123",
    }

    missing_header = await runtime.client.post("/api/v1/auth/register", json=payload)
    assert missing_header.status_code == 403
    invalid_origin = await runtime.client.post(
        "/api/v1/auth/register",
        headers={"X-Requested-With": "ai-agent-web", "Origin": "https://evil.example"},
        json=payload,
    )
    assert invalid_origin.status_code == 403

    valid_headers = {"X-Requested-With": "ai-agent-web"}
    assert (
        await runtime.client.post("/api/v1/auth/register", headers=valid_headers, json=payload)
    ).status_code == 204
    first_profile = (await runtime.client.get("/api/v1/me")).json()
    first_organization_id = first_profile["organizations"][0]["id"]

    assert (
        await runtime.client.post(
            "/api/v1/auth/register",
            headers=valid_headers,
            json={
                "email": "second@example.test",
                "display_name": "Second",
                "password": "second-local-password-123",
            },
        )
    ).status_code == 204
    second_profile = (await runtime.client.get("/api/v1/me")).json()
    assert second_profile["organizations"][0]["id"] != first_organization_id

    denied = await runtime.client.get(
        "/api/v1/organizations/current",
        headers={"X-Organization-Id": first_organization_id},
    )
    assert denied.status_code == 403


async def test_disabled_local_auth_is_not_exposed(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime

    options = await runtime.client.get("/api/v1/auth/options")
    assert options.json()["local_enabled"] is False
    response = await runtime.client.post(
        "/api/v1/auth/local/login",
        headers={"X-Requested-With": "ai-agent-web"},
        json={"email": "user@example.test", "password": "password"},
    )
    assert response.status_code == 404
