"""Deterministic free/busy + priority-weighted slot ranking.

Gemini is only used to synthesize the reasoning line shown to the user.
The slot generator and scoring are pure Python so the recommendation is
reproducible in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from level_core.schemas import (
    ActivityType,
    CachedEvent,
    Priority,
    Usual,
    UsualStatus,
)
from level_core.schemas.usual import hour_to_band
from level_core.tz import resolve_tz


@dataclass
class CandidateSlot:
    start: datetime
    end: datetime
    score: float
    conflicts: list[str] = field(default_factory=list)
    aligned_priorities: list[str] = field(default_factory=list)
    aligned_usuals: list[str] = field(default_factory=list)
    tz_name: str = ""

    @property
    def local_label(self) -> str:
        tz = resolve_tz(self.tz_name or None)
        return self.start.astimezone(tz).strftime("%a %b %d, %-I:%M %p")


def find_candidate_slots(
    *,
    events: list[CachedEvent],
    window_days: int,
    duration_minutes: int,
    starts_at: datetime | None = None,
    step_minutes: int = 30,
    day_start_hour: int = 8,
    day_end_hour: int = 21,
    tz: ZoneInfo | None = None,
) -> list[CandidateSlot]:
    starts_at = starts_at or datetime.now(UTC)
    tz = tz or resolve_tz()

    busy = sorted(
        [
            (e.time.start.astimezone(UTC), e.time.end.astimezone(UTC))
            for e in events
        ]
    )

    slots: list[CandidateSlot] = []
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)

    cursor_day = starts_at.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    for d in range(window_days):
        day = cursor_day + timedelta(days=d)
        day_start = day.replace(hour=day_start_hour, minute=0)
        day_end = day.replace(hour=day_end_hour, minute=0)
        slot_start = max(day_start, starts_at.astimezone(tz))
        while slot_start + duration <= day_end:
            slot_end = slot_start + duration
            utc_start = slot_start.astimezone(UTC)
            utc_end = slot_end.astimezone(UTC)
            conflicts = _overlap_ids(busy, utc_start, utc_end, events)
            slots.append(
                CandidateSlot(
                    start=utc_start,
                    end=utc_end,
                    score=0.0,
                    conflicts=conflicts,
                    tz_name=tz.key,
                )
            )
            slot_start += step
    return slots


def _overlap_ids(
    busy: list[tuple[datetime, datetime]],
    slot_start: datetime,
    slot_end: datetime,
    events: list[CachedEvent],
) -> list[str]:
    ids: list[str] = []
    for e in events:
        s = e.time.start.astimezone(UTC)
        t = e.time.end.astimezone(UTC)
        if s < slot_end and t > slot_start:
            ids.append(e.event_id)
    return ids


def score_slots(
    slots: list[CandidateSlot],
    *,
    activity_type: ActivityType,
    priorities: list[Priority],
    usuals: list[Usual],
    events_by_id: dict[str, CachedEvent],
    tz: ZoneInfo | None = None,
) -> list[CandidateSlot]:
    tz = tz or resolve_tz()
    kept_usuals = [u for u in usuals if u.status == UsualStatus.KEPT]

    ranked: list[CandidateSlot] = []
    for slot in slots:
        base = 1.0
        if slot.conflicts:
            base -= 0.5 + 0.05 * len(slot.conflicts)
            for cid in slot.conflicts:
                ev = events_by_id.get(cid)
                if ev and ev.activity_type in {u.activity_type for u in kept_usuals}:
                    base -= 0.25

        local = slot.start.astimezone(tz)
        band = hour_to_band(local.hour)

        aligned_p: list[str] = []
        for p in priorities:
            if p.status != "kept":
                continue
            if activity_type in p.activity_types:
                base += 0.1 * (p.weight / 5.0)
                aligned_p.append(p.text)

        aligned_u: list[str] = []
        for u in kept_usuals:
            if u.activity_type == activity_type and u.weekday == local.weekday() and u.hour_band == band:
                base += 0.15
                aligned_u.append(u.display_summary)

        slot.score = round(max(-1.0, min(1.5, base)), 4)
        slot.aligned_priorities = aligned_p
        slot.aligned_usuals = aligned_u
        ranked.append(slot)
    return sorted(ranked, key=lambda s: s.score, reverse=True)


# =============================================================================
# Event-kind windows: dinner is an evening, lunch is midday. Overnight hours
# are never a candidate even if the calendar is empty then.
# =============================================================================


@dataclass(frozen=True)
class EventKind:
    """Plausible clock window for a kind of plan, plus a preferred start."""

    label: str
    duration_minutes: int
    # Inclusive start hour, exclusive end hour, local. Slots must finish
    # before day_end_hour. Multiple windows cover split preferences
    # (coffee morning *or* mid-afternoon) without admitting 3am.
    windows: tuple[tuple[int, int], ...]
    ideal_hour: float
    activity_type: ActivityType
    weekdays_only: bool = False


# Floor / ceiling for any inferred window so a bad ideal never opens overnight.
_KIND_HOUR_FLOOR = 7
_KIND_HOUR_CEILING = 21

_DEFAULT_KIND = EventKind(
    label="that",
    duration_minutes=60,
    windows=((8, 20),),  # viable waking hours; never overnight
    ideal_hour=12.0,
    activity_type=ActivityType.PERSONAL,
)

# Clock window from words the user said about WHEN, not what the event is.
# First match wins — more specific (evening, after school) before generic.
_TOD_PATTERNS: tuple[tuple[re.Pattern[str], tuple[tuple[int, int], ...], float, bool], ...] = (
    (re.compile(r"\bafter\s+school\b", re.I), ((15, 19),), 16.5, False),
    (re.compile(r"\b(?:evening|tonight|after\s+work|after\s+hours)\b", re.I), ((17, 21),), 18.5, False),
    (re.compile(r"\bafternoon\b", re.I), ((12, 17),), 14.0, False),
    (re.compile(r"\bmorning\b", re.I), ((8, 12),), 9.5, False),
    (
        re.compile(r"\b(?:weekdays?|during\s+the\s+week|work\s+hours?)\b", re.I),
        ((9, 17),),
        10.5,
        True,
    ),
)


# Meal + drink clocks. These take precedence over time-of-day words
# because the meal name is stronger evidence than "this afternoon"
# ("lunch this afternoon" still means lunch, not 2pm). Duration is
# per-meal — dinner is longer than coffee. Tuple layout mirrors
# _TOD_PATTERNS with one extra column for duration_minutes:
#   (pattern, windows, ideal_hour, duration_minutes)
#
# Windows are conservative on both ends: dinner starts at 5pm, not
# 4:30; breakfast ends at 10, not 11 — because the user is asking
# when to BOOK it, which needs a socially normal time even if their
# calendar is empty at 3pm.
_MEAL_PATTERNS: tuple[tuple[re.Pattern[str], tuple[tuple[int, int], ...], float, int], ...] = (
    (re.compile(r"\bbreakfast\b", re.I), ((7, 10),), 8.0, 45),
    (re.compile(r"\bbrunch\b", re.I), ((10, 13),), 11.0, 75),
    (re.compile(r"\blunch(?:es|time)?\b", re.I), ((11, 14),), 12.5, 60),
    (re.compile(r"\b(?:coffee|espresso|latte)\b", re.I), ((8, 14),), 10.5, 30),
    (re.compile(r"\b(?:afternoon\s+tea|tea\s+time)\b", re.I), ((14, 17),), 15.5, 45),
    (re.compile(r"\bsnack\b", re.I), ((14, 17),), 15.5, 20),
    (re.compile(r"\bdinner\b", re.I), ((17, 21),), 18.5, 90),
    (re.compile(r"\b(?:happy\s+hour|drinks?|cocktails?)\b", re.I), ((16, 19),), 17.5, 60),
    (re.compile(r"\bnightcap\b", re.I), ((20, 22),), 20.5, 45),
)

_DURATION_MIN_RE = re.compile(r"\b(\d{1,3})\s*(?:minutes?|mins?)\b", re.I)
_DURATION_HR_RE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:hours?|hrs?)\b", re.I)
_AN_HOUR_RE = re.compile(r"\b(?:an|one)\s+hour\b", re.I)

_PLAN_LABEL_RE = re.compile(
    r"\b(?:for|to\s+(?:book|schedule|add|fit(?:\s+in)?))\s+"
    r"(?:a |an |the |my |our |some )?"
    r"(?P<label>.+?)"
    r"(?=\s+(?:this|next|today|tomorrow|tonight|on\b|at\b|in\s+the|in\s+a|"
    r"weekday|weekend|morning|afternoon|evening)\b|[?!.]|$)",
    re.I,
)
# "find lunch this week" / "when can I grab coffee tomorrow"
_PLAN_LABEL_FIND_RE = re.compile(
    r"\b(?:find|look(?:ing)?\s+for|search(?:ing)?\s+for)\s+"
    r"(?:me\s+|us\s+|a\s+|an\s+|the\s+)?"
    r"(?P<label>.+?)"
    r"(?=\s+(?:this|next|today|tomorrow|tonight|on\b|at\b|"
    r"weekday|weekend|morning|afternoon|evening)\b|[?!.]|$)",
    re.I,
)
_PLAN_LABEL_GET_RE = re.compile(
    r"\b(?:get|grab|have|do|squeeze(?:\s+in)?|book|schedule|add)\s+"
    r"(?:a |an |the |my |our |some )?"
    r"(?P<label>.+?)"
    r"(?=\s+(?:this|next|today|tomorrow|tonight|on\b|"
    r"weekday|weekend)\b|[?!.]|$)",
    re.I,
)
_BAD_PLAN_LABELS = frozenset(
    {
        "time", "a time", "the time", "slot", "a slot", "me", "it", "something",
        "best time", "a best time", "good time", "a good time", "open time",
        "best times", "good times", "a slot", "an opening",
    }
)


def plan_label_from_message(message: str) -> str:
    """The thing they want a slot for, in their words. Empty if we can't tell."""
    for pattern in (_PLAN_LABEL_RE, _PLAN_LABEL_FIND_RE, _PLAN_LABEL_GET_RE):
        m = pattern.search(message)
        if not m:
            continue
        label = re.sub(r"\s+", " ", m.group("label")).strip(" .,!?")
        label = re.sub(
            r"^(?:a |an |the )?(?:best |good |open )?(?:time|slot|opening|window)s?\s+(?:for\s+)?",
            "",
            label,
            flags=re.I,
        ).strip()
        if len(label) >= 2 and label.lower() not in _BAD_PLAN_LABELS:
            if len(label) > 48:
                label = label[:45].rstrip() + "\u2026"
            return label
    return ""


