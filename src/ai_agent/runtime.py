"""Construction and lifecycle of infrastructure-backed Agent services."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from ai_agent.agents.control import AgentControlService
from ai_agent.audit.service import AuditService
from ai_agent.config import CredentialVaultBackend, ExecutionMode, Settings
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
from ai_agent.mcp.network_policy import McpNetworkPolicy
from ai_agent.mcp.registry import McpServerRegistry
from ai_agent.mcp.tool_catalog import RedisCatalogCache, ToolCatalogService
from ai_agent.models import ModelProvider
from ai_agent.models.openai_compatible import OpenAICompatibleProvider
from ai_agent.permission_system.service import PermissionSystemService
from ai_agent.persistence import Database
from ai_agent.runs.events import RedisRunControl, RedisRunEventBus, RunControl, RunEventBus
from ai_agent.runs.executor import RunExecutor
from ai_agent.runs.jobs import RunJobService
from ai_agent.runs.worker import RedisWorkerRegistry

logger = logging.getLogger(__name__)


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
    audit: AuditService
    jobs: RunJobService | None = None
    worker_registry: RedisWorkerRegistry | None = None
    quota: RedisRunQuota | None = None
    vault: CredentialVault | None = None
    mcp_registry: McpServerRegistry | None = None
    connections: ConnectionService | None = None
    catalog: ToolCatalogService | None = None
    gateway: McpGateway | None = None
    permission_system: PermissionSystemService | None = None
    agent_control: AgentControlService | None = None

    async def ping(self) -> None:
        await self.database.ping()
        await self.sessions.ping()
        if self.vault is not None:
            await self.vault.ping()

    async def start(self, settings: Settings) -> None:
        if self.connections is not None:
            await self.connections.retry_pending_revocations()
        if settings.execution.mode == ExecutionMode.EXTERNAL_WORKER:
            return
        queued = await self.conversations.recover_incomplete_runs(
            datetime.now(UTC) - timedelta(seconds=settings.governance.stale_run_after_seconds)
        )
        for run in queued:
            try:
                accepted = self.executor.submit(run.id)
                if not accepted:
                    logger.debug(
                        "Skipped duplicate recovered Agent Run",
                        extra={"run_id": str(run.id)},
                    )
            except Exception:
                logger.exception("Failed to recover Agent Run", extra={"run_id": str(run.id)})
                await self.executor.reject_submission(
                    run.id,
                    str(run.trace_id),
                    "recovery_submission_failed",
                    "Run could not be resumed after service startup.",
                )

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
    audit = AuditService(
        settings.audit_integrity_key(),
        key_id=settings.governance.audit_integrity_key_id,
    )
    identities = IdentityService(database.session_factory, audit)
    agent_control = AgentControlService(
        database.session_factory,
        identities,
        settings.limits,
        settings.model.system_prompt,
        mode=settings.agent_control.mode,
        environment=settings.environment,
        audit=audit,
    )
    conversations = ConversationService(
        database.session_factory,
        identities,
        settings.limits,
        audit,
        durable_jobs_enabled=settings.execution.mode == ExecutionMode.EXTERNAL_WORKER,
        job_max_attempts=settings.execution.max_attempts,
        agent_control=agent_control,
    )
    jobs = RunJobService(database.session_factory, audit)
    worker_registry = RedisWorkerRegistry(redis)
    events = RedisRunEventBus(redis)
    control = RedisRunControl(redis)
    provider = OpenAICompatibleProvider(settings.model)
    oidc = OidcLoginService(settings.oidc, settings.platform, sessions, identities)
    vault: CredentialVault
    if settings.credentials.backend == CredentialVaultBackend.HASHICORP_VAULT:
        vault = HashicorpVaultCredentialVault(settings.credentials)
    else:
        vault = MemoryCredentialVault()
    network_policy = McpNetworkPolicy.from_values(
        allow_local_addresses=settings.mcp_gateway.allow_local_addresses,
        allow_private_addresses=settings.mcp_gateway.allow_private_addresses,
        allowed_hosts=settings.mcp_gateway.allowed_hosts,
        allowed_ips=settings.mcp_gateway.allowed_ips,
    )
    mcp_registry = McpServerRegistry(
        database.session_factory,
        identities,
        network_policy=network_policy,
        audit=audit,
    )
    connections = ConnectionService(
        database.session_factory,
        identities,
        sessions,
        vault,
        settings.platform,
        default_timeout_seconds=settings.mcp_gateway.default_timeout_seconds,
        signing_algorithms=settings.oidc.signing_algorithms,
        network_policy=network_policy,
        audit=audit,
    )
    catalog = ToolCatalogService(
        mcp_registry,
        connections,
        RedisCatalogCache(redis),
        ttl_seconds=settings.mcp_gateway.catalog_ttl_seconds,
        vault=vault,
        network_policy=network_policy,
    )
    gateway = McpGateway(
        catalog,
        rate_limiter=RedisSlidingWindowRateLimiter(redis),
        circuit_breaker=RedisCircuitBreaker(redis),
        audit=_tool_audit_writer(database.session_factory, audit),
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
        jobs=jobs,
        shutdown_grace_seconds=settings.governance.shutdown_grace_seconds,
        max_concurrent_runs=settings.governance.max_concurrent_runs,
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
        audit=audit,
        jobs=jobs,
        worker_registry=worker_registry,
        quota=quota,
        vault=vault,
        mcp_registry=mcp_registry,
        connections=connections,
        catalog=catalog,
        gateway=gateway,
        permission_system=permission_system,
        agent_control=agent_control,
    )


def _tool_audit_writer(
    session_factory: Any,
    audit: AuditService,
) -> Callable[..., Awaitable[None]]:
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
                audit.record(
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
