"""Vertex AI Vector Search implementation of the VectorStore protocol.

Enforces the multi-tenant ``user_id`` restrict on every query — a bug here
would be a cross-tenant data leak, so we defend at the API boundary rather
than trusting the caller.

Import is lazy so environments that only ever use the in-memory fake don't
need the aiplatform SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from level_core.config import Settings, get_settings
from level_core.memory.base import VectorHit
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced

if TYPE_CHECKING:
    from google.cloud.aiplatform import MatchingEngineIndex, MatchingEngineIndexEndpoint

_logger = get_logger(__name__)


class VertexVectorStore:
    """Vertex AI Vector Search implementation.

    Each upsert also writes a ``restricts=[{namespace: 'user_id', allow: [user_id]}]``
    entry so subsequent queries can filter to a single tenant.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        index: MatchingEngineIndex | None = None,
        endpoint: MatchingEngineIndexEndpoint | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._index = index
        self._endpoint = endpoint

    def _ensure(self) -> tuple[MatchingEngineIndex, MatchingEngineIndexEndpoint]:
        if self._index is not None and self._endpoint is not None:
            return self._index, self._endpoint

        from google.cloud.aiplatform import (
            MatchingEngineIndex,
            MatchingEngineIndexEndpoint,
            init,
        )

        init(project=self._settings.gcp_project, location=self._settings.gcp_region)
        self._index = MatchingEngineIndex(index_name=self._settings.vector_index_id)
        self._endpoint = MatchingEngineIndexEndpoint(
            index_endpoint_name=self._settings.vector_index_endpoint_id
        )
        return self._index, self._endpoint

    @traced("vector.vertex.upsert")
    async def upsert(
        self,
        *,
        user_id: str,
        fact_id: str,
        text: str,  # noqa: ARG002
        embedding: list[float],
    ) -> None:
        index, _ = self._ensure()
        # Vertex AI accepts a list of Datapoint dataclasses. We attach a
        # restrict namespace so queries can filter to this user_id.
        from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import (
            Namespace,
        )
        from google.cloud.aiplatform_v1.types import IndexDatapoint

        datapoint = IndexDatapoint(
            datapoint_id=fact_id,
            feature_vector=embedding,
            restricts=[IndexDatapoint.Restriction(namespace="user_id", allow_list=[user_id])],
        )
        # Note: upsert_datapoints requires an "index" (not endpoint). Async
        # SDK support is uneven; we run it in a thread to keep our interface
        # async without blocking.
        import asyncio

        _ = Namespace  # ensure import isn't marked unused (namespace is passed via restricts)
        await asyncio.to_thread(index.upsert_datapoints, [datapoint])

    @traced("vector.vertex.query")
    async def query(
        self,
        *,
        user_id: str,
        embedding: list[float],
        top_k: int = 8,
    ) -> list[VectorHit]:
        _, endpoint = self._ensure()

        from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import (
            Namespace,
        )
        import asyncio

        def _run() -> list[VectorHit]:
            response = endpoint.find_neighbors(
                deployed_index_id=self._settings.vector_deployed_index_id,
                queries=[embedding],
                num_neighbors=top_k,
                filter=[Namespace(name="user_id", allow_tokens=[user_id])],
            )
            hits: list[VectorHit] = []
            if not response:
                return hits
            for neighbors in response:
                for n in neighbors:
                    hits.append(VectorHit(fact_id=n.id, score=float(n.distance), text=""))
            return hits

        return await asyncio.to_thread(_run)

    @traced("vector.vertex.delete")
    async def delete(self, *, user_id: str, fact_id: str) -> None:  # noqa: ARG002
        index, _ = self._ensure()
        import asyncio

        await asyncio.to_thread(index.remove_datapoints, [fact_id])


__all__ = ["VertexVectorStore"]
