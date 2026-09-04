"""P1 single-node LangGraph Agent using a provider-neutral model contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Required, TypedDict

from langgraph.graph import END, START, StateGraph

from ai_agent.errors import ModelProviderError
from ai_agent.models import ModelMessage, ModelProvider, ModelUsageResult


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
    ) -> AgentResult:
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
        return AgentResult(answer=result.get("answer", ""), usage=result["usage"])

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
