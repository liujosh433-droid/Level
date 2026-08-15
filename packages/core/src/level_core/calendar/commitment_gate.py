"""Commitment gate — parse schedule asks, challenge with calendar + profile."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from level_core.calendar.availability import (
    _event_bounds,
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
from level_core.schemas.care import (
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
    active_care_roles,
    care_profile_snippet,
)
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

# Offline / model-down ONLY — never used to decide intent when Gemini is up.
_OFFLINE_SCHEDULE_HINT = re.compile(
    r"\b("
    r"calendar|schedule|book|add .+\bto my|"
    r"am i free|do i have time|what time|when can i|fit in|available"
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
    duration_named: bool = False
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
    local_time: str = "13:00"
    duration_minutes: int = 60
    duration_named: bool = False
    timezone: str = "America/Los_Angeles"
    notes: str = ""
    recurring: bool = False
    level_message: str = ""
    recommended_action: str = "confirm"
    citation_fact_ids: list[str] = Field(default_factory=list)


def looks_like_schedule_ask(text: str) -> bool:
    """Offline fallback gate only. Live path trusts Gemini ``is_schedule_ask``."""
    t = text.strip()
    if len(t) < 8:
        return False
    return bool(_OFFLINE_SCHEDULE_HINT.search(t))


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


def _offline_title(text: str) -> str:
    """Last-resort label when the model is down — never dump the user question."""
    t = (text or "").strip()
    if not t or _title_echoes_ask(t, t):
        return "Hold"
    if len(t) > 48:
        return t[:48].rsplit(" ", 1)[0] or "Hold"
    return t


def _title_echoes_ask(title: str, source_text: str) -> bool:
    """True when the model copied the user's question as the event title."""
    t = (title or "").strip().lower()
    src = (source_text or "").strip().lower()
    if not t:
        return True
    if len(t) > 48:
        return True
    if "?" in t:
        return True
    if t.startswith(("i need", "i want", "can you", "which day", "what time", "when can")):
        return True
    if src and (t in src or src.startswith(t[:24])):
        # Short named events ("soccer") can appear in the ask — only treat as echo
        # when the title is sentence-like.
        return len(t.split()) >= 6
    return False


def _hold_title(duration_minutes: int) -> str:
    if duration_minutes == 60:
        return "1-hour hold"
    if duration_minutes % 60 == 0:
        hours = duration_minutes // 60
        return f"{hours}-hour hold"
    return f"{duration_minutes}-minute hold"


def _human_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        dt = datetime(y, m, d)
        return f"{dt.strftime('%b')} {dt.day}"
    except ValueError:
        return iso


def _normalize_parsed(
    parsed: _ParsedIntent,
    *,
    today: str,
    source_text: str,
) -> _ParsedIntent:
    """Schema cleanup only — does not re-interpret the ask with keywords.

    Day/time/duration/title come from the model. We only fix invalid shapes
    and drop a ``local_date`` that contradicts model ``by_days``.
    """
    _ = today  # call-site clarity; day pinning is the model's job
    if parsed.kind not in ("add", "availability"):
        parsed.kind = "availability"
    title = (parsed.title or "").strip()
    if (
        not title
        or title.lower() in {"untitled", "plan", "event", "none"}
        or _title_echoes_ask(title, source_text)
    ):
        title = _hold_title(parsed.duration_minutes or 60)
    parsed.title = title[:48]
    if not parsed.local_time or not re.match(r"^\d{2}:\d{2}$", parsed.local_time):
        parsed.local_time = "13:00"
    if not parsed.duration_named or parsed.duration_minutes < 15:
        parsed.duration_minutes = 60
    parsed.duration_minutes = max(15, min(int(parsed.duration_minutes), 24 * 60))
    # Structured conflict: pinned date weekday outside model by_days → drop date.
    by_days = _weekday_map(parsed.by_days)
    if by_days and parsed.local_date:
        try:
            y, m, d = (int(x) for x in parsed.local_date.split("-"))
            pinned = date(y, m, d).weekday()
            allowed = {_WEEKDAY_INDEX[day] for day in by_days}
            if pinned not in allowed:
                parsed.local_date = None
        except ValueError:
            parsed.local_date = None
    if not parsed.timezone:
        parsed.timezone = "America/Los_Angeles"
    parsed.is_schedule_ask = True
    return parsed


def _role_who(role: CareRoleState) -> str:
    if role.people:
        return f"{role.label.lower()} for {', '.join(role.people[:2])}"
    return role.label.lower()


def _window_phrase(role: CareRoleState, window: ProtectedWindow) -> str:
    who = _role_who(role)
    lab = " ".join((window.label or "").split())
    low = lab.lower()
    if lab and not any(tok in low for tok in ("salience", "care block", "~")):
        return lab if who.split()[-1].lower() in low else f"{lab} — {who}"
    return who


