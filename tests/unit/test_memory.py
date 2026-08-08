"""Tests for the in-memory Memory Bank implementations."""

from __future__ import annotations

import pytest

from level_core.errors import NotFound
from level_core.memory.fakes import (
    InMemoryDecisionRepository,
    InMemoryFactRepository,
    InMemorySignalRepository,
    InMemoryVectorStore,
)
from level_core.schemas.decision import Decision
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource
from level_core.schemas.turn import Turn, TurnRole


class TestSignalRepository:
    async def test_upsert_and_get(self) -> None:
        repo = InMemorySignalRepository()
        signal = Signal(
            user_id="u1",
            source=SignalSource.GCAL,
            external_id="event-1",
            text="Picture day",
        )
        await repo.upsert(signal)
        fetched = await repo.get(user_id="u1", signal_id=signal.signal_id)
        assert fetched.text == "Picture day"

    async def test_missing_raises_not_found(self) -> None:
        repo = InMemorySignalRepository()
        with pytest.raises(NotFound):
            await repo.get(user_id="u1", signal_id="does-not-exist")


class TestFactRepository:
    async def test_get_many_preserves_missing(self) -> None:
        repo = InMemoryFactRepository()
        fact = Fact(user_id="u1", type=FactType.VALUE_STATEMENT, statement="I value time with kids")
        await repo.upsert(fact)

        got = await repo.get_many(user_id="u1", fact_ids=[fact.fact_id, "missing"])
        assert len(got) == 1
        assert got[0].statement == "I value time with kids"


class TestDecisionRepository:
    async def test_create_get_update_turns(self) -> None:
        repo = InMemoryDecisionRepository()
        decision = Decision(user_id="u1")
        await repo.create(decision)

        fetched = await repo.get(user_id="u1", decision_id=decision.decision_id)
        assert fetched.user_id == "u1"

        turn = Turn(
            user_id="u1",
            decision_id=decision.decision_id,
            role=TurnRole.USER,
            user_text="Hello",
        )
        await repo.append_turn(turn)

        turns = await repo.list_turns(user_id="u1", decision_id=decision.decision_id)
        assert len(turns) == 1
        assert turns[0].user_text == "Hello"

    async def test_update_missing_raises(self) -> None:
        repo = InMemoryDecisionRepository()
        decision = Decision(user_id="u1")
        with pytest.raises(NotFound):
            await repo.update(decision)


class TestVectorStore:
    async def test_identical_text_gets_perfect_score(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            user_id="u1", fact_id="f1", text="alpha", embedding=[1.0, 0.0, 0.0]
        )
        hits = await store.query(user_id="u1", embedding=[1.0, 0.0, 0.0], top_k=1)
        assert len(hits) == 1
        assert hits[0].fact_id == "f1"
        assert hits[0].score == pytest.approx(1.0)

    async def test_tenant_isolation(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            user_id="alice", fact_id="fa", text="alpha", embedding=[1.0, 0.0]
        )
        await store.upsert(
            user_id="bob", fact_id="fb", text="alpha", embedding=[1.0, 0.0]
        )
        hits = await store.query(user_id="alice", embedding=[1.0, 0.0], top_k=5)
        assert [h.fact_id for h in hits] == ["fa"]

    async def test_delete(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            user_id="u1", fact_id="f1", text="alpha", embedding=[1.0, 0.0]
        )
        await store.delete(user_id="u1", fact_id="f1")
        hits = await store.query(user_id="u1", embedding=[1.0, 0.0], top_k=5)
        assert hits == []
