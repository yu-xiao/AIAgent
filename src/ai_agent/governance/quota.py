"""Redis-backed distributed Run admission and daily budget accounting."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis

from ai_agent.config import QuotaSettings, RunLimitSettings
from ai_agent.errors import QuotaExceededError
from ai_agent.observability.metrics import QUOTA_REJECTIONS

_ACQUIRE = """
if redis.call('EXISTS', KEYS[8]) == 1 then return 'disabled' end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', tonumber(ARGV[1]) - 60000)
if redis.call('ZSCORE', KEYS[1], ARGV[3]) then return 'existing' end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return 'user_concurrency' end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[5]) then return 'organization_concurrency' end
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[6]) then return 'user_rate' end
if tonumber(redis.call('GET', KEYS[4]) or '0') + tonumber(ARGV[7]) > tonumber(ARGV[9]) then
  return 'user_tokens'
end
if tonumber(redis.call('GET', KEYS[5]) or '0') + tonumber(ARGV[7]) > tonumber(ARGV[10]) then
  return 'organization_tokens'
end
if tonumber(redis.call('GET', KEYS[6]) or '0') + tonumber(ARGV[8]) > tonumber(ARGV[11]) then
  return 'user_cost'
end
if tonumber(redis.call('GET', KEYS[7]) or '0') + tonumber(ARGV[8]) > tonumber(ARGV[12]) then
  return 'organization_cost'
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[3], ARGV[1], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[13])
redis.call('EXPIRE', KEYS[2], ARGV[13])
redis.call('EXPIRE', KEYS[3], 120)
redis.call('INCRBY', KEYS[4], ARGV[7])
redis.call('INCRBY', KEYS[5], ARGV[7])
redis.call('INCRBY', KEYS[6], ARGV[8])
redis.call('INCRBY', KEYS[7], ARGV[8])
redis.call('EXPIRE', KEYS[4], ARGV[14])
redis.call('EXPIRE', KEYS[5], ARGV[14])
redis.call('EXPIRE', KEYS[6], ARGV[14])
redis.call('EXPIRE', KEYS[7], ARGV[14])
return 'ok'
"""

_SETTLE = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
local token_adjustment = tonumber(ARGV[2]) - tonumber(ARGV[4])
local cost_adjustment = tonumber(ARGV[3]) - tonumber(ARGV[5])
redis.call('INCRBY', KEYS[3], token_adjustment)
redis.call('INCRBY', KEYS[4], token_adjustment)
redis.call('INCRBY', KEYS[5], cost_adjustment)
redis.call('INCRBY', KEYS[6], cost_adjustment)
return 'ok'
"""

_ROLLBACK = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('DECRBY', KEYS[4], ARGV[2])
redis.call('DECRBY', KEYS[5], ARGV[2])
redis.call('DECRBY', KEYS[6], ARGV[3])
redis.call('DECRBY', KEYS[7], ARGV[3])
return 'ok'
"""

_RELEASE_CONCURRENCY = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 'ok'
"""

_RESERVE = """
if redis.call('EXISTS', KEYS[6]) == 1 then return 'disabled' end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - 60000)
if redis.call('EXISTS', KEYS[7]) == 1 then return 'existing' end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 'user_rate' end
if tonumber(redis.call('GET', KEYS[2]) or '0') + tonumber(ARGV[4]) > tonumber(ARGV[6]) then
  return 'user_tokens'
end
if tonumber(redis.call('GET', KEYS[3]) or '0') + tonumber(ARGV[4]) > tonumber(ARGV[7]) then
  return 'organization_tokens'
end
if tonumber(redis.call('GET', KEYS[4]) or '0') + tonumber(ARGV[5]) > tonumber(ARGV[8]) then
  return 'user_cost'
end
if tonumber(redis.call('GET', KEYS[5]) or '0') + tonumber(ARGV[5]) > tonumber(ARGV[9]) then
  return 'organization_cost'
end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], 120)
redis.call('INCRBY', KEYS[2], ARGV[4])
redis.call('INCRBY', KEYS[3], ARGV[4])
redis.call('INCRBY', KEYS[4], ARGV[5])
redis.call('INCRBY', KEYS[5], ARGV[5])
redis.call('EXPIRE', KEYS[2], ARGV[10])
redis.call('EXPIRE', KEYS[3], ARGV[10])
redis.call('EXPIRE', KEYS[4], ARGV[10])
redis.call('EXPIRE', KEYS[5], ARGV[10])
redis.call('SET', KEYS[7], '1', 'EX', ARGV[10])
return 'ok'
"""

