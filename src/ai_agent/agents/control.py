"""Tenant-scoped managed Agent definitions, immutable versions, and releases."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.audit.service import AuditService
from ai_agent.config import AgentControlMode, Environment, RunLimitSettings
from ai_agent.errors import ConfigurationError, ConflictError, ResourceNotFoundError
from ai_agent.identity.service import (
    AGENT_DRAFT_WRITE,
    AGENT_RELEASE,
    AGENT_RELEASE_BYPASS,
    AGENT_VERSION_CREATE,
    AGENT_VIEW,
    IdentityService,
)
from ai_agent.persistence.models import (
    AgentDefinition,
    AgentDeployment,
    AgentDraft,
    AgentRelease,
    AgentReleaseAction,
    AgentReleaseStatus,
    AgentStatus,
    AgentVersion,
)

_AGENT_CODE = re.compile(r"^[a-z][a-z0-9-]{1,99}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class AgentVersionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    system_prompt: str = Field(min_length=1, max_length=20_000)
    model_policy_ref: Literal["default"] = "default"
    allowed_tools: tuple[str, ...] = Field(default=(), max_length=100)
    limits: RunLimitSettings
    citation_policy: Literal["optional", "required_if_tools_used"] = "optional"
    security_policy_ref: Literal["baseline-v1"] = "baseline-v1"

    @field_validator("system_prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Agent system prompt must not be blank.")
        return normalized

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not _TOOL_NAME.fullmatch(item) for item in normalized):
            raise ValueError("Agent Tool names contain an invalid value.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Agent Tool names must be unique.")
        return normalized


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    agent_id: UUID
    version_id: UUID
    config_digest: str
    config: AgentVersionConfig


class AgentControlService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identities: IdentityService,
        platform_limits: RunLimitSettings,
        default_system_prompt: str,
        *,
        mode: AgentControlMode = AgentControlMode.LEGACY,
        environment: Environment = Environment.DEVELOPMENT,
        audit: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._identities = identities
        self._platform_limits = platform_limits
        self._mode = mode
        self._environment = environment
        self._audit = audit or AuditService()
        self._default_config = AgentVersionConfig(
            system_prompt=default_system_prompt,
            limits=platform_limits,
        )

    @property
    def mode(self) -> AgentControlMode:
        return self._mode

    async def create_agent(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        code: str,
        display_name: str,
        description: str = "",
        config: AgentVersionConfig | None = None,
    ) -> tuple[AgentDefinition, AgentDraft]:
        await self._identities.access(actor_id, organization_id, AGENT_DRAFT_WRITE)
        normalized_code = code.strip().lower()
        if not _AGENT_CODE.fullmatch(normalized_code):
            raise ConflictError("Agent code must use lowercase letters, digits, and hyphens.")
        resolved_config = config or self._default_config
        self._validate_limits(resolved_config.limits)
        try:
            async with self._session_factory() as session, session.begin():
                has_agent = await session.scalar(
                    select(AgentDefinition.id).where(
                        AgentDefinition.organization_id == organization_id
                    )
                )
                agent = AgentDefinition(
                    organization_id=organization_id,
                    code=normalized_code,
                    display_name=display_name.strip(),
                    description=description.strip(),
                    is_default=has_agent is None,
                    created_by=actor_id,
                )
                session.add(agent)
                await session.flush()
                draft = AgentDraft(
                    agent_id=agent.id,
                    organization_id=organization_id,
                    revision=1,
                    config=resolved_config.model_dump(mode="json"),
                    updated_by=actor_id,
                )
                session.add(draft)
                session.add(
                    self._audit.record(
                        organization_id=organization_id,
                        actor_user_id=actor_id,
                        action="agent.created",
                        resource_type="agent",
                        resource_id=str(agent.id),
                        details={"code": normalized_code, "default": agent.is_default},
                    )
                )
                return agent, draft
        except IntegrityError as exc:
            raise ConflictError(
                "Agent code or default Agent conflicts with an existing Agent."
            ) from exc

    async def list_agents(
        self, actor_id: UUID, organization_id: UUID
    ) -> list[AgentDefinition]:
        await self._identities.access(actor_id, organization_id, AGENT_VIEW)
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(AgentDefinition)
                    .where(AgentDefinition.organization_id == organization_id)
                    .order_by(AgentDefinition.is_default.desc(), AgentDefinition.code)
                )
            )

    async def get_draft(
        self, actor_id: UUID, organization_id: UUID, agent_id: UUID
    ) -> AgentDraft:
        await self._identities.access(actor_id, organization_id, AGENT_DRAFT_WRITE)
        async with self._session_factory() as session:
            draft = await session.scalar(
                select(AgentDraft).where(
                    AgentDraft.agent_id == agent_id,
                    AgentDraft.organization_id == organization_id,
                )
            )
            if draft is None:
                raise ResourceNotFoundError("Agent draft not found.")
            return draft

    async def update_draft(
        self,
        actor_id: UUID,
        organization_id: UUID,
        agent_id: UUID,
        *,
        expected_revision: int,
        config: AgentVersionConfig,
    ) -> AgentDraft:
        await self._identities.access(actor_id, organization_id, AGENT_DRAFT_WRITE)
        self._validate_limits(config.limits)
        async with self._session_factory() as session, session.begin():
            draft = await session.scalar(
                select(AgentDraft)
                .where(
                    AgentDraft.agent_id == agent_id,
                    AgentDraft.organization_id == organization_id,
                )
                .with_for_update()
            )
            if draft is None:
                raise ResourceNotFoundError("Agent draft not found.")
            if draft.revision != expected_revision:
                raise ConflictError("Agent draft revision is stale.")
            draft.config = config.model_dump(mode="json")
            draft.revision += 1
            draft.updated_by = actor_id
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="agent.draft_updated",
                    resource_type="agent",
                    resource_id=str(agent_id),
                    details={"revision": draft.revision},
                )
            )
            return draft

    async def create_version(
        self,
        actor_id: UUID,
        organization_id: UUID,
        agent_id: UUID,
        *,
        expected_revision: int,
    ) -> AgentVersion:
        await self._identities.access(actor_id, organization_id, AGENT_VERSION_CREATE)
        async with self._session_factory() as session, session.begin():
            agent = await self._active_agent(session, organization_id, agent_id, lock=True)
            draft = await session.scalar(
                select(AgentDraft)
                .where(
                    AgentDraft.agent_id == agent.id,
                    AgentDraft.organization_id == organization_id,
                )
                .with_for_update()
            )
            if draft is None:
                raise ResourceNotFoundError("Agent draft not found.")
            if draft.revision != expected_revision:
                raise ConflictError("Agent draft revision is stale.")
            config = AgentVersionConfig.model_validate(draft.config)
            self._validate_limits(config.limits)
            snapshot = config.model_dump(mode="json")
            digest = _config_digest(snapshot)
            latest = await session.scalar(
                select(AgentVersion)
                .where(AgentVersion.agent_id == agent_id)
                .order_by(AgentVersion.version_number.desc())
                .limit(1)
            )
            if latest is not None and hmac.compare_digest(latest.config_digest, digest):
                raise ConflictError("Agent draft is unchanged from the latest version.")
            version = AgentVersion(
                agent_id=agent_id,
                organization_id=organization_id,
                version_number=1 if latest is None else latest.version_number + 1,
                config_snapshot=snapshot,
                config_digest=digest,
                created_by=actor_id,
            )
            session.add(version)
            await session.flush()
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="agent.version_created",
                    resource_type="agent_version",
                    resource_id=str(version.id),
                    details={
                        "agent_id": str(agent_id),
                        "version": version.version_number,
                        "config_digest": digest,
                    },
                )
            )
            return version

    async def list_versions(
        self, actor_id: UUID, organization_id: UUID, agent_id: UUID
    ) -> list[AgentVersion]:
        await self._identities.access(actor_id, organization_id, AGENT_VIEW)
        async with self._session_factory() as session:
            await self._active_agent(session, organization_id, agent_id)
            return list(
                await session.scalars(
                    select(AgentVersion)
                    .where(
                        AgentVersion.agent_id == agent_id,
                        AgentVersion.organization_id == organization_id,
                    )
                    .order_by(AgentVersion.version_number.desc())
                )
            )

    async def set_default(
        self, actor_id: UUID, organization_id: UUID, agent_id: UUID
    ) -> AgentDefinition:
        await self._identities.access(actor_id, organization_id, AGENT_RELEASE)
        async with self._session_factory() as session, session.begin():
            agents = list(
                await session.scalars(
                    select(AgentDefinition)
                    .where(AgentDefinition.organization_id == organization_id)
                    .order_by(AgentDefinition.id)
                    .with_for_update()
                )
            )
            selected = next(
                (
                    item
                    for item in agents
                    if item.id == agent_id and item.status == AgentStatus.ACTIVE
                ),
                None,
            )
            if selected is None:
                raise ResourceNotFoundError("Agent not found.")
            for item in agents:
                if item.id != agent_id and item.is_default:
                    item.is_default = False
            # The one-default partial unique index is immediate. Clear the old
            # default first so SQL update ordering cannot produce a transient conflict.
            await session.flush()
            selected.is_default = True
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="agent.default_changed",
                    resource_type="agent",
                    resource_id=str(agent_id),
                    details={},
                )
            )
            return selected

    async def release(
        self,
        actor_id: UUID,
        organization_id: UUID,
        agent_id: UUID,
        version_id: UUID,
        *,
        action: AgentReleaseAction,
        reason: str,
        idempotency_key: str | None,
        expected_generation: int | None,
        bypass_gate: bool,
    ) -> tuple[AgentRelease, AgentDeployment]:
        await self._identities.access(actor_id, organization_id, AGENT_RELEASE)
        if bypass_gate:
            await self._identities.access(actor_id, organization_id, AGENT_RELEASE_BYPASS)
        if self._environment == Environment.PRODUCTION and not bypass_gate:
            raise ConflictError("Production release requires an evaluation gate or audited bypass.")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ConflictError("Agent release reason must not be blank.")
        try:
            async with self._session_factory() as session, session.begin():
                if idempotency_key:
                    existing = await self._idempotent_release(
                        session,
                        organization_id,
                        agent_id,
                        version_id,
                        action,
                        normalized_reason,
                        bypass_gate,
                        idempotency_key,
                    )
                    if existing is not None:
                        return existing
                await self._active_agent(session, organization_id, agent_id, lock=True)
                # A competing request can commit while this request is waiting for the
                # Agent lock. Recheck the key before evaluating deployment generation.
                if idempotency_key:
                    existing = await self._idempotent_release(
                        session,
                        organization_id,
                        agent_id,
                        version_id,
                        action,
                        normalized_reason,
                        bypass_gate,
                        idempotency_key,
                    )
                    if existing is not None:
                        return existing
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.id == version_id,
                        AgentVersion.agent_id == agent_id,
                        AgentVersion.organization_id == organization_id,
                    )
                )
                if version is None:
                    raise ResourceNotFoundError("Agent version not found.")
                if action == AgentReleaseAction.ROLLBACK:
                    previously_deployed = await session.scalar(
                        select(AgentRelease.id).where(
                            AgentRelease.organization_id == organization_id,
                            AgentRelease.agent_id == agent_id,
                            AgentRelease.version_id == version_id,
                            AgentRelease.environment == self._environment.value,
                            AgentRelease.status == AgentReleaseStatus.DEPLOYED,
                        )
                    )
                    if previously_deployed is None:
                        raise ConflictError(
                            "Agent rollback target was not previously deployed."
                        )
                deployment = await self._deployment(
                    session, organization_id, agent_id, lock=True
                )
                current_generation = 0 if deployment is None else deployment.generation
                if expected_generation is not None and expected_generation != current_generation:
                    raise ConflictError("Agent deployment generation is stale.")
                if deployment is not None and deployment.version_id == version_id:
                    raise ConflictError("Agent version is already deployed.")
                previous_version_id = None if deployment is None else deployment.version_id
                if deployment is None:
                    deployment = AgentDeployment(
                        organization_id=organization_id,
                        agent_id=agent_id,
                        environment=self._environment.value,
                        version_id=version_id,
                        generation=1,
                        deployed_by=actor_id,
                    )
                    session.add(deployment)
                else:
                    deployment.version_id = version_id
                    deployment.generation += 1
                    deployment.deployed_by = actor_id
                release = AgentRelease(
                    organization_id=organization_id,
                    agent_id=agent_id,
                    version_id=version_id,
                    previous_version_id=previous_version_id,
                    environment=self._environment.value,
                    action=action,
                    status=AgentReleaseStatus.DEPLOYED,
                    reason=normalized_reason,
                    bypassed_gate=bypass_gate,
                    idempotency_key=idempotency_key,
                    requested_by=actor_id,
                )
                session.add(release)
                await session.flush()
                session.add(
                    self._audit.record(
                        organization_id=organization_id,
                        actor_user_id=actor_id,
                        action=f"agent.{action.value}",
                        resource_type="agent_release",
                        resource_id=str(release.id),
                        details={
                            "agent_id": str(agent_id),
                            "version_id": str(version_id),
                            "previous_version_id": (
                                str(previous_version_id) if previous_version_id else None
                            ),
                            "generation": deployment.generation,
                            "bypassed_gate": bypass_gate,
                            "reason": normalized_reason,
                        },
                    )
                )
                return release, deployment
        except IntegrityError as exc:
            # The database constraint is the final guard for dialects where row locks
            # cannot fully serialize a first deployment or duplicate key insertion.
            if idempotency_key:
                async with self._session_factory() as session:
                    existing = await self._idempotent_release(
                        session,
                        organization_id,
                        agent_id,
                        version_id,
                        action,
                        normalized_reason,
                        bypass_gate,
                        idempotency_key,
                    )
                    if existing is not None:
                        return existing
            raise ConflictError("Agent release conflicts with an existing release.") from exc

    async def list_releases(
        self, actor_id: UUID, organization_id: UUID, agent_id: UUID
    ) -> list[AgentRelease]:
        await self._identities.access(actor_id, organization_id, AGENT_VIEW)
        async with self._session_factory() as session:
            await self._active_agent(session, organization_id, agent_id)
            return list(
                await session.scalars(
                    select(AgentRelease)
                    .where(
                        AgentRelease.agent_id == agent_id,
                        AgentRelease.organization_id == organization_id,
                    )
                    .order_by(AgentRelease.created_at.desc(), AgentRelease.id.desc())
                )
            )

    async def resolve_for_run(
        self,
        session: AsyncSession,
        organization_id: UUID,
        requested_agent_id: UUID | None,
    ) -> ResolvedAgent | None:
        if self._mode == AgentControlMode.LEGACY:
            if requested_agent_id is not None:
                raise ConflictError("Managed Agent selection is disabled.")
            return None
        filters = [
            AgentDefinition.organization_id == organization_id,
            AgentDefinition.status == AgentStatus.ACTIVE,
            AgentDeployment.organization_id == organization_id,
            AgentDeployment.environment == self._environment.value,
            AgentVersion.organization_id == organization_id,
            AgentVersion.agent_id == AgentDefinition.id,
        ]
        if requested_agent_id is None:
            filters.append(AgentDefinition.is_default.is_(True))
        else:
            filters.append(AgentDefinition.id == requested_agent_id)
        row = (
            await session.execute(
                select(AgentDefinition, AgentDeployment, AgentVersion)
                .join(AgentDeployment, AgentDeployment.agent_id == AgentDefinition.id)
                .join(AgentVersion, AgentVersion.id == AgentDeployment.version_id)
                .where(*filters)
            )
        ).one_or_none()
        if row is None:
            if requested_agent_id is not None or self._mode == AgentControlMode.MANAGED_REQUIRED:
                raise ConflictError("No deployed managed Agent is available.")
            return None
        agent, _, version = row
        return self._resolved(agent, version)

    async def load_run_config(
        self,
        organization_id: UUID,
        agent_id: UUID,
        version_id: UUID,
        expected_digest: str,
    ) -> AgentVersionConfig:
        async with self._session_factory() as session:
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.id == version_id,
                    AgentVersion.agent_id == agent_id,
                    AgentVersion.organization_id == organization_id,
                )
            )
            if version is None:
                raise ConfigurationError("Run Agent version is unavailable.")
            if not hmac.compare_digest(version.config_digest, expected_digest):
                raise ConfigurationError("Run Agent version digest does not match.")
            config = AgentVersionConfig.model_validate(version.config_snapshot)
            if not hmac.compare_digest(
                version.config_digest,
                _config_digest(config.model_dump(mode="json")),
            ):
                raise ConfigurationError("Stored Agent version integrity check failed.")
            self._validate_limits(config.limits)
            return config

    async def _active_agent(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        *,
        lock: bool = False,
    ) -> AgentDefinition:
        statement = select(AgentDefinition).where(
            AgentDefinition.id == agent_id,
            AgentDefinition.organization_id == organization_id,
            AgentDefinition.status == AgentStatus.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update()
        agent = await session.scalar(statement)
        if agent is None:
            raise ResourceNotFoundError("Agent not found.")
        return agent

    async def _deployment(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        *,
        lock: bool = False,
    ) -> AgentDeployment | None:
        statement = select(AgentDeployment).where(
            AgentDeployment.organization_id == organization_id,
            AgentDeployment.agent_id == agent_id,
            AgentDeployment.environment == self._environment.value,
        )
        if lock:
            statement = statement.with_for_update()
        deployment: AgentDeployment | None = await session.scalar(statement)
        return deployment

    async def _idempotent_release(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        version_id: UUID,
        action: AgentReleaseAction,
        reason: str,
        bypass_gate: bool,
        idempotency_key: str,
    ) -> tuple[AgentRelease, AgentDeployment] | None:
        existing = await session.scalar(
            select(AgentRelease).where(
                AgentRelease.organization_id == organization_id,
                AgentRelease.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            return None
        if (
            existing.agent_id != agent_id
            or existing.version_id != version_id
            or existing.environment != self._environment.value
            or existing.action != action
            or existing.reason != reason
            or existing.bypassed_gate != bypass_gate
        ):
            raise ConflictError("Idempotency key was used for another release.")
        deployment = await self._deployment(session, organization_id, agent_id)
        if deployment is None:
            raise ConflictError("Idempotent release has no matching deployment.")
        return existing, deployment

    def _resolved(self, agent: AgentDefinition, version: AgentVersion) -> ResolvedAgent:
        config = AgentVersionConfig.model_validate(version.config_snapshot)
        digest = _config_digest(config.model_dump(mode="json"))
        if not hmac.compare_digest(digest, version.config_digest):
            raise ConfigurationError("Stored Agent version integrity check failed.")
        self._validate_limits(config.limits)
        return ResolvedAgent(agent.id, version.id, version.config_digest, config)

    def _validate_limits(self, limits: RunLimitSettings) -> None:
        fields = (
            "max_question_characters",
            "max_model_rounds",
            "max_tool_calls",
            "max_run_seconds",
            "max_input_tokens",
            "max_output_tokens",
            "max_cost_usd",
        )
        if any(
            getattr(limits, field) > getattr(self._platform_limits, field)
            for field in fields
        ):
            raise ConflictError("Agent limits cannot exceed platform hard limits.")


def _config_digest(snapshot: dict[str, object]) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
