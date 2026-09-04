"""Transactional conversation, message and Run persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.config import RunLimitSettings
from ai_agent.errors import ConflictError, ResourceNotFoundError, RunLimitError
from ai_agent.identity.service import AGENT_USE, IdentityService
from ai_agent.mcp.models import Citation as CitationValue
from ai_agent.mcp.models import ToolInvocationRecord
from ai_agent.persistence.models import (
    AuditLog,
    Citation,
    Conversation,
    Message,
    MessageRole,
    ModelUsage,
    Run,
    RunStatus,
    RunStep,
    ToolInvocation,
)

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
}


class ConversationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identities: IdentityService,
        limits: RunLimitSettings,
    ) -> None:
        self._session_factory = session_factory
        self._identities = identities
        self._limits = limits

    async def create_conversation(
        self, user_id: UUID, organization_id: UUID, title: str
    ) -> Conversation:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session, session.begin():
            conversation = Conversation(
                organization_id=organization_id,
                user_id=user_id,
                title=title,
            )
            session.add(conversation)
            await session.flush()
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="conversation.created",
                    resource_type="conversation",
                    resource_id=str(conversation.id),
                    details={},
                )
            )
            return conversation

    async def list_conversations(
        self, user_id: UUID, organization_id: UUID, *, offset: int, limit: int
    ) -> list[Conversation]:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(Conversation)
                .where(
                    Conversation.organization_id == organization_id,
                    Conversation.user_id == user_id,
                )
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(result)

    async def get_conversation(
        self, user_id: UUID, organization_id: UUID, conversation_id: UUID
    ) -> tuple[Conversation, list[Message]]:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == organization_id,
                    Conversation.user_id == user_id,
                )
            )
            if conversation is None:
                raise ResourceNotFoundError("Conversation not found.")
            messages = list(
                await session.scalars(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.organization_id == organization_id,
                    )
                    .order_by(Message.created_at, Message.id)
                )
            )
            return conversation, messages

    async def create_run(
        self,
        user_id: UUID,
        organization_id: UUID,
        conversation_id: UUID,
        content: str,
        idempotency_key: str | None,
        trace_id: UUID,
    ) -> tuple[Run, bool]:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        if len(content) > self._limits.max_question_characters:
            raise RunLimitError("Question exceeds the configured character limit.")
        async with self._session_factory() as session, session.begin():
            if idempotency_key:
                existing = await session.scalar(
                    select(Run).where(
                        Run.organization_id == organization_id,
                        Run.user_id == user_id,
                        Run.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    return existing, False
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == organization_id,
                    Conversation.user_id == user_id,
                )
                .with_for_update()
            )
            if conversation is None:
                raise ResourceNotFoundError("Conversation not found.")
            active = await session.scalar(
                select(Run.id).where(
                    Run.conversation_id == conversation_id,
                    Run.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
                )
            )
            if active is not None:
                raise ConflictError("Conversation already has an active Run.")
            message = Message(
                organization_id=organization_id,
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content=content,
            )
            session.add(message)
            await session.flush()
            run = Run(
                organization_id=organization_id,
                user_id=user_id,
                conversation_id=conversation_id,
                user_message_id=message.id,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                limits_snapshot=self._limits.model_dump(mode="json"),
            )
            session.add(run)
            conversation.updated_at = datetime.now(UTC)
            await session.flush()
            session.add(
                AuditLog(
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    action="run.queued",
                    resource_type="run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={"conversation_id": str(conversation_id)},
                )
            )
            return run, True

    async def get_run(self, user_id: UUID, organization_id: UUID, run_id: UUID) -> Run:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(Run).where(
                    Run.id == run_id,
                    Run.organization_id == organization_id,
                    Run.user_id == user_id,
                )
            )
            if run is None:
                raise ResourceNotFoundError("Run not found.")
            return run

    async def request_cancel(self, user_id: UUID, organization_id: UUID, run_id: UUID) -> Run:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(Run)
                .where(
                    Run.id == run_id,
                    Run.organization_id == organization_id,
                    Run.user_id == user_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFoundError("Run not found.")
            if run.status not in TERMINAL_RUN_STATUSES and run.cancellation_requested_at is None:
                run.cancellation_requested_at = datetime.now(UTC)
                session.add(
                    AuditLog(
                        organization_id=organization_id,
                        actor_user_id=user_id,
                        action="run.cancellation_requested",
                        resource_type="run",
                        resource_id=str(run.id),
                        trace_id=run.trace_id,
                        details={},
                    )
                )
            return run

    async def claim_run(self, run_id: UUID) -> Run | None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status != RunStatus.QUEUED:
                return None
            now = datetime.now(UTC)
            run.status = RunStatus.RUNNING
            run.started_at = now
            session.add(
                RunStep(
                    run_id=run.id,
                    sequence=1,
                    kind="model",
                    status="running",
                    started_at=now,
                )
            )
            return run

    async def load_run_messages(self, run: Run) -> list[Message]:
        async with self._session_factory() as session:
            user_message = await session.get(Message, run.user_message_id)
            if user_message is None:
                raise ResourceNotFoundError("Run input message not found.")
            result = await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == run.conversation_id,
                    Message.organization_id == run.organization_id,
                    Message.created_at <= user_message.created_at,
                )
                .order_by(Message.created_at, Message.id)
            )
            return list(result)

    async def complete_run(
        self,
        run_id: UUID,
        *,
        answer: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        citations: list[CitationValue] | None = None,
        tool_invocations: list[ToolInvocationRecord] | None = None,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status != RunStatus.RUNNING:
                return False
            now = datetime.now(UTC)
            message = Message(
                organization_id=run.organization_id,
                conversation_id=run.conversation_id,
                role=MessageRole.ASSISTANT,
                content=answer,
            )
            session.add(message)
            await session.flush()
            run.assistant_message_id = message.id
            run.status = RunStatus.COMPLETED
            run.completed_at = now
            step = await session.scalar(
                select(RunStep).where(RunStep.run_id == run.id, RunStep.sequence == 1)
            )
            if step:
                step.status = "completed"
                step.completed_at = now
            for invocation in tool_invocations or []:
                session.add(
                    ToolInvocation(
                        run_id=run.id,
                        organization_id=run.organization_id,
                        user_id=run.user_id,
                        server_code=invocation.server_code,
                        tool_name=invocation.tool_name,
                        status=invocation.status,
                        arguments_digest=invocation.arguments_digest,
                        duration_ms=invocation.duration_ms,
                        error=invocation.error,
                        trace_id=run.trace_id,
                    )
                )
            for citation in _deduplicate_citations(citations or []):
                session.add(
                    Citation(
                        run_id=run.id,
                        organization_id=run.organization_id,
                        source_system=citation.source_system,
                        server_code=citation.server_code,
                        tool_name=citation.tool_name,
                        resource_id=citation.resource_id,
                        queried_at=citation.queried_at,
                        trace_id=run.trace_id,
                        partial=citation.partial,
                    )
                )
            session.add(
                ModelUsage(
                    run_id=run.id,
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            )
            session.add(
                AuditLog(
                    organization_id=run.organization_id,
                    actor_user_id=run.user_id,
                    action="run.completed",
                    resource_type="run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_usd": cost_usd,
                        "tool_calls": len(tool_invocations or []),
                        "citations": len(_deduplicate_citations(citations or [])),
                    },
                )
            )
            return True

    async def list_run_citations(
        self, user_id: UUID, organization_id: UUID, run_id: UUID
    ) -> list[Citation]:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(Run.id).where(
                    Run.id == run_id,
                    Run.organization_id == organization_id,
                    Run.user_id == user_id,
                )
            )
            if run is None:
                raise ResourceNotFoundError("Run not found.")
            return list(
                await session.scalars(
                    select(Citation)
                    .where(
                        Citation.run_id == run_id,
                        Citation.organization_id == organization_id,
                    )
                    .order_by(Citation.created_at, Citation.id)
                )
            )

    async def list_run_tool_invocations(
        self, user_id: UUID, organization_id: UUID, run_id: UUID
    ) -> list[ToolInvocation]:
        await self._identities.access(user_id, organization_id, AGENT_USE)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(Run.id).where(
                    Run.id == run_id,
                    Run.organization_id == organization_id,
                    Run.user_id == user_id,
                )
            )
            if run is None:
                raise ResourceNotFoundError("Run not found.")
            return list(
                await session.scalars(
                    select(ToolInvocation)
                    .where(
                        ToolInvocation.run_id == run_id,
                        ToolInvocation.organization_id == organization_id,
                    )
                    .order_by(ToolInvocation.created_at, ToolInvocation.id)
                )
            )

    async def finish_run_with_error(
        self, run_id: UUID, status: RunStatus, error_code: str, public_message: str
    ) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                return
            now = datetime.now(UTC)
            run.status = status
            run.error_code = error_code
            run.error_message = public_message
            run.completed_at = now
            step = await session.scalar(
                select(RunStep).where(RunStep.run_id == run.id, RunStep.sequence == 1)
            )
            if step:
                step.status = status.value
                step.completed_at = now
            session.add(
                AuditLog(
                    organization_id=run.organization_id,
                    actor_user_id=run.user_id,
                    action=f"run.{status.value}",
                    resource_type="run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={"error_code": error_code},
                )
            )


def _deduplicate_citations(items: list[CitationValue]) -> list[CitationValue]:
    seen: set[tuple[str, str, str, str, bool]] = set()
    result: list[CitationValue] = []
    for item in items:
        key = (
            item.source_system,
            item.server_code,
            item.tool_name,
            item.trace_id,
            item.partial,
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
