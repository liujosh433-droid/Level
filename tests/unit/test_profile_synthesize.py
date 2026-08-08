"""Profile snapshot / contradiction / calendar pattern tests."""

from __future__ import annotations

from level_core.profile.synthesize import (
    calendar_pattern_facts,
    detect_contradictions,
    synthesize_snapshot,
)
from level_core.schemas.signal import Fact, FactType


def test_detects_commitment_vs_constraint_tension() -> None:
    facts = [
        Fact(
            user_id="u1",
            type=FactType.COMMITMENT,
            statement="I promised to protect weeknight evenings with my kid.",
            salience=0.9,
        ),
        Fact(
            user_id="u1",
            type=FactType.CONCERN,
            statement="I'm worried taking a late shift means I miss bedtime on weeknights.",
            salience=0.8,
        ),
    ]
    cons = detect_contradictions(facts, user_id="u1")
    assert cons
    assert "weeknight" in cons[0].summary.lower() or "evening" in cons[0].summary.lower()


def test_snapshot_builds_bullets() -> None:
    facts = [
        Fact(
            user_id="u1",
            type=FactType.VALUE_STATEMENT,
            statement="I value being present for my daughter during the school year.",
            salience=0.9,
        ),
        Fact(
            user_id="u1",
            type=FactType.CONSTRAINT,
            statement="I can't work past 6pm on Mondays.",
            salience=0.8,
        ),
    ]
    snap = synthesize_snapshot(facts, user_id="u1")
    assert snap.fact_count == 2
    assert any(b.category.value == "value" for b in snap.bullets)
    assert any(b.category.value == "constraint" for b in snap.bullets)


def test_calendar_patterns_from_evening_load() -> None:
    stmts = [
        "On my calendar Mon Aug 10 2026 6:30PM: Muay thai",
        "On my calendar Wed Aug 12 2026 7:00PM: Class",
        "On my calendar Fri Aug 14 2026 5:30PM: Dinner",
        "On my calendar Tue Aug 18 2026 12:00PM: ULTRASOUND",
        "On my calendar Thu Aug 20 2026 1:00PM: dentist checkup",
    ]
    facts = calendar_pattern_facts(stmts, user_id="u1")
    types = {f.type for f in facts}
    assert FactType.CONSTRAINT in types
    assert any("evening" in f.statement.lower() for f in facts)
