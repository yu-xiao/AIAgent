"""FastAPI application for P0 protocol probes and the P1 Agent platform."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from ai_agent.audit.api import router as audit_router
from ai_agent.config import Settings
from ai_agent.conversations.api import router as conversation_router
from ai_agent.errors import (
    AiAgentError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ModelProviderError,
    ResourceNotFoundError,
    RunLimitError,
)
from ai_agent.identity.api import router as identity_router
from ai_agent.runtime import AppServices, build_services


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    runtime_settings = settings or Settings()
    owns_services = services is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime_settings.platform.enabled:
            runtime_settings.validate_runtime()
            if app.state.services is None:
                app.state.services = build_services(runtime_settings)
        yield
        if owns_services and app.state.services is not None:
            await app.state.services.close()

    app = FastAPI(
        title=runtime_settings.app_name,
        version="0.2.0",
        description="P1 independent identity, RBAC and observable single-Agent Run service.",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.services = services
    app.include_router(identity_router)
    app.include_router(conversation_router)
    app.include_router(audit_router)

    @app.middleware("http")
    async def trace_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get("X-Trace-Id")
        try:
            trace_id = str(UUID(incoming)) if incoming else str(uuid4())
        except ValueError:
            trace_id = str(uuid4())
        request.state.trace_id = UUID(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(AuthenticationError)
    async def authentication_error(_: Request, exc: AuthenticationError) -> JSONResponse:
        return _error_response(status.HTTP_401_UNAUTHORIZED, "authentication_failed", str(exc))

    @app.exception_handler(AuthorizationError)
    async def authorization_error(_: Request, exc: AuthorizationError) -> JSONResponse:
        return _error_response(status.HTTP_403_FORBIDDEN, "access_denied", str(exc))

    @app.exception_handler(ResourceNotFoundError)
    async def not_found_error(_: Request, exc: ResourceNotFoundError) -> JSONResponse:
        return _error_response(status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict_error(_: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(RunLimitError)
    async def run_limit_error(_: Request, exc: RunLimitError) -> JSONResponse:
        return _error_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "run_limit", str(exc))

    @app.exception_handler(ModelProviderError)
    async def model_error(_: Request, exc: ModelProviderError) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, "model_provider_error", str(exc))

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        try:
            runtime_settings.validate_runtime(
                require_permission_token=runtime_settings.permission_system.enabled
            )
            if runtime_settings.platform.enabled:
                if app.state.services is None:
                    raise RuntimeError("P1 services have not started.")
                await app.state.services.ping()
        except (AiAgentError, RedisError, SQLAlchemyError, RuntimeError, OSError):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready"},
            )
        return JSONResponse(content={"status": "ready"})

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

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

    @app.get("/api/v1/p1/status", tags=["p1"])
    async def p1_status() -> dict[str, object]:
        return {
            "phase": "P1",
            "enabled": runtime_settings.platform.enabled,
            "runs_enabled": runtime_settings.platform.runs_enabled,
            "model_configured": runtime_settings.model.enabled,
            "capabilities": {
                "independent_oidc_session": True,
                "organization_rbac": True,
                "conversation_history": True,
                "langgraph_single_agent": True,
                "sse_run_events": True,
                "run_cancellation": True,
                "audit": True,
                "hard_limits": True,
            },
        }

    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
