"""Minimal OpenAI-compatible Chat Completions streaming adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ai_agent.config import ModelSettings
from ai_agent.errors import ModelProviderError
from ai_agent.models.base import ModelMessage, ModelStreamEvent, ModelToolCall, ModelUsageResult


class OpenAICompatibleProvider:
    def __init__(self, settings: ModelSettings) -> None:
        self._settings = settings

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    @property
    def model_name(self) -> str:
        return self._settings.model

    def conservative_input_tokens(self, messages: list[ModelMessage]) -> int:
        # Byte count plus message framing is a deliberately conservative tokenizer-independent cap.
        return sum(len(message.content.encode("utf-8")) + 8 for message in messages)

    async def stream(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        endpoint = f"{self._settings.base_url.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [_serialize_message(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-Trace-Id": trace_id,
        }
        usage: ModelUsageResult | None = None
        tool_call_buffers: dict[int, dict[str, str]] = {}
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", endpoint, json=payload, headers=headers) as response,
            ):
                response.raise_for_status()
                async for data in _iter_sse_data(response):
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    delta = _extract_delta(chunk)
                    if delta:
                        yield ModelStreamEvent(delta=delta)
                    _collect_tool_calls(chunk, tool_call_buffers)
                    parsed_usage = _extract_usage(chunk)
                    if parsed_usage is not None:
                        usage = parsed_usage
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ModelProviderError("Model provider streaming request failed.") from exc
        if usage is None:
            raise ModelProviderError("Model provider did not return token usage for cost control.")
        if tool_call_buffers:
            calls: list[ModelToolCall] = []
            for index in sorted(tool_call_buffers):
                item = tool_call_buffers[index]
                if not item["name"]:
                    raise ModelProviderError("Model provider returned a tool call without a name.")
                try:
                    arguments = json.loads(item["arguments"] or "{}")
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ModelProviderError(
                        "Model provider returned invalid tool arguments."
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelProviderError("Model provider returned non-object tool arguments.")
                calls.append(
                    ModelToolCall(
                        id=item["id"] or f"tool-call-{index}",
                        name=item["name"],
                        arguments=arguments,
                    )
                )
            yield ModelStreamEvent(tool_calls=tuple(calls))
        yield ModelStreamEvent(usage=usage)


def _serialize_message(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.role == "tool" and message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _collect_tool_calls(chunk: dict[str, Any], buffers: dict[int, dict[str, str]]) -> None:
    choices = chunk.get("choices", [])
    if not choices:
        return
    delta = choices[0].get("delta", {})
    for item in delta.get("tool_calls", []) or []:
        index = item.get("index", 0)
        if not isinstance(index, int):
            raise ModelProviderError("Model provider returned an invalid tool call index.")
        current = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if item.get("id"):
            current["id"] += str(item["id"])
        function = item.get("function", {})
        if function.get("name"):
            current["name"] += str(function["name"])
        if function.get("arguments"):
            current["arguments"] += str(function["arguments"])


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def _extract_delta(chunk: dict[str, Any]) -> str | None:
    choices = chunk.get("choices", [])
    if not choices:
        return None
    content = choices[0].get("delta", {}).get("content")
    if content is None:
        return None
    if not isinstance(content, str):
        raise ModelProviderError("Model provider returned an invalid text delta.")
    return content


def _extract_usage(chunk: dict[str, Any]) -> ModelUsageResult | None:
    usage = chunk.get("usage")
    if usage is None:
        return None
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ModelProviderError("Model provider returned invalid token usage.")
    return ModelUsageResult(input_tokens=input_tokens, output_tokens=output_tokens)
