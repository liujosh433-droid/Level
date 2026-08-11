"""Tests for care-role collision detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from level_core.calendar.role_collisions import (
    find_role_collisions,
    role_theft_copy_for_conflicts,
    synthesize_demo_collision_event,
)
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
)
from level_core.schemas.profile import BulletStatus


def _care() -> CareProfile:
    thu = 3  # Thursday
    return CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label=CARE_ROLE_LABELS[CareRoleId.CHILD_CARE],
                salience=0.92,
                status=BulletStatus.ACCEPTED,
                people=["Maya"],
                protected_windows=[
                    ProtectedWindow(
                        label="Thursday ~15:00 care block",
                        weekday=thu,
                        start_hour=15,
                        end_hour=17,
                    )
                ],
            )
        ],
    )


class TestRoleCollisions:
    def test_finds_overlap_with_protected_window(self) -> None:
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)  # Monday
        # Next Thursday 16:00
        thu = now + timedelta(days=3)
        thu = thu.replace(hour=16, minute=0)
        hits = find_role_collisions(
            care=_care(),
            events=[{"summary": "Networking dinner", "start": thu.isoformat()}],
            now=now,
        )
        assert len(hits) == 1
        assert hits[0].role_id is CareRoleId.CHILD_CARE
        assert hits[0].confirmed is True
        assert "Maya" in hits[0].theft_message

    def test_synthesize_demo_event(self) -> None:
        ev = synthesize_demo_collision_event(_care())
        assert ev is not None
        assert "Networking" in (ev.get("summary") or "")

    def test_role_theft_copy(self) -> None:
        msg = role_theft_copy_for_conflicts(
            care=_care(),
            conflict_labels=["School pickup — Maya 3:15pm"],
        )
        assert msg is not None
        assert "Child care" in msg
        assert "Keep" in msg
