from level_core.calendar.activity_art import activity_color, infer_activity_kind
from level_core.calendar.event_cues import EventCue, match_cues_for_summary
from level_core.profile.today import build_tomorrow_preview


def test_infer_activity_kinds() -> None:
    # Role is fallback when the title is generic; specific title cues win.
    assert infer_activity_kind("Random block", care_role="paid_work") == "work"
    assert infer_activity_kind("Soccer practice — Jordan") == "sports"
    assert infer_activity_kind("School pickup — Jordan") == "school"
    assert infer_activity_kind("Catch up on work email") == "work"
    assert infer_activity_kind("Dentist — Jordan") == "medical"
    # child_care used to force "school" even for dentist visits.
    assert (
        infer_activity_kind("Theo dentist (cleaning)", care_role="child_care")
        == "medical"
    )
    assert infer_activity_kind("Dinner with Diane") == "food"
    assert infer_activity_kind("Co-parent weekend") == "family"
    assert infer_activity_kind("Mystery block") == "generic"
    assert activity_color("sports") != activity_color("school")


def test_tomorrow_preview_includes_cues() -> None:
    summary, remember = build_tomorrow_preview(
        tomorrow_events=[
            {
                "summary": "Soccer practice — Jordan",
                "start": "2026-08-11T17:00:00-07:00",
                "all_day": False,
            }
        ],
        weekday_label="Monday",
        cues_by_event=[["Don't forget Jordan's shoes today!"]],
    )
    assert "1 event" in summary
    assert "shoes" in remember[0].lower()


def test_match_cues_for_soccer() -> None:
    cues = [
        EventCue(
            user_id="u1",
            keywords=["soccer", "practice"],
            reminder="Don't forget Jordan's shoes today!",
        )
    ]
    matched = match_cues_for_summary("Soccer practice — Jordan", cues)
    assert matched == ["Don't forget Jordan's shoes today!"]
    assert match_cues_for_summary("Catch up on work email", cues) == []
