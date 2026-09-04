from __future__ import annotations

import json

import pytest
import respx
from httpx import Request, Response
from pydantic import SecretStr

from ai_agent.config import ModelSettings
from ai_agent.errors import ModelProviderError
from ai_agent.models import ModelMessage
from ai_agent.models.openai_compatible import OpenAICompatibleProvider


@respx.mock
async def test_openai_compatible_stream_and_usage_contract() -> None:
    def response(request: Request) -> Response:
        assert request.headers["authorization"] == "Bearer model-secret"
        assert request.headers["x-trace-id"] == "trace-one"
        payload = json.loads(request.content)
        assert payload["model"] == "configured-model"
        assert payload["stream_options"] == {"include_usage": True}
        return Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    respx.post("https://model.example.test/v1/chat/completions").mock(side_effect=response)
    provider = _provider()
    events = [
        event
        async for event in provider.stream(
            [ModelMessage(role="user", content="Hi")],
            max_output_tokens=100,
            trace_id="trace-one",
        )
    ]
    assert [event.delta for event in events if event.delta] == ["Hello"]
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 7
    assert events[-1].usage.output_tokens == 2


@respx.mock
async def test_stream_without_usage_fails_closed() -> None:
    respx.post("https://model.example.test/v1/chat/completions").mock(
        return_value=Response(
            200,
            text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n',
        )
    )
    with pytest.raises(ModelProviderError, match="token usage"):
        _ = [
            event
            async for event in _provider().stream(
                [ModelMessage(role="user", content="Hi")],
                max_output_tokens=100,
                trace_id="trace-two",
            )
        ]


@respx.mock
async def test_openai_compatible_stream_parses_tool_calls() -> None:
    respx.post("https://model.example.test/v1/chat/completions").mock(
        return_value=Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                '"function":{"name":"list_datasets","arguments":"{\\"limit\\":"}}]}}]}\n\n'
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":"10}"}}]}}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\n'
                "data: [DONE]\n\n"
            ),
            headers={"Content-Type": "text/event-stream"},
        )
    )

    events = [
        event
        async for event in _provider().stream(
            [ModelMessage(role="user", content="List datasets")],
            max_output_tokens=100,
            trace_id="trace-tool",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "list_datasets",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    ]
    calls = [call for event in events for call in event.tool_calls]
    assert calls[0].name == "list_datasets"
    assert calls[0].arguments == {"limit": 10}


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ModelSettings(
            enabled=True,
            base_url="https://model.example.test/v1",
            model="configured-model",
            api_key=SecretStr("model-secret"),
        )
    )
