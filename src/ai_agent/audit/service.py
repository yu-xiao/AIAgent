"""Append-only audit record creation, redaction, and integrity verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_agent.persistence.models import AuditLog, utc_now

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "api_key",
    "credential",
)
_URL_KEY_SUFFIXES = ("_url", "_uri", "_endpoint")


@dataclass(frozen=True, slots=True)
class AuditVerificationResult:
    checked: int
    valid: int
    unsigned: int
    key_mismatch: int
    invalid: int


class AuditService:
    def __init__(self, signing_key: bytes | None = None, *, key_id: str = "") -> None:
        self._signing_key = signing_key
        self._key_id = key_id if signing_key else ""

    def record(
        self,
        *,
        organization_id: UUID | None,
        actor_user_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: str | None,
        trace_id: UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        record = AuditLog(
            id=uuid4(),
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            trace_id=trace_id,
            details=self.sanitize_details(details or {}),
            created_at=utc_now(),
            integrity_key_id=self._key_id or None,
        )
        if self._signing_key:
            record.integrity_hash = self._sign(record)
        return record

    def sanitize_details(self, value: dict[str, Any]) -> dict[str, Any]:
        sanitized = self._sanitize(value)
        if not isinstance(sanitized, dict):
            raise TypeError("Audit details must be a mapping.")
        return sanitized

    async def verify(
        self,
        session: AsyncSession,
        *,
        organization_id: UUID | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> AuditVerificationResult:
        query = select(AuditLog)
        if organization_id is not None:
            query = query.where(AuditLog.organization_id == organization_id)
        if created_after is not None:
            query = query.where(AuditLog.created_at >= created_after)
        if created_before is not None:
            query = query.where(AuditLog.created_at < created_before)
        records = list(await session.scalars(query))
        valid = unsigned = key_mismatch = invalid = 0
        for record in records:
            if not record.integrity_hash:
                unsigned += 1
            elif not self._signing_key or record.integrity_key_id != self._key_id:
                key_mismatch += 1
            elif secrets.compare_digest(record.integrity_hash, self._sign(record)):
                valid += 1
            else:
                invalid += 1
        return AuditVerificationResult(
            checked=len(records),
            valid=valid,
            unsigned=unsigned,
            key_mismatch=key_mismatch,
            invalid=invalid,
        )

    def _sign(self, record: AuditLog) -> str:
        if not self._signing_key:
            raise RuntimeError("Audit integrity signing key is unavailable.")
        return hmac.new(
            self._signing_key,
            self._canonical_payload(record).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _canonical_payload(self, record: AuditLog) -> str:
        created_at = record.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return json.dumps(
            {
                "action": record.action,
                "actor_user_id": _stringify_uuid(record.actor_user_id),
                "created_at": created_at.astimezone(UTC).isoformat(),
                "details": record.details,
                "id": str(record.id),
                "integrity_key_id": record.integrity_key_id,
                "organization_id": _stringify_uuid(record.organization_id),
                "resource_id": record.resource_id,
                "resource_type": record.resource_type,
                "trace_id": _stringify_uuid(record.trace_id),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _sanitize(self, value: Any, *, key: str = "") -> Any:
        normalized_key = key.lower().replace("-", "_")
        if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
            return _REDACTED
        if normalized_key.endswith(_URL_KEY_SUFFIXES) and isinstance(value, str):
            return _remove_url_query(value)
        if isinstance(value, dict):
            return {
                str(item_key): self._sanitize(item, key=str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize(item) for item in value]
        return value


def _stringify_uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _remove_url_query(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
