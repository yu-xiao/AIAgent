from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from redis.asyncio import Redis
from sqlalchemy import select

from ai_agent.audit.service import AuditService
from ai_agent.config import (
    ExecutionMode,
    ExecutionSettings,
    ModelSettings,
    QuotaSettings,
    RunLimitSettings,
)
from ai_agent.conversations.service import ConversationService
from ai_agent.governance.quota import RedisRunQuota
from ai_agent.identity.service import IdentityService
from ai_agent.persistence import Database
from ai_agent.persistence.models import (
    Base,
    Run,
    RunJob,
    RunJobAttempt,
    RunJobStatus,
    RunStatus,
)
from ai_agent.runs.events import MemoryRunBackend
from ai_agent.runs.executor import RunExecutor
from ai_agent.runs.jobs import RunJobService
from ai_agent.runs.worker import RedisWorkerRegistry, RunWorker
from tests.p1_conftest import FakeModelProvider, PlatformRuntime


@pytest.fixture
async def durable_runtime():
    database_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    database_file.close()
    database_url = f"sqlite+aiosqlite:///{database_file.name.replace(chr(92), '/')}"
    database = Database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    audit = AuditService(b"test-job-audit-key", key_id="test-v1")
    identities = IdentityService(database.session_factory, audit)
    user = await identities.upsert_oidc_user(
        issuer="https://identity.example.test",
        subject="durable-worker",
        display_name="Durable Worker",
        email=None,
    )
    organization = await identities.create_organization(user.id, "Durable Jobs")
    limits = RunLimitSettings()
    conversations = ConversationService(
        database.session_factory,
        identities,
        limits,
        audit,
        durable_jobs_enabled=True,
        job_max_attempts=3,
    )
    jobs = RunJobService(database.session_factory, audit)
    yield database, user, organization, conversations, jobs, limits
    await database.dispose()


async def _create_run(runtime, title: str = "Durable") -> Run:
    _, user, organization, conversations, _, _ = runtime
    conversation = await conversations.create_conversation(user.id, organization.id, title)
    run, created = await conversations.create_run(
        user.id,
        organization.id,
        conversation.id,
        "execute durably",
        None,
        uuid4(),
    )
    assert created
    return run


async def test_run_and_job_are_created_in_one_transaction(durable_runtime) -> None:
    database, _, _, _, _, _ = durable_runtime
    run = await _create_run(durable_runtime)

    async with database.session_factory() as session:
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))

    assert job is not None
    assert job.status == RunJobStatus.QUEUED
    assert job.max_attempts == 3


async def test_job_lease_is_exclusive_and_fenced(durable_runtime) -> None:
    _, _, _, _, jobs, _ = durable_runtime
    await _create_run(durable_runtime)
    lease = await jobs.claim("worker-one", lease_seconds=45)

    assert lease is not None
    assert await jobs.claim("worker-two", lease_seconds=45) is None
    stale = replace(lease, lease_token="stale-token")
    assert await jobs.mark_execution_started(stale) is False
    assert await jobs.mark_execution_started(lease) is True


async def test_completed_run_finalizes_job_and_attempt(durable_runtime) -> None:
    database, _, _, conversations, jobs, limits = durable_runtime
    run = await _create_run(durable_runtime)
    lease = await jobs.claim("worker-one", lease_seconds=45)
    assert lease is not None
    backend = MemoryRunBackend()
    executor = RunExecutor(
        conversations,
        FakeModelProvider(),
        backend,
        backend,
        limits,
        ModelSettings(
            input_price_per_million_tokens=1,
            output_price_per_million_tokens=1,
        ),
        jobs=jobs,
    )

    await executor.execute(run.id, job_lease=lease)

    async with database.session_factory() as session:
        stored_run = await session.get(Run, run.id)
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        attempt = await session.scalar(
            select(RunJobAttempt).where(RunJobAttempt.job_id == job.id)
        )
    assert stored_run is not None and stored_run.status == RunStatus.COMPLETED
    assert job is not None and job.status == RunJobStatus.SUCCEEDED
    assert attempt is not None and attempt.outcome == RunJobStatus.SUCCEEDED.value


