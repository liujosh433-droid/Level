"""Deterministic usual-gap arithmetic.

AI proposes and locks usuals (see ``profile.people_usuals``). This module only
asks: on this local date, does a matching instance exist?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from level_core.schemas.base import _now_utc
from level_core.schemas.care import (
    CarePerson,
    CareProfile,
    UsualWindow,
    locked_usuals,
)
from level_core.schemas.profile import BulletStatus

DEFAULT_TZ = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True, slots=True)
class UsualGap:
    """One Keep'd usual with no matching calendar instance on ``on_date``."""

    usual_id: str
    person_id: str
    display_name: str
    your_role: str
    their_relation: str
    label: str
    on_date: date
    weekday: int
    start_minute: int
    end_minute: int

    def banner(self) -> str:
        who = self.display_name.strip()
        line = f"{self.label} usually sits here"
        if who:
            line += f" for {who}"
        line += ". It isn't on the calendar this week."
        role = self.your_role.strip()
        if role:
            line += f" You're the {role}."
        return line


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
                    label=usual.label,
                    on_date=on_date,
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
    "UsualGap",
    "apply_usual_resolution",
    "event_belongs_to_person",
    "event_matches_usual",
    "event_person_id",
    "find_usual_gaps",
    "gap_decision_key",
    "horizon_dates",
    "parse_event_start",
    "usual_window_datetimes",
]
