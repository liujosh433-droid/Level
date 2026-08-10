"""Priority inference from calendar — a step beyond event dumps."""

from __future__ import annotations

from level_core.profile.synthesize import infer_priority_facts, synthesize_snapshot
from level_core.schemas.signal import Fact, FactType


def test_infers_family_and_work_priorities() -> None:
    events = [
        {"summary": "Soccer practice — Jordan", "start": "2026-08-11T17:00:00-07:00"},
        {"summary": "Soccer practice — Jordan", "start": "2026-08-13T17:00:00-07:00"},
        {"summary": "School pickup — Jordan", "start": "2026-08-12T15:30:00-07:00"},
        {"summary": "Work standup", "start": "2026-08-11T09:00:00-07:00"},
        {"summary": "Client sync", "start": "2026-08-11T14:00:00-07:00"},
        {"summary": "Sprint planning", "start": "2026-08-12T10:00:00-07:00"},
        {"summary": "Visit Mom", "start": "2026-08-10T18:00:00-07:00"},
    ]
    facts = infer_priority_facts(events, user_id="u1")
    joined = " ".join(f.statement.lower() for f in facts)
    assert "jordan" in joined
    assert "family" in joined or "jordan" in joined
    assert "work" in joined
    assert "mom" in joined
    # No raw analytics dumps.
    assert "in this window" not in joined
    assert "appears repeatedly" not in joined


def test_synthesize_skips_analytics_facts() -> None:
    facts = [
        Fact(
            user_id="u1",
            type=FactType.CONSTRAINT,
            statement="School/child-related events show up repeatedly on my calendar and need protected time.",
            salience=0.9,
            confidence=0.9,
        ),
        Fact(
            user_id="u1",
            type=FactType.VALUE_STATEMENT,
            statement="Family time with Jordan — protecting school, sports, and their day-to-day",
            salience=0.95,
            confidence=0.9,
            written_by="agenda_priorities@v1",
        ),
    ]
    snap = synthesize_snapshot(facts, user_id="u1")
    assert len(snap.bullets) == 1
    assert "Jordan" in snap.bullets[0].text
    assert snap.bullets[0].category.value == "priority"
