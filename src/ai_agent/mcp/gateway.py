"""Policy-enforcing MCP Tool Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from mcp.types import CallToolResult

from ai_agent.errors import (
    AuthorizationError,
    CircuitOpenError,
    GatewayTimeoutError,
    McpConnectionError,
    ProtocolValidationError,
    RateLimitExceededError,
)
from ai_agent.mcp.models import Citation, RunContext, ToolDefinition, ToolResult
from ai_agent.mcp.tool_catalog import ResolvedTool, ToolCatalogService


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


class McpGateway:
    def __init__(
        self,
        catalog: ToolCatalogService,
        *,
        rate_limiter: SlidingWindowRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
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
        self._validate_input(resolved.definition, arguments)
        key = f"{context.organization_id}:{resolved.server.id}"
        await self._rate_limiter.check(
            f"{key}:{context.user_id}", resolved.server.rate_limit_per_minute
        )
        await self._circuit_breaker.before(key)
        if self._semaphore_limits.get(key) != resolved.server.max_concurrency:
            self._semaphores[key] = asyncio.Semaphore(resolved.server.max_concurrency)
            self._semaphore_limits[key] = resolved.server.max_concurrency
        semaphore = self._semaphores[key]
        started = time.monotonic()
        try:
            async with semaphore:
                client = self._catalog.client_for(resolved, context)
                timeout = resolved.server.timeout_seconds
                if context.deadline is not None:
                    timeout = min(
                        timeout, max((context.deadline - datetime.now(UTC)).total_seconds(), 0.001)
                    )
                async with asyncio.timeout(timeout):
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
                await self._write_audit(context, resolved, "rejected", started, "permission")
                raise AuthorizationError("PermissionSystem denied the requested data scope.")
            await self._circuit_breaker.success(key)
            await self._write_audit(context, resolved, "succeeded", started, None)
            return result
        except TimeoutError as exc:
            await self._circuit_breaker.failure(
                key,
                resolved.server.circuit_breaker_threshold,
                resolved.server.circuit_breaker_recovery_seconds,
            )
            await self._write_audit(context, resolved, "timeout", started, "timeout")
            raise GatewayTimeoutError("MCP Tool request timed out.") from exc
        except (ProtocolValidationError, RateLimitExceededError, CircuitOpenError):
            await self._write_audit(context, resolved, "rejected", started, "policy")
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
            if isinstance(exc, McpConnectionError):
                raise
            raise McpConnectionError("MCP Tool call failed.") from exc

    def _validate_input(self, definition: ToolDefinition, arguments: dict[str, Any]) -> None:
        _validate_json_schema(arguments, definition.input_schema, path="$")

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
            _validate_json_schema(payload, resolved.definition.output_schema, path="$")
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
            return


def _validate_json_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not schema:
        return
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
        for required in schema.get("required", []):
            if required not in value:
                raise ProtocolValidationError(
                    f"Tool schema validation failed at {path}.{required}."
                )
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ProtocolValidationError(f"Tool schema rejected unknown fields at {path}.")
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                _validate_json_schema(value[name], child, path=f"{path}.{name}")
    elif expected == "array":
        if not isinstance(value, list):
            raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, path=f"{path}[{index}]")
    elif expected == "string" and not isinstance(value, str):
        raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
    elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
    elif expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ProtocolValidationError(f"Tool schema validation failed at {path}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolValidationError(f"Tool schema enum validation failed at {path}.")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ProtocolValidationError(f"Tool schema length validation failed at {path}.")


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


# Stable interface name used by the Agent orchestration layer.
ToolGateway = McpGateway
