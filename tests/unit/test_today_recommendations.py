"""Today reminders come from event cues — not invented profile/fact copy."""

from __future__ import annotations

from level_core.profile.today import build_recommendations
from level_core.schemas.profile import (
    BulletCategory,
    BulletStatus,
    ProfileBullet,
    ProfileSnapshot,
)
from level_core.schemas.signal import Fact, FactType


def _snapshot(*texts: str) -> ProfileSnapshot:
    bullets = [
        ProfileBullet(
            bullet_id=f"b{i}",
            category=BulletCategory.CONSTRAINT,
            text=text,
            status=BulletStatus.ACCEPTED,
        )
        for i, text in enumerate(texts)
    ]
    return ProfileSnapshot(user_id="u1", bullets=bullets, needs_review=False)


def test_rejects_calendar_pattern_bullets() -> None:
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Soccer practice — Jordan",
                "start": "2026-08-10T17:00:00-07:00",
                "cues": [],
            },
            {
                "summary": "Dentist — Jordan",
                "start": "2026-08-10T10:00:00-07:00",
                "cues": [],
            },
            {
                "summary": "Team dinner",
                "start": "2026-08-10T19:00:00-07:00",
                "cues": [],
            },
        ],
        snapshot=_snapshot(
            "School/child-related events show up repeatedly on my calendar and need protected time.",
            "I have multiple medical/health appointments on my calendar (4 in this window).",
            "My calendar shows frequent evening commitments (13 in the current window), so weeknights are packed.",
        ),
        facts=[],
    )
    assert recs == []


def test_cue_for_soccer_becomes_reminder() -> None:
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Soccer practice — Jordan",
                "start": "2026-08-10T17:00:00-07:00",
                "cues": ["Don't forget Jordan's shoes today!"],
            }
        ],
        snapshot=_snapshot(
            "School/child-related events show up repeatedly on my calendar and need protected time.",
        ),
        facts=[],
    )
    assert len(recs) == 1
    assert "shoes" in recs[0].lower()
    assert "repeatedly" not in recs[0].lower()


def test_profile_note_alone_does_not_become_reminder() -> None:
    """Matching notes used to invent Remember: tips — cues own day tips now."""
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Soccer practice — Jordan",
                "start": "2026-08-10T17:00:00-07:00",
                "cues": [],
            }
        ],
        snapshot=_snapshot(
            "Jordan often forgets soccer shoes for practice.",
        ),
        facts=[],
    )
    assert recs == []


def test_unrelated_profile_note_ignored() -> None:
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Catch up on work email",
                "start": "2026-08-10T09:00:00-07:00",
                "cues": [],
            }
        ],
        snapshot=_snapshot("Jordan often forgets soccer shoes for practice."),
        facts=[],
    )
    assert recs == []


def test_matching_fact_alone_does_not_become_reminder() -> None:
    fact = Fact(
        fact_id="f1",
        user_id="u1",
        type=FactType.CONSTRAINT,
        statement="Pack inhaler for Jordan's soccer games.",
        confidence=0.9,
    )
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Soccer practice — Jordan",
                "start": "2026-08-10T17:00:00-07:00",
                "cues": [],
            }
        ],
        snapshot=None,
        facts=[fact],
    )
    assert recs == []


def test_care_role_inventory_never_becomes_reminder() -> None:
    """Care graph evidence / role bullets are not day tips."""
    role_snap = ProfileSnapshot(
        user_id="u1",
        bullets=[
            ProfileBullet(
                bullet_id="r1",
                category=BulletCategory.ROLE,
                text="Child care with Theo, Nova",
                status=BulletStatus.ACCEPTED,
                care_role_id="child_care",
            )
        ],
        needs_review=False,
    )
    dump = Fact(
        fact_id="f_dump",
        user_id="u1",
        type=FactType.COMMITMENT,
        statement=(
            "I hold Numerous work-related entries including "
            "'Interview panel', 'Standup', 'sync w/ CareOps'"
        ),
        confidence=0.9,
        written_by="care_infer@v2",
    )
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Standup",
                "start": "2026-08-10T09:00:00-07:00",
                "cues": [],
            },
            {
                "summary": "Theo dentist (cleaning)",
                "start": "2026-08-10T15:30:00-07:00",
                "cues": [],
            },
        ],
        snapshot=role_snap,
        facts=[dump],
    )
    assert recs == []


def test_reminder_not_ellipsis_truncated() -> None:
    long_cue = (
        "Bring Theo's retainer case and the insurance card for the dentist visit "
        "after school pickup — do not leave them in the car."
    )
    recs = build_recommendations(
        today_events=[
            {
                "summary": "Theo dentist (cleaning)",
                "start": "2026-08-10T15:30:00-07:00",
                "cues": [long_cue],
            }
        ],
        snapshot=None,
        facts=[],
    )
    assert len(recs) == 1
    assert "…" not in recs[0]
    assert "insurance card" in recs[0].lower()
