"""Today home — day's calendar + recommendations + day check-in."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import (
    get_calendar_sync_store,
    get_event_cue_store,
    get_memory,
    get_token_store,
)
from level_core.auth.tokens import TokenStore
from level_core.calendar.activity_art import activity_color, infer_activity_kind
from level_core.calendar.agenda_sync import day_events_cached_or_live, refresh_agenda_cache
from level_core.calendar.event_cues import EventCue, EventCueStore, match_cues_for_summary
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.memory.base import MemoryBank
from level_core.models.base import GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.today import (
    build_recommendations,
    build_tomorrow_preview,
    format_event_time,
)
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource
from level_core.schemas.user import OAuthToken, format_person_name

_logger = get_logger(__name__)

router = APIRouter(prefix="/v1/today", tags=["today"])


class TodayEvent(BaseModel):
    id: str
    summary: str
    start: str | None = None
    end: str | None = None
    all_day: bool = False
    when_label: str = ""
    activity_kind: str = "generic"
    color: str = "#8aa4b0"
    cues: list[str] = Field(default_factory=list)


class TomorrowPreview(BaseModel):
    weekday_label: str = ""
    date_label: str = ""
    summary: str = ""
    remember: list[str] = Field(default_factory=list)
    events: list[TodayEvent] = Field(default_factory=list)


class TodayResponse(BaseModel):
    user_id: str
    display_name: str | None = None
    greeting_name: str = "there"
    weekday_label: str = ""
    date_label: str = ""
    google_connected: bool
    events: list[TodayEvent] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    tomorrow: TomorrowPreview | None = None
    profile_ready: bool = False
    needs_review: bool = False
    fact_count: int = 0
    manifesto: str | None = None


class DayCheckInRequest(BaseModel):
    message: str = Field(min_length=4, max_length=2000)


class DayCheckInResponse(BaseModel):
    reply: str
    facts_added: int = 0
    cues_added: int = 0
    today: TodayResponse | None = None


class _CheckInParse(BaseModel):
    profile_note: str = ""
    keywords: list[str] = Field(default_factory=list)
    reminder: str = ""
    reply: str = ""


class _TomorrowLLM(BaseModel):
    summary: str = ""
    remember: list[str] = Field(default_factory=list)


def _local_now() -> datetime:
    return datetime.now(tz=ZoneInfo("America/Los_Angeles"))


def _day_labels(day: datetime) -> tuple[str, str]:
    weekday = day.strftime("%A")
    date_label = f"{day.strftime('%B')} {day.day}, {day.year}"
    return weekday, date_label


def _greeting_labels(now: datetime | None = None) -> tuple[str, str]:
    local = now or _local_now()
    return _day_labels(local)


def _first_name(display_name: str | None) -> str:
    raw = format_person_name(display_name) or ""
    if not raw:
        return "there"
    if raw.lower() in {"guest parent", "caregiver", "guest"}:
        return "there"
    return raw.split()[0][:40]


def _to_today_event(raw: dict, user_cues: list) -> TodayEvent:
    summary = raw.get("summary") or "(no title)"
    kind = infer_activity_kind(summary)
    return TodayEvent(
        id=raw.get("id") or "",
        summary=summary,
        start=raw.get("start"),
        end=raw.get("end"),
        all_day=bool(raw.get("all_day")),
        when_label=format_event_time(raw.get("start"), all_day=bool(raw.get("all_day"))),
        activity_kind=kind,
        color=activity_color(kind),
        cues=match_cues_for_summary(summary, user_cues),
    )


async def _polish_tomorrow(
    *,
    weekday: str,
    events: list[TodayEvent],
    heuristic_summary: str,
    heuristic_remember: list[str],
    profile_bits: str,
) -> tuple[str, list[str]]:
    if not events:
        return heuristic_summary, heuristic_remember
    settings = get_settings()
    try:
        gemini = build_gemini_client(settings)
        agenda = "\n".join(
            f"- {e.when_label}: {e.summary}"
            + (f" (remember: {'; '.join(e.cues)})" if e.cues else "")
            for e in events
        )
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You write a brief 'what to expect tomorrow' for a busy caregiver. "
                    "summary: 1-2 short sentences covering the shape of the day "
                    "(not a full dump). remember: up to 3 concrete ahead-of-time tips "
                    "from cues/profile — gear, timing, energy. No fluff. JSON only."
                ),
                prompt=(
                    f"Tomorrow is {weekday}.\n"
                    f"Agenda:\n{agenda}\n\n"
                    f"Heuristic summary: {heuristic_summary}\n"
                    f"Heuristic remember: {heuristic_remember}\n"
                    f"Profile/history:\n{profile_bits or '(thin)'}"
                ),
                response_schema=_TomorrowLLM.model_json_schema(),
                temperature=0.3,
                max_output_tokens=220,
            )
        )
        out = _TomorrowLLM.model_validate(json.loads(resp.text))
        summary = (out.summary or heuristic_summary).strip()[:340]
        remember = [r.strip() for r in out.remember if r and r.strip()][:3]
        if not remember:
            remember = heuristic_remember
        return summary, remember
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        return heuristic_summary, heuristic_remember
    except Exception:  # noqa: BLE001
        return heuristic_summary, heuristic_remember


async def _warm_agenda_cache_bg(user_id: str, token: OAuthToken, sync_store: CalendarSyncStore) -> None:
    try:
        await refresh_agenda_cache(user_id=user_id, token=token, sync_store=sync_store)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("agenda_cache_bg_warm_failed", user_id=user_id, error=str(exc))


@router.get("", response_model=TodayResponse)
async def get_today(
    background_tasks: BackgroundTasks,
    polish: bool = Query(
        False,
        description="If true, Gemini-polish tomorrow blurb (slower). Default is fast heuristic.",
    ),
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    cues: EventCueStore = Depends(get_event_cue_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> TodayResponse:
    user = await tokens.get_user(user_id)
    display_name = format_person_name(user.display_name) if user else None
    weekday, date_label = _greeting_labels()

    token = await tokens.get_google_token(user_id)
    google_connected = token is not None and bool(
        token.refresh_token or token.access_token
    )
    user_cues = await cues.list_for_user(user_id)
    events_out: list[TodayEvent] = []
    tomorrow_events: list[TodayEvent] = []
    if token is not None:
        try:
            # Parallel day pulls; never block on full agenda resync (that was the lag).
            raw_today, raw_tomorrow = await asyncio.gather(
                day_events_cached_or_live(
                    user_id=user_id,
                    token=token,
                    sync_store=sync_store,
                    day_offset=0,
                ),
                day_events_cached_or_live(
                    user_id=user_id,
                    token=token,
                    sync_store=sync_store,
                    day_offset=1,
                ),
            )
            state = await sync_store.get(user_id)
            updated = state.agenda_updated_at if state else None
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            cache_stale = (
                state is None
                or updated is None
                or (datetime.now(tz=timezone.utc) - updated).total_seconds() > 3600
            )
            if cache_stale:
                background_tasks.add_task(_warm_agenda_cache_bg, user_id, token, sync_store)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Calendar read failed: {exc}") from exc
        events_out = [_to_today_event(e, user_cues) for e in raw_today]
        tomorrow_events = [_to_today_event(e, user_cues) for e in raw_tomorrow]

    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    recs = build_recommendations(
        today_events=[e.model_dump() for e in events_out],
        snapshot=snapshot,
        facts=facts,
    )

    tom_local = _local_now() + timedelta(days=1)
    tom_weekday, tom_date = _day_labels(tom_local)
    heur_summary, heur_remember = build_tomorrow_preview(
        tomorrow_events=[e.model_dump() for e in tomorrow_events],
        weekday_label=tom_weekday,
        cues_by_event=[e.cues for e in tomorrow_events],
        facts=facts,
        snapshot=snapshot,
    )
    # Hot path: heuristic only. Gemini polish was adding seconds to every Today load.
    summary, remember = heur_summary, heur_remember
    if polish and tomorrow_events:
        profile_bits = "\n".join(
            [
                *(f"- {f.statement}" for f in facts[:12]),
                *(
                    f"- {b.text}"
                    for b in (snapshot.bullets if snapshot else [])[:8]
                ),
            ]
        )
        summary, remember = await _polish_tomorrow(
            weekday=tom_weekday,
            events=tomorrow_events,
            heuristic_summary=heur_summary,
            heuristic_remember=heur_remember,
            profile_bits=profile_bits,
        )
    tomorrow = TomorrowPreview(
        weekday_label=tom_weekday,
        date_label=tom_date,
        summary=summary,
        remember=remember,
        events=tomorrow_events[:8],
    )

    return TodayResponse(
        user_id=user_id,
        display_name=display_name,
        greeting_name=_first_name(display_name),
        weekday_label=weekday,
        date_label=date_label,
        google_connected=google_connected,
        events=events_out,
        recommendations=recs,
        tomorrow=tomorrow,
        profile_ready=bool(snapshot and snapshot.bullets),
        needs_review=bool(snapshot.needs_review) if snapshot else False,
        fact_count=len(facts),
        manifesto=manifesto.statement if manifesto else None,
    )


@router.post("/check-in", response_model=DayCheckInResponse)
async def day_check_in(
    payload: DayCheckInRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    cue_store: EventCueStore = Depends(get_event_cue_store),
) -> DayCheckInResponse:
    """Friendly day check-in → profile fact + optional event-linked reminder."""
    import uuid

    settings = get_settings()
    gemini = build_gemini_client(settings)
    message = payload.message.strip()
    parsed = _CheckInParse(
        profile_note=message[:400],
        reply="Got it — I’ll remember that.",
    )
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You help Level learn from a caregiver's day check-in. "
                    "Extract a first-person profile_note (what is true about their life). "
                    "If they mention something tied to a recurring activity "
                    "(soccer, school, dinner, work, etc.), also extract lowercase keywords "
                    "that would appear in a calendar title, and a short warm reminder "
                    "shown on matching days (e.g. \"Don't forget Jordan's shoes today!\"). "
                    "If nothing activity-specific, leave keywords/reminder empty. "
                    "reply is 1 short warm sentence. JSON only."
                ),
                prompt=f"User said: {message}",
                response_schema=_CheckInParse.model_json_schema(),
                temperature=0.25,
                max_output_tokens=280,
            )
        )
        parsed = _CheckInParse.model_validate(json.loads(resp.text))
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        pass

    note = (parsed.profile_note or message).strip()[:500]
    facts_added = 0
    if len(note) >= 8:
        fact = Fact(
            user_id=user_id,
            type=FactType.CONSTRAINT if parsed.keywords else FactType.PREFERENCE,
            statement=note if note.lower().startswith("i ") else f"I notice: {note}",
            source_signal_ids=[],
            salience=0.7,
        )
        await memory.facts.upsert(fact)
        facts_added = 1
        signal = Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id=f"day-checkin:{uuid.uuid4().hex[:12]}",
            text=f"Day check-in: {message}",
        )
        await memory.signals.upsert(signal)

    cues_added = 0
    keywords = [k.strip().lower() for k in parsed.keywords if k and k.strip()][:6]
    reminder = (parsed.reminder or "").strip()[:220]
    if keywords and reminder:
        await cue_store.add(
            EventCue(
                user_id=user_id,
                keywords=keywords,
                reminder=reminder,
                source_text=message[:400],
            )
        )
        cues_added = 1

    reply = (parsed.reply or "Got it — I’ll keep that in mind.").strip()[:400]
    if cues_added and keywords:
        reply = f"{reply} I’ll nudge you on {keywords[0]} days."

    today = await get_today(user_id=user_id, memory=memory, tokens=tokens, cues=cue_store)
    return DayCheckInResponse(
        reply=reply,
        facts_added=facts_added,
        cues_added=cues_added,
        today=today,
    )


__all__ = ["router"]
