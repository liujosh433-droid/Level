"""LEVEL_ENV=cloud backend: Firestore per-user subtree.

Each collection lives at `users/{uid}/{collection}/{doc_id}`.
KV slots live at `users/{uid}/state/{slot}`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from level_core.config import get_settings
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
from level_core.storage.base import KVStore, UserStore

T = TypeVar("T", bound=BaseModel)


def _client() -> Any:
    from google.cloud import firestore

    settings = get_settings()
    return firestore.Client(
        project=settings.google_cloud_project or None,
        database=settings.level_firestore_database,
    )


class FirestoreRepo(Generic[T]):
    def __init__(
        self,
        *,
        user_id: str,
        collection: str,
        model: type[T],
        id_field: str,
    ) -> None:
        self._user_id = user_id
        self._collection = collection
        self._model = model
        self._id_field = id_field
        self._client = _client()

    def _col(self) -> Any:
        return (
            self._client.collection("users")
            .document(self._user_id)
            .collection(self._collection)
        )

    async def get(self, id_: str) -> T | None:
        snap = self._col().document(id_).get()
        if not snap.exists:
            return None
        return self._model.model_validate(snap.to_dict())

    async def list(self) -> list[T]:
        return [self._model.model_validate(d.to_dict()) for d in self._col().stream()]

    async def upsert(self, item: T) -> T:
        id_ = getattr(item, self._id_field)
        doc = self._col().document(id_)
        existing = doc.get()
        new_version = (existing.to_dict() or {}).get("version", 0) + 1 if existing.exists else 1
        payload = item.model_dump(mode="json")
        payload["version"] = new_version
        if "updated_at" in payload:
            payload["updated_at"] = datetime.utcnow().isoformat()
        doc.set(payload)
        return self._model.model_validate(payload)

    async def delete(self, id_: str) -> None:
        self._col().document(id_).delete()


class FirestoreKV(KVStore):
    def __init__(self, *, user_id: str, slot: str) -> None:
        self._user_id = user_id
        self._slot = slot
        self._client = _client()

    def _doc(self) -> Any:
        return (
            self._client.collection("users")
            .document(self._user_id)
            .collection("state")
            .document(self._slot)
        )

    async def read(self) -> dict[str, Any] | None:
        snap = self._doc().get()
        return snap.to_dict() if snap.exists else None

    async def write(self, value: dict[str, Any]) -> None:
        self._doc().set(value)


def make_firestore_store(user_id: str) -> UserStore:
    def repo(collection: str, model: type[BaseModel], id_field: str) -> FirestoreRepo:
        return FirestoreRepo(
            user_id=user_id, collection=collection, model=model, id_field=id_field
        )

    return UserStore(
        user_id=user_id,
        people=repo("care_people", CarePerson, "person_id"),
        usuals=repo("usuals", Usual, "usual_id"),
        priorities=repo("priorities", Priority, "priority_id"),
        reminders=repo("reminders", Reminder, "reminder_id"),
        contacts=repo("contacts", Contact, "contact_id"),
        agenda=repo("agenda_cache", CachedEvent, "event_id"),
        daily_agenda=repo("daily_agenda", DailyAgenda, "date"),
        chat_turns=repo("chat_turns", ChatMessage, "turn_id"),
        negatives=repo("negatives", NegativeFeedback, "negative_id"),
        ai_audit=repo("ai_audit", AiAuditEntry, "audit_id"),
        calendar_sync=FirestoreKV(user_id=user_id, slot="calendar_sync"),
        profile=FirestoreKV(user_id=user_id, slot="profile"),
        tokens=FirestoreKV(user_id=user_id, slot="google_oauth"),
    )
