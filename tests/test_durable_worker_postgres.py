from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from ai_agent.audit.service import AuditService
from ai_agent.config import RunLimitSettings
from ai_agent.conversations.service import ConversationService
from ai_agent.identity.service import IdentityService
from ai_agent.persistence import Database
from ai_agent.persistence.models import Base
from ai_agent.runs.jobs import RunJobService

_DATABASE_URL = os.environ.get("AI_AGENT_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="Dedicated PostgreSQL worker test database is not configured.",
)


async def test_two_workers_cannot_claim_the_same_postgres_job() -> None:
    assert _DATABASE_URL is not None
    database = Database(_DATABASE_URL)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        audit = AuditService(b"postgres-worker-test", key_id="test-v1")
        identities = IdentityService(database.session_factory, audit)
        user = await identities.upsert_oidc_user(
            issuer="https://identity.example.test",
            subject=f"postgres-worker-{uuid4()}",
            display_name="PostgreSQL Worker",
            email=None,
        )
        organization = await identities.create_organization(user.id, "PostgreSQL Worker")
        conversations = ConversationService(
            database.session_factory,
            identities,
            RunLimitSettings(),
            audit,
            durable_jobs_enabled=True,
        )
        conversation = await conversations.create_conversation(
            user.id,
            organization.id,
            "Concurrent lease",
        )
        await conversations.create_run(
            user.id,
            organization.id,
            conversation.id,
            "claim once",
            None,
            uuid4(),
        )
        jobs = RunJobService(database.session_factory, audit)

        first, second = await asyncio.gather(
            jobs.claim("worker-one", lease_seconds=45),
            jobs.claim("worker-two", lease_seconds=45),
        )

        assert sum(item is not None for item in (first, second)) == 1
    finally:
        await database.dispose()
