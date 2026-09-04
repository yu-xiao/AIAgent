"""Safe output models for MCP protocol probes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolDescriptor(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class ToolCallSummary(BaseModel):
    name: str
    is_error: bool
    structured_content: Any = None


class McpProbeResult(BaseModel):
    trace_id: str
    protocol_version: str
    server_name: str
    server_version: str
    tools: list[ToolDescriptor]
    missing_expected_tools: list[str] = Field(default_factory=list)
    sample_call: ToolCallSummary | None = None
