"""Memory Bank — Firestore + Vertex AI Vector Search behind clean interfaces.

Every downstream module (agents, API, jobs) depends only on the abstract
:mod:`level_core.memory.base` protocols. The Firestore-backed and
in-memory implementations are picked in :func:`build_memory` based on the
runtime mode. This keeps every unit test hermetic and every agent testable
against fakes.
"""

from level_core.memory.base import (
    DecisionRepository,
    FactRepository,
    ManifestoRepository,
    MemoryBank,
    SignalRepository,
    TurnRepository,
    VectorHit,
    VectorStore,
)
from level_core.memory.factory import build_memory
from level_core.memory.fakes import (
    InMemoryDecisionRepository,
    InMemoryFactRepository,
    InMemoryManifestoRepository,
    InMemoryMemoryBank,
    InMemorySignalRepository,
    InMemoryTurnRepository,
    InMemoryVectorStore,
)

__all__ = [
    "DecisionRepository",
    "FactRepository",
    "InMemoryDecisionRepository",
    "InMemoryFactRepository",
    "InMemoryManifestoRepository",
    "InMemoryMemoryBank",
    "InMemorySignalRepository",
    "InMemoryTurnRepository",
    "InMemoryVectorStore",
    "ManifestoRepository",
    "MemoryBank",
    "SignalRepository",
    "TurnRepository",
    "VectorHit",
    "VectorStore",
    "build_memory",
]
