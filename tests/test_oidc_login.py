from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response
from pydantic import SecretStr

from ai_agent.config import OidcSettings, PlatformSettings, TokenEndpointAuthMethod
from ai_agent.identity.oidc import OidcLoginService
from ai_agent.identity.sessions import MemorySessionStore


@dataclass(frozen=True, slots=True)
class FakeUser:
    id: UUID


class FakeIdentityService:
    def __init__(self) -> None:
        self.user = FakeUser(uuid4())
        self.calls: list[dict[str, str | None]] = []

    async def upsert_oidc_user(self, **kwargs: str | None) -> FakeUser:
        self.calls.append(kwargs)
        return self.user


@respx.mock
async def test_oidc_login_validates_jwt_nonce_and_consumes_transaction() -> None:
    issuer = "https://identity.example.test/agent"
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
    identity = FakeIdentityService()
    sessions = MemorySessionStore()
    service = OidcLoginService(
        OidcSettings(
            enabled=True,
            issuer=issuer,
            client_id="agent-client",
            client_secret=SecretStr(""),
            token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
            redirect_uri="https://agent.example.test/callback",
        ),
        PlatformSettings(enabled=True),
        sessions,
        identity,  # type: ignore[arg-type]
    )
    authorization_url = await service.begin()
    state = authorization_url.split("state=", 1)[1].split("&", 1)[0]
    transaction = await sessions.pop_oauth_transaction(state)
    assert transaction is not None
    await sessions.save_oauth_transaction(transaction, 600)
    id_token = jwt.encode(
        {
            "iss": issuer,
            "aud": "agent-client",
            "sub": "oidc-user",
            "name": "OIDC User",
            "email": "oidc@example.test",
            "nonce": transaction.nonce,
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )
    respx.post(token_endpoint).mock(
        return_value=Response(
            200,
            json={
                "access_token": "access-token",
                "id_token": id_token,
                "token_type": "Bearer",
                "expires_in": 300,
            },
        )
    )

    result = await service.finish(code="one-time-code", state=state)
    assert result.session_id
    assert result.csrf_token
    assert identity.calls == [
        {
            "issuer": issuer,
            "subject": "oidc-user",
            "display_name": "OIDC User",
            "email": "oidc@example.test",
        }
    ]
    assert await sessions.pop_oauth_transaction(state) is None
