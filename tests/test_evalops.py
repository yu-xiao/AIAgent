from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from ai_agent.config import EvaluationGateMode
from ai_agent.errors import AuthorizationError
from ai_agent.persistence.models import EvaluationDatasetVersion, EvaluationRunStatus
from tests.p1_conftest import PlatformRuntime


async def test_evalops_versioned_dataset_gate_and_stale_policy(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    agent_id, agent_version_id = await _create_agent_version(runtime, "eval-gate-agent")
    dataset_id, dataset_version_id = await _create_dataset_version(
        runtime,
        code="release-regression",
        cases=[_case("basic-answer")],
    )
    policy = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/evaluation-policy",
        headers={**runtime.headers, "If-Match": "0"},
        json={
            "dataset_version_id": str(dataset_version_id),
            "min_pass_rate": 1,
            "max_critical_failures": 0,
        },
    )
    assert policy.status_code == 200
    assert policy.json()["revision"] == 1

    control = runtime.services.agent_control
    assert control is not None
    monkeypatch.setattr(control, "_evaluation_gate_mode", EvaluationGateMode.REQUIRED)
    blocked = await _release(
        runtime,
        agent_id,
        agent_version_id,
        key="release-without-evaluation",
    )
    assert blocked.status_code == 409

    created = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/evaluations",
        headers={**runtime.headers, "Idempotency-Key": "evaluate-agent-v1"},
        json={"agent_version_id": str(agent_version_id)},
    )
    assert created.status_code == 202
    evaluation_run_id = UUID(created.json()["id"])
    repeated = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/evaluations",
        headers={**runtime.headers, "Idempotency-Key": "evaluate-agent-v1"},
        json={"agent_version_id": str(agent_version_id)},
    )
    assert repeated.status_code == 202
    assert repeated.json()["id"] == str(evaluation_run_id)

    completed = await _wait_for_evaluation(runtime, evaluation_run_id)
    assert completed["status"] == "completed"
    assert completed["gate_passed"] is True
    assert completed["pass_rate"] == 1
    assert completed["results"][0]["reason"] == "ok"
    assert completed["results"][0]["answer_digest"] is not None
    assert "answer" not in completed["results"][0]

    changed_suite = {
        "schema_version": 1,
        "cases": [_case("changed-case", question="changed question")],
    }
    updated = await runtime.client.put(
        f"/api/v1/evaluations/datasets/{dataset_id}/draft",
        headers={**runtime.headers, "If-Match": "1"},
        json={"suite": changed_suite},
    )
    assert updated.status_code == 200
    stale_draft = await runtime.client.put(
        f"/api/v1/evaluations/datasets/{dataset_id}/draft",
        headers={**runtime.headers, "If-Match": "1"},
        json={"suite": changed_suite},
    )
    assert stale_draft.status_code == 409
    versions = await runtime.client.get(
        f"/api/v1/evaluations/datasets/{dataset_id}/versions",
        headers=runtime.headers,
    )
    assert versions.json()[0]["suite"]["cases"][0]["key"] == "basic-answer"

    changed_policy = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/evaluation-policy",
        headers={**runtime.headers, "If-Match": "1"},
        json={
            "dataset_version_id": str(dataset_version_id),
            "min_pass_rate": 0.9,
            "max_critical_failures": 0,
        },
    )
    assert changed_policy.status_code == 200
    stale_gate = await _release(
        runtime,
        agent_id,
        agent_version_id,
        key="release-with-stale-evaluation",
        evaluation_run_id=evaluation_run_id,
    )
    assert stale_gate.status_code == 409

    fresh = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/evaluations",
        headers={**runtime.headers, "Idempotency-Key": "evaluate-agent-v1-policy-v2"},
        json={"agent_version_id": str(agent_version_id)},
    )
    fresh_run_id = UUID(fresh.json()["id"])
    fresh_result = await _wait_for_evaluation(runtime, fresh_run_id)
    assert fresh_result["gate_passed"] is True
    released = await _release(
        runtime,
        agent_id,
        agent_version_id,
        key="release-with-passed-evaluation",
        evaluation_run_id=fresh_run_id,
    )
    assert released.status_code == 200
    assert released.json()["release"]["evaluation_run_id"] == str(fresh_run_id)
    assert released.json()["release"]["gate_decision"] == "passed"
    assert released.json()["release"]["gate_policy_digest"] == changed_policy.json()[
        "policy_digest"
    ]