def calendar_title_from_label(label: str) -> str:
    """Short calendar title: 'lunch with a friend' → 'Lunch'."""
    raw = (label or "").strip()
    if not raw or raw.lower() in {"that", "time"}:
        return "Time block"
    head = re.split(r"\s+(?:with|at|and)\s+", raw, maxsplit=1, flags=re.I)[0].strip()
    if not head:
        head = raw
    return head[0].upper() + head[1:]


def infer_event_kind_from_patterns(message: str) -> EventKind | None:
    """Regex-only inference. Returns None when neither a meal name nor a
    time-of-day word is present, so an async caller can decide whether
    to escalate to the LLM window agent for uncommon labels
    ("afternoon tea", "power lunch", "little Theo's nap"). The
    synchronous ``infer_event_kind`` wraps this with a default so
    non-async callers still get a usable EventKind.

    Precedence: MEAL_PATTERNS before TOD_PATTERNS. Meal names are
    stronger evidence than time-of-day words - "lunch this afternoon"
    should still narrow to lunch's 11-14 window, not the full 12-17
    afternoon window.
    """
    label = plan_label_from_message(message) or _DEFAULT_KIND.label

    for pattern, windows, ideal, duration_minutes in _MEAL_PATTERNS:
        if pattern.search(message):
            return EventKind(
                label=label,
                duration_minutes=duration_minutes,
                windows=windows,
                ideal_hour=ideal,
                activity_type=ActivityType.PERSONAL,
                weekdays_only=False,
            )

    for pattern, windows, ideal, weekdays_only in _TOD_PATTERNS:
        if pattern.search(message):
            return EventKind(
                label=label,
                duration_minutes=_DEFAULT_KIND.duration_minutes,
                windows=windows,
                ideal_hour=ideal,
                activity_type=ActivityType.PERSONAL,
                weekdays_only=weekdays_only,
            )
    return None


