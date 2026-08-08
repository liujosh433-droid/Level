"""Protocol definitions for the Memory Bank.

Everything downstream of the Memory Bank (agents, API routes, jobs) depends
only on these protocols. The Firestore-backed implementation and the
in-memory fake implementation both satisfy them.

Using ``Protocol`` rather than an abstract base class means test doubles
don't have to inherit from anything — as long as they have the right shape
they type-check.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from level_core.schemas.bias import BiasEvent, BiasProfile, Manifesto
from level_core.schemas.decision import Decision
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal
from level_core.schemas.turn import Turn


# --- Vector Store -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One result from a semantic search."""

    fact_id: str
    score: float
    text: str


class VectorStore(Protocol):
    """Persistent semantic index over user facts.

    Implementations MUST enforce the ``user_id`` restrict at query time —
    a bug here is a cross-tenant data leak.
    """

    async def upsert(
        self,
        *,
        user_id: str,
        fact_id: str,
        text: str,
        embedding: list[float],
    ) -> None: ...

    async def query(
        self,
        *,
        user_id: str,
        embedding: list[float],
        top_k: int = 8,
    ) -> list[VectorHit]: ...

    async def delete(self, *, user_id: str, fact_id: str) -> None: ...


# --- Structured stores ------------------------------------------------------


class SignalRepository(Protocol):
    """Raw ingested signals."""

    async def upsert(self, signal: Signal) -> None: ...
    async def get(self, *, user_id: str, signal_id: str) -> Signal: ...
    async def list_by_source(
        self, *, user_id: str, source: str, since_cursor: str | None = None
    ) -> list[Signal]: ...


class FactRepository(Protocol):
    """Structured facts extracted from signals."""

    async def upsert(self, fact: Fact) -> None: ...
    async def get(self, *, user_id: str, fact_id: str) -> Fact: ...
    async def get_many(self, *, user_id: str, fact_ids: Iterable[str]) -> list[Fact]: ...
    async def list_for_user(self, *, user_id: str, limit: int = 100) -> list[Fact]: ...


class DecisionRepository(Protocol):
    """Decisions and their turns."""

    async def create(self, decision: Decision) -> None: ...
    async def get(self, *, user_id: str, decision_id: str) -> Decision: ...
    async def update(self, decision: Decision) -> None: ...
    async def append_turn(self, turn: Turn) -> None: ...
    async def list_turns(self, *, user_id: str, decision_id: str) -> list[Turn]: ...


class TurnRepository(Protocol):
    """Turn-scoped operations (bias events).

    Turns themselves are appended via ``DecisionRepository.append_turn``.
    This repository handles the cross-turn ``bias_events`` write path.
    """

    async def append_bias_event(self, event: BiasEvent) -> None: ...
    async def list_bias_events_for_user(
        self, *, user_id: str, limit: int = 500
    ) -> list[BiasEvent]: ...


class ManifestoRepository(Protocol):
    """Persistence for the user's evolving manifesto + bias profile."""

    async def get_current_manifesto(self, *, user_id: str) -> Manifesto | None: ...
    async def save_manifesto(self, manifesto: Manifesto) -> None: ...
    async def get_bias_profile(self, *, user_id: str) -> BiasProfile | None: ...
    async def save_bias_profile(self, profile: BiasProfile) -> None: ...
    async def get_profile_snapshot(self, *, user_id: str) -> ProfileSnapshot | None: ...
    async def save_profile_snapshot(self, snapshot: ProfileSnapshot) -> None: ...


@dataclass(slots=True)
class MemoryBank:
    """Convenience container that holds all repositories together.

    Passed as a single dependency into agents and API routes so we don't
    have to keep growing constructor argument lists.
    """

    signals: SignalRepository
    facts: FactRepository
    decisions: DecisionRepository
    turns: TurnRepository
    manifestos: ManifestoRepository
    vectors: VectorStore


__all__ = [
    "DecisionRepository",
    "FactRepository",
    "ManifestoRepository",
    "MemoryBank",
    "SignalRepository",
    "TurnRepository",
    "VectorHit",
    "VectorStore",
]
