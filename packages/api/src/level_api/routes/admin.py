"""Admin: live agent traces + store snapshot for the demo video."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.config import get_settings
from level_core.storage.base import UserStore

from level_api.deps import get_user_store

router = APIRouter()


def _require_admin() -> None:
    if not get_settings().level_admin_traces_enabled:
        raise HTTPException(status_code=404, detail="disabled")


@router.get("/traces")
async def traces(
    limit: int = 50, store: UserStore = Depends(get_user_store)
) -> dict[str, list[dict]]:
    _require_admin()
    entries = [a.model_dump(mode="json") for a in await store.ai_audit.list()]
    entries.sort(key=lambda a: a["created_at"], reverse=True)
    return {"traces": entries[:limit]}


@router.get("/store")
async def store_snapshot(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    """Live per-user JSON the demo inspector diffs as Level writes."""
    _require_admin()
    profile = dict(await store.profile.read() or {})
    agenda = await store.agenda.list()
    agenda.sort(key=lambda e: e.time.start, reverse=True)
    chat = await store.chat_turns.list()
    chat.sort(key=lambda t: t.created_at, reverse=True)
    negatives = await store.negatives.list()
    negatives.sort(key=lambda n: n.created_at, reverse=True)

    def _event(e: Any) -> dict[str, Any]:
        return {
            "event_id": e.event_id,
            "summary": e.summary,
            "start": e.time.start.isoformat(),
            "end": e.time.end.isoformat(),
            "origin": e.origin,
            "activity_type": e.activity_type,
        }

    return {
        "user_id": store.user_id,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "profile": {
            "email": profile.get("email"),
            "tz": profile.get("tz"),
            "dismissed_missing_week": profile.get("dismissed_missing_week"),
            "resolved_missing_week": profile.get("resolved_missing_week"),
            "pending_booking": profile.get("pending_booking"),
            "pending_find": profile.get("pending_find"),
            "pending_email_pick": profile.get("pending_email_pick"),
            "pending_email_draft": profile.get("pending_email_draft"),
            "calendar_window_days_back": profile.get("calendar_window_days_back"),
            "calendar_window_days_forward": profile.get("calendar_window_days_forward"),
        },
        "people": [p.model_dump(mode="json") for p in await store.people.list()],
        "priorities": [p.model_dump(mode="json") for p in await store.priorities.list()],
        "usuals": [u.model_dump(mode="json") for u in await store.usuals.list()],
        "reminders": [r.model_dump(mode="json") for r in await store.reminders.list()],
        "contacts": [c.model_dump(mode="json") for c in await store.contacts.list()],
        "agenda": {
            "total": len(agenda),
            "level": sum(1 for e in agenda if e.origin == "level"),
            "recent": [_event(e) for e in agenda[:20]],
            "level_recent": [_event(e) for e in agenda if e.origin == "level"][:12],
        },
        "chat_turns": [
            {
                "turn_id": t.turn_id,
                "role": t.role,
                "text": (t.text or "")[:180],
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in chat[:8]
        ],
        "negatives": [n.model_dump(mode="json") for n in negatives[:12]],
    }
