"""Outbound network policy for MCP HTTP endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network
from urllib.parse import SplitResult, urlsplit

from ai_agent.errors import ProtocolValidationError


class McpNetworkPolicyError(ProtocolValidationError):
    """Raised when an MCP endpoint violates the outbound network policy."""


@dataclass(frozen=True, slots=True)
class McpNetworkPolicy:
    """Allow safe MCP destinations while blocking SSRF-prone address classes."""

    allow_local_addresses: bool = True
    allow_private_addresses: bool = False
    allowed_hosts: frozenset[str] = frozenset()
    allowed_ips: tuple[IPv4Network | IPv6Network, ...] = ()

    @classmethod
    def from_values(
        cls,
        *,
        allow_local_addresses: bool = True,
        allow_private_addresses: bool = False,
        allowed_hosts: Iterable[str] = (),
        allowed_ips: Iterable[str] = (),
    ) -> McpNetworkPolicy:
        return cls(
            allow_local_addresses=allow_local_addresses,
            allow_private_addresses=allow_private_addresses,
            allowed_hosts=frozenset(_normalize_host(host) for host in allowed_hosts),
            allowed_ips=tuple(_parse_network(value) for value in allowed_ips),
        )

    def validate_url(self, url: str) -> None:
        """Validate the URL structure and any literal destination address."""

        parsed = _parse_http_url(url)
        host = _normalize_host(parsed.hostname or "")
        try:
            address = _normalize_address(ipaddress.ip_address(host))
        except ValueError:
            return
        if not self._is_address_allowed(host, address):
            raise McpNetworkPolicyError(
                "MCP endpoint is not allowed by the outbound network policy."
            )

    async def validate_url_resolution(self, url: str) -> None:
        """Resolve the endpoint immediately before a new HTTP session is opened."""

        parsed = _parse_http_url(url)
        host = _normalize_host(parsed.hostname or "")
        try:
            address = _normalize_address(ipaddress.ip_address(host))
        except ValueError:
            try:
                results = await asyncio.get_running_loop().getaddrinfo(
                    host,
                    parsed.port,
                    type=socket.SOCK_STREAM,
                )
            except (OSError, ValueError) as exc:
                raise McpNetworkPolicyError(
                    "MCP endpoint DNS resolution is not allowed by the outbound network policy."
                ) from exc
            addresses = {
                _normalize_address(ipaddress.ip_address(result[4][0])) for result in results
            }
            if not addresses or any(
                not self._is_address_allowed(host, candidate) for candidate in addresses
            ):
                raise McpNetworkPolicyError(
                    "MCP endpoint is not allowed by the outbound network policy."
                ) from None
            return

        if not self._is_address_allowed(host, address):
            raise McpNetworkPolicyError(
                "MCP endpoint is not allowed by the outbound network policy."
            )

    def _is_address_allowed(
        self,
        host: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        if host in self.allowed_hosts:
            if address.is_link_local or address.is_multicast or address.is_unspecified:
                return False
            if address.is_reserved:
                return False
            return True
        if any(address in network for network in self.allowed_ips):
            return True
        if address.is_loopback:
            return self.allow_local_addresses
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            return False
        if address.is_reserved:
            return False
        if address.is_private:
            return self.allow_private_addresses
        return True


def _parse_http_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise McpNetworkPolicyError(
            "MCP Server URL must be an absolute HTTP or HTTPS URL."
        ) from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise McpNetworkPolicyError("MCP Server URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise McpNetworkPolicyError(
            "MCP Server URL must not contain credentials or fragments."
        )
    if port is not None and not 0 <= port <= 65_535:
        raise McpNetworkPolicyError("MCP Server URL must be an absolute HTTP or HTTPS URL.")
    return parsed


def _normalize_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not host:
        raise ValueError("Host must not be empty.")
    return host


def _parse_network(value: str) -> IPv4Network | IPv6Network:
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(
            "Allowed MCP IP entries must be valid IP addresses or CIDR networks."
        ) from exc


def _normalize_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address
