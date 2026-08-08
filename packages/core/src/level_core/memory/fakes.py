"""In-memory implementations of the Memory Bank protocols.

Used by every unit test and by local dev mode. Not thread-safe (tests are
single-threaded); not durable (data lost on process exit — that's the point).

The vector store implementation uses cosine similarity over the provided
embeddings. For local dev we don't need FAISS — a hundred facts at a time is
trivially fast to iterate over.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from level_core.errors import NotFound
from level_core.memory.base import MemoryBank, VectorHit
from level_core.schemas.bias import BiasEvent, BiasProfile, Manifesto
from level_core.schemas.decision import Decision
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal
from level_core.schemas.turn import Turn


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(slots=True)
class InMemorySignalRepository:
    _by_user: dict[str, dict[str, Signal]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    async def upsert(self, signal: Signal) -> None:
        self._by_user[signal.user_id][signal.signal_id] = signal

    async def get(self, *, user_id: str, signal_id: str) -> Signal:
        try:
            return self._by_user[user_id][signal_id]
        except KeyError as exc:
            raise NotFound("signals", signal_id) from exc

    async def list_by_source(
        self, *, user_id: str, source: str, since_cursor: str | None = None  # noqa: ARG002
    ) -> list[Signal]:
        return [s for s in self._by_user[user_id].values() if s.source.value == source]

    async def clear_for_user(self, *, user_id: str) -> int:
        n = len(self._by_user.get(user_id, {}))
        self._by_user.pop(user_id, None)
        return n


@dataclass(slots=True)
class InMemoryFactRepository:
    _by_user: dict[str, dict[str, Fact]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    async def upsert(self, fact: Fact) -> None:
        self._by_user[fact.user_id][fact.fact_id] = fact

    async def get(self, *, user_id: str, fact_id: str) -> Fact:
        try:
            return self._by_user[user_id][fact_id]
        except KeyError as exc:
            raise NotFound("facts", fact_id) from exc

    async def get_many(self, *, user_id: str, fact_ids: Iterable[str]) -> list[Fact]:
        bucket = self._by_user.get(user_id, {})
        return [bucket[fid] for fid in fact_ids if fid in bucket]

    async def list_for_user(self, *, user_id: str, limit: int = 100) -> list[Fact]:
        return list(self._by_user.get(user_id, {}).values())[:limit]

    async def clear_for_user(self, *, user_id: str) -> int:
        n = len(self._by_user.get(user_id, {}))
        self._by_user.pop(user_id, None)
        return n


@dataclass(slots=True)
class InMemoryDecisionRepository:
    _decisions: dict[tuple[str, str], Decision] = field(default_factory=dict)
    _turns: dict[tuple[str, str], list[Turn]] = field(
        default_factory=lambda: defaultdict(list)
    )

    async def create(self, decision: Decision) -> None:
        self._decisions[(decision.user_id, decision.decision_id)] = decision

    async def get(self, *, user_id: str, decision_id: str) -> Decision:
        try:
            return self._decisions[(user_id, decision_id)]
        except KeyError as exc:
            raise NotFound("decisions", decision_id) from exc

    async def update(self, decision: Decision) -> None:
        key = (decision.user_id, decision.decision_id)
        if key not in self._decisions:
            raise NotFound("decisions", decision.decision_id)
        decision.touch()
        self._decisions[key] = decision

    async def append_turn(self, turn: Turn) -> None:
        self._turns[(turn.user_id, turn.decision_id)].append(turn)

    async def list_turns(self, *, user_id: str, decision_id: str) -> list[Turn]:
        return list(self._turns.get((user_id, decision_id), []))


@dataclass(slots=True)
class InMemoryTurnRepository:
    _bias_events: dict[str, list[BiasEvent]] = field(
        default_factory=lambda: defaultdict(list)
    )

    async def append_bias_event(self, event: BiasEvent) -> None:
        self._bias_events[event.user_id].append(event)

    async def list_bias_events_for_user(
        self, *, user_id: str, limit: int = 500
    ) -> list[BiasEvent]:
        return list(self._bias_events.get(user_id, []))[-limit:]


@dataclass(slots=True)
class InMemoryManifestoRepository:
    _manifestos: dict[str, Manifesto] = field(default_factory=dict)
    _bias_profiles: dict[str, BiasProfile] = field(default_factory=dict)
    _snapshots: dict[str, ProfileSnapshot] = field(default_factory=dict)

    async def get_current_manifesto(self, *, user_id: str) -> Manifesto | None:
        return self._manifestos.get(user_id)

    async def save_manifesto(self, manifesto: Manifesto) -> None:
        self._manifestos[manifesto.user_id] = manifesto

    async def get_bias_profile(self, *, user_id: str) -> BiasProfile | None:
        return self._bias_profiles.get(user_id)

    async def save_bias_profile(self, profile: BiasProfile) -> None:
        self._bias_profiles[profile.user_id] = profile

    async def get_profile_snapshot(self, *, user_id: str) -> ProfileSnapshot | None:
        return self._snapshots.get(user_id)

    async def save_profile_snapshot(self, snapshot: ProfileSnapshot) -> None:
        self._snapshots[snapshot.user_id] = snapshot

    async def clear_for_user(self, *, user_id: str) -> int:
        n = 0
        if self._manifestos.pop(user_id, None) is not None:
            n += 1
        if self._bias_profiles.pop(user_id, None) is not None:
            n += 1
        if self._snapshots.pop(user_id, None) is not None:
            n += 1
        return n


@dataclass(slots=True)
class _VectorEntry:
    fact_id: str
    text: str
    embedding: list[float]


@dataclass(slots=True)
class InMemoryVectorStore:
    _by_user: dict[str, dict[str, _VectorEntry]] = field(
        default_factory=lambda: defaultdict(dict)
    )

    async def upsert(
        self,
        *,
        user_id: str,
        fact_id: str,
        text: str,
        embedding: list[float],
    ) -> None:
        self._by_user[user_id][fact_id] = _VectorEntry(
            fact_id=fact_id, text=text, embedding=list(embedding)
        )

    async def query(
        self, *, user_id: str, embedding: list[float], top_k: int = 8
    ) -> list[VectorHit]:
        entries = self._by_user.get(user_id, {}).values()
        scored = [
            VectorHit(
                fact_id=e.fact_id,
                score=_cosine(embedding, e.embedding),
                text=e.text,
            )
            for e in entries
        ]
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    async def delete(self, *, user_id: str, fact_id: str) -> None:
        self._by_user.get(user_id, {}).pop(fact_id, None)

    async def clear_for_user(self, *, user_id: str) -> int:
        n = len(self._by_user.get(user_id, {}))
        self._by_user.pop(user_id, None)
        return n


def build_in_memory_bank() -> MemoryBank:
    """Convenience factory for tests + local dev."""
    return MemoryBank(
        signals=InMemorySignalRepository(),
        facts=InMemoryFactRepository(),
        decisions=InMemoryDecisionRepository(),
        turns=InMemoryTurnRepository(),
        manifestos=InMemoryManifestoRepository(),
        vectors=InMemoryVectorStore(),
    )


InMemoryMemoryBank = build_in_memory_bank


__all__ = [
    "InMemoryDecisionRepository",
    "InMemoryFactRepository",
    "InMemoryManifestoRepository",
    "InMemoryMemoryBank",
    "InMemorySignalRepository",
    "InMemoryTurnRepository",
    "InMemoryVectorStore",
    "build_in_memory_bank",
]
