"""Today home — day's calendar + recommendations + day check-in."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
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
from level_api.routes.sources import (
    _bg_enrich_care,
    _seed_care_from_agenda_fast,
)
from level_core.auth.tokens import TokenStore
from level_core.calendar.activity_art import activity_color, infer_activity_kind
from level_core.calendar.agenda_sync import day_events_cached_or_live, refresh_agenda_cache
from level_core.calendar.event_cues import EventCue, EventCueStore, match_cues_for_summary
from level_core.calendar.sync_state import (
    CalendarSyncStore,
    events_for_local_day,
)
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.memory.base import MemoryBank
from level_core.models.base import GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_infer_llm import classify_week_event_roles_ai
from level_core.schemas.care import clean_conflict_summaries
from level_core.profile.synthesize import (
    build_holding_summary,
    build_week_role_load,
    cached_care_graph,
    filter_events_for_local_week,
    invalidate_care_graph_cache,
    resolve_event_care_role,
)
from level_core.profile.today import (
    build_recommendations,
    build_tomorrow_preview,
    format_event_time,
)
from level_core.schemas.base import _now_utc
from level_core.schemas.care import CareGraph
from level_core.schemas.decision import DecisionStatus
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


class PendingChallenge(BaseModel):
    """Unsolicited role-theft challenge from the Continuous Action job."""

    decision_id: str
    trigger_label: str
    question: str | None = None
    challenge_type: str | None = None


class HoldingChip(BaseModel):
    label: str
    role_id: str
    color: str


class WeekRoleLoad(BaseModel):
    role_id: str
    label: str
    color: str
    percent: int
    event_count: int = 0
    minutes: int = 0


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
    pending_challenges: list[PendingChallenge] = Field(default_factory=list)
    care_graph: CareGraph | None = None
    holding: list[HoldingChip] = Field(default_factory=list)
    week_load: list[WeekRoleLoad] = Field(default_factory=list)
    conflict_summaries: list[str] = Field(default_factory=list)


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
    # Exact calendar titles from the provided agenda this note applies to.
    matched_titles: list[str] = Field(default_factory=list)
    # When the note is about job/work, set paid_work so we can tag those events.
    care_role: str = ""


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


def _to_today_event(
    raw: dict,
    user_cues: list,
    *,
    role_by_summary: dict[str, str] | None = None,
) -> TodayEvent:
    summary = raw.get("summary") or "(no title)"
    key = " ".join(summary.strip().lower().split())
    care_role = (role_by_summary or {}).get(key)
    kind = infer_activity_kind(summary, care_role=care_role)
    return TodayEvent(
        id=raw.get("id") or "",
        summary=summary,
        start=raw.get("start"),
        end=raw.get("end"),
        all_day=bool(raw.get("all_day")),
        when_label=format_event_time(
            raw.get("start"),
            end_raw=raw.get("end"),
            all_day=bool(raw.get("all_day")),
        ),
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


async def _bg_classify_week_roles(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
) -> None:
    """Background: classify this week's titles into care roles (do not block /today)."""
    try:
        care = await memory.manifestos.get_care_profile(user_id=user_id)
        if care is None:
            return
        state = await sync_store.get(user_id)
        if state is None or not state.events:
            return
        agenda_events = [
            {
                "summary": ev.summary,
                "start": ev.start,
                "end": ev.end,
                "all_day": ev.all_day,
            }
            for ev in state.events.values()
            if ev.summary
        ]
        week_events = filter_events_for_local_week(agenda_events)
        hints = dict(care.calendar_role_by_summary)
        needs = any(
            resolve_event_care_role(
                str(ev.get("summary") or ""),
                role_by_summary=hints or None,
            )
            is None
            for ev in week_events
        )
        if not needs or not week_events:
            return
        week_hints = await classify_week_event_roles_ai(
            week_events=week_events,  # type: ignore[arg-type]
            profile=care,
            gemini=build_gemini_client(get_settings()),
        )
        if not week_hints:
            return
        merged = {**hints, **week_hints}
        care = care.model_copy(
            update={
                "calendar_role_by_summary": merged,
                "version": int(care.version or 1) + 1,
                "updated_at": _now_utc(),
            }
        )
        invalidate_care_graph_cache(user_id)
        await memory.manifestos.save_care_profile(care)
        _logger.info(
            "week_roles_classified_bg",
            user_id=user_id,
            tagged=len(week_hints),
        )
    except Exception:  # noqa: BLE001
        _logger.exception("week_roles_classify_bg_failed", user_id=user_id)


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
    user, token, user_cues = await asyncio.gather(
        tokens.get_user(user_id),
        tokens.get_google_token(user_id),
        cues.list_for_user(user_id),
    )
    display_name = format_person_name(user.display_name) if user else None
    weekday, date_label = _greeting_labels()

    google_connected = token is not None and bool(
        token.refresh_token or token.access_token
    )
    events_out: list[TodayEvent] = []
    tomorrow_events: list[TodayEvent] = []
    care = None
    state = None
    if token is not None:
        try:
            # Parallel day pulls; never block on full agenda resync (that was the lag).
            raw_today, raw_tomorrow, state, care = await asyncio.gather(
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
                sync_store.get(user_id),
                memory.manifestos.get_care_profile(user_id=user_id),
            )
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
        role_hints = dict(care.calendar_role_by_summary) if care else None
        events_out = [
            _to_today_event(e, user_cues, role_by_summary=role_hints)
            for e in raw_today
        ]
        tomorrow_events = [
            _to_today_event(e, user_cues, role_by_summary=role_hints)
            for e in raw_tomorrow
        ]
    else:
        care = await memory.manifestos.get_care_profile(user_id=user_id)

    facts, snapshot, manifesto, decisions = await asyncio.gather(
        memory.facts.list_for_user(user_id=user_id, limit=200),
        memory.manifestos.get_profile_snapshot(user_id=user_id),
        memory.manifestos.get_current_manifesto(user_id=user_id),
        memory.decisions.list_for_user(user_id=user_id, limit=20),
    )
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

    # Hot path: use decision trigger only — do not await turn history per decision.
    pending: list[PendingChallenge] = []
    try:
        for d in decisions:
            if d.status is not DecisionStatus.OPEN:
                continue
            if d.origin != "async_role_theft":
                continue
            pending.append(
                PendingChallenge(
                    decision_id=d.decision_id,
                    trigger_label=d.trigger_label or "Care collision on your calendar",
                    question=None,
                    challenge_type="role_theft",
                )
            )
            if len(pending) >= 3:
                break
    except Exception:  # noqa: BLE001
        _logger.warning("pending_challenges_failed", user_id=user_id)

    care_graph = None
    holding: list[HoldingChip] = []
    week_load: list[WeekRoleLoad] = []
    conflict_summaries: list[str] = []
    try:
        agenda_events: list[dict] = [
            {
                "summary": e.summary,
                "start": e.start,
                "end": e.end,
                "all_day": e.all_day,
            }
            for e in events_out
            if e.summary
        ]
        # Prefer full agenda cache for category counts when available.
        if state and state.events:
            agenda_events = [
                {
                    "summary": ev.summary,
                    "start": ev.start,
                    "end": ev.end,
                    "all_day": ev.all_day,
                }
                for ev in state.events.values()
                if ev.summary
            ]
        elif state is None:
            try:
                state = await sync_store.get(user_id)
                if state and state.events:
                    agenda_events = [
                        {
                            "summary": ev.summary,
                            "start": ev.start,
                            "end": ev.end,
                            "all_day": ev.all_day,
                        }
                        for ev in state.events.values()
                        if ev.summary
                    ]
            except Exception:  # noqa: BLE001
                pass

        if agenda_events and (
            care is None
            or not care.roles
            or not care.calendar_role_by_summary
        ):
            # Missing care OR missing role hints — same bg path creates or enriches.
            background_tasks.add_task(
                _bg_enrich_care,
                user_id,
                memory,
                sync_store,
                force=True,
            )
        # Opt-in regex seed only (LEVEL_ALLOW_HEURISTIC_CARE=1).
        if (care is None or not care.roles) and agenda_events:
            care = await _seed_care_from_agenda_fast(
                user_id=user_id,
                memory=memory,
                sync_store=sync_store,
                events=agenda_events,
            )
            invalidate_care_graph_cache(user_id)

        if care is not None:
            week_events = filter_events_for_local_week(agenda_events)
            hints = dict(care.calendar_role_by_summary)
            needs_week_ai = any(
                resolve_event_care_role(
                    str(ev.get("summary") or ""),
                    role_by_summary=hints or None,
                )
                is None
                for ev in week_events
            )
            # Never await Gemini on /today — classify in the background.
            if needs_week_ai and week_events:
                background_tasks.add_task(
                    _bg_classify_week_roles,
                    user_id,
                    memory,
                    sync_store,
                )

            care_graph, _, _ = cached_care_graph(care, agenda_events or None)
            holding = [
                HoldingChip.model_validate(row)
                for row in build_holding_summary(care)
            ]
            week_load = [
                WeekRoleLoad.model_validate(row)
                for row in build_week_role_load(care, agenda_events)
            ]
            conflict_summaries = clean_conflict_summaries(care.conflict_summaries)[:3]
    except Exception:  # noqa: BLE001
        _logger.warning("care_graph_failed", user_id=user_id)

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
        pending_challenges=pending,
        care_graph=care_graph,
        holding=holding,
        week_load=week_load,
        conflict_summaries=conflict_summaries,
    )


