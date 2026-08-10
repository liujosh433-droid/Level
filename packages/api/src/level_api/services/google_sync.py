"""Google onboard + agenda refresh orchestration.

Initial connect may run Memory Bank / Gemini once.
Webhook-driven refreshes are agenda-only (no LLM).
"""

from __future__ import annotations

from level_api.dependencies import (
    cached_calendar_sync_store,
    cached_memory,
    cached_settings,
    cached_token_store,
)
from level_core.auth.google_oauth import credentials_from_token, token_from_credentials
from level_core.calendar.agenda_sync import ensure_calendar_watch, refresh_agenda_cache
from level_core.calendar.sync_state import CalendarSyncState
from level_core.observability.logger import get_logger
from level_core.profile.synthesize import infer_priority_facts
from level_core.schemas.base import _now_utc
from level_core.schemas.signal import Signal

_logger = get_logger(__name__)


async def _refresh_oauth_token(user_id: str):
    tokens = cached_token_store()
    token = await tokens.get_google_token(user_id)
    if token is None:
        return None
    settings = cached_settings()
    creds = credentials_from_token(token)
    refreshed = token_from_credentials(creds, user_id=user_id, settings=settings)
    if refreshed.refresh_token is None and token.refresh_token:
        refreshed = refreshed.model_copy(update={"refresh_token": token.refresh_token})
    await tokens.upsert_token(refreshed)
    return refreshed


async def onboard_google_user(user_id: str) -> None:
    """After OAuth: warm agenda cache, register watch, LLM ingest once if needed."""
    from level_api.routes.sources import (
        _persist_pattern_facts,
        _refresh_profile,
        _run_signals,
    )
    from level_core.ingest.google_live import pull_calendar

    sync_store = cached_calendar_sync_store()
    memory = cached_memory()
    state = await sync_store.get(user_id) or CalendarSyncState(user_id=user_id)

    token = await _refresh_oauth_token(user_id)
    if token is None:
        state = state.model_copy(
            update={
                "initial_sync_done": True,
                "initial_sync_error": "Google token missing after OAuth",
            }
        )
        await sync_store.upsert(state)
        return

    try:
        await refresh_agenda_cache(
            user_id=user_id, token=token, sync_store=sync_store
        )
        await ensure_calendar_watch(
            user_id=user_id, token=token, sync_store=sync_store
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("google_agenda_onboard_failed", user_id=user_id)
        state = await sync_store.get(user_id) or state
        state = state.model_copy(
            update={
                "initial_sync_done": True,
                "initial_sync_error": f"Agenda sync failed: {exc}",
            }
        )
        await sync_store.upsert(state)
        return

    state = await sync_store.get(user_id) or state
    # Agenda is enough for Today; don't leave Sources stuck if LLM is slow/fails.
    state = state.model_copy(update={"initial_sync_done": True, "initial_sync_error": None})
    await sync_store.upsert(state)

    if state.profile_ingested_at is not None:
        # Memory may have been wiped on reload — rebuild bullets from agenda cache.
        from level_api.routes.sources import ensure_profile_from_agenda

        await ensure_profile_from_agenda(
            user_id=user_id, memory=memory, sync_store=sync_store
        )
        _logger.info("google_onboard_skip_llm", user_id=user_id, reason="already_ingested")
        return

    # One-time Memory Bank / profile pass — never called from webhook.
    try:
        signals: list[Signal] = []
        cal = await pull_calendar(token, user_id=user_id, max_events=25)
        signals.extend(cal.signals)
        # Seed priorities from agenda immediately (not calendar-analytics dumps).
        state = await sync_store.get(user_id)
        if state and state.events:
            priority_facts = infer_priority_facts(
                [{"summary": e.summary, "start": e.start} for e in state.events.values()],
                user_id=user_id,
            )
            await _persist_pattern_facts(memory, priority_facts)
            await _refresh_profile(memory, user_id)

        summary = await _run_signals(memory, signals)
        snap = await _refresh_profile(memory, user_id)
        state = await sync_store.get(user_id) or state
        state = state.model_copy(
            update={
                "profile_ingested_at": _now_utc(),
                "initial_sync_done": True,
                "initial_sync_error": summary.detail or None,
            }
        )
        await sync_store.upsert(state)
        _logger.info(
            "google_onboard_llm_done",
            user_id=user_id,
            accepted=summary.accepted,
            bullets=len(snap.bullets),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.exception("google_onboard_llm_failed", user_id=user_id)
        from level_api.routes.sources import ensure_profile_from_agenda

        snap = await ensure_profile_from_agenda(
            user_id=user_id, memory=memory, sync_store=sync_store
        )
        state = await sync_store.get(user_id) or state
        state = state.model_copy(
            update={
                "initial_sync_done": True,
                "profile_ingested_at": (
                    _now_utc() if snap and snap.bullets else state.profile_ingested_at
                ),
                "initial_sync_error": (
                    None
                    if snap and snap.bullets
                    else f"Profile ingest failed: {exc}"
                ),
            }
        )
        await sync_store.upsert(state)


async def agenda_only_refresh(user_id: str) -> None:
    """Webhook path: update agenda cache only. Never Gemini / profile."""
    token = await _refresh_oauth_token(user_id)
    if token is None:
        _logger.warning("agenda_refresh_no_token", user_id=user_id)
        return
    sync_store = cached_calendar_sync_store()
    await refresh_agenda_cache(user_id=user_id, token=token, sync_store=sync_store)
    # Opportunistic channel renewal (no-op if still valid).
    await ensure_calendar_watch(user_id=user_id, token=token, sync_store=sync_store)


__all__ = ["agenda_only_refresh", "onboard_google_user"]
