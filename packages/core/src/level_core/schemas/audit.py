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
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
