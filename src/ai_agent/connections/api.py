"""Connection center HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ai_agent.connections.service import ConnectionService
from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId, require_csrf
from ai_agent.persistence.models import ConnectionOwnership, ConnectionStatus

router = APIRouter(prefix="/api/v1", tags=["connections"])


class ConnectionView(BaseModel):
    id: UUID
    server_id: UUID
    owner_user_id: UUID | None
    ownership: ConnectionOwnership
    status: ConnectionStatus
    external_issuer: str | None
    external_subject: str | None
    scopes: list[str]
    allowed_tools: list[str]
    allowed_tool_sets: list[str]
    expires_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_code: str = Field(min_length=2, max_length=100)
    access_token: SecretStr | None = Field(default=None, min_length=1)
    client_id: str | None = Field(default=None, min_length=1, max_length=300)
    client_secret: SecretStr | None = None
    token_url: str | None = Field(default=None, max_length=2_000)
    scope: str | None = Field(default=None, max_length=500)
    allowed_tools: list[str] = Field(min_length=1, max_length=500)
    allowed_tool_sets: list[str] = Field(default_factory=list, max_length=100)


class AuthorizationView(BaseModel):
    authorization_url: str


@router.get("/connections", response_model=list[ConnectionView])
async def list_connections(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[ConnectionView]:
    service = _connection_service(request)
    return [
        ConnectionView.model_validate(item)
        for item in await service.list_connections(identity.session.user_id, organization_id)
    ]


@router.post(
    "/connections/{server_code}/authorize",
    response_model=AuthorizationView,
)
async def authorize_connection(
    server_code: str,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> AuthorizationView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    url = await _connection_service(request).begin_personal_authorization(
        identity.session.user_id,
        organization_id,
        server_code,
        trace_id=request.state.trace_id,
    )
    return AuthorizationView(authorization_url=url)


@router.get("/connections/{server_code}/callback")
async def connection_callback(
    server_code: str,
    request: Request,
    identity: CurrentIdentity,
    code: str = Query(min_length=1, max_length=4_096),
    state: str = Query(min_length=20, max_length=512),
) -> RedirectResponse:
    await _connection_service(request).finish_personal_authorization(
        identity.session.user_id,
        server_code,
        code=code,
        state=state,
        trace_id=request.state.trace_id,
    )
    return RedirectResponse(
        request.app.state.settings.platform.post_login_redirect_uri,
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_connection(
    connection_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> Response:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    await _connection_service(request).disconnect(
        identity.session.user_id,
        organization_id,
        connection_id,
        trace_id=request.state.trace_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connections/{connection_id}/refresh", response_model=ConnectionView)
async def refresh_connection(
    connection_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> ConnectionView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    connection = await _connection_service(request).refresh_connection(
        identity.session.user_id,
        organization_id,
        connection_id,
        trace_id=request.state.trace_id,
    )
    return ConnectionView.model_validate(connection)


@router.post("/organization-connections", response_model=ConnectionView, status_code=201)
async def create_organization_connection(
    payload: OrganizationConnectionCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> ConnectionView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    connection = await _connection_service(request).create_organization_connection(
        identity.session.user_id,
        organization_id,
        payload.server_code,
        access_token=payload.access_token,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        token_url=payload.token_url,
        scope=payload.scope,
        allowed_tools=payload.allowed_tools,
        allowed_tool_sets=payload.allowed_tool_sets,
        trace_id=request.state.trace_id,
    )
    return ConnectionView.model_validate(connection)


def _connection_service(request: Request) -> ConnectionService:
    if not request.app.state.settings.mcp_gateway.enabled:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P2 connection center is disabled.")
    service = request.app.state.services.connections
    if service is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P2 connection center is disabled.")
    return cast(ConnectionService, service)
