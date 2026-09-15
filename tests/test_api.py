from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_agent.api import create_app
from ai_agent.config import (
    Environment,
    PermissionSystemSettings,
    PlatformSettings,
    SecuritySettings,
    Settings,
)
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


async def test_built_web_app_is_served_with_security_headers(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text("<main>Enterprise AI Agent</main>", encoding="utf-8")
    (assets / "app.js").write_text("export {};", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        platform=PlatformSettings(web_dist_path=str(tmp_path)),
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        index = await client.get("/")
        script = await client.get("/assets/app.js")

    assert index.status_code == 200
    assert "Enterprise AI Agent" in index.text
    assert script.status_code == 200
    assert "default-src 'self'" in index.headers["Content-Security-Policy"]