_ACQUIRE_CONCURRENCY = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[3]) then return 'existing' end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return 'user_concurrency' end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[5]) then return 'organization_concurrency' end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[6])
redis.call('EXPIRE', KEYS[2], ARGV[6])
return 'ok'
"""


@dataclass(frozen=True, slots=True)
class RunQuotaLease:
    lease_id: str
    organization_id: UUID
    user_id: UUID
    day: str
    reserved_tokens: int
    reserved_cost_micros: int
    owns_reservation: bool = True
    durable_reservation: bool = False


class RedisRunQuota:
    """Atomically enforces quotas shared by every API instance."""

    def __init__(
        self,
        redis: Redis,
        settings: QuotaSettings,
        run_limits: RunLimitSettings,
        *,
        lease_seconds: int,
        prefix: str = "ai-agent:governance",
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._run_limits = run_limits
        self._lease_seconds = lease_seconds
        self._prefix = prefix

    async def acquire(
        self,
        organization_id: UUID,
        user_id: UUID,
        lease_id: str,
        *,
        budget_day: date | None = None,
        claim_existing: bool = False,
    ) -> RunQuotaLease:
        now = datetime.now(UTC)
        now_ms = int(now.timestamp() * 1_000)
        expires_ms = now_ms + self._lease_seconds * 1_000
        day = (budget_day or now.date()).isoformat()
        reserved_tokens = self._run_limits.max_input_tokens + self._run_limits.max_output_tokens
        reserved_cost_micros = _cost_micros(self._run_limits.max_cost_usd)
        keys = self._keys(organization_id, user_id, day)
        result = await _eval(
            self._redis,
            _ACQUIRE,
            len(keys),
            *keys,
            now_ms,
            expires_ms,
            lease_id,
            self._settings.max_concurrent_runs_per_user,
            self._settings.max_concurrent_runs_per_organization,
            self._settings.max_runs_per_user_per_minute,
            reserved_tokens,
            reserved_cost_micros,
            self._settings.max_daily_tokens_per_user,
            self._settings.max_daily_tokens_per_organization,
            _cost_micros(self._settings.max_daily_cost_usd_per_user),
            _cost_micros(self._settings.max_daily_cost_usd_per_organization),
            self._lease_seconds + 60,
            _seconds_until_budget_expiry(now),
        )
        outcome = _decode(result)
        if outcome not in {"ok", "existing"}:
            QUOTA_REJECTIONS.labels(reason=outcome).inc()
            raise QuotaExceededError(_quota_message(outcome))
        return RunQuotaLease(
            lease_id=lease_id,
            organization_id=organization_id,
            user_id=user_id,
            day=day,
            reserved_tokens=reserved_tokens,
            reserved_cost_micros=reserved_cost_micros,
            owns_reservation=outcome == "ok" or claim_existing,
        )

    async def reserve(
        self,
        organization_id: UUID,
        user_id: UUID,
        lease_id: str,
        *,
        budget_day: date | None = None,
        claim_existing: bool = False,
    ) -> RunQuotaLease:
        now = datetime.now(UTC)
        now_ms = int(now.timestamp() * 1_000)
        day = (budget_day or now.date()).isoformat()
        reserved_tokens = self._run_limits.max_input_tokens + self._run_limits.max_output_tokens
        reserved_cost_micros = _cost_micros(self._run_limits.max_cost_usd)
        keys = self._keys(organization_id, user_id, day)
        result = await _eval(
            self._redis,
            _RESERVE,
            7,
            keys[2],
            keys[3],
            keys[4],
            keys[5],
            keys[6],
            keys[7],
            self._reservation_key(lease_id),
            now_ms,
            lease_id,
            self._settings.max_runs_per_user_per_minute,
            reserved_tokens,
            reserved_cost_micros,
            self._settings.max_daily_tokens_per_user,
            self._settings.max_daily_tokens_per_organization,
            _cost_micros(self._settings.max_daily_cost_usd_per_user),
            _cost_micros(self._settings.max_daily_cost_usd_per_organization),
            _seconds_until_budget_expiry(now),
        )
        outcome = _decode(result)
        if outcome not in {"ok", "existing"}:
            QUOTA_REJECTIONS.labels(reason=outcome).inc()
            raise QuotaExceededError(_quota_message(outcome))
        return RunQuotaLease(
            lease_id=lease_id,
            organization_id=organization_id,
            user_id=user_id,
            day=day,
            reserved_tokens=reserved_tokens,
            reserved_cost_micros=reserved_cost_micros,
            owns_reservation=outcome == "ok" or claim_existing,
            durable_reservation=True,
        )

    def reservation_lease(
        self,
        organization_id: UUID,
        user_id: UUID,
        lease_id: str,
        *,
        budget_day: date,
    ) -> RunQuotaLease:
        return RunQuotaLease(
            lease_id=lease_id,
            organization_id=organization_id,
            user_id=user_id,
            day=budget_day.isoformat(),
            reserved_tokens=(
                self._run_limits.max_input_tokens + self._run_limits.max_output_tokens
            ),
            reserved_cost_micros=_cost_micros(self._run_limits.max_cost_usd),
            durable_reservation=True,
        )

    async def acquire_concurrency(self, lease: RunQuotaLease) -> None:
        now = datetime.now(UTC)
        now_ms = int(now.timestamp() * 1_000)
        expires_ms = now_ms + self._lease_seconds * 1_000
        keys = self._keys(lease.organization_id, lease.user_id, lease.day)
        result = await _eval(
            self._redis,
            _ACQUIRE_CONCURRENCY,
            2,
            keys[0],
            keys[1],
            now_ms,
            expires_ms,
            lease.lease_id,
            self._settings.max_concurrent_runs_per_user,
            self._settings.max_concurrent_runs_per_organization,
            self._lease_seconds + 60,
        )
        outcome = _decode(result)
        if outcome not in {"ok", "existing"}:
            QUOTA_REJECTIONS.labels(reason=outcome).inc()
            raise QuotaExceededError(_quota_message(outcome))

    async def settle(self, lease: RunQuotaLease, *, tokens: int, cost_usd: float) -> None:
        keys = self._keys(lease.organization_id, lease.user_id, lease.day)
        await _eval(
            self._redis,
            _SETTLE,
            6,
            keys[0],
            keys[1],
            keys[3],
            keys[4],
            keys[5],
            keys[6],
            lease.lease_id,
            max(tokens, 0),
            _cost_micros(max(cost_usd, 0.0)),
            lease.reserved_tokens,
            lease.reserved_cost_micros,
        )
        await self._redis.delete(self._reservation_key(lease.lease_id))

    async def rollback(self, lease: RunQuotaLease) -> None:
        if not lease.owns_reservation:
            return
        if lease.durable_reservation and not await self._redis.exists(
            self._reservation_key(lease.lease_id)
        ):
            return
        keys = self._keys(lease.organization_id, lease.user_id, lease.day)
        await _eval(
            self._redis,
            _ROLLBACK,
            7,
            keys[0],
            keys[1],
            keys[2],
            keys[3],
            keys[4],
            keys[5],
            keys[6],
            lease.lease_id,
            lease.reserved_tokens,
            lease.reserved_cost_micros,
        )
        await self._redis.delete(self._reservation_key(lease.lease_id))

    async def abandon(self, lease: RunQuotaLease) -> None:
        """Release concurrency but preserve pessimistic cost after unmetered failures."""

        keys = self._keys(lease.organization_id, lease.user_id, lease.day)
        await _eval(
            self._redis,
            _RELEASE_CONCURRENCY,
            2,
            keys[0],
            keys[1],
            lease.lease_id,
        )

    async def runs_enabled(self) -> bool:
        return not bool(await self._redis.exists(self._disabled_key))

    async def disable_runs(self) -> None:
        await self._redis.set(self._disabled_key, "1")

    async def enable_runs(self) -> None:
        await self._redis.delete(self._disabled_key)

    @property
    def _disabled_key(self) -> str:
        return f"{self._prefix}:runs-disabled"

    def _keys(self, organization_id: UUID, user_id: UUID, day: str) -> list[str]:
        return [
            f"{self._prefix}:concurrent:organization:{organization_id}",
            f"{self._prefix}:concurrent:user:{user_id}",
            f"{self._prefix}:rate:user:{user_id}",
            f"{self._prefix}:daily:{day}:tokens:user:{user_id}",
            f"{self._prefix}:daily:{day}:tokens:organization:{organization_id}",
            f"{self._prefix}:daily:{day}:cost:user:{user_id}",
            f"{self._prefix}:daily:{day}:cost:organization:{organization_id}",
            self._disabled_key,
        ]

    def _reservation_key(self, lease_id: str) -> str:
        return f"{self._prefix}:reservation:{lease_id}"


def _seconds_until_budget_expiry(now: datetime) -> int:
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return max(int((tomorrow - now).total_seconds()) + 3_600, 3_600)


def _cost_micros(value: float) -> int:
    return round(value * 1_000_000)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _quota_message(reason: str) -> str:
    messages = {
        "disabled": "Agent Runs are disabled by the operational switch.",
        "user_concurrency": "User concurrent Run quota exceeded.",
        "organization_concurrency": "Organization concurrent Run quota exceeded.",
        "user_rate": "User Run rate quota exceeded.",
        "user_tokens": "User daily Token quota exceeded.",
        "organization_tokens": "Organization daily Token quota exceeded.",
        "user_cost": "User daily cost quota exceeded.",
        "organization_cost": "Organization daily cost quota exceeded.",
    }
    return messages.get(reason, "Run quota admission failed.")


async def _eval(redis: Redis, script: str, key_count: int, *values: str | int | float) -> Any:
    result = redis.eval(script, key_count, *values)
    return await cast(Awaitable[Any], result)
