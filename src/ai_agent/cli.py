"""P0 configuration, discovery, MCP probe and server commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import uvicorn
from redis.asyncio import Redis

from ai_agent.audit.service import AuditService
from ai_agent.config import Settings
from ai_agent.errors import AiAgentError, ConfigurationError
from ai_agent.governance.quota import RedisRunQuota
from ai_agent.governance.retention import AuditRetentionService
from ai_agent.mcp.auth import (
    AccessTokenProvider,
    ClientCredentialsTokenProvider,
    StaticAccessTokenProvider,
)
from ai_agent.mcp.client import McpProbeClient
from ai_agent.mcp.network_policy import McpNetworkPolicy
from ai_agent.oauth.client import DiscoveryClient
from ai_agent.observability.logging import configure_logging
from ai_agent.persistence import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-agent", description="Enterprise AI Agent P0 tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-config", help="Validate runtime configuration")
    check.add_argument("--require-oidc", action="store_true")
    check.add_argument("--require-permission", action="store_true")
    check.add_argument("--require-token", action="store_true")

    subparsers.add_parser("probe-oidc", help="Validate Agent OIDC discovery metadata")

    permission = subparsers.add_parser(
        "probe-permission", help="Probe PermissionSystem OAuth metadata and MCP tools"
    )
    permission.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip protected-resource and authorization-server discovery",
    )
    permission.add_argument(
        "--call-list-datasets",
        action="store_true",
        help="Also make a read-only list_datasets tool call",
    )

    serve = subparsers.add_parser("serve", help="Run the P0 FastAPI service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    prune = subparsers.add_parser("prune-audit", help="Apply configured audit retention")
    prune.add_argument(
        "--execute",
        action="store_true",
        help="Delete matching records; omission performs a dry run",
    )

    verify_audit = subparsers.add_parser("verify-audit", help="Verify audit record integrity")
    verify_audit.add_argument("--organization-id", type=UUID)
    verify_audit.add_argument("--created-after", type=_parse_datetime)
    verify_audit.add_argument("--created-before", type=_parse_datetime)

    runs = subparsers.add_parser("runs", help="Operate the distributed Run switch")
    runs.add_argument("action", choices=("enable", "disable", "status"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        if args.command == "check-config":
            settings.validate_runtime(
                require_oidc=args.require_oidc,
                require_permission_system=args.require_permission,
                require_permission_token=args.require_token,
            )
            _print_json({"status": "valid", "environment": settings.environment.value})
            return 0
        if args.command == "probe-oidc":
            return asyncio.run(_probe_oidc(settings))
        if args.command == "probe-permission":
            return asyncio.run(
                _probe_permission(
                    settings,
                    skip_discovery=args.skip_discovery,
                    call_list_datasets=args.call_list_datasets,
                )
            )
        if args.command == "serve":
            settings.validate_runtime()
            uvicorn.run(
                "ai_agent.main:app",
                host=args.host or settings.host,
                port=args.port or settings.port,
                log_level=settings.log_level.lower(),
                proxy_headers=bool(settings.security.trusted_proxy_ips),
                forwarded_allow_ips=list(settings.security.trusted_proxy_ips),
            )
            return 0
        if args.command == "prune-audit":
            return asyncio.run(_prune_audit(settings, execute=args.execute))
        if args.command == "verify-audit":
            return asyncio.run(
                _verify_audit(
                    settings,
                    organization_id=args.organization_id,
                    created_after=args.created_after,
                    created_before=args.created_before,
                )
            )
        if args.command == "runs":
            return asyncio.run(_operate_runs(settings, args.action))
    except (AiAgentError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


async def _prune_audit(settings: Settings, *, execute: bool) -> int:
    settings.validate_runtime()
    if not settings.platform.enabled:
        raise ConfigurationError("Audit retention requires the platform to be enabled.")
    database = Database(
        settings.platform.database_url,
        password=settings.database_password(),
    )
    audit = _audit_service(settings)
    try:
        result = await AuditRetentionService(database.session_factory, audit).prune(
            settings.governance.audit_retention_days,
            execute=execute,
        )
        _print_json(
            {
                "status": "executed" if execute else "dry_run",
                "cutoff": result.cutoff.isoformat(),
                "matched": result.matched,
            }
        )
    finally:
        await database.dispose()
    return 0


async def _verify_audit(
    settings: Settings,
    *,
    organization_id: UUID | None,
    created_after: datetime | None,
    created_before: datetime | None,
) -> int:
    settings.validate_runtime()
    if not settings.platform.enabled:
        raise ConfigurationError("Audit verification requires the platform to be enabled.")
    database = Database(
        settings.platform.database_url,
        password=settings.database_password(),
    )
    try:
        async with database.session_factory() as session:
            result = await _audit_service(settings).verify(
                session,
                organization_id=organization_id,
                created_after=created_after,
                created_before=created_before,
            )
        _print_json(
            {
                "status": "valid"
                if result.invalid == 0 and result.key_mismatch == 0
                else "invalid",
                "checked": result.checked,
                "valid": result.valid,
                "unsigned": result.unsigned,
                "key_mismatch": result.key_mismatch,
                "invalid": result.invalid,
            }
        )
        return 1 if result.invalid > 0 or result.key_mismatch > 0 else 0
    finally:
        await database.dispose()


async def _operate_runs(settings: Settings, action: str) -> int:
    settings.validate_runtime()
    if not settings.platform.enabled or not settings.governance.quota.enabled:
        raise ConfigurationError("Distributed Run control requires platform quotas to be enabled.")
    redis = Redis.from_url(settings.platform.redis_url, password=settings.redis_password())
    quota = RedisRunQuota(
        redis,
        settings.governance.quota,
        settings.limits,
        lease_seconds=int(settings.limits.max_run_seconds) + 60,
    )
    database = Database(
        settings.platform.database_url,
        password=settings.database_password(),
    )
    audit = _audit_service(settings)
    try:
        if action == "disable":
            await quota.disable_runs()
            await _write_control_audit(database, audit, "governance.runs_disabled")
        elif action == "enable":
            await quota.enable_runs()
            await _write_control_audit(database, audit, "governance.runs_enabled")
        _print_json({"runs_enabled": await quota.runs_enabled()})
    finally:
        await redis.aclose()
        await database.dispose()
    return 0


async def _write_control_audit(database: Database, audit: AuditService, action: str) -> None:
    async with database.session_factory() as session, session.begin():
        session.add(
            audit.record(
                organization_id=None,
                actor_user_id=None,
                action=action,
                resource_type="platform",
                resource_id=None,
                trace_id=None,
                details={"source": "operator_cli"},
            )
        )


def _audit_service(settings: Settings) -> AuditService:
    return AuditService(
        settings.audit_integrity_key(),
        key_id=settings.governance.audit_integrity_key_id,
    )


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Datetime must be ISO 8601 format.") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Datetime must include a UTC offset.")
    return parsed


async def _probe_oidc(settings: Settings) -> int:
    settings.validate_runtime(require_oidc=True)
    metadata = await DiscoveryClient().discover_oidc(settings.oidc.issuer)
    _print_json(
        {
            "status": "ok",
            "issuer": metadata.issuer,
            "authorization_endpoint": metadata.authorization_endpoint,
            "token_endpoint": metadata.token_endpoint,
            "has_jwks_uri": metadata.jwks_uri is not None,
            "supports_s256": "S256" in metadata.code_challenge_methods_supported,
        }
    )
    return 0


async def _probe_permission(
    settings: Settings,
    *,
    skip_discovery: bool,
    call_list_datasets: bool,
) -> int:
    settings.validate_runtime(
        require_permission_system=True,
        require_permission_token=True,
    )
    permission = settings.permission_system
    discovery_result: dict[str, object] | None = None
    if not skip_discovery:
        discovery = DiscoveryClient(timeout_seconds=permission.request_timeout_seconds)
        resource = await discovery.discover_protected_resource(
            permission.mcp_url,
            required_scope=permission.scope,
        )
        authorization = await discovery.discover_authorization_server(
            resource.authorization_servers[0]
        )
        discovery_result = {
            "resource": resource.resource,
            "authorization_server": authorization.issuer,
            "required_scope": permission.scope,
        }

    probe = McpProbeClient(
        mcp_url=permission.mcp_url,
        token_provider=_build_permission_token_provider(settings),
        expected_tools=permission.expected_tools,
        timeout_seconds=permission.request_timeout_seconds,
        network_policy=_mcp_network_policy(settings),
    )
    result = await probe.probe(
        call_tool_name="list_datasets" if call_list_datasets else None,
    )
    _print_json(
        {
            "status": "ok",
            "discovery": discovery_result,
            "mcp": result.model_dump(mode="json"),
        }
    )
    return 0


def _mcp_network_policy(settings: Settings) -> McpNetworkPolicy:
    return McpNetworkPolicy.from_values(
        allow_local_addresses=settings.mcp_gateway.allow_local_addresses,
        allow_private_addresses=settings.mcp_gateway.allow_private_addresses,
        allowed_hosts=settings.mcp_gateway.allowed_hosts,
        allowed_ips=settings.mcp_gateway.allowed_ips,
    )


def _build_permission_token_provider(settings: Settings) -> AccessTokenProvider:
    permission = settings.permission_system
    if permission.has_probe_token:
        return StaticAccessTokenProvider(permission.access_token)
    if permission.has_service_credentials:
        return ClientCredentialsTokenProvider(
            token_url=permission.token_url,
            client_id=permission.service_client_id,
            client_secret=permission.service_client_secret,
            scope=permission.scope,
            auth_method=permission.token_endpoint_auth_method,
            timeout_seconds=permission.request_timeout_seconds,
        )
    raise ConfigurationError("No PermissionSystem probe credential is configured.")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
