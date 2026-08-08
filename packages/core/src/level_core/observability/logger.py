"""Structured logging via structlog.

Every log line is a JSON object in cloud mode (Cloud Logging ingests
structured logs natively) and pretty-printed in local mode. Every log
line carries the current OTel ``trace_id`` when available, so logs can
be pivoted to the corresponding Cloud Trace span with one click.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from level_core.config import Environment, get_settings
from level_core.observability.tracer import current_trace_id

_configured = False


def _add_trace_id(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor that injects the current OTel trace id."""
    trace_id = current_trace_id()
    if trace_id is not None:
        event_dict["trace_id"] = trace_id
    return event_dict


def _add_service_name(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    settings = get_settings()
    event_dict.setdefault("service", settings.service_name)
    return event_dict


def _configure_once() -> None:
    global _configured
    if _configured:
        return

    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_name,
        _add_trace_id,
    ]

    if settings.env is Environment.CLOUD:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, initializing structlog on first call.

    ``initial_context`` is bound onto the returned logger. Call
    :func:`bind_context` afterwards to add more context in-place.
    """
    _configure_once()
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger


def bind_context(**context: Any) -> None:
    """Bind key/value context onto the current logging contextvars scope.

    Any log line emitted downstream in the same task/thread will include
    this context. Use in request/job entry points to attach
    ``user_id``, ``decision_id``, etc.
    """
    _configure_once()
    structlog.contextvars.bind_contextvars(**context)


def clear_context() -> None:
    """Clear the contextvars logging scope. Call at the end of a request."""
    structlog.contextvars.clear_contextvars()


__all__ = ["bind_context", "clear_context", "get_logger"]
