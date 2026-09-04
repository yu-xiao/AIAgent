"""PermissionSystem read-only Tool endpoints for the connection workbench."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId, require_csrf
from ai_agent.mcp.models import RunContext, ToolDefinition, ToolResult
from ai_agent.permission_system.service import PermissionSystemService

router = APIRouter(prefix="/api/v1/permission-system", tags=["permission-system"])


class PermissionToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)


class PermissionToolResultView(BaseModel):
    name: str
    is_error: bool
    structured_content: Any = None
    content: list[Any]
    trace_id: str
    truncated: bool
    citations: list[dict[str, Any]]


class PermissionToolsView(BaseModel):
    tools: list[ToolDefinition]


@router.get("/tools", response_model=PermissionToolsView)
async def list_permission_tools(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> PermissionToolsView:
    service = _service(request)
    context = _context(request, identity.session.user_id, organization_id)
    return PermissionToolsView(tools=await service.list_tools(context))


@router.post("/tools/{tool_name}", response_model=PermissionToolResultView)
async def call_permission_tool(
    tool_name: str,
    payload: PermissionToolCall,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> PermissionToolResultView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    service = _service(request)
    context = _context(request, identity.session.user_id, organization_id)
    result = await service.call_readonly(context, tool_name, payload.arguments)
    return _result_view(result)


def _context(request: Request, user_id: Any, organization_id: Any) -> RunContext:
    return RunContext(
        organization_id=organization_id,
        user_id=user_id,
        trace_id=str(request.state.trace_id),
    )


def _result_view(result: ToolResult) -> PermissionToolResultView:
    citations = result.citations or ([result.citation] if result.citation is not None else [])
    return PermissionToolResultView(
        name=result.name,
        is_error=result.is_error,
        structured_content=result.structured_content,
        content=result.content,
        trace_id=result.trace_id,
        truncated=result.truncated,
        citations=[item.model_dump(mode="json") for item in citations],
    )


def _service(request: Request) -> PermissionSystemService:
    settings = request.app.state.settings
    if not settings.mcp_gateway.enabled or not settings.permission_system.enabled:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P3 PermissionSystem integration is disabled.")
    service = request.app.state.services.permission_system
    if service is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="P3 PermissionSystem integration is disabled.")
    return cast(PermissionSystemService, service)
