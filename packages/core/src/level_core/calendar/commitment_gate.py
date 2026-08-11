"""Commitment gate — parse schedule asks, challenge with calendar + profile."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from level_core.calendar.availability import (
    day_agenda,
    draft_search_day,
    draft_window,
    find_conflicts,
    find_free_slots_nearby,
    occurrence_windows,
)
from level_core.calendar.proposals import ProposalStore
from level_core.calendar.sync_state import CalendarSyncState, CalendarSyncStore
from level_core.config import Settings, get_settings
from level_core.errors import ModelUnavailable
from level_core.ingest.google_live import _parse_when, list_primary_events_window
from level_core.memory.base import MemoryBank
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.schemas.care import care_profile_snippet
from level_core.schemas.commitment import (
    CommitmentCitation,
    CommitmentKind,
    CommitmentProposal,
    EventDraft,
    ProposalStatus,
    Weekday,
)
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import FactType
from level_core.schemas.user import OAuthToken

_WEEKDAY_INDEX = {
    Weekday.MO: 0,
    Weekday.TU: 1,
    Weekday.WE: 2,
    Weekday.TH: 3,
    Weekday.FR: 4,
    Weekday.SA: 5,
    Weekday.SU: 6,
}

_ADD_HINT = re.compile(
    r"\b(add|schedule|put|book|block)\b.+\b("
    r"calendar|every|weekly|recurring|"
    r"mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|"
    r"fri(day)?|sat(urday)?|sun(day)?"
    r")\b",
    re.I,
)
_AVAIL_HINT = re.compile(
    r"\b("
    r"do i have time|am i free|when (else )?am i free|when can i|"
    r"free at|fit in|make (it|dinner|lunch)|have room|"
    r"does .+ work|can i (do|make|meet)|available"
    r")\b",
    re.I,
)


class _ParsedIntent(BaseModel):
    is_schedule_ask: bool = False
    kind: str = "availability"  # add | availability
    title: str = "Untitled"
    by_days: list[str] = Field(default_factory=list)
    local_date: str | None = None
    local_time: str = "18:00"
    duration_minutes: int = 60
    timezone: str = "America/Los_Angeles"
    notes: str = ""
    recurring: bool = False


class _AdviceOut(BaseModel):
    level_message: str
    recommended_action: str = "confirm"
    citation_fact_ids: list[str] = Field(default_factory=list)


class _ScheduleResolve(BaseModel):
    """One-shot: parse schedule intent + Level reply (avoids two Gemini round-trips)."""

    is_schedule_ask: bool = True
    kind: str = "availability"
    title: str = ""
    by_days: list[str] = Field(default_factory=list)
    local_date: str | None = None
    local_time: str = "18:00"
    duration_minutes: int = 90
    timezone: str = "America/Los_Angeles"
    notes: str = ""
    recurring: bool = False
    level_message: str = ""
    recommended_action: str = "confirm"
    citation_fact_ids: list[str] = Field(default_factory=list)


def looks_like_schedule_ask(text: str) -> bool:
    t = text.strip()
    if len(t) < 8:
        return False
    return bool(_ADD_HINT.search(t) or _AVAIL_HINT.search(t))


def _weekday_map(raw: list[str]) -> list[Weekday]:
    out: list[Weekday] = []
    for item in raw:
        key = (item or "").strip().upper()[:2]
        aliases = {
            "MO": Weekday.MO,
            "TU": Weekday.TU,
            "WE": Weekday.WE,
            "TH": Weekday.TH,
            "FR": Weekday.FR,
            "SA": Weekday.SA,
            "SU": Weekday.SU,
            "M": Weekday.MO,
            "T": Weekday.TU,
            "W": Weekday.WE,
            "F": Weekday.FR,
        }
        # Prefer full tokens
        full = {
            "MONDAY": Weekday.MO,
            "TUESDAY": Weekday.TU,
            "WEDNESDAY": Weekday.WE,
            "THURSDAY": Weekday.TH,
            "FRIDAY": Weekday.FR,
            "SATURDAY": Weekday.SA,
            "SUNDAY": Weekday.SU,
            "MON": Weekday.MO,
            "TUE": Weekday.TU,
            "TUES": Weekday.TU,
            "WED": Weekday.WE,
            "THU": Weekday.TH,
            "THUR": Weekday.TH,
            "THURS": Weekday.TH,
            "FRI": Weekday.FR,
            "SAT": Weekday.SA,
            "SUN": Weekday.SU,
        }
        token = (item or "").strip().upper()
        day = full.get(token) or aliases.get(key)
        if day and day not in out:
            out.append(day)
    return out


def _heuristic_title(text: str) -> str:
    """Offline fallback title only — prefer the model on the live path."""
    t = text.strip()
    m = re.search(
        r"\b([A-Z][a-z]+)\s+wants?\s+to\s+(?:get\s+|have\s+)?(dinner|lunch|coffee|drinks|brunch)\b",
        t,
    )
    if m:
        return f"{m.group(2).title()} with {m.group(1)}"
    m = re.search(
        r"\b(dinner|lunch|coffee|drinks|brunch)\s+with\s+([A-Za-z]+)\b",
        t,
        re.I,
    )
    if m:
        return f"{m.group(1).title()} with {m.group(2).title()}"
    m = re.search(
        r"\b(add|schedule|put|book)\s+(.+?)\s+(every|on|at|to my)\b",
        t,
        re.I,
    )
    if m:
        return m.group(2).strip()[:80]
    return (t[:60] + "…") if len(t) > 60 else (t or "Plan")


def _heuristic_time(text: str) -> str | None:
    """Offline fallback — only used when Gemini is unavailable."""
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    ampm = m.group(3).lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _normalize_parsed(
    parsed: _ParsedIntent,
    *,
    today: str,
    source_text: str,
) -> _ParsedIntent:
    """Light schema cleanup on model output — does not re-interpret the ask.

    Critical: never force availability onto ``today``; that ignored weekend /
    relative day preferences the model already encoded in ``by_days``.
    """
    if parsed.kind not in ("add", "availability"):
        parsed.kind = "availability"
    title = (parsed.title or "").strip()
    if not title or title.lower() in {"untitled", "plan", "event", "none"}:
        # Last resort label from raw text — model should normally supply this.
        title = _heuristic_title(source_text)
    parsed.title = title[:120]
    if not parsed.local_time or not re.match(r"^\d{2}:\d{2}$", parsed.local_time):
        parsed.local_time = _heuristic_time(source_text) or "18:30"
    if parsed.duration_minutes < 15:
        parsed.duration_minutes = 90
    # If the model set preferred weekdays, drop a conflicting pinned date whose
    # weekday is outside that set (structured conflict, not keyword matching).
    by_days = _weekday_map(parsed.by_days)
    if by_days and parsed.local_date:
        try:
            from datetime import date as _date

            y, m, d = (int(x) for x in parsed.local_date.split("-"))
            pinned = _date(y, m, d).weekday()
            allowed = {_WEEKDAY_INDEX[day] for day in by_days}
            if pinned not in allowed:
                parsed.local_date = None
        except ValueError:
            parsed.local_date = None
    # Do NOT set local_date = today for bare availability — leave null so
    # draft_window / by_days drive the search.
    _ = today  # kept for call-site clarity / future logging
    if not parsed.timezone:
        parsed.timezone = "America/Los_Angeles"
    parsed.is_schedule_ask = True
    return parsed


async def _parse_intent(
    text: str,
    *,
    gemini: GeminiClient,
    settings: Settings,
    now: datetime,
) -> _ParsedIntent | None:
    if not looks_like_schedule_ask(text):
        return None
    # Use Pacific as product default for caregivers in this demo.
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    today = local.strftime("%Y-%m-%d")
    weekday_name = local.strftime("%A")
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You parse caregiver schedule requests for Level holistically. "
                    "Infer day/time/title from meaning — do not keyword-match naively.\n"
                    "Output STRICT JSON matching the schema.\n"
                    "kind=add when they want something on the calendar; "
                    "kind=availability when they ask if/when they have time or "
                    "what time works best.\n"
                    "title: short human label from the ask "
                    "(e.g. 'Grandparents visit'), never Untitled/Plan.\n"
                    "by_days: MO,TU,WE,TH,FR,SA,SU for preferred weekdays. "
                    "Weekend / Sat / Sun → [\"SA\",\"SU\"] with local_date null. "
                    "Never set local_date to today's weekday date when they asked "
                    "for weekend or another relative day window.\n"
                    "local_date: YYYY-MM-DD only when they name a specific day "
                    "(today/tonight/Thursday/Aug 15). Otherwise null and use by_days.\n"
                    "local_time: 24h HH:MM. If they gave no clock time, choose a "
                    "sensible default for the activity (visits ~14:00, dinner ~18:30).\n"
                    "duration_minutes: visits ~120, dinner/lunch ~90, coffee ~60, "
                    "meetings ~30 unless they said otherwise."
                ),
                prompt=(
                    f"Today is {weekday_name}, {today} (America/Los_Angeles).\n"
                    "Infer the schedule intent from the user's words.\n\n"
                    f"User said: {text.strip()}\n\n"
                    "Examples of correct shape:\n"
                    '- "grandparents visit on the weekend, when is best?" → '
                    'availability, title="Grandparents visit", by_days=["SA","SU"], '
                    "local_date=null, local_time≈14:00, duration≈120\n"
                    '- "dinner with Diane tonight at 6:30?" → availability, '
                    f'title="Dinner with Diane", local_date={today}, '
                    "by_days=[], local_time=18:30, duration=90\n"
                ),
                response_schema=_ParsedIntent.model_json_schema(),
                temperature=0.1,
                max_output_tokens=400,
            )
        )
        data = json.loads(resp.text)
        parsed = _ParsedIntent.model_validate(data)
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        # Offline fallback only — live path trusts Gemini.
        kind = "add" if _ADD_HINT.search(text) else "availability"
        parsed = _ParsedIntent(
            is_schedule_ask=True,
            kind=kind,
            title=_heuristic_title(text),
            local_date=None,
            by_days=[],
            local_time=_heuristic_time(text) or "18:30",
            duration_minutes=90,
            timezone="America/Los_Angeles",
        )
    except Exception:  # noqa: BLE001
        return None

    if not parsed.is_schedule_ask and not looks_like_schedule_ask(text):
        return None
    return _normalize_parsed(parsed, today=today, source_text=text)


def _draft_from_parsed(parsed: _ParsedIntent) -> EventDraft:
    by_days = _weekday_map(parsed.by_days)
    # Availability with preferred weekdays should still use by_days for
    # windowing — without forcing a recurring series.
    recurring = bool(parsed.recurring) or (
        parsed.kind == "add" and len(by_days) >= 1 and not parsed.local_date
    )
    title = (parsed.title or "").strip()[:120] or "Plan"
    return EventDraft(
        title=title,
        by_days=by_days,
        local_date=parsed.local_date,
        local_time=parsed.local_time if re.match(r"^\d{2}:\d{2}$", parsed.local_time) else "18:00",
        duration_minutes=max(15, min(parsed.duration_minutes, 24 * 60)),
        timezone=parsed.timezone or "America/Los_Angeles",
        notes=(parsed.notes or "")[:400],
        recurring=recurring,
    )


def _summarize_draft(kind: CommitmentKind, draft: EventDraft) -> str:
    time_label = draft.local_time
    try:
        h, m = (int(x) for x in draft.local_time.split(":"))
        ampm = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        time_label = f"{h12}:{m:02d}{ampm}"
    except ValueError:
        pass
    if draft.recurring and draft.by_days:
        days = "/".join(d.value for d in draft.by_days)
        base = f"{draft.title} every {days} at {time_label}"
    elif draft.local_date:
        base = f"{draft.title} on {draft.local_date} at {time_label}"
    else:
        base = f"{draft.title} at {time_label}"
    if kind is CommitmentKind.AVAILABILITY:
        return f"Check time for {base} ({draft.duration_minutes}m)"
    return f"Add {base} ({draft.duration_minutes}m)"


async def _profile_context(
    memory: MemoryBank, user_id: str
) -> tuple[str, dict[str, str], str | None]:
    """Return (prompt block, fact_id → statement, care_snippet) for grounding."""
    # Cap reads — full history was making schedule proposes feel slow.
    facts = await memory.facts.list_for_user(user_id=user_id, limit=48)
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    care_snip = care_profile_snippet(care) or None
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)

    priority_types = {
        FactType.COMMITMENT,
        FactType.CONSTRAINT,
        FactType.VALUE_STATEMENT,
        FactType.RELATIONSHIP,
        FactType.CONCERN,
        FactType.PREFERENCE,
    }
    fact_map: dict[str, str] = {}
    cite_lines: list[str] = []
    context_lines: list[str] = []
    for fact in facts:
        if fact.type not in priority_types:
            continue
        fact_map[fact.fact_id] = fact.statement
        cite_lines.append(f"- [{fact.fact_id}] ({fact.type.value}) {fact.statement}")
        if len(cite_lines) >= 12:
            break

    if care_snip:
        context_lines.append(care_snip)
    if snapshot:
        for b in snapshot.bullets:
            if b.status is BulletStatus.REJECTED:
                continue
            label = b.care_role_id or b.category.value
            context_lines.append(f"- ({label}) {b.text}")
            if len(context_lines) >= 10:
                break
        for c in snapshot.contradictions[:4]:
            if c.status is BulletStatus.REJECTED:
                continue
            context_lines.append(f"- (tension) {c.summary}")

    block = "Citable facts (ONLY these bracket ids may be cited):\n"
    block += "\n".join(cite_lines) if cite_lines else "(none)"
    block += "\n\nCare roles + background (do not invent ids; paraphrase if useful):\n"
    block += "\n".join(context_lines) if context_lines else "(none)"
    return block, fact_map, care_snip


_ID_LEAK = re.compile(
    r"\s*\[(?:bullet:)?[0-9a-f]{8,}[0-9a-f\-]*\]|\s*\[bullet:[^\]]+\]|\s*\[fact_[^\]]+\]",
    re.I,
)


def _sanitize_message(text: str) -> str:
    """Strip leaked ids only — never truncate the model's reply."""
    cleaned = _ID_LEAK.sub("", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _compact_agenda(
    events: list[dict[str, Any]],
    *,
    timezone_name: str = "America/Los_Angeles",
    limit: int = 40,
) -> str:
    lines: list[str] = []
    for event in events[:limit]:
        start_dt, _end = None, None
        try:
            from level_core.calendar.availability import _event_bounds

            start_dt, _end = _event_bounds(event, timezone_name=timezone_name)
        except Exception:  # noqa: BLE001
            continue
        if start_dt is None:
            continue
        local = start_dt.astimezone(ZoneInfo(timezone_name))
        summary = (event.get("summary") or "(no title)").strip()
        lines.append(
            f"- {local.strftime('%a')} {local.strftime('%b')} {local.day} "
            f"{local.strftime('%I:%M%p').lstrip('0')} · {summary}"
        )
    return "\n".join(lines) if lines else "(no events in range)"


async def _resolve_schedule(
    *,
    user_text: str,
    today: str,
    weekday_name: str,
    agenda_block: str,
    profile_block: str,
    gemini: GeminiClient,
    settings: Settings,
) -> _ScheduleResolve | None:
    """Single Gemini call: intent fields + full Level reply."""
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You are Level — a warm decision partner for a busy caregiver. "
                    "Parse the schedule ask holistically AND answer in one JSON object.\n"
                    "Fields:\n"
                    "- is_schedule_ask, kind (add|availability), title, by_days "
                    "(MO..SU), local_date (YYYY-MM-DD or null), local_time (HH:MM), "
                    "duration_minutes, recurring, timezone, notes\n"
                    "- level_message: complete reply the user will read — at most "
                    "3 short sentences, finished thoughts only. Do not trail off. "
                    "Name care collisions when relevant. Suggest times only from "
                    "the calendar context when possible.\n"
                    "- recommended_action: confirm|revise|decline\n"
                    "- citation_fact_ids: only ids from Citable facts\n\n"
                    "Rules: weekend asks → by_days SA,SU and local_date null. "
                    "Never pin weekend asks to today's weekday date. "
                    "Never invent free times that contradict the agenda. "
                    "No flattery, no lectures, no raw [bullet:...] ids."
                ),
                prompt=(
                    f"Today is {weekday_name}, {today} (America/Los_Angeles).\n\n"
                    f"User said: {user_text.strip()}\n\n"
                    f"Upcoming calendar (context):\n{agenda_block}\n\n"
                    f"{profile_block}\n"
                ),
                response_schema=_ScheduleResolve.model_json_schema(),
                temperature=0.2,
                max_output_tokens=700,
            )
        )
        return _ScheduleResolve.model_validate(json.loads(resp.text))
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        return None
    except Exception:  # noqa: BLE001
        return None


