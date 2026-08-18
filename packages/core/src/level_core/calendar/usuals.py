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

from level_core.config import get_settings
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CarePerson,
    HourBand,
    Usual,
    Weekday,
)
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
    if event.matched_person_ids:
        return event.matched_person_ids[0]
    lower = event.summary.lower()
    for p in people:
        if any(a.lower() in lower for a in [p.display_name, *p.aliases]):
            return p.person_id
    return None


def compute_usuals_from_events(
    events: Iterable[CachedEvent], people: list[CarePerson]
) -> list[UsualCandidate]:
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)

    groups: dict[
        tuple[str, Weekday, HourBand, ActivityType], list[CachedEvent]
    ] = defaultdict(list)
    weeks_observed: set[int] = set()

    for event in events:
        if event.activity_type is None:
            continue
        person_id = _resolve_person(event, people)
        if not person_id:
            continue
        local = event.time.start.astimezone(tz)
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
