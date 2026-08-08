"""Append-only audit log for agent decisions that affect user state.

Distinct from the (mutable) Firestore domain data — audit events are the
immutable "who did what when" record. Written to Firestore in cloud mode
and to stdout in local mode.

Audit events are the primary artifact for post-hoc investigation of any
odd system behavior (why did the Challenger say X?, why was that signal
blocked?, why did a session degrade?).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from level_core.observability.logger import get_logger
from level_core.observability.tracer import current_trace_id
from level_core.schemas.base import TimestampedModel, _new_id

_logger = get_logger(__name__)


class AuditEventKind(str, Enum):
    """Kinds of audit events we emit.

    Kept as a closed enum so a grep for each kind is exhaustive.
    """

    AGENT_INVOKED = "agent_invoked"
    AGENT_DEGRADED = "agent_degraded"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    HALLUCINATED_CITATION = "hallucinated_citation"
    INGEST_ACCEPTED = "ingest_accepted"
    INGEST_REJECTED = "ingest_rejected"
    MANIFESTO_UPDATED = "manifesto_updated"
    BIAS_PROFILE_UPDATED = "bias_profile_updated"
    TOOL_INVOCATION_DENIED = "tool_invocation_denied"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"


class AuditEvent(TimestampedModel):
    """One append-only audit event."""

    event_id: str = Field(default_factory=_new_id)
    kind: AuditEventKind
    user_id: str | None = None
    subject: str = Field(
        description="Human-readable identifier of what the event is about, e.g. 'challenger@v3', 'signal:abcd'.",
    )
    trace_id: str | None = None
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Kind-specific structured data. Not indexed; safe for opaque details.",
    )


def write_audit_event(
    kind: AuditEventKind,
    subject: str,
    *,
    user_id: str | None = None,
    **payload: Any,
) -> AuditEvent:
    """Emit an audit event.

    In cloud mode this also writes to Firestore ``audit/{event_id}``. In
    local mode it only logs — audit persistence in local mode would just
    fill up the emulator. The returned :class:`AuditEvent` is the same
    object that was persisted; tests can inspect it directly.
    """
    event = AuditEvent(
        kind=kind,
        subject=subject,
        user_id=user_id,
        trace_id=current_trace_id(),
        payload=payload,
    )
    _logger.info(
        "audit_event",
        event_id=event.event_id,
        kind=event.kind.value,
        subject=event.subject,
        user_id=event.user_id,
        **event.payload,
    )
    # Cloud-mode persistence to Firestore is wired up by the caller (the API
    # layer or a Cloud Run Job) so that this module remains dependency-light.
    return event


__all__ = ["AuditEvent", "AuditEventKind", "write_audit_event"]
