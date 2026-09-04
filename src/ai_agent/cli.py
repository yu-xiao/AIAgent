"""P0 configuration, discovery, MCP probe and server commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

import uvicorn

from ai_agent.config import Settings
from ai_agent.errors import AiAgentError, ConfigurationError
from ai_agent.mcp.auth import (
    AccessTokenProvider,
    ClientCredentialsTokenProvider,
    StaticAccessTokenProvider,
)
from ai_agent.mcp.client import McpProbeClient
from ai_agent.oauth.client import DiscoveryClient


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
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
            uvicorn.run(
                "ai_agent.main:app",
                host=args.host or settings.host,
                port=args.port or settings.port,
                log_level=settings.log_level.lower(),
            )
            return 0
    except (AiAgentError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


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
