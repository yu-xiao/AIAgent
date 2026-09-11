from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from ai_agent.persistence.models import Run, RunStatus
from ai_agent.runs.events import MemoryRunBackend
from ai_agent.runs.executor import RunExecutor
from tests.p1_conftest import PlatformRuntime


class BlockingRunExecutor(RunExecutor):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.started: list[UUID] = []
        self.active = 0
        self.peak_active = 0
        self.release = asyncio.Event()

    async def _execute(self, run_id: UUID, quota_lease: object | None = None) -> None:
        del quota_lease
        self.started.append(run_id)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1


class FailingEventBus(MemoryRunBackend):
    async def publish(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, object],
        *,
        terminal: bool = False,
    ) -> str:
        del run_id, event_type, payload, terminal
        raise RuntimeError("event backend is unavailable")


async def test_submission_failure_marks_run_failed_without_internal_error(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    conversation = await runtime.client.post(
        "/api/v1/conversations",
        headers=runtime.headers,
        json={"title": "Submission failure"},
    )
    conversation_id = UUID(conversation.json()["id"])

    def fail_submit(run_id: UUID, quota_lease: object | None = None) -> bool:
        del run_id, quota_lease
        raise ValueError("internal scheduler failure")

    monkeypatch.setattr(runtime.services.executor, "submit", fail_submit)
    response = await runtime.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=runtime.headers,
        json={"content": "hello"},
    )

    assert response.status_code == 503
    assert "internal scheduler failure" not in response.text
    async with runtime.services.database.session_factory() as session:
        run = await session.scalar(select(Run).where(Run.conversation_id == conversation_id))
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error_code == "submission_failed"


async def test_recovery_failure_does_not_block_later_queued_runs(
    platform_runtime: PlatformRuntime,
    monkeypatch,
) -> None:
    runtime = platform_runtime
    conversations = runtime.services.conversations
    first_conversation = await conversations.create_conversation(
        runtime.user.id,
        runtime.organization.id,
        "First recovery",
    )
    second_conversation = await conversations.create_conversation(
        runtime.user.id,
        runtime.organization.id,
        "Second recovery",
    )
    first_run, _ = await conversations.create_run(
        runtime.user.id,
        runtime.organization.id,
        first_conversation.id,
        "first",
        None,
        uuid4(),
    )
    second_run, _ = await conversations.create_run(
        runtime.user.id,
        runtime.organization.id,
        second_conversation.id,
        "second",
        None,
        uuid4(),
    )
    submitted: list[UUID] = []

    def submit(run_id: UUID, quota_lease: object | None = None) -> bool:
        del quota_lease
        if run_id == first_run.id:
            raise RuntimeError("scheduler unavailable")
        submitted.append(run_id)
        return True

    monkeypatch.setattr(runtime.services.executor, "submit", submit)
    await runtime.services.start(runtime.settings)

    first = await conversations.get_run(
        runtime.user.id,
        runtime.organization.id,
        first_run.id,
    )
    assert first.status == RunStatus.FAILED
    assert first.error_code == "recovery_submission_failed"
    assert submitted == [second_run.id]


async def test_executor_deduplicates_runs_and_enforces_process_capacity(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    executor = BlockingRunExecutor(
        runtime.services.conversations,
        runtime.provider,
        runtime.services.events,
        runtime.services.control,
        runtime.settings.limits,
        runtime.settings.model,
        max_concurrent_runs=1,
    )
    first_run_id = uuid4()
    second_run_id = uuid4()

    assert executor.submit(first_run_id) is True
    assert executor.submit(first_run_id) is False
    assert executor.submit(second_run_id) is True
    for _ in range(20):
        if executor.started:
            break
        await asyncio.sleep(0)
    assert executor.started == [first_run_id]
    assert executor.peak_active == 1

    executor.release.set()
    await executor.close()

    assert executor.started == [first_run_id, second_run_id]
    assert executor.peak_active == 1


async def test_event_failure_does_not_override_completed_run(
    platform_runtime: PlatformRuntime,
) -> None:
    runtime = platform_runtime
    conversations = runtime.services.conversations
    conversation = await conversations.create_conversation(
        runtime.user.id,
        runtime.organization.id,
        "Event failure",
    )
    run, created = await conversations.create_run(
        runtime.user.id,
        runtime.organization.id,
        conversation.id,
        "hello",
        None,
        uuid4(),
    )
    assert created
    executor = RunExecutor(
        conversations,
        runtime.provider,
        FailingEventBus(),
        MemoryRunBackend(),
        runtime.settings.limits,
        runtime.settings.model,
    )

    assert executor.submit(run.id)
    completed = await _wait_for_terminal_run(runtime, run.id)
    await executor.close()

    assert completed.status == RunStatus.COMPLETED


async def _wait_for_terminal_run(runtime: PlatformRuntime, run_id: UUID) -> Run:
    terminal_statuses = {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }
    for _ in range(100):
        run = await runtime.services.conversations.get_run(
            runtime.user.id,
            runtime.organization.id,
            run_id,
        )
        if run.status in terminal_statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run {run_id} did not reach a terminal status")
