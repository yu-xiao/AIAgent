"""Construction and lifecycle of infrastructure-backed Agent services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from ai_agent.config import CredentialVaultBackend, Settings
from ai_agent.connections.service import ConnectionService
from ai_agent.conversations.service import ConversationService
from ai_agent.credentials.vault import (
    CredentialVault,
    HashicorpVaultCredentialVault,
    MemoryCredentialVault,
)
from ai_agent.governance.quota import RedisRunQuota
from ai_agent.identity.oidc import OidcLoginService
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import RedisSessionStore, SessionStore
from ai_agent.mcp.gateway import (
    McpGateway,
    RedisCircuitBreaker,
    RedisSlidingWindowRateLimiter,
)
from ai_agent.mcp.registry import McpServerRegistry
from ai_agent.mcp.tool_catalog import RedisCatalogCache, ToolCatalogService
from ai_agent.models import ModelProvider
from ai_agent.models.openai_compatible import OpenAICompatibleProvider
from ai_agent.permission_system.service import PermissionSystemService
from ai_agent.persistence import Database
from ai_agent.persistence.models import AuditLog
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
    quota: RedisRunQuota | None = None
    vault: CredentialVault | None = None
    mcp_registry: McpServerRegistry | None = None
    connections: ConnectionService | None = None
    catalog: ToolCatalogService | None = None
    gateway: McpGateway | None = None
    permission_system: PermissionSystemService | None = None

    async def ping(self) -> None:
        await self.database.ping()
        await self.sessions.ping()
        if self.vault is not None:
            await self.vault.ping()

    async def start(self, settings: Settings) -> None:
        queued = await self.conversations.recover_incomplete_runs(
            datetime.now(UTC) - timedelta(seconds=settings.governance.stale_run_after_seconds)
        )
        for run in queued:
            self.executor.submit(run.id)

    async def close(self) -> None:
        await self.executor.close()
        if self.vault is not None:
            await self.vault.close()
        if self.redis is not None:
            await self.redis.aclose()
        await self.database.dispose()


def build_services(settings: Settings) -> AppServices:
    database = Database(
        settings.platform.database_url,
        password=settings.database_password(),
    )
    redis = Redis.from_url(
        settings.platform.redis_url,
        password=settings.redis_password(),
    )
    sessions = RedisSessionStore(redis)
    identities = IdentityService(database.session_factory)
    conversations = ConversationService(database.session_factory, identities, settings.limits)
    events = RedisRunEventBus(redis)
    control = RedisRunControl(redis)
    provider = OpenAICompatibleProvider(settings.model)
    oidc = OidcLoginService(settings.oidc, settings.platform, sessions, identities)
    vault: CredentialVault
    if settings.credentials.backend == CredentialVaultBackend.HASHICORP_VAULT:
        vault = HashicorpVaultCredentialVault(settings.credentials)
    else:
        vault = MemoryCredentialVault()
    mcp_registry = McpServerRegistry(database.session_factory, identities)
    connections = ConnectionService(
        database.session_factory,
        identities,
        sessions,
        vault,
        settings.platform,
        default_timeout_seconds=settings.mcp_gateway.default_timeout_seconds,
        signing_algorithms=settings.oidc.signing_algorithms,
    )
    catalog = ToolCatalogService(
        mcp_registry,
        connections,
        RedisCatalogCache(redis),
        ttl_seconds=settings.mcp_gateway.catalog_ttl_seconds,
        vault=vault,
    )
    gateway = McpGateway(
        catalog,
        rate_limiter=RedisSlidingWindowRateLimiter(redis),
        circuit_breaker=RedisCircuitBreaker(redis),
        audit=_tool_audit_writer(database.session_factory),
    )
    permission_system = PermissionSystemService(
        gateway,
        expected_tools=settings.permission_system.expected_tools,
    )
    quota = (
        RedisRunQuota(
            redis,
            settings.governance.quota,
            settings.limits,
            lease_seconds=(
                int(settings.limits.max_run_seconds + settings.governance.shutdown_grace_seconds)
                + 60
            ),
        )
        if settings.governance.enabled and settings.governance.quota.enabled
        else None
    )
    executor = RunExecutor(
        conversations,
        provider,
        events,
        control,
        settings.limits,
        settings.model,
        gateway=gateway if settings.mcp_gateway.enabled else None,
        tool_system_code=("permission-system" if settings.permission_system.enabled else None),
        tool_allowlist=(
            frozenset(settings.permission_system.expected_tools)
            if settings.permission_system.enabled
            else None
        ),
        personal_tools_only=settings.permission_system.enabled,
        quota=quota,
        shutdown_grace_seconds=settings.governance.shutdown_grace_seconds,
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
        quota=quota,
        vault=vault,
        mcp_registry=mcp_registry,
        connections=connections,
        catalog=catalog,
        gateway=gateway,
        permission_system=permission_system,
    )


def _tool_audit_writer(session_factory: Any) -> Callable[..., Awaitable[None]]:
    async def write(
        *,
        context: Any,
        server: Any,
        tool: Any,
        status: str,
        duration_ms: float,
        error: str | None,
    ) -> None:
        async with session_factory() as session, session.begin():
            session.add(
                AuditLog(
                    organization_id=context.organization_id,
                    actor_user_id=context.user_id,
                    action=f"tool.{status}",
                    resource_type="mcp_tool",
                    resource_id=tool.name,
                    trace_id=UUID(context.trace_id),
                    details={
                        "server_code": server.code,
                        "tool_name": tool.name,
                        "duration_ms": duration_ms,
                        "error": error,
                    },
                )
            )

    return write
