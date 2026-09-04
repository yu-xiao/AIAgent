from __future__ import annotations

import httpx

from ai_agent.api import create_app
from ai_agent.config import PermissionSystemSettings, Settings


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
