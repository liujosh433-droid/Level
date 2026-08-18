"""Reminders CRUD (extract via chat, dismiss via UI)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from level_core.schemas import NegativeAgent, ReminderStatus
from level_core.storage.base import UserStore
from level_core.storage.care_store import record_negative

from level_api.deps import get_user_store

router = APIRouter()


@router.get("")
async def list_reminders(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    return {"reminders": [r.model_dump(mode="json") for r in await store.reminders.list()]}


@router.post("/{reminder_id}/dismiss")
async def dismiss(reminder_id: str, store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    existing = await store.reminders.get(reminder_id)
    if not existing:
        raise HTTPException(status_code=404, detail="not_found")
    updated = await store.reminders.upsert(
        existing.model_copy(update={"status": ReminderStatus.DISMISSED})
    )
    await record_negative(
        store,
        agent=NegativeAgent.REMINDER,
        field="text",
        value=updated.text,
    )
    return {"status": "dismissed"}