def _hours_overlap(slot_start: datetime, slot_end: datetime, start_hour: int, end_hour: int) -> bool:
    h0 = slot_start.hour + slot_start.minute / 60.0
    h1 = slot_end.hour + slot_end.minute / 60.0
    if h1 <= h0:
        h1 += 24
    w1 = float(end_hour if end_hour > start_hour else start_hour + 1)
    return h0 < w1 and h1 > float(start_hour)


def _care_lens_for_slot(
    care: CareProfile | None,
    slot: Any,
    *,
    timezone_name: str,
) -> tuple[str | None, str | None]:
    """Return (crowds_clause, clear_clause) for one free slot vs Care Profile windows."""
    if care is None:
        return None, None
    try:
        start = datetime.fromisoformat(str(slot.start).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(slot.end).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(timezone_name)
    local_s, local_e = start.astimezone(tz), end.astimezone(tz)

    crowds: list[tuple[float, str]] = []
    clears: list[tuple[float, str]] = []
    for role in active_care_roles(care):
        if role.role_id is CareRoleId.PAID_WORK:
            continue
        for window in role.protected_windows[:4]:
            if window.weekday is not None and window.weekday != local_s.weekday():
                continue
            if window.start_hour is None:
                continue
            end_h = window.end_hour if window.end_hour is not None else window.start_hour + 1
            phrase = _window_phrase(role, window)
            if _hours_overlap(local_s, local_e, window.start_hour, end_h):
                crowds.append((role.salience, phrase))
            else:
                clears.append((role.salience, phrase))
        if (
            not role.protected_windows
            and role.people
            and role.role_id
            in {CareRoleId.CHILD_CARE, CareRoleId.ELDER_CARE, CareRoleId.PARTNER_COPARENT}
        ):
            clears.append((role.salience, _role_who(role)))

    crowds.sort(key=lambda x: -x[0])
    clears.sort(key=lambda x: -x[0])
    crowd_bit = f"that sits on {crowds[0][1]}" if crowds else None
    clear_bit = None
    if not crowds and clears:
        clear_bit = f"Clear of {clears[0][1]}"
    return crowd_bit, clear_bit


def _ground_availability_reply(
    *,
    title: str,
    free_slots: list[Any],
    conflicts: list[Any],
    care: CareProfile | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> str:
    """Name real free slots and what they protect or crowd in the Care Profile."""
    del title
    best = free_slots[0]
    crowd, clear = _care_lens_for_slot(care, best, timezone_name=timezone_name)
    if conflicts:
        msg = f"That overlaps {conflicts[0].label}. Soonest opening: {best.label}."
    else:
        msg = f"Soonest opening: {best.label}."

    used_alt: str | None = None
    if crowd:
        msg = msg.rstrip(".") + f" — {crowd}."
        alt = next(
            (
                s
                for s in free_slots[1:3]
                if not _care_lens_for_slot(care, s, timezone_name=timezone_name)[0]
            ),
            None,
        )
        if alt:
            msg += f" {alt.label} is clear of that window."
            used_alt = alt.label
    elif clear:
        msg += f" {clear}."

    leftover = [s.label for s in free_slots[1:3] if s.label != used_alt]
    if leftover:
        msg += f" Also free: {', '.join(leftover)}."
    return msg


def _offline_intent(text: str) -> _ParsedIntent:
    """Model-down draft only — coarse shape, not product intelligence."""
    return _ParsedIntent(
        is_schedule_ask=True,
        kind="availability",
        title=_offline_title(text),
        local_date=None,
        by_days=[],
        local_time="18:30",
        duration_minutes=60,
        timezone="America/Los_Angeles",
    )


def _search_hours(local_time: str) -> tuple[int, int]:
    """Search window from the draft clock time (morning / afternoon / evening)."""
    try:
        hour = int((local_time or "13:00").split(":")[0])
    except ValueError:
        hour = 13
    if hour < 12:
        return 8, 12
    if hour < 17:
        return 12, 17
    return 17, 21


def _draft_from_parsed(parsed: _ParsedIntent) -> EventDraft:
    by_days = _weekday_map(parsed.by_days)
    # Availability with preferred weekdays should still use by_days for
    # windowing — without forcing a recurring series.
    recurring = bool(parsed.recurring) or (
        parsed.kind == "add" and len(by_days) >= 1 and not parsed.local_date
    )
    title = (parsed.title or "").strip()[:48] or "Hold"
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
    pretty_date = _human_date(draft.local_date)
    dur = (
        "1-hour"
        if draft.duration_minutes == 60
        else f"{draft.duration_minutes}-minute"
    )
    if kind is CommitmentKind.AVAILABILITY:
        if pretty_date:
            return f"Soonest {dur} opening from {pretty_date}"
        if draft.by_days:
            days = "/".join(d.value for d in draft.by_days)
            return f"Soonest {dur} opening ({days})"
        return f"Soonest {dur} opening"
    if draft.recurring and draft.by_days:
        days = "/".join(d.value for d in draft.by_days)
        return f"Add {draft.title} every {days} at {time_label} ({draft.duration_minutes}m)"
    if pretty_date:
        return f"Add {draft.title} on {pretty_date} at {time_label} ({draft.duration_minutes}m)"
    return f"Add {draft.title} at {time_label} ({draft.duration_minutes}m)"


async def _profile_context(
    memory: MemoryBank, user_id: str
) -> tuple[str, dict[str, str], CareProfile | None]:
    """Return (prompt block, fact_id → statement, Care Profile) for grounding."""
    # Cap reads — full history was making schedule proposes feel slow.
    facts, care, snapshot = await asyncio.gather(
        memory.facts.list_for_user(user_id=user_id, limit=48),
        memory.manifestos.get_care_profile(user_id=user_id),
        memory.manifestos.get_profile_snapshot(user_id=user_id),
    )
    care_snip = care_profile_snippet(care) or None

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
    return block, fact_map, care


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
                    "Decide holistically whether this is a schedule ask, then parse "
                    "and answer in one JSON object. Do not rely on keyword lists.\n"
                    "Fields:\n"
                    "- is_schedule_ask: true only if they want to add something to "
                    "the calendar OR ask when/whether they have time for something. "
                    "False for day reflections, feelings, tips, or hard life decisions "
                    "with no scheduling ask.\n"
                    "- kind (add|availability), title, by_days (MO..SU), "
                    "local_date (YYYY-MM-DD or null), local_time (HH:MM), "
                    "duration_minutes, recurring, timezone, notes\n"
                    "- title: 2–5 word calendar name (Hold, Coffee, Pickup). "
                    "NEVER copy or paraphrase the user's question.\n"
                    "- duration_minutes: ONLY if they named a length (30 min, 1 hour, …). "
                    "If they did not, duration_minutes=60 and duration_named=false. "
                    "Never invent 90/120/180 for a generic 'opening'.\n"
                    "- duration_named: true only when they stated a length.\n"
                    "- local_time: morning→09:00, afternoon→13:00, evening/night→18:00. "
                    "If they did not name a time of day, 13:00.\n"
                    "- If they asked for the soonest opening on/after a date, "
                    "kind=availability and local_date = that floor date.\n"
                    "- level_message: at most 3 short complete sentences. "
                    "Name weekday + month + day + times (e.g. Wed Aug 26, 8:00–9:00am). "
                    "Never restate their question. Never vague filler like "
                    "'when you have a moment'.\n"
                    "- recommended_action: confirm|revise|decline\n"
                    "- citation_fact_ids: only ids from Citable facts\n\n"
                    "Rules: weekend asks → by_days SA,SU and local_date null. "
                    "today/tonight/this afternoon → local_date=today. "
                    "Never invent free times that contradict the agenda. "
                    "No flattery, no lectures, no raw [bullet:...] ids."
                ),
                prompt=(
                    f"Today is {weekday_name}, {today} (America/Los_Angeles).\n\n"
                    f"User said: {user_text.strip()}\n\n"
                    f"Upcoming calendar (context):\n{agenda_block}\n\n"
                    f"{profile_block}\n\n"
                    "If this is not a schedule ask, set is_schedule_ask=false and "
                    "leave other fields empty/default.\n"
                    "If they asked what time works, answer with a concrete gap "
                    "from that calendar, including the calendar date.\n"
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
    """If Gemini says this is a schedule ask, return a persisted proposal.

    Live path is AI-first: Gemini sets ``is_schedule_ask`` and the draft.
    Regex is only used if the model is unavailable (offline fallback).
    """
    settings = settings or get_settings()
    gemini = gemini or build_gemini_client(settings)
    now = now or datetime.now(tz=timezone.utc)

    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    today = local.strftime("%Y-%m-%d")
    weekday_name = local.strftime("%A")

    # Prefetch ~10 local days so weekend asks have context without a prior parse.
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    scan_start = day0.astimezone(timezone.utc) - timedelta(hours=1)
    scan_end = (day0 + timedelta(days=10)).astimezone(timezone.utc)

    events, (profile_block, fact_map, care) = await asyncio.gather(
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
        # Model down — coarse offline gate only.
        if not looks_like_schedule_ask(user_text):
            return None
        parsed = _normalize_parsed(
            _offline_intent(user_text),
            today=today,
            source_text=user_text,
        )
        kind = CommitmentKind.AVAILABILITY
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
                duration_named=resolved.duration_named,
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
    start_hour, end_hour = _search_hours(draft.local_time)
    free_slots = find_free_slots_nearby(
        events,
        anchor=window_start,
        duration=timedelta(minutes=draft.duration_minutes),
        timezone_name=draft.timezone,
        days=2 if not draft.by_days else 4,
        max_slots=3,
        preferred_weekdays=preferred,
        day_start_hour=start_hour,
        day_end_hour=end_hour,
    )

    # Availability answers must name calendar-derived free slots (not model fluff).
    if kind == CommitmentKind.AVAILABILITY and free_slots:
        level_message = _ground_availability_reply(
            title=draft.title,
            free_slots=free_slots,
            conflicts=conflicts,
            care=care,
            timezone_name=draft.timezone,
        )
    elif not level_message:
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
