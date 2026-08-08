"""OpenTelemetry bootstrap for the API service.

Called once at app startup so every request gets a root span. We deliberately
avoid ``FastAPIInstrumentor.instrument_app`` — current otel-fastapi (0.63b1)
crashes on CORS preflight OPTIONS with:

    AttributeError: '_IncludedRouter' object has no attribute 'path'

ASGI-level middleware + our ``@traced`` agent spans are enough for the
hackathon observability story.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from level_core.observability.tracer import configure_observability

if TYPE_CHECKING:
    from fastapi import FastAPI


def instrument_app(app: FastAPI) -> None:
    """Wire up OTel tracing on the ASGI app + httpx.

    Safe to call multiple times — the underlying instrumentors are idempotent.
    """
    configure_observability()

    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        # ASGI middleware is stable across FastAPI versions and does not
        # introspect Starlette route objects (the FastAPI instrumentor's bug).
        app.add_middleware(
            OpenTelemetryMiddleware,
            default_span_details=_span_details,
            exclude_spans=["receive", "send"],
        )
    except ImportError:
        pass

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except ImportError:
        pass


def _span_details(scope: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Name the request span from method + path — never touch route objects."""
    method = scope.get("method", "HTTP")
    path = scope.get("path", "/")
    return f"{method} {path}", {"http.method": method, "http.target": path}


__all__ = ["instrument_app"]
