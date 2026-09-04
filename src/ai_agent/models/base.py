"""Provider-neutral model streaming contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ModelUsageResult:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    delta: str | None = None
    usage: ModelUsageResult | None = None


class ModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def conservative_input_tokens(self, messages: list[ModelMessage]) -> int: ...

    def stream(
        self,
        messages: list[ModelMessage],
        *,
        max_output_tokens: int,
        trace_id: str,
    ) -> AsyncIterator[ModelStreamEvent]: ...
