"""Chat messages + router decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from level_core.schemas.activity import ActivityType
from level_core.schemas.care import CareRelation


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Inline extraction payloads. Router fills these when it is confident it
# already has everything the downstream specialist agent would extract,
# so we can save the specialist LLM call. Dispatcher falls back to the
# specialist agent when the router leaves these null (low confidence,
# multi-priority phrasings, novel wording).


class InlinePriority(BaseModel):
    """Router-extracted priority. Same shape as PriorityAgent output."""

    text: str = Field(min_length=1, max_length=240)
    weight: int = Field(default=3, ge=1, le=5)
    activity_types: list[ActivityType] = Field(default_factory=list)
    source_span: str = Field(min_length=1, max_length=240)


class InlinePersonEdit(BaseModel):
    """Router-extracted person edit. Same shape as PersonEditAgent output."""

    action: Literal["add", "change_relation", "rename", "mark_self", "remove"]
    target_name: str = Field(min_length=1, max_length=120)
    new_relation: CareRelation | None = None
    new_display_name: str | None = Field(default=None, max_length=120)
    source_span: str = Field(min_length=1, max_length=240)


class InlineReminder(BaseModel):
    """Router-extracted reminder. Same shape as ReminderAgent output."""

    text: str = Field(min_length=1, max_length=240)
    person_display_name: str | None = Field(default=None, max_length=120)
    activity_type: ActivityType
    lead_minutes: int = Field(default=60, ge=0, le=1440)
    source_span: str = Field(min_length=1, max_length=240)


class ChatRouterDecision(BaseModel):
    path: ChatRouterPath
    intent: ChatRouterIntent
    source_span: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    # Collaborative Partner rubric: when the router isn't sure what to do
    # (low confidence, missing required detail like a time or recipient),
    # it must ASK rather than guess. The chat handler renders these as
    # inline "before we book…" bubbles instead of running downstream agents.
    needs_clarification: bool = False
    clarifying_question: str | None = None
    # For `general/ask` chit-chat (greetings, "how are u", off-topic
    # questions) the router produces a warm 1-2 sentence reply inline.
    # Same LLM call - no extra latency - and it saves us from either
    # a canned "Noted." fallback or spinning up a whole second agent
    # for casual chat. Non-general paths leave this null.
    general_reply: str | None = None
    # Inline extraction: for the small, structured intents the router
    # already has everything the specialist agent would extract. Filling
    # these lets the dispatcher skip a second LLM roundtrip. Leave null
    # on low-confidence, multi-value, or unusual phrasings — the
    # specialist agent still runs as a fallback.
    inline_priority: InlinePriority | None = None
    inline_person_edit: InlinePersonEdit | None = None
    inline_reminder: InlineReminder | None = None


class ChatTurnResult(BaseModel):
    reply: str
    path: ChatRouterPath
    intent: ChatRouterIntent
    facts_added: int = 0
    proposal: dict[str, Any] | None = None
    confirmation_token: str | None = None
    # When the router asked back, the frontend surfaces the question as a
    # dedicated bubble with a pre-filled reply — the "guided step-by-step"
    # behavior that Collaborative Partner is scored on.
    needs_clarification: bool = False
    clarifying_question: str | None = None
