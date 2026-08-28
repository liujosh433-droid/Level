"""Data lifecycle: whoami, export, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from level_core.auth.sessions import SESSION_COOKIE_NAME
from level_core.auth.tokens import clear_tokens
from level_core.config import get_settings
from level_core.demo.seeder import is_demo_user
from level_core.schemas import CareRelation
from level_core.storage.base import UserStore
from level_core.storage.care_store import propose_person, set_person_status
from level_core.tz import resolve_tz_name
from pydantic import BaseModel, Field, model_validator

from level_api.deps import get_current_user_id, get_user_store

router = APIRouter()

_GENERIC_SELF = {"you", "me", "self", "myself", "a parent"}


class MePatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    tz: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def _at_least_one(self) -> MePatch:
        if not self.display_name and not self.tz:
            raise ValueError("nothing_to_update")
        return self


async def _self_person(store: UserStore):
    for person in await store.people.list():
        if person.is_self and (person.status or "") != "not_me":
            return person
    return None


async def _whoami_payload(store: UserStore) -> dict[str, Any]:
    profile = await store.profile.read() or {}
    tokens_present = bool((await store.tokens.read() or {}).get("access_token"))
    self_p = await _self_person(store)
    name = ""
    if self_p:
        name = (self_p.display_name or "").strip()
        if name.lower() in _GENERIC_SELF:
            name = ""
    if not name:
        name = str(profile.get("display_name") or "").strip()
    demo = is_demo_user(profile)
    return {
        "user_id": store.user_id,
        "email": profile.get("email"),
        "display_name": name or None,
        # ``google_connected`` gates the frontend's "Connect Google"
        # wall. For a demo user we return True even though the tokens
        # KV is empty - the seeded agenda + people are what actually
        # make the UI usable, and the wall would otherwise trap a
        # judge who bypassed OAuth on purpose.
        "google_connected": tokens_present or demo,
        "demo": demo,
        "demo_scenario": profile.get("demo_scenario") if demo else None,
        "tz": profile.get("tz"),
    }


@router.get("")
async def whoami(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return await _whoami_payload(store)


@router.patch("")
async def patch_me(body: MePatch, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    profile = dict(await store.profile.read() or {})
    if body.tz:
        resolved = resolve_tz_name(body.tz)
        if resolved != body.tz.strip():
            raise HTTPException(status_code=400, detail="invalid_tz")
        profile["tz"] = resolved
    name = (body.display_name or "").strip()
    if not name:
        if body.tz:
            await store.profile.write(profile)
            return await _whoami_payload(store)
        raise HTTPException(status_code=400, detail="name_required")
    profile["display_name"] = name
    await store.profile.write(profile)

    self_p = await _self_person(store)
    if self_p:
        await store.people.upsert(
            self_p.model_copy(update={"display_name": name, "status": "kept"})
        )
    else:
        created = await propose_person(
            store,
            display_name=name,
            relation=CareRelation.SELF,
            is_self=True,
        )
        await set_person_status(store, created.person_id, "kept")
        if created.display_name.strip() != name:
            await store.people.upsert(
                created.model_copy(update={"display_name": name, "is_self": True, "status": "kept"})
            )
    return await _whoami_payload(store)


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
    # Match the flags used to set the cookie (see auth.py) so browsers
    # actually drop it on HTTPS.
    settings = get_settings()
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=not settings.is_local,
        samesite="lax",
    )
    return {"status": "wiped", "user_id": user_id}
