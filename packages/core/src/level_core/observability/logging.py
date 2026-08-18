"""Structured logging with per-call trace_id and redaction.

Every log record is JSON with `trace_id`, `user_id_hash`, `level`, `event`,
plus arbitrary structured fields. User content is scrubbed by
`redact_for_log()` before it ever hits the log stream.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import structlog

from level_core.config import get_settings

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")
_MAX_VALUE_LEN = 200


def _redact_str(value: str) -> str:
    value = _EMAIL_RE.sub("<email>", value)
    value = _PHONE_RE.sub("<phone>", value)
    if len(value) > _MAX_VALUE_LEN:
        value = value[:_MAX_VALUE_LEN] + "...<truncated>"
    return value


def redact_for_log(value: Any) -> Any:
    """Recursively scrub PII and cap string length for safe logging."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, list | tuple):
        return [redact_for_log(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_for_log(v) for k, v in value.items()}
    return _redact_str(str(value))


def hash_user_id(user_id: str) -> str:
    """Stable short hash for logs so we can group per-user without leaking id."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


_configured = False


def _configure_once() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _log_level_from_str(get_settings().level_log_level)
        ),
        cache_logger_on_first_use=True,
    )


def _log_level_from_str(name: str) -> int:
    import logging

    return getattr(logging, name.upper(), logging.INFO)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    _configure_once()
    return structlog.get_logger(name)
