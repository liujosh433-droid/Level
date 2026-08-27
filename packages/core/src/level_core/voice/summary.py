"""Cache-aware wrapper around SummaryAgent for 'Hear my day'.

If no LLM backend is reachable (demo mode without ``GOOGLE_API_KEY``,
quota exhausted, or any stray SDK error), we synthesize a
deterministic 2-3 sentence summary from the event/missing/reminder
lines instead of 500ing. See ``_fallback_summary``.

Latency: the LLM path is 3-10s tail on ``flash``, which is a bad UX
when the user clicks "Hear my day" and stares at silence. Two things
soften this:

  1. Backend prewarm — ``prewarm_daily_summary`` runs as a background
     task after ``GET /v1/today``, so by the time the user clicks
     they hit the fingerprint cache (~50ms).
  2. Frontend parallelism — ``speakDay`` races the chime fetch
     against the summary fetch instead of running them serially.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from level_core.agents.summary import run as summary_run
from level_core.calendar.usuals import missing_usuals_today
from level_core.observability import get_logger
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store

_LOGGER = get_logger(__name__)


async def get_daily_summary(store: UserStore) -> str:
    tz = await tz_for_store(store)
    today = datetime.now(tz).date().isoformat()

    cache = await store.calendar_sync.read() or {}
    summary_cache: dict[str, Any] = cache.get("summary_cache", {})
    fingerprint = cache.get("events_fingerprint", "")

    cached = summary_cache.get(today)
    if cached and cached.get("fingerprint") == fingerprint:
        return cached["text"]

    events = await store.agenda.list()
    todays = [
        e
        for e in events
        if e.time.start.astimezone(tz).date().isoformat() == today
    ]
    todays.sort(key=lambda e: e.time.start)
    event_lines = [
        f"{e.time.start.astimezone(tz).strftime('%-I:%M %p')} {e.summary}"
        for e in todays
    ]

    usuals = await store.usuals.list()
    missing = missing_usuals_today(usuals=usuals, todays_events=todays, tz=tz)
    missing_lines = [f"{m.usual.display_summary} ({m.expected_hour_band.value})" for m in missing]

    reminder_lines: list[str] = []
    reminders_by_id = {
        r.reminder_id: r for r in await store.reminders.list() if r.status == "active"
    }
    for e in todays:
        for rid in e.matched_reminder_ids:
            r = reminders_by_id.get(rid)
            if r:
                reminder_lines.append(f"{r.text} on {e.summary}")

    # "Hear my day" is user-facing and heard aloud - if the LLM path
    # fails for any reason (soft-degrade, quota, or a stray SDK
    # exception), we'd rather speak a deterministic summary than a
    # 500. call_agent already soft-degrades on the expected failure
    # modes; the try/except catches genuine bugs so the endpoint
    # stays useful during a demo.
    text: str
    try:
        result = await summary_run(
            store=store,
            date_label=today,
            event_lines=event_lines or ["Nothing on the calendar."],
            missing_usual_lines=missing_lines,
            reminder_lines=reminder_lines,
        )
        if result.value:
            text = result.value.summary  # type: ignore[union-attr]
        else:
            text = _fallback_summary(
                event_count=len(todays),
                event_lines=event_lines,
                missing_lines=missing_lines,
                reminder_lines=reminder_lines,
            )
    except Exception as exc:  # noqa: BLE001 - endpoint must not 500
        _LOGGER.warning("voice.summary.llm_failed", error=str(exc)[:200])
        text = _fallback_summary(
            event_count=len(todays),
            event_lines=event_lines,
            missing_lines=missing_lines,
            reminder_lines=reminder_lines,
        )

    # Use `mutate` so a concurrent sync writing sync_tokens or a role
    # run writing last_role_run_fingerprint doesn't clobber this cache
    # (or vice versa). The txn re-reads the doc under contention.
    def _bump(current: dict[str, Any]) -> dict[str, Any]:
        current = dict(current)
        merged = dict(current.get("summary_cache") or {})
        merged[today] = {"text": text, "fingerprint": fingerprint}
        current["summary_cache"] = merged
        return current

    await store.calendar_sync.mutate(_bump)
    return text


async def prewarm_daily_summary(store: UserStore) -> None:
    """Fire-and-forget: populate today's summary cache in the background.

    Called from ``GET /v1/today`` after the response is sent so the
    "Hear my day" click hits the fingerprint cache (~50ms) instead of
    waiting for a cold LLM roundtrip (3-10s). Safe to call every
    ``/today`` load - ``get_daily_summary`` short-circuits when the
    cache is already valid for the current fingerprint.

    Never raises: the summary is a UX nicety, not a correctness
    boundary. Errors are logged and swallowed.
    """
    try:
        await get_daily_summary(store)
    except Exception as exc:  # noqa: BLE001 - background task, never propagate
        _LOGGER.info("voice.summary.prewarm_failed", error=str(exc)[:200])


def _fallback_summary(
    *,
    event_count: int,
    event_lines: list[str],
    missing_lines: list[str],
    reminder_lines: list[str],
) -> str:
    """LLM-free summary. Shape: 2-3 short sentences, TTS-friendly.

    Kept in this module (not the SummaryAgent) so the shape is
    controlled by the caller who knows the display context - here,
    a "Hear my day" audio playback.
    """
    if event_count == 0 and not missing_lines:
        return "Today looks quiet. Nothing on the calendar."

    parts: list[str] = []
    if event_count == 0:
        parts.append("Nothing on the calendar today.")
    elif event_count == 1:
        parts.append(f"One thing today: {event_lines[0]}.")
    elif event_count <= 3:
        parts.append(f"Today: {', then '.join(event_lines)}.")
    else:
        first_two = ", then ".join(event_lines[:2])
        parts.append(
            f"{event_count} things today, starting with {first_two}, "
            f"and {event_count - 2} more after."
        )

    if missing_lines:
        if len(missing_lines) == 1:
            parts.append(f"Heads up: {missing_lines[0]} isn't on the calendar.")
        else:
            parts.append(
                f"{len(missing_lines)} usual things aren't on the calendar: "
                f"{missing_lines[0]} and {len(missing_lines) - 1} more."
            )

    if reminder_lines:
        parts.append(f"Reminder: {reminder_lines[0]}.")

    return " ".join(parts)
