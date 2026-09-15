from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_agent.agents.control import AgentControlService, AgentVersionConfig
from ai_agent.agents.single_agent import AgentResult, SingleAgent
from ai_agent.config import AgentControlMode, Environment
from ai_agent.errors import AuthorizationError, ConflictError
from ai_agent.mcp.models import (
    RunContext,
    ToolDefinition,
    ToolInvocationRecord,
    ToolResult,
)
from ai_agent.models import (
    ModelMessage,
    ModelStreamEvent,
    ModelToolCall,
    ModelUsageResult,
)
from ai_agent.persistence.models import (
    AgentReleaseAction,
    AgentVersion,
    AuditLog,
    Run,
    RunStatus,
)
from tests.p1_conftest import PlatformRuntime


async def test_agent_version_release_rollback_and_run_binding(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    created = await runtime.client.post(
        "/api/v1/agents",
        headers=runtime.headers,
        json={"code": "support-agent", "display_name": "Support Agent"},
    )
    assert created.status_code == 201
    agent_id = UUID(created.json()["id"])
    assert created.json()["is_default"] is True

    draft = await runtime.client.get(
        f"/api/v1/agents/{agent_id}/draft", headers=runtime.headers
    )
    assert draft.status_code == 200
    managed_config = {
        **draft.json()["config"],
        "system_prompt": "You are the versioned support assistant.",
        "allowed_tools": [],
        "limits": {
            **draft.json()["config"]["limits"],
            "max_output_tokens": 64,
        },
    }
    updated = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers={**runtime.headers, "If-Match": '"1"'},
        json={"config": managed_config},
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    stale = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers={**runtime.headers, "If-Match": "1"},
        json={"config": managed_config},
    )
    assert stale.status_code == 409

    version_one = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=runtime.headers,
        json={"expected_revision": 2},
    )
    assert version_one.status_code == 201
    version_one_id = UUID(version_one.json()["id"])
    assert len(version_one.json()["config_digest"]) == 64

    release_one = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/releases",
        headers={**runtime.headers, "Idempotency-Key": "release-support-v1"},
        json={
            "version_id": str(version_one_id),
            "reason": "Initial development release",
            "expected_generation": 0,
        },
    )
    assert release_one.status_code == 200
    repeated = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/releases",
        headers={**runtime.headers, "Idempotency-Key": "release-support-v1"},
        json={
            "version_id": str(version_one_id),
            "reason": "Initial development release",
            "expected_generation": 0,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["release"]["id"] == release_one.json()["release"]["id"]
    assert repeated.json()["deployment"]["generation"] == 1
    mismatched_replay = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/releases",
        headers={**runtime.headers, "Idempotency-Key": "release-support-v1"},
        json={
            "version_id": str(version_one_id),
            "reason": "Different release request",
            "expected_generation": 0,
        },
    )
    assert mismatched_replay.status_code == 409

    service = runtime.services.agent_control
    assert service is not None
    monkeypatch.setattr(service, "_mode", AgentControlMode.MANAGED_OPTIONAL)
    conversation = await runtime.client.post(
        "/api/v1/conversations",
        headers=runtime.headers,
        json={"title": "Versioned Agent"},
    )
    run_response = await runtime.client.post(
        f"/api/v1/conversations/{conversation.json()['id']}/messages",
        headers=runtime.headers,
        json={"content": "use the deployed version"},
    )
    assert run_response.status_code == 202
    assert UUID(run_response.json()["agent_id"]) == agent_id
    assert UUID(run_response.json()["agent_version_id"]) == version_one_id
    run_id = UUID(run_response.json()["id"])
    run = await _wait_for_terminal_run(runtime, run_id)
    assert run.status == RunStatus.COMPLETED
    assert runtime.provider.last_messages[0].content == managed_config["system_prompt"]
    assert runtime.provider.last_max_output_tokens == 64

    next_config = {**managed_config, "system_prompt": "Version two support assistant."}
    updated_again = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/draft",
        headers={**runtime.headers, "If-Match": "2"},
        json={"config": next_config},
    )
    assert updated_again.status_code == 200
    version_two = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=runtime.headers,
        json={"expected_revision": 3},
    )
    version_two_id = UUID(version_two.json()["id"])
    release_two = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/releases",
        headers={**runtime.headers, "Idempotency-Key": "release-support-v2"},
        json={
            "version_id": str(version_two_id),
            "reason": "Second development release",
            "expected_generation": 1,
        },
    )
    assert release_two.json()["deployment"]["generation"] == 2
    rollback = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/rollback",
        headers={**runtime.headers, "Idempotency-Key": "rollback-support-v1"},
        json={
            "version_id": str(version_one_id),
            "reason": "Regression detected",
            "expected_generation": 2,
        },
    )
    assert rollback.status_code == 200
    assert rollback.json()["deployment"]["version_id"] == str(version_one_id)
    assert rollback.json()["deployment"]["generation"] == 3

    versions = await runtime.client.get(
        f"/api/v1/agents/{agent_id}/versions", headers=runtime.headers
    )
    version_one_after_changes = next(
        item for item in versions.json() if item["id"] == str(version_one_id)
    )
    assert version_one_after_changes["config"]["system_prompt"] == managed_config[
        "system_prompt"
    ]