async def _advise(
    *,
    kind: CommitmentKind,
    user_text: str,
    draft: EventDraft,
    conflicts: list[Any],
    free_slots: list[Any],
    agenda_lines: list[str],
    profile_block: str,
    fact_map: dict[str, str],
    gemini: GeminiClient,
    settings: Settings,
) -> _AdviceOut:
    """Legacy second-pass advice (kept for tests / fallback)."""
    conflict_lines = "\n".join(f"- {c.label}" for c in conflicts) or "(none — calendar is clear in this window)"
    slot_lines = "\n".join(f"- {s.label}" for s in free_slots) or "(none in the next few evenings)"
    agenda = "\n".join(f"- {a}" for a in agenda_lines) or "(nothing else on that day)"
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You are Level — a warm-but-honest decision partner for a busy caregiver. "
                    "You are NOT a yes-man calendar bot. Your lens is ROLE THEFT across care roles "
                    "(child care, elder care, paid work, self & recovery, logistics).\n\n"
                    "HARD RULES:\n"
                    "1) A HARD CONFLICT is ONLY something listed under 'Conflicts in the proposed window'. "
                    "Same-day events earlier/later that do NOT overlap are NOT conflicts — "
                    "never say dinner 'follows' or 'cuts into' an 11am block when asking about 7pm.\n"
                    "2) All-day events that appear under Conflicts DO block the evening — say that clearly.\n"
                    "3) Alternative times MUST be chosen only from 'Suggested free slots'. "
                    "If that list is empty, ask them to pick another day — do NOT invent Sunday morning coffee.\n"
                    "4) Never write raw ids, bullet ids, or brackets like [bullet:...] in level_message.\n"
                    "5) citation_fact_ids may only include ids from 'Citable facts'.\n"
                    "6) level_message: at most 3 short complete sentences. Never truncate mid-thought.\n"
                    "7) When a conflict or care role is at risk, name the CARE ROLE and sticky window "
                    "(e.g. child care pickup) — say what saying yes would crowd out."
                ),
                prompt=(
                    f"User asked ({kind.value}): {user_text.strip()}\n"
                    f"Understood as: {_summarize_draft(kind, draft)}\n\n"
                    f"Conflicts in the proposed window:\n{conflict_lines}\n\n"
                    f"Other events that same local day (NOT conflicts unless also listed above):\n{agenda}\n\n"
                    f"Suggested free slots (only suggest from these):\n{slot_lines}\n\n"
                    f"{profile_block}\n\n"
                    "If there is a hard conflict → recommended_action=revise, name the care collision if a care "
                    "role/window is involved, and point at a listed free slot.\n"
                    "If no hard conflict → recommended_action=confirm; you may note a soft squeeze "
                    "against care roles the user marked Keep, without blocking.\n"
                    "Return JSON: level_message, recommended_action "
                    "(confirm|revise|decline), citation_fact_ids."
                ),
                response_schema=_AdviceOut.model_json_schema(),
                temperature=0.25,
                max_output_tokens=500,
            )
        )
        advice = _AdviceOut.model_validate(json.loads(resp.text))
    except Exception:  # noqa: BLE001
        if conflicts:
            alt = free_slots[0].label if free_slots else "another evening this week"
            msg = (
                f"That overlaps {conflicts[0].label}. "
                f"How about {alt} instead?"
            )
            action = "revise"
        else:
            msg = (
                "You're clear on the calendar for that window."
            )
            action = "confirm"
        advice = _AdviceOut(level_message=msg, recommended_action=action)

    advice.level_message = _sanitize_message(advice.level_message)
    valid_ids = [fid for fid in advice.citation_fact_ids if fid in fact_map]
    advice.citation_fact_ids = valid_ids
    if advice.recommended_action not in ("confirm", "revise", "decline"):
        advice.recommended_action = "revise" if conflicts else "confirm"
    if conflicts and advice.recommended_action == "confirm":
        advice.recommended_action = "revise"
    return advice