@router.post("/check-in", response_model=DayCheckInResponse)
async def day_check_in(
    payload: DayCheckInRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    cue_store: EventCueStore = Depends(get_event_cue_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> DayCheckInResponse:
    """Friendly day check-in → profile fact + optional event-linked reminder."""
    settings = get_settings()
    gemini = build_gemini_client(settings)
    message = payload.message.strip()

    # Today's titles help the model attach reminders to real calendar events.
    agenda_titles: list[str] = []
    token, state = await asyncio.gather(
        tokens.get_google_token(user_id),
        sync_store.get(user_id),
    )
    try:
        if token is not None:
            raw_today = await day_events_cached_or_live(
                user_id=user_id,
                token=token,
                sync_store=sync_store,
                day_offset=0,
            )
            agenda_titles = [
                (e.get("summary") or "").strip()
                for e in raw_today
                if (e.get("summary") or "").strip()
            ][:20]
        if not agenda_titles and state and state.events:
            agenda_titles = [
                e["summary"]
                for e in events_for_local_day(state, day_offset=0)
                if e.get("summary")
            ][:20]
    except Exception:  # noqa: BLE001
        agenda_titles = []

    titles_block = "\n".join(f"- {t}" for t in agenda_titles) or "(no titles available)"
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
                    "Reason from the user's words + today's agenda together "
                    "(do not rely on fixed keyword lists).\n"
                    "Extract:\n"
                    "- profile_note: first-person fact about their life.\n"
                    "- keywords: lowercase tokens from agenda titles this note "
                    "should attach to (copy words that appear in those titles).\n"
                    "- matched_titles: exact agenda title strings this note applies "
                    "to (copy from the agenda list; empty if none fit).\n"
                    "- care_role: holistically choose paid_work, child_care, "
                    "elder_care, self_recovery, household_logistics, "
                    "partner_coparent, or empty. Use agenda + wording: e.g. "
                    "forgetting a charger for work + a Meeting on the agenda → "
                    "paid_work and match that Meeting. Classes/courses are not "
                    "self_recovery.\n"
                    "- reminder: short warm nudge shown on matching days.\n"
                    "- reply: 1 short warm sentence (complete — do not trail off).\n"
                    "JSON only."
                ),
                prompt=(
                    f"User said: {message}\n\n"
                    f"Today's agenda titles:\n{titles_block}\n"
                ),
                response_schema=_CheckInParse.model_json_schema(),
                temperature=0.2,
                max_output_tokens=320,
            )
        )
        parsed = _CheckInParse.model_validate(json.loads(resp.text))
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        pass

    note = (parsed.profile_note or message).strip()[:500]
    facts_added = 0
    fact = None
    signal = None
    if len(note) >= 8:
        fact = Fact(
            user_id=user_id,
            type=FactType.CONSTRAINT if parsed.keywords else FactType.PREFERENCE,
            statement=note if note.lower().startswith("i ") else f"I notice: {note}",
            source_signal_ids=[],
            salience=0.7,
        )
        facts_added = 1
        signal = Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id=f"day-checkin:{uuid.uuid4().hex[:12]}",
            text=f"Day check-in: {message}",
        )

    cues_added = 0
    keywords = [k.strip().lower() for k in parsed.keywords if k and k.strip()][:8]
    # Fold AI-matched agenda titles into cue keywords so reminders stick to events.
    for title in parsed.matched_titles:
        t = (title or "").strip()
        if not t:
            continue
        for token in re.findall(r"[a-z0-9']+", t.lower()):
            if len(token) >= 3 and token not in keywords:
                keywords.append(token)
        if len(keywords) >= 8:
            break
    keywords = keywords[:8]
    reminder = (parsed.reminder or "").strip()[:220]
    cue = None
    if keywords and reminder:
        cue = EventCue(
            user_id=user_id,
            keywords=keywords,
            reminder=reminder,
            source_text=message[:400],
        )
        cues_added = 1

    # Apply AI-chosen role tags immediately; holistic enrich reconciles the rest.
    role_raw = (parsed.care_role or "").strip().lower()
    titles_to_tag = [t.strip() for t in parsed.matched_titles if t and t.strip()]
    care = None
    if role_raw and titles_to_tag:
        try:
            care = await memory.manifestos.get_care_profile(user_id=user_id)
        except Exception:  # noqa: BLE001
            care = None

    persist_jobs = []
    if fact is not None and signal is not None:
        persist_jobs.append(memory.facts.upsert(fact))
        persist_jobs.append(memory.signals.upsert(signal))
    if cue is not None:
        persist_jobs.append(cue_store.add(cue))
    if persist_jobs:
        await asyncio.gather(*persist_jobs)

    if role_raw and titles_to_tag and care is not None:
        try:
            hints = dict(care.calendar_role_by_summary)
            for title in titles_to_tag:
                key = re.sub(r"\s+", " ", title.strip().lower())
                if key:
                    hints[key] = role_raw
            care = care.model_copy(
                update={
                    "calendar_role_by_summary": hints,
                    "version": int(care.version or 1) + 1,
                    "updated_at": _now_utc(),
                }
            )
            invalidate_care_graph_cache(user_id)
            await memory.manifestos.save_care_profile(care)
        except Exception:  # noqa: BLE001
            _logger.warning("checkin_care_tag_failed", user_id=user_id)

    # Reclassify agenda off the request path — never block the chat reply on it.
    if facts_added:
        background_tasks.add_task(
            _bg_enrich_care, user_id, memory, sync_store, force=True
        )

    fallback = "Got it — I’ll keep that in mind."
    reply = (parsed.reply or fallback).strip()
    # Models sometimes return Got it: " or a fully quoted sentence.
    if len(reply) >= 2 and reply[0] in "\"'" and reply[-1] == reply[0]:
        reply = reply[1:-1].strip()
    if reply.endswith('"') and reply.count('"') % 2 == 1:
        reply = reply[:-1].rstrip()
    reply = reply.strip() or fallback
    if cues_added and keywords:
        reply = f"{reply} I’ll nudge you on {keywords[0]} days."

    # Reply first — full Today rebuild was adding 1–2s after Gemini already ran.
    return DayCheckInResponse(
        reply=reply,
        facts_added=facts_added,
        cues_added=cues_added,
        today=None,
    )


__all__ = ["router"]
