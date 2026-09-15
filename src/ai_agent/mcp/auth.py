"""Access-token providers used by outbound MCP connections."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from weakref import WeakValueDictionary

import httpx
from pydantic import SecretStr, ValidationError

from ai_agent.config import TokenEndpointAuthMethod
from ai_agent.credentials.vault import CredentialVault
from ai_agent.errors import AuthenticationError, ConfigurationError
from ai_agent.mcp.network_policy import McpNetworkPolicy
from ai_agent.oauth.models import OAuthToken

_REFRESH_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class AccessTokenProvider(Protocol):
    async def get_access_token(self) -> SecretStr:
        """Return a usable bearer token without exposing it to logs."""


class VaultAccessTokenProvider:
    """Read a connection token from the vault without exposing its value."""

    def __init__(
        self,
        vault: CredentialVault,
        reference: str,
        *,
        timeout_seconds: float = 10.0,
        network_policy: McpNetworkPolicy | None = None,
    ) -> None:
        self._vault = vault
        self._reference = reference
        self._timeout_seconds = timeout_seconds
        self._network_policy = network_policy

    async def get_access_token(self) -> SecretStr:
        credential = await self._vault.get(self._reference)
        if credential is None:
            raise AuthenticationError("External connection credential is missing or expired.")
        if not credential.is_expired():
            return credential.access_token
        if not credential.can_refresh_client_credentials:
            raise AuthenticationError("External connection credential is missing or expired.")
        lock = _REFRESH_LOCKS.setdefault(self._reference, asyncio.Lock())
        async with lock:
            credential = await self._vault.get(self._reference)
            if credential is None:
                raise AuthenticationError("External connection credential is missing or expired.")
            if not credential.is_expired():
                return credential.access_token
            if not credential.can_refresh_client_credentials:
                raise AuthenticationError("External connection credential is missing or expired.")
            assert credential.client_id is not None
            assert credential.client_secret is not None
            assert credential.token_url is not None
            assert credential.token_endpoint_auth_method is not None
            provider = ClientCredentialsTokenProvider(
                token_url=credential.token_url,
                client_id=credential.client_id,
                client_secret=credential.client_secret,
                scope=credential.scope,
                auth_method=TokenEndpointAuthMethod(credential.token_endpoint_auth_method),
                timeout_seconds=self._timeout_seconds,
                network_policy=self._network_policy,
            )
            token = await provider.get_token()
            refreshed = credential.model_copy(
                update={
                    "access_token": token.access_token,
                    "token_type": token.token_type,
                    "expires_at": datetime.now(UTC) + timedelta(seconds=token.expires_in),
                },
                deep=True,
            )
            await self._vault.replace(self._reference, refreshed)
            return token.access_token


class StaticAccessTokenProvider:
    def __init__(self, access_token: SecretStr) -> None:
        if not access_token.get_secret_value():
            raise ConfigurationError("The configured access token is empty.")
        self._access_token = access_token

    async def get_access_token(self) -> SecretStr:
        return self._access_token


class ClientCredentialsTokenProvider:
    """Acquire and cache a short-lived OAuth client-credentials token."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str,
        auth_method: TokenEndpointAuthMethod,
        timeout_seconds: float,
        network_policy: McpNetworkPolicy | None = None,
    ) -> None:
        if not client_id or not client_secret.get_secret_value():
            raise ConfigurationError("Service client ID and secret are required.")
        if auth_method == TokenEndpointAuthMethod.NONE:
            raise ConfigurationError(
                "Client credentials require a confidential client auth method."
            )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._auth_method = auth_method
        self._timeout_seconds = timeout_seconds
        self._network_policy = network_policy
        self._cached_token: OAuthToken | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self) -> SecretStr:
        return (await self.get_token()).access_token

    async def get_token(self) -> OAuthToken:
        if self._is_cached_token_valid():
            assert self._cached_token is not None
            return self._cached_token.model_copy(deep=True)

        async with self._lock:
            if self._is_cached_token_valid():
                assert self._cached_token is not None
                return self._cached_token.model_copy(deep=True)
            token = await self._request_token()
            self._cached_token = token
            refresh_skew = min(30.0, token.expires_in * 0.1)
            self._expires_at = time.monotonic() + token.expires_in - refresh_skew
            return token.model_copy(deep=True)

    def _is_cached_token_valid(self) -> bool:
        return self._cached_token is not None and time.monotonic() < self._expires_at

    async def _request_token(self) -> OAuthToken:
        data = {"grant_type": "client_credentials", "scope": self._scope}
        auth: httpx.BasicAuth | None = None
        secret = self._client_secret.get_secret_value()
        if self._auth_method == TokenEndpointAuthMethod.CLIENT_SECRET_BASIC:
            auth = httpx.BasicAuth(self._client_id, secret)
        else:
            data.update({"client_id": self._client_id, "client_secret": secret})

        try:
            if self._network_policy is not None:
                await self._network_policy.validate_url_resolution(self._token_url)
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                if auth is None:
                    response = await client.post(
                        self._token_url,
                        data=data,
                        headers={"Accept": "application/json"},
                    )
                else:
                    response = await client.post(
                        self._token_url,
                        data=data,
                        auth=auth,
                        headers={"Accept": "application/json"},
                    )
                response.raise_for_status()
                return OAuthToken.model_validate(response.json())
        except (httpx.HTTPError, ValidationError) as exc:
            raise AuthenticationError("OAuth client-credentials token request failed.") from exc
