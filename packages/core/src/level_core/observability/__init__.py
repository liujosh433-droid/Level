"""OpenTelemetry + structured logging for Level.

Every agent invocation, every LLM call, every Firestore query, and every
Vector Search query becomes an OpenTelemetry span. Traces are exported to
Cloud Trace in cloud mode and printed to stdout in local mode.

Import ``configure_observability`` once at process startup (typically in
``level_api.telemetry`` or a Cloud Run Job's ``main``).
"""

from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import bind_context, get_logger
from level_core.observability.tracer import configure_observability, current_trace_id, traced

__all__ = [
    "AuditEventKind",
    "bind_context",
    "configure_observability",
    "current_trace_id",
    "get_logger",
    "traced",
    "write_audit_event",
]
