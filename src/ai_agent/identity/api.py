"""Authentication, current-user, organization and member APIs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_agent.config import Environment
from ai_agent.identity.dependencies import (
    CurrentIdentity,
    OrganizationId,
    require_csrf,
    require_platform,
)

router = APIRouter(prefix="/api/v1")


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class OrganizationView(BaseModel):
    id: UUID
    name: str
    created_at: datetime


class UserView(BaseModel):
    id: UUID
    display_name: str
    email: str | None


class MeView(UserView):
    csrf_token: str
    organizations: list[OrganizationView]


class MemberCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID


class MemberRolesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: set[str] = Field(min_length=1, max_length=20)


class MemberView(BaseModel):
    id: UUID
    user: UserView
    roles: list[str]


@router.get("/auth/login", tags=["authentication"])
async def login(request: Request) -> RedirectResponse:
    require_platform(request)
    url = await request.app.state.services.oidc.begin()
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/callback", tags=["authentication"])
async def callback(
    request: Request,
    code: str = Query(min_length=1, max_length=4_096),
    state_value: str = Query(alias="state", min_length=20, max_length=512),
) -> RedirectResponse:
    require_platform(request)
    result = await request.app.state.services.oidc.finish(code=code, state=state_value)
    settings = request.app.state.settings
    response = RedirectResponse(
        settings.platform.post_login_redirect_uri,
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        settings.platform.session_cookie_name,
        result.session_id,
        max_age=settings.platform.session_ttl_seconds,
        httponly=True,
        secure=settings.environment == Environment.PRODUCTION,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["authentication"])
async def logout(
    request: Request,
    identity: CurrentIdentity,
) -> Response:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    await request.app.state.services.identities.record_logout(identity.session.user_id)
    await request.app.state.services.sessions.delete_session(identity.session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(request.app.state.settings.platform.session_cookie_name, path="/")
    return response


@router.get("/me", response_model=MeView, tags=["identity"])
async def me(
    request: Request,
    identity: CurrentIdentity,
) -> MeView:
    identities = request.app.state.services.identities
    user = await identities.get_user(identity.session.user_id)
    organizations = await identities.list_organizations(user.id)
    return MeView(
        id=user.id,
        display_name=user.display_name,
        email=user.email,
        csrf_token=identity.session.csrf_token,
        organizations=[
            OrganizationView(id=item.id, name=item.name, created_at=item.created_at)
            for item in organizations
        ],
    )


@router.post(
    "/organizations",
    response_model=OrganizationView,
    status_code=status.HTTP_201_CREATED,
    tags=["organizations"],
)
async def create_organization(
    payload: OrganizationCreate,
    request: Request,
    identity: CurrentIdentity,
) -> OrganizationView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    organization = await request.app.state.services.identities.create_organization(
        identity.session.user_id, payload.name.strip()
    )
    return OrganizationView(
        id=organization.id,
        name=organization.name,
        created_at=organization.created_at,
    )


@router.get(
    "/organizations/current",
    response_model=OrganizationView,
    tags=["organizations"],
)
async def current_organization(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> OrganizationView:
    access = await request.app.state.services.identities.access(
        identity.session.user_id, organization_id
    )
    organization = access.organization
    return OrganizationView(
        id=organization.id,
        name=organization.name,
        created_at=organization.created_at,
    )


@router.get(
    "/organizations/current/members",
    response_model=list[MemberView],
    tags=["organizations"],
)
async def list_members(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[MemberView]:
    members = await request.app.state.services.identities.list_members(
        identity.session.user_id, organization_id
    )
    return [
        MemberView(
            id=member.id,
            user=UserView(id=user.id, display_name=user.display_name, email=user.email),
            roles=roles,
        )
        for member, user, roles in members
    ]


@router.post(
    "/organizations/current/members",
    response_model=MemberView,
    status_code=status.HTTP_201_CREATED,
    tags=["organizations"],
)
async def add_member(
    payload: MemberCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> MemberView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    member = await request.app.state.services.identities.add_member(
        identity.session.user_id, organization_id, payload.user_id
    )
    user = await request.app.state.services.identities.get_user(payload.user_id)
    return MemberView(
        id=member.id,
        user=UserView(id=user.id, display_name=user.display_name, email=user.email),
        roles=["member"],
    )


@router.put(
    "/organizations/current/members/{member_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["organizations"],
)
async def update_member_roles(
    member_id: UUID,
    payload: MemberRolesUpdate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> Response:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    await request.app.state.services.identities.set_member_roles(
        identity.session.user_id,
        organization_id,
        member_id,
        payload.roles,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/organizations/current/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["organizations"],
)
async def remove_member(
    member_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> Response:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    await request.app.state.services.identities.remove_member(
        identity.session.user_id,
        organization_id,
        member_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
