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
    get_proposal_store,
    get_token_store,
)
from level_api.services.chat_turn import run_chat_turn
from level_core.profile.persist import seed_care_from_agenda_fast
from level_api.services.care_enrich import enrich_care_from_agenda as _bg_enrich_care
from level_core.auth.tokens import TokenStore
from level_core.calendar.activity_art import activity_color, infer_activity_kind
from level_core.calendar.agenda_sync import (
    day_events_cached_or_live,
    refresh_agenda_cache,
    refresh_agenda_on_read,
)
from level_core.calendar.event_cues import EventCue, EventCueStore, match_cues_for_summary
from level_core.calendar.proposals import ProposalStore
from level_core.calendar.routines import (
    calendar_tz_label,
    classify_usual,
    format_usual_slot,
    normalize_routine,
    routine_word,
)
from level_core.calendar.usuals import (
    find_usual_gaps,
    horizon_dates,
    usual_local_slot,
    usuals_infer_needed,
)
from level_core.profile.care_store import apply_care, apply_series_usuals
from level_core.profile.people_usuals import merge_series_usuals
from level_core.schemas.care import pending_usuals
from level_core.schemas.commitment import CommitmentProposal
from level_core.calendar.sync_state import (
    CalendarSyncStore,
    events_for_local_day,
    watch_is_live,
)
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.memory.base import MemoryBank
from level_core.models.base import GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_infer_llm import classify_week_event_roles_ai
from level_core.schemas.care import clean_conflict_summaries
from level_core.profile.care_graph import (
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


class UsualGapOut(BaseModel):
    usual_id: str
    person_id: str
    display_name: str
    your_role: str = ""
    their_relation: str = ""
    label: str
    on_date: str
    weekday: int
    start_minute: int
    end_minute: int
    banner: str


class ProposedUsualSlotOut(BaseModel):
    usual_id: str
    weekday: int
    start_minute: int
    end_minute: int
    when_label: str = ""


class ProposedUsualOut(BaseModel):
    usual_id: str
    person_id: str
    display_name: str
    your_role: str = ""
    their_relation: str = ""
    care_role_id: str = ""
    label: str
    weekday: int
    start_minute: int
    end_minute: int
    when_label: str = ""
    usual_ids: list[str] = Field(default_factory=list)
    slots: list[ProposedUsualSlotOut] = Field(default_factory=list)


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
    usual_gaps: list[UsualGapOut] = Field(default_factory=list)
    proposed_usuals: list[ProposedUsualOut] = Field(default_factory=list)


class DayCheckInRequest(BaseModel):
    message: str = Field(min_length=4, max_length=2000)


class DayCheckInResponse(BaseModel):
    reply: str
    facts_added: int = 0
    cues_added: int = 0
    today: TodayResponse | None = None
    school_proposals: list[CommitmentProposal] = Field(default_factory=list)


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


def proposed_usual_views(care, events: list[dict] | None = None) -> list[ProposedUsualOut]:
    if care is None:
        return []
    groups: dict[tuple[str, str], list[tuple[object, object, str]]] = {}
    order: list[tuple[str, str]] = []
    for person, usual in pending_usuals(care):
        label = routine_word(
            classify_usual(
                usual,
                person,
                routine_by_summary=care.calendar_routine_by_summary,
            )
        )
        key = (person.person_id, label)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((person, usual, label))

    proposed: list[ProposedUsualOut] = []
    tz_label = calendar_tz_label()
    agenda = events or []
    for key in order[:6]:
        rows = groups[key]
        person, first, label = rows[0]
        usual_ids = [usual.usual_id for _person, usual, _label in rows]
        slots: list[ProposedUsualSlotOut] = []
        seen: set[tuple[int, int]] = set()
        refined: list[tuple[object, int, int, int]] = []
        for _person, usual, _label in rows:
            weekday, start_minute, end_minute = usual_local_slot(
                usual, person, agenda, care=care
            )
            refined.append((usual, weekday, start_minute, end_minute))
        refined.sort(key=lambda row: (row[1], row[2]))
        for usual, weekday, start_minute, end_minute in refined:
            band = (weekday, start_minute // 60)
            if band in seen:
                continue
            seen.add(band)
            slots.append(
                ProposedUsualSlotOut(
                    usual_id=usual.usual_id,
                    weekday=weekday,
                    start_minute=start_minute,
                    end_minute=end_minute,
                    when_label=format_usual_slot(
                        weekday,
                        start_minute,
                        end_minute,
                        tz_label=tz_label,
                    ),
                )
            )
        first_slot = slots[0] if slots else None
        proposed.append(
            ProposedUsualOut(
                usual_id=first.usual_id,
                person_id=person.person_id,
                display_name=person.display_name,
                your_role=person.your_role,
                their_relation=person.their_relation,
                care_role_id=person.care_role_id,
                label=label,
                weekday=first_slot.weekday if first_slot else first.weekday,
                start_minute=first_slot.start_minute if first_slot else first.start_minute,
                end_minute=first_slot.end_minute if first_slot else first.end_minute,
                when_label=", ".join(slot.when_label for slot in slots),
                usual_ids=usual_ids,
                slots=slots,
            )
        )
    return proposed


def _usual_gap_views(care, agenda_events: list[dict]) -> list[UsualGapOut]:
    gaps_out: list[UsualGapOut] = []
    if care is None:
        return gaps_out
    today_local = _local_now().date()
    for gap in find_usual_gaps(
        care=care,
        events=agenda_events,
        on_dates=horizon_dates(start=today_local, days=7),
    ):
        gaps_out.append(
            UsualGapOut(
                usual_id=gap.usual_id,
                person_id=gap.person_id,
                display_name=gap.display_name,
                your_role=gap.your_role,
                their_relation=gap.their_relation,
                label=gap.label,
                on_date=gap.on_date.isoformat(),
                weekday=gap.weekday,
                start_minute=gap.start_minute,
                end_minute=gap.end_minute,
                banner=gap.banner(),
            )
        )
        if len(gaps_out) >= 6:
            break
    return gaps_out


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
        return
    try:
        await _bg_enrich_care(user_id, get_memory(), sync_store, force=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("care_enrich_after_warm_failed", user_id=user_id, error=str(exc))


def _week_needs_classify(week_events: list[dict], care) -> bool:
    """True when a week title still lacks a Gemini role or routine."""
    hints = dict(care.calendar_role_by_summary)
    routines = dict(care.calendar_routine_by_summary)
    for ev in week_events:
        title = str(ev.get("summary") or "")
        if not title:
            continue
        if resolve_event_care_role(title, role_by_summary=hints or None) is None:
            return True
        key = " ".join(title.strip().lower().split())
        if not normalize_routine(routines.get(key, "")):
            return True
    return False


async def _bg_classify_week_roles(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
) -> None:
    """Background: classify this week's titles into care roles + routines."""
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
        if not week_events or not _week_needs_classify(week_events, care):
            return
        classified = await classify_week_event_roles_ai(
            week_events=week_events,  # type: ignore[arg-type]
            profile=care,
            gemini=build_gemini_client(get_settings()),
        )
        if not classified.roles and not classified.routines:
            return

        def _merge_hints(current):
            return current.model_copy(
                update={
                    "calendar_role_by_summary": {
                        **dict(current.calendar_role_by_summary),
                        **classified.roles,
                    },
                    "calendar_routine_by_summary": {
                        **dict(current.calendar_routine_by_summary),
                        **classified.routines,
                    },
                    "version": int(current.version or 1) + 1,
                    "updated_at": _now_utc(),
                }
            )

        await apply_care(memory, user_id, _merge_hints)
        _logger.info(
            "week_roles_classified_bg",
            user_id=user_id,
            tagged=len(classified.roles),
            routines=len(classified.routines),
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
            # No public HTTPS watch (localhost): pull deltas now so a just-deleted
            # event is gone on this load. Prod with a live watch uses the cache;
            # Google push refreshes it via /v1/sources/google/webhook.
            state = await refresh_agenda_on_read(
                user_id=user_id, token=token, sync_store=sync_store
            )
            raw_today, raw_tomorrow, care = await asyncio.gather(
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
                memory.manifestos.get_care_profile(user_id=user_id),
            )
            if watch_is_live(state):
                background_tasks.add_task(
                    _warm_agenda_cache_bg, user_id, token, sync_store
                )
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
    agenda_events: list[dict] = []
    try:
        agenda_events = [
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
                    "status": ev.status,
                    "recurring_event_id": ev.recurring_event_id,
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
                            "status": ev.status,
                            "recurring_event_id": ev.recurring_event_id,
                        }
                        for ev in state.events.values()
                        if ev.summary
                    ]
            except Exception:  # noqa: BLE001
                pass

        if care is not None and agenda_events:
            next_care = merge_series_usuals(care, agenda_events)
            if next_care.version != care.version:
                background_tasks.add_task(
                    apply_series_usuals, memory, user_id, agenda_events
                )
                care = next_care

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
        elif agenda_events and usuals_infer_needed(
            stored_fingerprint=state.care_infer_fingerprint if state else None,
            events=agenda_events,
        ):
            # Calendar changed (or never stamped) — re-propose usuals in the background.
            background_tasks.add_task(
                _bg_enrich_care,
                user_id,
                memory,
                sync_store,
                force=False,
            )
        # Opt-in regex seed only (LEVEL_ALLOW_HEURISTIC_CARE=1).
        if (care is None or not care.roles) and agenda_events:
            care = await seed_care_from_agenda_fast(
                user_id=user_id,
                memory=memory,
                events=agenda_events,
            )
            invalidate_care_graph_cache(user_id)

        if care is not None:
            week_events = filter_events_for_local_week(agenda_events)
            needs_week_ai = _week_needs_classify(week_events, care)
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

    usual_gaps: list[UsualGapOut] = []
    try:
        if care is not None:
            usual_gaps = _usual_gap_views(care, agenda_events)
    except Exception:  # noqa: BLE001
        _logger.warning("usual_gaps_failed", user_id=user_id)

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
        needs_review=bool(snapshot.needs_review if snapshot else False),
        usual_gaps=usual_gaps,
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
    store: ProposalStore = Depends(get_proposal_store),
) -> DayCheckInResponse:
    """Friendly day check-in — same router as /v1/chat."""
    result = await run_chat_turn(
        user_id=user_id,
        message=payload.message,
        memory=memory,
        tokens=tokens,
        sync_store=sync_store,
        store=store,
        cue_store=cue_store,
        background_tasks=background_tasks,
    )
    school = list(result.school_proposals)
    if result.proposal is not None and result.proposal.kind.value == "school_send":
        school.append(result.proposal)
    return DayCheckInResponse(
        reply=result.reply,
        facts_added=result.facts_added,
        cues_added=result.cues_added,
        today=None,
        school_proposals=school,
    )


__all__ = ["router"]
