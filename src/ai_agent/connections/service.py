"""OAuth and lifecycle services for personal and organization connections."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.audit.service import AuditService
from ai_agent.config import PlatformSettings, TokenEndpointAuthMethod
from ai_agent.credentials.vault import Credential, CredentialVault
from ai_agent.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ProtocolValidationError,
    ResourceNotFoundError,
)
from ai_agent.identity.service import (
    CONNECTION_ORGANIZATION_MANAGE,
    CONNECTION_PERSONAL_CREATE,
    CONNECTION_PERSONAL_DISCONNECT,
    IdentityService,
)
from ai_agent.identity.sessions import SessionStore, StoredOAuthTransaction
from ai_agent.oauth.client import AuthorizationCodeClient, AuthorizationTransaction, DiscoveryClient
from ai_agent.oauth.models import OAuthToken, OidcProviderMetadata
from ai_agent.oauth.validator import OidcIdTokenValidator
from ai_agent.persistence.models import (
    ConnectionOwnership,
    ConnectionStatus,
    ConnectionToolGrant,
    CredentialReference,
    ExternalAuthorizationGrant,
    ExternalConnection,
    McpAuthMode,
    McpServerDefinition,
    utc_now,
)

logger = logging.getLogger(__name__)


class ConnectionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identities: IdentityService,
        sessions: SessionStore,
        vault: CredentialVault,
        platform: PlatformSettings,
        *,
        default_client_id: str = "ai-agent-web",
        default_timeout_seconds: float = 10.0,
        signing_algorithms: tuple[str, ...] = ("RS256",),
        network_policy: Any | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._identities = identities
        self._sessions = sessions
        self._vault = vault
        self._platform = platform
        self._default_client_id = default_client_id
        self._default_timeout_seconds = default_timeout_seconds
        self._discovery = DiscoveryClient(timeout_seconds=default_timeout_seconds)
        self._id_token_validator = OidcIdTokenValidator(
            signing_algorithms=signing_algorithms,
            timeout_seconds=default_timeout_seconds,
        )
        self._network_policy = network_policy
        self._audit = audit or AuditService()

    async def begin_personal_authorization(
        self,
        user_id: UUID,
        organization_id: UUID,
        server_code: str,
        *,
        trace_id: UUID | None = None,
    ) -> str:
        await self._identities.access(user_id, organization_id, CONNECTION_PERSONAL_CREATE)
        server = await self._server(server_code, organization_id, enabled=True)
        if server.auth_mode != McpAuthMode.OAUTH_AUTHORIZATION_CODE:
            raise ConflictError("MCP Server does not support personal OAuth connections.")
        metadata = await self._authorization_metadata(server)
        if (
            metadata.code_challenge_methods_supported
            and "S256" not in metadata.code_challenge_methods_supported
        ):
            raise ProtocolValidationError("Authorization server does not support PKCE S256.")
        transaction = self._oauth_client(
            server, client_secret=await self._oauth_client_secret(server)
        ).create_authorization_transaction(metadata.authorization_endpoint)
        await self._sessions.save_oauth_transaction(
            StoredOAuthTransaction(
                state=transaction.state,
                nonce=transaction.nonce,
                code_verifier=transaction.code_verifier.get_secret_value(),
            ),
            self._platform.oauth_transaction_ttl_seconds,
        )
        expires_at = utc_now() + timedelta(seconds=self._platform.oauth_transaction_ttl_seconds)
        async with self._session_factory() as session, session.begin():
            session.add(
                ExternalAuthorizationGrant(
                    organization_id=organization_id,
                    user_id=user_id,
                    server_id=server.id,
                    state_hash=_digest(transaction.state),
                    nonce_hash=_digest(transaction.nonce),
                    expires_at=expires_at,
                )
            )
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="connection.authorization_started",
                    resource_type="mcp_server",
                    resource_id=str(server.id),
                    trace_id=trace_id,
                    details={"server_code": server.code},
                )
            )
        return transaction.authorization_url

    async def finish_personal_authorization(
        self,
        user_id: UUID,
        server_code: str,
        *,
        code: str,
        state: str,
        trace_id: UUID | None = None,
    ) -> ExternalConnection:
        stored = await self._sessions.pop_oauth_transaction(state)
        if stored is None:
            raise AuthenticationError("OAuth transaction is missing, expired, or already used.")
        async with self._session_factory() as session, session.begin():
            grant = await session.scalar(
                select(ExternalAuthorizationGrant)
                .where(
                    ExternalAuthorizationGrant.state_hash == _digest(state),
                    ExternalAuthorizationGrant.used_at.is_(None),
                )
                .with_for_update()
            )
            if grant is None or _as_utc(grant.expires_at) <= utc_now() or grant.user_id != user_id:
                raise AuthenticationError(
                    "OAuth transaction is invalid or belongs to another user."
                )
            if not _constant_digest_match(stored.nonce, grant.nonce_hash):
                raise AuthenticationError("OAuth transaction nonce validation failed.")
            server = await session.scalar(
                select(McpServerDefinition).where(
                    McpServerDefinition.id == grant.server_id,
                    McpServerDefinition.code == server_code,
                    McpServerDefinition.organization_id == grant.organization_id,
                    McpServerDefinition.enabled.is_(True),
                )
            )
            if server is None:
                raise ResourceNotFoundError("MCP Server is not registered or enabled.")
            grant.used_at = utc_now()
            organization_id = grant.organization_id
        metadata = await self._authorization_metadata(server)
        transaction = AuthorizationTransaction(
            authorization_url="",
            state=stored.state,
            nonce=stored.nonce,
            code_verifier=SecretStr(stored.code_verifier),
        )
        token = await self._oauth_client(
            server, client_secret=await self._oauth_client_secret(server)
        ).exchange_code(
            token_endpoint=metadata.token_endpoint,
            code=code,
            transaction=transaction,
            returned_state=state,
        )
        subject = await _subject_from_token(
            token,
            metadata,
            stored.nonce,
            self._oauth_client_id(server),
            validator=self._id_token_validator,
        )
        credential = _credential_from_token(token)
        access_reference = await self._vault.put(credential)
        try:
            connection, reference_to_revoke = await self._persist_personal_connection(
                user_id=user_id,
                organization_id=organization_id,
                server=server,
                metadata=metadata,
                subject=subject,
                token=token,
                credential=credential,
                access_reference=access_reference,
                trace_id=trace_id,
            )
        except Exception:
            await self._revoke_uncommitted_reference(access_reference)
            raise
        if reference_to_revoke is not None:
            await self._revoke_reference(reference_to_revoke)
        return connection

    async def _persist_personal_connection(
        self,
        *,
        user_id: UUID,
        organization_id: UUID,
        server: McpServerDefinition,
        metadata: OidcProviderMetadata,
        subject: str,
        token: OAuthToken,
        credential: Credential,
        access_reference: str,
        trace_id: UUID | None,
    ) -> tuple[ExternalConnection, str | None]:
        reference_to_revoke: str | None = None
        async with self._session_factory() as session, session.begin():
            previous = await session.scalar(
                select(ExternalConnection)
                .where(
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.server_id == server.id,
                    ExternalConnection.owner_user_id == user_id,
                    ExternalConnection.ownership == ConnectionOwnership.PERSONAL,
                )
                .with_for_update()
            )
            if previous is None:
                session.add(
                    CredentialReference(
                        organization_id=organization_id,
                        reference=access_reference,
                        vault_kind=self._vault.kind,
                        expires_at=credential.expires_at,
                    )
                )
                connection = ExternalConnection(
                    organization_id=organization_id,
                    server_id=server.id,
                    owner_user_id=user_id,
                    ownership=ConnectionOwnership.PERSONAL,
                    status=ConnectionStatus.ACTIVE,
                    external_issuer=metadata.issuer,
                    external_subject=subject,
                    scopes=_scopes(token, server),
                    credential_reference=access_reference,
                    expires_at=credential.expires_at,
                    allowed_tools=list(server.allowed_tools),
                    allowed_tool_sets=list(server.tool_sets),
                )
                session.add(connection)
                await session.flush()
            else:
                connection = previous
                reference_to_revoke = previous.credential_reference
                old_reference = await session.scalar(
                    select(CredentialReference).where(
                        CredentialReference.reference == previous.credential_reference
                    )
                )
                if old_reference:
                    old_reference.status = "revocation_pending"
                session.add(
                    CredentialReference(
                        organization_id=organization_id,
                        reference=access_reference,
                        vault_kind=self._vault.kind,
                        expires_at=credential.expires_at,
                    )
                )
                connection.status = ConnectionStatus.ACTIVE
                connection.external_issuer = metadata.issuer
                connection.external_subject = subject
                connection.scopes = _scopes(token, server)
                connection.credential_reference = access_reference
                connection.expires_at = credential.expires_at
                connection.allowed_tools = list(server.allowed_tools)
                connection.allowed_tool_sets = list(server.tool_sets)
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="connection.authorization_succeeded",
                    resource_type="external_connection",
                    resource_id=str(connection.id),
                    trace_id=trace_id,
                    details={"server_code": server.code, "scope": _scopes(token, server)},
                )
            )
        return connection, reference_to_revoke

    async def create_organization_connection(
        self,
        actor_id: UUID,
        organization_id: UUID,
        server_code: str,
        *,
        access_token: SecretStr | None = None,
        client_id: str | None = None,
        client_secret: SecretStr | None = None,
        token_url: str | None = None,
        scope: str | None = None,
        allowed_tools: list[str] | None = None,
        allowed_tool_sets: list[str] | None = None,
        trace_id: UUID | None = None,
    ) -> ExternalConnection:
        await self._identities.access(actor_id, organization_id, CONNECTION_ORGANIZATION_MANAGE)
        server = await self._server(server_code, organization_id, enabled=True)
        if server.auth_mode not in {McpAuthMode.CLIENT_CREDENTIALS, McpAuthMode.API_KEY}:
            raise ConflictError("MCP Server is not configured for an organization connection.")
        selected_tools = list(dict.fromkeys(allowed_tools or []))
        if not selected_tools:
            raise ProtocolValidationError(
                "Organization connections require an explicit non-empty Tool allowlist."
            )
        unknown_tools = set(selected_tools) - set(server.allowed_tools)
        if unknown_tools:
            raise AuthorizationError(
                "Organization connection Tool allowlist exceeds the MCP Server allowlist."
            )
        selected_tool_sets = list(dict.fromkeys(allowed_tool_sets or []))
        unknown_tool_sets = set(selected_tool_sets) - set(server.tool_sets)
        if unknown_tool_sets:
            raise AuthorizationError(
                "Organization connection Tool Sets are not registered by the MCP Server."
            )
        token: OAuthToken | None = None
        if access_token is None or not access_token.get_secret_value():
            if server.auth_mode != McpAuthMode.CLIENT_CREDENTIALS:
                raise AuthenticationError("Organization connection access token is required.")
            if not client_id or client_secret is None or not client_secret.get_secret_value():
                raise AuthenticationError("Client credentials are required for this connection.")
            if not token_url and not server.token_endpoint:
                raise ProtocolValidationError(
                    "MCP Server has no client-credentials token endpoint."
                )
            from ai_agent.mcp.auth import ClientCredentialsTokenProvider

            provider = ClientCredentialsTokenProvider(
                token_url=token_url or server.token_endpoint or "",
                client_id=client_id,
                client_secret=client_secret,
                scope=scope or server.required_scope,
                auth_method=TokenEndpointAuthMethod(server.token_endpoint_auth_method),
                timeout_seconds=self._default_timeout_seconds,
                network_policy=self._network_policy,
            )
            token = await provider.get_token()
            access_token = token.access_token
            expires_at = datetime.now(UTC) + timedelta(seconds=token.expires_in)
        else:
            expires_at = None
        credential = Credential(
            access_token=access_token,
            token_type=token.token_type if token is not None else "Bearer",
            client_secret=client_secret if token is not None else None,
            expires_at=expires_at,
            client_id=client_id if token is not None else None,
            token_url=(token_url or server.token_endpoint) if token is not None else None,
            scope=(scope or server.required_scope) if token is not None else "",
            token_endpoint_auth_method=(
                server.token_endpoint_auth_method if token is not None else None
            ),
        )
        reference = await self._vault.put(credential)
        persisted_expires_at = None if token is not None else expires_at
        try:
            connection, reference_to_revoke = await self._persist_organization_connection(
                actor_id=actor_id,
                organization_id=organization_id,
                server=server,
                reference=reference,
                expires_at=persisted_expires_at,
                scope=scope,
                selected_tools=selected_tools,
                selected_tool_sets=selected_tool_sets,
                trace_id=trace_id,
            )
        except Exception:
            await self._revoke_uncommitted_reference(reference)
            raise
        if reference_to_revoke is not None:
            await self._revoke_reference(reference_to_revoke)
        return connection

    async def _persist_organization_connection(
        self,
        *,
        actor_id: UUID,
        organization_id: UUID,
        server: McpServerDefinition,
        reference: str,
        expires_at: datetime | None,
        scope: str | None,
        selected_tools: list[str],
        selected_tool_sets: list[str],
        trace_id: UUID | None,
    ) -> tuple[ExternalConnection, str | None]:
        reference_to_revoke: str | None = None
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(ExternalConnection)
                .where(
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.server_id == server.id,
                    ExternalConnection.owner_user_id.is_(None),
                    ExternalConnection.ownership == ConnectionOwnership.ORGANIZATION,
                )
                .with_for_update()
            )
            rotated = existing is not None
            if existing:
                reference_to_revoke = existing.credential_reference
                old_reference = await session.scalar(
                    select(CredentialReference).where(
                        CredentialReference.reference == existing.credential_reference
                    )
                )
                if old_reference:
                    old_reference.status = "revocation_pending"
                session.add(
                    CredentialReference(
                        organization_id=organization_id,
                        reference=reference,
                        vault_kind=self._vault.kind,
                        expires_at=expires_at,
                    )
                )
                existing.status = ConnectionStatus.ACTIVE
                existing.credential_reference = reference
                existing.allowed_tools = selected_tools
                existing.allowed_tool_sets = selected_tool_sets
                connection = existing
            else:
                session.add(
                    CredentialReference(
                        organization_id=organization_id,
                        reference=reference,
                        vault_kind=self._vault.kind,
                        expires_at=expires_at,
                    )
                )
                connection = ExternalConnection(
                    organization_id=organization_id,
                    server_id=server.id,
                    owner_user_id=None,
                    ownership=ConnectionOwnership.ORGANIZATION,
                    status=ConnectionStatus.ACTIVE,
                    scopes=[scope or server.required_scope]
                    if scope or server.required_scope
                    else [],
                    credential_reference=reference,
                    allowed_tools=selected_tools,
                    allowed_tool_sets=selected_tool_sets,
                )
                session.add(connection)
                await session.flush()
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action=(
                        "connection.credential_rotated"
                        if rotated
                        else "connection.organization_created"
                    ),
                    resource_type="external_connection",
                    resource_id=str(connection.id),
                    trace_id=trace_id,
                    details={"server_code": server.code, "allowed_tools": connection.allowed_tools},
                )
            )
        return connection, reference_to_revoke

    async def _revoke_uncommitted_reference(self, reference: str) -> None:
        try:
            await self._vault.revoke(reference)
        except Exception:
            logger.exception("Failed to revoke an uncommitted Vault credential reference.")

    async def _revoke_reference(self, reference: str) -> bool:
        try:
            await self._vault.revoke(reference)
        except Exception:
            logger.exception("Credential revocation remains pending.")
            return False
        await self._mark_reference_revoked(reference)
        return True

    async def retry_pending_revocations(self, *, limit: int = 100) -> int:
        async with self._session_factory() as session:
            references = list(
                await session.scalars(
                    select(CredentialReference.reference)
                    .where(CredentialReference.status == "revocation_pending")
                    .order_by(CredentialReference.updated_at, CredentialReference.id)
                    .limit(limit)
                )
            )
        completed = 0
        for reference in references:
            if await self._revoke_reference(reference):
                completed += 1
        return completed

    async def list_connections(
        self, user_id: UUID, organization_id: UUID
    ) -> list[ExternalConnection]:
        await self._identities.access(user_id, organization_id)
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(ExternalConnection)
                    .where(
                        ExternalConnection.organization_id == organization_id,
                        (ExternalConnection.owner_user_id == user_id)
                        | (ExternalConnection.ownership == ConnectionOwnership.ORGANIZATION),
                    )
                    .order_by(ExternalConnection.created_at.desc(), ExternalConnection.id)
                )
            )

    async def disconnect(
        self,
        user_id: UUID,
        organization_id: UUID,
        connection_id: UUID,
        *,
        trace_id: UUID | None = None,
    ) -> None:
        await self._identities.access(user_id, organization_id, CONNECTION_PERSONAL_DISCONNECT)
        reference_to_revoke: str | None = None
        async with self._session_factory() as session, session.begin():
            connection = await session.scalar(
                select(ExternalConnection)
                .where(
                    ExternalConnection.id == connection_id,
                    ExternalConnection.organization_id == organization_id,
                )
                .with_for_update()
            )
            if connection is None:
                raise ResourceNotFoundError("External connection not found.")
            if connection.ownership == ConnectionOwnership.PERSONAL:
                if connection.owner_user_id != user_id:
                    raise AuthorizationError("This personal connection belongs to another user.")
            else:
                await self._identities.access(
                    user_id, organization_id, CONNECTION_ORGANIZATION_MANAGE
                )
            connection.status = ConnectionStatus.DISCONNECTED
            reference_to_revoke = connection.credential_reference
            credential_reference = await session.scalar(
                select(CredentialReference).where(
                    CredentialReference.reference == connection.credential_reference
                )
            )
            if credential_reference:
                credential_reference.status = "revocation_pending"
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="connection.disconnected",
                    resource_type="external_connection",
                    resource_id=str(connection.id),
                    trace_id=trace_id,
                    details={},
                )
            )
        if reference_to_revoke is not None:
            await self._vault.revoke(reference_to_revoke)
            await self._mark_reference_revoked(reference_to_revoke)

    async def refresh_connection(
        self,
        user_id: UUID,
        organization_id: UUID,
        connection_id: UUID,
        *,
        trace_id: UUID | None = None,
    ) -> ExternalConnection:
        """Refresh a personal OAuth token and rotate its vault reference."""

        await self._identities.access(user_id, organization_id, CONNECTION_PERSONAL_CREATE)
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(ExternalConnection)
                .where(
                    ExternalConnection.id == connection_id,
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.owner_user_id == user_id,
                    ExternalConnection.ownership == ConnectionOwnership.PERSONAL,
                )
            )
            if connection is None:
                raise ResourceNotFoundError("Personal external connection not found.")
            server = await session.get(McpServerDefinition, connection.server_id)
            if server is None or server.auth_mode != McpAuthMode.OAUTH_AUTHORIZATION_CODE:
                raise ConflictError("Connection does not support OAuth refresh.")
            current = await self._vault.get(connection.credential_reference)
            if current is None or current.refresh_token is None:
                raise AuthenticationError("External connection has no refresh token.")
            previous_reference = connection.credential_reference
        metadata = await self._authorization_metadata(server)
        token = await self._oauth_client(
            server, client_secret=await self._oauth_client_secret(server)
        ).refresh_token(
            token_endpoint=metadata.token_endpoint,
            refresh_token=current.refresh_token,
        )
        credential = _credential_from_token(token)
        if credential.refresh_token is None:
            credential.refresh_token = current.refresh_token
        reference = await self._vault.put(credential)
        try:
            connection = await self._persist_personal_refresh(
                user_id=user_id,
                organization_id=organization_id,
                connection_id=connection_id,
                previous_reference=previous_reference,
                reference=reference,
                credential=credential,
                server=server,
                trace_id=trace_id,
            )
        except Exception:
            await self._revoke_uncommitted_reference(reference)
            raise
        await self._revoke_reference(previous_reference)
        return connection

    async def _persist_personal_refresh(
        self,
        *,
        user_id: UUID,
        organization_id: UUID,
        connection_id: UUID,
        previous_reference: str,
        reference: str,
        credential: Credential,
        server: McpServerDefinition,
        trace_id: UUID | None,
    ) -> ExternalConnection:
        async with self._session_factory() as session, session.begin():
            connection = await session.scalar(
                select(ExternalConnection)
                .where(
                    ExternalConnection.id == connection_id,
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.owner_user_id == user_id,
                    ExternalConnection.ownership == ConnectionOwnership.PERSONAL,
                )
                .with_for_update()
            )
            if connection is None:
                raise ResourceNotFoundError("Personal external connection not found.")
            if connection.credential_reference != previous_reference:
                raise ConflictError("External connection credentials changed during refresh.")
            old_reference = await session.scalar(
                select(CredentialReference).where(
                    CredentialReference.reference == previous_reference
                )
            )
            if old_reference:
                old_reference.status = "revocation_pending"
            session.add(
                CredentialReference(
                    organization_id=organization_id,
                    reference=reference,
                    vault_kind=self._vault.kind,
                    expires_at=credential.expires_at,
                )
            )
            connection.credential_reference = reference
            connection.expires_at = credential.expires_at
            connection.status = ConnectionStatus.ACTIVE
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="connection.refreshed",
                    resource_type="external_connection",
                    resource_id=str(connection.id),
                    trace_id=trace_id,
                    details={"server_code": server.code},
                )
            )
        return connection

    async def _mark_reference_revoked(self, reference: str) -> None:
        async with self._session_factory() as session, session.begin():
            stored = await session.scalar(
                select(CredentialReference).where(CredentialReference.reference == reference)
            )
            if stored is not None:
                stored.status = "revoked"

    async def get_usable_connection(
        self, user_id: UUID, organization_id: UUID, server_id: UUID
    ) -> ExternalConnection:
        await self._identities.access(user_id, organization_id)
        async with self._session_factory() as session:
            personal = await session.scalar(
                select(ExternalConnection).where(
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.server_id == server_id,
                    ExternalConnection.owner_user_id == user_id,
                    ExternalConnection.ownership == ConnectionOwnership.PERSONAL,
                    ExternalConnection.status == ConnectionStatus.ACTIVE,
                )
            )
            if personal is not None:
                return personal
            shared = await session.scalar(
                select(ExternalConnection).where(
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.server_id == server_id,
                    ExternalConnection.owner_user_id.is_(None),
                    ExternalConnection.ownership == ConnectionOwnership.ORGANIZATION,
                    ExternalConnection.status == ConnectionStatus.ACTIVE,
                )
            )
            if shared is None:
                raise AuthorizationError("No active external connection is available.")
            return shared

    async def get_personal_connection(
        self, user_id: UUID, organization_id: UUID, server_id: UUID
    ) -> ExternalConnection:
        """Return only the user's active personal connection.

        PermissionSystem's employee flow must never silently fall back to an
        organization credential, which has different authorization semantics.
        """

        await self._identities.access(user_id, organization_id)
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(ExternalConnection).where(
                    ExternalConnection.organization_id == organization_id,
                    ExternalConnection.server_id == server_id,
                    ExternalConnection.owner_user_id == user_id,
                    ExternalConnection.ownership == ConnectionOwnership.PERSONAL,
                    ExternalConnection.status == ConnectionStatus.ACTIVE,
                )
            )
            if connection is None:
                raise AuthorizationError("No active personal external connection is available.")
            return connection

    async def list_tool_grants(self, connection_id: UUID) -> set[str]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ConnectionToolGrant.tool_name).where(
                    ConnectionToolGrant.connection_id == connection_id,
                    ConnectionToolGrant.enabled.is_(True),
                )
            )
            return set(rows)

    async def _server(
        self, code: str, organization_id: UUID, *, enabled: bool
    ) -> McpServerDefinition:
        async with self._session_factory() as session:
            query = select(McpServerDefinition).where(
                McpServerDefinition.code == code,
                McpServerDefinition.organization_id == organization_id,
            )
            if enabled:
                query = query.where(McpServerDefinition.enabled.is_(True))
            server = await session.scalar(query)
            if server is None:
                raise ResourceNotFoundError("MCP Server is not registered or enabled.")
            return server

    async def _authorization_metadata(self, server: McpServerDefinition) -> OidcProviderMetadata:
        if server.authorization_endpoint and server.token_endpoint:
            return OidcProviderMetadata(
                issuer=server.authorization_server or "",
                authorization_endpoint=server.authorization_endpoint,
                token_endpoint=server.token_endpoint,
                code_challenge_methods_supported=["S256"],
            )
        if not server.authorization_server:
            raise ProtocolValidationError("MCP Server has no authorization server configuration.")
        return await self._discovery.discover_authorization_server(server.authorization_server)

    def _oauth_client(
        self, server: McpServerDefinition, *, client_secret: SecretStr | None = None
    ) -> AuthorizationCodeClient:
        auth_method = TokenEndpointAuthMethod(server.token_endpoint_auth_method)
        return AuthorizationCodeClient(
            client_id=self._oauth_client_id(server),
            client_secret=client_secret or SecretStr(""),
            redirect_uri=(
                server.redirect_uri
                or self._platform.external_connection_redirect_uri.format(server_code=server.code)
            ),
            scope=server.required_scope or "openid",
            token_endpoint_auth_method=auth_method,
        )

    async def _oauth_client_secret(self, server: McpServerDefinition) -> SecretStr:
        if not server.oauth_client_secret_reference:
            return SecretStr("")
        credential = await self._vault.get(server.oauth_client_secret_reference)
        if credential is None or credential.client_secret is None:
            raise AuthenticationError("OAuth client credential is unavailable.")
        return credential.client_secret

    def _oauth_client_id(self, server: McpServerDefinition) -> str:
        return server.oauth_client_id or self._default_client_id


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _constant_digest_match(value: str, expected_digest: str) -> bool:
    return secrets.compare_digest(
        hashlib.sha256(value.encode("utf-8")).hexdigest(), expected_digest
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _credential_from_token(token: OAuthToken) -> Credential:
    return Credential(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=token.expires_in),
        token_type=token.token_type,
    )


def _scopes(token: OAuthToken, server: McpServerDefinition) -> list[str]:
    scopes = token.scope.split() if token.scope else []
    return scopes or ([server.required_scope] if server.required_scope else [])


async def _subject_from_token(
    token: OAuthToken,
    metadata: OidcProviderMetadata,
    expected_nonce: str,
    client_id: str,
    *,
    validator: OidcIdTokenValidator,
) -> str:
    if token.id_token is None:
        if token.subject:
            return token.subject
        raise AuthenticationError("OAuth response has no stable external subject.")
    claims = await validator.validate(
        token.id_token.get_secret_value(),
        metadata,
        client_id=client_id,
        expected_nonce=expected_nonce,
    )
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise AuthenticationError("OAuth ID token has no stable subject.")
    if token.subject and token.subject != subject:
        raise AuthenticationError("OAuth subject does not match the ID token.")
    return subject
