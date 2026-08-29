from datetime import UTC, datetime

from level_core.tz import as_utc, resolve_tz, resolve_tz_name


def test_as_utc_makes_naive_and_aware_comparable() -> None:
    naive = datetime(2026, 8, 28, 12, 0, 0)
    aware = datetime(2026, 8, 28, 11, 0, 0, tzinfo=UTC)
    assert as_utc(naive).tzinfo is UTC
    assert as_utc(aware) is aware
    assert sorted([naive, aware], key=as_utc) == [aware, naive]


def test_prefers_first_valid_iana_name() -> None:
    assert resolve_tz_name("America/New_York", "America/Los_Angeles") == "America/New_York"


def test_skips_blank_and_invalid() -> None:
    assert resolve_tz_name("", "not-a-zone", "America/Denver") == "America/Denver"


def test_profile_tz_wins_over_fallback() -> None:
    from level_core.tz import tz_from_profile

    zone = tz_from_profile({"tz": "America/New_York"})
    assert zone.key == "America/New_York"


def test_falls_back_to_settings_calendar_tz() -> None:
    from level_core.config import get_settings

    assert resolve_tz_name(None, "") == get_settings().calendar_tz
    assert resolve_tz().key == get_settings().calendar_tz
