"""PostgreSQL-backed durable Run job leases and recovery."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_agent.audit.service import AuditService
from ai_agent.observability.metrics import RUN_JOBS_TOTAL, RUN_QUEUE_DEPTH, RUN_QUEUE_OLDEST
from ai_agent.persistence.models import (
    Run,
    RunJob,
    RunJobAttempt,
    RunJobStatus,
    RunStatus,
    RunStep,
)

_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: UUID
    run_id: UUID
    organization_id: UUID
    user_id: UUID
    trace_id: UUID
    budget_day: date
    worker_id: str
    lease_token: str
    attempt_number: int


class RunJobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit or AuditService()

    async def claim(self, worker_id: str, *, lease_seconds: int) -> JobLease | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(RunJob)
                .where(
                    RunJob.status.in_((RunJobStatus.QUEUED, RunJobStatus.RETRY_WAIT)),
                    RunJob.available_at <= now,
                )
                .order_by(RunJob.available_at, RunJob.created_at, RunJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            run = await session.get(Run, job.run_id)
            if run is None or run.status in _TERMINAL_RUN_STATUSES:
                job.status = _job_status_for_run(run.status) if run else RunJobStatus.FAILED
                job.last_error_code = None if run else "run_missing"
                return None
            lease_token = secrets.token_hex(24)
            job.status = RunJobStatus.LEASED
            job.attempt_count += 1
            job.lease_owner = worker_id
            job.lease_token = lease_token
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.heartbeat_at = now
            session.add(
                RunJobAttempt(
                    job_id=job.id,
                    attempt_number=job.attempt_count,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            )
            session.add(
                self._audit.record(
                    organization_id=job.organization_id,
                    actor_user_id=None,
                    action="run_job.leased",
                    resource_type="run_job",
                    resource_id=str(job.id),
                    trace_id=run.trace_id,
                    details={"attempt": job.attempt_count},
                )
            )
            RUN_JOBS_TOTAL.labels(outcome="leased").inc()
            return JobLease(
                job_id=job.id,
                run_id=job.run_id,
                organization_id=job.organization_id,
                user_id=job.user_id,
                trace_id=run.trace_id,
                budget_day=run.created_at.date(),
                worker_id=worker_id,
                lease_token=lease_token,
                attempt_number=job.attempt_count,
            )

    async def mark_execution_started(self, lease: JobLease) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._locked_lease(session, lease)
            if job is None:
                return False
            job.execution_started_at = datetime.now(UTC)
            return True

    async def heartbeat(self, lease: JobLease, *, lease_seconds: int) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._locked_lease(session, lease)
            if job is None:
                return False
            now = datetime.now(UTC)
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def finish_from_run(self, lease: JobLease) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._locked_lease(session, lease)
            if job is None:
                return False
            run = await session.get(Run, lease.run_id)
            if run is None or run.status not in _TERMINAL_RUN_STATUSES:
                return False
            job.status = _job_status_for_run(run.status)
            job.last_error_code = run.error_code
            _clear_lease(job)
            await self._finish_attempt(
                session,
                lease,
                outcome=job.status.value,
                error_code=run.error_code,
            )
            session.add(
                self._audit.record(
                    organization_id=job.organization_id,
                    actor_user_id=None,
                    action=f"run_job.{job.status.value}",
                    resource_type="run_job",
                    resource_id=str(job.id),
                    trace_id=run.trace_id,
                    details={"attempt": lease.attempt_number},
                )
            )
            RUN_JOBS_TOTAL.labels(outcome=job.status.value).inc()
            return True

    async def release_before_execution(
        self,
        lease: JobLease,
        *,
        error_code: str,
        retry_delay_seconds: float,
        retry_max_delay_seconds: float = 300.0,
        retry_jitter_ratio: float = 0.0,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._locked_lease(session, lease)
            if job is None or job.execution_started_at is not None:
                return False
            now = datetime.now(UTC)
            job.failure_count += 1
            if job.failure_count >= job.max_attempts:
                job.status = RunJobStatus.DEAD_LETTER
                await self._fail_run(
                    session,
                    job.run_id,
                    "job_attempts_exhausted",
                    "Run could not be started after the configured attempts.",
                )
            else:
                job.status = RunJobStatus.RETRY_WAIT
                await self._reset_run_for_retry(session, job.run_id)
                job.available_at = now + timedelta(
                    seconds=_retry_delay(
                        retry_delay_seconds,
                        job.failure_count,
                        retry_max_delay_seconds,
                        retry_jitter_ratio,
                    )
                )
            job.last_error_code = error_code
            _clear_lease(job)
            await self._finish_attempt(
                session,
                lease,
                outcome=job.status.value,
                error_code=error_code,
            )
            RUN_JOBS_TOTAL.labels(outcome=job.status.value).inc()
            return True

    async def release_for_capacity(
        self,
        lease: JobLease,
        *,
        retry_delay_seconds: float,
        retry_jitter_ratio: float = 0.0,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._locked_lease(session, lease)
            if job is None or job.execution_started_at is not None:
                return False
            job.status = RunJobStatus.RETRY_WAIT
            job.available_at = datetime.now(UTC) + timedelta(
                seconds=_retry_delay(
                    retry_delay_seconds,
                    1,
                    retry_delay_seconds,
                    retry_jitter_ratio,
                )
            )
            job.last_error_code = "concurrency_capacity"
            _clear_lease(job)
            await self._finish_attempt(
                session,
                lease,
                outcome="capacity_wait",
                error_code="concurrency_capacity",
            )
            return True

    async def recover_expired(
        self,
        *,
        retry_delay_seconds: float,
        retry_max_delay_seconds: float = 300.0,
        retry_jitter_ratio: float = 0.0,
        limit: int = 100,
    ) -> int:
        now = datetime.now(UTC)
        recovered = 0
        async with self._session_factory() as session, session.begin():
            jobs = list(
                await session.scalars(
                    select(RunJob)
                    .where(
                        RunJob.status == RunJobStatus.LEASED,
                        RunJob.lease_expires_at < now,
                    )
                    .order_by(RunJob.lease_expires_at, RunJob.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for job in jobs:
                run = await session.get(Run, job.run_id)
                lease = _lease_from_job(job, run)
                if run is not None and run.status in _TERMINAL_RUN_STATUSES:
                    job.status = _job_status_for_run(run.status)
                    job.last_error_code = run.error_code
                elif job.execution_started_at is None and job.failure_count + 1 < job.max_attempts:
                    job.failure_count += 1
                    job.status = RunJobStatus.RETRY_WAIT
                    await self._reset_run_for_retry(session, job.run_id)
                    job.available_at = now + timedelta(
                        seconds=_retry_delay(
                            retry_delay_seconds,
                            job.failure_count,
                            retry_max_delay_seconds,
                            retry_jitter_ratio,
                        )
                    )
                    job.last_error_code = "lease_expired_before_execution"
                else:
                    job.failure_count += 1
                    job.status = RunJobStatus.DEAD_LETTER
                    job.last_error_code = "worker_lost_during_execution"
                    await self._fail_run(
                        session,
                        job.run_id,
                        "worker_lost_during_execution",
                        "Run stopped because its Worker lease expired during execution.",
                    )
                _clear_lease(job)
                if lease is not None:
                    await self._finish_attempt(
                        session,
                        lease,
                        outcome=job.status.value,
                        error_code=job.last_error_code,
                    )
                RUN_JOBS_TOTAL.labels(outcome=job.status.value).inc()
                recovered += 1
        return recovered

    async def observe_queue(self) -> None:
        async with self._session_factory() as session:
            depth, oldest = (
                await session.execute(
                    select(func.count(RunJob.id), func.min(RunJob.created_at)).where(
                        RunJob.status.in_((RunJobStatus.QUEUED, RunJobStatus.RETRY_WAIT))
                    )
                )
            ).one()
        RUN_QUEUE_DEPTH.set(int(depth or 0))
        if oldest is None:
            RUN_QUEUE_OLDEST.set(0)
        else:
            RUN_QUEUE_OLDEST.set(
                max((datetime.now(UTC) - _as_utc(oldest)).total_seconds(), 0)
            )

    async def cancel_queued(self, run_id: UUID) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await session.scalar(
                select(RunJob).where(RunJob.run_id == run_id).with_for_update()
            )
            if job is None or job.status not in {
                RunJobStatus.QUEUED,
                RunJobStatus.RETRY_WAIT,
            }:
                return False
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status in _TERMINAL_RUN_STATUSES:
                return False
            now = datetime.now(UTC)
            job.status = RunJobStatus.CANCELLED
            job.last_error_code = "cancelled"
            run.status = RunStatus.CANCELLED
            run.error_code = "cancelled"
            run.error_message = "Run was cancelled."
            run.completed_at = now
            session.add(
                self._audit.record(
                    organization_id=run.organization_id,
                    actor_user_id=run.user_id,
                    action="run.cancelled",
                    resource_type="run",
                    resource_id=str(run.id),
                    trace_id=run.trace_id,
                    details={"source": "durable_queue"},
                )
            )
            RUN_JOBS_TOTAL.labels(outcome=RunJobStatus.CANCELLED.value).inc()
            return True

    async def _locked_lease(
        self, session: AsyncSession, lease: JobLease
    ) -> RunJob | None:
        job: RunJob | None = await session.scalar(
            select(RunJob)
            .where(
                RunJob.id == lease.job_id,
                RunJob.status == RunJobStatus.LEASED,
                RunJob.lease_owner == lease.worker_id,
                RunJob.lease_token == lease.lease_token,
                RunJob.lease_expires_at >= datetime.now(UTC),
            )
            .with_for_update()
        )
        return job

    async def _finish_attempt(
        self,
        session: AsyncSession,
        lease: JobLease,
        *,
        outcome: str,
        error_code: str | None,
    ) -> None:
        attempt = await session.scalar(
            select(RunJobAttempt)
            .where(
                RunJobAttempt.job_id == lease.job_id,
                RunJobAttempt.attempt_number == lease.attempt_number,
                RunJobAttempt.lease_token == lease.lease_token,
            )
            .with_for_update()
        )
        if attempt is not None and attempt.completed_at is None:
            attempt.completed_at = datetime.now(UTC)
            attempt.outcome = outcome
            attempt.error_code = error_code

    async def _fail_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        error_code: str,
        message: str,
    ) -> None:
        run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.status in _TERMINAL_RUN_STATUSES:
            return
        now = datetime.now(UTC)
        run.status = RunStatus.FAILED
        run.error_code = error_code
        run.error_message = message
        run.completed_at = now
        step = await session.scalar(
            select(RunStep).where(RunStep.run_id == run.id, RunStep.sequence == 1)
        )
        if step is not None:
            step.status = RunStatus.FAILED.value
            step.completed_at = now
        session.add(
            self._audit.record(
                organization_id=run.organization_id,
                actor_user_id=run.user_id,
                action="run.failed",
                resource_type="run",
                resource_id=str(run.id),
                trace_id=run.trace_id,
                details={"error_code": error_code},
            )
        )

    async def _reset_run_for_retry(self, session: AsyncSession, run_id: UUID) -> None:
        run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.status != RunStatus.RUNNING:
            return
        run.status = RunStatus.QUEUED
        run.started_at = None
        await session.execute(delete(RunStep).where(RunStep.run_id == run_id))


def _clear_lease(job: RunJob) -> None:
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    job.heartbeat_at = None


def _lease_from_job(job: RunJob, run: Run | None) -> JobLease | None:
    if not job.lease_owner or not job.lease_token:
        return None
    return JobLease(
        job_id=job.id,
        run_id=job.run_id,
        organization_id=job.organization_id,
        user_id=job.user_id,
        trace_id=run.trace_id if run is not None else UUID(int=0),
        budget_day=(run.created_at if run is not None else job.created_at).date(),
        worker_id=job.lease_owner,
        lease_token=job.lease_token,
        attempt_number=job.attempt_count,
    )


def _job_status_for_run(status: RunStatus) -> RunJobStatus:
    if status == RunStatus.COMPLETED:
        return RunJobStatus.SUCCEEDED
    if status == RunStatus.CANCELLED:
        return RunJobStatus.CANCELLED
    return RunJobStatus.FAILED


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _retry_delay(
    base_seconds: float,
    failure_count: int,
    max_seconds: float,
    jitter_ratio: float,
) -> float:
    exponential = min(
        float(base_seconds * (2.0 ** max(failure_count - 1, 0))),
        float(max_seconds),
    )
    if jitter_ratio <= 0:
        return float(exponential)
    unit = secrets.randbelow(1_000_001) / 1_000_000
    jitter = (unit * 2 - 1) * jitter_ratio
    return float(max(min(exponential * (1 + jitter), max_seconds), 0.1))
