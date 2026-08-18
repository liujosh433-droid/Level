"""Ingest real personal sources: Google Calendar sync."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel

from level_api.auth_deps import require_user
from level_api.dependencies import (
    get_calendar_sync_store,
    get_memory,
    get_token_store,
)
from level_api.services.google_sync import agenda_only_refresh
from level_core.agents.ingest_normalizer import IngestNormalizer
from level_core.auth.google_oauth import credentials_from_token, token_from_credentials
from level_core.auth.tokens import TokenStore
from level_core.calendar.agenda_sync import ensure_calendar_watch, refresh_agenda_cache
from level_core.calendar.sync_state import CalendarSyncState, CalendarSyncStore, watch_is_live
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.guardrails.inbound import InboundGuardrail
from level_core.ingest.google_live import pull_calendar
from level_core.ingest.pipeline import IngestPipeline
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.persist import (
    persist_care_profile_from_events,
    refresh_persisted_profile,
)
from level_core.schemas.base import _now_utc
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal, SignalSource

router = APIRouter(prefix="/v1/sources", tags=["sources"])
_logger = get_logger(__name__)


class IngestSummary(BaseModel):
    accepted: int = 0
    blocked: int = 0
    skipped: int = 0
    facts: int = 0
    detail: str = ""
    stopped_early: bool = False
    profile_bullets: int = 0
    contradictions: int = 0


def _pipeline(memory: MemoryBank) -> IngestPipeline:
    settings = get_settings()
    return IngestPipeline(
        memory=memory,
        normalizer=IngestNormalizer(
            gemini=build_gemini_client(settings), model_id=settings.fast_model
        ),
        embedder=build_embedding_client(settings),
        guardrail=InboundGuardrail(settings=settings),
    )


async def _refresh_profile(memory: MemoryBank, user_id: str) -> ProfileSnapshot:
    return await refresh_persisted_profile(memory, user_id)


async def _infer_persist_care_profile(
    memory: MemoryBank,
    user_id: str,
    events: list[dict[str, str | None]],
    *,
    embed: bool = True,
) -> ProfileSnapshot | None:
    """Infer Care Profile from agenda events, persist facts + care + snapshot."""
    return await persist_care_profile_from_events(
        memory, user_id, events, embed=embed
    )


async def _run_signals(memory: MemoryBank, signals: list[Signal]) -> IngestSummary:
    pipeline = _pipeline(memory)
    summary = IngestSummary()
    for signal in signals:
        try:
            result = await pipeline.run(signal)
        except ModelUnavailable as exc:
            summary.stopped_early = True
            summary.detail = (
                f"Stopped early — Gemini quota/rate limit: {exc}. "
                f"Accepted {summary.accepted} so far; retry Sync later or use Vertex "
                f"(LEVEL_USE_AI_STUDIO=false)."
            )
            _logger.warning("ingest_stopped_quota", accepted=summary.accepted, error=str(exc))
            return summary
        if result.blocked:
            summary.blocked += 1
        elif result.skipped_duplicate:
            summary.skipped += 1
        elif result.signal is not None:
            summary.accepted += 1
            summary.facts += len(result.facts)
    return summary


class GoogleSyncStatus(BaseModel):
    google_connected: bool = False
    initial_sync_done: bool = False
    profile_ingested: bool = False
    agenda_event_count: int = 0
    watch_active: bool = False
    error: str | None = None


@router.get("/google/status", response_model=GoogleSyncStatus)
async def google_sync_status(
    user_id: str = Depends(require_user),
    tokens: TokenStore = Depends(get_token_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> GoogleSyncStatus:
    token, state = await asyncio.gather(
        tokens.get_google_token(user_id),
        sync_store.get(user_id),
    )
    connected = token is not None and bool(token.refresh_token or token.access_token)
    if not connected:
        return GoogleSyncStatus(google_connected=False)
    if state is None:
        return GoogleSyncStatus(google_connected=True, initial_sync_done=False)
    watch_active = watch_is_live(state)
    return GoogleSyncStatus(
        google_connected=True,
        initial_sync_done=state.initial_sync_done,
        profile_ingested=state.profile_ingested_at is not None,
        agenda_event_count=len(state.events),
        watch_active=watch_active,
        error=state.initial_sync_error,
    )


@router.post("/google/webhook")
async def google_calendar_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> Response:
    """Google Calendar push receiver — queue agenda refresh (fast 204)."""
    channel_id = request.headers.get("X-Goog-Channel-ID") or ""
    resource_state = (request.headers.get("X-Goog-Resource-State") or "").lower()
    channel_token = request.headers.get("X-Goog-Channel-Token") or ""

    if resource_state == "sync":
        return Response(status_code=204)

    if not channel_id:
        return Response(status_code=400)

    state = await sync_store.get_by_channel_id(channel_id)
    if state is None:
        _logger.warning("google_webhook_unknown_channel", channel_id=channel_id)
        return Response(status_code=204)

    if state.channel_token and channel_token and channel_token != state.channel_token:
        _logger.warning("google_webhook_bad_token", channel_id=channel_id)
        return Response(status_code=403)

    # Fast 204. Background: agenda pull, then usuals infer if the hash moved.
    background_tasks.add_task(agenda_only_refresh, state.user_id)
    _logger.info(
        "google_webhook_queued_agenda_refresh",
        user_id=state.user_id,
        channel_id=channel_id,
        resource_state=resource_state,
    )
    return Response(status_code=204)


@router.post("/google/sync", response_model=IngestSummary)
async def sync_google(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> IngestSummary:
    """Optional manual re-ingest (LLM). First connect auto-runs this via onboard."""
    token = await tokens.get_google_token(user_id)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google not connected. Visit /v1/auth/google/start first.",
        )

    signals: list[Signal] = []
    try:
        # Refresh access token if needed and persist it.
        creds = credentials_from_token(token)
        refreshed = token_from_credentials(creds, user_id=user_id, settings=get_settings())
        if refreshed.refresh_token is None and token.refresh_token:
            refreshed = refreshed.model_copy(update={"refresh_token": token.refresh_token})
        await tokens.upsert_token(refreshed)
        token = refreshed

        # Keep agenda cache fresh without waiting for a webhook.
        await asyncio.gather(
            refresh_agenda_cache(user_id=user_id, token=token, sync_store=sync_store),
            ensure_calendar_watch(user_id=user_id, token=token, sync_store=sync_store),
        )

        state = await sync_store.get(user_id)
        if state and state.events:
            priority_events = [
                {"summary": e.summary, "start": e.start} for e in state.events.values()
            ]
            # Prefer agenda cache for care; still pull a thin signal set for ingest.
            cal = await pull_calendar(token, user_id=user_id, max_events=25)
            signals.extend(cal.signals)
        else:
            cal = await pull_calendar(token, user_id=user_id, max_events=25)
            signals.extend(cal.signals)
            priority_events = [
                {"summary": (s.text or "").split(": ", 1)[-1], "start": None}
                for s in cal.signals
            ]
        care_snap = await _infer_persist_care_profile(
            memory, user_id, priority_events, embed=True
        )
        pattern_n = len(care_snap.bullets) if care_snap else 0

        _logger.info(
            "google_sync_pulled",
            user_id=user_id,
            calendar=len(cal.signals),
            patterns=pattern_n,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("google_sync_failed", user_id=user_id)
        raise HTTPException(status_code=502, detail=f"Google sync failed: {exc}") from exc

    summary = await _run_signals(memory, signals)
    snap = await _refresh_profile(memory, user_id)
    summary.profile_bullets = len(snap.bullets)
    summary.contradictions = len(snap.contradictions)
    cal_n = sum(1 for s in signals if s.source.value == "gcal")
    prefix = (
        f"Pulled {cal_n} calendar events; "
        f"profile {summary.profile_bullets} bullets / {summary.contradictions} tensions. "
        f"Please review the profile below."
    )
    summary.detail = f"{prefix} {summary.detail}".strip() if summary.detail else prefix

    prior = await sync_store.get(user_id)
    state = prior or CalendarSyncState(user_id=user_id)
    await sync_store.upsert(
        state.model_copy(
            update={
                "profile_ingested_at": _now_utc(),
                "initial_sync_done": True,
                "initial_sync_error": None,
            }
        )
    )
    return summary


@router.get("/facts", response_model=list[Fact])
async def list_facts(
    limit: int = 50,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> list[Fact]:
    return await memory.facts.list_for_user(user_id=user_id, limit=min(limit, 200))


@router.post("/reset")
async def reset_user_memory(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> dict[str, int | str]:
    """Clear facts/signals/vectors/profile for a user (local in-memory only)."""
    cleared = 0
    for repo in (memory.facts, memory.signals, memory.vectors, memory.manifestos):
        clear = getattr(repo, "clear_for_user", None)
        if callable(clear):
            cleared += int(await clear(user_id=user_id))
    if cleared == 0 and not hasattr(memory.facts, "clear_for_user"):
        raise HTTPException(
            status_code=501,
            detail="Memory reset is only implemented for local in-memory mode.",
        )
    return {"user_id": user_id, "cleared": cleared}


