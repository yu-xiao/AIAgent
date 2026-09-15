"""Independent durable Run Worker and Redis liveness registry."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis

from ai_agent.config import ExecutionSettings
from ai_agent.errors import QuotaExceededError
from ai_agent.governance.quota import RedisRunQuota, RunQuotaLease
from ai_agent.observability.metrics import LIVE_WORKERS
from ai_agent.runs.executor import RunExecutor
from ai_agent.runs.jobs import JobLease, RunJobService

logger = logging.getLogger(__name__)


class RedisWorkerRegistry:
    def __init__(self, redis: Redis, prefix: str = "ai-agent:workers") -> None:
        self._redis = redis
        self._key = f"{prefix}:heartbeats"

    async def beat(self, worker_id: str, *, stale_seconds: int) -> None:
        now = time.time()
        await cast(Awaitable[int], self._redis.zremrangebyscore(self._key, "-inf", now))
        await cast(
            Awaitable[int],
            self._redis.zadd(self._key, {worker_id: now + stale_seconds}),
        )
        await cast(Awaitable[bool], self._redis.expire(self._key, stale_seconds * 2))
        LIVE_WORKERS.set(await self._redis.zcard(self._key))

    async def remove(self, worker_id: str) -> None:
        await cast(Awaitable[int], self._redis.zrem(self._key, worker_id))
        LIVE_WORKERS.set(await self._redis.zcard(self._key))

    async def has_live_workers(self) -> bool:
        now = time.time()
        await cast(Awaitable[int], self._redis.zremrangebyscore(self._key, "-inf", now))
        count = await self._redis.zcard(self._key)
        LIVE_WORKERS.set(count)
        return bool(count)


class RunWorker:
    def __init__(
        self,
        jobs: RunJobService,
        executor: RunExecutor,
        registry: RedisWorkerRegistry,
        settings: ExecutionSettings,
        *,
        max_concurrency: int,
        quota: RedisRunQuota | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._jobs = jobs
        self._executor = executor
        self._registry = registry
        self._settings = settings
        self._max_concurrency = max_concurrency
        self._quota = quota
        self._worker_id = worker_id or f"worker-{secrets.token_hex(8)}"
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        heartbeat = asyncio.create_task(self._worker_heartbeat(), name="worker-heartbeat")
        try:
            while not self._stopping.is_set():
                await self._jobs.recover_expired(
                    retry_delay_seconds=self._settings.retry_delay_seconds,
                    retry_max_delay_seconds=self._settings.retry_max_delay_seconds,
                    retry_jitter_ratio=self._settings.retry_jitter_ratio,
                )
                await self._jobs.observe_queue()
                self._discard_finished_tasks()
                while len(self._tasks) < self._max_concurrency:
                    lease = await self._jobs.claim(
                        self._worker_id,
                        lease_seconds=self._settings.lease_seconds,
                    )
                    if lease is None:
                        break
                    task = asyncio.create_task(
                        self._execute(lease),
                        name=f"durable-run-{lease.run_id}",
                    )
                    self._tasks.add(task)
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self._settings.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._registry.remove(self._worker_id)
            await self._drain()

    def stop(self) -> None:
        self._stopping.set()

    async def _execute(self, lease: JobLease) -> None:
        heartbeat: asyncio.Task[None] | None = None
        quota_lease: RunQuotaLease | None = None
        try:
            if self._quota is not None:
                try:
                    quota_lease = await self._quota.reserve(
                        lease.organization_id,
                        lease.user_id,
                        str(lease.trace_id),
                        budget_day=lease.budget_day,
                        claim_existing=True,
                    )
                except QuotaExceededError:
                    await self._executor.reject_submission(
                        lease.run_id,
                        str(lease.trace_id),
                        "quota_recovery_denied",
                        "Run could not be executed within the current quota.",
                        job_lease=lease,
                    )
                    return
                try:
                    await self._quota.acquire_concurrency(quota_lease)
                except QuotaExceededError:
                    await self._jobs.release_for_capacity(
                        lease,
                        retry_delay_seconds=self._settings.retry_delay_seconds,
                        retry_jitter_ratio=self._settings.retry_jitter_ratio,
                    )
                    return
            heartbeat = asyncio.create_task(
                self._lease_heartbeat(lease),
                name=f"run-lease-heartbeat-{lease.run_id}",
            )
            await self._executor.execute(lease.run_id, quota_lease, job_lease=lease)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Durable Run execution failed", extra={"run_id": str(lease.run_id)})
            if self._quota is not None and quota_lease is not None:
                await self._quota.abandon(quota_lease)
            await self._jobs.release_before_execution(
                lease,
                error_code="worker_start_failed",
                retry_delay_seconds=self._settings.retry_delay_seconds,
                retry_max_delay_seconds=self._settings.retry_max_delay_seconds,
                retry_jitter_ratio=self._settings.retry_jitter_ratio,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._jobs.finish_from_run(lease)

    async def _worker_heartbeat(self) -> None:
        while True:
            await self._registry.beat(
                self._worker_id,
                stale_seconds=self._settings.worker_stale_seconds,
            )
            await asyncio.sleep(self._settings.heartbeat_seconds)

    async def _lease_heartbeat(self, lease: JobLease) -> None:
        while True:
            await asyncio.sleep(self._settings.heartbeat_seconds)
            active = await self._jobs.heartbeat(
                lease,
                lease_seconds=self._settings.lease_seconds,
            )
            if not active:
                return

    def _discard_finished_tasks(self) -> None:
        for task in list(self._tasks):
            if task.done():
                self._tasks.remove(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.error("Unhandled durable Run task failure", exc_info=task.exception())

    async def _drain(self) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(
            self._tasks,
            timeout=self._executor.shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
