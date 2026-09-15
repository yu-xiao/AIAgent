from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr
from sqlalchemy import select

from ai_agent.config import PlatformSettings, TokenEndpointAuthMethod
from ai_agent.connections.service import ConnectionService
from ai_agent.credentials.vault import MemoryCredentialVault
from ai_agent.errors import (
    AuthorizationError,
    CircuitOpenError,
    GatewayTimeoutError,
    ProtocolValidationError,
    RateLimitExceededError,
)
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import MemorySessionStore
from ai_agent.mcp.gateway import McpGateway
from ai_agent.mcp.models import RunContext, ToolDescriptor
from ai_agent.mcp.registry import McpServerRegistry, McpServerSpec
from ai_agent.mcp.schema import validate_instance, validate_schema
from ai_agent.mcp.tool_catalog import MemoryCatalogCache, ToolCatalogService
from ai_agent.persistence import Database
from ai_agent.persistence.models import Base, CredentialReference, McpAuthMode


class FakeMcpClient:
    def __init__(
        self, descriptors: list[ToolDescriptor], result: CallToolResult | None = None
    ) -> None:
        self.descriptors = descriptors
        self.result = result or CallToolResult(
            content=[TextContent(type="text", text="ok")],
            structuredContent={"ok": True},
        )
        self.calls: list[dict[str, Any]] = []

    async def list_tools(self) -> list[ToolDescriptor]:
        return self.descriptors

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        self.calls.append({"name": name, "arguments": arguments})
        return self.result


@pytest.fixture
async def p2_runtime():
    database_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    database_file.close()
    database_url = f"sqlite+aiosqlite:///{database_file.name.replace(chr(92), '/')}"
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    identities = IdentityService(database.session_factory)
    user = await identities.upsert_oidc_user(
        issuer="https://identity.example.test",
        subject="owner",
        display_name="Owner",
        email=None,
    )
    organization = await identities.create_organization(user.id, "P2")
    sessions = MemorySessionStore()
    vault = MemoryCredentialVault()
    registry = McpServerRegistry(database.session_factory, identities)
    connections = ConnectionService(
        database.session_factory,
        identities,
        sessions,
        vault,
        PlatformSettings(oauth_transaction_ttl_seconds=600),
    )
    yield database, identities, user, organization, registry, connections, vault
    await database.dispose()


async def test_registry_and_catalog_are_tenant_scoped(p2_runtime) -> None:
    _, _, user, organization, registry, connections, vault = p2_runtime
    server = await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="permission",
            display_name="Permission",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["permission.search"],
            tool_sets={"permission": ["permission.search"]},
        ),
    )
    await connections.create_organization_connection(
        user.id,
        organization.id,
        "permission",
        access_token=SecretStr("opaque-token"),
        allowed_tools=["permission.search"],
        allowed_tool_sets=["permission"],
    )
    fake = FakeMcpClient(
        [
            ToolDescriptor(
                name="permission.search",
                input_schema={"type": "object", "required": ["query"]},
            ),
            ToolDescriptor(name="permission.write", input_schema={"type": "object"}),
        ]
    )
    catalog = ToolCatalogService(
        registry,
        connections,
        MemoryCatalogCache(),
        client_factory=lambda *_: fake,
    )
    context = RunContext(
        organization_id=organization.id,
        user_id=user.id,
        trace_id=str(uuid4()),
    )
    tools = await catalog.list_tools(context, "permission")
    assert [item.name for item in tools] == ["permission.search"]
    assert server.code == "permission"
    connection = (await connections.list_connections(user.id, organization.id))[0]
    assert await vault.get(connection.credential_reference) is not None


async def test_empty_connection_allowlist_is_fail_closed(p2_runtime) -> None:
    _, _, user, organization, registry, connections, _ = p2_runtime
    server = await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="fail-closed",
            display_name="Fail Closed",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["read"],
        ),
    )
    with pytest.raises(ProtocolValidationError, match="explicit non-empty"):
        await connections.create_organization_connection(
            user.id,
            organization.id,
            server.code,
            access_token=SecretStr("opaque-token"),
        )
    with pytest.raises(AuthorizationError, match="exceeds"):
        await connections.create_organization_connection(
            user.id,
            organization.id,
            server.code,
            access_token=SecretStr("opaque-token"),
            allowed_tools=["write"],
        )


