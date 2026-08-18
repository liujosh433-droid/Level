"""Routine categories for Keep'd usuals — display and short calendar titles.

Does not invent people or slots. Classifies an already-detected usual from
titles the agenda already attached, with a child hour-band fallback.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from level_core.schemas.care import CarePerson, CareRoleId, UsualWindow

# Same zone usual inference uses (see level_core.calendar.usuals.DEFAULT_TZ).
CALENDAR_TZ = ZoneInfo("America/Los_Angeles")

ROUTINE_PICKUP = "pickup"
ROUTINE_SCHOOL = "school"
ROUTINE_ACTIVITY = "activity"
ROUTINE_CLINIC = "clinic"
ROUTINE_USUAL = "usual"

_KNOWN = frozenset(
    {ROUTINE_PICKUP, ROUTINE_SCHOOL, ROUTINE_ACTIVITY, ROUTINE_CLINIC, ROUTINE_USUAL}
)

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# First match wins. Pickup before school so "school run" / "after-school pickup" stay pickup.
_TITLE_ROUTINES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        ROUTINE_PICKUP,
        (
            "pickup",
            "pick up",
            "pick-up",
            "drop-off",
            "drop off",
            "dropoff",
            "school run",
            "carpool",
        ),
    ),
    (
        ROUTINE_SCHOOL,
        (
            "school",
            "preschool",
            "kindergarten",
            "daycare",
            "classroom",
            "homeroom",
        ),
    ),
    (
        ROUTINE_ACTIVITY,
        (
            "soccer",
            "practice",
            "game",
            "swim",
            "ballet",
            "piano",
            "lesson",
            "club",
            "robotics",
            "sports",
            "rehearsal",
            "activity",
            "after school",
            "after-school",
        ),
    ),
    (
        ROUTINE_CLINIC,
        (
            "clinic",
            "doctor",
            "dentist",
            "pediatric",
            "therapy",
            "appointment",
            "meds",
        ),
    ),
)


def _norm_title(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _blob(titles: list[str]) -> str:
    return " ".join(_norm_title(t) for t in titles if t)


def normalize_routine(raw: str) -> str:
    """Allow-listed routine word, or empty if Gemini/user sent something else."""
    kind = (raw or "").strip().lower()
    return kind if kind in _KNOWN else ""


def classify_routine(
    *,
    titles: list[str],
    start_minute: int = 15 * 60,
    care_role_id: str = "",
    routine_by_summary: dict[str, str] | None = None,
) -> str:
    """Gemini title map first; keywords / hour band only if still untagged."""
    hints = routine_by_summary or {}
    for title in titles:
        hit = normalize_routine(hints.get(_norm_title(title), ""))
        if hit:
            return hit
    exact = [t.strip().lower() for t in titles if (t or "").strip().lower() in _KNOWN]
    if exact:
        return exact[0]
    text = _blob(titles)
    for kind, words in _TITLE_ROUTINES:
        if any(word in text for word in words):
            return kind
    role = (care_role_id or "").strip().lower()
    if role == CareRoleId.CHILD_CARE.value:
        if start_minute < 10 * 60:
            return ROUTINE_SCHOOL
        if 14 * 60 <= start_minute < 17 * 60:
            return ROUTINE_PICKUP
        return ROUTINE_ACTIVITY
    if role == CareRoleId.ELDER_CARE.value:
        return ROUTINE_CLINIC
    return ROUTINE_USUAL


def routine_word(kind: str) -> str:
    return kind if kind in _KNOWN else ROUTINE_USUAL


def classify_usual(
    usual: UsualWindow,
    person: CarePerson,
    *,
    routine_by_summary: dict[str, str] | None = None,
) -> str:
    titles = [usual.label, *usual.evidence_titles]
    return classify_routine(
        titles=titles,
        start_minute=usual.start_minute,
        care_role_id=person.care_role_id,
        routine_by_summary=routine_by_summary,
    )


def format_clock(start_minute: int) -> str:
    hour, minute = divmod(max(0, min(int(start_minute), 24 * 60 - 1)), 60)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def _clock_bits(minute_of_day: int) -> tuple[str, str]:
    hour, minute = divmod(max(0, min(int(minute_of_day), 24 * 60 - 1)), 60)
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    face = f"{hour12}" if minute == 0 else f"{hour12}:{minute:02d}"
    return face, suffix


def format_clock_range(start_minute: int, end_minute: int | None = None) -> str:
    """Compact range like ``1–1:30pm``; start only when end is missing or equal."""
    start_face, start_suffix = _clock_bits(start_minute)
    if end_minute is None or int(end_minute) <= int(start_minute):
        return f"{start_face}{start_suffix}"
    end_face, end_suffix = _clock_bits(end_minute)
    if start_suffix == end_suffix:
        return f"{start_face}–{end_face}{end_suffix}"
    return f"{start_face}{start_suffix}–{end_face}{end_suffix}"


def calendar_tz_label(tz: ZoneInfo | None = None, *, when: datetime | None = None) -> str:
    """Abbreviation for the calendar zone usuals were inferred in (PT, not UTC)."""
    zone = tz or CALENDAR_TZ
    stamp = when or datetime.now(tz=zone)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=zone)
    else:
        stamp = stamp.astimezone(zone)
    raw = (stamp.tzname() or "").strip()
    if raw in {"PDT", "PST", "Pacific Daylight Time", "Pacific Standard Time"}:
        return "PT"
    if raw in {"EDT", "EST", "Eastern Daylight Time", "Eastern Standard Time"}:
        return "ET"
    if raw in {"CDT", "CST", "Central Daylight Time", "Central Standard Time"}:
        return "CT"
    if raw in {"MDT", "MST", "Mountain Daylight Time", "Mountain Standard Time"}:
        return "MT"
    return raw or "PT"


def format_usual_when(on_date: date, start_minute: int) -> str:
    return f"{on_date.strftime('%A, %B')} {on_date.day} at {format_clock(start_minute)}"


def format_usual_slot(
    weekday: int,
    start_minute: int,
    end_minute: int | None = None,
    *,
    tz_label: str | None = None,
) -> str:
    day = _WEEKDAYS[max(0, min(int(weekday), 6))]
    zone = tz_label if tz_label is not None else calendar_tz_label()
    clock = format_clock_range(start_minute, end_minute)
    if zone:
        return f"{day}s {clock} {zone}"
    return f"{day}s {clock}"


def usual_event_title(
    person: CarePerson,
    usual: UsualWindow,
    *,
    routine_by_summary: dict[str, str] | None = None,
) -> str:
    """Short calendar title — name + routine, not the original event wording."""
    who = (person.display_name or "").strip()
    kind = classify_usual(usual, person, routine_by_summary=routine_by_summary)
    if who:
        return f"{who} {routine_word(kind)}"[:120]
    return routine_word(kind)


__all__ = [
    "CALENDAR_TZ",
    "ROUTINE_ACTIVITY",
    "ROUTINE_CLINIC",
    "ROUTINE_PICKUP",
    "ROUTINE_SCHOOL",
    "ROUTINE_USUAL",
    "calendar_tz_label",
    "classify_routine",
    "classify_usual",
    "format_clock",
    "format_clock_range",
    "normalize_routine",
    "format_usual_slot",
    "format_usual_when",
    "routine_word",
    "usual_event_title",
]
