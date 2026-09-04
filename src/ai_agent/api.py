"""Minimal P0 API for runtime and protocol readiness inspection."""

from __future__ import annotations

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from ai_agent.config import Settings
from ai_agent.errors import AiAgentError


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or Settings()
    app = FastAPI(
        title=runtime_settings.app_name,
        version="0.1.0",
        description="P0 architecture and OAuth/MCP protocol validation service.",
    )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        try:
            runtime_settings.validate_runtime(
                require_permission_token=runtime_settings.permission_system.enabled
            )
        except AiAgentError as exc:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": str(exc)},
            )
        return JSONResponse(content={"status": "ready"})

    @app.get("/api/v1/p0/status", tags=["p0"])
    async def p0_status() -> dict[str, object]:
        permission = runtime_settings.permission_system
        return {
            "phase": "P0",
            "environment": runtime_settings.environment.value,
            "capabilities": {
                "oidc_discovery": True,
                "oauth_authorization_code_pkce": True,
                "oauth_client_credentials": True,
                "mcp_streamable_http": True,
                "mcp_initialize_list_call": True,
            },
            "integrations": {
                "agent_oidc": {"enabled": runtime_settings.oidc.enabled},
                "permission_system": {
                    "enabled": permission.enabled,
                    "has_probe_credentials": (
                        permission.has_probe_token or permission.has_service_credentials
                    ),
                    "expected_tools": list(permission.expected_tools),
                },
            },
        }

    return app
