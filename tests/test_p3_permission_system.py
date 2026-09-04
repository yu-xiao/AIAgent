from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from ai_agent.agents.single_agent import SingleAgent
from ai_agent.config import RunLimitSettings
from ai_agent.conversations.service import ConversationService
from ai_agent.errors import AuthorizationError, ResourceNotFoundError
from ai_agent.mcp.models import Citation, RunContext, ToolDefinition, ToolResult
from ai_agent.models import ModelMessage, ModelStreamEvent, ModelToolCall, ModelUsageResult
from ai_agent.permission_system.evaluation import (
    EvaluationAnswer,
    GoldenQuestion,
    citation_coverage,
    evaluate_golden_questions,
)
from ai_agent.permission_system.service import PermissionSystemService
from ai_agent.persistence.models import RunStatus


class FakePermissionGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.tools = [
            ToolDefinition(
                name="list_datasets",
                description="List authorized datasets",
                input_schema={"type": "object"},
                server_code="permission",
            ),
            ToolDefinition(
                name="query_dataset",
                input_schema={"type": "object", "required": ["dataset_id"]},
                server_code="permission",
            ),
        ]

    async def list_tools(
        self,
        context: RunContext,
        tool_set: str = "",
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> list[ToolDefinition]:
        del context, tool_set
        assert system_code == "permission-system"
        assert personal_only is True
        return self.tools

    async def call(
        self,
        context: RunContext,
        tool_name: str,
        arguments: dict[str, Any],
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> ToolResult:
        assert system_code == "permission-system"
        assert personal_only is True
        self.calls.append((context.trace_id, tool_name, arguments))
        citation = Citation(
            source_system="permission-system",
            server_code="permission",
            tool_name=tool_name,
            queried_at=datetime.now(UTC),
            trace_id=context.trace_id,
        )
        return ToolResult(
            name=tool_name,
            structured_content={"authorized": True},
            trace_id=context.trace_id,
            citation=citation,
            citations=[citation],
        )


def _context() -> RunContext:
    return RunContext(
        organization_id=uuid4(),
        user_id=uuid4(),
        trace_id=str(uuid4()),
    )


async def test_permission_service_allows_only_configured_read_tools() -> None:
    gateway = FakePermissionGateway()
    service = PermissionSystemService(gateway, expected_tools=("list_datasets", "query_dataset"))

    result = await service.query_dataset(_context(), dataset_id="orders")

    assert result.citation is not None
    assert gateway.calls[0][1:] == ("query_dataset", {"dataset_id": "orders"})
    with pytest.raises(AuthorizationError):
        await service.call_readonly(_context(), "delete_dataset", {})
    with pytest.raises(AuthorizationError):
        await service.call_readonly(_context(), "query_dataset", {"sql": "select 1"})


async def test_permission_service_rejects_tool_missing_from_connection() -> None:
    gateway = FakePermissionGateway()
    gateway.tools = []
    service = PermissionSystemService(gateway)

    with pytest.raises(ResourceNotFoundError):
        await service.list_datasets(_context())


async def test_permission_service_maps_explicit_scope_denial() -> None:
    class DeniedGateway(FakePermissionGateway):
        async def call(
            self,
            context: RunContext,
            tool_name: str,
            arguments: dict[str, Any],
            system_code: str | None = None,
            personal_only: bool = False,
        ) -> ToolResult:
            del arguments, system_code, personal_only
            return ToolResult(
                name=tool_name,
                is_error=True,
                structured_content={"error": "access denied"},
                trace_id=context.trace_id,
            )

    service = PermissionSystemService(DeniedGateway())

    with pytest.raises(AuthorizationError, match="data scope"):
        await service.list_datasets(_context())


class ToolCallingProvider:
    provider_name = "test"
    model_name = "tool-test"

    def __init__(self) -> None:
        self.round = 0
        self.seen_messages: list[list[ModelMessage]] = []

    def conservative_input_tokens(self, messages: list[ModelMessage]) -> int:
        return sum(len(message.content) + 8 for message in messages)

    async def stream(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        del max_output_tokens, trace_id
        self.seen_messages.append(messages)
        self.round += 1
        if self.round == 1:
            assert tools and tools[0]["function"]["name"] == "list_datasets"
            yield ModelStreamEvent(
                tool_calls=(ModelToolCall(id="call-1", name="list_datasets", arguments={}),)
            )
        else:
            assert messages[-1].role == "tool"
            yield ModelStreamEvent(delta="Authorized datasets found.")
        yield ModelStreamEvent(usage=ModelUsageResult(input_tokens=10, output_tokens=2))


class FakeAgentGateway(FakePermissionGateway):
    async def list_tools(
        self,
        context: RunContext,
        tool_set: str = "",
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> list[ToolDefinition]:
        del context, tool_set, system_code, personal_only
        return [
            ToolDefinition(
                name="list_datasets",
                input_schema={"type": "object"},
                server_code="permission",
            )
        ]

    async def call(
        self,
        context: RunContext,
        tool_name: str,
        arguments: dict[str, Any],
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> ToolResult:
        del arguments, system_code, personal_only
        citation = Citation(
            source_system="permission-system",
            server_code="permission",
            tool_name=tool_name,
            queried_at=datetime.now(UTC),
            trace_id=context.trace_id,
        )
        return ToolResult(
            name=tool_name,
            structured_content={"datasets": ["orders"]},
            trace_id=context.trace_id,
            citation=citation,
            citations=[citation],
        )


async def test_single_agent_executes_mcp_tool_and_returns_citation() -> None:
    provider = ToolCallingProvider()
    agent = SingleAgent(provider)
    deltas: list[str] = []

    result = await agent.run(
        [ModelMessage(role="user", content="list my datasets")],
        max_output_tokens=100,
        trace_id="trace-p3",
        on_delta=lambda delta: _record_delta(deltas, delta),
        gateway=FakeAgentGateway(),
        context=_context(),
        max_model_rounds=3,
        max_tool_calls=2,
    )

    assert result.answer == "Authorized datasets found."
    assert result.citations[0].tool_name == "list_datasets"
    assert result.tool_invocations[0].arguments_digest
    assert deltas == ["Authorized datasets found."]


async def _record_delta(deltas: list[str], delta: str) -> None:
    deltas.append(delta)


async def test_golden_evaluation_checks_tool_and_citation_coverage() -> None:
    async def runner(question: GoldenQuestion) -> EvaluationAnswer:
        return EvaluationAnswer(
            answer=question.question,
            used_tool=question.expected_tool,
            citations=(
                Citation(
                    source_system="permission-system",
                    server_code="permission",
                    tool_name=question.expected_tool,
                    queried_at=datetime.now(UTC),
                    trace_id="trace-p3",
                ),
            ),
        )

    results = await evaluate_golden_questions(
        runner,
        [
            GoldenQuestion("one", "list", "list_datasets"),
        ],
    )
    assert results[0].passed is True
    assert citation_coverage(results) == 1.0


async def test_run_persists_tool_evidence(platform_runtime) -> None:
    database = platform_runtime.services.database
    identities = platform_runtime.services.identities
    user = platform_runtime.user
    organization = platform_runtime.organization
    conversations = ConversationService(database.session_factory, identities, RunLimitSettings())
    conversation = await conversations.create_conversation(user.id, organization.id, "P3 evidence")
    run, created = await conversations.create_run(
        user.id,
        organization.id,
        conversation.id,
        "query orders",
        "p3-evidence",
        uuid4(),
    )
    assert created is True
    claimed = await conversations.claim_run(run.id)
    assert claimed is not None and claimed.status == RunStatus.RUNNING
    citation = Citation(
        source_system="permission-system",
        server_code="permission",
        tool_name="query_dataset",
        queried_at=datetime.now(UTC),
        trace_id=str(run.trace_id),
    )
    from ai_agent.mcp.models import ToolInvocationRecord

    await conversations.complete_run(
        run.id,
        answer="orders",
        provider="test",
        model="test",
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.01,
        citations=[citation],
        tool_invocations=[
            ToolInvocationRecord(
                tool_name="query_dataset",
                server_code="permission",
                status="succeeded",
                arguments_digest="digest",
                trace_id=str(run.trace_id),
            )
        ],
    )
    assert len(await conversations.list_run_citations(user.id, organization.id, run.id)) == 1
    assert len(await conversations.list_run_tool_invocations(user.id, organization.id, run.id)) == 1
