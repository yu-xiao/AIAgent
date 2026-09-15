"""Tenant-scoped immutable evaluation datasets, runs, and gate policies."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.audit.service import AuditService
from ai_agent.config import Environment
from ai_agent.errors import ConfigurationError, ConflictError, ResourceNotFoundError
from ai_agent.identity.service import (
    EVAL_DATASET_WRITE,
    EVAL_POLICY_MANAGE,
    EVAL_RUN_CREATE,
    EVAL_VIEW,
    IdentityService,
)
from ai_agent.persistence.models import (
    AgentDefinition,
    AgentEvaluationPolicy,
    AgentStatus,
    AgentVersion,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationDatasetDraft,
    EvaluationDatasetStatus,
    EvaluationDatasetVersion,
    EvaluationRun,
    EvaluationRunStatus,
)

_CODE = re.compile(r"^[a-z][a-z0-9-]{1,99}$")
_CASE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class EvaluationCaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=4_000)
    expected_tools: tuple[str, ...] = Field(default=(), max_length=20)
    requires_citation: bool = False
    expects_denial: bool = False
    severity: Literal["quality", "critical"] = "quality"

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = value.strip()
        if not _CASE_KEY.fullmatch(normalized):
            raise ValueError("Evaluation case key contains an invalid value.")
        return normalized

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Evaluation question must not be blank.")
        return normalized

    @field_validator("expected_tools")
    @classmethod
    def validate_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not _TOOL_NAME.fullmatch(item) for item in normalized):
            raise ValueError("Evaluation expected Tool names contain an invalid value.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Evaluation expected Tool names must be unique.")
        return normalized

    @model_validator(mode="after")
    def validate_expectations(self) -> EvaluationCaseSpec:
        if self.expects_denial and self.requires_citation:
            raise ValueError("A denial case cannot require a Citation.")
        return self


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    cases: tuple[EvaluationCaseSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> EvaluationSuite:
        keys = [item.key for item in self.cases]
        if len(set(keys)) != len(keys):
            raise ValueError("Evaluation case keys must be unique.")
        return self


@dataclass(frozen=True, slots=True)
class EvaluationCaseOutcome:
    case_key: str
    severity: str
    passed: bool
    reason: str
    used_tools: tuple[str, ...]
    citations_count: int
    answer_digest: str | None
    input_tokens: int
    output_tokens: int
    duration_ms: float


@dataclass(frozen=True, slots=True)
class GateAssessment:
    passed: bool
    decision: str
    evaluation_run_id: UUID | None
    policy_digest: str | None


class EvaluationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identities: IdentityService,
        *,
        environment: Environment = Environment.DEVELOPMENT,
        max_cases_per_dataset: int = 200,
        audit: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._identities = identities
        self._environment = environment
        self._max_cases_per_dataset = max_cases_per_dataset
        self._audit = audit or AuditService()

    async def create_dataset(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        code: str,
        display_name: str,
        description: str,
        suite: EvaluationSuite,
    ) -> tuple[EvaluationDataset, EvaluationDatasetDraft]:
        await self._identities.access(actor_id, organization_id, EVAL_DATASET_WRITE)
        self._validate_suite(suite)
        normalized_code = code.strip().lower()
        if not _CODE.fullmatch(normalized_code):
            raise ConflictError(
                "Evaluation dataset code must use lowercase letters, digits, and hyphens."
            )
        if not display_name.strip():
            raise ConflictError("Evaluation dataset display name must not be blank.")
        try:
            async with self._session_factory() as session, session.begin():
                dataset = EvaluationDataset(
                    organization_id=organization_id,
                    code=normalized_code,
                    display_name=display_name.strip(),
                    description=description.strip(),
                    created_by=actor_id,
                )
                session.add(dataset)
                await session.flush()
                draft = EvaluationDatasetDraft(
                    dataset_id=dataset.id,
                    organization_id=organization_id,
                    revision=1,
                    cases=suite.model_dump(mode="json"),
                    updated_by=actor_id,
                )
                session.add(draft)
                session.add(
                    self._audit.record(
                        organization_id=organization_id,
                        actor_user_id=actor_id,
                        action="evaluation.dataset_created",
                        resource_type="evaluation_dataset",
                        resource_id=str(dataset.id),
                        details={"code": normalized_code, "cases": len(suite.cases)},
                    )
                )
                return dataset, draft
        except IntegrityError as exc:
            raise ConflictError("Evaluation dataset code already exists.") from exc

    async def list_datasets(
        self, actor_id: UUID, organization_id: UUID
    ) -> list[EvaluationDataset]:
        await self._identities.access(actor_id, organization_id, EVAL_VIEW)
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(EvaluationDataset)
                    .where(EvaluationDataset.organization_id == organization_id)
                    .order_by(EvaluationDataset.code)
                )
            )

    async def get_draft(
        self, actor_id: UUID, organization_id: UUID, dataset_id: UUID
    ) -> EvaluationDatasetDraft:
        await self._identities.access(actor_id, organization_id, EVAL_DATASET_WRITE)
        async with self._session_factory() as session:
            draft = await session.scalar(
                select(EvaluationDatasetDraft).where(
                    EvaluationDatasetDraft.dataset_id == dataset_id,
                    EvaluationDatasetDraft.organization_id == organization_id,
                )
            )
            if draft is None:
                raise ResourceNotFoundError("Evaluation dataset draft not found.")
            return draft

    async def update_draft(
        self,
        actor_id: UUID,
        organization_id: UUID,
        dataset_id: UUID,
        *,
        expected_revision: int,
        suite: EvaluationSuite,
    ) -> EvaluationDatasetDraft:
        await self._identities.access(actor_id, organization_id, EVAL_DATASET_WRITE)
        self._validate_suite(suite)
        async with self._session_factory() as session, session.begin():
            draft = await session.scalar(
                select(EvaluationDatasetDraft)
                .where(
                    EvaluationDatasetDraft.dataset_id == dataset_id,
                    EvaluationDatasetDraft.organization_id == organization_id,
                )
                .with_for_update()
            )
            if draft is None:
                raise ResourceNotFoundError("Evaluation dataset draft not found.")
            if draft.revision != expected_revision:
                raise ConflictError("Evaluation dataset draft revision is stale.")
            draft.cases = suite.model_dump(mode="json")
            draft.revision += 1
            draft.updated_by = actor_id
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="evaluation.dataset_draft_updated",
                    resource_type="evaluation_dataset",
                    resource_id=str(dataset_id),
                    details={"revision": draft.revision, "cases": len(suite.cases)},
                )
            )
            return draft

    async def create_dataset_version(
        self,
        actor_id: UUID,
        organization_id: UUID,
        dataset_id: UUID,
        *,
        expected_revision: int,
    ) -> EvaluationDatasetVersion:
        await self._identities.access(actor_id, organization_id, EVAL_DATASET_WRITE)
        async with self._session_factory() as session, session.begin():
            dataset = await self._active_dataset(
                session, organization_id, dataset_id, lock=True
            )
            draft = await session.scalar(
                select(EvaluationDatasetDraft)
                .where(
                    EvaluationDatasetDraft.dataset_id == dataset.id,
                    EvaluationDatasetDraft.organization_id == organization_id,
                )
                .with_for_update()
            )
            if draft is None:
                raise ResourceNotFoundError("Evaluation dataset draft not found.")
            if draft.revision != expected_revision:
                raise ConflictError("Evaluation dataset draft revision is stale.")
            suite = EvaluationSuite.model_validate(draft.cases)
            self._validate_suite(suite)
            snapshot = suite.model_dump(mode="json")
            digest = _digest(snapshot)
            latest = await session.scalar(
                select(EvaluationDatasetVersion)
                .where(EvaluationDatasetVersion.dataset_id == dataset_id)
                .order_by(EvaluationDatasetVersion.version_number.desc())
                .limit(1)
            )
            if latest is not None and hmac.compare_digest(latest.cases_digest, digest):
                raise ConflictError(
                    "Evaluation dataset draft is unchanged from the latest version."
                )
            version = EvaluationDatasetVersion(
                dataset_id=dataset_id,
                organization_id=organization_id,
                version_number=1 if latest is None else latest.version_number + 1,
                cases_snapshot=snapshot,
                cases_digest=digest,
                created_by=actor_id,
            )
            session.add(version)
            await session.flush()
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="evaluation.dataset_version_created",
                    resource_type="evaluation_dataset_version",
                    resource_id=str(version.id),
                    details={
                        "dataset_id": str(dataset_id),
                        "version": version.version_number,
                        "cases_digest": digest,
                    },
                )
            )
            return version

    async def list_dataset_versions(
        self, actor_id: UUID, organization_id: UUID, dataset_id: UUID
    ) -> list[EvaluationDatasetVersion]:
        await self._identities.access(actor_id, organization_id, EVAL_VIEW)
        async with self._session_factory() as session:
            await self._active_dataset(session, organization_id, dataset_id)
            return list(
                await session.scalars(
                    select(EvaluationDatasetVersion)
                    .where(
                        EvaluationDatasetVersion.dataset_id == dataset_id,
                        EvaluationDatasetVersion.organization_id == organization_id,
                    )
                    .order_by(EvaluationDatasetVersion.version_number.desc())
                )
            )

    async def upsert_policy(
        self,
        actor_id: UUID,
        organization_id: UUID,
        agent_id: UUID,
        *,
        expected_revision: int,
        dataset_version_id: UUID,
        min_pass_rate: float,
        max_critical_failures: int,
        is_enabled: bool,
    ) -> AgentEvaluationPolicy:
        await self._identities.access(actor_id, organization_id, EVAL_POLICY_MANAGE)
        if not 0 <= min_pass_rate <= 1:
            raise ConflictError("Evaluation policy pass rate must be between zero and one.")
        if max_critical_failures < 0:
            raise ConflictError("Evaluation policy critical failure limit cannot be negative.")
        async with self._session_factory() as session, session.begin():
            await self._active_agent(session, organization_id, agent_id, lock=True)
            await self._dataset_version(
                session, organization_id, dataset_version_id, verify=True
            )
            policy = await session.scalar(
                select(AgentEvaluationPolicy)
                .where(
                    AgentEvaluationPolicy.organization_id == organization_id,
                    AgentEvaluationPolicy.agent_id == agent_id,
                    AgentEvaluationPolicy.environment == self._environment.value,
                )
                .with_for_update()
            )
            if policy is None:
                if expected_revision != 0:
                    raise ConflictError("Evaluation policy does not exist at that revision.")
                revision = 1
                digest = _policy_digest(
                    agent_id,
                    self._environment.value,
                    dataset_version_id,
                    revision,
                    min_pass_rate,
                    max_critical_failures,
                    is_enabled,
                )
                policy = AgentEvaluationPolicy(
                    organization_id=organization_id,
                    agent_id=agent_id,
                    environment=self._environment.value,
                    dataset_version_id=dataset_version_id,
                    revision=revision,
                    min_pass_rate=min_pass_rate,
                    max_critical_failures=max_critical_failures,
                    is_enabled=is_enabled,
                    policy_digest=digest,
                    updated_by=actor_id,
                )
                session.add(policy)
            else:
                if policy.revision != expected_revision:
                    raise ConflictError("Evaluation policy revision is stale.")
                policy.revision += 1
                policy.dataset_version_id = dataset_version_id
                policy.min_pass_rate = min_pass_rate
                policy.max_critical_failures = max_critical_failures
                policy.is_enabled = is_enabled
                policy.updated_by = actor_id
                policy.policy_digest = _policy_digest(
                    agent_id,
                    self._environment.value,
                    dataset_version_id,
                    policy.revision,
                    min_pass_rate,
                    max_critical_failures,
                    is_enabled,
                )
            await session.flush()
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="evaluation.policy_updated",
                    resource_type="agent_evaluation_policy",
                    resource_id=str(policy.id),
                    details={
                        "agent_id": str(agent_id),
                        "revision": policy.revision,
                        "dataset_version_id": str(dataset_version_id),
                        "min_pass_rate": min_pass_rate,
                        "max_critical_failures": max_critical_failures,
                        "enabled": is_enabled,
                        "policy_digest": policy.policy_digest,
                    },
                )
            )
            return policy

    async def get_policy(
        self, actor_id: UUID, organization_id: UUID, agent_id: UUID
    ) -> AgentEvaluationPolicy:
        await self._identities.access(actor_id, organization_id, EVAL_VIEW)
        async with self._session_factory() as session:
            await self._active_agent(session, organization_id, agent_id)
            policy = await self._policy(session, organization_id, agent_id)
            if policy is None:
                raise ResourceNotFoundError("Agent evaluation policy not found.")
            return policy

    async def create_run(
        self,
        actor_id: UUID,
        organization_id: UUID,
        agent_id: UUID,
        agent_version_id: UUID,
        *,
        idempotency_key: str | None,
        trace_id: UUID,
    ) -> tuple[EvaluationRun, bool]:
        await self._identities.access(actor_id, organization_id, EVAL_RUN_CREATE)
        try:
            async with self._session_factory() as session, session.begin():
                if idempotency_key:
                    existing = await session.scalar(
                        select(EvaluationRun).where(
                            EvaluationRun.organization_id == organization_id,
                            EvaluationRun.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        if (
                            existing.agent_id != agent_id
                            or existing.agent_version_id != agent_version_id
                        ):
                            raise ConflictError(
                                "Idempotency key was used for another evaluation run."
                            )
                        return existing, False
                await self._active_agent(session, organization_id, agent_id)
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.id == agent_version_id,
                        AgentVersion.agent_id == agent_id,
                        AgentVersion.organization_id == organization_id,
                    )
                )
                if version is None:
                    raise ResourceNotFoundError("Agent version not found.")
                policy = await self._policy(
                    session, organization_id, agent_id, enabled_only=True
                )
                if policy is None:
                    raise ConflictError("Agent has no enabled evaluation policy.")
                dataset_version = await self._dataset_version(
                    session, organization_id, policy.dataset_version_id, verify=True
                )
                suite = EvaluationSuite.model_validate(dataset_version.cases_snapshot)
                self._validate_suite(suite)
                run = EvaluationRun(
                    organization_id=organization_id,
                    agent_id=agent_id,
                    agent_version_id=agent_version_id,
                    agent_config_digest=version.config_digest,
                    dataset_version_id=dataset_version.id,
                    policy_id=policy.id,
                    policy_digest=policy.policy_digest,
                    min_pass_rate=policy.min_pass_rate,
                    max_critical_failures=policy.max_critical_failures,
                    requested_by=actor_id,
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                    total_cases=len(suite.cases),
                )
                session.add(run)
                await session.flush()
                session.add(
                    self._audit.record(
                        organization_id=organization_id,
                        actor_user_id=actor_id,
                        action="evaluation.run_queued",
                        resource_type="evaluation_run",
                        resource_id=str(run.id),
                        trace_id=run.trace_id,
                        details={
                            "agent_id": str(agent_id),
                            "agent_version_id": str(agent_version_id),
                            "dataset_version_id": str(dataset_version.id),
                            "policy_digest": policy.policy_digest,
                        },
                    )
                )
                return run, True
        except IntegrityError as exc:
            if idempotency_key:
                async with self._session_factory() as session:
                    existing = await session.scalar(
                        select(EvaluationRun).where(
                            EvaluationRun.organization_id == organization_id,
                            EvaluationRun.idempotency_key == idempotency_key,
                        )
                    )
                    if (
                        existing is not None
                        and existing.agent_id == agent_id
                        and existing.agent_version_id == agent_version_id
                    ):
                        return existing, False
            raise ConflictError("Evaluation run conflicts with an existing run.") from exc

    async def get_run(
        self, actor_id: UUID, organization_id: UUID, run_id: UUID
    ) -> tuple[EvaluationRun, list[EvaluationCaseResult]]:
        await self._identities.access(actor_id, organization_id, EVAL_VIEW)
        async with self._session_factory() as session:
            run = await session.scalar(
                select(EvaluationRun).where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.organization_id == organization_id,
                )
            )
            if run is None:
                raise ResourceNotFoundError("Evaluation run not found.")
            results = list(
                await session.scalars(
                    select(EvaluationCaseResult)
                    .where(
                        EvaluationCaseResult.evaluation_run_id == run_id,
                        EvaluationCaseResult.organization_id == organization_id,
                    )
                    .order_by(EvaluationCaseResult.created_at, EvaluationCaseResult.case_key)
                )
            )
            return run, results

    async def request_cancel(
        self, actor_id: UUID, organization_id: UUID, run_id: UUID
    ) -> EvaluationRun:
        await self._identities.access(actor_id, organization_id, EVAL_RUN_CREATE)
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.organization_id == organization_id,
                )
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFoundError("Evaluation run not found.")
            if run.status == EvaluationRunStatus.QUEUED:
                run.status = EvaluationRunStatus.CANCELLED
                run.cancellation_requested_at = datetime.now(UTC)
                run.completed_at = datetime.now(UTC)
            elif run.status == EvaluationRunStatus.RUNNING:
                run.cancellation_requested_at = datetime.now(UTC)
            else:
                return run
            session.add(
                self._audit.record(
                    organization_id=organization_id,
                    actor_user_id=actor_id,
                    action="evaluation.run_cancel_requested",
                    resource_type="evaluation_run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={"status": run.status.value},
                )
            )
            return run

    async def mark_cancelled(self, run_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .with_for_update()
            )
            if run is None or run.status not in {
                EvaluationRunStatus.QUEUED,
                EvaluationRunStatus.RUNNING,
            }:
                return
            now = datetime.now(UTC)
            run.status = EvaluationRunStatus.CANCELLED
            run.cancellation_requested_at = run.cancellation_requested_at or now
            run.completed_at = now
            session.add(
                self._audit.record(
                    organization_id=run.organization_id,
                    actor_user_id=run.requested_by,
                    action="evaluation.run_cancelled",
                    resource_type="evaluation_run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={},
                )
            )

    async def claim_run(self, run_id: UUID) -> EvaluationRun | None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .with_for_update(skip_locked=True)
            )
            if run is None or run.status != EvaluationRunStatus.QUEUED:
                return None
            if run.cancellation_requested_at is not None:
                run.status = EvaluationRunStatus.CANCELLED
                run.completed_at = datetime.now(UTC)
                return None
            run.status = EvaluationRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            return run

    async def load_suite(self, run: EvaluationRun) -> EvaluationSuite:
        async with self._session_factory() as session:
            version = await self._dataset_version(
                session, run.organization_id, run.dataset_version_id, verify=True
            )
            suite = EvaluationSuite.model_validate(version.cases_snapshot)
            self._validate_suite(suite)
            return suite

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(EvaluationRun.cancellation_requested_at).where(
                    EvaluationRun.id == run_id
                )
            )
            return value is not None

    async def complete_run(
        self, run_id: UUID, outcomes: list[EvaluationCaseOutcome]
    ) -> EvaluationRun:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .with_for_update()
            )
            if run is None or run.status != EvaluationRunStatus.RUNNING:
                raise ConflictError("Evaluation run is no longer active.")
            if run.cancellation_requested_at is not None:
                run.status = EvaluationRunStatus.CANCELLED
                run.completed_at = datetime.now(UTC)
                return run
            passed_cases = sum(item.passed for item in outcomes)
            critical_failures = sum(
                not item.passed and item.severity == "critical" for item in outcomes
            )
            total_cases = len(outcomes)
            pass_rate = passed_cases / total_cases if total_cases else 0.0
            run.total_cases = total_cases
            run.passed_cases = passed_cases
            run.critical_failures = critical_failures
            run.pass_rate = pass_rate
            run.gate_passed = (
                pass_rate >= run.min_pass_rate
                and critical_failures <= run.max_critical_failures
            )
            run.status = EvaluationRunStatus.COMPLETED
            run.completed_at = datetime.now(UTC)
            session.add_all(
                [
                    EvaluationCaseResult(
                        evaluation_run_id=run.id,
                        organization_id=run.organization_id,
                        case_key=item.case_key,
                        severity=item.severity,
                        passed=item.passed,
                        reason=item.reason,
                        used_tools=list(item.used_tools),
                        citations_count=item.citations_count,
                        answer_digest=item.answer_digest,
                        input_tokens=item.input_tokens,
                        output_tokens=item.output_tokens,
                        duration_ms=item.duration_ms,
                    )
                    for item in outcomes
                ]
            )
            session.add(
                self._audit.record(
                    organization_id=run.organization_id,
                    actor_user_id=run.requested_by,
                    action="evaluation.run_completed",
                    resource_type="evaluation_run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={
                        "total_cases": total_cases,
                        "passed_cases": passed_cases,
                        "critical_failures": critical_failures,
                        "pass_rate": pass_rate,
                        "gate_passed": run.gate_passed,
                    },
                )
            )
            return run

    async def fail_run(self, run_id: UUID, error_code: str) -> None:
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun)
                .where(EvaluationRun.id == run_id)
                .with_for_update()
            )
            if run is None or run.status not in {
                EvaluationRunStatus.QUEUED,
                EvaluationRunStatus.RUNNING,
            }:
                return
            run.status = EvaluationRunStatus.FAILED
            run.error_code = error_code
            run.completed_at = datetime.now(UTC)
            session.add(
                self._audit.record(
                    organization_id=run.organization_id,
                    actor_user_id=run.requested_by,
                    action="evaluation.run_failed",
                    resource_type="evaluation_run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={"error_code": error_code},
                )
            )

    async def recover_incomplete_runs(self) -> list[EvaluationRun]:
        async with self._session_factory() as session, session.begin():
            running = list(
                await session.scalars(
                    select(EvaluationRun)
                    .where(EvaluationRun.status == EvaluationRunStatus.RUNNING)
                    .with_for_update(skip_locked=True)
                )
            )
            now = datetime.now(UTC)
            for run in running:
                run.status = EvaluationRunStatus.FAILED
                run.error_code = "evaluation_interrupted"
                run.completed_at = now
                session.add(
                    self._audit.record(
                        organization_id=run.organization_id,
                        actor_user_id=run.requested_by,
                        action="evaluation.run_recovered_as_failed",
                        resource_type="evaluation_run",
                        resource_id=str(run.id),
                        trace_id=run.trace_id,
                        details={"error_code": run.error_code},
                    )
                )
            return list(
                await session.scalars(
                    select(EvaluationRun)
                    .where(EvaluationRun.status == EvaluationRunStatus.QUEUED)
                    .order_by(EvaluationRun.created_at, EvaluationRun.id)
                )
            )

    async def assess_release(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        agent_version_id: UUID,
        evaluation_run_id: UUID | None,
    ) -> GateAssessment:
        policy = await self._policy(
            session, organization_id, agent_id, enabled_only=True
        )
        if policy is None:
            return GateAssessment(False, "not_configured", None, None)
        if evaluation_run_id is None:
            return GateAssessment(False, "missing", None, policy.policy_digest)
        run = await session.scalar(
            select(EvaluationRun).where(
                EvaluationRun.id == evaluation_run_id,
                EvaluationRun.organization_id == organization_id,
                EvaluationRun.agent_id == agent_id,
                EvaluationRun.agent_version_id == agent_version_id,
                EvaluationRun.policy_id == policy.id,
                EvaluationRun.dataset_version_id == policy.dataset_version_id,
            )
        )
        if run is None:
            return GateAssessment(False, "invalid", None, policy.policy_digest)
        if not hmac.compare_digest(run.policy_digest, policy.policy_digest):
            return GateAssessment(False, "stale", run.id, policy.policy_digest)
        if run.status != EvaluationRunStatus.COMPLETED:
            return GateAssessment(False, "incomplete", run.id, policy.policy_digest)
        if run.gate_passed is not True:
            return GateAssessment(False, "failed", run.id, policy.policy_digest)
        return GateAssessment(True, "passed", run.id, policy.policy_digest)

    async def _active_dataset(
        self,
        session: AsyncSession,
        organization_id: UUID,
        dataset_id: UUID,
        *,
        lock: bool = False,
    ) -> EvaluationDataset:
        statement = select(EvaluationDataset).where(
            EvaluationDataset.id == dataset_id,
            EvaluationDataset.organization_id == organization_id,
            EvaluationDataset.status == EvaluationDatasetStatus.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update()
        dataset = await session.scalar(statement)
        if dataset is None:
            raise ResourceNotFoundError("Evaluation dataset not found.")
        return dataset

    async def _active_agent(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        *,
        lock: bool = False,
    ) -> AgentDefinition:
        statement = select(AgentDefinition).where(
            AgentDefinition.id == agent_id,
            AgentDefinition.organization_id == organization_id,
            AgentDefinition.status == AgentStatus.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update()
        agent = await session.scalar(statement)
        if agent is None:
            raise ResourceNotFoundError("Agent not found.")
        return agent

    async def _dataset_version(
        self,
        session: AsyncSession,
        organization_id: UUID,
        version_id: UUID,
        *,
        verify: bool,
    ) -> EvaluationDatasetVersion:
        version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.id == version_id,
                EvaluationDatasetVersion.organization_id == organization_id,
            )
        )
        if version is None:
            raise ResourceNotFoundError("Evaluation dataset version not found.")
        if verify:
            suite = EvaluationSuite.model_validate(version.cases_snapshot)
            if not hmac.compare_digest(
                version.cases_digest, _digest(suite.model_dump(mode="json"))
            ):
                raise ConfigurationError(
                    "Stored evaluation dataset version integrity check failed."
                )
        return version

    async def _policy(
        self,
        session: AsyncSession,
        organization_id: UUID,
        agent_id: UUID,
        *,
        enabled_only: bool = False,
    ) -> AgentEvaluationPolicy | None:
        filters = [
            AgentEvaluationPolicy.organization_id == organization_id,
            AgentEvaluationPolicy.agent_id == agent_id,
            AgentEvaluationPolicy.environment == self._environment.value,
        ]
        if enabled_only:
            filters.append(AgentEvaluationPolicy.is_enabled.is_(True))
        policy: AgentEvaluationPolicy | None = await session.scalar(
            select(AgentEvaluationPolicy).where(*filters)
        )
        return policy

    def _validate_suite(self, suite: EvaluationSuite) -> None:
        if len(suite.cases) > self._max_cases_per_dataset:
            raise ConflictError("Evaluation dataset exceeds the configured case limit.")


def _digest(value: dict[str, object]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _policy_digest(
    agent_id: UUID,
    environment: str,
    dataset_version_id: UUID,
    revision: int,
    min_pass_rate: float,
    max_critical_failures: int,
    is_enabled: bool,
) -> str:
    return _digest(
        {
            "agent_id": str(agent_id),
            "environment": environment,
            "dataset_version_id": str(dataset_version_id),
            "revision": revision,
            "min_pass_rate": min_pass_rate,
            "max_critical_failures": max_critical_failures,
            "is_enabled": is_enabled,
        }
    )