async def test_connection_rotation_invalidates_effective_catalog_cache(p2_runtime) -> None:
    _, _, user, organization, registry, connections, _ = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="rotated-policy",
            display_name="Rotated Policy",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["read", "write"],
        ),
    )
    await connections.create_organization_connection(
        user.id,
        organization.id,
        "rotated-policy",
        access_token=SecretStr("first-token"),
        allowed_tools=["read"],
    )
    fake = FakeMcpClient(
        [
            ToolDescriptor(name="read", input_schema={"type": "object"}),
            ToolDescriptor(name="write", input_schema={"type": "object"}),
        ]
    )
    catalog = ToolCatalogService(
        registry,
        connections,
        MemoryCatalogCache(),
        client_factory=lambda *_: fake,
    )
    context = RunContext(
        organization_id=organization.id,
        user_id=user.id,
        trace_id=str(uuid4()),
    )
    assert [tool.name for tool in await catalog.list_tools(context)] == ["read"]

    await connections.create_organization_connection(
        user.id,
        organization.id,
        "rotated-policy",
        access_token=SecretStr("second-token"),
        allowed_tools=["write"],
    )

    assert [tool.name for tool in await catalog.list_tools(context)] == ["write"]


def test_standard_json_schema_keywords_are_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "pattern": "^[a-z]+$"},
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
        },
        "required": ["code", "count"],
        "additionalProperties": False,
    }
    validate_instance({"code": "valid", "count": 2}, schema, label="test input")
    with pytest.raises(ProtocolValidationError, match="validation failed"):
        validate_instance({"code": "INVALID", "count": 4}, schema, label="test input")
    with pytest.raises(ProtocolValidationError, match="valid JSON Schema"):
        validate_schema({"type": "unknown"}, label="test schema")
    with pytest.raises(ProtocolValidationError, match="only local"):
        validate_schema(
            {"$ref": "https://schemas.example.test/tool.json"},
            label="test schema",
        )
    local_reference_schema = {
        "$defs": {"identifier": {"type": "string", "minLength": 1}},
        "properties": {"id": {"$ref": "#/$defs/identifier"}},
    }
    validate_schema(local_reference_schema, label="test schema")
    validate_instance({"id": "valid"}, local_reference_schema, label="test input")


async def test_uncommitted_organization_credential_is_revoked(
    p2_runtime, monkeypatch
) -> None:
    _, _, user, organization, registry, connections, vault = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="rollback-secret",
            display_name="Rollback Secret",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["read"],
        ),
    )

    async def fail_persistence(**_: Any):
        raise RuntimeError("database failed")

    monkeypatch.setattr(connections, "_persist_organization_connection", fail_persistence)
    with pytest.raises(RuntimeError, match="database failed"):
        await connections.create_organization_connection(
            user.id,
            organization.id,
            "rollback-secret",
            access_token=SecretStr("must-be-revoked"),
            allowed_tools=["read"],
        )

    assert vault._values == {}


async def test_pending_credential_revocation_is_retried(p2_runtime, monkeypatch) -> None:
    database, _, user, organization, registry, connections, vault = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="retry-revocation",
            display_name="Retry Revocation",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["read"],
        ),
    )
    first = await connections.create_organization_connection(
        user.id,
        organization.id,
        "retry-revocation",
        access_token=SecretStr("first-token"),
        allowed_tools=["read"],
    )
    first_reference = first.credential_reference
    original_revoke = vault.revoke

    async def fail_revoke(_: str) -> None:
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(vault, "revoke", fail_revoke)
    await connections.create_organization_connection(
        user.id,
        organization.id,
        "retry-revocation",
        access_token=SecretStr("second-token"),
        allowed_tools=["read"],
    )
    monkeypatch.setattr(vault, "revoke", original_revoke)

    assert await connections.retry_pending_revocations() == 1
    async with database.session_factory() as session:
        stored = await session.scalar(
            select(CredentialReference).where(
                CredentialReference.reference == first_reference
            )
        )
    assert stored is not None
    assert stored.status == "revoked"
    assert await vault.get(first_reference) is None


