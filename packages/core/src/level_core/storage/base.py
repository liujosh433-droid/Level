"""Storage protocol shared by local-JSON and Firestore backends.

All API routes hold a `UserStore` handle. Backend selection happens in
`storage/factory.py` based on LEVEL_ENV; feature code NEVER branches on env.
"""

from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

from level_core.schemas import (
    AiAuditEntry,
    CachedEvent,
    CarePerson,
    ChatMessage,
    Contact,
    DailyAgenda,
    NegativeFeedback,
    Priority,
    Reminder,
    Usual,
)

T = TypeVar("T", bound=BaseModel)


class Repo(Protocol, Generic[T]):
    async def get(self, id_: str) -> T | None: ...
    async def list(self) -> list[T]: ...
    async def upsert(self, item: T) -> T: ...
    async def delete(self, id_: str) -> None: ...


class KVStore(Protocol):
    """Single-doc key/value slot (calendar_sync, user profile bits)."""

    async def read(self) -> dict[str, Any] | None: ...
    async def write(self, value: dict[str, Any]) -> None: ...


class UserStore:
    """Per-user handle bundling every collection.

    Backends fill each field with a matching Repo/KVStore implementation.
    """

    people: Repo[CarePerson]
    usuals: Repo[Usual]
    priorities: Repo[Priority]
    reminders: Repo[Reminder]
    contacts: Repo[Contact]
    agenda: Repo[CachedEvent]
    daily_agenda: Repo[DailyAgenda]
    chat_turns: Repo[ChatMessage]
    negatives: Repo[NegativeFeedback]
    ai_audit: Repo[AiAuditEntry]
    calendar_sync: KVStore
    profile: KVStore
    tokens: KVStore

    def __init__(
        self,
        *,
        user_id: str,
        people: Repo[CarePerson],
        usuals: Repo[Usual],
        priorities: Repo[Priority],
        reminders: Repo[Reminder],
        contacts: Repo[Contact],
        agenda: Repo[CachedEvent],
        daily_agenda: Repo[DailyAgenda],
        chat_turns: Repo[ChatMessage],
        negatives: Repo[NegativeFeedback],
        ai_audit: Repo[AiAuditEntry],
        calendar_sync: KVStore,
        profile: KVStore,
        tokens: KVStore,
    ) -> None:
        self.user_id = user_id
        self.people = people
        self.usuals = usuals
        self.priorities = priorities
        self.reminders = reminders
        self.contacts = contacts
        self.agenda = agenda
        self.daily_agenda = daily_agenda
        self.chat_turns = chat_turns
        self.negatives = negatives
        self.ai_audit = ai_audit
        self.calendar_sync = calendar_sync
        self.profile = profile
        self.tokens = tokens
