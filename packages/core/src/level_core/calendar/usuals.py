"""Deterministic usual detection + missing-usual gap logic.

Groups cached events by (person_id, weekday, hour_band) over the observed
window. No LLM in this path - if two candidates tie, `UsualAgent` is called
by the caller (see api/routes/profile.py) using `pick_ties`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from level_core.calendar.person_match import resolve_person_id
from level_core.config import get_settings
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CarePerson,
    Category,
    HourBand,
    Usual,
    Weekday,
)
from level_core.schemas.activity import activity_category
from level_core.schemas.usual import hour_to_band


@dataclass(frozen=True)
class UsualCandidate:
    person_id: str
    weekday: Weekday
    hour_band: HourBand
    activity_type: ActivityType
    display_summary: str
    source_event_uids: tuple[str, ...]
    occurrences: int
    weeks_observed: int

    @property
    def confidence(self) -> float:
        if self.weeks_observed == 0:
            return 0.0
        return min(1.0, self.occurrences / max(3, self.weeks_observed))


def _resolve_person(event: CachedEvent, people: list[CarePerson]) -> str | None:
    return resolve_person_id(event, people)


def compute_usuals_from_events(
    events: Iterable[CachedEvent], people: list[CarePerson]
) -> list[UsualCandidate]:
    """Group past events into weekly-repeating "usuals".

    Only past events count as evidence: a scheduled-but-unlived future event
    is a plan, not a usual. Anchoring on lived history is what makes
    "missing usuals this week" trustworthy.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    now = datetime.now(tz)

    groups: dict[
        tuple[str, Weekday, HourBand, ActivityType], list[CachedEvent]
    ] = defaultdict(list)
    weeks_observed: set[int] = set()

    for event in events:
        if event.activity_type is None:
            continue
        local = event.time.start.astimezone(tz)
        if local >= now:
            continue
        person_id = _resolve_person(event, people)
        if not person_id:
            continue
        weeks_observed.add(local.isocalendar().week)
        key = (person_id, Weekday(local.weekday()), hour_to_band(local.hour), event.activity_type)
        groups[key].append(event)

    weeks = max(1, len(weeks_observed))
    candidates: list[UsualCandidate] = []
    for (person_id, weekday, band, activity), items in groups.items():
        if len(items) < 2:
            continue
        summary = _pick_display_summary(items)
        candidates.append(
            UsualCandidate(
                person_id=person_id,
                weekday=weekday,
                hour_band=band,
                activity_type=activity,
                display_summary=summary,
                source_event_uids=tuple(sorted({e.event_id for e in items})),
                occurrences=len(items),
                weeks_observed=weeks,
            )
        )
    return sorted(
        candidates,
        key=lambda c: (c.confidence, c.occurrences),
        reverse=True,
    )


def _pick_display_summary(items: list[CachedEvent]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for e in items:
        counts[e.summary] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


@dataclass
class MissingUsual:
    usual: Usual
    expected_hour_band: HourBand


def missing_usuals_today(
    *, usuals: list[Usual], todays_events: list[CachedEvent]
) -> list[MissingUsual]:
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    today = datetime.now(tz).date()
    today_wd = Weekday(today.weekday())

    covered: set[tuple[str, HourBand]] = set()
    for e in todays_events:
        local = e.time.start.astimezone(tz)
        if local.date() != today:
            continue
        band = hour_to_band(local.hour)
        for pid in e.matched_person_ids:
            covered.add((pid, band))

    out: list[MissingUsual] = []
    for u in usuals:
        if u.status != "kept" and u.status != "proposed":
            continue
        if u.weekday != today_wd:
            continue
        if (u.person_id, u.hour_band) in covered:
            continue
        out.append(MissingUsual(usual=u, expected_hour_band=u.hour_band))
    return out


@dataclass
class MissingCategoryGroup:
    """One row per (weekday, person, Category) that didn't happen this week.

    Coarser than `MissingUsual` on purpose: a Nova soccer game covers a Nova
    swim-lesson usual because both fall under Category.SPORTS, so users
    aren't bombarded with wording differences. The Category enum lives in
    schemas/activity.py and is a pure enum -> enum mapping over
    ActivityType (no keyword matching on event text).
    """

    weekday: Weekday
    person_id: str
    category: Category
    representative_usual_ids: tuple[str, ...]
    typical_hour_bands: tuple[HourBand, ...]


def missing_usuals_this_week(
    *, usuals: list[Usual], week_events: list[CachedEvent], as_of_date=None
) -> list[MissingCategoryGroup]:
    """Coarse missing-usuals view for the current Mon-Sun week.

    An event covers a usual if it matches (weekday, person, category) - so a
    soccer game and a swim lesson both count as "sports covered" for that
    person on that day. Only weekdays that have already passed (Mon..today)
    are returned; upcoming days are excluded on purpose.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    today = as_of_date or datetime.now(tz).date()
    today_wd = today.weekday()

    covered: set[tuple[Weekday, str, Category]] = set()
    for e in week_events:
        local = e.time.start.astimezone(tz)
        wd = Weekday(local.weekday())
        cat = activity_category(e.activity_type)
        for pid in e.matched_person_ids:
            covered.add((wd, pid, cat))

    buckets: dict[tuple[Weekday, str, Category], list[Usual]] = {}
    for u in usuals:
        if u.status not in ("kept", "proposed"):
            continue
        if int(u.weekday) > today_wd:
            continue
        cat = activity_category(u.activity_type)
        if cat == Category.OTHER:
            continue
        key = (u.weekday, u.person_id, cat)
        if key in covered:
            continue
        buckets.setdefault(key, []).append(u)

    out: list[MissingCategoryGroup] = []
    for (wd, pid, cat), group in buckets.items():
        out.append(
            MissingCategoryGroup(
                weekday=wd,
                person_id=pid,
                category=cat,
                representative_usual_ids=tuple(u.usual_id for u in group),
                typical_hour_bands=tuple(sorted({u.hour_band for u in group})),
            )
        )
    out.sort(key=lambda g: (int(g.weekday), g.category.value))
    return out


def rollup_for_role_agent(
    events: Iterable[CachedEvent], *, top_n: int = 40
) -> list[dict[str, str | int]]:
    """Compressed rollup used by RoleAgent - no full bodies leave the API."""
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    counts: dict[
        tuple[Weekday, HourBand, str], int
    ] = defaultdict(int)

    for event in events:
        local = event.time.start.astimezone(tz)
        summary_5 = " ".join(event.summary.split()[:5]).strip()
        if not summary_5:
            continue
        key = (Weekday(local.weekday()), hour_to_band(local.hour), summary_5)
        counts[key] += 1

    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return [
        {
            "weekday": int(k[0]),
            "hour_band": k[1].value,
            "summary_first_5_words": k[2],
            "count": v,
        }
        for k, v in ordered
    ]
