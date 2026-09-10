from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from ai_agent.config import McpGatewaySettings
from ai_agent.errors import McpConnectionError
from ai_agent.mcp.auth import StaticAccessTokenProvider
from ai_agent.mcp.client import McpProbeClient
from ai_agent.mcp.network_policy import McpNetworkPolicy, McpNetworkPolicyError


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://localhost:8000/mcp", True),
        ("http://localhost.localdomain:8000/mcp", True),
        ("http://127.0.0.1:8000/mcp", True),
        ("http://[::1]:8000/mcp", True),
        ("http://10.0.0.1:8000/mcp", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("http://0.0.0.0:8000/mcp", False),
        ("http://224.0.0.1:8000/mcp", False),
        ("http://192.0.2.1:8000/mcp", False),
    ],
)
def test_default_policy_allows_local_and_rejects_ssrf_targets(url: str, allowed: bool) -> None:
    policy = McpNetworkPolicy()

    if allowed:
        policy.validate_url(url)
    else:
        with pytest.raises(McpNetworkPolicyError):
            policy.validate_url(url)


def test_policy_supports_explicit_private_network_configuration() -> None:
    policy = McpNetworkPolicy.from_values(
        allow_private_addresses=True,
        allowed_hosts=[" Internal.Example. ", "internal.example"],
        allowed_ips=["10.0.0.1", "10.0.0.0/24", "10.0.0.0/24"],
    )

    assert policy.allowed_hosts == frozenset({"internal.example"})
    assert tuple(str(network) for network in policy.allowed_ips) == (
        "10.0.0.1/32",
        "10.0.0.0/24",
        "10.0.0.0/24",
    )
    policy.validate_url("http://10.0.0.1:8000/mcp")


def test_allowed_ip_can_override_default_private_block() -> None:
    policy = McpNetworkPolicy.from_values(allowed_ips=["10.0.0.0/8"])

    policy.validate_url("http://10.1.2.3:8000/mcp")


def test_configuration_normalizes_and_validates_network_settings() -> None:
    settings = McpGatewaySettings(
        allowed_hosts=(" Internal.Example. ", "internal.example"),
        allowed_ips=("10.0.0.1", "10.0.0.0/24"),
    )

    assert settings.allowed_hosts == ("internal.example",)
    assert settings.allowed_ips == ("10.0.0.1/32", "10.0.0.0/24")

    with pytest.raises(ValidationError):
        McpGatewaySettings(allowed_ips=("not-an-ip",))
    with pytest.raises(ValidationError):
        McpGatewaySettings(allowed_hosts=("bad host",))


async def test_dns_validation_rejects_any_dangerous_result() -> None:
    policy = McpNetworkPolicy()
    getaddrinfo = AsyncMock(
        return_value=[
            (2, 1, 6, "", ("203.0.113.10", 443)),
            (2, 1, 6, "", ("10.0.0.10", 443)),
        ]
    )

    with patch("asyncio.BaseEventLoop.getaddrinfo", getaddrinfo):
        with pytest.raises(McpNetworkPolicyError):
            await policy.validate_url_resolution("https://mcp.example.test/mcp")


async def test_probe_converts_network_policy_failure_without_leaking_token() -> None:
    token = "super-secret-token"

    class FailingNetworkPolicy:
        async def validate_url_resolution(self, url: str) -> None:
            del url
            raise McpNetworkPolicyError("internal details")

    client = McpProbeClient(
        mcp_url="http://localhost:8000/mcp",
        token_provider=StaticAccessTokenProvider(SecretStr(token)),
        network_policy=FailingNetworkPolicy(),  # type: ignore[arg-type]
    )

    with pytest.raises(McpConnectionError) as raised:
        await client.probe()

    assert str(raised.value) == "MCP endpoint is not allowed by the outbound network policy."
    assert token not in str(raised.value)
