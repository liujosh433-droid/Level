"""OpenTelemetry tracer setup + the ``@traced`` decorator.

The tracer is initialized lazily on first use. Calling
``configure_observability()`` at process startup makes exports predictable
and lets us fail fast in cloud mode if the exporter can't be constructed.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from level_core.config import Environment, OtelExporter, get_settings

_configured = False
_tracer: trace.Tracer | None = None

P = ParamSpec("P")
R = TypeVar("R")


def configure_observability() -> None:
    """Wire up the global OpenTelemetry TracerProvider.

    Idempotent — safe to call multiple times. Reads exporter preference
    from ``Settings.otel_exporter``:

    - ``console`` (default in local): pretty prints spans to stdout.
    - ``gcp``: exports to Cloud Trace via the GCP exporter package.
    """
    global _configured, _tracer
    if _configured:
        return

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": "0.1.0",
            "deployment.environment": settings.env.value,
        }
    )

    provider = TracerProvider(resource=resource)

    if settings.otel_exporter is OtelExporter.NONE:
        # Explicit no-op: no processor. Spans are still created (so trace_id
        # helpers work) but nothing is exported. Used in the test suite to
        # avoid BatchSpanProcessor flushes racing with pytest's stdout capture.
        pass
    elif settings.otel_exporter is OtelExporter.GCP and settings.env is Environment.CLOUD:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.gcp_project))
            )
        except ImportError:
            # Gracefully degrade to console if the exporter package isn't installed.
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("level")
    _configured = True


def _get_tracer() -> trace.Tracer:
    if _tracer is None:
        configure_observability()
    assert _tracer is not None
    return _tracer


def current_trace_id() -> str | None:
    """Return the current OTel trace id as a 32-char hex string, or None."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.trace_id == 0:
        return None
    return format(ctx.trace_id, "032x")


def traced(
    span_name: str | None = None,
    *,
    attributes: dict[str, str] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that wraps a function in an OTel span.

    Works for both sync and async functions. If the wrapped function
    raises, the span is marked with the error status before re-raising.
    Extra attributes can be attached at declaration time; the wrapped
    function may also set attributes on the current span via
    ``trace.get_current_span().set_attribute(...)``.
    """

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        name = span_name or f"{fn.__module__}.{fn.__qualname__}"

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                tracer = _get_tracer()
                with tracer.start_as_current_span(name) as span:
                    if attributes:
                        for key, value in attributes.items():
                            span.set_attribute(key, value)
                    try:
                        awaitable: Awaitable[R] = fn(*args, **kwargs)  # type: ignore[assignment]
                        return await awaitable
                    except Exception as exc:
                        span.record_exception(exc)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                        raise

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            tracer = _get_tracer()
            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for key, value in attributes.items():
                        span.set_attribute(key, value)
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                    raise

        return sync_wrapper

    return decorator


__all__ = ["configure_observability", "current_trace_id", "traced"]
