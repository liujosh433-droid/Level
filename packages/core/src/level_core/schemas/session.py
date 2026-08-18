"""Signed user session (cookie payload)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserSession(BaseModel):
    user_id: str
    email: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
