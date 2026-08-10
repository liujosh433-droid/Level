"""Build today's schedule view + short caregiver recommendations (no LLM)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from level_core.schemas.profile import BulletStatus, ProfileSnapshot
from level_core.schemas.signal import Fact, FactType


def _eveningish(start_raw: str | None) -> bool:
    if not start_raw or "T" not in start_raw:
        return False
    try:
        # 2026-08-15T18:30:00-07:00 or Z
        time_part = start_raw.split("T", 1)[1][:5]
        hour = int(time_part.split(":")[0])
        return hour >= 16
    except (ValueError, IndexError):
        return False


def build_recommendations(
    *,
    today_events: list[dict[str, Any]],
    snapshot: ProfileSnapshot | None,
    facts: list[Fact],
) -> list[str]:
    """Cheap, explainable nudges for the Today screen."""
    recs: list[str] = []
    n = len(today_events)
    evening_n = sum(1 for e in today_events if _eveningish(e.get("start")))

    if n == 0:
        recs.append("Your calendar looks open today — a good day for one thing that usually slips.")
    elif n >= 5:
        recs.append(
            f"You have {n} things on the calendar today. Protect one short buffer so the day doesn’t stack."
        )
    elif n >= 3:
        recs.append(f"{n} events today — keep transitions simple and leave travel time.")

    if evening_n >= 2:
        recs.append(
            "Two or more evening plans today. If nights usually run late, plan an earlier dinner or handoff."
        )

    titles = " ".join((e.get("summary") or "") for e in today_events).lower()
    if re.search(r"ultrasound|dentist|doctor|clinic|retainers?|therapy|appt", titles):
        recs.append("Health visit on the schedule — leave extra time for parking and forms.")

    constraints = [
        f
        for f in facts
        if f.type is FactType.CONSTRAINT and f.confidence >= 0.55
    ]
    constraints.sort(key=lambda f: f.salience, reverse=True)
    for c in constraints[:2]:
        if any(
            tok in c.statement.lower()
            for tok in ("evening", "night", "late", "solo", "pickup", "bedtime")
        ):
            if evening_n or n >= 2:
                recs.append(f"Remember: {c.statement.rstrip('.')}.")
                break

    if snapshot:
        for contra in snapshot.contradictions:
            if contra.status is BulletStatus.REJECTED:
                continue
            recs.append(f"Watch this tension today: {contra.summary}")
            break

    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for r in recs:
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
        if len(out) >= 3:
            break
    return out


def format_event_time(start_raw: str | None, *, all_day: bool = False) -> str:
    if all_day or not start_raw:
        return "All day"
    try:
        dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        label = dt.strftime("%I:%M %p")
        return label.lstrip("0")
    except ValueError:
        return start_raw


__all__ = ["build_recommendations", "format_event_time"]
