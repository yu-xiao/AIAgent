"""Opaque server-side login sessions and short-lived OAuth transactions."""

from __future__ import annotations

import json
import secrets
import time
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

    async def delete_user_sessions(self, user_id: UUID) -> int: ...

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
        index_key = f"user-sessions:{user_id}"
        now = time.time()
        await cast(Awaitable[int], self._redis.zremrangebyscore(index_key, "-inf", now))
        await cast(
            Awaitable[int],
            self._redis.zadd(index_key, {session_id: now + ttl_seconds}),
        )
        await cast(Awaitable[bool], self._redis.expire(index_key, ttl_seconds))
        return session_id, session

    async def get_session(self, session_id: str) -> AuthSession | None:
        value = await self._redis.get(f"session:{session_id}")
        if value is None:
            return None
        payload = json.loads(_decode(value))
        return AuthSession(user_id=UUID(payload["user_id"]), csrf_token=payload["csrf_token"])

    async def delete_session(self, session_id: str) -> None:
        key = f"session:{session_id}"
        value = await self._redis.get(key)
        await cast(Awaitable[int], self._redis.delete(key))
        if value is not None:
            payload = json.loads(_decode(value))
            await cast(
                Awaitable[int],
                self._redis.zrem(f"user-sessions:{payload['user_id']}", session_id),
            )

    async def delete_user_sessions(self, user_id: UUID) -> int:
        index_key = f"user-sessions:{user_id}"
        session_ids = await cast(
            Awaitable[list[bytes | str]], self._redis.zrange(index_key, 0, -1)
        )
        if not session_ids:
            return 0
        decoded_ids = [_decode(item) for item in session_ids]
        deleted = await cast(
            Awaitable[int],
            self._redis.delete(*(f"session:{session_id}" for session_id in decoded_ids)),
        )
        await cast(Awaitable[int], self._redis.delete(index_key))
        return int(deleted)

    async def ping(self) -> None:
        await cast(Awaitable[bool], self._redis.ping())


class MemorySessionStore:
    """Test-only store with the same consume-once semantics as Redis GETDEL."""

    def __init__(self) -> None:
        self.transactions: dict[str, StoredOAuthTransaction] = {}
        self.sessions: dict[str, AuthSession] = {}
        self.user_sessions: dict[UUID, set[str]] = {}

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
        self.user_sessions.setdefault(user_id, set()).add(session_id)
        return session_id, session

    async def get_session(self, session_id: str) -> AuthSession | None:
        return self.sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            sessions = self.user_sessions.get(session.user_id)
            if sessions is not None:
                sessions.discard(session_id)
                if not sessions:
                    self.user_sessions.pop(session.user_id, None)

    async def delete_user_sessions(self, user_id: UUID) -> int:
        session_ids = self.user_sessions.pop(user_id, set())
        for session_id in session_ids:
            self.sessions.pop(session_id, None)
        return len(session_ids)

    async def ping(self) -> None:
        return None


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
