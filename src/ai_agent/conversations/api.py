"""Conversation, message, Run, cancellation and SSE APIs."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_agent.conversations.service import TERMINAL_RUN_STATUSES
from ai_agent.identity.dependencies import (
    CurrentIdentity,
    OrganizationId,
    require_csrf,
)
from ai_agent.persistence.models import Conversation, Message, Run, RunStatus

router = APIRouter(prefix="/api/v1")
_EVENT_ID = re.compile(r"^[0-9]+-[0-9]+$")
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
LastEventId = Annotated[str | None, Header(alias="Last-Event-ID")]


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="New conversation", min_length=1, max_length=200)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)


class MessageView(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime


class ConversationView(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationView):
    messages: list[MessageView]


class CitationView(BaseModel):
    id: UUID
    source_system: str
    server_code: str
    tool_name: str
    resource_id: str | None
    queried_at: datetime
    trace_id: UUID
    partial: bool
    created_at: datetime


class RunView(BaseModel):
    id: UUID
    conversation_id: UUID
    status: str
    trace_id: UUID
    error_code: str | None
    error_message: str | None
    cancellation_requested_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    events_url: str
    citations: list[CitationView] = Field(default_factory=list)


class ToolInvocationView(BaseModel):
    id: UUID
    server_code: str
    tool_name: str
    status: str
    arguments_digest: str
    duration_ms: float | None
    error: str | None
    trace_id: UUID
    created_at: datetime


@router.get("/conversations", response_model=list[ConversationView], tags=["conversations"])
async def list_conversations(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    offset: Offset = 0,
    limit: Limit = 50,
) -> list[ConversationView]:
    conversations = await request.app.state.services.conversations.list_conversations(
        identity.session.user_id,
        organization_id,
        offset=offset,
        limit=limit,
    )
    return [_conversation_view(item) for item in conversations]


@router.post(
    "/conversations",
    response_model=ConversationView,
    status_code=status.HTTP_201_CREATED,
    tags=["conversations"],
)
async def create_conversation(
    payload: ConversationCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> ConversationView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    conversation = await request.app.state.services.conversations.create_conversation(
        identity.session.user_id,
        organization_id,
        payload.title.strip(),
    )
    return _conversation_view(conversation)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    tags=["conversations"],
)
async def get_conversation(
    conversation_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> ConversationDetail:
    conversation, messages = await request.app.state.services.conversations.get_conversation(
        identity.session.user_id, organization_id, conversation_id
    )
    return ConversationDetail(
        **_conversation_view(conversation).model_dump(),
        messages=[_message_view(item) for item in messages],
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
async def create_message_run(
    conversation_id: UUID,
    payload: MessageCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    idempotency_key: IdempotencyKey = None,
) -> RunView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    settings = request.app.state.settings
    if not settings.platform.runs_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Runs are disabled by the global switch.",
        )
    services = request.app.state.services
    if services.quota is not None and not await services.quota.runs_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Runs are disabled by the operational switch.",
        )
    if not services.executor.accepting:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Runs are draining for service shutdown.",
        )
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain between 1 and 200 characters.",
        )
    content = payload.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Message content must not be blank.",
        )
    quota_lease = None
    if services.quota is not None:
        quota_lease = await services.quota.acquire(
            organization_id,
            identity.session.user_id,
            str(request.state.trace_id),
        )
    try:
        run, created = await services.conversations.create_run(
            identity.session.user_id,
            organization_id,
            conversation_id,
            content,
            idempotency_key,
            request.state.trace_id,
        )
    except Exception:
        if services.quota is not None and quota_lease is not None:
            await services.quota.rollback(quota_lease)
        raise
    if created:
        await services.events.publish(
            run.id,
            "run.queued",
            {"trace_id": str(run.trace_id)},
        )
        try:
            services.executor.submit(run.id, quota_lease)
        except RuntimeError as exc:
            if services.quota is not None and quota_lease is not None:
                await services.quota.rollback(quota_lease)
            await services.conversations.finish_run_with_error(
                run.id,
                RunStatus.FAILED,
                "service_draining",
                "Run was rejected because the service is draining.",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent Runs are draining for service shutdown.",
            ) from exc
    elif services.quota is not None and quota_lease is not None:
        await services.quota.rollback(quota_lease)
    return _run_view(run)


@router.get("/runs/{run_id}", response_model=RunView, tags=["runs"])
async def get_run(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> RunView:
    run = await request.app.state.services.conversations.get_run(
        identity.session.user_id, organization_id, run_id
    )
    citations = await request.app.state.services.conversations.list_run_citations(
        identity.session.user_id, organization_id, run_id
    )
    return _run_view(run, citations)


@router.post("/runs/{run_id}/cancel", response_model=RunView, tags=["runs"])
async def cancel_run(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> RunView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    run = await request.app.state.services.conversations.request_cancel(
        identity.session.user_id, organization_id, run_id
    )
    if run.status not in TERMINAL_RUN_STATUSES:
        await request.app.state.services.control.request_cancel(run.id)
        await request.app.state.services.events.publish(
            run.id,
            "run.cancellation_requested",
            {"trace_id": str(run.trace_id)},
        )
    return _run_view(run)


@router.get("/runs/{run_id}/citations", response_model=list[CitationView], tags=["runs"])
async def list_run_citations(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[CitationView]:
    citations = await request.app.state.services.conversations.list_run_citations(
        identity.session.user_id, organization_id, run_id
    )
    return [CitationView.model_validate(item) for item in citations]


@router.get(
    "/runs/{run_id}/tool-invocations",
    response_model=list[ToolInvocationView],
    tags=["runs"],
)
async def list_run_tool_invocations(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[ToolInvocationView]:
    invocations = await request.app.state.services.conversations.list_run_tool_invocations(
        identity.session.user_id, organization_id, run_id
    )
    return [ToolInvocationView.model_validate(item) for item in invocations]


@router.get("/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    run = await request.app.state.services.conversations.get_run(
        identity.session.user_id, organization_id, run_id
    )
    cursor = last_event_id or "0-0"
    if not _EVENT_ID.fullmatch(cursor):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID is invalid.",
        )
    return StreamingResponse(
        _event_stream(request, run, cursor, identity.session.user_id, organization_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_stream(
    request: Request,
    initial_run: Run,
    cursor: str,
    user_id: UUID,
    organization_id: UUID,
) -> AsyncIterator[str]:
    run = initial_run
    while True:
        if await request.is_disconnected():
            return
        events = await request.app.state.services.events.read(run.id, cursor, block_ms=15_000)
        if events:
            for event in events:
                cursor = event.event_id
                payload = json.dumps(event.payload, separators=(",", ":"), ensure_ascii=False)
                yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {payload}\n\n"
                if event.terminal:
                    return
            continue
        run = await request.app.state.services.conversations.get_run(
            user_id, organization_id, run.id
        )
        if run.status in TERMINAL_RUN_STATUSES:
            payload = json.dumps(
                {
                    "trace_id": str(run.trace_id),
                    "status": run.status.value,
                    "error_code": run.error_code,
                    "message": run.error_message,
                },
                separators=(",", ":"),
            )
            yield f"event: run.snapshot\ndata: {payload}\n\n"
            return
        yield ": heartbeat\n\n"


def _conversation_view(item: Conversation) -> ConversationView:
    return ConversationView(
        id=item.id,
        title=item.title,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _message_view(item: Message) -> MessageView:
    return MessageView(
        id=item.id,
        role=item.role.value,
        content=item.content,
        created_at=item.created_at,
    )


def _run_view(run: Run, citations: list[object] | None = None) -> RunView:
    return RunView(
        id=run.id,
        conversation_id=run.conversation_id,
        status=run.status.value,
        trace_id=run.trace_id,
        error_code=run.error_code,
        error_message=run.error_message,
        cancellation_requested_at=run.cancellation_requested_at,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        events_url=f"/api/v1/runs/{run.id}/events",
        citations=[CitationView.model_validate(item) for item in citations or []],
    )
