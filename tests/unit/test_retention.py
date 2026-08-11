"""Retention policy: TTL + soft caps protect Keep'd / cited care facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from level_core.memory.fakes import build_in_memory_bank
from level_core.memory.retention import (
    RetentionPolicy,
    eviction_score,
    protected_fact_ids,
    prune_user_facts,
    select_facts_to_prune,
)
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    CareRoleState,
)
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType
from level_core.schemas.turn import ChallengeQuestion, Citation, Turn, TurnRole, TurnStatus


def _fact(
    fid: str,
    *,
    ftype: FactType = FactType.EVENT,
    salience: float = 0.4,
    days_ago: int = 0,
    statement: str | None = None,
) -> Fact:
    now = datetime.now(tz=timezone.utc)
    stamp = now - timedelta(days=days_ago)
    return Fact(
        fact_id=fid,  # type: ignore[call-arg]
        user_id="u1",
        type=ftype,
        statement=statement or f"Fact {fid}",
        salience=salience,
        created_at=stamp,
        updated_at=stamp,
    )


class TestSelectFactsToPrune:
    def test_ttl_drops_old_events_but_keeps_pinned(self) -> None:
        pinned = {"pin-1"}
        facts = [
            _fact("old-event", ftype=FactType.EVENT, days_ago=120),
            _fact("pin-1", ftype=FactType.RELATIONSHIP, salience=0.9, days_ago=200),
            _fact("fresh-event", ftype=FactType.EVENT, days_ago=5),
        ]
        doomed = select_facts_to_prune(
            facts,
            pinned=pinned,
            cited=set(),
            policy=RetentionPolicy(max_facts_per_user=150, event_ttl_days=90),
        )
        ids = {f.fact_id for f in doomed}
        assert "old-event" in ids
        assert "pin-1" not in ids
        assert "fresh-event" not in ids

    def test_soft_cap_prunes_lowest_score_first(self) -> None:
        facts = [
            _fact(f"e{i}", ftype=FactType.EVENT, salience=0.2 + i * 0.01, days_ago=10)
            for i in range(20)
        ]
        facts.append(
            _fact("keep-me", ftype=FactType.VALUE_STATEMENT, salience=0.95, days_ago=1)
        )
        doomed = select_facts_to_prune(
            facts,
            pinned={"keep-me"},
            cited=set(),
            policy=RetentionPolicy(max_facts_per_user=10, event_ttl_days=90),
        )
        assert len(doomed) == 11  # 21 total - 10 cap; keep-me protected
        assert all(f.fact_id != "keep-me" for f in doomed)

    def test_cited_facts_are_protected(self) -> None:
        facts = [_fact("cited", days_ago=200, ftype=FactType.EVENT)]
        doomed = select_facts_to_prune(
            facts,
            pinned=set(),
            cited={"cited"},
            policy=RetentionPolicy(max_facts_per_user=1, event_ttl_days=30),
        )
        assert doomed == []


class TestEvictionScore:
    def test_pinned_outranks_stale_event(self) -> None:
        now = datetime.now(tz=timezone.utc)
        pinned_f = _fact("p", ftype=FactType.RELATIONSHIP, salience=0.5, days_ago=100)
        stale = _fact("s", ftype=FactType.EVENT, salience=0.9, days_ago=100)
        assert eviction_score(
            pinned_f, now=now, pinned={"p"}, cited=set()
        ) > eviction_score(stale, now=now, pinned=set(), cited=set())


class TestPruneUserFacts:
    async def test_end_to_end_deletes_vectors_and_preserves_care_pins(self) -> None:
        memory = build_in_memory_bank()
        pin = _fact(
            "care-pin",
            ftype=FactType.RELATIONSHIP,
            salience=0.92,
            days_ago=400,
            statement="Child care — Maya pickup",
        )
        old = _fact("stale-cal", ftype=FactType.EVENT, days_ago=200)
        await memory.facts.upsert(pin)
        await memory.facts.upsert(old)
        await memory.vectors.upsert(
            user_id="u1", fact_id=pin.fact_id, text=pin.statement, embedding=[0.1] * 8
        )
        await memory.vectors.upsert(
            user_id="u1", fact_id=old.fact_id, text=old.statement, embedding=[0.2] * 8
        )
        await memory.manifestos.save_care_profile(
            CareProfile(
                user_id="u1",
                roles=[
                    CareRoleState(
                        role_id=CareRoleId.CHILD_CARE,
                        label=CARE_ROLE_LABELS[CareRoleId.CHILD_CARE],
                        status=BulletStatus.ACCEPTED,
                        source_fact_ids=[pin.fact_id],
                        salience=0.92,
                    )
                ],
            )
        )

        result = await prune_user_facts(
            memory,
            user_id="u1",
            policy=RetentionPolicy(max_facts_per_user=150, event_ttl_days=90),
        )
        assert result.pruned == 1
        assert old.fact_id in result.pruned_fact_ids
        remaining = await memory.facts.list_for_user(user_id="u1", limit=50)
        assert {f.fact_id for f in remaining} == {pin.fact_id}
        # Vector for stale gone; pin remains
        hits = await memory.vectors.query(
            user_id="u1", embedding=[0.1] * 8, top_k=5
        )
        assert all(h.fact_id != old.fact_id for h in hits)

    async def test_citation_protects_from_ttl(self) -> None:
        memory = build_in_memory_bank()
        cited = _fact("cited-event", ftype=FactType.EVENT, days_ago=200)
        await memory.facts.upsert(cited)
        decision = Decision(user_id="u1", status=DecisionStatus.OPEN)
        await memory.decisions.create(decision)
        await memory.decisions.append_turn(
            Turn(
                user_id="u1",
                decision_id=decision.decision_id,
                role=TurnRole.LEVEL,
                status=TurnStatus.COMPLETE,
                challenger_questions=[
                    ChallengeQuestion(
                        question="Does this steal from pickup you protected?",
                        challenge_type="role_theft",
                        citations=[
                            Citation(fact_id=cited.fact_id, quote="cited-event")
                        ],
                    )
                ],
            )
        )
        result = await prune_user_facts(
            memory,
            user_id="u1",
            policy=RetentionPolicy(max_facts_per_user=150, event_ttl_days=30),
        )
        assert result.pruned == 0
        left = await memory.facts.list_for_user(user_id="u1", limit=10)
        assert len(left) == 1

    def test_protected_fact_ids_from_care(self) -> None:
        care = CareProfile(
            user_id="u1",
            roles=[
                CareRoleState(
                    role_id=CareRoleId.CHILD_CARE,
                    label="Child care",
                    status=BulletStatus.ACCEPTED,
                    source_fact_ids=["a", "b"],
                ),
                CareRoleState(
                    role_id=CareRoleId.ELDER_CARE,
                    label="Elder care",
                    status=BulletStatus.REJECTED,
                    source_fact_ids=["c"],
                ),
            ],
        )
        assert protected_fact_ids(care) == {"a", "b"}
