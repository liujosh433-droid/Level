"""Sources page endpoints: connection status, sync trigger, window slider."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import ensure_watch, refresh_agenda
from level_core.storage.base import UserStore
from pydantic import BaseModel, Field

from level_api.deps import get_user_store

router = APIRouter()


class WindowUpdate(BaseModel):
    days_back: int = Field(ge=1, le=365)
    days_forward: int = Field(ge=1, le=365)


@router.get("/status")
async def status(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    tokens = await store.tokens.read() or {}
    profile = await store.profile.read() or {}
    sync = await store.calendar_sync.read() or {}
    audit_count = len(await store.ai_audit.list())
    return {
        "google_connected": bool(tokens.get("access_token")),
        "email": tokens.get("email"),
        "calendar_id": sync.get("calendar_id"),
        "last_pull_at": sync.get("last_pull_at"),
        "days_back": profile.get("calendar_window_days_back"),
        "days_forward": profile.get("calendar_window_days_forward"),
        "watch": sync.get("watch_channel"),
        "ai_calls_total": audit_count,
    }


@router.post("/sync")
async def sync(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    result = await refresh_agenda(store)
    watch_set = await ensure_watch(store)
    if result.fingerprint_changed:
        enrich = await enrich_agenda(store)
    else:
        enrich = None
    return {
        "refresh": {
            "added": result.added,
            "updated": result.updated,
            "removed": result.removed,
            "total_cached": result.total_cached,
            "fingerprint": result.fingerprint,
            "fingerprint_changed": result.fingerprint_changed,
        },
        "enrich": {
            "classified": enrich.classified if enrich else 0,
            "people_matched": enrich.people_matched if enrich else 0,
            "reminders_matched": enrich.reminders_matched if enrich else 0,
        },
        "watch_registered": watch_set,
    }


@router.post("/window")
async def update_window(
    body: WindowUpdate, store: UserStore = Depends(get_user_store)
) -> dict[str, Any]:
    profile = await store.profile.read() or {}
    profile["calendar_window_days_back"] = body.days_back
    profile["calendar_window_days_forward"] = body.days_forward
    await store.profile.write(profile)
    return {"ok": True, **profile}
