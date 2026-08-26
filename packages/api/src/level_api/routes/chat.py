"""Chat: router-driven dispatch, streaming SSE for replies."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from level_core.agents.adk_runner import is_adk_enabled, plan_and_dispatch
from level_core.agents.base import QuotaExhausted
from level_core.agents.book import run as book_run
from level_core.agents.chat_router import run as router_run
from level_core.agents.model_armor import BLOCK_REPLY as ARMOR_BLOCK_REPLY
from level_core.agents.person_edit import run as person_edit_run
from level_core.agents.priority import run as priority_run
from level_core.agents.reminder import run as reminder_run
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import refresh_agenda
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.email.drafter import draft_email
from level_core.email.resolve import (
    EmailCandidate,
    is_email_request,
    pick_candidate,
    resolve_email_targets,
)
from level_core.schedule.book import book_event, delete_event, move_event
from level_core.schedule.slots import (
    calendar_title_from_label,
    infer_event_kind,
    parse_duration_minutes,
    plan_label_from_message,
    recommend_slots,
)
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    ChatMessage,
    ChatRole,
    ChatRouterIntent,
    ChatRouterPath,
    NegativeAgent,
)
from level_core.schemas.usual import hour_to_band
from level_core.schemas.care import CareRelation, role_for_relation
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store
from level_core.storage.care_store import (
    add_priority,
    add_reminder,
    find_person_by_name,
    new_id,
    parse_person_intro,
    parse_reminder,
    parse_reminder_followup,
    record_negative,
    relation_from_phrase,
    relation_label,
    set_person_status,
    upsert_kept_person,
)
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from level_api.deps import get_user_store
from level_api.routes.email import register_pending_draft

router = APIRouter()
logger = get_logger(__name__)


class ChatTurn(BaseModel):
    """One prior turn shipped by the client for session context."""

    role: Literal["user", "assistant"]
    text: str


class ChatBody(BaseModel):
    message: str
    # Client attaches recent turns (excluding the current message) so the
    # router and downstream extractors can resolve pronouns / partial info
    # like "Tuesday 7:45am" after an earlier "put the drop-off back".
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    include_profile: bool = False


MAX_HISTORY_TURNS = 8
MAX_HISTORY_CHARS_PER_TURN = 400


def _prepare_history(history: list[ChatTurn] | None) -> list[dict[str, str]]:
    """Trim + cap the client-supplied history before we forward it to an LLM.

    Cheap defense against runaway prompts; we take at most the last
    `MAX_HISTORY_TURNS` turns and truncate any single turn to
    `MAX_HISTORY_CHARS_PER_TURN` characters.
    """
    if not history:
        return []
    trimmed = history[-MAX_HISTORY_TURNS:]
    out: list[dict[str, str]] = []
    for t in trimmed:
        text = (t.text or "").strip()
        if not text:
            continue
        if len(text) > MAX_HISTORY_CHARS_PER_TURN:
            text = text[:MAX_HISTORY_CHARS_PER_TURN] + "\u2026"
        out.append({"role": t.role, "text": text})
    return out


@router.post("/chat")
async def chat(body: ChatBody, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return await _handle_message(store, body.message, _prepare_history(body.history))


@router.get("/chat/stream")
async def chat_stream(
    message: str, store: UserStore = Depends(get_user_store)
) -> EventSourceResponse:
    """SSE stream. Reconstructs history from persisted chat_turns so
    EventSource (GET only) has the same conversational context that
    POST /v1/chat receives via the request body.
    """

    async def event_source() -> AsyncIterator[dict[str, Any]]:
        history = await _history_from_store(store)
        result = await _handle_message(store, message, history)
        for chunk in _chunk(result["reply"], size=64):
            yield {"event": "delta", "data": json.dumps({"text": chunk})}
            await asyncio.sleep(0.02)
        yield {"event": "done", "data": json.dumps(result)}

    return EventSourceResponse(event_source())


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _history_from_store(store: UserStore) -> list[dict[str, str]]:
    """Reconstruct the last few chat turns from persisted state.

    Kept in sync with _prepare_history for the POST path — same caps
    (MAX_HISTORY_TURNS, MAX_HISTORY_CHARS_PER_TURN) so the router prompt
    stays cheap and both entrypoints see the same conversation shape.
    """
    turns = await store.chat_turns.list()
    turns.sort(key=lambda t: t.created_at)
    tail = turns[-MAX_HISTORY_TURNS:]
    out: list[dict[str, str]] = []
    for t in tail:
        text = (t.text or "").strip()
        if not text:
            continue
        if len(text) > MAX_HISTORY_CHARS_PER_TURN:
            text = text[:MAX_HISTORY_CHARS_PER_TURN] + "\u2026"
        out.append({"role": str(t.role), "text": text})
    return out


async def _handle_message(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    turn_in = ChatMessage(
        turn_id=new_id("tin"), role=ChatRole.USER, text=message
    )
    await store.chat_turns.upsert(turn_in)

    try:
        return await _dispatch_message(store, message, history)
    except QuotaExhausted as err:
        wait = f" in about {err.retry_after_s}s" if err.retry_after_s else ""
        logger.warning(
            "chat.quota",
            user=store.user_id,
            retry_after_s=err.retry_after_s,
        )
        return await _ack_no_agent(
            store,
            f"I\u2019m at my daily Gemini quota{wait}. Your message is saved in the box \u2014 try again shortly, or I can still book times without the model.",
        )


async def _dispatch_message(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    # Confirm-flow gate: if the user has a pending booking (from a previous
    # turn where we flagged conflicts / priorities) and this turn is a
    # simple yes/no, act on it before doing anything else.
    pending_response = await _handle_pending_confirmation(store, message)
    if pending_response is not None:
        return pending_response

    pending_email = await _handle_pending_email_pick(store, message)
    if pending_email is not None:
        return pending_email

    # Fast path: explicit priority statements ("prioritize X over Y") skip
    # the router + PriorityAgent so they still work when Gemini quota is
    # exhausted — and they're ~instant either way.
    fast_prio = await _try_fast_priority(store, message)
    if fast_prio is not None:
        return fast_prio

    fast_person = await _try_fast_person(store, message)
    if fast_person is not None:
        return fast_person

    # Fast path: "remind me to bring a charger" skips ReminderAgent.
    fast_rem = await _try_fast_reminder(store, message, history)
    if fast_rem is not None:
        return fast_rem

    # Fast path: "email Nova's teacher…" resolves contacts without Gemini.
    fast_email = await _try_fast_email(store, message, history)
    if fast_email is not None:
        return fast_email

    # Fast path: create / move / delete when the user named a day + time.
    # Incomplete calendar asks get a clarify reply instead of an LLM call.
    fast_cal = await _try_fast_calendar(store, message)
    if fast_cal is not None:
        return fast_cal

    decision = await router_run(store=store, user_message=message, history=history)
    if not decision.value:
        # Router blocked (quota/gate) OR no value returned: soft-degrade
        # with a canned reply so chat never goes silent. This is the
        # rubric-mandated failure isolation for the Gateway component.
        if decision.blocked_by_safety:
            # Model Armor tripped. Return the injection-specific canned
            # reply so the user knows what happened; the budget message
            # would confuse the user by claiming a resource issue when
            # the actual gate was security.
            reply = ARMOR_BLOCK_REPLY
        elif decision.soft_degraded:
            reply = (
                "I\u2019m at my model budget for the moment. Your message is "
                "saved \u2014 try again in a few minutes, or ask me to book a "
                "concrete time and I\u2019ll do it without the model."
            )
        else:
            reply = "I heard you. I'll remember that."
        return await _ack_no_agent(store, reply)

    # Collaborative Partner: honor the router's clarifying-question exit.
    # This is the "asks clarifying questions, guides step-by-step" bullet.
    needs_clarify = bool(getattr(decision.value, "needs_clarification", False))
    clarifying_q = getattr(decision.value, "clarifying_question", None)
    if needs_clarify and clarifying_q:
        await _write_reply(store, clarifying_q)
        return {
            "reply": clarifying_q,
            "path": decision.value.path.value,  # type: ignore[union-attr]
            "intent": decision.value.intent.value,  # type: ignore[union-attr]
            "needs_clarification": True,
            "clarifying_question": clarifying_q,
        }

    path = decision.value.path  # type: ignore[union-attr]
    intent = decision.value.intent  # type: ignore[union-attr]

    if path == ChatRouterPath.PROFILE and intent == ChatRouterIntent.PRIORITY:
        return await _extract_priority(store, message, decision.value, history)  # type: ignore[arg-type]
    if path == ChatRouterPath.REMINDER and intent == ChatRouterIntent.ADD_REMINDER:
        return await _extract_reminder(store, message, decision.value, history)  # type: ignore[arg-type]
    if path == ChatRouterPath.PROFILE and intent == ChatRouterIntent.PERSON_UPDATE:
        return await _person_update(store, message, decision.value, history)  # type: ignore[arg-type]
    if path == ChatRouterPath.SCHEDULE and intent == ChatRouterIntent.BOOK_NOW:
        return await _book(store, message, history)
    if path == ChatRouterPath.SCHEDULE and intent == ChatRouterIntent.FIND_TIME:
        return await _fast_find_time(store, message)
    if path == ChatRouterPath.SCHEDULE:
        return await _book(store, message, history)
    if path == ChatRouterPath.EMAIL:
        return await _handle_email_request(store, message, history)
    return {
        "reply": "Noted. I keep an eye on your calendar and remind you when things line up.",
        "path": path,
        "intent": intent,
    }


async def _extract_priority(
    store: UserStore, message: str, decision: Any, history: list[dict[str, str]]
) -> dict[str, Any]:
    result = await priority_run(store=store, message=message, history=history)
    if not result.value or result.value.priority is None:
        return await _ack_no_agent(store, "I hear you. Say more when you're ready.")
    ep = result.value.priority
    prio = await add_priority(
        store,
        text=ep.text,
        weight=ep.weight,
        activity_types=ep.activity_types,
        source_span=ep.source_span,
    )
    await _write_reply(store, f"Saved '{prio.text}' as a priority.")
    return {
        "reply": f"Saved '{prio.text}' as a priority (weight {prio.weight}).",
        "path": "profile",
        "intent": "priority",
        "priority_id": prio.priority_id,
    }


_PRIORITY_LEAD = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"prioritize|priority|"
    r"make\s+sure|"
    r"never\s+miss|"
    r"always\s+(?:protect|put)|"
    r"prefer"
    r")\b",
    re.IGNORECASE,
)

_PRIORITY_ACTIVITY_HINTS: list[tuple[re.Pattern[str], ActivityType]] = [
    (re.compile(r"\b(?:soccer)\b", re.I), ActivityType.SPORTS_SOCCER),
    (re.compile(r"\bbasketball\b", re.I), ActivityType.SPORTS_BASKETBALL),
    (re.compile(r"\bswim(?:ming)?\b", re.I), ActivityType.SPORTS_SWIM),
    (re.compile(r"\bsports?\b", re.I), ActivityType.SPORTS_OTHER),
    (re.compile(r"\bdrop[\s-]?off\b", re.I), ActivityType.SCHOOL_DROPOFF),
    (re.compile(r"\bpick[\s-]?up\b", re.I), ActivityType.SCHOOL_PICKUP),
    (re.compile(r"\bschool\b", re.I), ActivityType.SCHOOL_EVENT),
    (re.compile(r"\btherapy\b", re.I), ActivityType.MEDICAL_THERAPY),
    (re.compile(r"\b(?:dentist|doctor|medical|appointment)\b", re.I), ActivityType.MEDICAL_APPT),
    (re.compile(r"\b(?:elder|dad|mom|father|mother|parent|family)\b", re.I), ActivityType.FAMILY),
    (re.compile(r"\bwork\b", re.I), ActivityType.WORK),
]


async def _try_fast_priority(
    store: UserStore, message: str
) -> dict[str, Any] | None:
    """Save an explicit priority without an LLM call.

    Matches statements like "prioritize elder care over sports" or
    "never miss Sunday physical therapy". Anything vaguer falls through
    to the router so we don't over-capture chit-chat.
    """
    text = message.strip()
    if not text or not _PRIORITY_LEAD.search(text):
        return None
    if _TIME_RANGE_RE.search(text):
        # "prioritize booking Tuesday 2-3pm" is a schedule, not a priority.
        return None

    types: list[ActivityType] = []
    seen: set[ActivityType] = set()
    for pattern, activity in _PRIORITY_ACTIVITY_HINTS:
        if pattern.search(text) and activity not in seen:
            types.append(activity)
            seen.add(activity)

    weight = 5 if re.search(r"\bnever miss\b", text, re.I) else 4
    prio = await add_priority(
        store,
        text=text,
        weight=weight,
        activity_types=types,
        source_span=text[:200],
    )
    logger.info(
        "chat.priority.fast_hit",
        user=store.user_id,
        weight=weight,
        types=[t.value for t in types],
    )
    reply = f"Saved \u201c{prio.text}\u201d as a priority. I\u2019ll flag bookings that collide with it."
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "profile",
        "intent": "priority",
        "priority_id": prio.priority_id,
    }


async def _try_fast_person(store: UserStore, message: str) -> dict[str, Any] | None:
    """Add or correct a person without Gemini: 'Alex is my co-parent'."""
    parsed = parse_person_intro(message)
    if parsed is None:
        return None
    name, relation = parsed
    return await _remember_person(store, name=name, relation=relation, source_span=message[:200])


async def _remember_person(
    store: UserStore, *, name: str, relation: Any, source_span: str | None
) -> dict[str, Any]:
    existing = await find_person_by_name(store, name)
    person = await upsert_kept_person(
        store,
        display_name=name if existing is None else existing.display_name,
        relation=relation,
        is_self=relation == CareRelation.SELF,
        source_span=source_span,
    )
    # Roster changed - re-run enrich so cached matched_person_ids on
    # existing events stops pointing at [self_id] for events whose summary
    # names the person we just added / updated.
    if existing is None:
        try:
            await enrich_agenda(store)
        except Exception:  # noqa: BLE001
            pass
    label = relation_label(person.relation)
    if existing is None:
        reply = f"Got it \u2014 I\u2019ll remember {person.display_name} as your {label}."
    elif existing.relation != person.relation:
        reply = (
            f"Updated {person.display_name}: "
            f"{relation_label(existing.relation)} \u2192 {label}."
        )
    else:
        reply = f"{person.display_name} is already on your list as your {label}."
    await _write_reply(store, reply)
    return {"reply": reply, "path": "profile", "intent": "person_update", "person_id": person.person_id}


async def _extract_reminder(
    store: UserStore, message: str, decision: Any, history: list[dict[str, str]]
) -> dict[str, Any]:
    result = await reminder_run(store=store, message=message, history=history)
    if result.value and result.value.reminder is not None:
        er = result.value.reminder
        return await _save_reminder(
            store,
            text=er.text,
            person_display_name=er.person_display_name,
            activity_type=er.activity_type or ActivityType.OTHER,
            source_span=er.source_span,
            lead_minutes=er.lead_minutes,
        )
    parsed = parse_reminder(message) or parse_reminder_followup(message, history)
    if parsed is not None:
        return await _save_reminder(
            store,
            text=parsed.text,
            person_display_name=parsed.person_display_name,
            activity_type=parsed.activity_type,
            source_span=parsed.source_span,
        )
    return await _ack_no_agent(store, "Tell me the thing you might forget and I'll surface it.")


async def _try_fast_reminder(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any] | None:
    """Save an explicit reminder without an LLM call."""
    if _TIME_RANGE_RE.search(message):
        return None
    parsed = parse_reminder(message) or parse_reminder_followup(message, history)
    if parsed is None:
        return None
    logger.info(
        "chat.reminder.fast_hit",
        user=store.user_id,
        activity=parsed.activity_type.value,
    )
    return await _save_reminder(
        store,
        text=parsed.text,
        person_display_name=parsed.person_display_name,
        activity_type=parsed.activity_type,
        source_span=parsed.source_span,
    )


async def _save_reminder(
    store: UserStore,
    *,
    text: str,
    person_display_name: str | None,
    activity_type: ActivityType,
    source_span: str | None,
    lead_minutes: int = 60,
) -> dict[str, Any]:
    person_id: str | None = None
    if person_display_name:
        found = await find_person_by_name(store, person_display_name)
        if found is not None:
            person_id = found.person_id
    reminder = await add_reminder(
        store,
        text=text,
        person_id=person_id,
        activity_type=activity_type,
        lead_minutes=lead_minutes,
        source_span=source_span,
    )
    await enrich_agenda(store)
    where = activity_type.category.label.lower()
    if activity_type == ActivityType.OTHER:
        reply = (
            f"Reminder saved: '{reminder.text}'. "
            "I'll keep it in Reminders — tell me which events it belongs on "
            "(dropoff, pickup, work, soccer) if you want it on the calendar."
        )
    else:
        reply = f"Reminder saved: '{reminder.text}'. I'll flag it on {where} events."
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "reminder",
        "intent": "add_reminder",
        "reminder_id": reminder.reminder_id,
    }


async def _person_update(
    store: UserStore, message: str, decision: Any, history: list[dict[str, str]]
) -> dict[str, Any]:
    result = await person_edit_run(store=store, message=message, history=history)
    edit = result.value.edit if (result.value and result.value.edit) else None  # type: ignore[union-attr]
    if edit is None or edit.action == "unknown":
        parsed = parse_person_intro(message)
        if parsed is not None:
            name, relation = parsed
            return await _remember_person(
                store, name=name, relation=relation, source_span=message[:200]
            )
        return await _ack_no_agent(
            store,
            "Tell me who they are \u2014 \u201cAlex is my co-parent\u201d or \u201cRobert is my kid, not my dad\u201d.",
        )

    target = await find_person_by_name(store, edit.target_name)
    if target is None:
        relation = edit.new_relation
        if relation is None and edit.action in {"add", "change_relation"}:
            relation = relation_from_phrase(message)
        if relation is not None:
            return await _remember_person(
                store,
                name=edit.target_name,
                relation=relation,
                source_span=edit.source_span,
            )
        return await _ack_no_agent(
            store,
            f"I don\u2019t have {edit.target_name} yet. Try \u201c{edit.target_name} is my co-parent\u201d (or kid, elder, partner).",
        )

    if edit.action == "add" and edit.new_relation is not None:
        return await _remember_person(
            store,
            name=target.display_name,
            relation=edit.new_relation,
            source_span=edit.source_span,
        )

    if edit.action == "change_relation" and edit.new_relation is not None:
        old_relation = target.relation.value
        updated = await store.people.upsert(
            target.model_copy(
                update={
                    "relation": edit.new_relation,
                    "care_role_id": role_for_relation(edit.new_relation),
                    "status": "kept",
                }
            )
        )
        # Record so RoleAgent won't re-propose the wrong classification on refresh.
        await record_negative(
            store,
            agent=NegativeAgent.ROLE,
            field="relation",
            value=f"{updated.display_name}={old_relation}",
            reason=f"user said {edit.new_relation.value}",
        )
        reply = f"Updated {updated.display_name}: {old_relation} \u2192 {edit.new_relation.value}."
        await _write_reply(store, reply)
        return {"reply": reply, "path": "profile", "intent": "person_update"}

    if edit.action == "rename" and edit.new_display_name:
        old_name = target.display_name
        new_aliases = sorted({*target.aliases, old_name})
        updated = await store.people.upsert(
            target.model_copy(
                update={
                    "display_name": edit.new_display_name.strip(),
                    "aliases": new_aliases,
                    "status": "kept",
                }
            )
        )
        try:
            await enrich_agenda(store)  # display_name changed, rematch events
        except Exception:  # noqa: BLE001
            pass
        reply = f"Got it \u2014 calling them {updated.display_name}."
        await _write_reply(store, reply)
        return {"reply": reply, "path": "profile", "intent": "person_update"}

    if edit.action == "mark_self":
        updated = await store.people.upsert(
            target.model_copy(update={"is_self": True, "status": "kept"})
        )
        try:
            await enrich_agenda(store)  # events previously tagged to X now tag to Me
        except Exception:  # noqa: BLE001
            pass
        reply = f"Marked {updated.display_name} as you."
        await _write_reply(store, reply)
        return {"reply": reply, "path": "profile", "intent": "person_update"}

    if edit.action == "remove":
        await set_person_status(store, target.person_id, "not_me")
        await record_negative(
            store,
            agent=NegativeAgent.ROLE,
            field="display_name",
            value=target.display_name,
            reason="user removed",
        )
        try:
            await enrich_agenda(store)  # events tagged to removed person fall back to self
        except Exception:  # noqa: BLE001
            pass
        reply = f"Removed {target.display_name} from your care list."
        await _write_reply(store, reply)
        return {"reply": reply, "path": "profile", "intent": "person_update"}

    return await _ack_no_agent(store, "I heard you, but I'm not sure how to change that yet.")


async def _book(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    """LLM-driven booking: only reached when the fast-path can't match.

    Uses BookAgent to extract a booking spec, resolves it to a concrete
    datetime range, inserts into Google Calendar, and re-syncs the local
    agenda cache.
    """
    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    result = await book_run(
        store=store,
        message=message,
        today_iso=today_local.isoformat(),
        history=history,
    )

    booking = result.value.booking if (result.value and result.value.booking) else None  # type: ignore[union-attr]
    if booking is None:
        logger.info("chat.book.no_extract", user=store.user_id, message_len=len(message))
        return await _ack_no_agent(
            store,
            "Tell me the day and time (e.g. \u201cTuesday 7:45\u201312am, Nova drop-off\u201d) and I\u2019ll add it.",
        )

    try:
        start_dt, end_dt = _resolve_range(booking, today_local, tz)
    except ValueError as err:
        logger.info(
            "chat.book.bad_range",
            user=store.user_id,
            start=booking.start_hhmm,
            end=booking.end_hhmm,
            err=str(err),
        )
        return await _ack_no_agent(store, f"I couldn\u2019t work out the time: {err}. Try again with a clearer range?")

    return await _propose_cal_change(
        store,
        action="create",
        message=message,
        title=booking.title,
        start_dt=start_dt,
        end_dt=end_dt,
        source_span=booking.source_span,
        location=booking.location,
    )


# Fast-path booking: deterministic regex + usual lookup. Handles common
# shapes like "Tuesday Aug 25 2:00-3:30pm" and "put back Tuesday 7:45am-
# 8:22am" in ~1-2s without any LLM calls.

_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_MONTH_INDEX: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_WEEKDAY_RE = re.compile(
    r"\b(Mon(?:day)?|Tue(?:s(?:day)?)?|Wed(?:nesday|s)?|Thu(?:r(?:s(?:day)?)?)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)\b",
    re.IGNORECASE,
)
# "August 25" / "Aug 25" / "Aug 25th" / "Aug 25, 2026"
_NAMED_DATE_RE = re.compile(
    r"\b(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
# "8/25", "8/25/26", "8/25/2026" - keep the leading token modest so we
# don't match phone numbers or slashes in URLs.
_SLASH_DATE_RE = re.compile(
    r"(?<![\d/])(?P<mon>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?(?![\d/])"
)
# "<start> [-|to|\u2013] <end>". am/pm optional on either endpoint; the
# missing one inherits from the other.
_TIME_OPT = r"(\d{1,2}(?::\d{2})?)\s*(am|pm)?"
_TIME_RANGE_RE = re.compile(
    rf"{_TIME_OPT}\s*(?:-|to|\u2013|\u2014|\u2212)\s*{_TIME_OPT}",
    re.IGNORECASE,
)


def _find_explicit_date(message: str, today: date) -> date | None:
    """Parse the first explicit date mentioned in `message`, or None.

    Year rolls forward: if only "Aug 25" is given and Aug 25 has already
    passed this year, we assume next year. This matches how humans usually
    talk about dates in chat.
    """
    m = _NAMED_DATE_RE.search(message)
    if m:
        month = _MONTH_INDEX.get(m.group("mon").lower())
        day = int(m.group("day"))
        year = int(m.group("year")) if m.group("year") else None
    else:
        m = _SLASH_DATE_RE.search(message)
        if m:
            month = int(m.group("mon"))
            day = int(m.group("day"))
            year_raw = m.group("year")
            if year_raw is None:
                year = None
            else:
                year = int(year_raw)
                if year < 100:
                    year += 2000
        else:
            if re.search(r"\btoday\b", message, re.IGNORECASE):
                return today
            if re.search(r"\btomorrow\b", message, re.IGNORECASE):
                return today + timedelta(days=1)
            return None
    if month is None or not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    try:
        if year is not None:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        # Rolling: "Aug 25" said in September means next year.
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate
    except ValueError:
        return None


_CANCEL_LEAD = re.compile(
    r"\b(?:remove|delete|cancel|unbook|take\s+off)\b",
    re.IGNORECASE,
)
_MOVE_LEAD = re.compile(r"\b(?:move|reschedule|shift)\b", re.IGNORECASE)
_CREATE_LEAD = re.compile(
    r"\b(?:book|schedule|create)\b|\bput\b.+\bback\b",
    re.IGNORECASE,
)
_ADD_LEAD = re.compile(r"\b(?:add|put)\b", re.IGNORECASE)

# Availability search — phrasing varies a lot; we match intent, not one idiom.
_LOOKUP_EXISTING = re.compile(
    r"\b(?:what time (?:is|does|do)|when (?:is|are|does|do) my|when does)\b",
    re.IGNORECASE,
)
_FIND_VERB = re.compile(
    r"\b(?:find|look(?:ing)?\s+for|search(?:ing)?\s+for|suggest|recommend|"
    r"squeeze(?:\s+in)?|pick\s+(?:a |the )?(?:time|slot)|"
    r"any\s+(?:room|chance|way|openings?)|got any|have any|"
    r"is there|are there)\b",
    re.IGNORECASE,
)
_WHEN_SEARCH = re.compile(
    r"\bwhen\s+(?:can|could|should|would)\b|"
    r"\bwhen(?:'s|\s+is|\s+are)\s+(?:a |the )?(?:good |best |open )?(?:times?|slots?)\b",
    re.IGNORECASE,
)
_FREE_ASK = re.compile(
    r"\b(?:am I|are we|i(?:'m| am)|we(?:'re| are))\s+free\b|"
    r"\bfree\s+(?:this|next|tomorrow|today|on)\b|"
    r"\b(?:best|good|open)\s+(?:times?|slots?)\b|"
    r"\b(?:openings?|availability)\b",
    re.IGNORECASE,
)
_HORIZON_HINT = re.compile(
    r"\b(?:this\s+week|next\s+week|this\s+weekend|tomorrow|sometime|soon)\b",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^\s*(?:can|could|would|should|do|does|is|are|any|got|have)\b",
    re.IGNORECASE,
)


def _parse_time_range(message: str) -> tuple[time, time] | None:
    time_match = _TIME_RANGE_RE.search(message)
    if not time_match:
        return None
    start_num, start_suffix = time_match.group(1), time_match.group(2)
    end_num, end_suffix = time_match.group(3), time_match.group(4)
    try:
        start_t = _compose_time(start_num, start_suffix or end_suffix)
        end_t = _compose_time(end_num, end_suffix or start_suffix)
    except ValueError:
        return None
    if end_t <= start_t:
        return None
    return start_t, end_t


def _parse_all_time_ranges(message: str) -> list[tuple[time, time]]:
    out: list[tuple[time, time]] = []
    for m in _TIME_RANGE_RE.finditer(message):
        try:
            start_t = _compose_time(m.group(1), m.group(2) or m.group(4))
            end_t = _compose_time(m.group(3), m.group(4) or m.group(2))
        except ValueError:
            continue
        if end_t > start_t:
            out.append((start_t, end_t))
    return out


def _times_close(a: datetime, b: time, *, slack_min: int = 2) -> bool:
    return abs((a.hour * 60 + a.minute) - (b.hour * 60 + b.minute)) <= slack_min


def _resolve_target_date(text: str, today: date) -> date | None:
    explicit = _find_explicit_date(text, today)
    if explicit is not None:
        return explicit
    weekday_match = _WEEKDAY_RE.search(text)
    if not weekday_match:
        return None
    weekday = _WEEKDAY_INDEX.get(weekday_match.group(1).lower())
    if weekday is None:
        return None
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def _event_title_matches(event: Any, hint: str | None) -> bool:
    if not hint:
        return True
    hint_l = hint.lower()
    if hint_l in (event.summary or "").lower():
        return True
    if event.activity_type and event.activity_type.category.label.lower() == hint_l:
        return True
    return False


async def _match_agenda_events(
    store: UserStore,
    *,
    day: date,
    start_t: time | None,
    end_t: time | None,
    title_hint: str | None,
) -> list[Any]:
    tz = await tz_for_store(store)
    matches: list[Any] = []
    for event in await store.agenda.list():
        if event.time.all_day:
            continue
        local_start = event.time.start.astimezone(tz)
        local_end = event.time.end.astimezone(tz)
        if local_start.date() != day:
            continue
        if start_t is not None and not _times_close(local_start, start_t):
            continue
        if end_t is not None and not _times_close(local_end, end_t):
            continue
        matches.append(event)
    if title_hint:
        titled = [e for e in matches if _event_title_matches(e, title_hint)]
        if titled:
            return titled
        if start_t is None:
            return titled
    return matches


def _fmt_hm_time(t: time) -> str:
    suffix = "am" if t.hour < 12 else "pm"
    hour_12 = t.hour % 12 or 12
    if t.minute == 0:
        return f"{hour_12}{suffix}"
    return f"{hour_12}:{t.minute:02d}{suffix}"


async def _clarify(store: UserStore, text: str) -> dict[str, Any]:
    # _ack_no_agent now handles persistence; the standalone _write_reply
    # would double-persist.
    return await _ack_no_agent(store, text)


async def _try_fast_calendar(
    store: UserStore, message: str
) -> dict[str, Any] | None:
    """Create / move / delete without an LLM when the user named a day+time.

    Incomplete calendar asks get a clarify reply (still no LLM). Returns
    None only when the message isn't a calendar mutation at all.
    """
    if _is_find_time(message):
        return await _fast_find_time(store, message)

    picked = await _try_pick_offered_slot(store, message)
    if picked is not None:
        return picked

    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    is_move = bool(_MOVE_LEAD.search(message))
    is_cancel = bool(_CANCEL_LEAD.search(message)) and not is_move
    is_create_verb = bool(_CREATE_LEAD.search(message)) and not is_move and not is_cancel
    is_add = bool(_ADD_LEAD.search(message)) and not is_move and not is_cancel
    has_range = _TIME_RANGE_RE.search(message) is not None
    has_day = _resolve_target_date(message, today_local) is not None
    has_title = _title_from_message(message) is not None

    if is_move:
        return await _fast_move(store, message)
    if is_cancel:
        return await _fast_delete(store, message)
    if is_create_verb:
        return await _fast_create(store, message)
    # "add/put …" only counts as calendar when it already looks like one.
    if is_add and (has_range or has_day or has_title):
        return await _fast_create(store, message)
    # Bare "Tuesday 7:45-8:22 dropoff" still counts as create.
    if has_range and has_day:
        return await _fast_create(store, message)
    return None


def _is_find_time(message: str) -> bool:
    """True when the user wants open slots suggested, however they phrased it.

    Catches 'find lunch this week', 'when can I grab coffee', 'any room for
    a walk', 'schedule dinner this week' — not 'what time is pickup' and
    not a concrete 'book Tuesday 2-3pm'.
    """
    text = message.strip()
    if not text:
        return False
    if _MOVE_LEAD.search(text) or _CANCEL_LEAD.search(text):
        return False
    if _LOOKUP_EXISTING.search(text) and not _FIND_VERB.search(text):
        return False
    has_range = _TIME_RANGE_RE.search(text) is not None
    if has_range and (_CREATE_LEAD.search(text) or _ADD_LEAD.search(text)):
        return False
    if _FIND_VERB.search(text) or _WHEN_SEARCH.search(text) or _FREE_ASK.search(text):
        return True
    # "can we do lunch this week?" / "schedule lunch this week" with no clock.
    if not has_range and _HORIZON_HINT.search(text):
        if _QUESTION_START.search(text) or text.endswith("?"):
            return True
        if _CREATE_LEAD.search(text) or _ADD_LEAD.search(text):
            return True
    return False


def _horizon_for_message(
    message: str, now_local: datetime
) -> tuple[datetime, int, str]:
    """(search start, day count, label) for 'this week' / weekend / tomorrow."""
    today = now_local.date()
    text = message.lower()

    if re.search(r"\bthis\s+weekend\b", text):
        sat_delta = (5 - today.weekday()) % 7
        sat = today + timedelta(days=sat_delta)
        if today.weekday() == 6:
            return now_local, 1, "this weekend"
        start = datetime.combine(sat, time.min, tzinfo=now_local.tzinfo)
        if sat == today:
            start = now_local
        days = 1 + (6 - sat.weekday())
        return start, days, "this weekend"

    if re.search(r"\bnext\s+week\b", text):
        next_mon = today + timedelta(days=(7 - today.weekday()))
        start = datetime.combine(next_mon, time.min, tzinfo=now_local.tzinfo)
        return start, 7, "next week"

    if re.search(r"\btomorrow\b", text):
        tmr = today + timedelta(days=1)
        start = datetime.combine(tmr, time.min, tzinfo=now_local.tzinfo)
        return start, 1, "tomorrow"

    if re.search(r"\btoday\b", text) and not re.search(r"\bthis\s+week\b", text):
        return now_local, 1, "today"

    explicit = _find_explicit_date(message, today)
    weekday_match = _WEEKDAY_RE.search(message)
    if explicit is not None and not re.search(r"\bthis\s+week\b", text):
        start = datetime.combine(explicit, time.min, tzinfo=now_local.tzinfo)
        if explicit == today:
            start = now_local
        return start, 1, explicit.strftime("%A")

    if weekday_match and not re.search(r"\bthis\s+week\b", text):
        weekday = _WEEKDAY_INDEX.get(weekday_match.group(1).lower())
        if weekday is not None:
            target = today + timedelta(days=(weekday - today.weekday()) % 7)
            start = datetime.combine(target, time.min, tzinfo=now_local.tzinfo)
            if target == today:
                start = now_local
            return start, 1, target.strftime("%A")

    remaining = max(1, 7 - today.weekday())
    return now_local, remaining, "this week"


async def _fast_find_time(store: UserStore, message: str) -> dict[str, Any]:
    """Suggest open slots from the cached agenda. No LLM, no calendar write."""
    tz = await tz_for_store(store)
    now_local = datetime.now(tz)
    kind = infer_event_kind(message)
    duration = parse_duration_minutes(message, kind.duration_minutes)
    starts_at, window_days, horizon_label = _horizon_for_message(message, now_local)

    events = await store.agenda.list()
    priorities = await store.priorities.list()
    usuals = await store.usuals.list()

    picks = recommend_slots(
        events=events,
        kind=kind,
        starts_at=starts_at,
        window_days=window_days,
        priorities=priorities,
        usuals=usuals,
        duration_minutes=duration,
        limit=4,
        tz=tz,
    )

    if not picks and horizon_label == "this week":
        picks = recommend_slots(
            events=events,
            kind=kind,
            starts_at=now_local,
            window_days=7,
            priorities=priorities,
            usuals=usuals,
            duration_minutes=duration,
            limit=4,
            tz=tz,
        )
        if picks:
            horizon_label = "the next few days"

    if not picks:
        what = kind.label if kind.label != "that" else "window"
        reply = (
            f"I don\u2019t see an open {what} {horizon_label} "
            f"(about {duration} min, {_kind_hours_phrase(kind)}). "
            f"Want me to look at next week?"
        )
        await _write_reply(store, reply)
        return {"reply": reply, "path": "schedule", "intent": "find_time"}

    hours_note = _kind_hours_phrase(kind)
    what = f"for {kind.label} " if kind.label != "that" else ""
    lines = [
        f"Best times {what}{horizon_label} "
        f"(about {duration} min, {hours_note}):",
        "",
    ]
    for slot in picks:
        local_start = slot.start.astimezone(tz)
        local_end = slot.end.astimezone(tz)
        day = local_start.strftime("%A")
        lines.append(
            f"\u2022 {day} {_fmt_local(local_start)}\u2013{_fmt_local(local_end)}"
        )
    lines.append("")
    lines.append("Say the one you want and I\u2019ll add it.")
    reply = "\n".join(lines)
    title = calendar_title_from_label(kind.label)
    await _write_pending_find(
        store,
        _PendingFind(
            title=title,
            expires_at=(datetime.now(UTC) + timedelta(minutes=PENDING_BOOKING_TTL_MIN)).isoformat(),
            slots=[
                {"start_iso": s.start.isoformat(), "end_iso": s.end.isoformat()}
                for s in picks
            ],
        ),
    )
    logger.info(
        "chat.find_time.ok",
        user=store.user_id,
        kind=kind.label,
        title=title,
        horizon=horizon_label,
        n=len(picks),
    )
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "schedule",
        "intent": "find_time",
        "slots": [
            {
                "start_iso": s.start.isoformat(),
                "end_iso": s.end.isoformat(),
                "local_label": s.local_label,
            }
            for s in picks
        ],
    }


def _kind_hours_phrase(kind: Any) -> str:
    parts: list[str] = []
    for start_h, end_h in kind.windows:
        parts.append(f"{_hour_word(start_h)}\u2013{_hour_word(end_h)}")
    span = parts[0] if len(parts) == 1 else " or ".join(parts)
    if kind.weekdays_only:
        return f"weekdays {span}"
    return span


def _hour_word(hour: int) -> str:
    h = hour % 24
    suffix = "am" if h < 12 else "pm"
    hour_12 = h % 12 or 12
    return f"{hour_12}{suffix}"


def _looks_like_slot_pick(message: str) -> bool:
    """True for a follow-up like 'Sunday 12pm-1pm' or just 'Sunday'."""
    text = message.strip()
    if not text:
        return False
    if _TIME_RANGE_RE.search(text):
        return True
    return bool(
        re.fullmatch(
            r"(?:(?:the|that|go|book|yes)\s+)*"
            r"(?:today|tomorrow|mon(?:day)?|tue(?:s|sday)?|wed(?:s|nesday)?|"
            r"thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
            r"(?:\s+one)?[.!]?",
            text,
            re.IGNORECASE,
        )
    )


async def _try_pick_offered_slot(
    store: UserStore, message: str
) -> dict[str, Any] | None:
    """If we just suggested slots and the user named one, book under that title."""
    if _MOVE_LEAD.search(message) or _CANCEL_LEAD.search(message):
        return None
    if not _looks_like_slot_pick(message):
        return None
    pending = await _read_pending_find(store)
    if pending is None:
        return None

    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    parsed = _parse_time_range(message)
    target = _resolve_target_date(message, today_local)

    offered: list[tuple[datetime, datetime]] = []
    for raw in pending.slots:
        try:
            start = datetime.fromisoformat(raw["start_iso"])
            end = datetime.fromisoformat(raw["end_iso"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        offered.append((start.astimezone(tz), end.astimezone(tz)))
    if not offered:
        await _clear_pending_find(store)
        return None

    if parsed is None and target is not None:
        matches = [(s, e) for s, e in offered if s.date() == target]
    else:
        matches = []
        for start_dt, end_dt in offered:
            if target is not None and start_dt.date() != target:
                continue
            if parsed is not None:
                if not _times_close(start_dt, parsed[0]):
                    continue
                if not _times_close(end_dt, parsed[1]):
                    continue
            elif target is None:
                continue
            matches.append((start_dt, end_dt))

    if len(matches) != 1:
        return None

    start_dt, end_dt = matches[0]
    title = pending.title or "Time block"
    await _clear_pending_find(store)
    logger.info(
        "chat.find_time.picked",
        user=store.user_id,
        title=title,
        start=start_dt.isoformat(),
    )
    return await _propose_cal_change(
        store,
        action="create",
        message=message,
        title=title,
        start_dt=start_dt,
        end_dt=end_dt,
        source_span=message[:200],
        location=None,
        activity=_TITLE_TO_ACTIVITY.get(title),
    )


async def _fast_create(store: UserStore, message: str) -> dict[str, Any]:
    parsed = _parse_time_range(message)
    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    target = _resolve_target_date(message, today_local)
    if parsed is None or target is None:
        return await _clarify(
            store,
            "Tell me the day and time range to add (e.g. \u201cTuesday 7:45\u20138:22am dropoff\u201d).",
        )
    start_t, end_t = parsed
    start_dt = datetime.combine(target, start_t, tzinfo=tz)
    end_dt = datetime.combine(target, end_t, tzinfo=tz)
    pending_find = await _read_pending_find(store)
    title = (
        _title_from_message(message)
        or _title_from_plan_label(message)
        or (pending_find.title if pending_find else None)
        or await _title_from_usual_category(store, target.weekday(), start_t.hour)
        or "Time block"
    )
    if pending_find:
        await _clear_pending_find(store)
    logger.info(
        "chat.book.fast_hit",
        user=store.user_id,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        title=title,
    )
    return await _propose_cal_change(
        store,
        action="create",
        message=message,
        title=title,
        start_dt=start_dt,
        end_dt=end_dt,
        source_span=message[:200],
        location=None,
        activity=_TITLE_TO_ACTIVITY.get(title),
    )


async def _fast_delete(store: UserStore, message: str) -> dict[str, Any]:
    parsed = _parse_time_range(message)
    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    target = _resolve_target_date(message, today_local) or today_local
    title_hint = _title_from_message(message)
    if parsed is None and not title_hint:
        return await _clarify(
            store,
            "Tell me which event to remove \u2014 a title and time helps (e.g. \u201cremove Dropoff 7:45\u20138:22am today\u201d).",
        )
    start_t, end_t = parsed if parsed else (None, None)
    matches = await _match_agenda_events(
        store, day=target, start_t=start_t, end_t=end_t, title_hint=title_hint
    )
    if not matches:
        when = f"{_fmt_hm_time(start_t)}\u2013{_fmt_hm_time(end_t)} " if start_t and end_t else ""
        day_label = "today" if target == today_local else target.strftime("%A, %b %-d")
        return await _clarify(store, f"I couldn\u2019t find {when}on {day_label}.")
    if len(matches) > 1:
        listed = "\n".join(
            f"\u2022 {e.summary} ({_fmt_local(e.time.start.astimezone(tz))}\u2013{_fmt_local(e.time.end.astimezone(tz))})"
            for e in matches[:4]
        )
        return await _clarify(store, f"A few events match. Which one should I remove?\n\n{listed}")

    event = matches[0]
    tzinfo = tz
    start_dt = event.time.start.astimezone(tzinfo)
    end_dt = event.time.end.astimezone(tzinfo)
    return await _propose_cal_change(
        store,
        action="delete",
        message=message,
        title=event.summary,
        start_dt=start_dt,
        end_dt=end_dt,
        source_span=message[:200],
        location=None,
        event_id=event.event_id,
        calendar_id=event.calendar_id or "primary",
        activity=event.activity_type,
    )


async def _fast_move(store: UserStore, message: str) -> dict[str, Any]:
    tz = await tz_for_store(store)
    today_local = datetime.now(tz).date()
    parts = re.split(r"\bto\b", message, maxsplit=1, flags=re.IGNORECASE)
    src_text = parts[0]
    dest_text = parts[1].strip() if len(parts) == 2 else ""

    src_day = _resolve_target_date(src_text, today_local) or today_local
    dest_day = _resolve_target_date(dest_text, today_local) if dest_text else None
    dest_has_range = _parse_time_range(dest_text) is not None if dest_text else False
    ranges = _parse_all_time_ranges(message)

    if len(ranges) >= 2:
        src_range, dest_range = ranges[0], ranges[1]
    elif dest_has_range:
        dest_range = _parse_time_range(dest_text)
        src_range = _parse_time_range(src_text)
    elif dest_day is not None:
        src_range = ranges[0] if ranges else None
        dest_range = None
    else:
        src_range = ranges[0] if ranges else None
        dest_range = None

    title_hint = _title_from_message(src_text) or _title_from_message(message)

    if dest_day is None and dest_range is None:
        return await _clarify(
            store,
            "Where should I move it? Give the new day and time (e.g. \u201cmove Dropoff to Thursday 8:00\u20138:30am\u201d).",
        )

    src_start, src_end = src_range if src_range else (None, None)
    matches = await _match_agenda_events(
        store, day=src_day, start_t=src_start, end_t=src_end, title_hint=title_hint
    )
    if not matches:
        return await _clarify(
            store,
            "I couldn\u2019t find that event to move. Name the current day and time, or the title.",
        )
    if len(matches) > 1:
        listed = "\n".join(
            f"\u2022 {e.summary} ({_fmt_local(e.time.start.astimezone(tz))}\u2013{_fmt_local(e.time.end.astimezone(tz))})"
            for e in matches[:4]
        )
        return await _clarify(store, f"A few events match. Which one should I move?\n\n{listed}")

    event = matches[0]
    old_start = event.time.start.astimezone(tz)
    old_end = event.time.end.astimezone(tz)
    duration = old_end - old_start
    new_day = dest_day or old_start.date()
    if dest_range:
        new_start = datetime.combine(new_day, dest_range[0], tzinfo=tz)
        new_end = datetime.combine(new_day, dest_range[1], tzinfo=tz)
    else:
        new_start = datetime.combine(new_day, old_start.time(), tzinfo=tz)
        new_end = new_start + duration

    return await _propose_cal_change(
        store,
        action="move",
        message=message,
        title=event.summary,
        start_dt=new_start,
        end_dt=new_end,
        source_span=message[:200],
        location=None,
        event_id=event.event_id,
        calendar_id=event.calendar_id or "primary",
        old_start_dt=old_start,
        old_end_dt=old_end,
        activity=event.activity_type,
    )


# Keywords the user is likely to type as the "kind" of thing they're
# booking. We reflect their word back rather than inventing a title. If
# multiple hit, the first (left-most in the message) wins.
_TITLE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdrop[\s-]?off\b", re.IGNORECASE), "Dropoff"),
    (re.compile(r"\bpick[\s-]?up\b", re.IGNORECASE), "Pickup"),
    (re.compile(r"\blunch\b", re.IGNORECASE), "Lunch"),
    (re.compile(r"\bdinner\b", re.IGNORECASE), "Dinner"),
    (re.compile(r"\bbrunch\b", re.IGNORECASE), "Brunch"),
    (re.compile(r"\bbreakfast\b", re.IGNORECASE), "Breakfast"),
    (re.compile(r"\bcoffee\b", re.IGNORECASE), "Coffee"),
    (re.compile(r"\bschool\b", re.IGNORECASE), "School"),
    (re.compile(r"\btherapy\b", re.IGNORECASE), "Therapy"),
    (re.compile(r"\b(?:dentist|doctor|medical|appointment|appt)\b", re.IGNORECASE), "Medical"),
    (re.compile(r"\b(?:soccer|basketball|swim|sports?)\b", re.IGNORECASE), "Sports"),
    (re.compile(r"\bwork\b", re.IGNORECASE), "Work"),
    (re.compile(r"\bcommute\b", re.IGNORECASE), "Commute"),
    (re.compile(r"\bfamily\b", re.IGNORECASE), "Family"),
    (re.compile(r"\bpersonal\b", re.IGNORECASE), "Personal"),
    (re.compile(r"\b(?:meeting|call|standup|1:1)\b", re.IGNORECASE), "Meeting"),
]


def _title_from_message(message: str) -> str | None:
    """Return a canonical title for the first activity keyword the user typed.

    We use the user's words rather than inventing prose from the matched
    usual, so titles are honest and predictable.
    """
    best: tuple[int, str] | None = None
    for pattern, label in _TITLE_KEYWORDS:
        m = pattern.search(message)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), label)
    return best[1] if best else None


def _title_from_plan_label(message: str) -> str | None:
    """Title from 'book a lunch event next Wednesday' when no keyword hit."""
    label = plan_label_from_message(message)
    if not label:
        return None
    title = calendar_title_from_label(label)
    title = re.sub(r"\s+events?\s*$", "", title, flags=re.I).strip()
    if len(title) < 2:
        return None
    return title


async def _title_from_usual_category(
    store: UserStore, weekday: int, start_hour: int
) -> str | None:
    """Fallback title: coarse Category label of a matching usual (e.g.
    "Dropoff", "School"). Never surfaces the raw display_summary which
    contains noisy auto-derived text like "N \u2192 BrightStart".
    """
    band = hour_to_band(start_hour)
    for u in await store.usuals.list():
        if int(u.weekday) == weekday and u.hour_band == band:
            return u.activity_type.category.label
    return None


def _compose_time(num_raw: str, suffix_raw: str | None) -> time:
    """Compose a `time` from a numeric part ("7", "7:45") and optional am/pm.

    If `suffix_raw` is None we fall back to a small heuristic: 6-11 -> AM,
    everything else -> PM. This lets us parse "3-4" as 3pm-4pm without
    forcing users to write it out.
    """
    s = num_raw.strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", s)
    if not m:
        raise ValueError(f"unparseable time: {num_raw!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = suffix_raw.lower() if suffix_raw else ("am" if 6 <= hour <= 11 else "pm")
    if hour == 12:
        hour = 0
    if suffix == "pm":
        hour += 12
    if not (0 <= hour < 24) or not (0 <= minute < 60):
        raise ValueError(f"out-of-range time: {num_raw!r}")
    return time(hour=hour, minute=minute)


async def _finalize_cal_change(
    store: UserStore,
    *,
    action: Literal["create", "move", "delete"],
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    source_span: str,
    location: str | None,
    event_id: str | None = None,
    calendar_id: str | None = None,
    old_start_dt: datetime | None = None,
    old_end_dt: datetime | None = None,
) -> dict[str, Any]:
    """Write the create / move / delete to Google Calendar and refresh the
    local agenda. Shared by the fast path, LLM book path, and confirm-yes.
    """
    tokens = await store.tokens.read() or {}
    if not tokens.get("access_token"):
        logger.info("chat.cal.no_google", user=store.user_id, action=action)
        return await _ack_no_agent(
            store,
            "I can\u2019t reach your Google Calendar yet \u2014 connect Google in Sources and try again.",
        )

    cal_id = calendar_id or "primary"
    booked_id = event_id
    html_link = ""
    try:
        if action == "create":
            booked = await book_event(
                store,
                summary=title,
                start=start_dt,
                end=end_dt,
                reason=f"chat: {source_span}",
                location=location,
            )
            booked_id = booked.event_id
            html_link = booked.html_link
        elif action == "move":
            if not event_id:
                return await _ack_no_agent(store, "I lost track of which event to move. Try again?")
            booked = await move_event(
                store,
                event_id=event_id,
                start=start_dt,
                end=end_dt,
                calendar_id=cal_id,
            )
            booked_id = booked.event_id
            html_link = booked.html_link
        else:
            if not event_id:
                return await _ack_no_agent(store, "I lost track of which event to remove. Try again?")
            await delete_event(store, event_id=event_id, calendar_id=cal_id)
            await store.agenda.delete(event_id)
    except Exception as err:
        logger.exception(
            "chat.cal.google_failed",
            user=store.user_id,
            action=action,
            title=title,
            event_id=event_id,
            err=str(err),
        )
        verb = {"create": "booking", "move": "move", "delete": "delete"}[action]
        return await _ack_no_agent(
            store,
            f"Google didn\u2019t accept the {verb} ({err}). Try again in a moment?",
        )

    asyncio.create_task(_refresh_after_book(store))

    day_label = start_dt.strftime("%A, %b %-d")
    time_label = f"{_fmt_local(start_dt)}\u2013{_fmt_local(end_dt)}"
    if action == "create":
        reply = f"Added \u201c{title}\u201d to your calendar for {day_label}, {time_label}."
        intent = "book_now"
    elif action == "move":
        if old_start_dt is not None:
            old_day = old_start_dt.strftime("%A, %b %-d")
            old_time = f"{_fmt_local(old_start_dt)}\u2013{_fmt_local(old_end_dt or old_start_dt)}"
            reply = (
                f"Moved \u201c{title}\u201d from {old_day}, {old_time} "
                f"to {day_label}, {time_label}."
            )
        else:
            reply = f"Moved \u201c{title}\u201d to {day_label}, {time_label}."
        intent = "move"
    else:
        reply = f"Removed \u201c{title}\u201d from {day_label}, {time_label}."
        intent = "cancel"

    logger.info(
        "chat.cal.ok",
        user=store.user_id,
        action=action,
        title=title,
        event_id=booked_id,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
    )
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "schedule",
        "intent": intent,
        "event_id": booked_id,
        "html_link": html_link,
    }


async def _refresh_after_book(store: UserStore) -> None:
    """Background: pull latest agenda + re-enrich. Never let this raise."""
    try:
        refresh = await refresh_agenda(store)
        if refresh.fingerprint_changed:
            await enrich_agenda(store)
    except Exception as err:
        logger.warning("chat.book.post_sync_failed", user=store.user_id, err=str(err))


# =============================================================================
# Email from chat: resolve saved contacts (Nova's teacher), ask only when
# several match, then draft for edit-before-send.
# =============================================================================

PENDING_EMAIL_PICK_KEY = "pending_email_pick"
PENDING_EMAIL_DRAFT_KEY = "pending_email_draft"
PENDING_EMAIL_TTL_MIN = 10


class _PendingEmailPick(BaseModel):
    intent: str
    expires_at: str
    candidates: list[dict[str, str | None]] = Field(default_factory=list)


async def _try_fast_email(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any] | None:
    if not is_email_request(message):
        return None
    return await _handle_email_request(store, message, history)


async def _handle_email_request(
    store: UserStore, message: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    # ADK hot-path: when LEVEL_ADK_MODE=true, ask the ADK LlmAgent to
    # pick the tool BEFORE we run any resolution. The planner audit row
    # is what /admin/traces uses to render the "router -> planner -> tool"
    # waterfall for the demo video. When disabled, this is a no-op.
    if is_adk_enabled():
        plan = await plan_and_dispatch(
            store=store,
            intent="send_email",
            user_message=message,
            trace_id=f"chat_{message[:20]}",
        )
        logger.info(
            "chat.email.adk_plan",
            user=store.user_id,
            tool=plan.tool,
            used_adk=plan.used_adk,
            fallback=plan.fallback_reason,
        )
    people = await store.people.list()
    contacts = await store.contacts.list()
    resolved = resolve_email_targets(message, people, contacts, history)
    if resolved.status == "ask":
        await _write_pending_email_pick(store, message, resolved.candidates)
        await _write_reply(store, resolved.reply)
        return {
            "reply": resolved.reply,
            "path": "email",
            "intent": "send_email",
            "needs_confirm": True,
        }
    if resolved.status != "match":
        await _clear_pending_email_pick(store)
        await _write_reply(store, resolved.reply)
        return {"reply": resolved.reply, "path": "email", "intent": "send_email"}
    await _clear_pending_email_pick(store)
    return await _draft_for_candidate(store, resolved.candidates[0], message)


async def _handle_pending_email_pick(
    store: UserStore, message: str
) -> dict[str, Any] | None:
    pending = await _read_pending_email_pick(store)
    if pending is None:
        return None
    if is_email_request(message):
        return None
    people = await store.people.list()
    contacts = await store.contacts.list()
    by_id = {c.contact_id: c for c in contacts}
    people_by_id = {p.person_id: p for p in people}
    candidates: list[EmailCandidate] = []
    for raw in pending.candidates:
        contact = by_id.get(str(raw.get("contact_id") or ""))
        if not contact:
            continue
        candidates.append(
            EmailCandidate(contact=contact, person=people_by_id.get(contact.person_id))
        )
    picked = pick_candidate(message, candidates)
    if picked is None:
        return None
    await _clear_pending_email_pick(store)
    return await _draft_for_candidate(store, picked, pending.intent)


async def _draft_for_candidate(
    store: UserStore, candidate: EmailCandidate, intent: str
) -> dict[str, Any]:
    contact = candidate.contact
    person = candidate.person
    kid = person.display_name if person else None
    if not contact.email:
        reply = (
            f"I have {candidate.label}, but no email address yet. "
            "Add it on Contacts and I\u2019ll draft this."
        )
        await _write_reply(store, reply)
        return {"reply": reply, "path": "email", "intent": "send_email"}

    drafted = await draft_email(
        store,
        intent=intent,
        contact_display_name=contact.name,
        kid_display_name=kid,
        extra_notes=intent,
    )

    register_pending_draft(drafted.confirmation_token, str(contact.email))
    await _write_pending_email_draft(
        store,
        token=drafted.confirmation_token,
        to=str(contact.email),
        subject=drafted.subject,
        contact_name=contact.name,
        person_name=kid,
        kind=contact.kind.value,
    )
    reply = (
        f"Draft for {candidate.label}. Edit anything you want, then send \u2014 "
        "I won\u2019t send until you do."
    )
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "email",
        "intent": "send_email",
        "email_draft": {
            "to": str(contact.email),
            "subject": drafted.subject,
            "body": drafted.body,
            "confirmation_token": drafted.confirmation_token,
            "contact_name": contact.name,
            "person_name": kid,
            "kind": contact.kind.value,
        },
    }


async def _read_pending_email_pick(store: UserStore) -> _PendingEmailPick | None:
    profile = await store.profile.read() or {}
    raw = profile.get(PENDING_EMAIL_PICK_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        pending = _PendingEmailPick.model_validate(raw)
    except Exception:
        await _clear_pending_email_pick(store)
        return None
    try:
        expires = datetime.fromisoformat(pending.expires_at)
    except ValueError:
        await _clear_pending_email_pick(store)
        return None
    if expires <= datetime.now(UTC):
        await _clear_pending_email_pick(store)
        return None
    return pending


async def _write_pending_email_pick(
    store: UserStore, intent: str, candidates: list[EmailCandidate]
) -> None:
    profile = dict(await store.profile.read() or {})
    profile[PENDING_EMAIL_PICK_KEY] = _PendingEmailPick(
        intent=intent,
        expires_at=(datetime.now(UTC) + timedelta(minutes=PENDING_EMAIL_TTL_MIN)).isoformat(),
        candidates=[
            {
                "contact_id": c.contact.contact_id,
                "name": c.contact.name,
                "kind": c.contact.kind.value,
                "person_id": c.contact.person_id,
                "person_name": c.person.display_name if c.person else None,
                "email": str(c.contact.email) if c.contact.email else None,
            }
            for c in candidates
        ],
    ).model_dump()
    await store.profile.write(profile)


async def _clear_pending_email_pick(store: UserStore) -> None:
    profile = dict(await store.profile.read() or {})
    if PENDING_EMAIL_PICK_KEY in profile:
        profile.pop(PENDING_EMAIL_PICK_KEY, None)
        await store.profile.write(profile)


async def _write_pending_email_draft(
    store: UserStore,
    *,
    token: str,
    to: str,
    subject: str,
    contact_name: str,
    person_name: str | None,
    kind: str,
) -> None:
    profile = dict(await store.profile.read() or {})
    profile[PENDING_EMAIL_DRAFT_KEY] = {
        "confirmation_token": token,
        "to": to,
        "subject": subject,
        "contact_name": contact_name,
        "person_name": person_name,
        "kind": kind,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=PENDING_EMAIL_TTL_MIN)).isoformat(),
    }
    await store.profile.write(profile)


# =============================================================================
# Confirm-flow: side-effecting calendar writes ask first when they might
# collide with an existing event or a stated priority.
#
# State model: a single "pending booking" lives in `store.profile["pending_
# booking"]` with an expires_at. Human-in-the-loop for calendar writes is a
# core guardrail (never take an external action without a confirmation when
# there's a plausible reason to pause).
# =============================================================================

PENDING_BOOKING_TTL_MIN = 10
PENDING_BOOKING_KEY = "pending_booking"
PENDING_FIND_KEY = "pending_find"


class _PendingFind(BaseModel):
    title: str
    expires_at: str
    slots: list[dict[str, str]] = Field(default_factory=list)


class _PendingBooking(BaseModel):
    action: Literal["create", "move", "delete"] = "create"
    title: str
    start_iso: str
    end_iso: str
    source_span: str
    location: str | None = None
    event_id: str | None = None
    calendar_id: str | None = None
    old_start_iso: str | None = None
    old_end_iso: str | None = None
    conflicts: list[str] = Field(default_factory=list)
    priority_notes: list[str] = Field(default_factory=list)
    expires_at: str  # ISO-8601 UTC


_TITLE_TO_ACTIVITY: dict[str, ActivityType] = {
    "Dropoff": ActivityType.SCHOOL_DROPOFF,
    "Pickup": ActivityType.SCHOOL_PICKUP,
    "Lunch": ActivityType.PERSONAL,
    "Dinner": ActivityType.PERSONAL,
    "Brunch": ActivityType.PERSONAL,
    "Breakfast": ActivityType.PERSONAL,
    "Coffee": ActivityType.PERSONAL,
    "School": ActivityType.SCHOOL_EVENT,
    "Therapy": ActivityType.MEDICAL_THERAPY,
    "Medical": ActivityType.MEDICAL_APPT,
    "Sports": ActivityType.SPORTS_OTHER,
    "Work": ActivityType.WORK,
    "Commute": ActivityType.COMMUTE,
    "Family": ActivityType.FAMILY,
    "Personal": ActivityType.PERSONAL,
    "Meeting": ActivityType.WORK,
}


async def _propose_cal_change(
    store: UserStore,
    *,
    action: Literal["create", "move", "delete"],
    message: str,
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    source_span: str,
    location: str | None,
    event_id: str | None = None,
    calendar_id: str | None = None,
    old_start_dt: datetime | None = None,
    old_end_dt: datetime | None = None,
    activity: ActivityType | None = None,
) -> dict[str, Any]:
    """Check conflicts and priority overlaps, then either write immediately
    or stash a pending change and ask the user to confirm.
    """
    ignore_ids = {event_id} if event_id else set()
    overlapping: list[CachedEvent] = []
    if action != "delete":
        overlapping = await _overlapping_events(
            store, start_dt, end_dt, ignore_event_ids=ignore_ids
        )
    conflicts = _conflict_labels(overlapping, start_dt.tzinfo)
    priority_notes = await _find_priority_notes(
        store,
        message=message,
        title=title,
        overlapping=overlapping,
        activity=activity,
    )

    if not conflicts and not priority_notes:
        return await _finalize_cal_change(
            store,
            action=action,
            title=title,
            start_dt=start_dt,
            end_dt=end_dt,
            source_span=source_span,
            location=location,
            event_id=event_id,
            calendar_id=calendar_id,
            old_start_dt=old_start_dt,
            old_end_dt=old_end_dt,
        )

    expires_at = (datetime.now(UTC) + timedelta(minutes=PENDING_BOOKING_TTL_MIN)).isoformat()
    pending = _PendingBooking(
        action=action,
        title=title,
        start_iso=start_dt.isoformat(),
        end_iso=end_dt.isoformat(),
        source_span=source_span,
        location=location,
        event_id=event_id,
        calendar_id=calendar_id,
        old_start_iso=old_start_dt.isoformat() if old_start_dt else None,
        old_end_iso=old_end_dt.isoformat() if old_end_dt else None,
        conflicts=conflicts,
        priority_notes=priority_notes,
        expires_at=expires_at,
    )
    await _write_pending(store, pending)

    day_label = start_dt.strftime("%A, %b %-d")
    time_label = f"{_fmt_local(start_dt)}\u2013{_fmt_local(end_dt)}"
    if action == "create":
        lead = f"About to book \u201c{title}\u201d for {day_label}, {time_label}."
        ask = "Book it anyway? Reply \u201cyes\u201d to confirm or \u201cno\u201d to skip."
    elif action == "move":
        if old_start_dt is not None:
            old_day = old_start_dt.strftime("%A, %b %-d")
            old_time = f"{_fmt_local(old_start_dt)}\u2013{_fmt_local(old_end_dt or old_start_dt)}"
            lead = (
                f"About to move \u201c{title}\u201d from {old_day}, {old_time} "
                f"to {day_label}, {time_label}."
            )
        else:
            lead = f"About to move \u201c{title}\u201d to {day_label}, {time_label}."
        ask = "Move it anyway? Reply \u201cyes\u201d to confirm or \u201cno\u201d to skip."
    else:
        lead = f"About to remove \u201c{title}\u201d from {day_label}, {time_label}."
        ask = "Remove it anyway? Reply \u201cyes\u201d to confirm or \u201cno\u201d to skip."

    parts: list[str] = [lead]
    if conflicts:
        shown = conflicts[:3]
        if len(shown) == 1:
            parts.append(f"That overlaps {shown[0]}.")
        else:
            bullets = "\n".join(f"\u2022 {c}" for c in shown)
            parts.append(f"That overlaps:\n{bullets}")
    if priority_notes:
        shown = priority_notes[:2]
        quoted = "\n".join(f"\u201c{n}\u201d" for n in shown)
        parts.append(f"You also said:\n{quoted}")
    parts.append(ask)
    reply = "\n\n".join(parts)

    logger.info(
        "chat.cal.needs_confirm",
        user=store.user_id,
        action=action,
        title=title,
        conflicts=len(conflicts),
        priorities=len(priority_notes),
    )
    await _write_reply(store, reply)
    return {
        "reply": reply,
        "path": "schedule",
        "intent": "confirm_cal",
        "needs_confirm": True,
        "pending": pending.model_dump(),
    }


async def _handle_pending_confirmation(
    store: UserStore, message: str
) -> dict[str, Any] | None:
    """If there's a live pending calendar change and this message is yes/no,
    handle it and return a response. Otherwise return None and let normal
    flow run.
    """
    pending = await _read_pending(store)
    if pending is None:
        return None

    # Expired -> clear silently, don't hijack the message.
    try:
        expires = datetime.fromisoformat(pending.expires_at)
    except ValueError:
        expires = datetime.now(UTC) - timedelta(seconds=1)
    if expires <= datetime.now(UTC):
        await _clear_pending(store)
        return None

    if _is_affirmative(message):
        try:
            start_dt = datetime.fromisoformat(pending.start_iso)
            end_dt = datetime.fromisoformat(pending.end_iso)
        except ValueError:
            await _clear_pending(store)
            return None
        old_start = (
            datetime.fromisoformat(pending.old_start_iso)
            if pending.old_start_iso
            else None
        )
        old_end = (
            datetime.fromisoformat(pending.old_end_iso)
            if pending.old_end_iso
            else None
        )
        await _clear_pending(store)
        logger.info(
            "chat.cal.confirmed",
            user=store.user_id,
            action=pending.action,
            title=pending.title,
            start=pending.start_iso,
        )
        return await _finalize_cal_change(
            store,
            action=pending.action,
            title=pending.title,
            start_dt=start_dt,
            end_dt=end_dt,
            source_span=f"confirmed: {pending.source_span}",
            location=pending.location,
            event_id=pending.event_id,
            calendar_id=pending.calendar_id,
            old_start_dt=old_start,
            old_end_dt=old_end,
        )

    if _is_negative(message):
        await _clear_pending(store)
        if pending.action == "move":
            reply = f"Left \u201c{pending.title}\u201d where it is."
        elif pending.action == "delete":
            reply = f"Left \u201c{pending.title}\u201d on your calendar."
        else:
            reply = f"Skipped \u201c{pending.title}\u201d. Nothing added to your calendar."
        logger.info(
            "chat.cal.declined",
            user=store.user_id,
            action=pending.action,
            title=pending.title,
        )
        await _write_reply(store, reply)
        return {
            "reply": reply,
            "path": "schedule",
            "intent": "confirm_cal",
            "confirmed": False,
        }

    # Not a yes/no reply. Leave pending in place (TTL will expire it) and
    # let the normal dispatch handle whatever the user actually said.
    return None


async def _overlapping_events(
    store: UserStore,
    start_dt: datetime,
    end_dt: datetime,
    *,
    ignore_event_ids: set[str] | None = None,
) -> list[CachedEvent]:
    skip = ignore_event_ids or set()
    out: list[CachedEvent] = []
    for e in await store.agenda.list():
        if e.time.all_day or e.event_id in skip:
            continue
        if e.time.start < end_dt and e.time.end > start_dt:
            out.append(e)
    return out


def _conflict_labels(events: list[CachedEvent], tzinfo: Any) -> list[str]:
    labels: list[str] = []
    for e in events:
        local_start = e.time.start.astimezone(tzinfo)
        local_end = e.time.end.astimezone(tzinfo)
        labels.append(f"{e.summary} ({_fmt_local(local_start)}\u2013{_fmt_local(local_end)})")
    return labels


async def _find_conflicts(
    store: UserStore,
    start_dt: datetime,
    end_dt: datetime,
    *,
    ignore_event_ids: set[str] | None = None,
) -> list[str]:
    """Return short human labels for cached events that overlap [start,end)."""
    overlapping = await _overlapping_events(
        store, start_dt, end_dt, ignore_event_ids=ignore_event_ids
    )
    return _conflict_labels(overlapping, start_dt.tzinfo)


_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "into", "over", "this", "that",
        "have", "has", "had", "was", "were", "will", "would", "should", "could",
        "on", "of", "in", "at", "to", "by", "an", "or", "as", "is", "it", "be",
        "am", "pm", "off", "up", "my", "me",
        # Date/time words must not count as a "topic". Otherwise booking a
        # random Sunday slot matches "Never miss Sunday Physical Therapy".
        "never", "miss", "always", "please", "dont",
        "time", "block", "hour", "hours", "week", "weeks", "day", "days",
        "today", "tomorrow", "tonight", "next", "last",
        "mon", "monday", "tue", "tues", "tuesday", "wed", "weds", "wednesday",
        "thu", "thur", "thurs", "thursday", "fri", "friday",
        "sat", "saturday", "sun", "sunday",
        "jan", "january", "feb", "february", "mar", "march", "apr", "april",
        "may", "jun", "june", "jul", "july", "aug", "august",
        "sep", "sept", "september", "oct", "october", "nov", "november",
        "dec", "december",
    }
)


def _keywords(text: str) -> set[str]:
    """Rough content-word set for cheap semantic overlap between two strings.

    Lowercased, alphanumerics only, len>=3, drops common stopwords. Not
    linguistic - just enough to catch "elder care" priority <-> "Elder Care
    visit" event without spuriously matching "the meeting" to "the walk".
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