async def test_gateway_validates_input_and_enforces_rate_limit(p2_runtime) -> None:
    _, _, user, organization, registry, connections, _ = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="search",
            display_name="Search",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["search"],
            rate_limit_per_minute=1,
        ),
    )
    await connections.create_organization_connection(
        user.id,
        organization.id,
        "search",
        access_token=SecretStr("opaque-token"),
        allowed_tools=["search"],
    )
    fake = FakeMcpClient(
        [ToolDescriptor(name="search", input_schema={"type": "object", "required": ["q"]})],
        CallToolResult(structuredContent={"ok": True}, content=[]),
    )
    catalog = ToolCatalogService(
        registry,
        connections,
        MemoryCatalogCache(),
        client_factory=lambda *_: fake,
    )
    gateway = McpGateway(catalog)
    context = RunContext(organization_id=organization.id, user_id=user.id, trace_id=str(uuid4()))
    with pytest.raises(ProtocolValidationError):
        await gateway.call(context, "search", {})
    result = await gateway.call(context, "search", {"q": "users"})
    assert result.structured_content == {"ok": True}
    with pytest.raises(RateLimitExceededError):
        await gateway.call(context, "search", {"q": "again"})


async def test_gateway_timeout_opens_circuit(p2_runtime) -> None:
    _, _, user, organization, registry, connections, _ = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="slow",
            display_name="Slow",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.API_KEY,
            allowed_tools=["slow"],
            timeout_seconds=0.01,
            circuit_breaker_threshold=1,
        ),
    )
    await connections.create_organization_connection(
        user.id,
        organization.id,
        "slow",
        access_token=SecretStr("opaque-token"),
        allowed_tools=["slow"],
    )

    class SlowClient(FakeMcpClient):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
            del name, arguments
            await asyncio.sleep(1)
            return self.result

    fake = SlowClient([ToolDescriptor(name="slow", input_schema={"type": "object"})])
    catalog = ToolCatalogService(
        registry,
        connections,
        MemoryCatalogCache(),
        client_factory=lambda *_: fake,
    )
    gateway = McpGateway(catalog)
    context = RunContext(organization_id=organization.id, user_id=user.id, trace_id=str(uuid4()))
    with pytest.raises(GatewayTimeoutError):
        await gateway.call(context, "slow", {})
    with pytest.raises(CircuitOpenError):
        await gateway.call(context, "slow", {})


@respx.mock
async def test_personal_oauth_connection_uses_one_time_state_and_vault(p2_runtime) -> None:
    _, _, user, organization, registry, connections, vault = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="oauth",
            display_name="OAuth",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.OAUTH_AUTHORIZATION_CODE,
            authorization_server="https://id.example.test",
            oauth_client_id="agent-client",
            required_scope="business.read",
        ),
    )
    issuer = "https://id.example.test"
    authorization_endpoint = f"{issuer}/authorize"
    token_endpoint = f"{issuer}/token"
    jwks_uri = f"{issuer}/jwks"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key())
    respx.get(f"{issuer}/.well-known/openid-configuration").mock(
        return_value=Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": authorization_endpoint,
                "token_endpoint": token_endpoint,
                "jwks_uri": jwks_uri,
                "code_challenge_methods_supported": ["S256"],
            },
        )
    )
    respx.get(jwks_uri).mock(
        return_value=Response(200, json={"keys": [{**json.loads(public_jwk), "kid": "key-1"}]})
    )
    url = await connections.begin_personal_authorization(user.id, organization.id, "oauth")
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(url).query)
    state = query["state"][0]
    nonce = query["nonce"][0]
    token_route = respx.post("https://id.example.test/token").mock(
        return_value=Response(
            200,
            json={
                "access_token": "external-access",
                "refresh_token": "external-refresh",
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "business.read",
                "id_token": jwt.encode(
                    {
                        "iss": issuer,
                        "aud": "agent-client",
                        "sub": "business-user-1",
                        "nonce": nonce,
                        "iat": datetime.now(UTC),
                        "exp": datetime.now(UTC) + timedelta(minutes=5),
                    },
                    key=private_key,
                    algorithm="RS256",
                    headers={"kid": "key-1"},
                ),
            },
        )
    )
    connection = await connections.finish_personal_authorization(
        user.id,
        "oauth",
        code="one-time",
        state=state,
    )
    assert token_route.called
    assert connection.external_subject == "business-user-1"
    assert (await vault.get(connection.credential_reference)) is not None
    with pytest.raises(Exception, match=r"missing|expired|already"):
        await connections.finish_personal_authorization(
            user.id,
            "oauth",
            code="one-time",
            state=state,
        )


