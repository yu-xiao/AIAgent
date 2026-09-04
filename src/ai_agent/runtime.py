"""Construction and lifecycle of P1 infrastructure-backed services."""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from ai_agent.config import Settings
from ai_agent.conversations.service import ConversationService
from ai_agent.identity.oidc import OidcLoginService
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import RedisSessionStore, SessionStore
from ai_agent.models import ModelProvider
from ai_agent.models.openai_compatible import OpenAICompatibleProvider
from ai_agent.persistence import Database
from ai_agent.runs.events import RedisRunControl, RedisRunEventBus, RunControl, RunEventBus
from ai_agent.runs.executor import RunExecutor


@dataclass(slots=True)
class AppServices:
    database: Database
    redis: Redis | None
    sessions: SessionStore
    identities: IdentityService
    oidc: OidcLoginService
    conversations: ConversationService
    events: RunEventBus
    control: RunControl
    executor: RunExecutor
    provider: ModelProvider

    async def ping(self) -> None:
        await self.database.ping()
        await self.sessions.ping()

    async def close(self) -> None:
        await self.executor.close()
        if self.redis is not None:
            await self.redis.aclose()
        await self.database.dispose()


def build_services(settings: Settings) -> AppServices:
    database = Database(settings.platform.database_url)
    redis = Redis.from_url(settings.platform.redis_url)
    sessions = RedisSessionStore(redis)
    identities = IdentityService(database.session_factory)
    conversations = ConversationService(database.session_factory, identities, settings.limits)
    events = RedisRunEventBus(redis)
    control = RedisRunControl(redis)
    provider = OpenAICompatibleProvider(settings.model)
    oidc = OidcLoginService(settings.oidc, settings.platform, sessions, identities)
    executor = RunExecutor(
        conversations,
        provider,
        events,
        control,
        settings.limits,
        settings.model,
    )
    return AppServices(
        database=database,
        redis=redis,
        sessions=sessions,
        identities=identities,
        oidc=oidc,
        conversations=conversations,
        events=events,
        control=control,
        executor=executor,
        provider=provider,
    )
