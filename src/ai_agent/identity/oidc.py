"""OIDC login orchestration with PKCE, nonce validation and opaque sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from ai_agent.config import OidcSettings, PlatformSettings
from ai_agent.errors import AuthenticationError, ProtocolValidationError
from ai_agent.identity.service import IdentityService
from ai_agent.identity.sessions import SessionStore, StoredOAuthTransaction
from ai_agent.oauth.client import AuthorizationCodeClient, AuthorizationTransaction, DiscoveryClient
from ai_agent.oauth.models import OidcProviderMetadata
from ai_agent.oauth.validator import OidcIdTokenValidator


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
        self._id_token_validator = OidcIdTokenValidator(
            signing_algorithms=oidc.signing_algorithms,
        )

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
            token.id_token.get_secret_value(), metadata, stored.nonce
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
        self, encoded: str, metadata: OidcProviderMetadata, expected_nonce: str
    ) -> dict[str, Any]:
        return await self._id_token_validator.validate(
            encoded,
            metadata,
            client_id=self._oidc.client_id,
            expected_nonce=expected_nonce,
        )


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