async def test_failed_critical_evaluation_is_advisory_until_gate_required(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    agent_id, agent_version_id = await _create_agent_version(runtime, "advisory-agent")
    _, dataset_version_id = await _create_dataset_version(
        runtime,
        code="critical-regression",
        cases=[
            _case(
                "missing-tool",
                expected_tools=["query_dataset"],
                severity="critical",
            )
        ],
    )
    policy = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/evaluation-policy",
        headers={**runtime.headers, "If-Match": "0"},
        json={
            "dataset_version_id": str(dataset_version_id),
            "min_pass_rate": 0,
            "max_critical_failures": 0,
        },
    )
    assert policy.status_code == 200
    evaluation = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/evaluations",
        headers={**runtime.headers, "Idempotency-Key": "critical-evaluation"},
        json={"agent_version_id": str(agent_version_id)},
    )
    evaluation_id = UUID(evaluation.json()["id"])
    result = await _wait_for_evaluation(runtime, evaluation_id)
    assert result["gate_passed"] is False
    assert result["critical_failures"] == 1
    assert result["results"][0]["reason"] == "wrong_tool"

    control = runtime.services.agent_control
    assert control is not None
    monkeypatch.setattr(control, "_evaluation_gate_mode", EvaluationGateMode.REQUIRED)
    blocked = await _release(
        runtime,
        agent_id,
        agent_version_id,
        key="required-failed-evaluation",
        evaluation_run_id=evaluation_id,
    )
    assert blocked.status_code == 409

    monkeypatch.setattr(control, "_evaluation_gate_mode", EvaluationGateMode.ADVISORY)
    advisory = await _release(
        runtime,
        agent_id,
        agent_version_id,
        key="advisory-failed-evaluation",
        evaluation_run_id=evaluation_id,
    )
    assert advisory.status_code == 200
    assert advisory.json()["release"]["gate_decision"] == "failed"


async def test_evaluation_run_is_tenant_scoped_and_cancellable(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    agent_id, agent_version_id = await _create_agent_version(runtime, "cancel-eval-agent")
    _, dataset_version_id = await _create_dataset_version(
        runtime,
        code="cancel-suite",
        cases=[_case("slow-case")],
    )
    await runtime.client.put(
        f"/api/v1/agents/{agent_id}/evaluation-policy",
        headers={**runtime.headers, "If-Match": "0"},
        json={
            "dataset_version_id": str(dataset_version_id),
            "min_pass_rate": 1,
            "max_critical_failures": 0,
        },
    )
    runtime.provider.gate = asyncio.Event()
    created = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/evaluations",
        headers={**runtime.headers, "Idempotency-Key": "cancel-evaluation"},
        json={"agent_version_id": str(agent_version_id)},
    )
    run_id = UUID(created.json()["id"])
    for _ in range(100):
        current = await runtime.client.get(
            f"/api/v1/evaluations/runs/{run_id}", headers=runtime.headers
        )
        if current.json()["status"] == "running":
            break
        await asyncio.sleep(0.01)
    cancelled = await runtime.client.post(
        f"/api/v1/evaluations/runs/{run_id}/cancel",
        headers=runtime.headers,
    )
    assert cancelled.status_code == 200
    runtime.provider.gate.set()
    final = await _wait_for_evaluation(runtime, run_id)
    assert final["status"] == "cancelled"

    second_org = await runtime.services.identities.create_organization(
        runtime.user.id, "Other Eval Tenant"
    )
    hidden = await runtime.client.get(
        f"/api/v1/evaluations/runs/{run_id}",
        headers={
            "X-Organization-Id": str(second_org.id),
            "X-CSRF-Token": runtime.csrf_token,
        },
    )
    assert hidden.status_code == 404


