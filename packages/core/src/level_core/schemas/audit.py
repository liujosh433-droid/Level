"""AI audit log entry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AiAuditEntry(BaseModel):
    audit_id: str
    agent: str
    model: str
    prompt_hash: str
    response: dict[str, Any] | list[Any] | str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate_usd: float = 0.0
    latency_ms: int = 0
    hallucinated: bool = False
    loop_broken: bool = False
    blocked_by_safety: bool = False
    # Set when a call was routed to a fallback model (Gemini 2.5, Gemma).
    # Populated by base.py._invoke_with_retry so /admin/traces can show
    # exactly when quota degradation happened during the demo.
    fallback_used: str | None = None
    # For multi-turn generative agents: how many refinement turns ran
    # before we accepted the output (1 = single-shot, N = N-1 refinements).
    turns_taken: int = 1
    # Optional parent audit id: when a chat turn triggers router → agent →
    # tool, the router's audit_id lives here so /admin/traces can build a
    # tree without a separate spans table.
    parent_audit_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
