"""Google Calendar push webhook + reclassify."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from level_core.calendar.enrich import enrich_agenda, reclassify_all
from level_core.calendar.sync import refresh_agenda
from level_core.calendar.webhook import verify_channel
from level_core.storage.base import UserStore
from level_core.storage.factory import get_store

from level_api.deps import get_user_store

router = APIRouter()


@router.post("/reclassify")
async def reclassify(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    reset = await reclassify_all(store)
    events = await store.agenda.list()
    counts: Counter[str] = Counter()
    for e in events:
        counts[str(e.activity_type) if e.activity_type else "unclassified"] += 1
    return {"reset": reset, "total": len(events), "by_activity": dict(counts)}


@router.get("/summary")
async def activity_summary(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    events = await store.agenda.list()
    counts: Counter[str] = Counter()
    for e in events:
        counts[str(e.activity_type) if e.activity_type else "unclassified"] += 1
    return {
        "total": len(events),
        "by_activity": dict(counts),
        "sample": [
            {"summary": e.summary, "activity_type": e.activity_type}
            for e in events[:12]
        ],
    }


@router.post("/webhook", status_code=204)
async def calendar_webhook(
    request: Request,
    background: BackgroundTasks,
    x_goog_channel_id: str | None = Header(default=None),
    x_goog_channel_token: str | None = Header(default=None),
    x_goog_resource_state: str | None = Header(default=None),
) -> None:
    if x_goog_resource_state == "sync":
        return

    user_id = request.query_params.get("uid")
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_uid")

    store = get_store(user_id)
    if not await verify_channel(
        store, channel_id=x_goog_channel_id, channel_token=x_goog_channel_token
    ):
        raise HTTPException(status_code=401, detail="bad_channel")

    async def _refresh_and_enrich() -> None:
        result = await refresh_agenda(store)
        if result.fingerprint_changed:
            await enrich_agenda(store)

    background.add_task(_refresh_and_enrich)
    return None
