"""Admin: live agent trace view for the demo video."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from level_core.config import get_settings
from level_core.storage.base import UserStore

from level_api.deps import get_user_store

router = APIRouter()


@router.get("/traces")
async def traces(
    limit: int = 50, store: UserStore = Depends(get_user_store)
) -> dict[str, list[dict]]:
    if not get_settings().level_admin_traces_enabled:
        raise HTTPException(status_code=404, detail="disabled")
    entries = [a.model_dump(mode="json") for a in await store.ai_audit.list()]
    entries.sort(key=lambda a: a["created_at"], reverse=True)
    return {"traces": entries[:limit]}
