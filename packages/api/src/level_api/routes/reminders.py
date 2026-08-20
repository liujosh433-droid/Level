"""Reminders CRUD (extract via chat, dismiss via UI)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.storage.base import UserStore
from level_core.storage.care_store import delete_reminder

from level_api.deps import get_user_store

router = APIRouter()


@router.get("")
async def list_reminders(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return {"reminders": [r.model_dump(mode="json") for r in await store.reminders.list()]}


@router.post("/{reminder_id}/dismiss")
async def dismiss(reminder_id: str, store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    removed = await delete_reminder(store, reminder_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}
