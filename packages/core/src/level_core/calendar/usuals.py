"""Deterministic usual arithmetic.

Keep'd people are the lock. This module hangs repeating calendar rows on those
people (weekday + hour band) and asks whether an instance exists that day.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from level_core.calendar.routines import (
    classify_routine,
    classify_usual,
    format_usual_when,
    normalize_routine,
    routine_word,
)
from level_core.schemas.base import _now_utc
from level_core.schemas.care import (
    CarePerson,
    CareProfile,
    UsualWindow,
    held_care_people,
    locked_usuals,
)
from level_core.schemas.profile import BulletStatus

DEFAULT_TZ = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class SeriesUsual:
    """A repeating slot derived from Keep'd people + the dated agenda."""

    person_id: str
    label: str
    weekday: int
    start_minute: int
    end_minute: int
    evidence_titles: tuple[str, ...]
    hit_count: int
    last_seen_on: str | None


@dataclass(frozen=True, slots=True)
class UsualGap:
    """One Keep'd usual with no matching calendar instance on ``on_date``."""

    usual_id: str
    person_id: str
    display_name: str
    your_role: str
    their_relation: str
    label: str
    care_role_id: str
    on_date: date
    weekday: int
    start_minute: int
    end_minute: int
    routine_by_summary: dict[str, str] | None = None

    def banner(self) -> str:
        who = self.display_name.strip() or "this person"
        kind = classify_routine(
            titles=[self.label],
            start_minute=self.start_minute,
            care_role_id=self.care_role_id,
            routine_by_summary=self.routine_by_summary,
        )
        when = format_usual_when(self.on_date, self.start_minute)
        return f"Missing {who} {routine_word(kind)} on {when}."


