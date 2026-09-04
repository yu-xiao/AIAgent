"""Safe output models for MCP protocol probes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ToolDescriptor(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    version: str = "1"
    tool_set: str | None = None
    risk_level: str = "low"


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


class ToolDefinition(BaseModel):
    """Platform-normalized, allow-listed MCP tool metadata."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    server_code: str
    version: str = "1"
    tool_set: str | None = None
    risk_level: str = "low"


class Citation(BaseModel):
    source_system: str
    server_code: str
    tool_name: str
    queried_at: datetime
    trace_id: str
    resource_id: str | None = None
    partial: bool = False


class ToolResult(BaseModel):
    name: str
    is_error: bool = False
    structured_content: Any = None
    content: list[Any] = Field(default_factory=list)
    trace_id: str
    truncated: bool = False
    citation: Citation | None = None
    citations: list[Citation] = Field(default_factory=list)
    duration_ms: float | None = None


class ToolInvocationRecord(BaseModel):
    """Safe, persistence-ready summary of a Gateway invocation."""

    tool_name: str
    server_code: str
    status: str
    arguments_digest: str
    trace_id: str
    duration_ms: float | None = None
    error: str | None = None


class RunContext(BaseModel):
    organization_id: UUID
    user_id: UUID
    trace_id: str
    deadline: datetime | None = None
    token_budget: int = 0

    def has_expired(self) -> bool:
        return self.deadline is not None and self.deadline <= datetime.now(UTC)
