"""Per-user MCP Tool Catalog discovery and cache isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ai_agent.connections.service import ConnectionService
from ai_agent.credentials.vault import CredentialVault
from ai_agent.errors import AuthorizationError, McpConnectionError, ResourceNotFoundError
from ai_agent.mcp.client import McpToolClient
from ai_agent.mcp.models import RunContext, ToolDefinition, ToolDescriptor
from ai_agent.mcp.network_policy import McpNetworkPolicy
from ai_agent.mcp.registry import McpServerRegistry
from ai_agent.persistence.models import ExternalConnection, McpServerDefinition


class CatalogCache:
    async def get(
        self, key: str
    ) -> list[ToolDefinition] | None:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    async def set(self, key: str, value: list[ToolDefinition], ttl_seconds: int) -> None:
        raise NotImplementedError

    async def delete(self, prefix: str) -> None:
        raise NotImplementedError


class MemoryCatalogCache(CatalogCache):
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, list[ToolDefinition]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> list[ToolDefinition] | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            return [tool.model_copy(deep=True) for tool in value]

    async def set(self, key: str, value: list[ToolDefinition], ttl_seconds: int) -> None:
        async with self._lock:
            self._values[key] = (
                time.monotonic() + ttl_seconds,
                [tool.model_copy(deep=True) for tool in value],
            )

    async def delete(self, prefix: str) -> None:
        async with self._lock:
            for key in list(self._values):
                if key.startswith(prefix):
                    self._values.pop(key, None)


class RedisCatalogCache(CatalogCache):
    def __init__(self, redis: Any, prefix: str = "mcp-catalog:") -> None:
        self._redis = redis
        self._prefix = prefix

    async def get(self, key: str) -> list[ToolDefinition] | None:
        value = await self._redis.get(self._prefix + key)
        if value is None:
            return None
        payload = json.loads(value.decode() if isinstance(value, bytes) else value)
        return [ToolDefinition.model_validate(item) for item in payload]

    async def set(self, key: str, value: list[ToolDefinition], ttl_seconds: int) -> None:
        payload = json.dumps(
            [item.model_dump(mode="json") for item in value], separators=(",", ":")
        )
        await self._redis.set(self._prefix + key, payload, ex=ttl_seconds)

    async def delete(self, prefix: str) -> None:
        cursor = 0
        pattern = self._prefix + prefix + "*"
        while True:
            cursor, keys = await self._redis.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                await self._redis.delete(*keys)
            if cursor == 0:
                break


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    definition: ToolDefinition
    server: McpServerDefinition
    connection: ExternalConnection


class ToolCatalogService:
    def __init__(
        self,
        registry: McpServerRegistry,
        connections: ConnectionService,
        cache: CatalogCache | None = None,
        *,
        ttl_seconds: int = 300,
        client_factory: Callable[
            [McpServerDefinition, ExternalConnection, RunContext], McpToolClient
        ]
        | None = None,
        vault: CredentialVault | None = None,
        network_policy: McpNetworkPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._connections = connections
        self._cache = cache or MemoryCatalogCache()
        self._ttl_seconds = ttl_seconds
        self._vault = vault
        self._network_policy = network_policy or McpNetworkPolicy()
        self._client_factory = client_factory or self._default_client

    async def list_tools(
        self,
        context: RunContext,
        tool_set: str = "",
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> list[ToolDefinition]:
        servers = await self._registry.list(
            context.user_id, context.organization_id, include_disabled=False
        )
        result: list[ToolDefinition] = []
        for server in servers:
            if system_code is not None and server.system_code != system_code:
                continue
            try:
                await self._registry.require_tool_access(
                    context.user_id, context.organization_id, server.system_code
                )
                get_connection = (
                    self._connections.get_personal_connection
                    if personal_only
                    else self._connections.get_usable_connection
                )
                connection = await get_connection(
                    context.user_id, context.organization_id, server.id
                )
            except (AuthorizationError, ResourceNotFoundError):
                continue
            grants = await self._load_grants(connection.id)
            key = self._cache_key(context, server, connection, tool_set, grants)
            cached = await self._cache.get(key)
            if cached is None:
                client = self._client_factory(server, connection, context)
                try:
                    descriptors = await client.list_tools()
                except McpConnectionError:
                    continue
                cached = self._filter(server, connection, descriptors, tool_set, grants)
                await self._cache.set(key, cached, self._ttl_seconds)
            result.extend(cached)
        return _deduplicate(result)

    async def resolve_tool(
        self,
        context: RunContext,
        name: str,
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> ResolvedTool:
        servers = await self._registry.list(
            context.user_id, context.organization_id, include_disabled=False
        )
        for server in servers:
            if system_code is not None and server.system_code != system_code:
                continue
            try:
                await self._registry.require_tool_access(
                    context.user_id, context.organization_id, server.system_code
                )
                get_connection = (
                    self._connections.get_personal_connection
                    if personal_only
                    else self._connections.get_usable_connection
                )
                connection = await get_connection(
                    context.user_id, context.organization_id, server.id
                )
            except (AuthorizationError, ResourceNotFoundError):
                continue
            grants = await self._load_grants(connection.id)
            key = self._cache_key(context, server, connection, "", grants)
            cached = await self._cache.get(key)
            if cached is None:
                try:
                    descriptors = await self._client_factory(
                        server, connection, context
                    ).list_tools()
                except McpConnectionError:
                    continue
                cached = self._filter(server, connection, descriptors, "", grants)
                await self._cache.set(key, cached, self._ttl_seconds)
            for definition in cached:
                if definition.name == name:
                    return ResolvedTool(definition, server, connection)
        raise ResourceNotFoundError("Requested MCP Tool is not available for this connection.")

    def client_for(self, resolved: ResolvedTool, context: RunContext) -> McpToolClient:
        return self._client_factory(resolved.server, resolved.connection, context)

    async def invalidate_server(self, organization_id: UUID, server_id: UUID) -> None:
        # Server configuration changes invalidate every user-specific view in the tenant.
        await self._cache.delete(f"{organization_id}:")

    async def _load_grants(self, connection_id: UUID) -> set[str]:
        method = getattr(self._connections, "list_tool_grants", None)
        if method is None:
            return set()
        return set(await method(connection_id))

    def _filter(
        self,
        server: McpServerDefinition,
        connection: ExternalConnection,
        descriptors: list[ToolDescriptor],
        tool_set: str,
        grants: set[str],
    ) -> list[ToolDefinition]:
        server_allowed = set(server.allowed_tools) or None
        connection_allowed = set(connection.allowed_tools) or None
        if server_allowed is not None and connection_allowed is not None:
            allowed: set[str] | None = server_allowed & connection_allowed
        else:
            allowed = server_allowed or connection_allowed
        selected_names = set(server.tool_sets.get(tool_set, [])) if tool_set else None
        if (
            tool_set
            and connection.allowed_tool_sets
            and tool_set not in set(connection.allowed_tool_sets)
        ):
            return []
        result: list[ToolDefinition] = []
        for descriptor in descriptors:
            if allowed is not None and descriptor.name not in allowed:
                continue
            if selected_names is not None and descriptor.name not in selected_names:
                continue
            if grants and descriptor.name not in grants:
                continue
            result.append(
                ToolDefinition(
                    name=descriptor.name,
                    description=descriptor.description,
                    input_schema=descriptor.input_schema,
                    output_schema=descriptor.output_schema,
                    server_code=server.code,
                    risk_level=server.risk_level,
                )
            )
        return result

    def _cache_key(
        self,
        context: RunContext,
        server: McpServerDefinition,
        connection: ExternalConnection,
        tool_set: str,
        grants: set[str],
    ) -> str:
        scope_digest = _scope_digest(connection.scopes)
        return ":".join(
            (
                str(context.organization_id),
                str(context.user_id),
                str(server.id),
                str(connection.id),
                scope_digest,
                _scope_digest(sorted(grants)),
                str(server.config_version),
                tool_set,
            )
        )

    def _default_client(
        self, server: McpServerDefinition, connection: ExternalConnection, context: RunContext
    ) -> McpToolClient:
        from ai_agent.mcp.auth import VaultAccessTokenProvider

        if self._vault is None:
            raise RuntimeError("ToolCatalogService requires a credential vault.")
        return McpToolClient(
            mcp_url=server.mcp_url,
            token_provider=VaultAccessTokenProvider(self._vault, connection.credential_reference),
            timeout_seconds=server.timeout_seconds,
            credential_header=server.credential_header,
            network_policy=self._network_policy,
            extra_headers={
                "X-Trace-Id": context.trace_id,
                "X-Agent-User-Id": str(context.user_id),
                "X-Agent-Organization-Id": str(context.organization_id),
            },
        )


def _scope_digest(scopes: list[str]) -> str:
    serialized = json.dumps(sorted(scopes), separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _deduplicate(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    seen: set[str] = set()
    result: list[ToolDefinition] = []
    for tool in tools:
        if tool.name not in seen:
            seen.add(tool.name)
            result.append(tool)
    return result
