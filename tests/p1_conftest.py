from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from pydantic import SecretStr

from ai_agent.api import create_app
from ai_agent.audit.service import AuditService
from ai_agent.config import ModelSettings, OidcSettings, PlatformSettings, Settings
from ai_agent.conversations.service import ConversationService
from ai_agent.identity.oidc import OidcLoginService
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import MemorySessionStore
from ai_agent.models import ModelMessage, ModelStreamEvent, ModelUsageResult
from ai_agent.persistence import Database
from ai_agent.persistence.models import Base, Organization, User
from ai_agent.runs.events import MemoryRunBackend
from ai_agent.runs.executor import RunExecutor
from ai_agent.runtime import AppServices


class FakeModelProvider:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self) -> None:
        self.gate: asyncio.Event | None = None

    def conservative_input_tokens(self, messages: list[ModelMessage]) -> int:
        return sum(len(item.content.encode("utf-8")) + 8 for item in messages)

    async def stream(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
    ) -> AsyncIterator[ModelStreamEvent]:
        del max_output_tokens, trace_id
        if self.gate is not None:
            await self.gate.wait()
        answer = f"Echo: {messages[-1].content}"
        yield ModelStreamEvent(delta=answer)
        yield ModelStreamEvent(usage=ModelUsageResult(input_tokens=20, output_tokens=5))


@dataclass(slots=True)
class PlatformRuntime:
    client: httpx.AsyncClient
    services: AppServices
    settings: Settings
    user: User
    organization: Organization
    session_id: str
    csrf_token: str
    provider: FakeModelProvider

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Organization-Id": str(self.organization.id),
            "X-CSRF-Token": self.csrf_token,
        }


@pytest.fixture
async def platform_runtime() -> AsyncIterator[PlatformRuntime]:
    database_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    database_file.close()
    database_url = f"sqlite+aiosqlite:///{database_file.name.replace(chr(92), '/')}"
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = Settings(
        _env_file=None,
        platform=PlatformSettings(
            enabled=True,
            database_url=database_url,
            redis_url="redis://localhost:6379/0",
        ),
        oidc=OidcSettings(enabled=True, issuer="https://identity.example.test/agent"),
        model=ModelSettings(
            enabled=True,
            base_url="https://model.example.test/v1",
            model="configured-outside-source",
            api_key=SecretStr("test-only-key"),
            input_price_per_million_tokens=1,
            output_price_per_million_tokens=2,
        ),
    )
    sessions = MemorySessionStore()
    audit = AuditService(b"test-audit-key", key_id="test-v1")
    identities = IdentityService(database.session_factory, audit)
    conversations = ConversationService(
        database.session_factory,
        identities,
        settings.limits,
        audit,
    )
    backend = MemoryRunBackend()
    provider = FakeModelProvider()
    oidc = OidcLoginService(settings.oidc, settings.platform, sessions, identities)
    executor = RunExecutor(
        conversations,
        provider,
        backend,
        backend,
        settings.limits,
        settings.model,
    )
    services = AppServices(
        database=database,
        redis=None,
        sessions=sessions,
        identities=identities,
        oidc=oidc,
        conversations=conversations,
        events=backend,
        control=backend,
        executor=executor,
        provider=provider,
        audit=audit,
    )
    user = await identities.upsert_oidc_user(
        issuer=settings.oidc.issuer,
        subject="owner-subject",
        display_name="Owner",
        email="owner@example.test",
    )
    organization = await identities.create_organization(user.id, "Test Organization")
    session_id, session = await sessions.create_session(
        user.id, settings.platform.session_ttl_seconds
    )
    app = create_app(settings, services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={settings.platform.session_cookie_name: session_id},
    ) as client:
        yield PlatformRuntime(
            client=client,
            services=services,
            settings=settings,
            user=user,
            organization=organization,
            session_id=session_id,
            csrf_token=session.csrf_token,
            provider=provider,
        )
    await services.close()
