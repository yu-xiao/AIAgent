"""Local registration/login endpoints with pre-login CSRF and abuse protection."""

from __future__ import annotations

import hashlib
from typing import Annotated, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ai_agent.config import Environment
from ai_agent.errors import RateLimitExceededError
from ai_agent.identity.dependencies import require_platform
from ai_agent.identity.local import LocalAuthService
from ai_agent.mcp.gateway import RedisSlidingWindowRateLimiter

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("请输入有效邮箱.")
        name, domain = value.split("@")
        if not name or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("请输入有效邮箱.")
        return value


class RegisterInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("请输入显示名称.")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if len(set(value.get_secret_value())) < 5:
            raise ValueError("请使用更难猜测的密码.")
        return value


@router.get("/options")
async def auth_options(request: Request, response: Response) -> dict[str, bool]:
    settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    enabled = settings.platform.enabled
    return {
        "local_enabled": enabled and settings.local_auth.enabled,
        "registration_enabled": (
            enabled and settings.local_auth.enabled and settings.local_auth.registration_enabled
        ),
        "oidc_enabled": enabled and settings.oidc.enabled,
    }


async def guard_local_auth(request: Request) -> LocalAuthService:
    require_platform(request)
    settings = request.app.state.settings
    services = request.app.state.services
    if not settings.local_auth.enabled or services.local_auth is None:
        raise HTTPException(404, "本地账号登录未启用.")
    if request.headers.get("X-Requested-With") != "ai-agent-web":
        raise HTTPException(403, "请求来源校验失败.")
    if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "请使用 JSON 请求.")
    origin = request.headers.get("Origin")
    if origin:
        frontend = urlsplit(settings.platform.post_login_redirect_uri)
        allowed = {str(request.base_url).rstrip("/"), f"{frontend.scheme}://{frontend.netloc}"}
        if origin not in allowed:
            raise HTTPException(403, "请求来源校验失败.")
    if services.redis is None:
        raise HTTPException(503, "认证限流服务暂不可用.")
    host = request.client.host if request.client else "unknown"
    await check_limit(request, "ip:" + host, 20)
    return cast(LocalAuthService, services.local_auth)


async def check_limit(request: Request, key: str, limit: int) -> None:
    digest = hashlib.sha256(key.encode()).hexdigest()
    limiter = RedisSlidingWindowRateLimiter(request.app.state.services.redis, prefix="auth:rate")
    try:
        await limiter.check(digest, limit)
    except RateLimitExceededError as exc:
        raise HTTPException(
            429,
            "尝试次数过多, 请稍后重试.",
            headers={"Retry-After": "60"},
        ) from exc


async def login_response(request: Request, user_id: UUID) -> Response:
    settings = request.app.state.settings
    sessions = request.app.state.services.sessions
    session_id, _ = await sessions.create_session(user_id, settings.platform.session_ttl_seconds)
    old_id = request.cookies.get(settings.platform.session_cookie_name)
    if old_id:
        await sessions.delete_session(old_id)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        settings.platform.session_cookie_name,
        session_id,
        max_age=settings.platform.session_ttl_seconds,
        httponly=True,
        secure=settings.environment == Environment.PRODUCTION,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/register", status_code=204)
async def register(
    payload: RegisterInput,
    request: Request,
    service: Annotated[LocalAuthService, Depends(guard_local_auth)],
) -> Response:
    if not request.app.state.settings.local_auth.registration_enabled:
        raise HTTPException(403, "注册已关闭, 请联系管理员.")
    host = request.client.host if request.client else "unknown"
    await check_limit(request, "registration:" + host, 5)
    user = await service.register(
        payload.email,
        payload.display_name,
        payload.password.get_secret_value(),
    )
    return await login_response(request, user.id)


@router.post("/local/login", status_code=204)
async def local_login(
    payload: LoginInput,
    request: Request,
    service: Annotated[LocalAuthService, Depends(guard_local_auth)],
) -> Response:
    await check_limit(request, "email:" + payload.email, 10)
    user = await service.login(payload.email, payload.password.get_secret_value())
    return await login_response(request, user.id)