def infer_event_kind(message: str) -> EventKind:
    """Regex-only, synchronous. Falls back to a "waking hours" window
    when nothing matched. Used everywhere except the chat fast-path,
    which uses the async wrapper (see ``infer_event_kind_async``).
    """
    hit = infer_event_kind_from_patterns(message)
    if hit is not None:
        return hit
    label = plan_label_from_message(message) or _DEFAULT_KIND.label
    return EventKind(
        label=label,
        duration_minutes=_DEFAULT_KIND.duration_minutes,
        windows=_DEFAULT_KIND.windows,
        ideal_hour=_DEFAULT_KIND.ideal_hour,
        activity_type=ActivityType.PERSONAL,
        weekdays_only=False,
    )


async def infer_event_kind_async(
    store,  # type: ignore[no-untyped-def]  # avoid circular import on UserStore
    message: str,
) -> EventKind:
    """Regex-first, LLM-fallback event-kind inference.

    Fast path (~50µs, no network): if the message contains a known
    meal name or time-of-day word, return the deterministic
    EventKind immediately. This covers "lunch", "coffee", "in the
    evening", "after school", etc.

    Slow path (~500-1500ms): only when the fast path missed AND we
    extracted a real plan label ("afternoon tea", "power lunch",
    "playdate for Nova", "book club"). We ask SlotWindowAgent for
    a socially-normal window instead of shipping the 8am-8pm default,
    which was surfacing dinner-time slots for lunch requests.

    Failure mode: if the LLM path errors (unavailable, safety,
    quota, schema mismatch), ``call_agent`` returns
    ``soft_degraded=True`` with ``value=None`` and we transparently
    fall back to the sync default. The user never sees a 500 - at
    worst they get the "waking hours" window that used to fire
    unconditionally.
    """
    hit = infer_event_kind_from_patterns(message)
    if hit is not None:
        return hit

    label = plan_label_from_message(message)
    if not label:
        # No label + no time-of-day word means we've got nothing for
        # the LLM to work with either. Return the default and let the
        # ranker deal with it.
        return EventKind(
            label=_DEFAULT_KIND.label,
            duration_minutes=_DEFAULT_KIND.duration_minutes,
            windows=_DEFAULT_KIND.windows,
            ideal_hour=_DEFAULT_KIND.ideal_hour,
            activity_type=ActivityType.PERSONAL,
            weekdays_only=False,
        )

    # Import here to avoid making the whole slots module import the
    # agent registry (which pulls in Gemini SDK). Kept sync-side
    # patterns pure and free of LLM deps.
    from level_core.agents import slot_window as _slot_window

    result = await _slot_window.run(store=store, message=message, label=label)
    proposed = getattr(result.value, "window", None) if result.value else None
    if proposed is None:
        return EventKind(
            label=label,
            duration_minutes=_DEFAULT_KIND.duration_minutes,
            windows=_DEFAULT_KIND.windows,
            ideal_hour=_DEFAULT_KIND.ideal_hour,
            activity_type=ActivityType.PERSONAL,
            weekdays_only=False,
        )

    # Re-clamp to the module-wide floor/ceiling before returning so
    # a rogue LLM window can't sneak past ``recommend_slots``'
    # clamp either.
    start_h = max(_KIND_HOUR_FLOOR, min(proposed.start_hour, _KIND_HOUR_CEILING - 1))
    end_h = max(start_h + 1, min(proposed.end_hour, _KIND_HOUR_CEILING))
    ideal = float(max(start_h, min(proposed.ideal_hour, end_h)))
    return EventKind(
        label=label,
        duration_minutes=proposed.duration_minutes,
        windows=((start_h, end_h),),
        ideal_hour=ideal,
        activity_type=ActivityType.PERSONAL,
        weekdays_only=False,
    )


