"""FastAPI dependencies for opaque sessions, CSRF and organization selection."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from ai_agent.identity.sessions import AuthSession


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    session_id: str
    session: AuthSession


def require_platform(request: Request) -> None:
    if not request.app.state.settings.platform.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="P1 platform is disabled.",
        )


async def current_identity(request: Request) -> RequestIdentity:
    require_platform(request)
    cookie_name = request.app.state.settings.platform.session_cookie_name
    session_id = request.cookies.get(cookie_name)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required.")
    auth_session = await request.app.state.services.sessions.get_session(session_id)
    if auth_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is missing or expired.",
        )
    return RequestIdentity(session_id=session_id, session=auth_session)


async def require_csrf(
    identity: RequestIdentity,
    x_csrf_token: str | None,
) -> None:
    if not x_csrf_token or not secrets.compare_digest(
        x_csrf_token.encode(), identity.session.csrf_token.encode()
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed.")


def organization_id_header(
    x_organization_id: str | None = Header(default=None),
) -> UUID:
    if not x_organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-Id header is required.",
        )
    try:
        return UUID(x_organization_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Organization-Id header is invalid.",
        ) from exc


CurrentIdentity = Annotated[RequestIdentity, Depends(current_identity)]
OrganizationId = Annotated[UUID, Depends(organization_id_header)]
