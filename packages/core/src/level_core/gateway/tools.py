"""Register Memory Bank tools on the Agent Gateway for scoped agent access."""

from __future__ import annotations

from level_core.gateway.router import AgentGateway
from level_core.memory.base import MemoryBank
from level_core.models.base import EmbeddingClient


def register_memory_tools(
    gateway: AgentGateway,
    memory: MemoryBank,
    *,
    embedder: EmbeddingClient | None = None,
) -> AgentGateway:
    """Idempotent-ish: skips tools already registered."""

    async def get_care_profile(*, user_id: str):
        return await memory.manifestos.get_care_profile(user_id=user_id)

    async def get_manifesto(*, user_id: str):
        return await memory.manifestos.get_current_manifesto(user_id=user_id)

    async def get_facts(*, user_id: str, fact_ids: list[str] | None = None, limit: int = 100):
        if fact_ids:
            return await memory.facts.get_many(user_id=user_id, fact_ids=fact_ids)
        return await memory.facts.list_for_user(user_id=user_id, limit=limit)

    async def get_bias_profile(*, user_id: str):
        return await memory.manifestos.get_bias_profile(user_id=user_id)

    async def vector_search(*, user_id: str, embedding: list[float], top_k: int = 8):
        return await memory.vectors.query(user_id=user_id, embedding=embedding, top_k=top_k)

    async def embed_query(*, texts: list[str]):
        if embedder is None:
            raise RuntimeError("embedder not configured for gateway")
        return await embedder.embed(texts=texts)

    for name, handler in (
        ("get_care_profile", get_care_profile),
        ("get_manifesto", get_manifesto),
        ("get_facts", get_facts),
        ("get_bias_profile", get_bias_profile),
        ("vector_search", vector_search),
        ("embed_query", embed_query),
    ):
        if name not in gateway.registered_tools():
            gateway.register(name, handler)

    return gateway


__all__ = ["register_memory_tools"]
