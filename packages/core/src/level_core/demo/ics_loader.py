"""ICS -> CachedEvent loader for demo mode.

Handles the three ICS features the ``example-data/`` fixtures use:

- RRULE (weekly recurrence with UNTIL) - expanded via ``dateutil.rrule``.
- EXDATE (per-occurrence exclusion, e.g. Labor Day) - subtracted from
  the expansion.
- VTIMEZONE with named TZID - honored by ``icalendar``'s parser; we
  keep the offsets as tzinfo on the emitted datetimes.

Two design decisions worth calling out:

1. **Whole-week date shift.** The ICS files are engineered around
   ``anchor_date = 2026-08-26`` (the hackathon demo week). If a judge
   runs the demo months later, we shift every occurrence by whole
   weeks so today lands inside the "demo week" with the missing
   usuals intact. Whole weeks preserve weekday alignment (Nova ballet
   stays on Thursday).

2. **Pre-populated ``matched_person_ids`` + ``activity_type``.** The
   real pipeline runs an LLM enrich pass to classify events and
   attach people. Demo mode skips that so the app is usable with no
   ``GOOGLE_API_KEY`` configured. We do a cheap substring/alias scan
   against the seeded people and use ``heuristic_activity`` for the
   activity type. It's not as good as Gemini but it's free, fast, and
   accurate enough for the curated demo calendar.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar

from level_core.calendar.enrich import heuristic_activity
from level_core.schemas.activity import ActivityType
from level_core.schemas.agenda import CachedEvent, EventTime
from level_core.schemas.care import CarePerson

_LOGGER = logging.getLogger(__name__)

# Cap how far past anchor_date we'll expand recurring events. Matches
# the default calendar sync window (LEVEL_CAL_DAYS_BACK/FORWARD) so
# demo agenda footprint mirrors real usage without blowing up storage.
_DEFAULT_DAYS_BACK = 14
_DEFAULT_DAYS_FORWARD = 28


# Process-wide cache of parsed ICS Calendar objects, keyed by
# (absolute path, file mtime). The ICS files ship inside the wheel and
# never change during a process lifetime, so parsing them on every
# demo login was pure waste (~100-300ms per file for the fixtures we
# ship). This gates it to one parse per file per process; the mtime
# key also invalidates the cache if a contributor regenerates the
# fixtures in place.
_calendar_cache: dict[tuple[str, float], Any] = {}


def _load_calendar(ics_path: Path) -> Any:
    """Return a parsed ``icalendar.Calendar`` for ``ics_path``, cached."""
    key = (str(ics_path.resolve()), ics_path.stat().st_mtime)
    cached = _calendar_cache.get(key)
    if cached is not None:
        return cached
    with ics_path.open("rb") as fh:
        cal = Calendar.from_ical(fh.read())
    _calendar_cache[key] = cal
    return cal


@dataclass(frozen=True)
class ParsedOccurrence:
    """One materialized event occurrence, pre-CachedEvent."""

    uid: str
    summary: str
    location: str | None
    start: datetime
    end: datetime


def load_events(
    ics_path: Path,
    *,
    people: list[CarePerson],
    tz: str,
    anchor_date: date,
    now: datetime | None = None,
    days_back: int = _DEFAULT_DAYS_BACK,
    days_forward: int = _DEFAULT_DAYS_FORWARD,
) -> list[CachedEvent]:
    """Parse an ICS fixture and return CachedEvent rows.

    Occurrences are shifted so ``anchor_date`` (the fixture's original
    demo Wednesday) lines up with the caller's real "today" week -
    see the module docstring for the whole-week rationale.

    ``people`` is used ONLY to prefill ``matched_person_ids`` via
    substring scan. It is never persisted here.
    """
    if not ics_path.exists():
        raise FileNotFoundError(f"demo ICS not found: {ics_path}")

    tzinfo = ZoneInfo(tz)
    reference = now or datetime.now(tzinfo)
    shift_days = _compute_shift_days(anchor_date, reference.date())
    window_start = reference - timedelta(days=days_back)
    window_end = reference + timedelta(days=days_forward)

    cal = _load_calendar(ics_path)

    occurrences: list[ParsedOccurrence] = []
    for vevent in cal.walk("VEVENT"):
        occurrences.extend(
            _expand_vevent(
                vevent,
                shift=timedelta(days=shift_days),
                tzinfo=tzinfo,
                window_start=window_start,
                window_end=window_end,
            )
        )

    return [
        _build_cached_event(occ, calendar_id=ics_path.stem, people=people)
        for occ in occurrences
    ]


def _compute_shift_days(anchor: date, today: date) -> int:
    """Whole-week shift that aligns ``anchor``'s ISO week with today's.

    We compute the delta between the two ISO Mondays rather than
    rounding ``(today - anchor) / 7``. The old rounding formula
    silently mis-aligned any date that was 4-6 days after the anchor
    inside the same ISO week (e.g. anchor Wed 8/26 + today Sun 8/30
    is only 4 days apart but rounds up to a 7-day shift, pushing the
    anchor week's missing usuals INTO NEXT WEEK, so /today sees no
    nudge). Monday-to-Monday math is exact and preserves weekday
    alignment for every calendar date.
    """
    today_monday = today - timedelta(days=today.weekday())
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    return (today_monday - anchor_monday).days


def _expand_vevent(
    vevent,  # type: ignore[no-untyped-def]  # icalendar.cal.Event
    *,
    shift: timedelta,
    tzinfo: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
) -> list[ParsedOccurrence]:
    uid = str(vevent.get("UID") or "").strip() or _fallback_uid(vevent)
    summary = str(vevent.get("SUMMARY") or "").strip()
    location_raw = vevent.get("LOCATION")
    location = str(location_raw).strip() if location_raw else None

    dtstart_prop = vevent.get("DTSTART")
    dtend_prop = vevent.get("DTEND")
    if dtstart_prop is None or dtend_prop is None:
        return []

    dtstart = _coerce_to_datetime(dtstart_prop.dt, tzinfo)
    dtend = _coerce_to_datetime(dtend_prop.dt, tzinfo)
    duration = dtend - dtstart

    rrule_prop = vevent.get("RRULE")
    exdates = _collect_exdates(vevent, tzinfo)

    if rrule_prop is None:
        shifted = dtstart + shift
        if not _in_window(shifted, window_start, window_end):
            return []
        return [
            ParsedOccurrence(
                uid=uid,
                summary=summary,
                location=location,
                start=shifted,
                end=shifted + duration,
            )
        ]

    # dateutil handles UNTIL/COUNT/BYDAY/BYMONTHDAY expansion.
    rrule_text = rrule_prop.to_ical().decode("utf-8")
    try:
        rule = rrulestr(rrule_text, dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        _LOGGER.warning("demo.ics.bad_rrule uid=%s err=%s", uid, exc)
        return []

    # Expand up to the shifted window's forward edge, translated back
    # into the ICS's original date space.
    expand_until = window_end - shift + timedelta(days=1)
    expand_from = window_start - shift - timedelta(days=1)

    out: list[ParsedOccurrence] = []
    for occ_start in rule.between(expand_from, expand_until, inc=True):
        if occ_start in exdates:
            continue
        shifted_start = occ_start + shift
        if not _in_window(shifted_start, window_start, window_end):
            continue
        out.append(
            ParsedOccurrence(
                uid=uid,
                summary=summary,
                location=location,
                start=shifted_start,
                end=shifted_start + duration,
            )
        )
    return out


def _in_window(when: datetime, start: datetime, end: datetime) -> bool:
    return start <= when <= end


def _coerce_to_datetime(value, tzinfo: ZoneInfo) -> datetime:  # type: ignore[no-untyped-def]
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tzinfo)
    # All-day DTSTART comes in as a date; treat as midnight in the cal TZ.
    return datetime.combine(value, datetime.min.time(), tzinfo=tzinfo)


def _collect_exdates(vevent, tzinfo: ZoneInfo) -> set[datetime]:  # type: ignore[no-untyped-def]
    raw = vevent.get("EXDATE")
    if raw is None:
        return set()
    entries = raw if isinstance(raw, list) else [raw]
    out: set[datetime] = set()
    for entry in entries:
        items = getattr(entry, "dts", None) or [entry]
        for item in items:
            dt = getattr(item, "dt", item)
            out.add(_coerce_to_datetime(dt, tzinfo))
    return out


def _fallback_uid(vevent) -> str:  # type: ignore[no-untyped-def]
    summary = str(vevent.get("SUMMARY") or "event").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", summary).strip("-")
    return f"demo-{slug}"


def _build_cached_event(
    occ: ParsedOccurrence,
    *,
    calendar_id: str,
    people: list[CarePerson],
) -> CachedEvent:
    matched_person_ids, tokens = _match_people(occ.summary, people)
    activity = heuristic_activity(occ.summary)
    if activity is None:
        # Second-pass demo-only classifier for messy titles the shared
        # production heuristic deliberately doesn't cover because they'd
        # false-positive on real calendars. Two rules, both requiring a
        # matched person as context:
        #
        #  - "PT" as a whole word next to a person => MEDICAL_THERAPY.
        #    ("Helen PT", "PT - Helen"). Bare "PT" on a real user's
        #    calendar could mean product team, so the person-adjacency
        #    guard is what keeps this from being a demo-flavored bug.
        #
        #  - Any orphan grocery-store brand ("TJ's", "Ralphs") could go
        #    here too, but the shared heuristic now covers the common
        #    ones - see enrich.py comment.
        activity = _demo_secondary_classifier(occ.summary, matched_person_ids)
    event_id = f"demo:{occ.uid}:{occ.start.strftime('%Y%m%dT%H%M')}"
    return CachedEvent(
        event_id=event_id,
        calendar_id=calendar_id,
        summary=occ.summary,
        time=EventTime(
            start=occ.start,
            end=occ.end,
            tz=str(occ.start.tzinfo) if occ.start.tzinfo else "UTC",
            all_day=False,
        ),
        location=occ.location,
        attendee_tokens=tokens,
        activity_type=activity,
        classified_at=datetime.now(UTC) if activity else None,
        matched_person_ids=matched_person_ids,
        origin="google",
    )


# Whole-word "PT" (case-insensitive). Matches "Helen PT", "PT - Helen",
# and "PT session", but not "MPT", "Sept", or bare "part-time".
_PT_TOKEN = re.compile(r"\bpt\b", re.IGNORECASE)


def _demo_secondary_classifier(
    summary: str, matched_person_ids: list[str]
) -> ActivityType | None:
    """Demo-only classifier for messy variants that don't belong in the
    shared production heuristic. Every rule here MUST require a matched
    person as context so we don't overreach.
    """
    if not summary or not matched_person_ids:
        return None
    if _PT_TOKEN.search(summary):
        return ActivityType.MEDICAL_THERAPY
    return None


_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _match_people(
    summary: str, people: list[CarePerson]
) -> tuple[list[str], list[str]]:
    """Cheap alias scan. Skips self so 'Josh + Nova pickup' doesn't
    tag self on every event - matches how the LLM enricher behaves.
    """
    if not summary:
        return [], []
    tokens = {t.lower() for t in _NAME_TOKEN.findall(summary)}
    matched: list[str] = []
    stable_tokens: list[str] = []
    for person in people:
        if person.is_self:
            continue
        names = {person.display_name.lower(), *(a.lower() for a in person.aliases or ())}
        if names & tokens:
            matched.append(person.person_id)
            stable_tokens.append(person.display_name.split()[0])
    return matched, stable_tokens
