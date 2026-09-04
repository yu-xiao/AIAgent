from __future__ import annotations

import base64

import respx
from httpx import Request, Response
from pydantic import SecretStr

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.mcp.auth import ClientCredentialsTokenProvider


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
