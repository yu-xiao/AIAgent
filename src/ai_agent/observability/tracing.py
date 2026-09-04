"""Optional OTLP/HTTP trace export."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from ai_agent.config import ObservabilitySettings


@dataclass(slots=True)
class TracingRuntime:
    provider: object | None = None

    def instrument(self, app: FastAPI) -> None:
        if self.provider is None:
            return
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health/live,health/ready,metrics",
        )

    def close(self) -> None:
        if self.provider is None:
            return
        shutdown = getattr(self.provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


def configure_tracing(settings: ObservabilitySettings) -> TracingRuntime:
    if not settings.tracing_enabled:
        return TracingRuntime()

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.service_name}),
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    return TracingRuntime(provider)


@contextmanager
def operation_span(
    name: str,
    *,
    trace_id: str,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Iterator[None]:
    from opentelemetry import trace

    with trace.get_tracer("ai_agent").start_as_current_span(name) as span:
        span.set_attribute("ai_agent.trace_id", trace_id)
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        yield
