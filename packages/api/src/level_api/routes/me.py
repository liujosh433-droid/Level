"""Data lifecycle: whoami, export, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from level_core.auth.sessions import SESSION_COOKIE_NAME
from level_core.auth.tokens import clear_tokens
from level_core.storage.base import UserStore

from level_api.deps import get_current_user_id, get_user_store

router = APIRouter()


@router.get("")
async def whoami(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    profile = await store.profile.read() or {}
    tokens_present = bool((await store.tokens.read() or {}).get("access_token"))
    return {
        "user_id": store.user_id,
        "email": profile.get("email"),
        "google_connected": tokens_present,
        "tz": profile.get("tz"),
    }


@router.get("/export")
async def export_all(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return {
        "profile": await store.profile.read(),
        "people": [p.model_dump(mode="json") for p in await store.people.list()],
        "usuals": [u.model_dump(mode="json") for u in await store.usuals.list()],
        "priorities": [p.model_dump(mode="json") for p in await store.priorities.list()],
        "reminders": [r.model_dump(mode="json") for r in await store.reminders.list()],
        "contacts": [c.model_dump(mode="json") for c in await store.contacts.list()],
        "chat_turns": [t.model_dump(mode="json") for t in await store.chat_turns.list()],
        "ai_audit": [a.model_dump(mode="json") for a in await store.ai_audit.list()],
    }


@router.delete("")
async def delete_me(
    response: Response,
    user_id: str = Depends(get_current_user_id),
    store: UserStore = Depends(get_user_store),
) -> dict[str, str]:
    for p in await store.people.list():
        await store.people.delete(p.person_id)
    for u in await store.usuals.list():
        await store.usuals.delete(u.usual_id)
    for p in await store.priorities.list():
        await store.priorities.delete(p.priority_id)
    for r in await store.reminders.list():
        await store.reminders.delete(r.reminder_id)
    for c in await store.contacts.list():
        await store.contacts.delete(c.contact_id)
    for a in await store.agenda.list():
        await store.agenda.delete(a.event_id)
    for t in await store.chat_turns.list():
        await store.chat_turns.delete(t.turn_id)
    for a in await store.ai_audit.list():
        await store.ai_audit.delete(a.audit_id)
    for n in await store.negatives.list():
        await store.negatives.delete(n.negative_id)
    await store.calendar_sync.write({})
    await store.profile.write({})
    await clear_tokens(store)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "wiped", "user_id": user_id}