def parse_event_start(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def agenda_fingerprint(
    events: list[dict[str, str | None]],
    *,
    tz: ZoneInfo = DEFAULT_TZ,
) -> str:
    """Stable hash of timed agenda rows. A delete/add changes this; order does not."""
    rows: list[str] = []
    for event in events:
        if (event.get("status") or "confirmed").lower() == "cancelled":
            continue
        title = _norm_title(event.get("summary") or "")
        start = parse_event_start(event.get("start"))
        if not title or start is None:
            continue
        local = start.astimezone(tz)
        rows.append(f"{local.date().isoformat()}|{local.strftime('%H%M')}|{title}")
    if not rows:
        return ""
    rows.sort()
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    return digest[:32]


def usuals_infer_needed(
    *,
    stored_fingerprint: str | None,
    events: list[dict[str, str | None]],
    tz: ZoneInfo = DEFAULT_TZ,
) -> bool:
    """True when the dated agenda no longer matches the last usuals infer."""
    live = agenda_fingerprint(events, tz=tz)
    if not live:
        return False
    return (stored_fingerprint or "") != live


def dated_event_evidence_lines(
    events: list[dict[str, str | None]],
    *,
    limit: int = 80,
) -> list[str]:
    """Dated title lines for Gemini usuals — series first, then the rest.

    Ranking which lines to show is not inventing a usual.
    """
    parsed: list[tuple[datetime, str, str]] = []
    for event in events:
        title = (event.get("summary") or "").strip()
        start_raw = (event.get("start") or "").strip()
        if not title or title == "(no title)" or not start_raw:
            continue
        start = parse_event_start(start_raw) or datetime.min.replace(tzinfo=timezone.utc)
        parsed.append((start, start_raw[:16], title[:120]))
    parsed.sort(key=lambda row: row[0])
    counts = Counter(_norm_title(title) for _start, _raw, title in parsed)
    series = [row for row in parsed if counts[_norm_title(row[2])] >= 2]
    rest = [row for row in parsed if counts[_norm_title(row[2])] < 2]
    return [f"{raw} {title}" for _start, raw, title in (series + rest)[: max(1, limit)]]


def horizon_dates(*, start: date, days: int = 7) -> list[date]:
    days = max(1, min(int(days), 21))
    return [start + timedelta(days=i) for i in range(days)]


def _norm_title(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _person_keys(person: CarePerson) -> set[str]:
    keys = {_norm_title(person.display_name), person.person_id.lower()}
    keys.update(_norm_title(a) for a in person.aliases if a)
    keys.discard("")
    return keys


def keepd_held_people(care: CareProfile | None) -> list[CarePerson]:
    """Keep'd dependents — the only people whose calendar may become usuals."""
    return [
        person
        for person in held_care_people(care)
        if person.status in {BulletStatus.ACCEPTED, BulletStatus.EDITED}
    ]


def resolve_event_people(
    event: dict[str, str | None],
    *,
    care: CareProfile,
) -> list[CarePerson]:
    """Hang an event on Keep'd people: tag, name on the title, or role hint."""
    people = keepd_held_people(care)
    if not people:
        return []
    tagged = event_person_id(event, care=care)
    if tagged:
        hit = [
            person
            for person in people
            if tagged.lower() in _person_keys(person) or tagged == person.person_id
        ]
        if hit:
            return hit
    title = _norm_title(event.get("summary") or "")
    if not title:
        return []
    named: list[CarePerson] = []
    for person in people:
        if any(len(key) >= 3 and key in title for key in _person_keys(person)):
            named.append(person)
    if named:
        return named
    role = (care.calendar_role_by_summary or {}).get(title)
    if not role:
        return []
    want = str(role).strip().lower()
    return [person for person in people if (person.care_role_id or "").strip().lower() == want]


def series_usuals_from_agenda(
    *,
    care: CareProfile | None,
    events: list[dict[str, str | None]],
    tz: ZoneInfo = DEFAULT_TZ,
) -> list[SeriesUsual]:
    """Repeating weekday+hour slots for Keep'd people. No new people invented."""
    if care is None or not events:
        return []
    buckets: dict[tuple[str, int, int], list[tuple[date, int, str, str]]] = {}
    for event in events:
        if (event.get("status") or "confirmed").lower() == "cancelled":
            continue
        start = parse_event_start(event.get("start"))
        if start is None:
            continue
        local = start.astimezone(tz)
        start_minute = local.hour * 60 + local.minute
        band = (start_minute // 60) * 60
        end = parse_event_start(event.get("end"))
        if end is not None:
            end_local = end.astimezone(tz)
            end_minute = end_local.hour * 60 + end_local.minute
        else:
            end_minute = band + 60
        title = (event.get("summary") or "").strip()[:120]
        recurring = (
            (event.get("recurring_event_id") or event.get("recurringEventId") or "")
            .strip()
        )
        for person in resolve_event_people(event, care=care):
            key = (person.person_id, local.weekday(), band)
            buckets.setdefault(key, []).append(
                (local.date(), end_minute, title, recurring)
            )
    out: list[SeriesUsual] = []
    for (person_id, weekday, band), hits in buckets.items():
        days = {row[0] for row in hits}
        recurring = any(row[3] for row in hits)
        if len(days) < 2 and not recurring:
            continue
        titles = [row[2] for row in hits if row[2]]
        if not titles:
            continue
        person = next(
            (p for p in keepd_held_people(care) if p.person_id == person_id),
            None,
        )
        hints = dict(care.calendar_routine_by_summary)
        votes = [
            hit
            for title in titles
            if (hit := normalize_routine(hints.get(_norm_title(title), "")))
        ]
        if votes:
            label = Counter(votes).most_common(1)[0][0]
        else:
            label = classify_routine(
                titles=titles,
                start_minute=band,
                care_role_id=person.care_role_id if person else "",
                routine_by_summary=hints,
            )
        end_minute = max(row[1] for row in hits)
        if end_minute <= band:
            end_minute = band + 60
        last = max(days)
        out.append(
            SeriesUsual(
                person_id=person_id,
                label=label,
                weekday=weekday,
                start_minute=band,
                end_minute=min(end_minute, 24 * 60),
                evidence_titles=tuple(dict.fromkeys(titles))[:8],
                hit_count=len(days),
                last_seen_on=last.isoformat(),
            )
        )
    return out


def event_person_id(
    event: dict[str, str | None],
    *,
    care: CareProfile,
) -> str | None:
    raw = (event.get("person_id") or "").strip()
    if raw:
        return raw
    title = _norm_title(event.get("summary") or "")
    if not title:
        return None
    return care.calendar_person_by_summary.get(title) or None


def event_belongs_to_person(
    event: dict[str, str | None],
    *,
    person: CarePerson,
    care: CareProfile,
) -> bool:
    tagged = event_person_id(event, care=care)
    if not tagged:
        return False
    return tagged.lower() in _person_keys(person)


def event_matches_usual(
    event: dict[str, str | None],
    *,
    usual: UsualWindow,
    person: CarePerson,
    care: CareProfile,
    on_date: date,
    tz: ZoneInfo = DEFAULT_TZ,
) -> bool:
    """True when this event is an instance of the locked usual on ``on_date``."""
    if (event.get("status") or "confirmed").lower() == "cancelled":
        return False
    if event.get("all_day") in {True, "true", "1"}:
        return False
    start = parse_event_start(event.get("start"))
    if start is None:
        return False
    local = start.astimezone(tz)
    if local.date() != on_date:
        return False
    if local.weekday() != usual.weekday:
        return False
    minute = local.hour * 60 + local.minute
    lo = min(usual.start_minute, usual.end_minute)
    hi = max(usual.start_minute, usual.end_minute)
    if hi <= lo:
        hi = lo + 60
    if minute < lo or minute >= hi:
        return False

    if event_belongs_to_person(event, person=person, care=care):
        return True
    if any(hit.person_id == person.person_id for hit in resolve_event_people(event, care=care)):
        return True
    title = _norm_title(event.get("summary") or "")
    if title and title in {_norm_title(t) for t in usual.evidence_titles}:
        return True
    return False


def find_usual_gaps(
    *,
    care: CareProfile | None,
    events: list[dict[str, str | None]],
    on_dates: list[date],
    tz: ZoneInfo = DEFAULT_TZ,
) -> list[UsualGap]:
    """Return missing Keep'd usuals. No model, no title regex invent."""
    if care is None or not on_dates:
        return []
    pairs = locked_usuals(care)
    if not pairs:
        return []
    exception_set = {
        (usual.usual_id, day)
        for _person, usual in pairs
        for day in usual.exceptions
    }
    gaps: list[UsualGap] = []
    for on_date in on_dates:
        weekday = on_date.weekday()
        for person, usual in pairs:
            if usual.weekday != weekday:
                continue
            if (usual.usual_id, on_date.isoformat()) in exception_set:
                continue
            present = any(
                event_matches_usual(
                    ev,
                    usual=usual,
                    person=person,
                    care=care,
                    on_date=on_date,
                    tz=tz,
                )
                for ev in events
            )
            if present:
                continue
            gaps.append(
                UsualGap(
                    usual_id=usual.usual_id,
                    person_id=person.person_id,
                    display_name=person.display_name,
                    your_role=person.your_role,
                    their_relation=person.their_relation,
                    label=routine_word(
                        classify_usual(
                            usual,
                            person,
                            routine_by_summary=care.calendar_routine_by_summary,
                        )
                    ),
                    care_role_id=person.care_role_id,
                    on_date=on_date,
                    routine_by_summary=dict(care.calendar_routine_by_summary) or None,
                    weekday=usual.weekday,
                    start_minute=usual.start_minute,
                    end_minute=usual.end_minute,
                )
            )
    return gaps


def usual_window_datetimes(
    usual: UsualWindow,
    on_date: date,
    *,
    tz: ZoneInfo = DEFAULT_TZ,
) -> tuple[datetime, datetime]:
    """Local start/end for putting a usual back on ``on_date``."""
    start = datetime(
        on_date.year,
        on_date.month,
        on_date.day,
        usual.start_minute // 60,
        usual.start_minute % 60,
        tzinfo=tz,
    )
    end_minute = usual.end_minute if usual.end_minute > usual.start_minute else usual.start_minute + 60
    end = datetime(
        on_date.year,
        on_date.month,
        on_date.day,
        min(end_minute // 60, 23),
        end_minute % 60 if end_minute < 24 * 60 else 59,
        tzinfo=tz,
    )
    if end <= start:
        end = start + timedelta(hours=1)
    return start, end


def gap_decision_key(usual_id: str, on_date: date) -> str:
    return f"usual_gap:{usual_id}:{on_date.isoformat()}"


def apply_usual_resolution(
    care: CareProfile,
    *,
    usual_id: str,
    action: str,
    on_date: date | None = None,
) -> CareProfile:
    """Mutate a usual: keep, exception, or reject. Calendar writes happen elsewhere."""
    action = (action or "").strip().lower().replace("-", "_")
    people: list[CarePerson] = []
    changed = False
    for person in care.people_profiles:
        usuals: list[UsualWindow] = []
        for usual in person.usuals:
            if usual.usual_id != usual_id:
                usuals.append(usual)
                continue
            changed = True
            if action in {"exception", "this_week", "this_week_is_different"}:
                day = (on_date or date.today()).isoformat()
                extra = list(usual.exceptions)
                if day not in extra:
                    extra.append(day)
                usuals.append(
                    usual.model_copy(update={"exceptions": extra[-24:]})
                )
            elif action in {"reject", "not_me", "not me"}:
                usuals.append(usual.model_copy(update={"status": BulletStatus.REJECTED}))
            elif action in {"keep", "put_back", "put_it_back"}:
                usuals.append(usual.model_copy(update={"status": BulletStatus.ACCEPTED}))
            else:
                usuals.append(usual)
        people.append(person.model_copy(update={"usuals": usuals}))
    if not changed:
        return care
    return care.model_copy(
        update={
            "people_profiles": people,
            "version": int(care.version or 1) + 1,
            "updated_at": _now_utc(),
        }
    )


__all__ = [
    "DEFAULT_TZ",
    "SeriesUsual",
    "UsualGap",
    "agenda_fingerprint",
    "apply_usual_resolution",
    "dated_event_evidence_lines",
    "event_belongs_to_person",
    "event_matches_usual",
    "event_person_id",
    "find_usual_gaps",
    "gap_decision_key",
    "horizon_dates",
    "keepd_held_people",
    "parse_event_start",
    "resolve_event_people",
    "series_usuals_from_agenda",
    "usual_window_datetimes",
    "usuals_infer_needed",
]
