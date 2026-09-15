"""Policy-enforcing MCP Tool Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

from mcp.types import CallToolResult
from redis.asyncio import Redis

from ai_agent.errors import (
    AuthorizationError,
    CircuitOpenError,
    GatewayTimeoutError,
    McpConnectionError,
    ProtocolValidationError,
    RateLimitExceededError,
)
from ai_agent.mcp.models import Citation, RunContext, ToolDefinition, ToolResult
from ai_agent.mcp.schema import validate_instance
from ai_agent.mcp.tool_catalog import ResolvedTool, ToolCatalogService
from ai_agent.observability.metrics import AUDIT_WRITE_FAILURES, MCP_CALLS, MCP_DURATION
from ai_agent.observability.tracing import operation_span


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, *, window_seconds: float = 60.0) -> None:
        now = time.monotonic()
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                raise RateLimitExceededError("MCP Tool rate limit exceeded.")
            events.append(now)


class CircuitBreaker:
    def __init__(self) -> None:
        self._failures: dict[str, int] = defaultdict(int)
        self._opened_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def before(self, key: str) -> None:
        async with self._lock:
            opened_until = self._opened_until.get(key, 0)
            if opened_until > time.monotonic():
                raise CircuitOpenError("MCP Server circuit breaker is open.")
            if opened_until:
                self._opened_until.pop(key, None)

    async def success(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)
            self._opened_until.pop(key, None)

    async def failure(self, key: str, threshold: int, recovery_seconds: float) -> None:
        async with self._lock:
            self._failures[key] += 1
            if self._failures[key] >= threshold:
                self._opened_until[key] = time.monotonic() + recovery_seconds


class RedisSlidingWindowRateLimiter:
    """Sliding-window limiter shared by all API instances."""

    _SCRIPT = """
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - tonumber(ARGV[2]))
    if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
    redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
    """

    def __init__(self, redis: Redis, prefix: str = "ai-agent:mcp:rate") -> None:
        self._redis = redis
        self._prefix = prefix

    async def check(self, key: str, limit: int, *, window_seconds: float = 60.0) -> None:
        now_ms = int(time.time() * 1_000)
        member = f"{now_ms}:{time.monotonic_ns()}"
        allowed = await _redis_eval(
            self._redis,
            self._SCRIPT,
            1,
            f"{self._prefix}:{key}",
            now_ms,
            max(round(window_seconds * 1_000), 1),
            limit,
            member,
        )
        if not bool(allowed):
            raise RateLimitExceededError("MCP Tool rate limit exceeded.")


class RedisCircuitBreaker:
    """Failure threshold and open state shared by all API instances."""

    _FAILURE_SCRIPT = """
    local failures = redis.call('INCR', KEYS[1])
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
    if failures >= tonumber(ARGV[2]) then
      redis.call('SET', KEYS[2], '1', 'PX', ARGV[1])
    end
    return failures
    """

    def __init__(self, redis: Redis, prefix: str = "ai-agent:mcp:circuit") -> None:
        self._redis = redis
        self._prefix = prefix

    async def before(self, key: str) -> None:
        if await self._redis.exists(self._open_key(key)):
            raise CircuitOpenError("MCP Server circuit breaker is open.")

    async def success(self, key: str) -> None:
        await self._redis.delete(self._failure_key(key), self._open_key(key))

    async def failure(self, key: str, threshold: int, recovery_seconds: float) -> None:
        await _redis_eval(
            self._redis,
            self._FAILURE_SCRIPT,
            2,
            self._failure_key(key),
            self._open_key(key),
            max(round(recovery_seconds * 1_000), 1),
            threshold,
        )

    def _failure_key(self, key: str) -> str:
        return f"{self._prefix}:failures:{key}"

    def _open_key(self, key: str) -> str:
        return f"{self._prefix}:open:{key}"


class McpGateway:
    def __init__(
        self,
        catalog: ToolCatalogService,
        *,
        rate_limiter: SlidingWindowRateLimiter | RedisSlidingWindowRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | RedisCircuitBreaker | None = None,
        audit: Any | None = None,
    ) -> None:
        self._catalog = catalog
        self._rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self._circuit_breaker = circuit_breaker or CircuitBreaker()
        self._audit = audit
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}

    async def list_tools(
        self,
        context: RunContext,
        tool_set: str = "",
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> list[ToolDefinition]:
        if context.has_expired():
            raise GatewayTimeoutError("MCP Tool request deadline has expired.")
        return await self._catalog.list_tools(context, tool_set, system_code, personal_only)

    async def call(
        self,
        context: RunContext,
        tool_name: str,
        arguments: dict[str, Any],
        system_code: str | None = None,
        personal_only: bool = False,
    ) -> ToolResult:
        if context.has_expired():
            raise GatewayTimeoutError("MCP Tool request deadline has expired.")
        resolved = await self._catalog.resolve_tool(context, tool_name, system_code, personal_only)
        key = f"{context.organization_id}:{resolved.server.id}"
        started = time.monotonic()
        try:
            self._validate_input(resolved.definition, arguments)
            await self._rate_limiter.check(
                f"{key}:{context.user_id}", resolved.server.rate_limit_per_minute
            )
            await self._circuit_breaker.before(key)
            if self._semaphore_limits.get(key) != resolved.server.max_concurrency:
                self._semaphores[key] = asyncio.Semaphore(resolved.server.max_concurrency)
                self._semaphore_limits[key] = resolved.server.max_concurrency
            semaphore = self._semaphores[key]
            async with semaphore:
                client = self._catalog.client_for(resolved, context)
                timeout = resolved.server.timeout_seconds
                if context.deadline is not None:
                    timeout = min(
                        timeout, max((context.deadline - datetime.now(UTC)).total_seconds(), 0.001)
                    )
                async with asyncio.timeout(timeout):
                    with operation_span(
                        "mcp.tool.call",
                        trace_id=context.trace_id,
                        attributes={
                            "mcp.server": resolved.server.code,
                            "mcp.tool": resolved.definition.name,
                        },
                    ):
                        raw = await client.call_tool(tool_name, arguments)
            result = self._result(
                resolved,
                raw,
                context,
                resource_id=_resource_id(arguments),
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            if (
                resolved.server.system_code == "permission-system"
                and result.is_error
                and _is_permission_denial(result)
            ):
                await self._circuit_breaker.success(key)
                await self._write_audit(context, resolved, "rejected", started, "permission")
                self._observe(resolved, "rejected", started)
                raise AuthorizationError("PermissionSystem denied the requested data scope.")
            await self._circuit_breaker.success(key)
            await self._write_audit(context, resolved, "succeeded", started, None)
            self._observe(resolved, "succeeded", started)
            return result
        except TimeoutError as exc:
            await self._circuit_breaker.failure(
                key,
                resolved.server.circuit_breaker_threshold,
                resolved.server.circuit_breaker_recovery_seconds,
            )
            await self._write_audit(context, resolved, "timeout", started, "timeout")
            self._observe(resolved, "timeout", started)
            raise GatewayTimeoutError("MCP Tool request timed out.") from exc
        except (ProtocolValidationError, RateLimitExceededError, CircuitOpenError):
            await self._write_audit(context, resolved, "rejected", started, "policy")
            self._observe(resolved, "rejected", started)
            raise
        except AuthorizationError:
            raise
        except Exception as exc:
            await self._circuit_breaker.failure(
                key,
                resolved.server.circuit_breaker_threshold,
                resolved.server.circuit_breaker_recovery_seconds,
            )
            await self._write_audit(context, resolved, "failed", started, "connection")
            self._observe(resolved, "failed", started)
            if isinstance(exc, McpConnectionError):
                raise
            raise McpConnectionError("MCP Tool call failed.") from exc

    def _validate_input(self, definition: ToolDefinition, arguments: dict[str, Any]) -> None:
        validate_instance(arguments, definition.input_schema, label="MCP Tool input")

    def _result(
        self,
        resolved: ResolvedTool,
        raw: CallToolResult,
        context: RunContext,
        *,
        resource_id: str | None = None,
        duration_ms: float | None = None,
    ) -> ToolResult:
        structured = _to_json(raw.structured_content)
        content = [_to_json(item) for item in raw.content]
        payload = structured if structured is not None else content
        if resolved.definition.output_schema:
            validate_instance(payload, resolved.definition.output_schema, label="MCP Tool output")
        encoded = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        truncated = len(encoded.encode("utf-8")) > resolved.server.response_size_limit
        if truncated:
            payload = {
                "truncated": True,
                "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            }
            structured = payload if structured is not None else None
            content = [] if structured is not None else [payload]
        else:
            payload = _sanitize(payload)
            if structured is not None:
                structured = payload
            else:
                content = payload if isinstance(payload, list) else [payload]
        citation = Citation(
            source_system=resolved.server.system_code,
            server_code=resolved.server.code,
            tool_name=resolved.definition.name,
            queried_at=datetime.now(UTC),
            trace_id=context.trace_id,
            resource_id=resource_id,
            partial=truncated,
        )
        return ToolResult(
            name=resolved.definition.name,
            is_error=bool(raw.is_error),
            structured_content=structured,
            content=content,
            trace_id=context.trace_id,
            truncated=truncated,
            citation=citation,
            citations=[citation],
            duration_ms=duration_ms,
        )

    async def _write_audit(
        self,
        context: RunContext,
        resolved: ResolvedTool,
        status: str,
        started: float,
        error: str | None,
    ) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(
                context=context,
                server=resolved.server,
                tool=resolved.definition,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                error=error,
            )
        except Exception:
            # Audit outages must be observable by operations, but must not turn a
            # completed business query into a second, misleading MCP failure.
            AUDIT_WRITE_FAILURES.inc()

    @staticmethod
    def _observe(resolved: ResolvedTool, status: str, started: float) -> None:
        MCP_CALLS.labels(server=resolved.server.code, status=status).inc()
        MCP_DURATION.labels(server=resolved.server.code).observe(time.monotonic() - started)


def _to_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_json(model_dump(mode="json"))
    return str(value)


def _sanitize(value: Any) -> Any:
    sensitive = {
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "token",
        "credential",
        "authorization",
        "password",
        "secret",
        "cookie",
    }
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(part in key.lower().replace("_", "") for part in sensitive)
            else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _is_permission_denial(result: ToolResult) -> bool:
    payload = result.structured_content if result.structured_content is not None else result.content
    text = json.dumps(payload, ensure_ascii=False, default=str).lower()
    return any(
        marker in text
        for marker in ("forbidden", "unauthorized", "access denied", "permission denied")
    )


def _resource_id(arguments: dict[str, Any]) -> str | None:
    for key in ("dataset_id", "resource_id"):
        value = arguments.get(key)
        if isinstance(value, (str, int)) and len(str(value)) <= 300:
            return str(value)
    return None


async def _redis_eval(redis: Redis, script: str, key_count: int, *values: str | int | float) -> Any:
    result = redis.eval(script, key_count, *values)
    return await cast(Awaitable[Any], result)


# Stable interface name used by the Agent orchestration layer.
ToolGateway = McpGateway
