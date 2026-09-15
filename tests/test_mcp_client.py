from __future__ import annotations

import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent.errors import ProtocolValidationError
from ai_agent.mcp.auth import StaticAccessTokenProvider
from ai_agent.mcp.client import LimitedAsyncByteStream, McpHttpClient, McpProbeClient


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    server = MCPServer("contract-test", version="1.0.0")

    @server.tool(structured_output=True)
    def list_datasets() -> dict[str, list[dict[str, str]]]:
        return {"datasets": [{"id": "employees", "name": "Employees"}]}

    @server.tool()
    def describe_dataset(dataset_id: str) -> dict[str, str]:
        return {"id": dataset_id}

    @server.tool()
    def query_dataset(dataset_id: str, limit: int = 10) -> dict[str, object]:
        return {"id": dataset_id, "limit": limit, "rows": []}

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    port = _free_port()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not uvicorn_server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not uvicorn_server.started:
        raise RuntimeError("Test MCP server did not start.")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


async def test_official_mcp_client_initializes_lists_and_calls(mcp_url: str) -> None:
    client = McpProbeClient(
        mcp_url=mcp_url,
        token_provider=StaticAccessTokenProvider(SecretStr("test-only-token")),
        expected_tools=("list_datasets", "describe_dataset", "query_dataset"),
    )

    result = await client.probe(call_tool_name="list_datasets")

    assert result.server_name == "contract-test"
    assert {tool.name for tool in result.tools} == {
        "list_datasets",
        "describe_dataset",
        "query_dataset",
    }
    assert result.sample_call is not None
    assert result.sample_call.is_error is False
    assert result.sample_call.structured_content == {
        "datasets": [{"id": "employees", "name": "Employees"}]
    }


async def test_missing_expected_tool_fails_contract(mcp_url: str) -> None:
    client = McpProbeClient(
        mcp_url=mcp_url,
        token_provider=StaticAccessTokenProvider(SecretStr("test-only-token")),
        expected_tools=("write_dataset",),
    )

    with pytest.raises(ProtocolValidationError, match="write_dataset"):
        await client.probe()


async def test_response_stream_stops_at_byte_limit() -> None:
    class Source(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"1234"
            yield b"5678"

    stream = LimitedAsyncByteStream(Source(), max_bytes=6)

    with pytest.raises(ProtocolValidationError, match="size limit"):
        _ = [chunk async for chunk in stream]


@pytest.mark.parametrize("content_length", ["not-a-number", "-1"])
async def test_response_rejects_invalid_content_length(content_length: str) -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Length": content_length}, content=b"ok")

    async with McpHttpClient(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ProtocolValidationError, match="invalid Content-Length"):
            await client.get("https://mcp.example.test/mcp")


async def test_response_rejects_declared_content_length_over_limit() -> None:
    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Length": "7"}, content=b"1234567")

    async with McpHttpClient(
        transport=httpx2.MockTransport(handler), max_response_bytes=6
    ) as client:
        with pytest.raises(ProtocolValidationError, match="size limit"):
            await client.get("https://mcp.example.test/mcp")


async def test_non_streaming_response_is_limited_while_downloading() -> None:
    class Source(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"1234"
            yield b"5678"

    async def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=Source())

    async with McpHttpClient(
        transport=httpx2.MockTransport(handler), max_response_bytes=6
    ) as client:
        with pytest.raises(ProtocolValidationError, match="size limit"):
            await client.get("https://mcp.example.test/mcp")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
