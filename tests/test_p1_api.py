from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from ai_agent.errors import AuthorizationError, ConflictError
from ai_agent.identity.service import AGENT_USE, MEMBER_ROLE
from ai_agent.persistence.models import RunStatus
from tests.p1_conftest import PlatformRuntime


async def test_conversation_run_sse_idempotency_and_audit(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    me = await runtime.client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["id"] == str(runtime.user.id)

    conversation_response = await runtime.client.post(
        "/api/v1/conversations",
        headers=runtime.headers,
        json={"title": "P1 test"},
    )
    assert conversation_response.status_code == 201
    conversation_id = conversation_response.json()["id"]
    request_trace = str(uuid4())
    run_response = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={
            **runtime.headers,
            "Idempotency-Key": "p1-test-one",
            "X-Trace-Id": request_trace,
        },
        json={"content": "hello"},
    )
    assert run_response.status_code == 202
    run_id = run_response.json()["id"]
    assert run_response.json()["trace_id"] == request_trace

    completed = await _wait_for_terminal(runtime, run_id)
    assert completed["status"] == RunStatus.COMPLETED.value

    recent_runs = await runtime.client.get(
        f"/api/v1/conversations/{conversation_id}/runs?limit=1",
        headers=runtime.headers,
    )
    assert recent_runs.status_code == 200
    assert [item["id"] for item in recent_runs.json()] == [run_id]

    events = await runtime.client.get(
        f"/api/v1/runs/{run_id}/events",
        headers=runtime.headers,
    )
    assert events.status_code == 200
    assert "event: message.delta" in events.text
    assert "event: run.completed" in events.text
    assert "Echo: hello" in events.text

    conversation = await runtime.client.get(
        f"/api/v1/conversations/{conversation_id}", headers=runtime.headers
    )
    assert [item["role"] for item in conversation.json()["messages"]] == ["user", "assistant"]
    assert conversation.json()["messages"][-1]["content"] == "Echo: hello"

    duplicate = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={**runtime.headers, "Idempotency-Key": "p1-test-one"},
        json={"content": "hello"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == run_id

    audit = await runtime.client.get("/api/v1/admin/audit-logs", headers=runtime.headers)
    assert audit.status_code == 200
    assert {item["action"] for item in audit.json()} >= {
        "conversation.created",
        "run.queued",
        "run.completed",
    }


async def test_csrf_global_switch_and_question_limit(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    rejected = await runtime.client.post(
        "/api/v1/conversations",
        headers={"X-Organization-Id": str(runtime.organization.id)},
        json={"title": "No CSRF"},
    )
    assert rejected.status_code == 403

    conversation = await runtime.client.post(
        "/api/v1/conversations", headers=runtime.headers, json={"title": "Limits"}
    )
    conversation_id = conversation.json()["id"]
    too_long = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=runtime.headers,
        json={"content": "x" * (runtime.settings.limits.max_question_characters + 1)},
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "run_limit"

    runtime.settings.platform.runs_enabled = False
    disabled = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=runtime.headers,
        json={"content": "blocked"},
    )
    assert disabled.status_code == 503


async def test_p3_status_exposes_readonly_closure(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    runtime.settings.permission_system.enabled = True
    runtime.settings.mcp_gateway.enabled = True

    response = await runtime.client.get("/api/v1/p3/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["phase"] == "P3"
    assert payload["enabled"] is True
    assert "query_dataset" in payload["expected_tools"]


async def test_run_can_be_cancelled(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    runtime.provider.gate = asyncio.Event()
    conversation = await runtime.client.post(
        "/api/v1/conversations", headers=runtime.headers, json={"title": "Cancel"}
    )
    run_response = await runtime.client.post(
        f"/api/v1/conversations/{conversation.json()['id']}/messages",
        headers=runtime.headers,
        json={"content": "wait"},
    )
    run_id = run_response.json()["id"]
    await asyncio.sleep(0)
    cancelled = await runtime.client.post(f"/api/v1/runs/{run_id}/cancel", headers=runtime.headers)
    assert cancelled.status_code == 200
    runtime.provider.gate.set()
    terminal = await _wait_for_terminal(runtime, run_id)
    assert terminal["status"] == RunStatus.CANCELLED.value


async def test_rbac_and_organization_isolation(platform_runtime: PlatformRuntime) -> None:
    runtime = platform_runtime
    identities = runtime.services.identities
    second = await identities.upsert_oidc_user(
        issuer=runtime.settings.oidc.issuer,
        subject="member-subject",
        display_name="Member",
        email=None,
    )
    member = await identities.add_member(runtime.user.id, runtime.organization.id, second.id)
    access = await identities.access(second.id, runtime.organization.id, AGENT_USE)
    assert AGENT_USE in access.permissions
    with pytest.raises(AuthorizationError):
        await identities.list_members(second.id, runtime.organization.id)

    members = await identities.list_members(runtime.user.id, runtime.organization.id)
    owner_member = next(item for item, user, _ in members if user.id == runtime.user.id)
    with pytest.raises(ConflictError, match="at least one administrator"):
        await identities.set_member_roles(
            runtime.user.id,
            runtime.organization.id,
            owner_member.id,
            {MEMBER_ROLE},
        )
    assert member.user_id == second.id

    isolated = await identities.create_organization(second.id, "Second Organization")
    denied = await runtime.client.get(
        "/api/v1/conversations",
        headers={"X-Organization-Id": str(isolated.id)},
    )
    assert denied.status_code == 403


async def _wait_for_terminal(runtime: PlatformRuntime, run_id: str) -> dict[str, object]:
    for _ in range(100):
        response = await runtime.client.get(f"/api/v1/runs/{run_id}", headers=runtime.headers)
        payload = response.json()
        if payload["status"] in {item.value for item in RunStatus if item != RunStatus.RUNNING} - {
            RunStatus.QUEUED.value
        }:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run {UUID(run_id)} did not reach a terminal status")