def parse_duration_minutes(message: str, default: int) -> int:
    if _AN_HOUR_RE.search(message):
        return 60
    m = _DURATION_MIN_RE.search(message)
    if m:
        return max(15, min(240, int(m.group(1))))
    m = _DURATION_HR_RE.search(message)
    if m:
        hours = float(m.group(1))
        return max(15, min(240, int(hours * 60)))
    return default


def recommend_slots(
    *,
    events: list[CachedEvent],
    kind: EventKind,
    starts_at: datetime,
    window_days: int,
    priorities: list[Priority],
    usuals: list[Usual],
    duration_minutes: int | None = None,
    limit: int = 4,
    tz: ZoneInfo | None = None,
) -> list[CandidateSlot]:
    """Free slots inside the kind's plausible hours, one best start per day.

    Overnight / 3am gaps are never generated: windows are clamped to
    daytime/evening. Busy (timed) events knock a slot out entirely;
    all-day events are ignored so a holiday label doesn't wipe dinner.
    """
    duration = duration_minutes or kind.duration_minutes
    timed = [e for e in events if not e.time.all_day]
    tz = tz or resolve_tz()
    seen: set[tuple[str, str]] = set()
    raw: list[CandidateSlot] = []
    for start_h, end_h in kind.windows:
        start_h = max(_KIND_HOUR_FLOOR, min(start_h, _KIND_HOUR_CEILING - 1))
        end_h = max(start_h + 1, min(end_h, _KIND_HOUR_CEILING))
        for slot in find_candidate_slots(
            events=timed,
            window_days=window_days,
            duration_minutes=duration,
            starts_at=starts_at,
            day_start_hour=start_h,
            day_end_hour=end_h,
            tz=tz,
        ):
            local = slot.start.astimezone(tz)
            if kind.weekdays_only and local.weekday() >= 5:
                continue
            key = (slot.start.isoformat(), slot.end.isoformat())
            if key in seen:
                continue
            seen.add(key)
            raw.append(slot)

    events_by_id = {e.event_id: e for e in timed}
    ranked = score_slots(
        raw,
        activity_type=kind.activity_type,
        priorities=priorities,
        usuals=usuals,
        events_by_id=events_by_id,
        tz=tz,
    )
    free = [s for s in ranked if not s.conflicts]
    if not free:
        return []

    half_width = max(1.0, 2.5)
    by_day: dict = {}
    for slot in free:
        day = slot.start.astimezone(tz).date()
        by_day[day] = by_day.get(day, 0) + 1

    for slot in free:
        local = slot.start.astimezone(tz)
        hour = local.hour + local.minute / 60.0
        dist = abs(hour - kind.ideal_hour)
        slot.score = round(
            slot.score
            + 0.45 * max(0.0, 1.0 - dist / half_width)
            + 0.03 * by_day[local.date()],
            4,
        )

    best_for_day: dict = {}
    for slot in sorted(free, key=lambda s: s.score, reverse=True):
        day = slot.start.astimezone(tz).date()
        if day not in best_for_day:
            best_for_day[day] = slot
    return sorted(best_for_day.values(), key=lambda s: s.score, reverse=True)[:limit]
