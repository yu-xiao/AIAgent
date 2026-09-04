"""Small dependency-free-of-test-framework capacity probe for HTTP and live Run paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Agent capacity probe")
    parser.add_argument("--base-url", default="https://localhost")
    parser.add_argument("--mode", choices=("http", "runs"), default="http")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--p95-target-ms", type=float)
    parser.add_argument("--confirm-live-cost", action="store_true")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if args.concurrency < 1 or args.requests < 1:
        raise ValueError("concurrency and requests must be positive")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout) as client:
        if args.mode == "runs":
            if not args.confirm_live_cost:
                raise ValueError("run mode requires --confirm-live-cost")
            durations, failures = await _run_load(client, args.concurrency, args.requests)
        else:
            durations, failures = await _http_load(client, args.concurrency, args.requests)
    report = _report(durations, failures)
    target = args.p95_target_ms or (90_000 if args.mode == "runs" else 500)
    report["p95_target_ms"] = target
    print(json.dumps(report, indent=2))
    return 0 if report["error_rate"] < 0.01 and report["p95_ms"] <= target else 1


async def _http_load(
    client: httpx.AsyncClient, concurrency: int, requests: int
) -> tuple[list[float], int]:
    semaphore = asyncio.Semaphore(concurrency)

    async def request() -> tuple[float, bool]:
        async with semaphore:
            started = time.perf_counter()
            response = await client.get("/health/ready")
            return time.perf_counter() - started, response.status_code != 200

    results = await asyncio.gather(*(request() for _ in range(requests)))
    return [duration for duration, _ in results], sum(failed for _, failed in results)


async def _run_load(
    client: httpx.AsyncClient, concurrency: int, requests: int
) -> tuple[list[float], int]:
    session_cookie = _secret_file("AI_AGENT_LOAD_SESSION_COOKIE_FILE")
    csrf = _secret_file("AI_AGENT_LOAD_CSRF_FILE")
    organization_id = os.environ.get("AI_AGENT_LOAD_ORGANIZATION_ID", "")
    cookie_name = os.environ.get("AI_AGENT_LOAD_SESSION_COOKIE_NAME", "ai_agent_session")
    if not organization_id:
        raise ValueError("AI_AGENT_LOAD_ORGANIZATION_ID is required")
    client.cookies.set(cookie_name, session_cookie)
    headers = {"X-Organization-Id": organization_id, "X-CSRF-Token": csrf}
    conversations: asyncio.Queue[str] = asyncio.Queue()
    for index in range(concurrency):
        response = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"title": f"P5 capacity {index}"},
        )
        response.raise_for_status()
        await conversations.put(str(response.json()["id"]))

    async def execute(index: int) -> tuple[float, bool]:
        conversation_id = await conversations.get()
        started = time.perf_counter()
        failed = False
        try:
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={**headers, "Idempotency-Key": f"p5-load-{index}"},
                json={"content": "Reply with the word OK only."},
            )
            if response.status_code != 202:
                return time.perf_counter() - started, True
            run_id = response.json()["id"]
            while True:
                run = await client.get(f"/api/v1/runs/{run_id}", headers=headers)
                if run.status_code != 200:
                    failed = True
                    break
                status = run.json()["status"]
                if status in {"completed", "failed", "cancelled", "timed_out"}:
                    failed = status != "completed"
                    break
                await asyncio.sleep(0.1)
        finally:
            await conversations.put(conversation_id)
        return time.perf_counter() - started, failed

    results = await asyncio.gather(*(execute(index) for index in range(requests)))
    return [duration for duration, _ in results], sum(failed for _, failed in results)


def _secret_file(environment_name: str) -> str:
    value = os.environ.get(environment_name, "")
    if not value:
        raise ValueError(f"{environment_name} is required")
    secret = Path(value).read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError(f"{environment_name} points to an empty file")
    return secret


def _report(durations: list[float], failures: int) -> dict[str, Any]:
    ordered = sorted(durations)

    def percentile(value: float) -> float:
        index = min(round((len(ordered) - 1) * value), len(ordered) - 1)
        return round(ordered[index] * 1_000, 2)

    return {
        "requests": len(durations),
        "failures": failures,
        "error_rate": failures / len(durations),
        "mean_ms": round(statistics.fmean(durations) * 1_000, 2),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
    }


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
