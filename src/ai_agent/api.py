"""FastAPI application for P0 through P3 Agent platform capabilities."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_agent.audit.api import router as audit_router
from ai_agent.config import Environment, Settings
from ai_agent.connections.api import router as connection_router
from ai_agent.conversations.api import router as conversation_router
from ai_agent.errors import (
    AiAgentError,
    AuthenticationError,
    AuthorizationError,
    CircuitOpenError,
    ConflictError,
    GatewayTimeoutError,
    McpConnectionError,
    ModelProviderError,
    ProtocolValidationError,
    QuotaExceededError,
    RateLimitExceededError,
    ResourceNotFoundError,
    RunLimitError,
)
from ai_agent.identity.api import router as identity_router
from ai_agent.mcp.api import router as mcp_router
from ai_agent.observability.metrics import HTTP_DURATION, HTTP_IN_PROGRESS, HTTP_REQUESTS
from ai_agent.observability.tracing import configure_tracing
from ai_agent.permission_system.api import router as permission_system_router
from ai_agent.runtime import AppServices, build_services


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        received = 0
        messages: list[Message] = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    response = _error_response(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "request_too_large",
                        "Request body exceeds the configured size limit.",
                    )
                    await response(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        position = 0

        async def replay_receive() -> Message:
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            return await receive()

        await self._app(scope, replay_receive, send)


def create_app(settings: Settings | None = None, services: AppServices | None = None) -> FastAPI:
    runtime_settings = settings or Settings()
    if runtime_settings.environment == Environment.PRODUCTION:
        runtime_settings.validate_runtime()
    owns_services = services is None
    tracing = configure_tracing(runtime_settings.observability)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime_settings.platform.enabled:
            runtime_settings.validate_runtime()
            if app.state.services is None:
                app.state.services = build_services(runtime_settings)
                await app.state.services.start(runtime_settings)
        try:
            yield
        finally:
            if owns_services and app.state.services is not None:
                await app.state.services.close()
            tracing.close()

    app = FastAPI(
        title=runtime_settings.app_name,
        version="0.3.0",
        description=(
            "Independent identity, RBAC, connections, policy-enforced MCP Gateway and "
            "PermissionSystem read-only business closure."
        ),
        lifespan=lifespan,
        docs_url=None if runtime_settings.environment == Environment.PRODUCTION else "/docs",
        redoc_url=None if runtime_settings.environment == Environment.PRODUCTION else "/redoc",
        openapi_url=(
            None if runtime_settings.environment == Environment.PRODUCTION else "/openapi.json"
        ),
    )
    if runtime_settings.security.allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(runtime_settings.security.allowed_hosts),
            www_redirect=False,
        )
    tracing.instrument(app)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=runtime_settings.governance.max_request_body_bytes,
    )
    app.state.settings = runtime_settings
    app.state.services = services
    app.include_router(identity_router)
    app.include_router(conversation_router)
    app.include_router(connection_router)
    app.include_router(mcp_router)
    app.include_router(audit_router)
    app.include_router(permission_system_router)

    @app.middleware("http")
    async def trace_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.monotonic()
        HTTP_IN_PROGRESS.inc()
        incoming = request.headers.get("X-Trace-Id")
        try:
            trace_id = str(UUID(incoming)) if incoming else str(uuid4())
        except ValueError:
            trace_id = str(uuid4())
        request.state.trace_id = UUID(trace_id)
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        response: Response
        try:
            content_length = request.headers.get("Content-Length")
            try:
                declared_length = int(content_length) if content_length is not None else None
            except ValueError:
                response = _error_response(
                    status.HTTP_400_BAD_REQUEST,
                    "invalid_content_length",
                    "Content-Length header is invalid.",
                )
            else:
                oversized = (
                    declared_length is not None
                    and declared_length > runtime_settings.governance.max_request_body_bytes
                )
                if oversized:
                    response = _error_response(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        "request_too_large",
                        "Request body exceeds the configured size limit.",
                    )
                else:
                    response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Trace-Id"] = trace_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            if runtime_settings.environment == Environment.PRODUCTION:
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            HTTP_REQUESTS.labels(
                method=request.method,
                route=route,
                status=str(status_code),
            ).inc()
            HTTP_DURATION.labels(method=request.method, route=route).observe(
                time.monotonic() - started
            )
            HTTP_IN_PROGRESS.dec()

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

    @app.exception_handler(McpConnectionError)
    async def mcp_connection_error(_: Request, exc: McpConnectionError) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, "mcp_connection_error", str(exc))

    @app.exception_handler(ProtocolValidationError)
    async def protocol_error(_: Request, exc: ProtocolValidationError) -> JSONResponse:
        return _error_response(status.HTTP_502_BAD_GATEWAY, "mcp_protocol_error", str(exc))

    @app.exception_handler(GatewayTimeoutError)
    async def gateway_timeout(_: Request, exc: GatewayTimeoutError) -> JSONResponse:
        return _error_response(status.HTTP_504_GATEWAY_TIMEOUT, "mcp_timeout", str(exc))

    @app.exception_handler(RateLimitExceededError)
    async def gateway_rate_limit(_: Request, exc: RateLimitExceededError) -> JSONResponse:
        return _error_response(status.HTTP_429_TOO_MANY_REQUESTS, "mcp_rate_limited", str(exc))

    @app.exception_handler(QuotaExceededError)
    async def run_quota_error(_: Request, exc: QuotaExceededError) -> JSONResponse:
        return _error_response(status.HTTP_429_TOO_MANY_REQUESTS, "run_quota_exceeded", str(exc))

    @app.exception_handler(CircuitOpenError)
    async def gateway_circuit(_: Request, exc: CircuitOpenError) -> JSONResponse:
        return _error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "mcp_circuit_open", str(exc))

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready() -> JSONResponse:
        try:
            runtime_settings.validate_runtime(
                require_permission_token=(
                    runtime_settings.permission_system.enabled
                    and not runtime_settings.platform.enabled
                )
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

    @app.get("/api/v1/p2/status", tags=["p2"])
    async def p2_status() -> dict[str, object]:
        return {
            "phase": "P2",
            "enabled": runtime_settings.mcp_gateway.enabled,
            "capabilities": {
                "trusted_mcp_server_registry": True,
                "personal_oauth_connections": True,
                "organization_connections": True,
                "credential_references": True,
                "tool_catalog_isolation": True,
                "mcp_gateway": True,
                "schema_validation": True,
                "rate_limit_timeout_circuit_breaker": True,
                "trace_and_tool_audit": True,
            },
        }

    @app.get("/api/v1/p3/status", tags=["p3"])
    async def p3_status() -> dict[str, object]:
        permission = runtime_settings.permission_system
        return {
            "phase": "P3",
            "enabled": permission.enabled and runtime_settings.mcp_gateway.enabled,
            "capabilities": {
                "permission_system_personal_connection": True,
                "readonly_tool_allowlist": True,
                "permission_denial_fail_closed": True,
                "run_tool_calling": True,
                "citation_persistence": True,
                "golden_question_evaluation": True,
            },
            "expected_tools": list(permission.expected_tools),
        }

    @app.get("/api/v1/p5/status", tags=["p5"])
    async def p5_status() -> dict[str, object]:
        governance = runtime_settings.governance
        runs_enabled = runtime_settings.platform.runs_enabled
        worker_available = runtime_settings.execution.mode.value == "embedded"
        if app.state.services is not None and app.state.services.quota is not None:
            runs_enabled = runs_enabled and await app.state.services.quota.runs_enabled()
        if (
            app.state.services is not None
            and app.state.services.worker_registry is not None
            and not worker_available
        ):
            worker_available = await app.state.services.worker_registry.has_live_workers()
            runs_enabled = runs_enabled and worker_available
        return {
            "phase": "P5",
            "enabled": governance.enabled,
            "runs_enabled": runs_enabled,
            "execution_mode": runtime_settings.execution.mode.value,
            "worker_available": worker_available,
            "capabilities": {
                "distributed_quotas": governance.quota.enabled,
                "durable_credential_vault": runtime_settings.credentials.backend.value
                == "hashicorp_vault",
                "distributed_mcp_policies": True,
                "graceful_shutdown_and_recovery": True,
                "durable_run_jobs": runtime_settings.execution.mode.value == "external_worker",
                "prometheus_metrics_and_alerts": True,
                "optional_otlp_tracing": runtime_settings.observability.tracing_enabled,
                "audit_retention": True,
            },
            "audit_retention_days": governance.audit_retention_days,
        }

    return app


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
