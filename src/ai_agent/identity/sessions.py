"""Opaque server-side login sessions and short-lived OAuth transactions."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable
from dataclasses import asdict, dataclass
from typing import Protocol, cast
from uuid import UUID

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class AuthSession:
    user_id: UUID
    csrf_token: str


@dataclass(frozen=True, slots=True)
class StoredOAuthTransaction:
    state: str
    nonce: str
    code_verifier: str


class SessionStore(Protocol):
    async def save_oauth_transaction(
        self, transaction: StoredOAuthTransaction, ttl_seconds: int
    ) -> None: ...

    async def pop_oauth_transaction(self, state: str) -> StoredOAuthTransaction | None: ...

    async def create_session(self, user_id: UUID, ttl_seconds: int) -> tuple[str, AuthSession]: ...

    async def get_session(self, session_id: str) -> AuthSession | None: ...

    async def delete_session(self, session_id: str) -> None: ...

    async def ping(self) -> None: ...


class RedisSessionStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save_oauth_transaction(
        self, transaction: StoredOAuthTransaction, ttl_seconds: int
    ) -> None:
        await self._redis.set(
            f"oauth:{transaction.state}",
            json.dumps(asdict(transaction), separators=(",", ":")),
            ex=ttl_seconds,
        )

    async def pop_oauth_transaction(self, state: str) -> StoredOAuthTransaction | None:
        value = await self._redis.getdel(f"oauth:{state}")
        if value is None:
            return None
        payload = json.loads(_decode(value))
        return StoredOAuthTransaction(**payload)

    async def create_session(self, user_id: UUID, ttl_seconds: int) -> tuple[str, AuthSession]:
        session_id = secrets.token_urlsafe(32)
        session = AuthSession(user_id=user_id, csrf_token=secrets.token_urlsafe(32))
        await self._redis.set(
            f"session:{session_id}",
            json.dumps({"user_id": str(user_id), "csrf_token": session.csrf_token}),
            ex=ttl_seconds,
        )
        return session_id, session

    async def get_session(self, session_id: str) -> AuthSession | None:
        value = await self._redis.get(f"session:{session_id}")
        if value is None:
            return None
        payload = json.loads(_decode(value))
        return AuthSession(user_id=UUID(payload["user_id"]), csrf_token=payload["csrf_token"])

    async def delete_session(self, session_id: str) -> None:
        await self._redis.delete(f"session:{session_id}")

    async def ping(self) -> None:
        await cast(Awaitable[bool], self._redis.ping())


class MemorySessionStore:
    """Test-only store with the same consume-once semantics as Redis GETDEL."""

    def __init__(self) -> None:
        self.transactions: dict[str, StoredOAuthTransaction] = {}
        self.sessions: dict[str, AuthSession] = {}

    async def save_oauth_transaction(
        self, transaction: StoredOAuthTransaction, ttl_seconds: int
    ) -> None:
        del ttl_seconds
        self.transactions[transaction.state] = transaction

    async def pop_oauth_transaction(self, state: str) -> StoredOAuthTransaction | None:
        return self.transactions.pop(state, None)

    async def create_session(self, user_id: UUID, ttl_seconds: int) -> tuple[str, AuthSession]:
        del ttl_seconds
        session_id = secrets.token_urlsafe(32)
        session = AuthSession(user_id=user_id, csrf_token=secrets.token_urlsafe(32))
        self.sessions[session_id] = session
        return session_id, session

    async def get_session(self, session_id: str) -> AuthSession | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    async def ping(self) -> None:
        return None


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
