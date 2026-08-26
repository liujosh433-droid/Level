"""LEVEL_ENV=local backend: one JSON file per collection under .level/local_store."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, TypeVar

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

# One lock per file so agenda writes don't stall people/admin reads.
_path_locks: dict[str, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    key = str(path)
    lock = _path_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _path_locks[key] = lock
    return lock


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

    def _lock(self) -> asyncio.Lock:
        return _lock_for(self._path)

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        return json.loads(self._path.read_text() or "[]")

    def _dump_unlocked(self, items: list[dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, default=str, indent=2))
        tmp.replace(self._path)

    async def _load(self) -> list[dict[str, Any]]:
        async with self._lock():
            return self._load_unlocked()

    async def _dump(self, items: list[dict[str, Any]]) -> None:
        async with self._lock():
            self._dump_unlocked(items)

    async def get(self, id_: str) -> T | None:
        for row in await self._load():
            if row.get(self._id_field) == id_:
                return self._model.model_validate(row)
        return None

    async def list(self) -> list[T]:
        return [self._model.model_validate(row) for row in await self._load()]

    async def upsert(self, item: T) -> T:
        async with self._lock():
            rows = self._load_unlocked()
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
                    self._dump_unlocked(rows)
                    return updated

            rows.append(payload)
            self._dump_unlocked(rows)
            return updated

    async def upsert_many(self, items: list[T]) -> None:
        if not items:
            return
        async with self._lock():
            rows = self._load_unlocked()
            index = {row.get(self._id_field): i for i, row in enumerate(rows)}
            now = datetime.utcnow()
            for item in items:
                id_ = getattr(item, self._id_field)
                updated = item.model_copy(update={"version": getattr(item, "version", 1)})
                if hasattr(updated, "updated_at"):
                    updated = updated.model_copy(update={"updated_at": now})
                payload = json.loads(updated.model_dump_json())
                if id_ in index:
                    payload["version"] = rows[index[id_]].get("version", 0) + 1
                    rows[index[id_]] = payload
                else:
                    index[id_] = len(rows)
                    rows.append(payload)
            self._dump_unlocked(rows)

    async def delete(self, id_: str) -> None:
        async with self._lock():
            rows = [r for r in self._load_unlocked() if r.get(self._id_field) != id_]
            self._dump_unlocked(rows)

    async def delete_many(self, ids: list[str]) -> None:
        if not ids:
            return
        drop = set(ids)
        async with self._lock():
            rows = [r for r in self._load_unlocked() if r.get(self._id_field) not in drop]
            self._dump_unlocked(rows)


class LocalKV(KVStore):
    def __init__(self, *, user_id: str, slot: str) -> None:
        self._path = _root_dir() / user_id / f"{slot}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_unlocked(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        raw = self._path.read_text().strip()
        if not raw:
            return None
        return json.loads(raw)

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, default=str, indent=2))
        tmp.replace(self._path)

    async def read(self) -> dict[str, Any] | None:
        async with _lock_for(self._path):
            return self._read_unlocked()

    async def write(self, value: dict[str, Any]) -> None:
        async with _lock_for(self._path):
            self._write_unlocked(value)

    async def update_fields(self, **fields: Any) -> None:
        """Atomic top-level merge of the given fields into the doc."""
        if not fields:
            return
        async with _lock_for(self._path):
            current = self._read_unlocked() or {}
            current.update(fields)
            self._write_unlocked(current)

    async def mutate(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Atomic read -> transform -> write, all under the file lock."""
        async with _lock_for(self._path):
            current = self._read_unlocked() or {}
            result = fn(dict(current))
            if inspect.isawaitable(result):
                result = await result
            assert isinstance(result, dict)
            self._write_unlocked(result)
            return result


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
