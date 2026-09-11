from __future__ import annotations

import httpx
import pytest

from ai_agent.api import create_app
from ai_agent.config import Environment, PermissionSystemSettings, SecuritySettings, Settings
from ai_agent.errors import ConfigurationError


async def test_health_and_p0_status() -> None:
    app = create_app(Settings(_env_file=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        p0 = await client.get("/api/v1/p0/status")

    assert live.json() == {"status": "ok"}
    assert ready.json() == {"status": "ready"}
    assert p0.json()["capabilities"]["mcp_initialize_list_call"] is True


async def test_enabled_integration_without_credentials_is_not_ready() -> None:
    settings = Settings(
        _env_file=None,
        permission_system=PermissionSystemSettings(enabled=True),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


async def test_production_hides_debug_documents_and_rejects_untrusted_hosts() -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.PRODUCTION,
        security=SecuritySettings(
            allowed_hosts=("agent.example.test",),
            trusted_proxy_ips=("172.20.0.0/16",),
        ),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://agent.example.test",
    ) as client:
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        openapi = await client.get("/openapi.json")
        invalid_host = await client.get("/health/live", headers={"Host": "attacker.example"})

    assert docs.status_code == 404
    assert redoc.status_code == 404
    assert openapi.status_code == 404
    assert invalid_host.status_code == 400


def test_production_app_creation_fails_closed_without_boundary_config() -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION)

    with pytest.raises(ConfigurationError, match="allowed host"):
        create_app(settings)


async def test_development_keeps_debug_documents() -> None:
    app = create_app(Settings(_env_file=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        docs = await client.get("/docs")
        openapi = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert openapi.status_code == 200
