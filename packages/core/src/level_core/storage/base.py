"""Storage protocol shared by local-JSON and Firestore backends.

All API routes hold a `UserStore` handle. Backend selection happens in
`storage/factory.py` based on LEVEL_ENV; feature code NEVER branches on env.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Generic, Protocol, TypeVar

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
    async def upsert_many(self, items: list[T]) -> None: ...
    async def delete(self, id_: str) -> None: ...
    async def delete_many(self, ids: list[str]) -> None: ...


class KVStore(Protocol):
    """Single-doc key/value slot (calendar_sync, user profile bits).

    Two atomicity primitives beyond blind `write()`:
      * `update_fields()` merges the given top-level fields into the
        existing doc atomically. Use this when you only want to change a
        subset of the doc (e.g. `last_role_run_fingerprint`) - blind
        `write(entire_dict)` on the read result races against every
        other writer to the same slot.
      * `mutate(fn)` runs a full read-modify-write inside a lock (local)
        or transaction (Firestore), retrying under contention. Use this
        when the new value depends on the old (counters, rollovers).
    """

    async def read(self) -> dict[str, Any] | None: ...
    async def write(self, value: dict[str, Any]) -> None: ...
    async def update_fields(self, **fields: Any) -> None: ...
    async def mutate(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]: ...


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
        reset_all: Callable[[], Awaitable[None]] | None = None,
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
        self._reset_all_impl = reset_all

    async def reset_all(self) -> None:
        """Wipe every document under this user's subtree.

        Storage-native fast path used by the demo seeder to reset a
        session in one shot instead of the slow "list every collection,
        then delete every doc" dance. Both concrete backends provide
        an implementation - local rmtrees the user's directory,
        Firestore uses ``client.recursive_delete`` under the user doc.

        Callers MUST authorize before invoking - this method assumes
        the caller has already confirmed it's safe to wipe (e.g. via a
        demo-user prefix check in the demo seeder).
        """
        if self._reset_all_impl is None:
            raise NotImplementedError(
                "backend for UserStore did not provide reset_all()"
            )
        await self._reset_all_impl()
