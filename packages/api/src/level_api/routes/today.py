"""Today page: greeting, events, missing usuals, day summary for TTS."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import agenda_is_fresh, refresh_agenda
from level_core.calendar.usuals import (
    current_week_bounds,
    missing_usuals_this_week,
    missing_usuals_today,
)
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.schemas import ActivityType, CachedEvent, LoadBucket
from level_core.storage.base import UserStore
from level_core.tz import resolve_tz
from level_core.voice.summary import get_daily_summary, prewarm_daily_summary

from level_api.deps import get_user_store

logger = get_logger(__name__)

router = APIRouter()


async def _enrich_safe(store: UserStore) -> None:
    try:
        await enrich_agenda(store)
    except Exception as exc:  # noqa: BLE001 - never fail the homepage on classify
        logger.warning("today.enrich_failed", error=str(exc)[:300])
    # Chain: once agenda is enriched, warm the "Hear my day" cache
    # in the same background task so the user's click doesn't pay
    # a cold LLM roundtrip. See voice.summary.prewarm_daily_summary.
    await prewarm_daily_summary(store)


async def refresh_and_enrich_safe(store: UserStore) -> None:
    """Pull Google, then classify. Safe to run after the HTTP response."""
    try:
        result = await refresh_agenda(store)
    except Exception as exc:  # noqa: BLE001
        logger.warning("today.refresh_failed", error=str(exc)[:300])
        await store.calendar_sync.update_fields(last_error=str(exc)[:400])
        return
    try:
        existing = await store.agenda.list()
        if result.fingerprint_changed or any(e.activity_type is None for e in existing):
            await enrich_agenda(store)
    except Exception as exc:  # noqa: BLE001
        logger.warning("today.enrich_failed", error=str(exc)[:300])
    # Chain the summary prewarm after the refresh/enrich so the
    # cached text reflects the freshest fingerprint.
    await prewarm_daily_summary(store)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _event_local_date(event: CachedEvent, tz: ZoneInfo):
    return _aware(event.time.start).astimezone(tz).date()


@router.get("")
async def get_today(
    background: BackgroundTasks,
    store: UserStore = Depends(get_user_store),
    tz_name: str | None = Query(default=None, alias="tz", max_length=80),
) -> dict[str, Any]:
    settings = get_settings()
    profile = await store.profile.read() or {}
    tz = resolve_tz(tz_name, profile.get("tz") if isinstance(profile.get("tz"), str) else None)
    if tz_name and tz.key == tz_name.strip() and profile.get("tz") != tz.key:
        profile = dict(profile)
        profile["tz"] = tz.key
        await store.profile.write(profile)

    tokens = await store.tokens.read() or {}
    events = await store.agenda.list()
    pulling = False
    scheduled_summary_prewarm = False
    if tokens.get("access_token") and settings.is_local:
        sync_meta = await store.calendar_sync.read() or {}
        stale = not agenda_is_fresh(sync_meta)
        if stale or not events:
            pulling = not events
            background.add_task(refresh_and_enrich_safe, store)
            scheduled_summary_prewarm = True  # chained inside refresh task
        elif any(e.activity_type is None for e in events):
            background.add_task(_enrich_safe, store)
            scheduled_summary_prewarm = True  # chained inside enrich task
    if not scheduled_summary_prewarm and events:
        # Fresh agenda, no enrichment work needed - still worth warming
        # the summary cache so "Hear my day" is instant. get_daily_summary
        # is idempotent and cache-checks the current fingerprint, so this
        # is cheap on subsequent /today loads.
        background.add_task(prewarm_daily_summary, store)

    events.sort(key=lambda e: e.time.start)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    todays = [e for e in events if _event_local_date(e, tz) == today]
    tomorrows = [e for e in events if _event_local_date(e, tz) == tomorrow]

    usuals = await store.usuals.list()
    missing = missing_usuals_today(usuals=usuals, todays_events=todays, tz=tz)

    reminders_by_id = {
        r.reminder_id: r for r in await store.reminders.list() if r.status == "active"
    }
    people_by_id = {p.person_id: p for p in await store.people.list()}
    events_by_id = {e.event_id: e for e in events}

    def _view(e: Any) -> dict[str, Any]:
        return {
            "event_id": e.event_id,
            "summary": e.summary,
            "start": _aware(e.time.start).astimezone(tz).isoformat(),
            "end": _aware(e.time.end).astimezone(tz).isoformat(),
            "activity_type": e.activity_type,
            "origin": e.origin,
            "level_reason": e.level_reason,
            "people": [
                {
                    "person_id": pid,
                    "display_name": people_by_id.get(pid).display_name if people_by_id.get(pid) else None,
                }
                for pid in e.matched_person_ids
                if people_by_id.get(pid)
            ],
            "reminders": [
                {"reminder_id": rid, "text": reminders_by_id[rid].text}
                for rid in e.matched_reminder_ids
                if reminders_by_id.get(rid)
            ],
        }

    week_start, week_end = current_week_bounds(today)
    week = [
        e for e in events
        if week_start <= _event_local_date(e, tz) < week_end
    ]
    missing_week = missing_usuals_this_week(
        usuals=usuals,
        week_events=week,
        as_of_date=today,
        events_by_id=events_by_id,
        tz=tz,
    )

    week_start_iso = week_start.isoformat()
    dismissed_this_week = profile.get("dismissed_missing_week") == week_start_iso
    resolved_ids = _resolved_group_ids(profile, week_start_iso)
    missing_week_view = (
        []
        if dismissed_this_week
        else [
            row
            for row in (
                _decorate_missing_group(g, usuals, events_by_id, people_by_id, tz, today)
                for g in missing_week
            )
            if row["group_id"] not in resolved_ids
        ]
    )

    sync_meta = await store.calendar_sync.read() or {}
    calendars = sync_meta.get("calendars") or []
    # Proactive cards are populated by the nightly job (packages/jobs).
    # We only surface cards for the CURRENT week so a stale run doesn't
    # keep nudging after Monday rolls over.
    proactive_raw = profile.get("proactive_cards") or {}
    week_start_iso = week_start.isoformat()
    proactive_cards: list[dict[str, Any]] = []
    if (
        isinstance(proactive_raw, dict)
        and proactive_raw.get("week_start") == week_start_iso
    ):
        for card in (proactive_raw.get("cards") or [])[:5]:
            if card.get("card_id") in _dismissed_card_ids(profile, week_start_iso):
                continue
            proactive_cards.append(card)
    return {
        "date": today.isoformat(),
        "tz": tz.key,
        "today": [_view(e) for e in todays],
        "tomorrow": [_view(e) for e in tomorrows],
        "missing_usuals": [
            {
                "usual_id": m.usual.usual_id,
                "display_summary": m.usual.display_summary,
                "person_id": m.usual.person_id,
                "hour_band": m.usual.hour_band,
            }
            for m in missing
        ],
        "missing_usuals_week": missing_week_view,
        "missing_usuals_week_dismissed": dismissed_this_week,
        "week_load": _week_load(week),
        "proactive_cards": proactive_cards,
        "sync": {
            "calendars": calendars,
            "last_error": sync_meta.get("last_error"),
            "last_pull_at": sync_meta.get("last_pull_at"),
            "total_cached": len(events),
            "pulling": pulling,
        },
    }


def _dismissed_card_ids(profile: dict[str, Any], week_start_iso: str) -> set[str]:
    """Return card ids the user dismissed this week."""
    raw = profile.get("dismissed_proactive_cards")
    if not isinstance(raw, dict) or raw.get("week_start") != week_start_iso:
        return set()
    return {str(x) for x in (raw.get("card_ids") or []) if x}


class DismissCardBody(BaseModel):
    card_id: str = Field(min_length=1, max_length=160)


@router.post("/proactive-cards/dismiss")
async def dismiss_proactive_card(
    body: DismissCardBody, store: UserStore = Depends(get_user_store)
) -> dict[str, str]:
    """Hide one proactive card for the rest of this ISO week.

    We keep the dismissal per-card (not global) so unrelated cards keep
    working. Next week the nightly job regenerates fresh cards.
    """
    profile = await store.profile.read() or {}
    tz = resolve_tz(profile.get("tz") if isinstance(profile.get("tz"), str) else None)
    today = datetime.now(tz).date()
    week_start, _week_end = current_week_bounds(today)
    week_start_iso = week_start.isoformat()
    raw = profile.get("dismissed_proactive_cards")
    if not isinstance(raw, dict) or raw.get("week_start") != week_start_iso:
        ids: list[str] = []
    else:
        ids = [str(x) for x in (raw.get("card_ids") or []) if x]
    if body.card_id not in ids:
        ids.append(body.card_id)
    profile["dismissed_proactive_cards"] = {
        "week_start": week_start_iso,
        "card_ids": ids,
    }
    await store.profile.write(profile)
    return {"status": "dismissed", "card_id": body.card_id}


@router.post("/missing-week/dismiss")
async def dismiss_missing_week(store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    """Hide 'usuals missing this week' until next Monday.

    The user is saying this week is intentionally different, not that the
    usuals are wrong forever. Next week the list comes back.
    """
    profile = await store.profile.read() or {}
    tz = resolve_tz(profile.get("tz") if isinstance(profile.get("tz"), str) else None)
    today = datetime.now(tz).date()
    week_start, _week_end = current_week_bounds(today)
    profile["dismissed_missing_week"] = week_start.isoformat()
    await store.profile.write(profile)
    return {"status": "dismissed", "week_start": week_start.isoformat()}


class ResolveMissingBody(BaseModel):
    group_id: str = Field(min_length=1, max_length=160)


@router.post("/missing-week/resolve")
async def resolve_missing_group(
    body: ResolveMissingBody,
    store: UserStore = Depends(get_user_store),
) -> dict[str, Any]:
    """Hide one missing-usual row until next Monday.

    The user handled this gap (coverage arranged, or they just don't need
    the nag). Other missing usuals this week stay. Next week it can return.
    """
    profile = await store.profile.read() or {}
    tz = resolve_tz(profile.get("tz") if isinstance(profile.get("tz"), str) else None)
    today = datetime.now(tz).date()
    week_start, _week_end = current_week_bounds(today)
    week_start_iso = week_start.isoformat()
    raw = profile.get("resolved_missing_week")
    if not isinstance(raw, dict) or raw.get("week_start") != week_start_iso:
        ids: list[str] = []
    else:
        ids = [str(x) for x in (raw.get("group_ids") or []) if x]
    if body.group_id not in ids:
        ids.append(body.group_id)
    profile["resolved_missing_week"] = {"week_start": week_start_iso, "group_ids": ids}
    await store.profile.write(profile)
    return {"status": "resolved", "group_id": body.group_id, "week_start": week_start_iso}


def _resolved_group_ids(profile: dict[str, Any], week_start_iso: str) -> set[str]:
    raw = profile.get("resolved_missing_week")
    if not isinstance(raw, dict) or raw.get("week_start") != week_start_iso:
        return set()
    return {str(x) for x in (raw.get("group_ids") or []) if x}


def _decorate_missing_group(
    group: Any,
    all_usuals: list[Any],
    events_by_id: dict[str, CachedEvent],
    people_by_id: dict[str, Any],
    tz: ZoneInfo,
    today: Any,
) -> dict[str, Any]:
    """Coarse category-level missing entry with typical time + person context."""
    starts: list[int] = []
    durations: list[int] = []
    usuals_by_id = {u.usual_id: u for u in all_usuals}
    for uid in group.representative_usual_ids:
        u = usuals_by_id.get(uid)
        if not u:
            continue
        for src_uid in u.source_event_uids:
            ev = events_by_id.get(src_uid)
            if not ev or ev.time.all_day:
                continue
            s_local = ev.time.start.astimezone(tz)
            e_local = ev.time.end.astimezone(tz)
            starts.append(s_local.hour * 60 + s_local.minute)
            durations.append(max(15, int((e_local - s_local).total_seconds() // 60)))
    if starts:
        start_min = int(median(starts))
        dur_min = int(median(durations))
        typical_start = _fmt_hm(start_min)
        typical_end = _fmt_hm(start_min + dur_min)
    else:
        typical_start = None
        typical_end = None

    person_views: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pid in group.person_ids:
        if pid in seen:
            continue
        seen.add(pid)
        person = people_by_id.get(pid)
        if not person:
            continue
        person_views.append(
            {
                "person_id": pid,
                "display_name": person.display_name,
                "relation": person.relation.value,
            }
        )
    person_views.sort(key=lambda p: (p["display_name"] or "").lower())
    primary = person_views[0] if person_views else {
        "person_id": group.person_id,
        "display_name": None,
        "relation": None,
    }
    week_start, _week_end = current_week_bounds(today)
    day_this_week = week_start + timedelta(days=int(group.weekday))
    return {
        "group_id": f"{int(group.weekday)}:{group.category.value}",
        "weekday": int(group.weekday),
        "date": day_this_week.isoformat(),
        "category": group.category.value,
        "category_label": group.category.label,
        "person_id": primary["person_id"],
        "person_name": primary["display_name"],
        "person_relation": primary["relation"],
        "people": person_views,
        "typical_start": typical_start,
        "typical_end": typical_end,
    }


def _fmt_hm(total_minutes: int) -> str:
    total_minutes = max(0, min(23 * 60 + 59, total_minutes))
    hour_24 = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "am" if hour_24 < 12 else "pm"
    hour_12 = hour_24 % 12 or 12
    if minute == 0:
        return f"{hour_12}{suffix}"
    return f"{hour_12}:{minute:02d}{suffix}"


def _week_load(week_events: list[Any]) -> list[dict[str, Any]]:
    """Weekly percentage load, rolled up to LoadBucket.

    We group at the coarse level (School, Sports, Medical, Work, ...) so the
    bar isn't fractured into eleven 3% slivers. The finer ActivityType is
    still used by the missing-usuals view where the specificity matters.
    """
    counts: Counter[LoadBucket] = Counter()
    for e in week_events:
        activity = e.activity_type or ActivityType.OTHER
        counts[activity.load_bucket] += 1
    total = sum(counts.values())
    if total == 0:
        return []
    return [
        {
            "bucket": bucket.value,
            "label": bucket.label,
            "color": bucket.color,
            "count": n,
            "percent": round((n / total) * 100),
        }
        for bucket, n in counts.most_common()
    ]


@router.get("/summary")
async def summary(store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    text = await get_daily_summary(store)
    return {"summary": text}


@router.get("/learned")
async def what_level_learned(
    store: UserStore = Depends(get_user_store),
) -> dict[str, Any]:
    """Return the 3 most recent corrections + memories Level applied.

    Powers the "What Level learned" strip on /today. This surfaces BOTH:
      * negatives - things Level tried and the user rejected. Get
        injected as few-shot into the next matching agent call.
      * memories - long-lived facts the user confirmed via a keep chip.
        Get recalled by EmailAgent + SummaryAgent as context.

    Both are the visible manifestation of the "constantly adapts to
    user" rubric bullet.
    """
    from level_core.agents.memory_bank import recall as recall_memories

    negatives = await store.negatives.list()
    negatives.sort(key=lambda n: n.created_at, reverse=True)
    latest_neg = negatives[:3]

    memories = await recall_memories(store, limit=3)

    return {
        "total": len(negatives),
        "memories_total": (
            len((await store.profile.read() or {}).get("memory_bank", {}).get("memories") or [])
        ),
        "recent": [
            {
                "kind": "negative",
                "negative_id": n.negative_id,
                "agent": n.agent.value,
                "field": n.field,
                "value": n.value,
                "reason": n.reason,
                "created_at": n.created_at.isoformat(),
            }
            for n in latest_neg
        ],
        "memories": [
            {
                "kind": "memory",
                "id": m["id"],
                "text": m["text"],
                "tags": m.get("tags") or [],
                "created_at": m.get("created_at"),
            }
            for m in memories
        ],
    }
