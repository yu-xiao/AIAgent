from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta

import respx
from httpx import Request, Response
from pydantic import SecretStr

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.credentials.vault import Credential, MemoryCredentialVault
from ai_agent.mcp.auth import ClientCredentialsTokenProvider, VaultAccessTokenProvider


@respx.mock
async def test_client_credentials_token_is_authenticated_and_cached() -> None:
    def token_response(request: Request) -> Response:
        expected = base64.b64encode(b"probe-client:probe-secret").decode("ascii")
        assert request.headers["authorization"] == f"Basic {expected}"
        assert b"grant_type=client_credentials" in request.content
        assert b"scope=permission-system-mcp" in request.content
        return Response(
            200,
            json={"access_token": "short-lived", "token_type": "Bearer", "expires_in": 300},
        )

    route = respx.post("https://id.example.test/token").mock(side_effect=token_response)
    provider = ClientCredentialsTokenProvider(
        token_url="https://id.example.test/token",
        client_id="probe-client",
        client_secret=SecretStr("probe-secret"),
        scope="permission-system-mcp",
        auth_method=TokenEndpointAuthMethod.CLIENT_SECRET_BASIC,
        timeout_seconds=5,
    )

    first = await provider.get_access_token()
    second = await provider.get_access_token()

    assert first.get_secret_value() == "short-lived"
    assert second.get_secret_value() == "short-lived"
    assert route.call_count == 1


@respx.mock
async def test_expired_vault_client_credentials_are_refreshed_once() -> None:
    vault = MemoryCredentialVault()
    reference = await vault.put(
        Credential(
            access_token=SecretStr("expired"),
            client_id="service-client",
            client_secret=SecretStr("service-secret"),
            token_url="https://id.example.test/token",
            scope="business.read",
            token_endpoint_auth_method=TokenEndpointAuthMethod.CLIENT_SECRET_BASIC.value,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    route = respx.post("https://id.example.test/token").mock(
        return_value=Response(
            200,
            json={"access_token": "renewed", "token_type": "Bearer", "expires_in": 600},
        )
    )
    providers = [VaultAccessTokenProvider(vault, reference) for _ in range(3)]

    tokens = await asyncio.gather(*(provider.get_access_token() for provider in providers))

    assert [token.get_secret_value() for token in tokens] == ["renewed"] * 3
    assert route.call_count == 1
    stored = await vault.get(reference)
    assert stored is not None
    assert stored.access_token.get_secret_value() == "renewed"
