"""Firestore-backed VectorStore — works before Vertex Index deploy finishes.

Stores embeddings under ``users/{uid}/embeddings/{fact_id}`` and ranks with
cosine similarity in-process. Fine for hackathon volumes (hundreds–thousands
of facts). Swap to :class:`VertexVectorStore` when the Index Endpoint is live.
"""

from __future__ import annotations

import math
from typing import Any

from level_core.config import Settings, get_settings
from level_core.memory.base import VectorHit
from level_core.observability.tracer import traced


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class FirestoreVectorStore:
    """Cosine search over embeddings persisted in Firestore."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = client

    def _db(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud.firestore_v1 import AsyncClient

        self._client = AsyncClient(
            project=self._settings.gcp_project,
            database=self._settings.firestore_database,
        )
        return self._client

    def _col(self, user_id: str) -> Any:
        return self._db().collection("users").document(user_id).collection("embeddings")

    @traced("vector.firestore.upsert")
    async def upsert(
        self,
        *,
        user_id: str,
        fact_id: str,
        text: str,
        embedding: list[float],
    ) -> None:
        await self._col(user_id).document(fact_id).set(
            {"fact_id": fact_id, "text": text, "embedding": embedding},
            merge=True,
        )

    @traced("vector.firestore.query")
    async def query(
        self,
        *,
        user_id: str,
        embedding: list[float],
        top_k: int = 8,
    ) -> list[VectorHit]:
        docs = self._col(user_id).stream()
        scored: list[VectorHit] = []
        async for doc in docs:
            data = doc.to_dict() or {}
            vec = data.get("embedding") or []
            if not isinstance(vec, list):
                continue
            scored.append(
                VectorHit(
                    fact_id=str(data.get("fact_id") or doc.id),
                    score=_cosine(embedding, [float(x) for x in vec]),
                    text=str(data.get("text") or ""),
                )
            )
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    @traced("vector.firestore.delete")
    async def delete(self, *, user_id: str, fact_id: str) -> None:
        await self._col(user_id).document(fact_id).delete()


__all__ = ["FirestoreVectorStore"]
