"""Managed Agent definition, version, and release APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ai_agent.agents.control import AgentControlService, AgentVersionConfig
from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId, require_csrf
from ai_agent.persistence.models import (
    AgentDefinition,
    AgentDeployment,
    AgentDraft,
    AgentRelease,
    AgentReleaseAction,
    AgentVersion,
)

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str, Header(alias="If-Match")]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1_000)
    config: AgentVersionConfig | None = None


class AgentView(BaseModel):
    id: UUID
    code: str
    display_name: str
    description: str
    status: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: AgentVersionConfig


class DraftView(BaseModel):
    agent_id: UUID
    revision: int
    config: AgentVersionConfig
    updated_at: datetime


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class VersionView(BaseModel):
    id: UUID
    agent_id: UUID
    version_number: int
    config: AgentVersionConfig
    config_digest: str
    created_at: datetime


class ReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: UUID
    reason: str = Field(min_length=1, max_length=500)
    expected_generation: int | None = Field(default=None, ge=0)
    bypass_gate: bool = False


class DeploymentView(BaseModel):
    agent_id: UUID
    environment: str
    version_id: UUID
    generation: int
    updated_at: datetime


class ReleaseView(BaseModel):
    id: UUID
    agent_id: UUID
    version_id: UUID
    previous_version_id: UUID | None
    environment: str
    action: str
    status: str
    reason: str
    bypassed_gate: bool
    created_at: datetime


class ReleaseResult(BaseModel):
    release: ReleaseView
    deployment: DeploymentView


@router.post(
    "/agents",
    response_model=AgentView,
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
)
async def create_agent(
    payload: AgentCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> AgentView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    agent, _ = await _service(request).create_agent(
        identity.session.user_id,
        organization_id,
        code=payload.code,
        display_name=payload.display_name,
        description=payload.description,
        config=payload.config,
    )
    return _agent_view(agent)


@router.get("/agents", response_model=list[AgentView], tags=["agents"])
async def list_agents(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[AgentView]:
    agents = await _service(request).list_agents(identity.session.user_id, organization_id)
    return [_agent_view(item) for item in agents]


@router.get("/agents/{agent_id}/draft", response_model=DraftView, tags=["agents"])
async def get_draft(
    agent_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> DraftView:
    draft = await _service(request).get_draft(
        identity.session.user_id, organization_id, agent_id
    )
    return _draft_view(draft)


@router.put("/agents/{agent_id}/draft", response_model=DraftView, tags=["agents"])
async def update_draft(
    agent_id: UUID,
    payload: DraftUpdate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    if_match: IfMatch,
) -> DraftView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    draft = await _service(request).update_draft(
        identity.session.user_id,
        organization_id,
        agent_id,
        expected_revision=_parse_revision(if_match),
        config=payload.config,
    )
    return _draft_view(draft)


@router.post(
    "/agents/{agent_id}/versions",
    response_model=VersionView,
    status_code=status.HTTP_201_CREATED,
    tags=["agents"],
)
async def create_version(
    agent_id: UUID,
    payload: VersionCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> VersionView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    version = await _service(request).create_version(
        identity.session.user_id,
        organization_id,
        agent_id,
        expected_revision=payload.expected_revision,
    )
    return _version_view(version)


@router.get(
    "/agents/{agent_id}/versions", response_model=list[VersionView], tags=["agents"]
)
async def list_versions(
    agent_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[VersionView]:
    versions = await _service(request).list_versions(
        identity.session.user_id, organization_id, agent_id
    )
    return [_version_view(item) for item in versions]


@router.post("/agents/{agent_id}/default", response_model=AgentView, tags=["agents"])
async def set_default_agent(
    agent_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> AgentView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    agent = await _service(request).set_default(
        identity.session.user_id, organization_id, agent_id
    )
    return _agent_view(agent)


@router.post("/agents/{agent_id}/releases", response_model=ReleaseResult, tags=["agents"])
async def release_agent(
    agent_id: UUID,
    payload: ReleaseCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    idempotency_key: IdempotencyKey = None,
) -> ReleaseResult:
    return await _release(
        agent_id,
        payload,
        request,
        identity,
        organization_id,
        idempotency_key,
        AgentReleaseAction.DEPLOY,
    )


@router.post("/agents/{agent_id}/rollback", response_model=ReleaseResult, tags=["agents"])
async def rollback_agent(
    agent_id: UUID,
    payload: ReleaseCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    idempotency_key: IdempotencyKey = None,
) -> ReleaseResult:
    return await _release(
        agent_id,
        payload,
        request,
        identity,
        organization_id,
        idempotency_key,
        AgentReleaseAction.ROLLBACK,
    )


@router.get(
    "/agents/{agent_id}/releases", response_model=list[ReleaseView], tags=["agents"]
)
async def list_releases(
    agent_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[ReleaseView]:
    releases = await _service(request).list_releases(
        identity.session.user_id, organization_id, agent_id
    )
    return [_release_view(item) for item in releases]


async def _release(
    agent_id: UUID,
    payload: ReleaseCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: UUID,
    idempotency_key: str | None,
    action: AgentReleaseAction,
) -> ReleaseResult:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain between 1 and 200 characters.",
        )
    release, deployment = await _service(request).release(
        identity.session.user_id,
        organization_id,
        agent_id,
        payload.version_id,
        action=action,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        expected_generation=payload.expected_generation,
        bypass_gate=payload.bypass_gate,
    )
    return ReleaseResult(
        release=_release_view(release),
        deployment=_deployment_view(deployment),
    )


def _service(request: Request) -> AgentControlService:
    service = cast(AgentControlService | None, request.app.state.services.agent_control)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Managed Agent control plane is unavailable.",
        )
    return service


def _parse_revision(value: str) -> int:
    normalized = value.strip().strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="If-Match must contain the current numeric draft revision.",
        ) from exc
    if revision < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="If-Match must contain a positive draft revision.",
        )
    return revision


def _agent_view(agent: AgentDefinition) -> AgentView:
    return AgentView(
        id=agent.id,
        code=agent.code,
        display_name=agent.display_name,
        description=agent.description,
        status=agent.status.value,
        is_default=agent.is_default,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _draft_view(draft: AgentDraft) -> DraftView:
    return DraftView(
        agent_id=draft.agent_id,
        revision=draft.revision,
        config=AgentVersionConfig.model_validate(draft.config),
        updated_at=draft.updated_at,
    )


def _version_view(version: AgentVersion) -> VersionView:
    return VersionView(
        id=version.id,
        agent_id=version.agent_id,
        version_number=version.version_number,
        config=AgentVersionConfig.model_validate(version.config_snapshot),
        config_digest=version.config_digest,
        created_at=version.created_at,
    )


def _release_view(release: AgentRelease) -> ReleaseView:
    return ReleaseView(
        id=release.id,
        agent_id=release.agent_id,
        version_id=release.version_id,
        previous_version_id=release.previous_version_id,
        environment=release.environment,
        action=release.action.value,
        status=release.status.value,
        reason=release.reason,
        bypassed_gate=release.bypassed_gate,
        created_at=release.created_at,
    )


def _deployment_view(deployment: AgentDeployment) -> DeploymentView:
    return DeploymentView(
        agent_id=deployment.agent_id,
        environment=deployment.environment,
        version_id=deployment.version_id,
        generation=deployment.generation,
        updated_at=deployment.updated_at,
    )
