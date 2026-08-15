"""Routine categories for Keep'd usuals — display and short calendar titles.

Does not invent people or slots. Classifies an already-detected usual from
titles the agenda already attached, with a child hour-band fallback.
"""

from __future__ import annotations

from datetime import date

from level_core.schemas.care import CarePerson, CareRoleId, UsualWindow

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


def format_usual_when(on_date: date, start_minute: int) -> str:
    return f"{on_date.strftime('%A, %B')} {on_date.day} at {format_clock(start_minute)}"


def format_usual_slot(weekday: int, start_minute: int) -> str:
    day = _WEEKDAYS[max(0, min(int(weekday), 6))]
    return f"{day}s at {format_clock(start_minute)}"


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
    "ROUTINE_ACTIVITY",
    "ROUTINE_CLINIC",
    "ROUTINE_PICKUP",
    "ROUTINE_SCHOOL",
    "ROUTINE_USUAL",
    "classify_routine",
    "classify_usual",
    "format_clock",
    "normalize_routine",
    "format_usual_slot",
    "format_usual_when",
    "routine_word",
    "usual_event_title",
]
