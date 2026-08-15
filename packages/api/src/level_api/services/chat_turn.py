"""One Gemini router for Today and About me chat.

Classifies the turn (schedule / email / profile / general), then runs the
matching existing path. Does not invent people or send mail.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

from fastapi import BackgroundTasks
from pydantic import BaseModel, Field

from level_api.routes.care_actions import (
    extract_school_paper,
    propose_from_school_extract,
    propose_sick_day_notes,
)
from level_api.services.care_enrich import enrich_care_from_agenda as _bg_enrich_care
from level_core.auth.tokens import TokenStore
from level_core.calendar.commitment_gate import propose_from_text
from level_core.calendar.event_cues import EventCue, EventCueStore
from level_core.calendar.proposals import ProposalStore
from level_core.calendar.sync_state import CalendarSyncStore, events_for_local_day
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.calendar.agenda_sync import day_events_cached_or_live
from level_core.memory.base import MemoryBank
from level_core.models.base import GenerationRequest
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_infer_llm import apply_note_to_care_profile_ai
from level_core.profile.care_store import apply_care, save_care
from level_core.schemas.care import CareProfile
from level_core.schemas.commitment import CommitmentProposal
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource
from level_core.schemas.user import format_person_name

_logger = get_logger(__name__)

_PATHS = frozenset({"schedule", "email", "profile", "general"})


class ChatTurnParse(BaseModel):
    path: str = "general"
    reply: str = ""
    wants_paper_upload: bool = False
    is_sick_day: bool = False
    notify_contact: bool = False
    sick_person_names: list[str] = Field(default_factory=list)
    contact_role: str = ""
    profile_note: str = ""
    keywords: list[str] = Field(default_factory=list)
    reminder: str = ""
    matched_titles: list[str] = Field(default_factory=list)
    care_role: str = ""


class ChatTurnResult(BaseModel):
    reply: str
    path: str = "general"
    proposal: CommitmentProposal | None = None
    school_proposals: list[CommitmentProposal] = Field(default_factory=list)
    wants_paper_upload: bool = False
    facts_added: int = 0
    cues_added: int = 0


def normalize_chat_path(raw: str) -> str:
    path = (raw or "").strip().lower()
    return path if path in _PATHS else "general"


def _clean_reply(text: str, *, fallback: str) -> str:
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    if t.endswith('"') and t.count('"') % 2 == 1:
        t = t[:-1].rstrip()
    t = t.strip()
    return t or fallback


def _first_name(display_name: str | None) -> str:
    raw = format_person_name(display_name) or ""
    if not raw or raw.lower() in {"guest parent", "caregiver", "guest"}:
        return "there"
    return raw.split()[0][:40]


async def _agenda_titles(
    *,
    user_id: str,
    tokens: TokenStore,
    sync_store: CalendarSyncStore,
) -> tuple[list[str], object | None, object | None, object | None]:
    token, state, user = await asyncio.gather(
        tokens.get_google_token(user_id),
        sync_store.get(user_id),
        tokens.get_user(user_id),
    )
    titles: list[str] = []
    try:
        if token is not None:
            raw_today = await day_events_cached_or_live(
                user_id=user_id,
                token=token,
                sync_store=sync_store,
                day_offset=0,
            )
            titles = [
                (e.get("summary") or "").strip()
                for e in raw_today
                if (e.get("summary") or "").strip()
            ][:20]
        if not titles and state and state.events:
            titles = [
                e["summary"]
                for e in events_for_local_day(state, day_offset=0)
                if e.get("summary")
            ][:20]
    except Exception:  # noqa: BLE001
        titles = []
    return titles, token, state, user


async def _parse_turn(
    *,
    message: str,
    agenda_titles: list[str],
) -> ChatTurnParse:
    settings = get_settings()
    gemini = build_gemini_client(settings)
    titles_block = "\n".join(f"- {t}" for t in agenda_titles) or "(no titles available)"
    parsed = ChatTurnParse(reply="Got it — I’ll keep that in mind.", path="general")
    try:
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                system_instruction=(
                    "You route one caregiver message for Level. Reason from their "
                    "words + today's agenda. Do not keyword-match naively.\n"
                    "path must be exactly one of:\n"
                    "- schedule: they want to add, book, or check calendar time.\n"
                    "- email: they want Level to email a saved institutional "
                    "contact (teacher, doctor, attendance) — sick note, form, "
                    "permission slip, or 'send this'. Never a friend text.\n"
                    "- profile: they are stating or correcting who they hold, "
                    "a priority, a care role, or how they want to be known.\n"
                    "- general: a question, how the day is going, advice, or "
                    "anything else. Answer it. Do not treat a question as a "
                    "profile edit.\n"
                    "wants_paper_upload: true when they want to send a form / "
                    "slip but did not paste the form text.\n"
                    "is_sick_day / notify_contact / sick_person_names / "
                    "contact_role: only for email about someone already in care.\n"
                    "For general or profile, also fill profile_note, keywords, "
                    "matched_titles, care_role, reminder when they shared "
                    "something to remember.\n"
                    "reply: 1–2 short complete sentences that answer them.\n"
                    "JSON only."
                ),
                prompt=f"User said: {message}\n\nToday's agenda titles:\n{titles_block}\n",
                response_schema=ChatTurnParse.model_json_schema(),
                temperature=0.2,
                max_output_tokens=800,
            )
        )
        parsed = ChatTurnParse.model_validate(json.loads(resp.text))
    except (ModelUnavailable, json.JSONDecodeError, ValueError):
        pass
    except Exception:  # noqa: BLE001
        _logger.warning("chat_turn_parse_failed")
    parsed.path = normalize_chat_path(parsed.path)
    return parsed


async def _persist_day_note(
    *,
    user_id: str,
    message: str,
    parsed: ChatTurnParse,
    memory: MemoryBank,
    cue_store: EventCueStore,
    background_tasks: BackgroundTasks,
    sync_store: CalendarSyncStore,
) -> tuple[int, int]:
    note = (parsed.profile_note or "").strip()[:500]
    facts_added = 0
    durable = (
        len(note) >= 12
        and note.lower().startswith("i ")
        and not note.lower().startswith("i notice")
    )
    if durable:
        fact = Fact(
            user_id=user_id,
            type=FactType.CONSTRAINT if parsed.keywords else FactType.PREFERENCE,
            statement=note,
            source_signal_ids=[],
            salience=0.7,
        )
        signal = Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id=f"day-checkin:{uuid.uuid4().hex[:12]}",
            text=f"Day check-in: {message}",
        )
        await memory.facts.upsert(fact)
        await memory.signals.upsert(signal)
        facts_added = 1
    keywords = [k.strip().lower() for k in parsed.keywords if k and k.strip()][:8]
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
    cues_added = 0
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
    role_raw = (parsed.care_role or "").strip().lower()
    titles_to_tag = [t.strip() for t in parsed.matched_titles if t and t.strip()]
    if role_raw and titles_to_tag:
        try:
            care = await memory.manifestos.get_care_profile(user_id=user_id)
            if care is not None:
                tagged = dict(care.calendar_role_by_summary)
                for title in titles_to_tag:
                    key = re.sub(r"\s+", " ", title.strip().lower())
                    if key:
                        tagged[key] = role_raw

                def _tag_titles(current):
                    next_hints = dict(current.calendar_role_by_summary)
                    next_hints.update(tagged)
                    return current.model_copy(
                        update={
                            "calendar_role_by_summary": next_hints,
                            "version": int(current.version or 1) + 1,
                        }
                    )

                await apply_care(memory, user_id, _tag_titles)
        except Exception:  # noqa: BLE001
            _logger.warning("chat_turn_role_tag_failed", user_id=user_id)
    if facts_added:
        background_tasks.add_task(
            _bg_enrich_care, user_id, memory, sync_store, force=True
        )
    return facts_added, cues_added


async def run_chat_turn(
    *,
    user_id: str,
    message: str,
    memory: MemoryBank,
    tokens: TokenStore,
    sync_store: CalendarSyncStore,
    store: ProposalStore,
    cue_store: EventCueStore,
    background_tasks: BackgroundTasks,
) -> ChatTurnResult:
    """Route one user message. Nothing is sent or booked until they confirm."""
    message = (message or "").strip()
    titles, token, state, user = await _agenda_titles(
        user_id=user_id, tokens=tokens, sync_store=sync_store
    )
    parsed = await _parse_turn(message=message, agenda_titles=titles)
    fallback = "Got it — I’ll keep that in mind."
    reply = _clean_reply(parsed.reply, fallback=fallback)
    path = parsed.path
    result = ChatTurnResult(reply=reply, path=path)

    if path == "schedule":
        if token is None:
            result.reply = "Connect Google Calendar on Sources first and I can book that."
            return result
        try:
            proposal = await propose_from_text(
                user_id=user_id,
                user_text=message,
                token=token,  # type: ignore[arg-type]
                memory=memory,
                store=store,
                sync_store=sync_store,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("chat_turn_schedule_failed", error=str(exc))
            proposal = None
        if proposal is not None:
            result.proposal = proposal
            result.reply = proposal.level_message or proposal.summary or reply
            return result
        path = "general"

    if path == "email":
        if parsed.is_sick_day or parsed.notify_contact:
            try:
                care = await memory.manifestos.get_care_profile(user_id=user_id)
                sick_events: list[dict[str, str | None]] = []
                if state and getattr(state, "events", None):
                    sick_events = [
                        {
                            "id": ev.id,
                            "summary": ev.summary,
                            "start": ev.start,
                            "end": ev.end,
                            "status": ev.status,
                        }
                        for ev in state.events.values()
                        if ev.summary
                    ]
                if care is not None:
                    proposals, ask = await propose_sick_day_notes(
                        user_id=user_id,
                        user_text=message,
                        care=care,
                        named=list(parsed.sick_person_names),
                        events=sick_events,
                        store=store,
                        contact_role=parsed.contact_role or "teacher",
                        cancel_today=parsed.is_sick_day,
                        from_name="" if _first_name(getattr(user, "display_name", None)) == "there"
                        else _first_name(getattr(user, "display_name", None)),
                    )
                    result.school_proposals = proposals
                    if ask:
                        result.reply = ask
                    elif proposals:
                        result.reply = proposals[0].level_message or reply
                    return result
            except Exception:  # noqa: BLE001
                _logger.warning("chat_turn_email_failed", user_id=user_id)
        if len(message) >= 80:
            try:
                extract = await extract_school_paper(message)
                care = await memory.manifestos.get_care_profile(user_id=user_id)
                proposal, ask = await propose_from_school_extract(
                    user_id=user_id,
                    user_text=message[:400],
                    extract=extract,
                    care=care,
                    memory=memory,
                    store=store,
                )
                if proposal is not None:
                    result.school_proposals = [proposal]
                    result.reply = proposal.level_message or reply
                    return result
                if ask:
                    result.reply = ask
                    result.wants_paper_upload = True
                    return result
            except Exception:  # noqa: BLE001
                _logger.warning("chat_turn_paper_extract_failed", user_id=user_id)
        result.wants_paper_upload = True
        result.reply = reply or "Upload or paste the slip and I’ll draft the email."
        return result

    if path == "profile":
        care = await memory.manifestos.get_care_profile(user_id=user_id)
        if care is None:
            care = CareProfile(user_id=user_id, roles=[], version=1)
            boot = await apply_note_to_care_profile_ai(care, message)
            if boot is not None:
                care, note_reply = boot
                await save_care(memory, care, expected_version=None)
                result.reply = _clean_reply(note_reply, fallback=reply)
                result.facts_added = 1
            else:
                result.reply = reply
        else:
            expected = care.version
            updated = await apply_note_to_care_profile_ai(care, message)
            if updated is not None:
                care, note_reply = updated
                await save_care(memory, care, expected_version=expected)
                result.reply = _clean_reply(note_reply, fallback=reply)
                result.facts_added = 1
        return result

    facts_added, cues_added = await _persist_day_note(
        user_id=user_id,
        message=message,
        parsed=parsed,
        memory=memory,
        cue_store=cue_store,
        background_tasks=background_tasks,
        sync_store=sync_store,
    )
    result.facts_added = facts_added
    result.cues_added = cues_added
    if cues_added and parsed.keywords:
        result.reply = f"{reply} I’ll nudge you on {parsed.keywords[0]} days."
    else:
        result.reply = reply
    return result


__all__ = [
    "ChatTurnParse",
    "ChatTurnResult",
    "normalize_chat_path",
    "run_chat_turn",
]
