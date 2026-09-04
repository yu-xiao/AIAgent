from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
import respx
from httpx import Response
from pydantic import SecretStr

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.errors import AuthenticationError, ProtocolValidationError
from ai_agent.oauth.client import AuthorizationCodeClient, DiscoveryClient


@respx.mock
async def test_oidc_discovery_validates_issuer() -> None:
    issuer = "https://id.example.test/agent"
    respx.get(f"{issuer}/.well-known/openid-configuration").mock(
        return_value=Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "jwks_uri": f"{issuer}/jwks",
                "code_challenge_methods_supported": ["S256"],
            },
        )
    )

    metadata = await DiscoveryClient().discover_oidc(issuer)

    assert metadata.issuer == issuer
    assert "S256" in metadata.code_challenge_methods_supported


@respx.mock
async def test_protected_resource_discovery() -> None:
    mcp_url = "https://business.example.test/mcp"
    respx.get("https://business.example.test/.well-known/oauth-protected-resource/mcp").mock(
        return_value=Response(
            200,
            json={
                "resource": mcp_url,
                "authorization_servers": ["https://id.example.test"],
                "scopes_supported": ["permission-system-mcp"],
            },
        )
    )

    metadata = await DiscoveryClient().discover_protected_resource(
        mcp_url,
        required_scope="permission-system-mcp",
    )

    assert metadata.resource == mcp_url


@respx.mock
async def test_protected_resource_rejects_missing_scope() -> None:
    mcp_url = "https://business.example.test/mcp"
    respx.get("https://business.example.test/.well-known/oauth-protected-resource/mcp").mock(
        return_value=Response(
            200,
            json={
                "resource": mcp_url,
                "authorization_servers": ["https://id.example.test"],
                "scopes_supported": ["another-scope"],
            },
        )
    )
    respx.get("https://business.example.test/.well-known/oauth-protected-resource").mock(
        return_value=Response(404)
    )

    with pytest.raises(ProtocolValidationError):
        await DiscoveryClient().discover_protected_resource(
            mcp_url,
            required_scope="permission-system-mcp",
        )


def test_authorization_transaction_contains_pkce_state_and_nonce() -> None:
    client = _authorization_client()

    transaction = client.create_authorization_transaction("https://id.example.test/authorize")
    query = parse_qs(urlsplit(transaction.authorization_url).query)

    assert query["state"] == [transaction.state]
    assert query["nonce"] == [transaction.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert transaction.code_verifier.get_secret_value() not in transaction.authorization_url


@respx.mock
async def test_exchange_code_and_refresh_token() -> None:
    token_route = respx.post("https://id.example.test/token").mock(
        side_effect=[
            Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            ),
            Response(
                200,
                json={
                    "access_token": "access-2",
                    "token_type": "Bearer",
                    "expires_in": 300,
                },
            ),
        ]
    )
    client = _authorization_client()
    transaction = client.create_authorization_transaction("https://id.example.test/authorize")

    token = await client.exchange_code(
        token_endpoint="https://id.example.test/token",
        code="one-time-code",
        transaction=transaction,
        returned_state=transaction.state,
    )
    refreshed = await client.refresh_token(
        token_endpoint="https://id.example.test/token",
        refresh_token=SecretStr("refresh-1"),
    )

    assert token.access_token.get_secret_value() == "access-1"
    assert refreshed.access_token.get_secret_value() == "access-2"
    assert token_route.call_count == 2


async def test_exchange_code_rejects_wrong_state_before_http_call() -> None:
    client = _authorization_client()
    transaction = client.create_authorization_transaction("https://id.example.test/authorize")

    with pytest.raises(AuthenticationError, match="state"):
        await client.exchange_code(
            token_endpoint="https://id.example.test/token",
            code="one-time-code",
            transaction=transaction,
            returned_state="wrong-state",
        )


def _authorization_client() -> AuthorizationCodeClient:
    return AuthorizationCodeClient(
        client_id="agent-client",
        client_secret=SecretStr("agent-secret"),
        redirect_uri="https://agent.example.test/callback",
        scope="openid profile",
        token_endpoint_auth_method=TokenEndpointAuthMethod.CLIENT_SECRET_BASIC,
    )