async def test_standard_member_cannot_manage_evaluation_dataset(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    member = await runtime.services.identities.upsert_oidc_user(
        issuer="https://identity.example.test/agent",
        subject="eval-standard-member",
        display_name="Eval Standard Member",
        email=None,
    )
    await runtime.services.identities.add_member(
        runtime.user.id, runtime.organization.id, member.id
    )
    evaluations = runtime.services.evaluations
    assert evaluations is not None
    with pytest.raises(AuthorizationError):
        await evaluations.create_dataset(
            member.id,
            runtime.organization.id,
            code="unauthorized-eval",
            display_name="Unauthorized Eval",
            description="",
            suite=_suite([_case("denied")]),
        )


async def test_tampered_dataset_version_fails_evaluation_closed(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    agent_id, agent_version_id = await _create_agent_version(runtime, "tamper-eval-agent")
    _, dataset_version_id = await _create_dataset_version(
        runtime,
        code="tamper-suite",
        cases=[_case("integrity-case")],
    )
    policy = await runtime.client.put(
        f"/api/v1/agents/{agent_id}/evaluation-policy",
        headers={**runtime.headers, "If-Match": "0"},
        json={
            "dataset_version_id": str(dataset_version_id),
            "min_pass_rate": 1,
            "max_critical_failures": 0,
        },
    )
    assert policy.status_code == 200
    evaluations = runtime.services.evaluations
    evaluation_executor = runtime.services.evaluation_executor
    assert evaluations is not None
    assert evaluation_executor is not None
    run, created = await evaluations.create_run(
        runtime.user.id,
        runtime.organization.id,
        agent_id,
        agent_version_id,
        idempotency_key="tampered-dataset-run",
        trace_id=uuid4(),
    )
    assert created is True
    async with runtime.services.database.session_factory() as session, session.begin():
        version = await session.get(EvaluationDatasetVersion, dataset_version_id)
        assert version is not None
        version.cases_snapshot = {
            "schema_version": 1,
            "cases": [_case("modified-after-versioning")],
        }

    await evaluation_executor.execute(run.id)
    failed, results = await evaluations.get_run(
        runtime.user.id, runtime.organization.id, run.id
    )
    assert failed.status == EvaluationRunStatus.FAILED
    assert failed.error_code == "evaluation_execution_failed"
    assert results == []


async def _create_agent_version(
    runtime: PlatformRuntime, code: str
) -> tuple[UUID, UUID]:
    created = await runtime.client.post(
        "/api/v1/agents",
        headers=runtime.headers,
        json={"code": code, "display_name": code},
    )
    assert created.status_code == 201
    agent_id = UUID(created.json()["id"])
    version = await runtime.client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=runtime.headers,
        json={"expected_revision": 1},
    )
    assert version.status_code == 201
    return agent_id, UUID(version.json()["id"])


async def _create_dataset_version(
    runtime: PlatformRuntime,
    *,
    code: str,
    cases: list[dict[str, object]],
) -> tuple[UUID, UUID]:
    created = await runtime.client.post(
        "/api/v1/evaluations/datasets",
        headers=runtime.headers,
        json={
            "code": code,
            "display_name": code,
            "suite": {"schema_version": 1, "cases": cases},
        },
    )
    assert created.status_code == 201
    dataset_id = UUID(created.json()["id"])
    version = await runtime.client.post(
        f"/api/v1/evaluations/datasets/{dataset_id}/versions",
        headers=runtime.headers,
        json={"expected_revision": 1},
    )
    assert version.status_code == 201
    return dataset_id, UUID(version.json()["id"])


async def _release(
    runtime: PlatformRuntime,
    agent_id: UUID,
    agent_version_id: UUID,
    *,
    key: str,
    evaluation_run_id: UUID | None = None,
):
    return await runtime.client.post(
        f"/api/v1/agents/{agent_id}/releases",
        headers={**runtime.headers, "Idempotency-Key": key},
        json={
            "version_id": str(agent_version_id),
            "reason": "EvalOps development release",
            "expected_generation": 0,
            "evaluation_run_id": (
                str(evaluation_run_id) if evaluation_run_id is not None else None
            ),
        },
    )


async def _wait_for_evaluation(
    runtime: PlatformRuntime, run_id: UUID
) -> dict[str, object]:
    for _ in range(200):
        response = await runtime.client.get(
            f"/api/v1/evaluations/runs/{run_id}", headers=runtime.headers
        )
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("Evaluation run did not complete.")


def _suite(cases: list[dict[str, object]]):
    from ai_agent.evaluations.service import EvaluationSuite

    return EvaluationSuite.model_validate({"schema_version": 1, "cases": cases})


def _case(
    key: str,
    *,
    question: str = "respond safely",
    expected_tools: list[str] | None = None,
    severity: str = "quality",
) -> dict[str, object]:
    return {
        "key": key,
        "question": question,
        "expected_tools": expected_tools or [],
        "requires_citation": False,
        "expects_denial": False,
        "severity": severity,
    }
