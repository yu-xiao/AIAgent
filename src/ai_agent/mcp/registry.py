"""Trusted MCP Server registration and policy persistence."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.errors import (
    ConflictError,
    ProtocolValidationError,
    ResourceNotFoundError,
)
from ai_agent.identity.service import MCP_SERVER_MANAGE, MCP_SERVER_VIEW, IdentityService
from ai_agent.persistence.models import AuditLog, McpAuthMode, McpServerDefinition, McpTransport


class McpServerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=200)
    system_code: str = Field(min_length=1, max_length=100)
    mcp_url: str = Field(min_length=1, max_length=2_000)
    transport: McpTransport = McpTransport.STREAMABLE_HTTP
    auth_mode: McpAuthMode
    credential_header: str = Field(
        default="Authorization", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$"
    )
    authorization_server: str | None = Field(default=None, max_length=2_000)
    authorization_endpoint: str | None = Field(default=None, max_length=2_000)
    token_endpoint: str | None = Field(default=None, max_length=2_000)
    oauth_client_id: str | None = Field(default=None, max_length=300)
    token_endpoint_auth_method: TokenEndpointAuthMethod = TokenEndpointAuthMethod.NONE
    oauth_client_secret_reference: str | None = Field(default=None, max_length=300)
    redirect_uri: str | None = Field(default=None, max_length=2_000)
    required_scope: str = Field(default="", max_length=500)
    allowed_tools: list[str] = Field(default_factory=list, max_length=500)
    tool_sets: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    risk_level: str = Field(default="low", min_length=1, max_length=30)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    max_concurrency: int = Field(default=10, ge=1, le=1_000)
    circuit_breaker_threshold: int = Field(default=3, ge=1, le=100)
    circuit_breaker_recovery_seconds: float = Field(default=30.0, gt=0, le=3_600)
    response_size_limit: int = Field(default=1_000_000, ge=1_024, le=50_000_000)
    enabled: bool = True

    @field_validator("mcp_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        _validate_mcp_url(value)
        return value

    @field_validator("authorization_server", "authorization_endpoint", "token_endpoint")
    @classmethod
    def validate_optional_url(cls, value: str | None) -> str | None:
        if value:
            _validate_http_url(value)
        return value

    @field_validator("redirect_uri")
    @classmethod
    def validate_redirect_uri(cls, value: str | None) -> str | None:
        if value:
            _validate_http_url(value)
        return value


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    mcp_url: str | None = Field(default=None, max_length=2_000)
    auth_mode: McpAuthMode | None = None
    credential_header: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9-]+$"
    )
    authorization_server: str | None = Field(default=None, max_length=2_000)
    authorization_endpoint: str | None = Field(default=None, max_length=2_000)
    token_endpoint: str | None = Field(default=None, max_length=2_000)
    oauth_client_id: str | None = Field(default=None, max_length=300)
    token_endpoint_auth_method: TokenEndpointAuthMethod | None = None
    oauth_client_secret_reference: str | None = Field(default=None, max_length=300)
    redirect_uri: str | None = Field(default=None, max_length=2_000)
    required_scope: str | None = Field(default=None, max_length=500)
    allowed_tools: list[str] | None = Field(default=None, max_length=500)
    tool_sets: dict[str, list[str]] | None = Field(default=None, max_length=100)
    risk_level: str | None = Field(default=None, min_length=1, max_length=30)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)
    max_concurrency: int | None = Field(default=None, ge=1, le=1_000)
    circuit_breaker_threshold: int | None = Field(default=None, ge=1, le=100)
    circuit_breaker_recovery_seconds: float | None = Field(default=None, gt=0, le=3_600)
    response_size_limit: int | None = Field(default=None, ge=1_024, le=50_000_000)
    enabled: bool | None = None

    @field_validator("mcp_url")
    @classmethod
    def validate_mcp_url(cls, value: str | None) -> str | None:
        if value:
            _validate_mcp_url(value)
        return value

    @field_validator("authorization_server", "authorization_endpoint", "token_endpoint")
    @classmethod
    def validate_optional_urls(cls, value: str | None) -> str | None:
        if value:
            _validate_http_url(value)
        return value

    @field_validator("redirect_uri")
    @classmethod
    def validate_update_redirect_uri(cls, value: str | None) -> str | None:
        if value:
            _validate_http_url(value)
        return value


class McpServerRegistry:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identities: IdentityService,
    ) -> None:
        self._session_factory = session_factory
        self._identities = identities

    async def create(
        self,
        actor_id: UUID,
        organization_id: UUID,
        spec: McpServerSpec,
        *,
        trace_id: UUID | None = None,
    ) -> McpServerDefinition:
        await self._identities.access(actor_id, organization_id, MCP_SERVER_MANAGE)
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(McpServerDefinition).where(
                    McpServerDefinition.organization_id == organization_id,
                    McpServerDefinition.code == spec.code,
                )
            )
            if existing:
                raise ConflictError("MCP Server code is already registered in this organization.")
            server = McpServerDefinition(
                organization_id=organization_id,
                **spec.model_dump(),
            )
            session.add(server)
            await session.flush()
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="mcp_server.created",
                    resource_type="mcp_server",
                    resource_id=str(server.id),
                    trace_id=trace_id,
                    details={"code": server.code, "mcp_url": server.mcp_url},
                )
            )
            return server

    async def list(
        self, actor_id: UUID, organization_id: UUID, *, include_disabled: bool = True
    ) -> list[McpServerDefinition]:
        await self._identities.access(actor_id, organization_id, MCP_SERVER_VIEW)
        async with self._session_factory() as session:
            query = select(McpServerDefinition).where(
                McpServerDefinition.organization_id == organization_id
            )
            if not include_disabled:
                query = query.where(McpServerDefinition.enabled.is_(True))
            return list(await session.scalars(query.order_by(McpServerDefinition.code)))

    async def get_by_code(
        self, actor_id: UUID, organization_id: UUID, code: str, *, require_enabled: bool = True
    ) -> McpServerDefinition:
        await self._identities.access(actor_id, organization_id, MCP_SERVER_VIEW)
        async with self._session_factory() as session:
            query = select(McpServerDefinition).where(
                McpServerDefinition.organization_id == organization_id,
                McpServerDefinition.code == code,
            )
            if require_enabled:
                query = query.where(McpServerDefinition.enabled.is_(True))
            server = await session.scalar(query)
            if server is None:
                raise ResourceNotFoundError("MCP Server is not registered or enabled.")
            return server

    async def require_tool_access(
        self, actor_id: UUID, organization_id: UUID, system_code: str
    ) -> None:
        await self._identities.require_tool_access(actor_id, organization_id, system_code)

    async def update(
        self,
        actor_id: UUID,
        organization_id: UUID,
        server_id: UUID,
        changes: McpServerUpdate,
        *,
        trace_id: UUID | None = None,
    ) -> McpServerDefinition:
        await self._identities.access(actor_id, organization_id, MCP_SERVER_MANAGE)
        async with self._session_factory() as session, session.begin():
            server = await session.scalar(
                select(McpServerDefinition)
                .where(
                    McpServerDefinition.id == server_id,
                    McpServerDefinition.organization_id == organization_id,
                )
                .with_for_update()
            )
            if server is None:
                raise ResourceNotFoundError("MCP Server not found.")
            values = changes.model_dump(exclude_unset=True)
            if "mcp_url" in values:
                _validate_mcp_url(values["mcp_url"])
            for name, value in values.items():
                setattr(server, name, value)
            server.config_version += 1
            await session.flush()
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="mcp_server.updated",
                    resource_type="mcp_server",
                    resource_id=str(server.id),
                    trace_id=trace_id,
                    details={"code": server.code, "config_version": server.config_version},
                )
            )
            return server


def _validate_mcp_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProtocolValidationError("MCP Server URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ProtocolValidationError("MCP Server URL must not contain credentials or fragments.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ProtocolValidationError("MCP Server URL must not target localhost.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise ProtocolValidationError(
            "MCP Server URL must not target a private or reserved address."
        )


def _validate_http_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProtocolValidationError("URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ProtocolValidationError("URL must not contain credentials or fragments.")
