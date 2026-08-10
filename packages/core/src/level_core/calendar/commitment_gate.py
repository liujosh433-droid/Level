"""Commitment gate — parse schedule asks, challenge with calendar + profile."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

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
from level_core.config import Settings, get_settings
from level_core.errors import ModelUnavailable
from level_core.ingest.google_live import list_primary_events_window
from level_core.memory.base import MemoryBank
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.models.factory import build_gemini_client
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
    t = text.strip()
    # "Diane wants to get dinner..." / "dinner with Diane"
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
    for word in ("dinner", "lunch", "coffee", "swim", "swimming", "pottery"):
        if re.search(rf"\b{word}\b", t, re.I):
            return word.title() if word != "swim" else "Swimming"
    return "Plan"


def _heuristic_time(text: str) -> str | None:
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
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You parse caregiver schedule requests for Level. "
                    "Output STRICT JSON matching the schema. "
                    "kind=add when they want something put on the calendar. "
                    "kind=availability when they ask if they have time / when they're free. "
                    "title must be a short human label (e.g. 'Dinner with Diane'), never 'Untitled'. "
                    "by_days uses MO,TU,WE,TH,FR,SA,SU. "
                    "local_time is 24h HH:MM. local_date is YYYY-MM-DD or null. "
                    "For 'today'/'tonight' use the provided today date. "
                    "Dinner/lunch default duration 90; coffee 60; workouts 60; meetings 30 unless said."
                ),
                prompt=(
                    f"Today's local date (America/Los_Angeles): {today}\n"
                    f"Default timezone: America/Los_Angeles\n"
                    f"User said: {text.strip()}"
                ),
                response_schema=_ParsedIntent.model_json_schema(),
                temperature=0.1,
                max_output_tokens=400,
            )
        )
        data = json.loads(resp.text)
        parsed = _ParsedIntent.model_validate(data)
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        kind = "add" if _ADD_HINT.search(text) else "availability"
        parsed = _ParsedIntent(
            is_schedule_ask=True,
            kind=kind,
            title=_heuristic_title(text),
            local_date=today if kind == "availability" else None,
            local_time=_heuristic_time(text) or "18:30",
            duration_minutes=90,
            timezone="America/Los_Angeles",
        )
    except Exception:  # noqa: BLE001
        return None

    if not parsed.is_schedule_ask and not looks_like_schedule_ask(text):
        return None
    parsed.is_schedule_ask = True
    if parsed.kind not in ("add", "availability"):
        parsed.kind = "add" if _ADD_HINT.search(text) else "availability"
    if not parsed.title or parsed.title.strip().lower() in {"untitled", "plan", "event", "none"}:
        parsed.title = _heuristic_title(text)
    if not parsed.local_time or not re.match(r"^\d{2}:\d{2}$", parsed.local_time):
        parsed.local_time = _heuristic_time(text) or "18:30"
    if parsed.kind == "availability" and not parsed.local_date:
        parsed.local_date = today
    if not parsed.timezone:
        parsed.timezone = "America/Los_Angeles"
    return parsed


def _draft_from_parsed(parsed: _ParsedIntent) -> EventDraft:
    by_days = _weekday_map(parsed.by_days)
    recurring = bool(parsed.recurring or (len(by_days) >= 1 and parsed.kind == "add"))
    title = (parsed.title or "").strip()[:120] or _heuristic_title(parsed.notes or "Plan")
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


async def _profile_context(memory: MemoryBank, user_id: str) -> tuple[str, dict[str, str]]:
    """Return (prompt block, fact_id → statement) for grounding."""
    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)

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
        if len(cite_lines) >= 24:
            break

    if snapshot:
        for b in snapshot.bullets:
            if b.status is BulletStatus.REJECTED:
                continue
            # Bullets are context only — never citeable ids (avoids [bullet:...] in replies).
            context_lines.append(f"- ({b.category.value}) {b.text}")
            if len(context_lines) >= 12:
                break
        for c in snapshot.contradictions[:6]:
            if c.status is BulletStatus.REJECTED:
                continue
            context_lines.append(f"- (tension) {c.summary}")

    if manifesto and manifesto.statement:
        context_lines.append(f"- (manifesto) {manifesto.statement[:400]}")

    block = "Citable facts (ONLY these bracket ids may be cited):\n"
    block += "\n".join(cite_lines) if cite_lines else "(none)"
    block += "\n\nBackground profile (do not invent ids; paraphrase if useful):\n"
    block += "\n".join(context_lines) if context_lines else "(none)"
    return block, fact_map


_ID_LEAK = re.compile(
    r"\s*\[(?:bullet:)?[0-9a-f]{8,}[0-9a-f\-]*\]|\s*\[bullet:[^\]]+\]|\s*\[fact_[^\]]+\]",
    re.I,
)


def _sanitize_message(text: str) -> str:
    cleaned = _ID_LEAK.sub("", text or "")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:800]


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
    conflict_lines = "\n".join(f"- {c.label}" for c in conflicts) or "(none — calendar is clear in this window)"
    slot_lines = "\n".join(f"- {s.label}" for s in free_slots) or "(none in the next few evenings)"
    agenda = "\n".join(f"- {a}" for a in agenda_lines) or "(nothing else on that day)"
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.reasoning_model,
                system_instruction=(
                    "You are Level — a warm-but-honest decision partner for a busy caregiver. "
                    "You are NOT a yes-man calendar bot.\n\n"
                    "HARD RULES:\n"
                    "1) A HARD CONFLICT is ONLY something listed under 'Conflicts in the proposed window'. "
                    "Same-day events earlier/later that do NOT overlap are NOT conflicts — "
                    "never say dinner 'follows' or 'cuts into' an 11am block when asking about 7pm.\n"
                    "2) All-day events that appear under Conflicts DO block the evening — say that clearly.\n"
                    "3) Alternative times MUST be chosen only from 'Suggested free slots'. "
                    "If that list is empty, ask them to pick another day — do NOT invent Sunday morning coffee.\n"
                    "4) Never write raw ids, bullet ids, or brackets like [bullet:...] in level_message.\n"
                    "5) citation_fact_ids may only include ids from 'Citable facts'.\n"
                    "6) 1-3 short sentences. No flattery. No lectures."
                ),
                prompt=(
                    f"User asked ({kind.value}): {user_text.strip()}\n"
                    f"Understood as: {_summarize_draft(kind, draft)}\n\n"
                    f"Conflicts in the proposed window:\n{conflict_lines}\n\n"
                    f"Other events that same local day (NOT conflicts unless also listed above):\n{agenda}\n\n"
                    f"Suggested free slots (only suggest from these):\n{slot_lines}\n\n"
                    f"{profile_block}\n\n"
                    "If there is a hard conflict → recommended_action=revise and point at a listed free slot.\n"
                    "If no hard conflict → recommended_action=confirm; you may briefly note other same-day "
                    "events as context, and optionally a soft profile caution.\n"
                    "Return JSON: level_message, recommended_action "
                    "(confirm|revise|decline), citation_fact_ids."
                ),
                response_schema=_AdviceOut.model_json_schema(),
                temperature=0.25,
                max_output_tokens=350,
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
    # If model ignored a hard conflict, force revise.
    if conflicts and advice.recommended_action == "confirm":
        advice.recommended_action = "revise"
    return advice


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
) -> CommitmentProposal | None:
    """If ``user_text`` is a schedule ask, return a persisted proposal; else None."""
    settings = settings or get_settings()
    gemini = gemini or build_gemini_client(settings)
    now = now or datetime.now(tz=timezone.utc)

    parsed = await _parse_intent(user_text, gemini=gemini, settings=settings, now=now)
    if parsed is None:
        return None

    kind = CommitmentKind.ADD if parsed.kind == "add" else CommitmentKind.AVAILABILITY
    draft = _draft_from_parsed(parsed)

    # Pull calendar window covering the ask day + a few days of alternatives.
    windows = occurrence_windows(draft, now=now, weeks=2)
    day_start, day_end = draft_search_day(draft, now=now)
    scan_start = min(min(w[0] for w in windows), day_start) - timedelta(hours=1)
    scan_end = max(max(w[1] for w in windows), day_end) + timedelta(days=4)

    events = await list_primary_events_window(
        token, time_min=scan_start, time_max=scan_end
    )

    conflicts: list[Any] = []
    for w0, w1 in windows[:6]:
        conflicts.extend(
            find_conflicts(
                events,
                window_start=w0,
                window_end=w1,
                timezone_name=draft.timezone,
            )
        )
    # Dedupe by summary+start
    seen: set[str] = set()
    uniq = []
    for c in conflicts:
        key = f"{c.summary}|{c.start}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    conflicts = uniq[:8]

    window_start, _ = windows[0]
    agenda_lines = day_agenda(
        events,
        day_start=day_start,
        day_end=day_end,
        timezone_name=draft.timezone,
    )
    free_slots = find_free_slots_nearby(
        events,
        anchor=window_start,
        duration=timedelta(minutes=draft.duration_minutes),
        timezone_name=draft.timezone,
        days=4,
        max_slots=4,
    )

    profile_block, fact_map = await _profile_context(memory, user_id)
    advice = await _advise(
        kind=kind,
        user_text=user_text,
        draft=draft,
        conflicts=conflicts,
        free_slots=free_slots,
        agenda_lines=agenda_lines,
        profile_block=profile_block,
        fact_map=fact_map,
        gemini=gemini,
        settings=settings,
    )

    citations = [
        CommitmentCitation(fact_id=fid, quote=fact_map[fid][:220])
        for fid in advice.citation_fact_ids
        if fid in fact_map
    ]

    proposal = CommitmentProposal(
        user_id=user_id,
        kind=kind,
        status=ProposalStatus.PENDING,
        user_text=user_text.strip(),
        draft=draft,
        summary=_summarize_draft(kind, draft),
        level_message=advice.level_message.strip()[:800],
        conflicts=conflicts,
        free_slots=free_slots,
        citations=citations,
        recommended_action=advice.recommended_action,
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