async def test_expired_lease_cannot_commit_run_result(durable_runtime) -> None:
    database, _, _, conversations, jobs, _ = durable_runtime
    run = await _create_run(durable_runtime)
    lease = await jobs.claim("expired-worker", lease_seconds=45)
    assert lease is not None
    assert await conversations.claim_run(run.id) is not None
    assert await jobs.mark_execution_started(lease)
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    completed = await conversations.complete_run(
        run.id,
        answer="must not commit",
        provider="fake",
        model="fake",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        job_lease=lease,
    )

    assert completed is False
    async with database.session_factory() as session:
        stored_run = await session.get(Run, run.id)
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
    assert stored_run is not None and stored_run.status == RunStatus.RUNNING
    assert job is not None and job.status == RunJobStatus.LEASED


async def test_pre_model_cancellation_rolls_back_durable_quota(durable_runtime) -> None:
    database, user, organization, conversations, jobs, limits = durable_runtime
    run = await _create_run(durable_runtime)
    redis = FakeRedis()
    reserved_tokens = limits.max_input_tokens + limits.max_output_tokens
    quota = RedisRunQuota(
        cast(Redis, redis),
        QuotaSettings(
            max_daily_tokens_per_user=reserved_tokens,
            max_daily_tokens_per_organization=reserved_tokens,
            max_daily_cost_usd_per_user=limits.max_cost_usd,
            max_daily_cost_usd_per_organization=limits.max_cost_usd,
        ),
        limits,
        lease_seconds=120,
    )
    await quota.reserve(
        organization.id,
        user.id,
        str(run.trace_id),
        budget_day=run.created_at.date(),
    )
    worker_quota = await quota.reserve(
        organization.id,
        user.id,
        str(run.trace_id),
        budget_day=run.created_at.date(),
        claim_existing=True,
    )
    await quota.acquire_concurrency(worker_quota)
    lease = await jobs.claim("cancel-race-worker", lease_seconds=45)
    assert lease is not None
    await conversations.request_cancel(user.id, organization.id, run.id)
    backend = MemoryRunBackend()
    executor = RunExecutor(
        conversations,
        FakeModelProvider(),
        backend,
        backend,
        limits,
        ModelSettings(),
        quota=quota,
        jobs=jobs,
    )

    await executor.execute(run.id, worker_quota, job_lease=lease)

    async with database.session_factory() as session:
        stored_run = await session.get(Run, run.id)
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
    assert stored_run is not None and stored_run.status == RunStatus.CANCELLED
    assert job is not None and job.status == RunJobStatus.CANCELLED
    replacement = await quota.reserve(
        organization.id,
        user.id,
        str(uuid4()),
        budget_day=run.created_at.date(),
    )
    await quota.rollback(replacement)
    await redis.aclose()


async def test_pre_execution_retries_use_exponential_backoff(durable_runtime) -> None:
    database, _, _, _, jobs, _ = durable_runtime
    run = await _create_run(durable_runtime)
    first = await jobs.claim("retry-worker-one", lease_seconds=45)
    assert first is not None
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await jobs.recover_expired(retry_delay_seconds=2, retry_jitter_ratio=0) == 1
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        first_delay = (_utc(job.available_at) - datetime.now(UTC)).total_seconds()
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
    assert 1.5 <= first_delay <= 2.1

    second = await jobs.claim("retry-worker-two", lease_seconds=45)
    assert second is not None
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await jobs.recover_expired(retry_delay_seconds=2, retry_jitter_ratio=0) == 1
    async with database.session_factory() as session:
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        second_delay = (_utc(job.available_at) - datetime.now(UTC)).total_seconds()
    assert 3.5 <= second_delay <= 4.1


async def test_expired_pre_execution_lease_is_requeued(durable_runtime) -> None:
    database, _, _, _, jobs, _ = durable_runtime
    run = await _create_run(durable_runtime)
    lease = await jobs.claim("lost-worker", lease_seconds=45)
    assert lease is not None
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await jobs.recover_expired(retry_delay_seconds=0.1) == 1
    async with database.session_factory() as session:
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
    assert job is not None and job.status == RunJobStatus.RETRY_WAIT
    assert job.execution_started_at is None