async def test_managed_agent_is_tenant_scoped_and_required_mode_fails_closed(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    first = await runtime.client.post(
        "/api/v1/agents",
        headers=runtime.headers,
        json={"code": "tenant-agent", "display_name": "Tenant Agent"},
    )
    agent_id = first.json()["id"]
    second_org = await runtime.services.identities.create_organization(
        runtime.user.id, "Second Agent Tenant"
    )
    second_headers = {
        "X-Organization-Id": str(second_org.id),
        "X-CSRF-Token": runtime.csrf_token,
    }
    hidden = await runtime.client.get(
        f"/api/v1/agents/{agent_id}/versions", headers=second_headers
    )
    assert hidden.status_code == 404

    service = runtime.services.agent_control
    assert service is not None
    monkeypatch.setattr(service, "_mode", AgentControlMode.MANAGED_REQUIRED)
    conversation = await runtime.client.post(
        "/api/v1/conversations",
        headers=second_headers,
        json={"title": "No deployment"},
    )
    rejected = await runtime.client.post(
        f"/api/v1/conversations/{conversation.json()['id']}/messages",
        headers=second_headers,
        json={"content": "must fail closed"},
    )
    assert rejected.status_code == 409


async def test_default_agent_switch_keeps_exactly_one_default(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    first = await runtime.client.post(
        "/api/v1/agents",
        headers=runtime.headers,
        json={"code": "first-default", "display_name": "First Default"},
    )
    second = await runtime.client.post(
        "/api/v1/agents",
        headers=runtime.headers,
        json={"code": "second-default", "display_name": "Second Default"},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    switched = await runtime.client.post(
        f"/api/v1/agents/{second.json()['id']}/default",
        headers=runtime.headers,
    )
    assert switched.status_code == 200
    assert switched.json()["is_default"] is True

    listed = await runtime.client.get("/api/v1/agents", headers=runtime.headers)
    defaults = [item for item in listed.json() if item["is_default"]]
    assert [item["id"] for item in defaults] == [second.json()["id"]]


async def test_rollback_rejects_version_that_was_never_deployed(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    agent_id, _ = await _create_released_agent(runtime, code="rollback-history-agent")
    service = runtime.services.agent_control
    assert service is not None
    draft = await service.get_draft(
        runtime.user.id,
        runtime.organization.id,
        agent_id,
    )
    next_config = AgentVersionConfig.model_validate(
        {**draft.config, "system_prompt": "An unpublished rollback target."}
    )
    updated = await service.update_draft(
        runtime.user.id,
        runtime.organization.id,
        agent_id,
        expected_revision=draft.revision,
        config=next_config,
    )
    unpublished = await service.create_version(
        runtime.user.id,
        runtime.organization.id,
        agent_id,
        expected_revision=updated.revision,
    )

    with pytest.raises(ConflictError, match="not previously deployed"):
        await service.release(
            runtime.user.id,
            runtime.organization.id,
            agent_id,
            unpublished.id,
            action=AgentReleaseAction.ROLLBACK,
            reason="Invalid rollback target",
            idempotency_key="rollback-unpublished-version",
            expected_generation=1,
            bypass_gate=False,
        )


async def test_production_release_requires_explicit_audited_bypass(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    service = runtime.services.agent_control
    assert service is not None
    agent, draft = await service.create_agent(
        runtime.user.id,
        runtime.organization.id,
        code="production-gate",
        display_name="Production Gate",
    )
    version = await service.create_version(
        runtime.user.id,
        runtime.organization.id,
        agent.id,
        expected_revision=draft.revision,
    )
    production = AgentControlService(
        runtime.services.database.session_factory,
        runtime.services.identities,
        runtime.settings.limits,
        runtime.settings.model.system_prompt,
        mode=AgentControlMode.MANAGED_OPTIONAL,
        environment=Environment.PRODUCTION,
        audit=runtime.services.audit,
    )
    try:
        await production.release(
            runtime.user.id,
            runtime.organization.id,
            agent.id,
            version.id,
            action=AgentReleaseAction.DEPLOY,
            reason="No evaluation yet",
            idempotency_key="production-without-gate",
            expected_generation=0,
            bypass_gate=False,
        )
    except ConflictError:
        pass
    else:
        raise AssertionError("Production release bypassed the missing evaluation gate.")

    release, deployment = await production.release(
        runtime.user.id,
        runtime.organization.id,
        agent.id,
        version.id,
        action=AgentReleaseAction.DEPLOY,
        reason="Emergency development validation",
        idempotency_key="production-audited-bypass",
        expected_generation=0,
        bypass_gate=True,
    )
    assert release.bypassed_gate is True
    assert deployment.generation == 1
    async with runtime.services.database.session_factory() as session:
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.resource_id == str(release.id),
                AuditLog.action == "agent.deploy",
            )
        )
    assert audit is not None
    assert audit.details["bypassed_gate"] is True


async def test_standard_member_cannot_manage_agent_versions(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    member = await runtime.services.identities.upsert_oidc_user(
        issuer="https://identity.example.test/agent",
        subject="agent-standard-member",
        display_name="Agent Standard Member",
        email=None,
    )
    await runtime.services.identities.add_member(
        runtime.user.id, runtime.organization.id, member.id
    )
    service = runtime.services.agent_control
    assert service is not None

    with pytest.raises(AuthorizationError):
        await service.create_agent(
            member.id,
            runtime.organization.id,
            code="unauthorized-agent",
            display_name="Unauthorized Agent",
        )


async def test_tampered_version_snapshot_fails_run_closed(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    _, version_id = await _create_released_agent(runtime, code="integrity-agent")
    service = runtime.services.agent_control
    assert service is not None
    monkeypatch.setattr(service, "_mode", AgentControlMode.MANAGED_REQUIRED)
    run = await _create_managed_run(runtime, "detect a modified version")

    async with runtime.services.database.session_factory() as session, session.begin():
        version = await session.get(AgentVersion, version_id)
        assert version is not None
        version.config_snapshot = {
            **version.config_snapshot,
            "system_prompt": "This snapshot was modified after publication.",
        }

    await runtime.services.executor.execute(run.id)
    failed = await _load_run(runtime, run.id)
    assert failed.status == RunStatus.FAILED
    assert failed.error_code == "agent_policy_failed"


async def test_managed_tool_allowlist_can_only_narrow_platform_allowlist(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    await _create_released_agent(
        runtime,
        code="tool-policy-agent",
        allowed_tools=("shared_query", "managed_only"),
    )
    service = runtime.services.agent_control
    assert service is not None
    monkeypatch.setattr(service, "_mode", AgentControlMode.MANAGED_REQUIRED)
    monkeypatch.setattr(
        runtime.services.executor,
        "_tool_allowlist",
        frozenset({"shared_query", "platform_only"}),
    )
    observed_allowlists: list[frozenset[str] | None] = []

    async def run_agent(
        messages: list[ModelMessage],
        **kwargs: Any,
    ) -> AgentResult:
        del messages
        observed_allowlists.append(kwargs["tool_allowlist"])
        return AgentResult("allowed", ModelUsageResult(input_tokens=20, output_tokens=5))

    monkeypatch.setattr(runtime.services.executor._agent, "run", run_agent)
    run = await _create_managed_run(runtime, "use an allowed tool")
    await runtime.services.executor.execute(run.id)

    completed = await _load_run(runtime, run.id)
    assert completed.status == RunStatus.COMPLETED
    assert observed_allowlists == [frozenset({"shared_query"})]


async def test_required_citation_policy_fails_when_tool_evidence_is_missing(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    await _create_released_agent(
        runtime,
        code="citation-policy-agent",
        allowed_tools=("safe_query",),
        citation_policy="required_if_tools_used",
    )
    service = runtime.services.agent_control
    assert service is not None
    monkeypatch.setattr(service, "_mode", AgentControlMode.MANAGED_REQUIRED)

    async def run_agent(
        messages: list[ModelMessage],
        **kwargs: Any,
    ) -> AgentResult:
        del messages, kwargs
        invocation = ToolInvocationRecord(
            tool_name="safe_query",
            server_code="permission",
            status="succeeded",
            arguments_digest="0" * 64,
            trace_id="citation-policy-test",
        )
        return AgentResult(
            "answer without evidence",
            ModelUsageResult(input_tokens=20, output_tokens=5),
            citations=[],
            tool_invocations=[invocation],
        )

    monkeypatch.setattr(runtime.services.executor._agent, "run", run_agent)
    run = await _create_managed_run(runtime, "require source evidence")
    await runtime.services.executor.execute(run.id)

    failed = await _load_run(runtime, run.id)
    assert failed.status == RunStatus.FAILED
    assert failed.error_code == "agent_policy_failed"


async def test_tool_call_without_citation_still_records_invocation() -> None:
    result = await SingleAgent(_MissingCitationProvider()).run(
        [ModelMessage(role="user", content="query")],
        max_output_tokens=100,
        trace_id="missing-citation-test",
        on_delta=_discard_delta,
        gateway=_MissingCitationGateway(),
        context=RunContext(
            organization_id=uuid4(),
            user_id=uuid4(),
            trace_id="missing-citation-test",
        ),
        tool_allowlist=frozenset({"safe_query"}),
    )

    assert result.citations == []
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0].tool_name == "safe_query"
    assert result.tool_invocations[0].server_code == "permission"


class _MissingCitationProvider:
    provider_name = "missing-citation"
    model_name = "missing-citation-model"

    def conservative_input_tokens(self, messages: list[ModelMessage]) -> int:
        return sum(len(item.content) for item in messages)

    async def stream(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        del max_output_tokens, trace_id
        if messages[-1].role != "tool":
            assert tools is not None
            yield ModelStreamEvent(
                tool_calls=(ModelToolCall(id="call-1", name="safe_query", arguments={}),),
                usage=ModelUsageResult(input_tokens=10, output_tokens=1),
            )
            return
        yield ModelStreamEvent(
            delta="answer without citation",
            usage=ModelUsageResult(input_tokens=10, output_tokens=2),
        )


class _MissingCitationGateway:
    async def list_tools(
        self,
        context: RunContext,
        tool_set: str = "",
    ) -> list[ToolDefinition]:
        del context, tool_set
        return [
            ToolDefinition(
                name="safe_query",
                input_schema={"type": "object", "properties": {}},
                server_code="permission",
            )
        ]

    async def call(
        self,
        context: RunContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        del arguments
        return ToolResult(
            name=tool_name,
            structured_content={"value": "unattributed"},
            trace_id=context.trace_id,
        )


async def _discard_delta(delta: str) -> None:
    del delta


async def _create_released_agent(
    runtime: PlatformRuntime,
    *,
    code: str,
    allowed_tools: tuple[str, ...] = (),
    citation_policy: str = "optional",
) -> tuple[UUID, UUID]:
    service = runtime.services.agent_control
    assert service is not None
    config = AgentVersionConfig(
        system_prompt=f"System prompt for {code}.",
        allowed_tools=allowed_tools,
        limits=runtime.settings.limits,
        citation_policy=citation_policy,
    )
    agent, draft = await service.create_agent(
        runtime.user.id,
        runtime.organization.id,
        code=code,
        display_name=code,
        config=config,
    )
    version = await service.create_version(
        runtime.user.id,
        runtime.organization.id,
        agent.id,
        expected_revision=draft.revision,
    )
    await service.release(
        runtime.user.id,
        runtime.organization.id,
        agent.id,
        version.id,
        action=AgentReleaseAction.DEPLOY,
        reason="Development test release",
        idempotency_key=f"release-{code}",
        expected_generation=0,
        bypass_gate=False,
    )
    return agent.id, version.id


async def _create_managed_run(runtime: PlatformRuntime, content: str) -> Run:
    conversation = await runtime.services.conversations.create_conversation(
        runtime.user.id,
        runtime.organization.id,
        content,
    )
    run, created = await runtime.services.conversations.create_run(
        runtime.user.id,
        runtime.organization.id,
        conversation.id,
        content,
        None,
        uuid4(),
    )
    assert created is True
    return run


async def _load_run(runtime: PlatformRuntime, run_id: UUID) -> Run:
    async with runtime.services.database.session_factory() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    return run


async def _wait_for_terminal_run(runtime: PlatformRuntime, run_id: UUID) -> Run:
    for _ in range(100):
        async with runtime.services.database.session_factory() as session:
            run = await session.get(Run, run_id)
        assert run is not None
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("Managed Agent Run did not complete.")
