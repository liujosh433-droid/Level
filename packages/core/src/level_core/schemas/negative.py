"""Not-me / dismiss feedback used to teach agents in subsequent calls."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class NegativeAgent(StrEnum):
    ROLE = "RoleAgent"
    USUAL = "UsualAgent"
    PRIORITY = "PriorityAgent"
    REMINDER = "ReminderAgent"
    ACTIVITY = "ActivityAgent"


class NegativeFeedback(BaseModel):
    """Persisted to users/{uid}/negatives/{id}; last 20 injected as few-shot."""

    negative_id: str
    agent: NegativeAgent
    field: str
    value: str
    reason: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
