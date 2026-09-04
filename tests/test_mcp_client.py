from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from ai_agent.errors import ProtocolValidationError
from ai_agent.mcp.auth import StaticAccessTokenProvider
from ai_agent.mcp.client import McpProbeClient


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
