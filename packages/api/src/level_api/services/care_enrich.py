"""Care Profile infer / usuals refresh — shared by Today, Sources, and Google sync."""

from __future__ import annotations

import asyncio

from level_core.agents.ingest_normalizer import IngestNormalizer
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.calendar.usuals import agenda_fingerprint, usuals_infer_needed
from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.guardrails.inbound import InboundGuardrail
from level_core.ingest.pipeline import IngestPipeline
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_infer_llm import enrich_care_profile_holistic
from level_core.profile.care_store import apply_care, apply_series_usuals
from level_core.profile.people_usuals import merge_series_usuals
from level_core.profile.persist import (
    persist_care_profile_from_events,
    refresh_persisted_profile,
)
from level_core.profile.synthesize import _is_analytics_statement
from level_core.schemas.base import _now_utc
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Signal

_logger = get_logger(__name__)


class SignalIngestResult:
    accepted: int = 0
    detail: str = ""

    def __init__(self, *, accepted: int = 0, detail: str = "") -> None:
        self.accepted = accepted
        self.detail = detail


def _snapshot_needs_priority_rebuild(snapshot: ProfileSnapshot | None) -> bool:
    if snapshot is None or not snapshot.bullets:
        return True
    analytic = sum(1 for b in snapshot.bullets if _is_analytics_statement(b.text))
    return analytic >= max(1, (len(snapshot.bullets) + 1) // 2)


async def stamp_care_infer_fingerprint(
    sync_store: CalendarSyncStore,
    user_id: str,
    fingerprint: str,
) -> None:
    if not fingerprint:
        return
    latest = await sync_store.get(user_id)
    if latest is None or latest.care_infer_fingerprint == fingerprint:
        return
    await sync_store.upsert(
        latest.model_copy(update={"care_infer_fingerprint": fingerprint})
    )


async def ensure_profile_from_agenda(
    *,
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
) -> ProfileSnapshot | None:
    """Heal priorities from agenda only when the Care Profile is missing."""
    existing, care, state = await asyncio.gather(
        memory.manifestos.get_profile_snapshot(user_id=user_id),
        memory.manifestos.get_care_profile(user_id=user_id),
        sync_store.get(user_id),
    )
    if care is not None and care.roles:
        if existing and existing.bullets and not _snapshot_needs_priority_rebuild(existing):
            return existing
        return await refresh_persisted_profile(memory, user_id)

    if existing and existing.bullets and not _snapshot_needs_priority_rebuild(existing):
        return existing

    if state is None or not state.events:
        return existing

    events = [
        {"summary": e.summary, "start": e.start} for e in state.events.values() if e.summary
    ]
    snap = await persist_care_profile_from_events(memory, user_id, events, embed=False)
    if snap is None:
        return existing
    if snap.bullets:
        state = state.model_copy(
            update={
                "profile_ingested_at": state.profile_ingested_at or _now_utc(),
                "initial_sync_done": True,
                "initial_sync_error": None,
            }
        )
        await sync_store.upsert(state)
        _logger.info(
            "profile_healed_from_agenda",
            user_id=user_id,
            bullets=len(snap.bullets),
            events=len(state.events),
        )
    return snap


async def enrich_care_from_agenda(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
    *,
    force: bool = False,
) -> None:
    """Background holistic AI refresh of Care Profile, including usuals.

    Runs when role hints are missing, force=True (new memory), or the dated
    agenda fingerprint no longer matches the last infer. Keep / Not me still
    wins on merge — this only proposes or refreshes unlocked usuals.
    """
    try:
        care = await memory.manifestos.get_care_profile(user_id=user_id)
        state = await sync_store.get(user_id)
        if state is None or not state.events:
            return
        events = [
            {
                "summary": e.summary,
                "start": e.start,
                "end": e.end,
                "status": e.status,
                "recurring_event_id": e.recurring_event_id,
            }
            for e in state.events.values()
            if e.summary
        ]
        if not events:
            return
        live_fp = agenda_fingerprint(events)
        agenda_changed = usuals_infer_needed(
            stored_fingerprint=state.care_infer_fingerprint,
            events=events,
        )
        if care is not None:
            care = await apply_series_usuals(memory, user_id, events) or care

        if care is None or not care.roles:
            snap = await persist_care_profile_from_events(
                memory, user_id, events, embed=False
            )
            await stamp_care_infer_fingerprint(sync_store, user_id, live_fp)
            _logger.info(
                "care_inferred_background",
                user_id=user_id,
                bullets=len(snap.bullets) if snap else 0,
                events=len(events),
                fingerprint=live_fp[:12] if live_fp else "",
            )
            return

        if care.calendar_role_by_summary and not force and not agenda_changed:
            return
        facts = await memory.facts.list_for_user(user_id=user_id, limit=100)
        snippets = [
            f.statement
            for f in facts
            if f.statement and not f.statement.lower().startswith("calendar pattern")
        ][:16]
        async def _holistic(current):
            enriched = await enrich_care_profile_holistic(
                current, events, fact_snippets=snippets
            )
            return merge_series_usuals(enriched, events)

        care = await apply_care(memory, user_id, _holistic) or care
        await refresh_persisted_profile(memory, user_id)
        await stamp_care_infer_fingerprint(sync_store, user_id, live_fp)
        _logger.info(
            "care_enriched_background",
            user_id=user_id,
            version=care.version,
            force=force,
            agenda_changed=agenda_changed,
            usuals=sum(len(p.usuals) for p in care.people_profiles),
        )
    except Exception:  # noqa: BLE001
        _logger.exception("care_enrich_background_failed", user_id=user_id)


async def run_ingest_signals(
    memory: MemoryBank, signals: list[Signal]
) -> SignalIngestResult:
    settings = get_settings()
    pipeline = IngestPipeline(
        memory=memory,
        normalizer=IngestNormalizer(
            gemini=build_gemini_client(settings), model_id=settings.fast_model
        ),
        embedder=build_embedding_client(settings),
        guardrail=InboundGuardrail(settings=settings),
    )
    accepted = 0
    for signal in signals:
        try:
            result = await pipeline.run(signal)
        except ModelUnavailable as exc:
            return SignalIngestResult(
                accepted=accepted,
                detail=(
                    f"Stopped early — Gemini quota/rate limit: {exc}. "
                    f"Accepted {accepted} so far; retry Sync later or use Vertex "
                    f"(LEVEL_USE_AI_STUDIO=false)."
                ),
            )
        if result.signal is not None and not result.blocked and not result.skipped_duplicate:
            accepted += 1
    return SignalIngestResult(accepted=accepted)


__all__ = [
    "SignalIngestResult",
    "enrich_care_from_agenda",
    "ensure_profile_from_agenda",
    "run_ingest_signals",
    "stamp_care_infer_fingerprint",
]
