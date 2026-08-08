"""Factory that builds the right :class:`MemoryBank` for the current mode.

Callers never construct repositories directly — they always go through
``build_memory()`` (or accept a ``MemoryBank`` injected by tests).
"""

from __future__ import annotations

from level_core.config import Settings, get_settings
from level_core.memory.base import MemoryBank
from level_core.memory.fakes import build_in_memory_bank


def build_memory(settings: Settings | None = None) -> MemoryBank:
    """Return a MemoryBank appropriate for the current runtime.

    In local mode this is the in-process fake. In cloud mode the Firestore
    + Vertex Vector Search implementations are wired together. Real-mode
    imports are lazy so local dev doesn't require the aiplatform SDK.
    """
    settings = settings or get_settings()

    if settings.is_local:
        return build_in_memory_bank()

    from level_core.memory.firestore_store import (
        FirestoreDecisionRepository,
        FirestoreFactRepository,
        FirestoreManifestoRepository,
        FirestoreSignalRepository,
        FirestoreTurnRepository,
    )

    if settings.vector_backend == "vertex":
        from level_core.memory.vector_store import VertexVectorStore

        vectors = VertexVectorStore(settings=settings)
    else:
        from level_core.memory.firestore_vectors import FirestoreVectorStore

        vectors = FirestoreVectorStore(settings=settings)

    return MemoryBank(
        signals=FirestoreSignalRepository(),
        facts=FirestoreFactRepository(),
        decisions=FirestoreDecisionRepository(),
        turns=FirestoreTurnRepository(),
        manifestos=FirestoreManifestoRepository(),
        vectors=vectors,
    )


__all__ = ["build_memory"]
