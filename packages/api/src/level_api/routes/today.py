"""Today home — day's calendar + recommendations for busy caregivers."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import get_memory, get_token_store
from level_core.auth.tokens import TokenStore
from level_core.ingest.google_live import fetch_today_events
from level_core.memory.base import MemoryBank
from level_core.profile.today import build_recommendations, format_event_time

router = APIRouter(prefix="/v1/today", tags=["today"])


class TodayEvent(BaseModel):
    id: str
    summary: str
    start: str | None = None
    end: str | None = None
    all_day: bool = False
    when_label: str = ""


class TodayResponse(BaseModel):
    user_id: str
    google_connected: bool
    events: list[TodayEvent] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    profile_ready: bool = False
    needs_review: bool = False
    fact_count: int = 0
    manifesto: str | None = None


@router.get("", response_model=TodayResponse)
async def get_today(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
) -> TodayResponse:
    token = await tokens.get_google_token(user_id)
    google_connected = token is not None and bool(
        token.refresh_token or token.access_token
    )
    events_out: list[TodayEvent] = []
    if token is not None:
        try:
            raw = await fetch_today_events(token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Calendar read failed: {exc}") from exc
        for e in raw:
            events_out.append(
                TodayEvent(
                    id=e.get("id") or "",
                    summary=e.get("summary") or "(no title)",
                    start=e.get("start"),
                    end=e.get("end"),
                    all_day=bool(e.get("all_day")),
                    when_label=format_event_time(
                        e.get("start"), all_day=bool(e.get("all_day"))
                    ),
                )
            )

    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    recs = build_recommendations(
        today_events=[e.model_dump() for e in events_out],
        snapshot=snapshot,
        facts=facts,
    )
    return TodayResponse(
        user_id=user_id,
        google_connected=google_connected,
        events=events_out,
        recommendations=recs,
        profile_ready=bool(snapshot and snapshot.bullets),
        needs_review=bool(snapshot.needs_review) if snapshot else False,
        fact_count=len(facts),
        manifesto=manifesto.statement if manifesto else None,
    )


__all__ = ["router"]