_CARE_RELATIONS = frozenset(
    {
        CareRelation.CHILD,
        CareRelation.ELDER,
        CareRelation.COPARENT,
        CareRelation.PARTNER,
    }
)

_KINSHIP_RELATIONS: dict[str, frozenset[CareRelation]] = {
    "mom": frozenset({CareRelation.ELDER}),
    "mother": frozenset({CareRelation.ELDER}),
    "mama": frozenset({CareRelation.ELDER}),
    "mum": frozenset({CareRelation.ELDER}),
    "dad": frozenset({CareRelation.ELDER}),
    "daddy": frozenset({CareRelation.ELDER}),
    "father": frozenset({CareRelation.ELDER}),
    "papa": frozenset({CareRelation.ELDER}),
    "parent": frozenset({CareRelation.ELDER}),
    "parents": frozenset({CareRelation.ELDER}),
    "grandma": frozenset({CareRelation.ELDER}),
    "grandpa": frozenset({CareRelation.ELDER}),
    "grandmother": frozenset({CareRelation.ELDER}),
    "grandfather": frozenset({CareRelation.ELDER}),
    "nana": frozenset({CareRelation.ELDER}),
    "kid": frozenset({CareRelation.CHILD}),
    "kids": frozenset({CareRelation.CHILD}),
    "child": frozenset({CareRelation.CHILD}),
    "children": frozenset({CareRelation.CHILD}),
    "son": frozenset({CareRelation.CHILD}),
    "daughter": frozenset({CareRelation.CHILD}),
    "coparent": frozenset({CareRelation.COPARENT}),
    "partner": frozenset({CareRelation.PARTNER}),
}


