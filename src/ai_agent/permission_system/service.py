"""PermissionSystem business boundary built on the MCP Gateway.

This module deliberately does not know PermissionSystem's database or REST API. It
only allows the configured, read-only MCP contract and preserves the Gateway's
identity, authorization and citation behavior.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from ai_agent.errors import AuthorizationError, ProtocolValidationError, ResourceNotFoundError
from ai_agent.mcp.gateway import McpGateway
from ai_agent.mcp.models import RunContext, ToolDefinition, ToolResult

PERMISSION_SYSTEM_CODE = "permission-system"
DEFAULT_PERMISSION_TOOLS = ("list_datasets", "describe_dataset", "query_dataset")
_FORBIDDEN_ARGUMENT_KEYS = {
    "access_token",
    "authorization",
    "client_secret",
    "cookie",
    "headers",
    "http_method",
    "password",
    "script",
    "sql",
    "sql_query",
    "statement",
    "token",
    "url",
}
_MAX_ARGUMENT_BYTES = 32_768


class PermissionSystemService:
    """PermissionSystem's first-party, read-only Tool facade."""

    def __init__(
        self,
        gateway: McpGateway,
        *,
        expected_tools: tuple[str, ...] = DEFAULT_PERMISSION_TOOLS,
    ) -> None:
        self._gateway = gateway
        self._expected_tools = tuple(dict.fromkeys(expected_tools))
        if not self._expected_tools:
            raise ValueError("PermissionSystem must expose at least one expected read-only Tool.")

    @property
    def expected_tools(self) -> tuple[str, ...]:
        return self._expected_tools

    async def list_tools(self, context: RunContext) -> list[ToolDefinition]:
        method = self._gateway.list_tools
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(method).parameters
        if "system_code" in parameters:
            kwargs["system_code"] = PERMISSION_SYSTEM_CODE
        if "personal_only" in parameters:
            kwargs["personal_only"] = True
        # The server's registered allow-list is authoritative. The optional
        # "permission" Tool Set is not required because P0 servers expose the
        # three stable names directly.
        tools = await method(context, "", **kwargs)
        expected = set(self._expected_tools)
        return [tool for tool in tools if tool.name in expected]

    async def call_readonly(
        self,
        context: RunContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        if tool_name not in self._expected_tools:
            raise AuthorizationError("PermissionSystem Tool is not in the read-only allowlist.")
        _validate_arguments(arguments)
        available = await self.list_tools(context)
        if tool_name not in {tool.name for tool in available}:
            raise ResourceNotFoundError(
                "PermissionSystem Tool is not available for this connection."
            )
        method = self._gateway.call
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(method).parameters
        if "system_code" in parameters:
            kwargs["system_code"] = PERMISSION_SYSTEM_CODE
        if "personal_only" in parameters:
            kwargs["personal_only"] = True
        result = await method(context, tool_name, arguments, **kwargs)
        if result.is_error and _looks_like_permission_denial(result):
            raise AuthorizationError("PermissionSystem denied the requested data scope.")
        return result

    async def call(
        self, context: RunContext, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """ToolGateway-compatible alias used by orchestration adapters."""

        return await self.call_readonly(context, tool_name, arguments)

    async def list_datasets(self, context: RunContext, **arguments: Any) -> ToolResult:
        return await self.call_readonly(context, "list_datasets", arguments)

    async def describe_dataset(
        self, context: RunContext, dataset_id: str, **arguments: Any
    ) -> ToolResult:
        return await self.call_readonly(
            context, "describe_dataset", {"dataset_id": dataset_id, **arguments}
        )

    async def query_dataset(
        self, context: RunContext, dataset_id: str, **arguments: Any
    ) -> ToolResult:
        return await self.call_readonly(
            context, "query_dataset", {"dataset_id": dataset_id, **arguments}
        )


def _validate_arguments(arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ProtocolValidationError("PermissionSystem Tool arguments must be a JSON object.")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(
            "PermissionSystem Tool arguments must be JSON serializable."
        ) from exc
    if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        raise ProtocolValidationError("PermissionSystem Tool arguments exceed the size limit.")
    _check_nested_keys(arguments)


def _check_nested_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_ARGUMENT_KEYS:
                raise AuthorizationError(
                    "PermissionSystem Tool arguments contain a forbidden field."
                )
            _check_nested_keys(child)
    elif isinstance(value, list):
        for item in value:
            _check_nested_keys(item)


def _looks_like_permission_denial(result: ToolResult) -> bool:
    payload = result.structured_content if result.structured_content is not None else result.content
    text = json.dumps(payload, ensure_ascii=False, default=str).lower()
    return any(
        marker in text
        for marker in ("forbidden", "unauthorized", "access denied", "permission denied")
    )
