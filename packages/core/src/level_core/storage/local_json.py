"""LEVEL_ENV=local backend: one JSON file per collection under .level/local_store."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

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
from level_core.storage.base import KVStore, UserStore

T = TypeVar("T", bound=BaseModel)

_root_lock = asyncio.Lock()


def _root_dir() -> Path:
    p = Path(".level/local_store")
    p.mkdir(parents=True, exist_ok=True)
    return p


class LocalRepo(Generic[T]):
    """JSON list on disk keyed by `id_field` on the model."""

    def __init__(
        self,
        *,
        user_id: str,
        collection: str,
        model: type[T],
        id_field: str,
    ) -> None:
        self._path = _root_dir() / user_id / f"{collection}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._model = model
        self._id_field = id_field

    async def _load(self) -> list[dict[str, Any]]:
        async with _root_lock:
            if not self._path.exists():
                return []
            return json.loads(self._path.read_text() or "[]")

    async def _dump(self, items: list[dict[str, Any]]) -> None:
        async with _root_lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, default=str, indent=2))
            tmp.replace(self._path)

    async def get(self, id_: str) -> T | None:
        for row in await self._load():
            if row.get(self._id_field) == id_:
                return self._model.model_validate(row)
        return None

    async def list(self) -> list[T]:
        return [self._model.model_validate(row) for row in await self._load()]

    async def upsert(self, item: T) -> T:
        rows = await self._load()
        id_ = getattr(item, self._id_field)

        updated = item.model_copy(update={"version": getattr(item, "version", 1)})
        if hasattr(updated, "updated_at"):
            updated = updated.model_copy(update={"updated_at": datetime.utcnow()})

        payload = json.loads(updated.model_dump_json())
        for i, row in enumerate(rows):
            if row.get(self._id_field) == id_:
                new_version = row.get("version", 0) + 1
                payload["version"] = new_version
                updated = self._model.model_validate(payload)
                rows[i] = payload
                await self._dump(rows)
                return updated

        rows.append(payload)
        await self._dump(rows)
        return updated

    async def delete(self, id_: str) -> None:
        rows = [r for r in await self._load() if r.get(self._id_field) != id_]
        await self._dump(rows)


class LocalKV(KVStore):
    def __init__(self, *, user_id: str, slot: str) -> None:
        self._path = _root_dir() / user_id / f"{slot}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def read(self) -> dict[str, Any] | None:
        async with _root_lock:
            if not self._path.exists():
                return None
            raw = self._path.read_text().strip()
            if not raw:
                return None
            return json.loads(raw)

    async def write(self, value: dict[str, Any]) -> None:
        async with _root_lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(value, default=str, indent=2))
            tmp.replace(self._path)


def make_local_store(user_id: str) -> UserStore:
    def repo(collection: str, model: type[BaseModel], id_field: str) -> LocalRepo:
        return LocalRepo(user_id=user_id, collection=collection, model=model, id_field=id_field)

    return UserStore(
        user_id=user_id,
        people=repo("people", CarePerson, "person_id"),
        usuals=repo("usuals", Usual, "usual_id"),
        priorities=repo("priorities", Priority, "priority_id"),
        reminders=repo("reminders", Reminder, "reminder_id"),
        contacts=repo("contacts", Contact, "contact_id"),
        agenda=repo("agenda", CachedEvent, "event_id"),
        daily_agenda=repo("daily_agenda", DailyAgenda, "date"),
        chat_turns=repo("chat_turns", ChatMessage, "turn_id"),
        negatives=repo("negatives", NegativeFeedback, "negative_id"),
        ai_audit=repo("ai_audit", AiAuditEntry, "audit_id"),
        calendar_sync=LocalKV(user_id=user_id, slot="calendar_sync"),
        profile=LocalKV(user_id=user_id, slot="profile"),
        tokens=LocalKV(user_id=user_id, slot="tokens"),
    )
