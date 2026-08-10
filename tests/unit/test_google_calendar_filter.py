"""Calendar window + recurring/noise filter + Drive topic match tests."""

from __future__ import annotations

from datetime import datetime, timezone

from level_core.ingest.google_live import (
    _parse_when,
    calendar_window,
    drive_topic_score,
    filter_calendar_events,
    topics_from_calendar_titles,
)


def test_calendar_window_is_tight_near_term() -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    start, end = calendar_window(now)
    assert start == datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    assert end.day == 12 and end.month == 9  # +28 days from Aug 15


def test_skips_google_recurring_and_high_frequency_titles() -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    items = [
        {
            "id": "r1",
            "recurringEventId": "series-nav",
            "summary": "NAVIGUARD",
            "start": {"dateTime": "2026-08-15T09:00:00+00:00"},
        },
        {
            "id": "a1",
            "summary": "AMC should NOT BILL",
            "start": {"dateTime": "2026-08-10T09:00:00+00:00"},
        },
        {
            "id": "a2",
            "summary": "AMC should NOT BILL",
            "start": {"dateTime": "2026-08-11T09:00:00+00:00"},
        },
        {
            "id": "a3",
            "summary": "AMC should NOT BILL",
            "start": {"dateTime": "2026-08-12T09:00:00+00:00"},
        },
        {
            "id": "u1",
            "summary": "Parent-teacher conference",
            "start": {"dateTime": "2026-08-20T17:00:00+00:00"},
            "description": "Bring report card",
        },
        {
            "id": "u2",
            "summary": "Muay thai",
            "start": {"dateTime": "2026-08-16T18:00:00+00:00"},
        },
    ]
    kept = filter_calendar_events(items, now=now, max_events=20)
    titles = {(e.get("summary") or "") for e in kept}
    assert "NAVIGUARD" not in titles
    assert "AMC should NOT BILL" not in titles
    assert "Parent-teacher conference" in titles
    assert "Muay thai" in titles


def test_parse_when_all_day_is_utc_aware() -> None:
    dt = _parse_when("2026-08-08")
    assert dt is not None
    assert dt.tzinfo is not None
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    # Must not raise (naive vs aware subtraction).
    assert abs((dt - now).total_seconds()) > 0


def test_filter_accepts_all_day_events() -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    items = [
        {
            "id": "d1",
            "summary": "Co-parent weekend",
            "start": {"date": "2026-08-16"},
        },
        {
            "id": "t1",
            "summary": "Night class",
            "start": {"dateTime": "2026-08-17T18:30:00-07:00"},
        },
    ]
    kept = filter_calendar_events(items, now=now, max_events=10)
    titles = {(e.get("summary") or "") for e in kept}
    assert "Co-parent weekend" in titles
    assert "Night class" in titles


def test_drive_only_matches_calendar_topics() -> None:
    topics = topics_from_calendar_titles(
        ["ULTRASOUND", "Muay thai", "UT Austin PDR", "Japan trip"]
    )
    assert "ultrasound" in topics
    assert "japan" in topics
    # Random solar-car doc should not match these personal calendar topics.
    assert drive_topic_score("Solstice 2025-2026 budget", "battery HV array", topics) == 0
    assert drive_topic_score("Ultrasound prep notes", "", topics) > 0
    assert drive_topic_score("Random notes", "flight itinerary for japan in november", topics) > 0
