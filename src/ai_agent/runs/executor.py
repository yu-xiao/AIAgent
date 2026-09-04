"""Bounded single-process P1 Run executor."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from prometheus_client import Counter, Histogram

from ai_agent.agents import SingleAgent
from ai_agent.config import ModelSettings, RunLimitSettings
from ai_agent.conversations.service import ConversationService
from ai_agent.errors import ModelProviderError, RunLimitError
from ai_agent.models import ModelMessage, ModelProvider
from ai_agent.persistence.models import Message, RunStatus
from ai_agent.runs.events import RunControl, RunEventBus

logger = logging.getLogger(__name__)

RUNS_TOTAL = Counter("ai_agent_runs_total", "Agent Runs by final status", ["status"])
RUN_DURATION = Histogram("ai_agent_run_duration_seconds", "Agent Run duration")


class RunCancelledError(Exception):
    pass


class RunExecutor:
    def __init__(
        self,
        conversations: ConversationService,
        provider: ModelProvider,
        events: RunEventBus,
        control: RunControl,
        limits: RunLimitSettings,
        model_settings: ModelSettings,
    ) -> None:
        self._conversations = conversations
        self._provider = provider
        self._agent = SingleAgent(provider)
        self._events = events
        self._control = control
        self._limits = limits
        self._model_settings = model_settings
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def submit(self, run_id: UUID) -> None:
        if self._closed:
            raise RuntimeError("Run executor is closed.")
        task = asyncio.create_task(self._execute(run_id), name=f"agent-run-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _execute(self, run_id: UUID) -> None:
        run = await self._conversations.claim_run(run_id)
        if run is None:
            return
        trace_id = str(run.trace_id)
        await self._events.publish(run_id, "run.started", {"trace_id": trace_id})
        try:
            if await self._control.is_cancel_requested(run_id):
                raise RunCancelledError
            stored_messages = await self._conversations.load_run_messages(run)
            messages = self._bounded_messages(stored_messages)
            estimated_input = self._provider.conservative_input_tokens(messages)
            self._check_preflight_cost(estimated_input)

            async def on_delta(delta: str) -> None:
                if await self._control.is_cancel_requested(run_id):
                    raise RunCancelledError
                await self._events.publish(
                    run_id,
                    "message.delta",
                    {"delta": delta, "trace_id": trace_id},
                )

            with RUN_DURATION.time():
                async with asyncio.timeout(self._limits.max_run_seconds):
                    result = await self._agent.run(
                        messages,
                        max_output_tokens=self._limits.max_output_tokens,
                        trace_id=trace_id,
                        on_delta=on_delta,
                    )
            if result.usage.input_tokens > self._limits.max_input_tokens:
                raise RunLimitError("Model reported input usage above the configured limit.")
            if result.usage.output_tokens > self._limits.max_output_tokens:
                raise RunLimitError("Model reported output usage above the configured limit.")
            cost = self._actual_cost(
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
            if cost > self._limits.max_cost_usd:
                raise RunLimitError("Model usage exceeded the configured cost limit.")
            await self._conversations.complete_run(
                run_id,
                answer=result.answer,
                provider=self._provider.provider_name,
                model=self._provider.model_name,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_usd=cost,
            )
            RUNS_TOTAL.labels(status=RunStatus.COMPLETED.value).inc()
            await self._events.publish(
                run_id,
                "run.completed",
                {
                    "trace_id": trace_id,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cost_usd": cost,
                },
                terminal=True,
            )
        except RunCancelledError:
            await self._finish_error(
                run_id,
                RunStatus.CANCELLED,
                "cancelled",
                "Run was cancelled.",
                trace_id,
            )
        except TimeoutError:
            await self._finish_error(
                run_id,
                RunStatus.TIMED_OUT,
                "run_timeout",
                "Run exceeded its time limit.",
                trace_id,
            )
        except RunLimitError:
            await self._finish_error(
                run_id,
                RunStatus.FAILED,
                "limit_exceeded",
                "Run exceeded a configured hard limit.",
                trace_id,
            )
        except ModelProviderError:
            await self._finish_error(
                run_id,
                RunStatus.FAILED,
                "model_provider_error",
                "Model provider request failed.",
                trace_id,
            )
        except asyncio.CancelledError:
            await self._conversations.finish_run_with_error(
                run_id,
                RunStatus.FAILED,
                "service_shutdown",
                "Run stopped because the service shut down.",
            )
            raise
        except Exception:
            logger.exception("Unexpected Agent Run failure", extra={"run_id": str(run_id)})
            await self._finish_error(
                run_id,
                RunStatus.FAILED,
                "internal_error",
                "Run failed unexpectedly.",
                trace_id,
            )
        finally:
            await self._control.clear(run_id)

    def _bounded_messages(self, stored: list[Message]) -> list[ModelMessage]:
        system = ModelMessage(role="system", content=self._model_settings.system_prompt)
        budget = self._limits.max_input_tokens - self._provider.conservative_input_tokens([system])
        selected: list[ModelMessage] = []
        for item in reversed(stored):
            message = ModelMessage(role=item.role.value, content=item.content)
            size = self._provider.conservative_input_tokens([message])
            if size > budget:
                if not selected:
                    raise RunLimitError("Latest message exceeds the model input limit.")
                break
            selected.append(message)
            budget -= size
        selected.reverse()
        return [system, *selected]

    def _check_preflight_cost(self, estimated_input_tokens: int) -> None:
        worst_case = self._actual_cost(
            estimated_input_tokens,
            self._limits.max_output_tokens,
        )
        if worst_case > self._limits.max_cost_usd:
            raise RunLimitError("Worst-case model request cost exceeds the configured limit.")

    def _actual_cost(self, input_tokens: int, output_tokens: int) -> float:
        value = (
            input_tokens * self._model_settings.input_price_per_million_tokens
            + output_tokens * self._model_settings.output_price_per_million_tokens
        ) / 1_000_000
        return round(value, 8)

    async def _finish_error(
        self,
        run_id: UUID,
        status: RunStatus,
        error_code: str,
        message: str,
        trace_id: str,
    ) -> None:
        await self._conversations.finish_run_with_error(run_id, status, error_code, message)
        RUNS_TOTAL.labels(status=status.value).inc()
        await self._events.publish(
            run_id,
            f"run.{status.value}",
            {"trace_id": trace_id, "error_code": error_code, "message": message},
            terminal=True,
        )
