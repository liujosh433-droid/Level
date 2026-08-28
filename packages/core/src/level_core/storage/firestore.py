"""LEVEL_ENV=cloud backend: Firestore per-user subtree.

Each collection lives at `users/{uid}/{collection}/{doc_id}`.
KV slots live at `users/{uid}/state/{slot}`.

All async methods here wrap the underlying google-cloud-firestore
Python client (which is synchronous) via ``asyncio.to_thread``. That
matters: the raw client would block the FastAPI event loop for the
duration of every Firestore round trip, so under any real concurrency
a single slow request would stall every other in-flight chat, sync,
or admin call on the same instance.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Generic, TypeVar

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
    kwargs: dict[str, Any] = {}
    if settings.google_cloud_project:
        kwargs["project"] = settings.google_cloud_project
    # Passing database="(default)" makes some client versions send
    # "%28default%29" and Firestore rejects it. Omit for the default DB.
    db = (settings.level_firestore_database or "").strip()
    if db and db not in {"(default)", "%28default%29"}:
        kwargs["database"] = db
    return firestore.Client(**kwargs)


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
        def _sync() -> dict[str, Any] | None:
            snap = self._col().document(id_).get()
            return snap.to_dict() if snap.exists else None

        data = await asyncio.to_thread(_sync)
        return self._model.model_validate(data) if data is not None else None

    async def list(self) -> list[T]:
        def _sync() -> list[dict[str, Any]]:
            return [d.to_dict() for d in self._col().stream()]

        rows = await asyncio.to_thread(_sync)
        return [self._model.model_validate(row) for row in rows]

    async def list_latest(
        self,
        limit: int,
        *,
        order_by: str = "created_at",
        desc: bool = True,
    ) -> list[T]:
        """Return the newest ``limit`` rows via an indexed query.

        Falls back to a full scan sorted in memory if the backend
        rejects the query (missing index, etc.); the caller's caller
        should still cap payload size regardless.
        """
        from google.cloud.firestore_v1.base_query import BaseQuery

        direction = BaseQuery.DESCENDING if desc else BaseQuery.ASCENDING

        def _sync() -> list[dict[str, Any]]:
            try:
                query = self._col().order_by(order_by, direction=direction).limit(limit)
                return [d.to_dict() for d in query.stream()]
            except Exception:
                # Missing index or unsupported order_by column — degrade
                # to full scan. Bounded by caller-side ceiling.
                rows = [d.to_dict() for d in self._col().stream()]
                rows.sort(key=lambda r: r.get(order_by, ""), reverse=desc)
                return rows[:limit]

        rows = await asyncio.to_thread(_sync)
        return [self._model.model_validate(r) for r in rows]

    async def list_since(
        self, since: datetime, *, field: str = "created_at"
    ) -> list[T]:
        """Return every row where ``field >= since``.

        Used by the gate to hydrate counters from ai_audit without
        scanning the full history. Falls back to full scan when the
        query fails.
        """
        cutoff = since.isoformat()

        def _sync() -> list[dict[str, Any]]:
            try:
                query = self._col().where(field, ">=", cutoff)
                return [d.to_dict() for d in query.stream()]
            except Exception:
                return [d.to_dict() for d in self._col().stream()]

        rows = await asyncio.to_thread(_sync)
        keep: list[T] = []
        for r in rows:
            try:
                model = self._model.model_validate(r)
            except Exception:  # noqa: BLE001
                continue
            keep.append(model)
        return keep

    async def upsert(self, item: T) -> T:
        id_ = getattr(item, self._id_field)
        payload = item.model_dump(mode="json")

        def _sync() -> dict[str, Any]:
            doc = self._col().document(id_)
            existing = doc.get()
            new_version = (
                (existing.to_dict() or {}).get("version", 0) + 1
                if existing.exists
                else 1
            )
            payload["version"] = new_version
            if "updated_at" in payload:
                payload["updated_at"] = datetime.now(UTC).isoformat()
            doc.set(payload)
            return payload

        result = await asyncio.to_thread(_sync)
        return self._model.model_validate(result)

    async def upsert_many(self, items: list[T]) -> None:
        if not items:
            return
        now = datetime.now(UTC).isoformat()
        serialized: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            id_ = getattr(item, self._id_field)
            payload = item.model_dump(mode="json")
            payload["version"] = int(payload.get("version") or 1) + 1
            if "updated_at" in payload:
                payload["updated_at"] = now
            serialized.append((id_, payload))

        def _sync() -> None:
            batch = self._client.batch()
            pending = 0
            for id_, payload in serialized:
                doc = self._col().document(id_)
                batch.set(doc, payload)
                pending += 1
                if pending >= 400:
                    batch.commit()
                    batch = self._client.batch()
                    pending = 0
            if pending:
                batch.commit()

        await asyncio.to_thread(_sync)

    async def delete(self, id_: str) -> None:
        await asyncio.to_thread(lambda: self._col().document(id_).delete())

    async def delete_many(self, ids: list[str]) -> None:
        if not ids:
            return

        def _sync() -> None:
            batch = self._client.batch()
            pending = 0
            for id_ in ids:
                batch.delete(self._col().document(id_))
                pending += 1
                if pending >= 400:
                    batch.commit()
                    batch = self._client.batch()
                    pending = 0
            if pending:
                batch.commit()

        await asyncio.to_thread(_sync)


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
        def _sync() -> dict[str, Any] | None:
            snap = self._doc().get()
            return snap.to_dict() if snap.exists else None

        return await asyncio.to_thread(_sync)

    async def write(self, value: dict[str, Any]) -> None:
        await asyncio.to_thread(lambda: self._doc().set(value))

    async def update_fields(self, **fields: Any) -> None:
        """Atomic top-level merge (Firestore native `set(..., merge=True)`)."""
        if not fields:
            return
        await asyncio.to_thread(lambda: self._doc().set(fields, merge=True))

    async def mutate(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Atomic read -> transform -> write via a Firestore transaction.

        The transaction body must be synchronous: Firestore's
        `@transactional` decorator drives it synchronously with
        auto-retry on contention. If a caller needs to `await` inside
        the transform, they must hoist that work above the mutate call
        (see `agents/gate.py::record_charge`).

        The entire transaction runs on a thread so we don't block the
        event loop for the duration of a Firestore round trip.
        """
        from google.cloud import firestore

        doc = self._doc()

        def _run() -> dict[str, Any]:
            @firestore.transactional  # type: ignore[misc]
            def _txn(transaction: Any) -> dict[str, Any]:
                snap = doc.get(transaction=transaction)
                current = snap.to_dict() if snap.exists else {}
                result = fn(dict(current))
                if inspect.isawaitable(result):
                    raise RuntimeError(
                        "FirestoreKV.mutate does not support async transform "
                        "functions; do async work outside the transaction."
                    )
                assert isinstance(result, dict)
                transaction.set(doc, result)
                return result

            return _txn(self._client.transaction())

        return await asyncio.to_thread(_run)


def make_firestore_store(user_id: str) -> UserStore:
    def repo(collection: str, model: type[BaseModel], id_field: str) -> FirestoreRepo:
        return FirestoreRepo(
            user_id=user_id, collection=collection, model=model, id_field=id_field
        )

    async def _reset_all() -> None:
        """Recursively delete every doc under ``users/{uid}``.

        Fast path for the demo reset - one SDK call that batches
        internally, replacing the previous "list every collection then
        delete_many" dance (which was 10 sequential round trips at
        ~50-150ms each before we could even start writing seed data).

        ``recursive_delete`` walks the subtree in parallel via the
        Firestore client's built-in batcher, so this is dramatically
        faster in the warm-slot case (previous judge polluted).
        """
        client = _client()
        await asyncio.to_thread(
            lambda: client.recursive_delete(
                client.collection("users").document(user_id)
            )
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
        reset_all=_reset_all,
    )