async def test_expired_execution_lease_fails_closed(durable_runtime) -> None:
    database, _, _, _, jobs, _ = durable_runtime
    run = await _create_run(durable_runtime)
    lease = await jobs.claim("lost-worker", lease_seconds=45)
    assert lease is not None
    assert await jobs.mark_execution_started(lease)
    async with database.session_factory() as session, session.begin():
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await jobs.recover_expired(retry_delay_seconds=0.1) == 1
    async with database.session_factory() as session:
        stored_run = await session.get(Run, run.id)
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
    assert stored_run is not None and stored_run.status == RunStatus.FAILED
    assert stored_run.error_code == "worker_lost_during_execution"
    assert job is not None and job.status == RunJobStatus.DEAD_LETTER


async def test_queued_job_can_be_cancelled_atomically(durable_runtime) -> None:
    database, _, _, _, jobs, _ = durable_runtime
    run = await _create_run(durable_runtime)

    assert await jobs.cancel_queued(run.id)

    async with database.session_factory() as session:
        stored_run = await session.get(Run, run.id)
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
    assert stored_run is not None and stored_run.status == RunStatus.CANCELLED
    assert job is not None and job.status == RunJobStatus.CANCELLED


async def test_worker_loop_executes_durable_job_and_reports_health(durable_runtime) -> None:
    database, _, _, conversations, jobs, limits = durable_runtime
    run = await _create_run(durable_runtime)
    backend = MemoryRunBackend()
    executor = RunExecutor(
        conversations,
        FakeModelProvider(),
        backend,
        backend,
        limits,
        ModelSettings(
            input_price_per_million_tokens=1,
            output_price_per_million_tokens=1,
        ),
        jobs=jobs,
    )
    redis = FakeRedis()
    registry = RedisWorkerRegistry(cast(Redis, redis))
    worker = RunWorker(
        jobs,
        executor,
        registry,
        ExecutionSettings(
            lease_seconds=15,
            heartbeat_seconds=1,
            worker_stale_seconds=5,
            poll_interval_seconds=0.1,
        ),
        max_concurrency=1,
        worker_id="worker-test",
    )
    worker_task = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(100):
            async with database.session_factory() as session:
                job = await session.scalar(select(RunJob).where(RunJob.run_id == run.id))
            if job is not None and job.status == RunJobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.02)
        assert job is not None and job.status == RunJobStatus.SUCCEEDED
        assert await registry.has_live_workers()
    finally:
        worker.stop()
        await worker_task
        await redis.aclose()


async def test_external_worker_api_requires_health_and_only_persists_job(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    runtime.settings.execution = ExecutionSettings(mode=ExecutionMode.EXTERNAL_WORKER)
    conversations = ConversationService(
        runtime.services.database.session_factory,
        runtime.services.identities,
        runtime.settings.limits,
        runtime.services.audit,
        durable_jobs_enabled=True,
    )
    jobs = RunJobService(runtime.services.database.session_factory, runtime.services.audit)
    redis = FakeRedis()
    registry = RedisWorkerRegistry(cast(Redis, redis))
    runtime.services.conversations = conversations
    runtime.services.jobs = jobs
    runtime.services.worker_registry = registry
    conversation = await runtime.client.post(
        "/api/v1/conversations",
        headers=runtime.headers,
        json={"title": "External Worker"},
    )
    conversation_id = conversation.json()["id"]

    unavailable = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=runtime.headers,
        json={"content": "wait for worker"},
    )
    assert unavailable.status_code == 503

    await registry.beat("worker-api-test", stale_seconds=30)
    accepted = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=runtime.headers,
        json={"content": "persist only"},
    )
    assert accepted.status_code == 202
    run_id = UUID(accepted.json()["id"])
    await asyncio.sleep(0)
    async with runtime.services.database.session_factory() as session:
        job = await session.scalar(select(RunJob).where(RunJob.run_id == run_id))
        run = await session.get(Run, run_id)
    assert job is not None and job.status == RunJobStatus.QUEUED
    assert run is not None and run.status == RunStatus.QUEUED
    await redis.aclose()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
