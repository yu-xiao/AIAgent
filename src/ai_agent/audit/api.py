"""Read-only organization audit API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Select, select

from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId
from ai_agent.identity.service import AUDIT_VIEW
from ai_agent.persistence.models import AuditLog

router = APIRouter(prefix="/api/v1/admin", tags=["audit"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=200)]
ExportLimit = Annotated[int, Query(ge=1, le=10_000)]
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
    integrity_key_id: str | None
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
        query = _filtered_query(
            organization_id,
            action=action,
            trace_id=trace_id,
            created_after=created_after,
            created_before=created_before,
        )
        records = list(
            await session.scalars(
                query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
    return [_to_view(item, services.audit) for item in records]


@router.get("/audit-logs/export")
async def export_audit_logs(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    limit: ExportLimit = 10_000,
    action: ActionFilter = None,
    trace_id: TraceFilter = None,
    created_after: DateFilter = None,
    created_before: DateFilter = None,
) -> StreamingResponse:
    services = request.app.state.services
    await services.identities.access(identity.session.user_id, organization_id, AUDIT_VIEW)
    async with services.database.session_factory() as session:
        records = list(
            await session.scalars(
                _filtered_query(
                    organization_id,
                    action=action,
                    trace_id=trace_id,
                    created_after=created_after,
                    created_before=created_before,
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
            )
        )

    async def lines() -> AsyncIterator[bytes]:
        for record in records:
            view = _to_view(record, services.audit)
            payload = view.model_dump(mode="json")
            payload["integrity_hash"] = record.integrity_hash
            yield (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode()

    return StreamingResponse(lines(), media_type="application/x-ndjson")


def _filtered_query(
    organization_id: UUID,
    *,
    action: str | None,
    trace_id: UUID | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> Select[tuple[AuditLog]]:
    query = select(AuditLog).where(AuditLog.organization_id == organization_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    if trace_id is not None:
        query = query.where(AuditLog.trace_id == trace_id)
    if created_after is not None:
        query = query.where(AuditLog.created_at >= created_after)
    if created_before is not None:
        query = query.where(AuditLog.created_at < created_before)
    return query


def _to_view(record: AuditLog, audit: Any) -> AuditLogView:
    return AuditLogView(
        id=record.id,
        action=record.action,
        actor_user_id=record.actor_user_id,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        trace_id=record.trace_id,
        details=audit.sanitize_details(record.details),
        integrity_key_id=record.integrity_key_id,
        created_at=record.created_at,
    )
