"""Explicit, auditable retention jobs for append-only audit records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.persistence.models import AuditLog


@dataclass(frozen=True, slots=True)
class RetentionResult:
    cutoff: datetime
    matched: int
    executed: bool


class AuditRetentionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def prune(self, retention_days: int, *, execute: bool) -> RetentionResult:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._session_factory() as session, session.begin():
            counts = list(
                await session.execute(
                    select(AuditLog.organization_id, func.count(AuditLog.id))
                    .where(AuditLog.created_at < cutoff)
                    .group_by(AuditLog.organization_id)
                )
            )
            matched = sum(int(count) for _, count in counts)
            if not execute or matched == 0:
                return RetentionResult(cutoff=cutoff, matched=matched, executed=execute)
            await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
            for organization_id, count in counts:
                session.add(
                    _retention_audit(
                        organization_id=organization_id,
                        retention_days=retention_days,
                        cutoff=cutoff,
                        deleted=int(count),
                    )
                )
            return RetentionResult(cutoff=cutoff, matched=matched, executed=True)


def _retention_audit(
    *,
    organization_id: UUID | None,
    retention_days: int,
    cutoff: datetime,
    deleted: int,
) -> AuditLog:
    return AuditLog(
        organization_id=organization_id,
        actor_user_id=None,
        action="governance.audit_retention_executed",
        resource_type="audit_log",
        resource_id=None,
        trace_id=None,
        details={
            "retention_days": retention_days,
            "cutoff": cutoff.isoformat(),
            "deleted": deleted,
        },
    )
