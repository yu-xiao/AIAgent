from __future__ import annotations

import json

from ai_agent.audit.service import AuditService
from ai_agent.persistence.models import AuditLog
from tests.p1_conftest import PlatformRuntime


def test_audit_details_are_recursively_redacted_and_urls_are_cleaned() -> None:
    audit = AuditService(b"test-audit-key", key_id="test-v1")

    details = audit.sanitize_details(
        {
            "nested": {
                "api_key": "do-not-store",
                "callback_url": "https://example.test/callback?code=secret#fragment",
            },
            "items": [{"authorization": "Bearer secret"}],
        }
    )

    assert details == {
        "nested": {
            "api_key": "[REDACTED]",
            "callback_url": "https://example.test/callback",
        },
        "items": [{"authorization": "[REDACTED]"}],
    }


async def test_audit_integrity_verification_detects_tampering(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    audit = runtime.services.audit
    record = audit.record(
        organization_id=runtime.organization.id,
        actor_user_id=runtime.user.id,
        action="compliance.test",
        resource_type="test",
        resource_id="integrity",
        details={"state": "original"},
    )
    async with runtime.services.database.session_factory() as session, session.begin():
        session.add(record)

    async with runtime.services.database.session_factory() as session:
        verified = await audit.verify(session, organization_id=runtime.organization.id)
    assert verified.valid >= 1
    assert verified.invalid == 0
    assert verified.key_mismatch == 0

    async with runtime.services.database.session_factory() as session, session.begin():
        stored = await session.get(AuditLog, record.id)
        assert stored is not None
        stored.details = {"state": "tampered"}

    async with runtime.services.database.session_factory() as session:
        tampered = await audit.verify(session, organization_id=runtime.organization.id)
    assert tampered.invalid == 1


async def test_audit_export_is_ndjson_and_redacted(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    record = runtime.services.audit.record(
        organization_id=runtime.organization.id,
        actor_user_id=runtime.user.id,
        action="compliance.export_test",
        resource_type="test",
        resource_id="export",
        details={
            "secret_token": "do-not-export",
            "redirect_url": "https://example.test/redirect?code=do-not-export#fragment",
        },
    )
    async with runtime.services.database.session_factory() as session, session.begin():
        session.add(record)

    response = await runtime.client.get(
        "/api/v1/admin/audit-logs/export",
        headers=runtime.headers,
        params={"action": "compliance.export_test"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert len(lines) == 1
    assert lines[0]["integrity_key_id"] == "test-v1"
    assert len(lines[0]["integrity_hash"]) == 64
    assert lines[0]["details"] == {
        "secret_token": "[REDACTED]",
        "redirect_url": "https://example.test/redirect",
    }
    assert "do-not-export" not in response.text
    assert "fragment" not in response.text


async def test_audit_export_requires_audit_view_permission(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    member_user = await runtime.services.identities.upsert_oidc_user(
        issuer=runtime.settings.oidc.issuer,
        subject="audit-member-subject",
        display_name="Audit Member",
        email=None,
    )
    await runtime.services.identities.add_member(
        runtime.user.id,
        runtime.organization.id,
        member_user.id,
    )
    session_id, _ = await runtime.services.sessions.create_session(
        member_user.id,
        runtime.settings.platform.session_ttl_seconds,
    )
    cookie_name = runtime.settings.platform.session_cookie_name
    runtime.client.cookies.set(cookie_name, session_id)
    response = await runtime.client.get(
        "/api/v1/admin/audit-logs/export",
        headers={"X-Organization-Id": str(runtime.organization.id)},
    )

    assert response.status_code == 403
