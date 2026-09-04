"""MCP Server administration and Tool Catalog API."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict

from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId, require_csrf
from ai_agent.mcp.models import RunContext, ToolDefinition
from ai_agent.mcp.registry import McpServerRegistry, McpServerSpec, McpServerUpdate
from ai_agent.persistence.models import McpAuthMode, McpTransport

router = APIRouter(prefix="/api/v1", tags=["mcp"])


class McpServerView(BaseModel):
    id: UUID
    code: str
    display_name: str
    system_code: str
    mcp_url: str
    transport: McpTransport
    auth_mode: McpAuthMode
    credential_header: str
    authorization_server: str | None
    required_scope: str
    allowed_tools: list[str]
    tool_sets: dict[str, list[str]]
    risk_level: str
    timeout_seconds: float
    rate_limit_per_minute: int
    max_concurrency: int
    response_size_limit: int
    enabled: bool
    config_version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ToolListView(BaseModel):
    tools: list[ToolDefinition]


@router.get("/admin/mcp-servers", response_model=list[McpServerView])
async def list_mcp_servers(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[McpServerView]:
    registry = _registry(request)
    return [
        McpServerView.model_validate(item)
        for item in await registry.list(identity.session.user_id, organization_id)
    ]


@router.post("/admin/mcp-servers", response_model=McpServerView, status_code=201)
async def create_mcp_server(
    payload: McpServerSpec,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> McpServerView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    server = await _registry(request).create(
        identity.session.user_id,
        organization_id,
        payload,
        trace_id=request.state.trace_id,
    )
    return McpServerView.model_validate(server)


@router.put("/admin/mcp-servers/{server_id}", response_model=McpServerView)
async def update_mcp_server(
    server_id: UUID,
    payload: McpServerUpdate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> McpServerView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    server = await _registry(request).update(
        identity.session.user_id,
        organization_id,
        server_id,
        payload,
        trace_id=request.state.trace_id,
    )
    catalog = request.app.state.services.catalog
    if catalog is not None:
        await catalog.invalidate_server(organization_id, server.id)
    return McpServerView.model_validate(server)


@router.get("/connections/tools", response_model=ToolListView)
async def list_connection_tools(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    tool_set: str = "",
) -> ToolListView:
    if not request.app.state.settings.mcp_gateway.enabled:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P2 MCP Gateway is disabled.")
    catalog = request.app.state.services.catalog
    if catalog is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P2 MCP Gateway is disabled.")
    context = RunContext(
        organization_id=organization_id,
        user_id=identity.session.user_id,
        trace_id=str(request.state.trace_id),
    )
    return ToolListView(tools=await catalog.list_tools(context, tool_set))


def _registry(request: Request) -> McpServerRegistry:
    if not request.app.state.settings.mcp_gateway.enabled:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="P2 MCP registry is disabled."
        )
    registry = request.app.state.services.mcp_registry
    if registry is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="P2 MCP registry is disabled."
        )
    return cast(McpServerRegistry, registry)
