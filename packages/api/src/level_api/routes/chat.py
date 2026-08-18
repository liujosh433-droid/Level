"""Chat: router-driven dispatch, streaming SSE for replies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from level_core.agents.chat_router import run as router_run
from level_core.agents.priority import run as priority_run
from level_core.agents.reminder import run as reminder_run
from level_core.schemas import (
    ActivityType,
    ChatMessage,
    ChatRole,
    ChatRouterIntent,
    ChatRouterPath,
)
from level_core.storage.base import UserStore
from level_core.storage.care_store import (
    add_priority,
    add_reminder,
    new_id,
)
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from level_api.deps import get_user_store

router = APIRouter()


class ChatBody(BaseModel):
    message: str
    include_profile: bool = False


@router.post("/chat")
async def chat(body: ChatBody, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return await _handle_message(store, body.message)


@router.get("/chat/stream")
async def chat_stream(
    message: str, store: UserStore = Depends(get_user_store)
) -> EventSourceResponse:
    async def event_source() -> AsyncIterator[dict[str, Any]]:
        result = await _handle_message(store, message)
        for chunk in _chunk(result["reply"], size=64):
            yield {"event": "delta", "data": json.dumps({"text": chunk})}
            await asyncio.sleep(0.02)
        yield {"event": "done", "data": json.dumps(result)}

    return EventSourceResponse(event_source())


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def _handle_message(store: UserStore, message: str) -> dict[str, Any]:
    turn_in = ChatMessage(
        turn_id=new_id("tin"), role=ChatRole.USER, text=message
    )
    await store.chat_turns.upsert(turn_in)

    decision = await router_run(store=store, user_message=message)
    if not decision.value:
        return _ack_no_agent(store, "I heard you. I'll remember that.")

    path = decision.value.path  # type: ignore[union-attr]
    intent = decision.value.intent  # type: ignore[union-attr]

    if path == ChatRouterPath.PROFILE and intent == ChatRouterIntent.PRIORITY:
        return await _extract_priority(store, message, decision.value)  # type: ignore[arg-type]
    if path == ChatRouterPath.REMINDER and intent == ChatRouterIntent.ADD_REMINDER:
        return await _extract_reminder(store, message, decision.value)  # type: ignore[arg-type]
    if path == ChatRouterPath.PROFILE and intent == ChatRouterIntent.PERSON_UPDATE:
        return await _person_update(store, message, decision.value)  # type: ignore[arg-type]
    if path == ChatRouterPath.SCHEDULE:
        return {
            "reply": (
                "Got it. Ask me to 'find a time' with a duration and I'll suggest slots"
                " that fit your usuals and priorities."
            ),
            "path": path,
            "intent": intent,
        }
    if path == ChatRouterPath.EMAIL:
        return {
            "reply": "Open Contacts and tap the person you'd like to email. I'll draft it and let you edit before it sends.",
            "path": path,
            "intent": intent,
        }
    return {
        "reply": "Noted. I keep an eye on your calendar and remind you when things line up.",
        "path": path,
        "intent": intent,
    }


async def _extract_priority(
    store: UserStore, message: str, decision: Any
) -> dict[str, Any]:
    result = await priority_run(store=store, message=message)
    if not result.value or result.value.priority is None:
        return _ack_no_agent(store, "I hear you. Say more when you're ready.")
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


async def _extract_reminder(
    store: UserStore, message: str, decision: Any
) -> dict[str, Any]:
    result = await reminder_run(store=store, message=message)
    if not result.value or result.value.reminder is None:
        return _ack_no_agent(store, "Tell me the thing you might forget and I'll surface it.")
    er = result.value.reminder
    person_id: str | None = None
    if er.person_display_name:
        for p in await store.people.list():
            if p.display_name.lower() == er.person_display_name.lower() or er.person_display_name.lower() in [a.lower() for a in p.aliases]:
                person_id = p.person_id
                break
    reminder = await add_reminder(
        store,
        text=er.text,
        person_id=person_id,
        activity_type=er.activity_type or ActivityType.OTHER,
        lead_minutes=er.lead_minutes,
        source_span=er.source_span,
    )
    from level_core.calendar.enrich import enrich_agenda

    await enrich_agenda(store)
    await _write_reply(
        store, "Reminder saved. I'll show it whenever a matching event comes up."
    )
    return {
        "reply": f"Reminder saved: '{reminder.text}'. I'll flag it on matching events.",
        "path": "reminder",
        "intent": "add_reminder",
        "reminder_id": reminder.reminder_id,
    }


async def _person_update(store: UserStore, message: str, decision: Any) -> dict[str, Any]:
    return {
        "reply": (
            "Thanks, I'll adjust. You can also open About me and tap 'Not me' or"
            " 'Keep' on anyone I've proposed."
        ),
        "path": "profile",
        "intent": "person_update",
    }


def _ack_no_agent(store: UserStore, text: str) -> dict[str, Any]:
    return {"reply": text, "path": "general", "intent": "ask"}


async def _write_reply(store: UserStore, text: str) -> None:
    reply = ChatMessage(
        turn_id=new_id("tout"),
        role=ChatRole.ASSISTANT,
        text=text,
        created_at=datetime.utcnow(),
    )
    await store.chat_turns.upsert(reply)
