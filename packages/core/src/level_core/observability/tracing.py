"""OpenTelemetry spans around every agent call and storage write."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from level_core.config import get_settings

_provider_initialized = False


def _init_provider() -> None:
    global _provider_initialized
    if _provider_initialized:
        return
    _provider_initialized = True

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": settings.level_service_name,
            "service.version": "2.0.0",
        }
    )
    provider = TracerProvider(resource=resource)

    if settings.level_otel_exporter == "cloud":
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
        except Exception:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)


def tracer() -> trace.Tracer:
    _init_provider()
    return trace.get_tracer("level")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v)
            except Exception:
                sp.set_attribute(k, str(v))
        yield sp
