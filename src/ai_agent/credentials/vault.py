"""Secret references used by outbound integrations.

The memory implementation is intentionally suitable only for development and tests. A
production deployment should provide the same protocol using a KMS/Vault-backed store.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from ai_agent.config import CredentialSettings
from ai_agent.errors import AuthenticationError, ConfigurationError
from ai_agent.observability.metrics import VAULT_OPERATIONS


class Credential(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    access_token: SecretStr
    refresh_token: SecretStr | None = None
    client_secret: SecretStr | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None

    def is_expired(self, skew_seconds: float = 30.0) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC) + timedelta(
            seconds=skew_seconds
        )


class CredentialVault(Protocol):
    kind: str

    async def put(self, credential: Credential, *, ttl_seconds: int | None = None) -> str: ...

    async def get(self, reference: str) -> Credential | None: ...

    async def revoke(self, reference: str) -> None: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class MemoryCredentialVault:
    """Process-local vault that never serializes secrets to the database or logs."""

    kind = "memory"

    def __init__(self) -> None:
        self._values: dict[str, Credential] = {}
        self._lock = asyncio.Lock()

    async def put(self, credential: Credential, *, ttl_seconds: int | None = None) -> str:
        del ttl_seconds
        if not credential.access_token.get_secret_value():
            raise ConfigurationError("Credential access token must not be empty.")
        reference = f"mem_{secrets.token_urlsafe(24)}"
        async with self._lock:
            self._values[reference] = credential
        return reference

    async def get(self, reference: str) -> Credential | None:
        if not reference:
            raise AuthenticationError("Credential reference is missing.")
        async with self._lock:
            credential = self._values.get(reference)
            return credential.model_copy(deep=True) if credential else None

    async def revoke(self, reference: str) -> None:
        async with self._lock:
            self._values.pop(reference, None)

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None


class HashicorpVaultCredentialVault:
    """HashiCorp Vault KV v2 store using opaque application references."""

    kind = "hashicorp_vault"

    def __init__(self, settings: CredentialSettings) -> None:
        self._address = settings.address.rstrip("/")
        self._mount_path = settings.mount_path.strip("/")
        self._path_prefix = settings.path_prefix.strip("/")
        self._token_file = Path(settings.token_file)
        self._namespace = settings.namespace.strip()
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def put(self, credential: Credential, *, ttl_seconds: int | None = None) -> str:
        if not credential.access_token.get_secret_value():
            raise ConfigurationError("Credential access token must not be empty.")
        reference = f"vault_{secrets.token_urlsafe(24)}"
        try:
            await self._write(reference, credential, ttl_seconds=ttl_seconds)
        except Exception:
            VAULT_OPERATIONS.labels(operation="write", status="failed").inc()
            raise
        VAULT_OPERATIONS.labels(operation="write", status="succeeded").inc()
        return reference

    async def get(self, reference: str) -> Credential | None:
        self._validate_reference(reference)
        response = await self._client.get(self._data_url(reference), headers=self._headers())
        if response.status_code == 404:
            VAULT_OPERATIONS.labels(operation="read", status="missing").inc()
            return None
        try:
            self._raise_for_status(response, "read")
        except Exception:
            VAULT_OPERATIONS.labels(operation="read", status="failed").inc()
            raise
        try:
            payload = response.json()["data"]["data"]
            credential = Credential.model_validate(payload["credential"])
        except (KeyError, TypeError, ValueError) as exc:
            VAULT_OPERATIONS.labels(operation="read", status="failed").inc()
            raise AuthenticationError("Vault returned an invalid credential payload.") from exc
        if credential.is_expired(skew_seconds=0):
            VAULT_OPERATIONS.labels(operation="read", status="expired").inc()
            return None
        VAULT_OPERATIONS.labels(operation="read", status="succeeded").inc()
        return credential

    async def revoke(self, reference: str) -> None:
        self._validate_reference(reference)
        try:
            response = await self._client.delete(
                self._metadata_url(reference), headers=self._headers()
            )
            if response.status_code not in {200, 204, 404}:
                self._raise_for_status(response, "revoke")
        except Exception:
            VAULT_OPERATIONS.labels(operation="revoke", status="failed").inc()
            raise
        VAULT_OPERATIONS.labels(operation="revoke", status="succeeded").inc()

    async def ping(self) -> None:
        response = await self._client.get(f"{self._address}/v1/sys/health", headers=self._headers())
        if response.status_code not in {200, 429, 472, 473}:
            raise AuthenticationError("HashiCorp Vault is unavailable or sealed.")

    async def close(self) -> None:
        await self._client.aclose()

    async def _write(
        self,
        reference: str,
        credential: Credential,
        *,
        ttl_seconds: int | None,
    ) -> None:
        payload = credential.model_dump(mode="json")
        payload["access_token"] = credential.access_token.get_secret_value()
        if credential.refresh_token is not None:
            payload["refresh_token"] = credential.refresh_token.get_secret_value()
        if credential.client_secret is not None:
            payload["client_secret"] = credential.client_secret.get_secret_value()
        response = await self._client.post(
            self._data_url(reference),
            headers=self._headers(),
            json={"data": {"credential": payload, "ttl_seconds": ttl_seconds}},
        )
        self._raise_for_status(response, "write")

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthenticationError("HashiCorp Vault token file cannot be read.") from exc
        if not token:
            raise AuthenticationError("HashiCorp Vault token file is empty.")
        headers = {"X-Vault-Token": token}
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        return headers

    def _data_url(self, reference: str) -> str:
        return f"{self._address}/v1/{self._mount_path}/data/{self._path_prefix}/{reference}"

    def _metadata_url(self, reference: str) -> str:
        return f"{self._address}/v1/{self._mount_path}/metadata/{self._path_prefix}/{reference}"

    @staticmethod
    def _validate_reference(reference: str) -> None:
        suffix = reference[6:].replace("-", "").replace("_", "")
        if not reference.startswith("vault_") or not suffix.isalnum():
            raise AuthenticationError("Credential reference is invalid.")

    @staticmethod
    def _raise_for_status(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        if response.status_code in {401, 403}:
            raise AuthenticationError(f"HashiCorp Vault denied credential {operation}.")
        raise AuthenticationError(f"HashiCorp Vault credential {operation} failed.")