def _kinship_relations_in(text: str) -> set[CareRelation]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    out: set[CareRelation] = set()
    for word, rels in _KINSHIP_RELATIONS.items():
        if word in words:
            out |= set(rels)
    return out


async def _find_priority_notes(
    store: UserStore,
    *,
    message: str,
    title: str,
    overlapping: list[CachedEvent],
    activity: ActivityType | None = None,
) -> list[str]:
    """Return priority notes only when this change actually touches one.

    Related if:
      (a) the event being written shares an activity_type with the priority, OR
      (b) a known person is named in both the priority and the event/overlap, OR
      (c) the priority names mom/kid/... and an overlapping event is about
          that relation (Helen follow-up vs "never miss time with my mom"), OR
      (d) a FAMILY priority overlaps a care-person event, OR
      (e) the priority shares a content word with the title or overlap.
    """
    activity = activity or _TITLE_TO_ACTIVITY.get(title)
    topic_words = _keywords(title)
    for event in overlapping:
        topic_words |= _keywords(event.summary or "")
    conflict_text = " ".join(e.summary or "" for e in overlapping)

    people = await store.people.list()
    people_by_id = {p.person_id: p for p in people}
    conflict_people = []
    seen_pids: set[str] = set()
    for event in overlapping:
        for pid in event.matched_person_ids:
            person = people_by_id.get(pid)
            if person and pid not in seen_pids:
                seen_pids.add(pid)
                conflict_people.append(person)
        hay = (event.summary or "").lower()
        for person in people:
            name = person.display_name.strip().lower()
            if len(name) >= 2 and name in hay and person.person_id not in seen_pids:
                seen_pids.add(person.person_id)
                conflict_people.append(person)

    notes: list[str] = []
    for p in await store.priorities.list():
        if p.status != "kept":
            continue
        if activity is not None and activity in p.activity_types:
            notes.append(p.text)
            continue

        prio_words = _keywords(p.text)
        haystack = f"{message} {title} {conflict_text}".lower()
        prio_l = p.text.lower()

        person_match = False
        for person in people:
            for name in [person.display_name, *(person.aliases or [])]:
                n = name.strip().lower()
                if len(n) >= 2 and n in prio_l and n in haystack:
                    person_match = True
                    break
            if person_match:
                break

        kinship = _kinship_relations_in(p.text)
        kinship_hit = any(person.relation in kinship for person in conflict_people)
        family_hit = ActivityType.FAMILY in p.activity_types and any(
            person.relation in _CARE_RELATIONS for person in conflict_people
        )
        overlap = prio_words & topic_words
        if person_match or kinship_hit or family_hit or overlap:
            notes.append(p.text)
    return notes


