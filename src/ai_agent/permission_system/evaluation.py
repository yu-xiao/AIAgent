"""Offline regression evaluation primitives for the PermissionSystem closure."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from ai_agent.mcp.models import Citation


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    id: str
    question: str
    expected_tool: str
    expects_citation: bool = True
    expects_denial: bool = False


@dataclass(frozen=True, slots=True)
class EvaluationAnswer:
    answer: str
    citations: tuple[Citation, ...] = ()
    denied: bool = False
    used_tool: str | None = None


@dataclass(frozen=True, slots=True)
class GoldenQuestionResult:
    question_id: str
    passed: bool
    reason: str


DEFAULT_GOLDEN_QUESTIONS: tuple[GoldenQuestion, ...] = (
    GoldenQuestion("datasets.list", "我可以查询哪些数据集?", "list_datasets"),
    GoldenQuestion("datasets.describe", "请说明订单数据集的字段。", "describe_dataset"),
    GoldenQuestion("datasets.query", "查询我有权限查看的订单数据。", "query_dataset"),
    GoldenQuestion(
        "datasets.denied",
        "查询我没有权限的数据集。",
        "query_dataset",
        expects_citation=False,
        expects_denial=True,
    ),
)


async def evaluate_golden_questions(
    runner: Callable[[GoldenQuestion], Awaitable[EvaluationAnswer]],
    questions: Iterable[GoldenQuestion] = DEFAULT_GOLDEN_QUESTIONS,
) -> list[GoldenQuestionResult]:
    results: list[GoldenQuestionResult] = []
    for question in questions:
        try:
            response = await runner(question)
        except Exception:
            results.append(GoldenQuestionResult(question.id, False, "runner_error"))
            continue
        if question.expects_denial:
            passed = response.denied and not response.citations
            reason = "denied" if passed else "expected_denial"
        elif response.denied:
            passed = False
            reason = "unexpected_denial"
        elif response.used_tool != question.expected_tool:
            passed = False
            reason = "wrong_tool"
        elif question.expects_citation and not response.citations:
            passed = False
            reason = "missing_citation"
        elif question.expects_citation and any(item.partial for item in response.citations):
            passed = False
            reason = "partial_evidence"
        else:
            passed = True
            reason = "ok"
        results.append(GoldenQuestionResult(question.id, passed, reason))
    return results


def citation_coverage(results: Iterable[GoldenQuestionResult]) -> float:
    items = list(results)
    if not items:
        return 1.0
    return sum(item.passed for item in items) / len(items)
