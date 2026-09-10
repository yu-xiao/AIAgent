"""OAuth discovery, PKCE authorization requests and token operations."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import SecretStr, ValidationError

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.errors import AuthenticationError, ProtocolValidationError
from ai_agent.oauth.models import OAuthToken, OidcProviderMetadata, ProtectedResourceMetadata
from ai_agent.oauth.pkce import create_nonce, create_pkce_pair, create_state


@dataclass(frozen=True, slots=True)
class AuthorizationTransaction:
    authorization_url: str
    state: str
    nonce: str
    code_verifier: SecretStr


class DiscoveryClient:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def discover_oidc(self, issuer: str) -> OidcProviderMetadata:
        metadata_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        payload = await self._get_json(metadata_url)
        try:
            metadata = OidcProviderMetadata.model_validate(payload)
        except ValidationError as exc:
            raise ProtocolValidationError("OIDC discovery metadata is invalid.") from exc
        if metadata.issuer.rstrip("/") != issuer.rstrip("/"):
            raise ProtocolValidationError("OIDC discovery issuer does not match configuration.")
        _require_absolute_urls(
            metadata.authorization_endpoint,
            metadata.token_endpoint,
            *([metadata.jwks_uri] if metadata.jwks_uri else []),
        )
        return metadata

    async def discover_authorization_server(self, issuer: str) -> OidcProviderMetadata:
        candidates = (
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
        )
        last_error: Exception | None = None
        for metadata_url in candidates:
            try:
                payload = await self._get_json(metadata_url)
                metadata = OidcProviderMetadata.model_validate(payload)
                if metadata.issuer.rstrip("/") != issuer.rstrip("/"):
                    raise ProtocolValidationError(
                        "Authorization server issuer does not match protected resource metadata."
                    )
                _require_absolute_urls(
                    metadata.authorization_endpoint,
                    metadata.token_endpoint,
                    *([metadata.jwks_uri] if metadata.jwks_uri else []),
                )
                return metadata
            except (httpx.HTTPError, ValidationError, ProtocolValidationError) as exc:
                last_error = exc
        raise ProtocolValidationError(
            "Authorization server discovery failed for every supported metadata endpoint."
        ) from last_error

    async def discover_protected_resource(
        self,
        mcp_url: str,
        *,
        required_scope: str,
    ) -> ProtectedResourceMetadata:
        parsed = urlsplit(mcp_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        resource_path = parsed.path.rstrip("/")
        candidates = (
            f"{origin}/.well-known/oauth-protected-resource{resource_path}",
            f"{origin}/.well-known/oauth-protected-resource",
        )
        last_error: Exception | None = None
        for metadata_url in dict.fromkeys(candidates):
            try:
                payload = await self._get_json(metadata_url)
                metadata = ProtectedResourceMetadata.model_validate(payload)
                if metadata.resource.rstrip("/") != mcp_url.rstrip("/"):
                    raise ProtocolValidationError(
                        "Protected resource metadata returned an unexpected resource."
                    )
                if required_scope not in metadata.scopes_supported:
                    raise ProtocolValidationError(
                        "Protected resource metadata does not advertise the required scope."
                    )
                if not metadata.authorization_servers:
                    raise ProtocolValidationError(
                        "Protected resource metadata has no authorization server."
                    )
                _require_absolute_urls(metadata.resource, *metadata.authorization_servers)
                return metadata
            except (httpx.HTTPError, ValidationError, ProtocolValidationError) as exc:
                last_error = exc
        raise ProtocolValidationError(
            "MCP protected resource metadata discovery failed."
        ) from last_error

    async def _get_json(self, url: str) -> object:
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            return response.json()


class AuthorizationCodeClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        scope: str,
        token_endpoint_auth_method: TokenEndpointAuthMethod,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scope = scope
        self._token_endpoint_auth_method = token_endpoint_auth_method
        self._timeout_seconds = timeout_seconds

    def create_authorization_transaction(
        self,
        authorization_endpoint: str,
    ) -> AuthorizationTransaction:
        pkce = create_pkce_pair()
        state = create_state()
        nonce = create_nonce()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scope,
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce.code_challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in authorization_endpoint else "?"
        return AuthorizationTransaction(
            authorization_url=f"{authorization_endpoint}{separator}{query}",
            state=state,
            nonce=nonce,
            code_verifier=SecretStr(pkce.code_verifier),
        )

    async def exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        transaction: AuthorizationTransaction,
        returned_state: str,
    ) -> OAuthToken:
        if not secrets_compare(transaction.state, returned_state):
            raise AuthenticationError("OAuth state validation failed.")
        return await self._fetch_token(
            token_endpoint,
            grant_type="authorization_code",
            code=code,
            redirect_uri=self._redirect_uri,
            code_verifier=transaction.code_verifier.get_secret_value(),
        )

    async def refresh_token(
        self,
        *,
        token_endpoint: str,
        refresh_token: SecretStr,
    ) -> OAuthToken:
        return await self._fetch_token(
            token_endpoint,
            grant_type="refresh_token",
            refresh_token=refresh_token.get_secret_value(),
        )

    async def _fetch_token(self, token_endpoint: str, **kwargs: str) -> OAuthToken:
        data = dict(kwargs)
        data["client_id"] = self._client_id
        auth: httpx.BasicAuth | None = None
        secret = self._client_secret.get_secret_value()
        if self._token_endpoint_auth_method == TokenEndpointAuthMethod.CLIENT_SECRET_BASIC:
            auth = httpx.BasicAuth(self._client_id, secret)
        elif self._token_endpoint_auth_method == TokenEndpointAuthMethod.CLIENT_SECRET_POST:
            data["client_secret"] = secret
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                if auth is None:
                    response = await client.post(
                        token_endpoint,
                        data=data,
                        headers={"Accept": "application/json"},
                    )
                else:
                    response = await client.post(
                        token_endpoint,
                        data=data,
                        auth=auth,
                        headers={"Accept": "application/json"},
                    )
                response.raise_for_status()
                return OAuthToken.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise AuthenticationError("OAuth token request failed.") from exc


def secrets_compare(expected: str, actual: str) -> bool:
    import secrets

    return secrets.compare_digest(expected.encode("utf-8"), actual.encode("utf-8"))


def _require_absolute_urls(*values: str) -> None:
    for value in values:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProtocolValidationError("Protocol metadata contains a non-HTTP absolute URL.")
        if parsed.username or parsed.password:
            raise ProtocolValidationError("Protocol metadata URL contains embedded credentials.")
