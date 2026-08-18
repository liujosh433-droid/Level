"""Chat messages + router decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatRouterPath(StrEnum):
    SCHEDULE = "schedule"
    EMAIL = "email"
    PROFILE = "profile"
    REMINDER = "reminder"
    GENERAL = "general"


class ChatRouterIntent(StrEnum):
    PRIORITY = "priority"
    PERSON_UPDATE = "person_update"
    USUAL_UPDATE = "usual_update"
    CONTACT_ADD = "contact_add"
    FIND_TIME = "find_time"
    BOOK_NOW = "book_now"
    SEND_EMAIL = "send_email"
    ASK = "ask"
    ADD_REMINDER = "add_reminder"


class ChatMessage(BaseModel):
    turn_id: str
    role: ChatRole
    text: str
    path: ChatRouterPath | None = None
    refs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatRouterDecision(BaseModel):
    path: ChatRouterPath
    intent: ChatRouterIntent
    source_span: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ChatTurnResult(BaseModel):
    reply: str
    path: ChatRouterPath
    intent: ChatRouterIntent
    facts_added: int = 0
    proposal: dict[str, Any] | None = None
    confirmation_token: str | None = None