def _cached_event_to_gcal(ev: Any) -> dict[str, Any] | None:
    """Shape agenda-cache rows like Google API events for conflict helpers."""
    summary = getattr(ev, "summary", None) or "(no title)"
    start = getattr(ev, "start", None)
    end = getattr(ev, "end", None)
    all_day = bool(getattr(ev, "all_day", False))
    if not start:
        return None
    if all_day or (isinstance(start, str) and "T" not in start and len(start) >= 10):
        day = start[:10]
        end_day = (end[:10] if isinstance(end, str) and end else day)
        return {
            "summary": summary,
            "start": {"date": day},
            "end": {"date": end_day},
        }
    return {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end or start},
    }


def _events_from_agenda_cache(
    state: CalendarSyncState | None,
    *,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    if state is None or not state.events:
        return []
    out: list[dict[str, Any]] = []
    for ev in state.events.values():
        if (ev.status or "").lower() == "cancelled":
            continue
        when = _parse_when(ev.start)
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if time_min <= when <= time_max:
            shaped = _cached_event_to_gcal(ev)
            if shaped:
                out.append(shaped)
    return out


async def _load_events_for_window(
    *,
    token: OAuthToken,
    time_min: datetime,
    time_max: datetime,
    sync_store: CalendarSyncStore | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Prefer fresh agenda cache; fall back to a live Google window pull."""
    if sync_store is not None and user_id:
        try:
            state = await sync_store.get(user_id)
            if state and state.agenda_updated_at:
                updated = state.agenda_updated_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = (datetime.now(tz=timezone.utc) - updated).total_seconds()
                if age <= 6 * 3600:
                    cached = _events_from_agenda_cache(
                        state, time_min=time_min, time_max=time_max
                    )
                    if cached:
                        return cached
        except Exception:  # noqa: BLE001
            pass
    return await list_primary_events_window(
        token, time_min=time_min, time_max=time_max
    )


async def propose_from_text(
    *,
    user_id: str,
    user_text: str,
    token: OAuthToken,
    memory: MemoryBank,
    store: ProposalStore,
    gemini: GeminiClient | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    sync_store: CalendarSyncStore | None = None,
) -> CommitmentProposal | None:
    """If ``user_text`` is a schedule ask, return a persisted proposal; else None.

    One Gemini call (intent + full reply) after a narrow calendar/profile fetch.
    """
    settings = settings or get_settings()
    gemini = gemini or build_gemini_client(settings)
    now = now or datetime.now(tz=timezone.utc)

    if not looks_like_schedule_ask(user_text):
        return None

    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    today = local.strftime("%Y-%m-%d")
    weekday_name = local.strftime("%A")

    # Prefetch ~10 local days so weekend asks have context without a prior parse.
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    scan_start = day0.astimezone(timezone.utc) - timedelta(hours=1)
    scan_end = (day0 + timedelta(days=10)).astimezone(timezone.utc)

    events, (profile_block, fact_map, _care_snip) = await asyncio.gather(
        _load_events_for_window(
            token=token,
            time_min=scan_start,
            time_max=scan_end,
            sync_store=sync_store,
            user_id=user_id,
        ),
        _profile_context(memory, user_id),
    )
    agenda_block = _compact_agenda(events, timezone_name="America/Los_Angeles")

    resolved = await _resolve_schedule(
        user_text=user_text,
        today=today,
        weekday_name=weekday_name,
        agenda_block=agenda_block,
        profile_block=profile_block,
        gemini=gemini,
        settings=settings,
    )

    level_message = ""
    recommended = "confirm"
    cite_ids: list[str] = []

    if resolved is None:
        parsed = await _parse_intent(
            user_text, gemini=gemini, settings=settings, now=now
        )
        if parsed is None:
            return None
        kind = (
            CommitmentKind.ADD if parsed.kind == "add" else CommitmentKind.AVAILABILITY
        )
        draft = _draft_from_parsed(parsed)
    else:
        if not resolved.is_schedule_ask:
            return None
        parsed = _normalize_parsed(
            _ParsedIntent(
                is_schedule_ask=True,
                kind=resolved.kind,
                title=resolved.title,
                by_days=resolved.by_days,
                local_date=resolved.local_date,
                local_time=resolved.local_time,
                duration_minutes=resolved.duration_minutes,
                timezone=resolved.timezone or "America/Los_Angeles",
                notes=resolved.notes,
                recurring=resolved.recurring,
            ),
            today=today,
            source_text=user_text,
        )
        kind = (
            CommitmentKind.ADD if parsed.kind == "add" else CommitmentKind.AVAILABILITY
        )
        draft = _draft_from_parsed(parsed)
        level_message = _sanitize_message(resolved.level_message)
        recommended = resolved.recommended_action
        cite_ids = list(resolved.citation_fact_ids)

    windows = occurrence_windows(draft, now=now, weeks=1)
    day_start, day_end = draft_search_day(draft, now=now)
    if not windows:
        windows = [draft_window(draft, now=now)]

    conflicts: list[Any] = []
    for w0, w1 in windows[:4]:
        conflicts.extend(
            find_conflicts(
                events,
                window_start=w0,
                window_end=w1,
                timezone_name=draft.timezone,
            )
        )
    seen: set[str] = set()
    uniq = []
    for c in conflicts:
        key = f"{c.summary}|{c.start}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    conflicts = uniq[:6]

    window_start, _ = windows[0]
    preferred = (
        {_WEEKDAY_INDEX[d] for d in draft.by_days} if draft.by_days else None
    )
    free_slots = find_free_slots_nearby(
        events,
        anchor=window_start,
        duration=timedelta(minutes=draft.duration_minutes),
        timezone_name=draft.timezone,
        days=2 if not draft.by_days else 4,
        max_slots=3,
        preferred_weekdays=preferred,
    )

    if not level_message:
        advice = await _advise(
            kind=kind,
            user_text=user_text,
            draft=draft,
            conflicts=conflicts,
            free_slots=free_slots,
            agenda_lines=day_agenda(
                events,
                day_start=day_start,
                day_end=day_end,
                timezone_name=draft.timezone,
            ),
            profile_block=profile_block,
            fact_map=fact_map,
            gemini=gemini,
            settings=settings,
        )
        level_message = advice.level_message
        recommended = advice.recommended_action
        cite_ids = advice.citation_fact_ids

    if recommended not in ("confirm", "revise", "decline"):
        recommended = "revise" if conflicts else "confirm"
    if conflicts and recommended == "confirm":
        recommended = "revise"

    citations = [
        CommitmentCitation(fact_id=fid, quote=fact_map[fid][:220])
        for fid in cite_ids
        if fid in fact_map
    ]

    proposal = CommitmentProposal(
        user_id=user_id,
        kind=kind,
        status=ProposalStatus.PENDING,
        user_text=user_text.strip(),
        draft=draft,
        summary=_summarize_draft(kind, draft),
        level_message=level_message,
        conflicts=conflicts,
        free_slots=free_slots,
        citations=citations,
        recommended_action=recommended,
        written_by="commitment_gate@v1",
    )
    await store.save(proposal)
    return proposal



def apply_draft_to_window(
    draft: EventDraft,
    *,
    slot_start_iso: str | None = None,
) -> tuple[datetime, datetime]:
    """Resolve the window to write (optional override from a chosen free slot)."""
    if slot_start_iso:
        start = datetime.fromisoformat(slot_start_iso.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = start + timedelta(minutes=draft.duration_minutes)
        return start, end
    return draft_window(draft)


__all__ = [
    "apply_draft_to_window",
    "looks_like_schedule_ask",
    "propose_from_text",
]
