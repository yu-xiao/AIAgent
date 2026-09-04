"""Single-node Agent with bounded, provider-neutral MCP tool calling."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Required, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from ai_agent.errors import ModelProviderError
from ai_agent.mcp.models import (
    Citation,
    RunContext,
    ToolDefinition,
    ToolInvocationRecord,
    ToolResult,
)
from ai_agent.models import ModelMessage, ModelProvider, ModelToolCall, ModelUsageResult


class AgentState(TypedDict, total=False):
    messages: Required[list[ModelMessage]]
    max_output_tokens: Required[int]
    trace_id: Required[str]
    on_delta: Required[Callable[[str], Awaitable[None]]]
    answer: str
    usage: ModelUsageResult


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    usage: ModelUsageResult
    citations: list[Citation] = field(default_factory=list)
    tool_invocations: list[ToolInvocationRecord] = field(default_factory=list)


class SingleAgent:
    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        builder = StateGraph(AgentState)
        builder.add_node("model", self._model_node)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        self._graph = builder.compile()

    async def run(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
        on_delta: Callable[[str], Awaitable[None]],
        gateway: Any | None = None,
        context: RunContext | None = None,
        tool_set: str = "",
        max_model_rounds: int = 6,
        max_tool_calls: int = 10,
        tool_system_code: str | None = None,
        tool_allowlist: frozenset[str] | None = None,
        personal_only: bool = False,
    ) -> AgentResult:
        if gateway is None or context is None or not _supports_tools(self._provider):
            result = await self._graph.ainvoke(
                AgentState(
                    messages=messages,
                    max_output_tokens=max_output_tokens,
                    trace_id=trace_id,
                    on_delta=on_delta,
                )
            )
            if "usage" not in result:
                raise ModelProviderError("Model stream completed without usage information.")
            return AgentResult(
                answer=result.get("answer", ""),
                usage=result["usage"],
                citations=[],
                tool_invocations=[],
            )

        definitions = await _list_gateway_tools(
            gateway, context, tool_set, tool_system_code, personal_only
        )
        if tool_allowlist is not None:
            definitions = [item for item in definitions if item.name in tool_allowlist]
        provider_tools = [_provider_tool(definition) for definition in definitions]
        working_messages = list(messages)
        all_citations: list[Citation] = []
        invocations: list[ToolInvocationRecord] = []
        total_usage = ModelUsageResult(input_tokens=0, output_tokens=0)
        tool_calls_used = 0
        for _ in range(max_model_rounds):
            answer, usage, tool_calls = await self._stream_model(
                working_messages,
                max_output_tokens=max_output_tokens,
                trace_id=trace_id,
                on_delta=on_delta,
                tools=provider_tools,
            )
            total_usage = ModelUsageResult(
                input_tokens=total_usage.input_tokens + usage.input_tokens,
                output_tokens=total_usage.output_tokens + usage.output_tokens,
            )
            if not tool_calls:
                return AgentResult(answer, total_usage, all_citations, invocations)
            if tool_calls_used + len(tool_calls) > max_tool_calls:
                raise ModelProviderError("Agent exceeded the configured MCP Tool call limit.")
            working_messages.append(
                ModelMessage(role="assistant", content=answer, tool_calls=tuple(tool_calls))
            )
            for call in tool_calls:
                tool_calls_used += 1
                if tool_allowlist is not None and call.name not in tool_allowlist:
                    raise ModelProviderError(
                        "Agent requested a Tool outside the configured allowlist."
                    )
                result = await _call_gateway(
                    gateway,
                    context,
                    call.name,
                    call.arguments,
                    tool_system_code,
                    personal_only,
                )
                _append_result(working_messages, call, result)
                all_citations.extend(result.citations)
                if result.citation is not None and not result.citations:
                    all_citations.append(result.citation)
                if result.citation is not None:
                    invocations.append(
                        ToolInvocationRecord(
                            tool_name=call.name,
                            server_code=result.citation.server_code,
                            status="failed" if result.is_error else "succeeded",
                            arguments_digest=_digest_arguments(call.arguments),
                            trace_id=trace_id,
                            duration_ms=result.duration_ms,
                        )
                    )
        raise ModelProviderError("Agent exceeded the configured model round limit.")

    async def _stream_model(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
        on_delta: Callable[[str], Awaitable[None]],
        tools: list[dict[str, Any]],
    ) -> tuple[str, ModelUsageResult, list[ModelToolCall]]:
        chunks: list[str] = []
        calls: list[ModelToolCall] = []
        usage: ModelUsageResult | None = None
        stream_kwargs: dict[str, Any] = {
            "max_output_tokens": max_output_tokens,
            "trace_id": trace_id,
        }
        if _supports_tools(self._provider):
            stream_kwargs["tools"] = tools or None
        async for event in self._provider.stream(messages, **stream_kwargs):
            if event.delta:
                chunks.append(event.delta)
                await on_delta(event.delta)
            if event.tool_calls:
                calls.extend(event.tool_calls)
            if event.usage:
                usage = event.usage
        if usage is None:
            raise ModelProviderError("Model stream completed without usage information.")
        return "".join(chunks), usage, calls

    async def _model_node(self, state: AgentState) -> dict[str, object]:
        chunks: list[str] = []
        usage: ModelUsageResult | None = None
        async for event in self._provider.stream(
            state["messages"],
            max_output_tokens=state["max_output_tokens"],
            trace_id=state["trace_id"],
        ):
            if event.delta:
                chunks.append(event.delta)
                await state["on_delta"](event.delta)
            if event.usage:
                usage = event.usage
        if usage is None:
            raise ModelProviderError("Model stream completed without usage information.")
        return {"answer": "".join(chunks), "usage": usage}


def _supports_tools(provider: ModelProvider) -> bool:
    try:
        parameters = inspect.signature(provider.stream).parameters
    except (TypeError, ValueError):
        return False
    return "tools" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


async def _list_gateway_tools(
    gateway: Any,
    context: RunContext,
    tool_set: str,
    system_code: str | None,
    personal_only: bool,
) -> list[ToolDefinition]:
    method = gateway.list_tools
    parameters = inspect.signature(method).parameters
    kwargs: dict[str, Any] = {}
    if "system_code" in parameters:
        kwargs["system_code"] = system_code
    if "personal_only" in parameters:
        kwargs["personal_only"] = personal_only
    return cast(list[ToolDefinition], await method(context, tool_set, **kwargs))


async def _call_gateway(
    gateway: Any,
    context: RunContext,
    tool_name: str,
    arguments: dict[str, Any],
    system_code: str | None,
    personal_only: bool,
) -> ToolResult:
    method = gateway.call
    parameters = inspect.signature(method).parameters
    kwargs: dict[str, Any] = {}
    if "system_code" in parameters:
        kwargs["system_code"] = system_code
    if "personal_only" in parameters:
        kwargs["personal_only"] = personal_only
    return cast(ToolResult, await method(context, tool_name, arguments, **kwargs))


def _provider_tool(definition: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": definition.name,
            "description": definition.description or "",
            "parameters": definition.input_schema or {"type": "object", "properties": {}},
        },
    }


def _append_result(messages: list[ModelMessage], call: ModelToolCall, result: ToolResult) -> None:
    payload = result.structured_content if result.structured_content is not None else result.content
    if result.citation is not None:
        payload = {
            "data": payload,
            "citation": result.citation.model_dump(mode="json"),
            "partial": result.truncated or result.citation.partial,
        }
    messages.append(
        ModelMessage(
            role="tool",
            content=json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")),
            tool_call_id=call.id,
        )
    )


def _digest_arguments(arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
