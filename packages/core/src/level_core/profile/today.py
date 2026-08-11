"""Build today's schedule view + short, actionable reminders for *today*."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact


def _tip_text(text: str) -> str | None:
    """Normalize whitespace — do not ellipsis-truncate (UI can wrap)."""
    text = " ".join((text or "").strip().split())
    if not text:
        return None
    text = re.sub(r"^(remember:?|tip:?|note:?)\s*", "", text, flags=re.I)
    return text or None


def _format_reminder(text: str, *, event_summary: str | None = None) -> str | None:
    """Normalize a user/AI cue — do not invent Remember: copy or paste event titles."""
    del event_summary  # matching is structural elsewhere; wording stays the cue's own
    tip = _tip_text(text)
    if not tip:
        return None
    return tip[0].upper() + tip[1:] if tip[0].islower() else tip


def build_recommendations(
    *,
    today_events: list[dict[str, Any]],
    snapshot: ProfileSnapshot | None,
    facts: list[Fact],
) -> list[str]:
    """Day tips from event cues the user taught Level (check-in).

    Profile-note / fact tip invent is intentionally omitted — Gemini polish
    (optional) or structured cues own the wording. ``snapshot`` / ``facts``
    kept for API compatibility.
    """
    del snapshot, facts
    if not today_events:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for e in today_events:
        for cue in e.get("cues") or []:
            tip = _format_reminder(str(cue))
            if not tip:
                continue
            key = tip.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(tip)
            if len(out) >= 4:
                return out
    return out


def format_event_time(
    start_raw: str | None,
    *,
    end_raw: str | None = None,
    all_day: bool = False,
) -> str:
    """Human time label — prefer a start–end range when end is known."""
    if all_day or not start_raw:
        return "All day"

    def _parse(raw: str) -> datetime | None:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _clock(dt: datetime) -> str:
        return dt.strftime("%I:%M %p").lstrip("0")

    start = _parse(start_raw)
    if start is None:
        return start_raw

    end = _parse(end_raw) if end_raw else None
    if end is None or end <= start:
        return _clock(start)

    if start.strftime("%p") == end.strftime("%p"):
        start_bit = start.strftime("%I:%M").lstrip("0")
        return f"{start_bit} – {_clock(end)}"
    return f"{_clock(start)} – {_clock(end)}"


def build_tomorrow_preview(
    *,
    tomorrow_events: list[dict[str, Any]],
    weekday_label: str,
    cues_by_event: list[list[str]] | None = None,
    facts: list[Fact] | None = None,
    snapshot: ProfileSnapshot | None = None,
) -> tuple[str, list[str]]:
    """Compact count summary + remember tips from event cues only."""
    del facts, snapshot
    cues_by_event = cues_by_event or [[] for _ in tomorrow_events]
    n = len(tomorrow_events)

    if n == 0:
        return (f"Nothing on the calendar for {weekday_label} yet.", [])

    summary = f"{n} event{'s' if n != 1 else ''} tomorrow"
    remember: list[str] = []

    for cues in cues_by_event:
        for c in cues:
            tip = _format_reminder(c)
            if tip and tip not in remember:
                remember.append(tip)
            if len(remember) >= 3:
                break
        if len(remember) >= 3:
            break

    return summary, remember[:3]


__all__ = ["build_recommendations", "build_tomorrow_preview", "format_event_time"]
