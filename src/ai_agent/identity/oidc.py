"""OIDC login orchestration with PKCE, nonce validation and opaque sessions."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK
from pydantic import SecretStr

from ai_agent.config import OidcSettings, PlatformSettings
from ai_agent.errors import AuthenticationError, ProtocolValidationError
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import SessionStore, StoredOAuthTransaction
from ai_agent.oauth.client import AuthorizationCodeClient, AuthorizationTransaction, DiscoveryClient
from ai_agent.oauth.models import OidcProviderMetadata


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_id: str
    csrf_token: str


class OidcLoginService:
    def __init__(
        self,
        oidc: OidcSettings,
        platform: PlatformSettings,
        sessions: SessionStore,
        identities: IdentityService,
    ) -> None:
        self._oidc = oidc
        self._platform = platform
        self._sessions = sessions
        self._identities = identities
        self._discovery = DiscoveryClient()

    async def begin(self) -> str:
        metadata = await self._discovery.discover_oidc(self._oidc.issuer)
        _require_secure_metadata(metadata, self._platform)
        if "S256" not in metadata.code_challenge_methods_supported:
            raise ProtocolValidationError("OIDC provider does not advertise PKCE S256 support.")
        transaction = self._client().create_authorization_transaction(
            metadata.authorization_endpoint
        )
        await self._sessions.save_oauth_transaction(
            StoredOAuthTransaction(
                state=transaction.state,
                nonce=transaction.nonce,
                code_verifier=transaction.code_verifier.get_secret_value(),
            ),
            self._platform.oauth_transaction_ttl_seconds,
        )
        return transaction.authorization_url

    async def finish(self, *, code: str, state: str) -> LoginResult:
        stored = await self._sessions.pop_oauth_transaction(state)
        if stored is None:
            raise AuthenticationError("OAuth transaction is missing, expired, or already used.")
        metadata = await self._discovery.discover_oidc(self._oidc.issuer)
        _require_secure_metadata(metadata, self._platform)
        transaction = AuthorizationTransaction(
            authorization_url="",
            state=stored.state,
            nonce=stored.nonce,
            code_verifier=SecretStr(stored.code_verifier),
        )
        token = await self._client().exchange_code(
            token_endpoint=metadata.token_endpoint,
            code=code,
            transaction=transaction,
            returned_state=state,
        )
        if token.id_token is None or metadata.jwks_uri is None:
            raise AuthenticationError("OIDC response is missing a verifiable ID token.")
        claims = await self._validate_id_token(
            token.id_token.get_secret_value(), metadata.jwks_uri, stored.nonce
        )
        user = await self._identities.upsert_oidc_user(
            issuer=self._oidc.issuer,
            subject=_required_text(claims, "sub"),
            display_name=_display_name(claims),
            email=_optional_text(claims, "email"),
        )
        session_id, session = await self._sessions.create_session(
            user.id, self._platform.session_ttl_seconds
        )
        return LoginResult(session_id=session_id, csrf_token=session.csrf_token)

    def _client(self) -> AuthorizationCodeClient:
        return AuthorizationCodeClient(
            client_id=self._oidc.client_id,
            client_secret=self._oidc.client_secret,
            redirect_uri=self._oidc.redirect_uri,
            scope=self._oidc.scope,
            token_endpoint_auth_method=self._oidc.token_endpoint_auth_method,
        )

    async def _validate_id_token(
        self, encoded: str, jwks_uri: str, expected_nonce: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(encoded)
            algorithm = header.get("alg")
            key_id = header.get("kid")
            if algorithm not in self._oidc.signing_algorithms or not isinstance(key_id, str):
                raise AuthenticationError("OIDC ID token uses an untrusted signing key.")
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(jwks_uri, headers={"Accept": "application/json"})
                response.raise_for_status()
                keys = response.json().get("keys", [])
            key_data = next((item for item in keys if item.get("kid") == key_id), None)
            if key_data is None:
                raise AuthenticationError("OIDC signing key was not found.")
            key = PyJWK.from_dict(key_data, algorithm=algorithm).key
            claims = jwt.decode(
                encoded,
                key=key,
                algorithms=list(self._oidc.signing_algorithms),
                audience=self._oidc.client_id,
                issuer=self._oidc.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
            nonce = _required_text(claims, "nonce")
            if not secrets.compare_digest(nonce.encode(), expected_nonce.encode()):
                raise AuthenticationError("OIDC nonce validation failed.")
            return dict(claims)
        except AuthenticationError:
            raise
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationError("OIDC ID token validation failed.") from exc


def _required_text(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"OIDC claim '{name}' is missing or invalid.")
    return value


def _optional_text(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _display_name(claims: dict[str, Any]) -> str:
    for name in ("name", "preferred_username", "email", "sub"):
        value = _optional_text(claims, name)
        if value:
            return value[:200]
    raise AuthenticationError("OIDC identity has no usable display name.")


def _require_secure_metadata(metadata: OidcProviderMetadata, platform: PlatformSettings) -> None:
    if platform.post_login_redirect_uri.startswith("https://"):
        endpoints = (
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            metadata.jwks_uri,
        )
        if any(endpoint and urlsplit(endpoint).scheme != "https" for endpoint in endpoints):
            raise AuthenticationError(
                "OIDC provider metadata must use HTTPS for the configured redirect."
            )
