"""The activity_type enum is the shared join key across usuals, reminders,
priorities, and agenda_cache - test it doesn't drift accidentally."""

from level_core.schemas import ALL_ACTIVITY_TYPES, ActivityType


def test_enum_values_are_dotted_or_flat() -> None:
    for at in ALL_ACTIVITY_TYPES:
        assert isinstance(at.value, str)
        assert at.value == at.value.lower()
        assert " " not in at.value


def test_no_duplicates() -> None:
    assert len(ALL_ACTIVITY_TYPES) == len(set(ALL_ACTIVITY_TYPES))


def test_common_types_present() -> None:
    for expected in (
        "sports.soccer",
        "school.pickup",
        "medical.appointment",
        "other",
    ):
        assert ActivityType(expected)
