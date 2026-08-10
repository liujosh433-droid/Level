"""Build today's schedule view + short, actionable reminders for *today*."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from level_core.schemas.profile import BulletStatus, ProfileSnapshot
from level_core.schemas.signal import Fact, FactType

_STOP = frozenset(
    {
        "with",
        "from",
        "that",
        "this",
        "have",
        "your",
        "their",
        "about",
        "after",
        "before",
        "today",
        "tomorrow",
        "week",
        "time",
        "will",
        "need",
        "just",
        "into",
        "when",
        "then",
        "than",
        "them",
        "they",
        "been",
        "were",
        "also",
        "only",
        "very",
        "much",
        "more",
        "some",
        "like",
        "does",
        "done",
        "make",
        "take",
        "over",
        "under",
        "each",
        "other",
        "onto",
        # Generic calendar-analytics words — never treat as a match signal.
        "calendar",
        "events",
        "event",
        "window",
        "current",
        "shows",
        "show",
        "repeatedly",
        "frequent",
        "multiple",
        "related",
        "protected",
        "commitments",
        "commitment",
        "appointments",
        "appointment",
        "weeknights",
        "weeknight",
        "evenings",
        "evening",
        "mornings",
        "morning",
        "busy",
        "schedule",
        "scheduling",
    }
)

# Ingested calendar facts look like this — never show them as reminders.
_CAL_DUMP = re.compile(
    r"^(on my calendar|calendar:|drive doc:)",
    re.IGNORECASE,
)

# Profile bullets that describe calendar *patterns*, not actionable today tips.
_ANALYTICAL = re.compile(
    r"("
    r"show up repeatedly|"
    r"on my calendar|"
    r"my calendar shows|"
    r"in (this|the) (current )?window|"
    r"frequent (evening|morning|weekday|weekend)|"
    r"multiple (medical|health|school|work)|"
    r"events show up|"
    r"need protected time|"
    r"\b\d+\s+in (this|the)\b|"
    r"child-related events|"
    r"so weeknights|"
    r"tend to (have|be|run)"
    r")",
    re.IGNORECASE,
)

_ALREADY_REMINDER = re.compile(
    r"^(remember\b|don'?t forget\b|bring\b|pack\b|leave\b|grab\b|check\b|call\b|text\b)",
    re.IGNORECASE,
)


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z]{3,}", (text or "").lower())
        if t not in _STOP
    }


def _score_overlap(text: str, schedule_tokens: set[str]) -> int:
    toks = _tokens(text)
    if not toks or not schedule_tokens:
        return 0
    return len(toks & schedule_tokens)


def _best_event_match(
    text: str, events: list[dict[str, Any]]
) -> tuple[str | None, int]:
    """Return (event summary, overlap score) for the best-matching today event."""
    best_summary: str | None = None
    best_score = 0
    for e in events:
        summary = (e.get("summary") or "").strip()
        if not summary:
            continue
        score = _score_overlap(text, _tokens(summary))
        if score > best_score:
            best_score = score
            best_summary = summary
    return best_summary, best_score


def _active_bullets(snapshot: ProfileSnapshot | None) -> list:
    if not snapshot:
        return []
    return [
        b
        for b in snapshot.bullets
        if b.status is not BulletStatus.REJECTED and (b.text or "").strip()
    ]


def _is_calendar_dump(text: str) -> bool:
    """Reject raw ingest lines that read like calendar dumps, not tips."""
    t = (text or "").strip()
    if not t:
        return True
    if _CAL_DUMP.match(t):
        return True
    # Long dated event restatements ("… Aug 11 2026 …")
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b", t, re.I):
        if len(t) > 80:
            return True
    if t.count("—") + t.count(" - ") >= 2 and len(t) > 100:
        return True
    return False


def _is_analytical_insight(text: str) -> bool:
    """Reject 'your calendar pattern is…' bullets — not day-of reminders."""
    t = (text or "").strip()
    if not t:
        return True
    if _ANALYTICAL.search(t):
        return True
    # First-person calendar analytics without a concrete action.
    if re.search(r"\b(on my calendar|my calendar)\b", t, re.I) and not _ALREADY_REMINDER.search(t):
        return True
    return False


def _tip_text(text: str, *, limit: int = 96) -> str | None:
    """Normalize text for scannable UI — skip dumps / fluff."""
    text = " ".join((text or "").strip().split())
    if not text or _is_calendar_dump(text) or _is_analytical_insight(text):
        return None
    text = re.sub(r"^(remember:?|tip:?|note:?)\s*", "", text, flags=re.I)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def _format_reminder(text: str, *, event_summary: str | None = None, limit: int = 110) -> str | None:
    """Turn a cue/profile note into a short day-of reminder."""
    tip = _tip_text(text, limit=limit)
    if not tip:
        return None
    if _ALREADY_REMINDER.search(tip):
        body = tip[0].upper() + tip[1:] if tip else tip
        return body if len(body) <= limit else _tip_text(body, limit=limit)

    # Keep first person / concrete notes readable; prefix Remember.
    cleaned = tip.rstrip(".")
    # Soften "I forgot…" → actionable reminder.
    cleaned = re.sub(r"^i (often |always |sometimes )?forget( to)?\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^i (need|have) to\s+", "", cleaned, flags=re.I)
    if not cleaned:
        return None
    if cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]

    if event_summary and len(cleaned) < 70:
        # Light grounding when we know which event triggered the tip.
        ev = re.sub(r"\s+", " ", event_summary).strip()
        if ev and ev.lower() not in cleaned.lower() and len(ev) <= 36:
            reminder = f"Remember: {cleaned} ({ev})"
        else:
            reminder = f"Remember: {cleaned}"
    else:
        reminder = f"Remember: {cleaned}"

    if len(reminder) <= limit:
        return reminder
    return _tip_text(reminder, limit=limit)


def build_recommendations(
    *,
    today_events: list[dict[str, Any]],
    snapshot: ProfileSnapshot | None,
    facts: list[Fact],
) -> list[str]:
    """Actionable reminders for *today* — cues + profile notes that match today's events.

    Never surfaces calendar-pattern analytics ("frequent evening commitments…").
    """
    if not today_events:
        return []

    scored: list[tuple[float, str]] = []

    # 1) Check-in cues already matched onto today's events — highest priority.
    for e in today_events:
        for cue in e.get("cues") or []:
            tip = _format_reminder(str(cue), event_summary=None, limit=100)
            if tip:
                scored.append((5.0, tip))

    # 2) Profile bullets that match a *specific* today event (no busy-day free pass).
    for b in _active_bullets(snapshot):
        matched_summary, score = _best_event_match(b.text, today_events)
        if score < 1 or not matched_summary:
            continue
        tip = _format_reminder(b.text, event_summary=matched_summary, limit=110)
        if not tip:
            continue
        scored.append((float(score) + 2.0, tip))

    # 3) Facts that match a specific today event.
    for f in facts:
        if f.type not in (
            FactType.CONSTRAINT,
            FactType.COMMITMENT,
            FactType.PREFERENCE,
            FactType.CONCERN,
        ):
            continue
        if f.confidence < 0.55:
            continue
        matched_summary, score = _best_event_match(f.statement, today_events)
        if score < 1 or not matched_summary:
            continue
        tip = _format_reminder(f.statement, event_summary=matched_summary, limit=110)
        if not tip:
            continue
        scored.append((float(score) + 1.0, tip))

    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    seen: set[str] = set()
    out: list[str] = []
    for _, text in scored:
        key = re.sub(r"^remember:\s*", "", text.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= 4:
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


def build_tomorrow_preview(
    *,
    tomorrow_events: list[dict[str, Any]],
    weekday_label: str,
    cues_by_event: list[list[str]] | None = None,
    facts: list[Fact] | None = None,
    snapshot: ProfileSnapshot | None = None,
) -> tuple[str, list[str]]:
    """Compact count summary + short remember tips (events rendered in UI)."""
    cues_by_event = cues_by_event or [[] for _ in tomorrow_events]
    facts = facts or []
    n = len(tomorrow_events)

    if n == 0:
        return (f"Nothing on the calendar for {weekday_label} yet.", [])

    summary = f"{n} event{'s' if n != 1 else ''} tomorrow"
    remember: list[str] = []

    for cues in cues_by_event:
        for c in cues:
            tip = _format_reminder(c, limit=88)
            if tip and tip not in remember:
                remember.append(tip)
            if len(remember) >= 3:
                break
        if len(remember) >= 3:
            break

    scored: list[tuple[float, str]] = []
    for b in _active_bullets(snapshot):
        matched_summary, score = _best_event_match(b.text, tomorrow_events)
        if score < 1 or not matched_summary:
            continue
        tip = _format_reminder(b.text, event_summary=matched_summary, limit=88)
        if tip:
            scored.append((score + 1.0, tip))
    for f in facts:
        if f.type not in (FactType.CONSTRAINT, FactType.COMMITMENT, FactType.PREFERENCE):
            continue
        matched_summary, score = _best_event_match(f.statement, tomorrow_events)
        if score < 1 or not matched_summary:
            continue
        tip = _format_reminder(f.statement, event_summary=matched_summary, limit=88)
        if tip:
            scored.append((float(score), tip))
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    for _, tip in scored:
        key = re.sub(r"^remember:\s*", "", tip.lower()).strip()
        if any(re.sub(r"^remember:\s*", "", r.lower()).strip() == key for r in remember):
            continue
        remember.append(tip)
        if len(remember) >= 3:
            break

    return summary, remember[:3]


__all__ = ["build_recommendations", "build_tomorrow_preview", "format_event_time"]