_AFFIRMATIVE = re.compile(
    r"^\s*(?:y|yes+|yep|yup|yeah|ok(?:ay)?|sure|please|go|do it|book it|move it|remove it|confirm(?:ed)?)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"^\s*(?:n|no+|nope|nah|cancel|skip|stop|don'?t|nvm|nevermind|never mind)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _is_affirmative(message: str) -> bool:
    return bool(_AFFIRMATIVE.match(message))


def _is_negative(message: str) -> bool:
    return bool(_NEGATIVE.match(message))


async def _read_pending(store: UserStore) -> _PendingBooking | None:
    profile = await store.profile.read() or {}
    raw = profile.get(PENDING_BOOKING_KEY)
    if not raw:
        return None
    try:
        return _PendingBooking.model_validate(raw)
    except Exception:  # noqa: BLE001 - malformed pending shouldn't 500 the chat
        logger.warning("chat.pending.corrupt", user=store.user_id)
        await _clear_pending(store)
        return None


async def _write_pending(store: UserStore, pending: _PendingBooking) -> None:
    profile = await store.profile.read() or {}
    profile[PENDING_BOOKING_KEY] = pending.model_dump()
    await store.profile.write(profile)


async def _clear_pending(store: UserStore) -> None:
    profile = await store.profile.read() or {}
    if PENDING_BOOKING_KEY in profile:
        profile.pop(PENDING_BOOKING_KEY, None)
        await store.profile.write(profile)


async def _read_pending_find(store: UserStore) -> _PendingFind | None:
    profile = await store.profile.read() or {}
    raw = profile.get(PENDING_FIND_KEY)
    if not raw:
        return None
    try:
        pending = _PendingFind.model_validate(raw)
    except Exception:  # noqa: BLE001
        logger.warning("chat.pending_find.corrupt", user=store.user_id)
        await _clear_pending_find(store)
        return None
    try:
        expires = datetime.fromisoformat(pending.expires_at)
    except ValueError:
        expires = datetime.now(UTC) - timedelta(seconds=1)
    if expires <= datetime.now(UTC):
        await _clear_pending_find(store)
        return None
    return pending


async def _write_pending_find(store: UserStore, pending: _PendingFind) -> None:
    profile = await store.profile.read() or {}
    profile[PENDING_FIND_KEY] = pending.model_dump()
    await store.profile.write(profile)


async def _clear_pending_find(store: UserStore) -> None:
    profile = await store.profile.read() or {}
    if PENDING_FIND_KEY in profile:
        profile.pop(PENDING_FIND_KEY, None)
        await store.profile.write(profile)


def _resolve_range(
    booking: Any, today_local: date, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """Pick the concrete date (from iso_date, weekday, or default=today) and
    combine with the extracted HH:MM start/end. Returns tz-aware datetimes.
    Raises ValueError if the times don't parse or end <= start.

    Weekday resolution mirrors the fast-path: THIS WEEK's occurrence, even
    if the time has already passed today. Retroactive bookings are a valid
    use case; "next Tuesday" is how a user asks for next week.
    """
    start_t = _parse_hhmm(booking.start_hhmm)
    end_t = _parse_hhmm(booking.end_hhmm)
    if end_t <= start_t:
        raise ValueError("end time is not after start time")

    if booking.iso_date:
        target = date.fromisoformat(booking.iso_date)
    elif booking.weekday is not None:
        delta = (booking.weekday - today_local.weekday()) % 7
        target = today_local + timedelta(days=delta)
    else:
        target = today_local

    start_dt = datetime.combine(target, start_t, tzinfo=tz)
    end_dt = datetime.combine(target, end_t, tzinfo=tz)
    return start_dt, end_dt


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def _fmt_local(dt: datetime) -> str:
    fmt = "%-I%p" if dt.minute == 0 else "%-I:%M%p"
    return dt.strftime(fmt).lower()


async def _ack_no_agent(store: UserStore, text: str) -> dict[str, Any]:
    """Short-circuit reply from a non-agent handler.

    Previously this was sync and only shaped the response dict, which
    meant every `return _ack_no_agent(...)` site silently dropped the
    assistant turn from `chat_turns`. The SSE reconstruct path
    (`_load_history`) then rebuilt future prompts without the reply,
    producing "I forgot what we were talking about" behavior after
    soft-degrade, quota, or fast-path acknowledgements. Now it persists.
    """
    await _write_reply(store, text)
    return {"reply": text, "path": "general", "intent": "ask"}


async def _write_reply(store: UserStore, text: str) -> None:
    reply = ChatMessage(
        turn_id=new_id("tout"),
        role=ChatRole.ASSISTANT,
        text=text,
        created_at=datetime.utcnow(),
    )
    await store.chat_turns.upsert(reply)
