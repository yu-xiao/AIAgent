"""Secret references used by outbound integrations.

The memory implementation is intentionally suitable only for development and tests. A
production deployment should provide the same protocol using a KMS/Vault-backed store.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

from ai_agent.errors import AuthenticationError, ConfigurationError


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
    async def put(self, credential: Credential, *, ttl_seconds: int | None = None) -> str: ...

    async def get(self, reference: str) -> Credential | None: ...

    async def revoke(self, reference: str) -> None: ...


class MemoryCredentialVault:
    """Process-local vault that never serializes secrets to the database or logs."""

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
