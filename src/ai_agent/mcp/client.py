"""Official MCP SDK Streamable HTTP protocol probe."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from ai_agent.errors import McpConnectionError, ProtocolValidationError
from ai_agent.mcp.auth import AccessTokenProvider
from ai_agent.mcp.models import McpProbeResult, ToolCallSummary, ToolDescriptor


class McpProbeClient:
    def __init__(
        self,
        *,
        mcp_url: str,
        token_provider: AccessTokenProvider,
        expected_tools: tuple[str, ...] = (),
        timeout_seconds: float = 10.0,
    ) -> None:
        self._mcp_url = mcp_url
        self._token_provider = token_provider
        self._expected_tools = expected_tools
        self._timeout_seconds = timeout_seconds

    async def probe(
        self,
        *,
        call_tool_name: str | None = None,
        call_arguments: dict[str, Any] | None = None,
    ) -> McpProbeResult:
        token = await self._token_provider.get_access_token()
        trace_id = str(uuid4())
        headers = {
            "Authorization": f"Bearer {token.get_secret_value()}",
            "Accept": "application/json, text/event-stream",
            "X-Trace-Id": trace_id,
        }

        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(
                    self._mcp_url,
                    http_client=http_client,
                    terminate_on_close=False,
                ) as transport:
                    read_stream, write_stream = transport
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=self._timeout_seconds,
                    ) as session:
                        initialized = await session.initialize()
                        listed = await session.list_tools()
                        advertised_names = {tool.name for tool in listed.tools}
                        call_result: object | None = None
                        if call_tool_name and call_tool_name in advertised_names:
                            call_result = await session.call_tool(
                                call_tool_name,
                                call_arguments or {},
                                read_timeout_seconds=self._timeout_seconds,
                            )

            tools = [
                ToolDescriptor(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                )
                for tool in listed.tools
            ]
            names = {tool.name for tool in tools}
            missing = sorted(set(self._expected_tools) - names)
            if missing:
                raise ProtocolValidationError(
                    "MCP server is missing expected tools: " + ", ".join(missing)
                )
            if call_tool_name and call_tool_name not in names:
                raise ProtocolValidationError(
                    f"Requested probe tool is not advertised: {call_tool_name}."
                )

            sample_call = None
            if call_tool_name:
                if not isinstance(call_result, CallToolResult):
                    raise ProtocolValidationError(
                        "MCP probe tool did not return a completed CallToolResult."
                    )
                sample_call = ToolCallSummary(
                    name=call_tool_name,
                    is_error=call_result.is_error,
                    structured_content=call_result.structured_content,
                )

            return McpProbeResult(
                trace_id=trace_id,
                protocol_version=initialized.protocol_version,
                server_name=initialized.server_info.name,
                server_version=initialized.server_info.version,
                tools=tools,
                missing_expected_tools=missing,
                sample_call=sample_call,
            )
        except ProtocolValidationError:
            raise
        except Exception as exc:
            raise McpConnectionError(
                "MCP Streamable HTTP initialize/list/call probe failed."
            ) from exc


class McpToolClient:
    """Small per-request MCP client used by the Gateway.

    Each operation opens a fresh Streamable HTTP session. This keeps credentials and
    cancellation scoped to one call and avoids sharing SDK sessions across users.
    """

    def __init__(
        self,
        *,
        mcp_url: str,
        token_provider: AccessTokenProvider,
        timeout_seconds: float = 10.0,
        extra_headers: dict[str, str] | None = None,
        credential_header: str = "Authorization",
    ) -> None:
        self._mcp_url = mcp_url
        self._token_provider = token_provider
        self._timeout_seconds = timeout_seconds
        self._extra_headers = extra_headers or {}
        self._credential_header = credential_header

    async def list_tools(self) -> list[ToolDescriptor]:
        async def operation(session: ClientSession) -> list[ToolDescriptor]:
            await session.initialize()
            listed = await session.list_tools()
            return [
                ToolDescriptor(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                )
                for tool in listed.tools
            ]

        return cast(list[ToolDescriptor], await self._run(operation))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        async def operation(session: ClientSession) -> CallToolResult:
            await session.initialize()
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=self._timeout_seconds,
            )
            if not isinstance(result, CallToolResult):
                raise ProtocolValidationError("MCP tool did not return a CallToolResult.")
            return result

        return cast(CallToolResult, await self._run(operation))

    async def _run(self, operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        token = await self._token_provider.get_access_token()
        headers = {
            self._credential_header: (
                f"Bearer {token.get_secret_value()}"
                if self._credential_header.lower() == "authorization"
                else token.get_secret_value()
            ),
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(
                    self._mcp_url,
                    http_client=http_client,
                    terminate_on_close=False,
                ) as transport:
                    read_stream, write_stream = transport
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=self._timeout_seconds,
                    ) as session:
                        return await operation(session)
        except (ProtocolValidationError, McpConnectionError):
            raise
        except Exception as exc:
            raise McpConnectionError("MCP Gateway request failed.") from exc