def test_mcp_server_spec_allows_local_target_for_runtime_policy() -> None:
    spec = McpServerSpec(
        code="local",
        display_name="Local",
        system_code="internal",
        mcp_url="http://127.0.0.1:8000/mcp",
        auth_mode=McpAuthMode.API_KEY,
    )

    assert spec.mcp_url == "http://127.0.0.1:8000/mcp"


async def test_connection_center_api_hides_secret_and_requires_csrf(platform_runtime) -> None:
    runtime = platform_runtime
    runtime.settings.mcp_gateway.enabled = True
    vault = MemoryCredentialVault()
    registry = McpServerRegistry(
        runtime.services.database.session_factory, runtime.services.identities
    )
    connections = ConnectionService(
        runtime.services.database.session_factory,
        runtime.services.identities,
        runtime.services.sessions,
        vault,
        runtime.settings.platform,
    )
    runtime.services.vault = vault
    runtime.services.mcp_registry = registry
    runtime.services.connections = connections
    without_csrf = await runtime.client.post(
        "/api/v1/admin/mcp-servers",
        headers={"X-Organization-Id": str(runtime.organization.id)},
        json={
            "code": "api-server",
            "display_name": "API Server",
            "system_code": "permission-system",
            "mcp_url": "https://mcp.example.test/mcp",
            "auth_mode": "api_key",
            "allowed_tools": ["read"],
        },
    )
    assert without_csrf.status_code == 403
    created = await runtime.client.post(
        "/api/v1/admin/mcp-servers",
        headers=runtime.headers,
        json={
            "code": "api-server",
            "display_name": "API Server",
            "system_code": "permission-system",
            "mcp_url": "https://mcp.example.test/mcp",
            "auth_mode": "api_key",
            "allowed_tools": ["read"],
        },
    )
    assert created.status_code == 201
    connection = await runtime.client.post(
        "/api/v1/organization-connections",
        headers=runtime.headers,
        json={
            "server_code": "api-server",
            "access_token": "do-not-return",
            "allowed_tools": ["read"],
        },
    )
    assert connection.status_code == 201
    assert "access_token" not in connection.text
    listed = await runtime.client.get("/api/v1/connections", headers=runtime.headers)
    assert listed.status_code == 200
    assert listed.json()[0]["status"] == "active"


@respx.mock
async def test_organization_client_credentials_are_exchanged_before_storage(p2_runtime) -> None:
    _, _, user, organization, registry, connections, vault = p2_runtime
    await registry.create(
        user.id,
        organization.id,
        McpServerSpec(
            code="service",
            display_name="Service",
            system_code="permission-system",
            mcp_url="https://mcp.example.test/mcp",
            auth_mode=McpAuthMode.CLIENT_CREDENTIALS,
            token_endpoint="https://id.example.test/token",
            token_endpoint_auth_method=TokenEndpointAuthMethod.CLIENT_SECRET_BASIC,
            required_scope="business.read",
            allowed_tools=["read"],
        ),
    )
    route = respx.post("https://id.example.test/token").mock(
        return_value=Response(
            200,
            json={"access_token": "service-access", "expires_in": 300},
        )
    )
    connection = await connections.create_organization_connection(
        user.id,
        organization.id,
        "service",
        client_id="service-client",
        client_secret=SecretStr("service-secret"),
        allowed_tools=["read"],
    )
    assert route.call_count == 1
    stored = await vault.get(connection.credential_reference)
    assert stored is not None
    assert stored.access_token.get_secret_value() == "service-access"
