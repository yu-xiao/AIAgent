"""Local password authentication for development and internal testing."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ai_agent.audit.service import AuditService
from ai_agent.errors import AuthenticationError, ConflictError
from ai_agent.identity.service import IdentityService
from ai_agent.persistence.database import Database
from ai_agent.persistence.models import LocalCredential, User


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=5, dklen=32
    )
    return f"scrypt-v1${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        version, salt, digest = stored.split("$")
        if version != "scrypt-v1" or len(salt) != 32 or len(digest) != 64:
            return False
        bytes.fromhex(salt)
        bytes.fromhex(digest)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


class LocalAuthService:
    def __init__(self, database: Database, identities: IdentityService, audit: AuditService):
        self._database = database
        self._identities = identities
        self._audit = audit
        self._hash_slots = asyncio.Semaphore(4)
        self._dummy_hash = hash_password(secrets.token_urlsafe(32))

    async def register(self, email: str, display_name: str, password: str) -> User:
        async with self._hash_slots:
            hashed = await asyncio.to_thread(hash_password, password)
        try:
            async with self._database.session_factory() as session, session.begin():
                user = User(
                    issuer="urn:ai-agent:local",
                    subject=str(uuid4()),
                    email=email,
                    display_name=display_name,
                )
                session.add(user)
                await session.flush()
                session.add(LocalCredential(email=email, user_id=user.id, password_hash=hashed))
                await session.flush()
                await self._identities.create_organization_in_session(
                    session, user.id, f"{display_name}的工作区"[:200]
                )
                session.add(
                    self._audit.record(
                        organization_id=None,
                        actor_user_id=user.id,
                        action="auth.register",
                        resource_type="user",
                        resource_id=str(user.id),
                        details={"source": "local"},
                    )
                )
                return user
        except IntegrityError as exc:
            raise ConflictError("该邮箱无法注册, 请登录或使用其他邮箱.") from exc

    async def login(self, email: str, password: str) -> User:
        async with self._database.session_factory() as session, session.begin():
            credential = await session.get(LocalCredential, email)
            hashed = credential.password_hash if credential else self._dummy_hash
            async with self._hash_slots:
                valid = await asyncio.to_thread(verify_password, password, hashed)
            user: User | None = (
                await session.scalar(select(User).where(User.id == credential.user_id))
                if credential
                else None
            )
            allowed = valid and user is not None and user.is_active
            session.add(
                self._audit.record(
                    organization_id=None,
                    actor_user_id=user.id if user else None,
                    action="auth.login" if allowed else "auth.login_failed",
                    resource_type="session",
                    resource_id=None,
                    details={"source": "local"},
                )
            )
        if not allowed or user is None:
            raise AuthenticationError("邮箱或密码错误.")
        return user
