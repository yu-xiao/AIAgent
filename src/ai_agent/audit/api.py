"""Read-only organization audit API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId
from ai_agent.identity.service import AUDIT_VIEW
from ai_agent.persistence.models import AuditLog

router = APIRouter(prefix="/api/v1/admin", tags=["audit"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=200)]
ActionFilter = Annotated[str | None, Query(min_length=1, max_length=100)]
TraceFilter = Annotated[UUID | None, Query()]
DateFilter = Annotated[datetime | None, Query()]


class AuditLogView(BaseModel):
    id: UUID
    action: str
    actor_user_id: UUID | None
    resource_type: str
    resource_id: str | None
    trace_id: UUID | None
    details: dict[str, Any]
    created_at: datetime


@router.get("/audit-logs", response_model=list[AuditLogView])
async def list_audit_logs(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    offset: Offset = 0,
    limit: Limit = 100,
    action: ActionFilter = None,
    trace_id: TraceFilter = None,
    created_after: DateFilter = None,
    created_before: DateFilter = None,
) -> list[AuditLogView]:
    services = request.app.state.services
    await services.identities.access(identity.session.user_id, organization_id, AUDIT_VIEW)
    async with services.database.session_factory() as session:
        query = select(AuditLog).where(AuditLog.organization_id == organization_id)
        if action is not None:
            query = query.where(AuditLog.action == action)
        if trace_id is not None:
            query = query.where(AuditLog.trace_id == trace_id)
        if created_after is not None:
            query = query.where(AuditLog.created_at >= created_after)
        if created_before is not None:
            query = query.where(AuditLog.created_at < created_before)
        records = list(
            await session.scalars(
                query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
    return [
        AuditLogView(
            id=item.id,
            action=item.action,
            actor_user_id=item.actor_user_id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            trace_id=item.trace_id,
            details=item.details,
            created_at=item.created_at,
        )
        for item in records
    ]
