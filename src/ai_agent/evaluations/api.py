"""EvalOps dataset, policy, and execution APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ai_agent.evaluations.executor import EvaluationExecutor
from ai_agent.evaluations.service import EvaluationService, EvaluationSuite
from ai_agent.identity.dependencies import CurrentIdentity, OrganizationId, require_csrf
from ai_agent.persistence.models import (
    AgentEvaluationPolicy,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationDatasetDraft,
    EvaluationDatasetVersion,
    EvaluationRun,
)

router = APIRouter(prefix="/api/v1")
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str, Header(alias="If-Match")]


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1_000)
    suite: EvaluationSuite


class DatasetView(BaseModel):
    id: UUID
    code: str
    display_name: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime


class DatasetDraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: EvaluationSuite


class DatasetDraftView(BaseModel):
    dataset_id: UUID
    revision: int
    suite: EvaluationSuite
    updated_at: datetime


class DatasetVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class DatasetVersionView(BaseModel):
    id: UUID
    dataset_id: UUID
    version_number: int
    suite: EvaluationSuite
    cases_digest: str
    created_at: datetime


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version_id: UUID
    min_pass_rate: float = Field(ge=0, le=1)
    max_critical_failures: int = Field(default=0, ge=0)
    is_enabled: bool = True


class PolicyView(BaseModel):
    id: UUID
    agent_id: UUID
    environment: str
    dataset_version_id: UUID
    revision: int
    min_pass_rate: float
    max_critical_failures: int
    is_enabled: bool
    policy_digest: str
    updated_at: datetime


class EvaluationRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version_id: UUID


class EvaluationCaseResultView(BaseModel):
    case_key: str
    severity: str
    passed: bool
    reason: str
    used_tools: list[str]
    citations_count: int
    answer_digest: str | None
    input_tokens: int
    output_tokens: int
    duration_ms: float


class EvaluationRunView(BaseModel):
    id: UUID
    agent_id: UUID
    agent_version_id: UUID
    dataset_version_id: UUID
    policy_id: UUID
    policy_digest: str
    status: str
    total_cases: int
    passed_cases: int
    critical_failures: int
    pass_rate: float | None
    gate_passed: bool | None
    error_code: str | None
    cancellation_requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    results: list[EvaluationCaseResultView] = Field(default_factory=list)


@router.post(
    "/evaluations/datasets",
    response_model=DatasetView,
    status_code=status.HTTP_201_CREATED,
    tags=["evaluations"],
)
async def create_dataset(
    payload: DatasetCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> DatasetView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    dataset, _ = await _service(request).create_dataset(
        identity.session.user_id,
        organization_id,
        code=payload.code,
        display_name=payload.display_name,
        description=payload.description,
        suite=payload.suite,
    )
    return _dataset_view(dataset)


@router.get(
    "/evaluations/datasets", response_model=list[DatasetView], tags=["evaluations"]
)
async def list_datasets(
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[DatasetView]:
    datasets = await _service(request).list_datasets(
        identity.session.user_id, organization_id
    )
    return [_dataset_view(item) for item in datasets]


@router.get(
    "/evaluations/datasets/{dataset_id}/draft",
    response_model=DatasetDraftView,
    tags=["evaluations"],
)
async def get_dataset_draft(
    dataset_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> DatasetDraftView:
    draft = await _service(request).get_draft(
        identity.session.user_id, organization_id, dataset_id
    )
    return _draft_view(draft)


@router.put(
    "/evaluations/datasets/{dataset_id}/draft",
    response_model=DatasetDraftView,
    tags=["evaluations"],
)
async def update_dataset_draft(
    dataset_id: UUID,
    payload: DatasetDraftUpdate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    if_match: IfMatch,
) -> DatasetDraftView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    draft = await _service(request).update_draft(
        identity.session.user_id,
        organization_id,
        dataset_id,
        expected_revision=_parse_revision(if_match, allow_zero=False),
        suite=payload.suite,
    )
    return _draft_view(draft)


@router.post(
    "/evaluations/datasets/{dataset_id}/versions",
    response_model=DatasetVersionView,
    status_code=status.HTTP_201_CREATED,
    tags=["evaluations"],
)
async def create_dataset_version(
    dataset_id: UUID,
    payload: DatasetVersionCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> DatasetVersionView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    version = await _service(request).create_dataset_version(
        identity.session.user_id,
        organization_id,
        dataset_id,
        expected_revision=payload.expected_revision,
    )
    return _version_view(version)


@router.get(
    "/evaluations/datasets/{dataset_id}/versions",
    response_model=list[DatasetVersionView],
    tags=["evaluations"],
)
async def list_dataset_versions(
    dataset_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> list[DatasetVersionView]:
    versions = await _service(request).list_dataset_versions(
        identity.session.user_id, organization_id, dataset_id
    )
    return [_version_view(item) for item in versions]


@router.put(
    "/agents/{agent_id}/evaluation-policy",
    response_model=PolicyView,
    tags=["evaluations"],
)
async def update_policy(
    agent_id: UUID,
    payload: PolicyUpdate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    if_match: IfMatch,
) -> PolicyView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    policy = await _service(request).upsert_policy(
        identity.session.user_id,
        organization_id,
        agent_id,
        expected_revision=_parse_revision(if_match, allow_zero=True),
        dataset_version_id=payload.dataset_version_id,
        min_pass_rate=payload.min_pass_rate,
        max_critical_failures=payload.max_critical_failures,
        is_enabled=payload.is_enabled,
    )
    return _policy_view(policy)


@router.get(
    "/agents/{agent_id}/evaluation-policy",
    response_model=PolicyView,
    tags=["evaluations"],
)
async def get_policy(
    agent_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> PolicyView:
    policy = await _service(request).get_policy(
        identity.session.user_id, organization_id, agent_id
    )
    return _policy_view(policy)


@router.post(
    "/agents/{agent_id}/evaluations",
    response_model=EvaluationRunView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["evaluations"],
)
async def create_evaluation_run(
    agent_id: UUID,
    payload: EvaluationRunCreate,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
    idempotency_key: IdempotencyKey = None,
) -> EvaluationRunView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    _validate_idempotency_key(idempotency_key)
    executor = _executor(request)
    run, created = await _service(request).create_run(
        identity.session.user_id,
        organization_id,
        agent_id,
        payload.agent_version_id,
        idempotency_key=idempotency_key,
        trace_id=request.state.trace_id,
    )
    if created:
        try:
            executor.submit(run.id)
        except Exception as exc:
            await _service(request).fail_run(run.id, "evaluation_submission_failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Evaluation run could not be submitted.",
            ) from exc
    return _run_view(run, [])


@router.get(
    "/evaluations/runs/{run_id}",
    response_model=EvaluationRunView,
    tags=["evaluations"],
)
async def get_evaluation_run(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> EvaluationRunView:
    run, results = await _service(request).get_run(
        identity.session.user_id, organization_id, run_id
    )
    return _run_view(run, results)


@router.post(
    "/evaluations/runs/{run_id}/cancel",
    response_model=EvaluationRunView,
    tags=["evaluations"],
)
async def cancel_evaluation_run(
    run_id: UUID,
    request: Request,
    identity: CurrentIdentity,
    organization_id: OrganizationId,
) -> EvaluationRunView:
    await require_csrf(identity, request.headers.get("X-CSRF-Token"))
    run = await _service(request).request_cancel(
        identity.session.user_id, organization_id, run_id
    )
    return _run_view(run, [])


def _service(request: Request) -> EvaluationService:
    service = cast(EvaluationService | None, request.app.state.services.evaluations)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evaluation service is unavailable.",
        )
    return service


def _executor(request: Request) -> EvaluationExecutor:
    executor = cast(
        EvaluationExecutor | None, request.app.state.services.evaluation_executor
    )
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evaluation executor is unavailable.",
        )
    return executor


def _validate_idempotency_key(value: str | None) -> None:
    if value is not None and not 1 <= len(value) <= 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain between 1 and 200 characters.",
        )


def _parse_revision(value: str, *, allow_zero: bool) -> int:
    normalized = value.strip().strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="If-Match must contain a numeric revision.",
        ) from exc
    minimum = 0 if allow_zero else 1
    if revision < minimum:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"If-Match revision must be at least {minimum}.",
        )
    return revision


def _dataset_view(dataset: EvaluationDataset) -> DatasetView:
    return DatasetView(
        id=dataset.id,
        code=dataset.code,
        display_name=dataset.display_name,
        description=dataset.description,
        status=dataset.status.value,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def _draft_view(draft: EvaluationDatasetDraft) -> DatasetDraftView:
    return DatasetDraftView(
        dataset_id=draft.dataset_id,
        revision=draft.revision,
        suite=EvaluationSuite.model_validate(draft.cases),
        updated_at=draft.updated_at,
    )


def _version_view(version: EvaluationDatasetVersion) -> DatasetVersionView:
    return DatasetVersionView(
        id=version.id,
        dataset_id=version.dataset_id,
        version_number=version.version_number,
        suite=EvaluationSuite.model_validate(version.cases_snapshot),
        cases_digest=version.cases_digest,
        created_at=version.created_at,
    )


def _policy_view(policy: AgentEvaluationPolicy) -> PolicyView:
    return PolicyView(
        id=policy.id,
        agent_id=policy.agent_id,
        environment=policy.environment,
        dataset_version_id=policy.dataset_version_id,
        revision=policy.revision,
        min_pass_rate=policy.min_pass_rate,
        max_critical_failures=policy.max_critical_failures,
        is_enabled=policy.is_enabled,
        policy_digest=policy.policy_digest,
        updated_at=policy.updated_at,
    )


def _run_view(
    run: EvaluationRun, results: list[EvaluationCaseResult]
) -> EvaluationRunView:
    return EvaluationRunView(
        id=run.id,
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        dataset_version_id=run.dataset_version_id,
        policy_id=run.policy_id,
        policy_digest=run.policy_digest,
        status=run.status.value,
        total_cases=run.total_cases,
        passed_cases=run.passed_cases,
        critical_failures=run.critical_failures,
        pass_rate=run.pass_rate,
        gate_passed=run.gate_passed,
        error_code=run.error_code,
        cancellation_requested_at=run.cancellation_requested_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        results=[_result_view(item) for item in results],
    )


def _result_view(result: EvaluationCaseResult) -> EvaluationCaseResultView:
    return EvaluationCaseResultView(
        case_key=result.case_key,
        severity=result.severity,
        passed=result.passed,
        reason=result.reason,
        used_tools=result.used_tools,
        citations_count=result.citations_count,
        answer_digest=result.answer_digest,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        duration_ms=result.duration_ms,
    )
