"""Redis-backed Run events and cancellation signals."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    event_type: str
    payload: dict[str, Any]
    terminal: bool = False


class RunEventBus(Protocol):
    async def publish(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> str: ...

    async def read(
        self, run_id: UUID, after_id: str, *, block_ms: int = 15_000
    ) -> list[RunEvent]: ...


class RunControl(Protocol):
    async def request_cancel(self, run_id: UUID) -> None: ...

    async def is_cancel_requested(self, run_id: UUID) -> bool: ...

    async def clear(self, run_id: UUID) -> None: ...


class RedisRunEventBus:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> str:
        event_id = await self._redis.xadd(
            f"run-events:{run_id}",
            {
                "type": event_type,
                "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                "terminal": "1" if terminal else "0",
            },
            maxlen=1_000,
            approximate=True,
        )
        await self._redis.expire(f"run-events:{run_id}", 86_400)
        return _decode(event_id)

    async def read(self, run_id: UUID, after_id: str, *, block_ms: int = 15_000) -> list[RunEvent]:
        streams = await self._redis.xread(
            {f"run-events:{run_id}": after_id}, count=100, block=block_ms
        )
        if not streams:
            return []
        events: list[RunEvent] = []
        for _, entries in streams:
            for event_id, fields in entries:
                decoded = {_decode(key): _decode(value) for key, value in fields.items()}
                events.append(
                    RunEvent(
                        event_id=_decode(event_id),
                        event_type=decoded["type"],
                        payload=json.loads(decoded["payload"]),
                        terminal=decoded["terminal"] == "1",
                    )
                )
        return events


class RedisRunControl:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def request_cancel(self, run_id: UUID) -> None:
        await self._redis.set(f"run-cancel:{run_id}", "1", ex=300)

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        return bool(await self._redis.exists(f"run-cancel:{run_id}"))

    async def clear(self, run_id: UUID) -> None:
        await self._redis.delete(f"run-cancel:{run_id}")


class MemoryRunBackend:
    """Test event bus and cancellation control."""

    def __init__(self) -> None:
        self.events: dict[UUID, list[RunEvent]] = {}
        self.cancelled: set[UUID] = set()
        self._condition = asyncio.Condition()

    async def publish(
        self,
        run_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> str:
        async with self._condition:
            items = self.events.setdefault(run_id, [])
            event_id = f"{len(items) + 1}-0"
            items.append(RunEvent(event_id, event_type, payload, terminal))
            self._condition.notify_all()
            return event_id

    async def read(self, run_id: UUID, after_id: str, *, block_ms: int = 15_000) -> list[RunEvent]:
        offset = int(after_id.split("-", maxsplit=1)[0])
        async with self._condition:
            if len(self.events.get(run_id, [])) <= offset:
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=max(block_ms / 1_000, 0.001)
                    )
                except TimeoutError:
                    return []
            return self.events.get(run_id, [])[offset:]

    async def request_cancel(self, run_id: UUID) -> None:
        self.cancelled.add(run_id)

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        return run_id in self.cancelled

    async def clear(self, run_id: UUID) -> None:
        self.cancelled.discard(run_id)


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
