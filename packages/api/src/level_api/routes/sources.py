"""Ingest real personal sources: ChatGPT Memory paste + Google sync."""

from __future__ import annotations

import re

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import get_calendar_sync_store, get_memory, get_token_store
from level_api.services.google_sync import agenda_only_refresh
from level_core.agents.ingest_normalizer import IngestNormalizer
from level_core.auth.tokens import TokenStore
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.config import get_settings
from level_core.guardrails.inbound import InboundGuardrail
from level_core.ingest.chatgpt_memory import (
    extract_from_chatgpt_memory,
    memory_extract_to_facts,
)
from level_core.ingest.google_live import pull_calendar
from level_core.ingest.pipeline import IngestPipeline
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.persist import (
    persist_care_profile_from_events,
    refresh_persisted_profile,
)
from level_core.profile.synthesize import (
    adjust_care_profile_from_note,
    apply_bullet_feedback_to_care_profile,
    build_about_summary,
    cached_care_graph,
    care_profile_to_snapshot,
    refresh_profile_and_manifesto,
)
from level_core.schemas.care import CareGraph
from level_core.schemas.base import _now_utc
from level_core.schemas.profile import BulletStatus, ProfileSnapshot
from level_core.schemas.signal import Fact, Signal

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


def _snapshot_needs_priority_rebuild(snapshot: ProfileSnapshot | None) -> bool:
    if snapshot is None or not snapshot.bullets:
        return True
    # Old calendar-analytics bullets should be replaced with inferred priorities.
    from level_core.profile.synthesize import _is_analytics_statement

    analytic = sum(1 for b in snapshot.bullets if _is_analytics_statement(b.text))
    return analytic >= max(1, (len(snapshot.bullets) + 1) // 2)


async def ensure_profile_from_agenda(
    *,
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
) -> ProfileSnapshot | None:
    """Heal priorities from agenda only when the Care Profile is missing.

    Never re-runs Gemini when a care model already exists — page loads must stay cheap.
    """
    existing = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is not None and care.roles:
        if existing and existing.bullets and not _snapshot_needs_priority_rebuild(existing):
            return existing
        # Cheap refresh from existing care (no AI).
        from level_core.profile.persist import refresh_persisted_profile

        return await refresh_persisted_profile(memory, user_id)

    if existing and existing.bullets and not _snapshot_needs_priority_rebuild(existing):
        return existing

    state = await sync_store.get(user_id)
    if state is None or not state.events:
        return existing

    events = [
        {"summary": e.summary, "start": e.start} for e in state.events.values() if e.summary
    ]
    # Skip embeddings on heal — keeps Priorities navigable after API reload.
    snap = await _infer_persist_care_profile(memory, user_id, events, embed=False)
    if snap is None:
        return existing
    if snap.bullets:
        state = await sync_store.get(user_id) or state
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


async def _bg_enrich_care(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
    *,
    force: bool = False,
) -> None:
    """Background holistic AI refresh of Care Profile event roles + conflicts.

    By default only runs when calendar role hints are missing. Pass force=True
    after new memory (day check-in) so ambiguous titles get reclassified from
    context rather than regex.
    """
    try:
        care = await memory.manifestos.get_care_profile(user_id=user_id)
        if care is None:
            return
        if care.calendar_role_by_summary and not force:
            return
        state = await sync_store.get(user_id)
        if state is None or not state.events:
            return
        events = [
            {"summary": e.summary, "start": e.start}
            for e in state.events.values()
            if e.summary
        ]
        if not events:
            return
        from level_core.profile.care_infer_llm import enrich_care_profile_holistic

        facts = await memory.facts.list_for_user(user_id=user_id, limit=100)
        snippets = [
            f.statement
            for f in facts
            if f.statement and not f.statement.lower().startswith("calendar pattern")
        ][:16]
        care = await enrich_care_profile_holistic(
            care, events, fact_snippets=snippets
        )
        from level_core.profile.synthesize import invalidate_care_graph_cache

        invalidate_care_graph_cache(user_id)
        await memory.manifestos.save_care_profile(care)
        await refresh_persisted_profile(memory, user_id)
        _logger.info(
            "care_enriched_background",
            user_id=user_id,
            version=care.version,
            force=force,
        )
    except Exception:  # noqa: BLE001
        _logger.exception("care_enrich_background_failed", user_id=user_id)


async def _bg_enrich_care_if_needed(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
) -> None:
    """Compat wrapper — enrich only when calendar hints are missing."""
    await _bg_enrich_care(user_id, memory, sync_store, force=False)


async def _persist_pattern_facts(
    memory: MemoryBank,
    facts: list[Fact],
    *,
    embed: bool = True,
) -> int:
    if not facts:
        return 0
    settings = get_settings()
    embedder = build_embedding_client(settings) if embed else None
    n = 0
    # List once — not once per fact (heal used to re-scan on every insert).
    existing = await memory.facts.list_for_user(user_id=facts[0].user_id, limit=200)
    existing_stmts = {e.statement for e in existing}
    for fact in facts:
        if fact.statement in existing_stmts:
            continue
        await memory.facts.upsert(fact)
        existing_stmts.add(fact.statement)
        embeddings: list = []
        if embedder is not None:
            try:
                embeddings = await embedder.embed(texts=[fact.statement])
            except Exception:  # noqa: BLE001
                embeddings = []
        if embeddings:
            await memory.vectors.upsert(
                user_id=fact.user_id,
                fact_id=fact.fact_id,
                text=fact.statement,
                embedding=embeddings[0],
            )
        n += 1
    return n


async def _run_signals(memory: MemoryBank, signals: list[Signal]) -> IngestSummary:
    from level_core.errors import ModelUnavailable

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


class ChatGPTMemoryRequest(BaseModel):
    text: str = Field(min_length=20, max_length=24_000)


@router.post("/chatgpt", response_model=IngestSummary)
async def ingest_chatgpt_memory(
    payload: ChatGPTMemoryRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> IngestSummary:
    """Paste ChatGPT Memory → extract facts → reassess Care Profile + graph."""
    from level_core.profile.care_infer_llm import apply_note_to_care_profile_ai
    from level_core.profile.synthesize import (
        cached_care_graph,
        invalidate_care_graph_cache,
    )
    from level_core.schemas.care import CareProfile

    paste = payload.text.strip()
    settings = get_settings()
    gemini = build_gemini_client(settings)
    try:
        extracted = await extract_from_chatgpt_memory(paste, gemini=gemini)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    facts = memory_extract_to_facts(extracted, user_id=user_id, paste=paste)
    if not facts and not (extracted.care_note or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No care-relevant facts found in that Memory paste.",
        )

    summary = IngestSummary()
    try:
        summary.facts = await _persist_pattern_facts(memory, facts, embed=True)
        summary.accepted = summary.facts
    except Exception as exc:  # noqa: BLE001
        _logger.exception("chatgpt_memory_persist_failed", user_id=user_id)
        raise HTTPException(
            status_code=502,
            detail=f"Could not save Memory facts: {exc}",
        ) from exc

    # Fold the full distillate into Care Profile (bootstrap if missing).
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is None:
        care = CareProfile(user_id=user_id, roles=[])

    care_note = extracted.care_note.strip() if extracted.care_note else ""
    facts_block = "\n".join(f"- {f}" for f in extracted.facts[:20])
    apply_text = "\n".join(part for part in (care_note, facts_block) if part).strip()

    if apply_text:
        updated = await apply_note_to_care_profile_ai(care, apply_text, gemini=gemini)
        if updated is not None:
            care, _reply = updated
        else:
            care = adjust_care_profile_from_note(care, apply_text)
        care = care.model_copy(
            update={
                "version": int(care.version or 1) + 1,
                "updated_at": _now_utc(),
            }
        )
        invalidate_care_graph_cache(user_id)
        await memory.manifestos.save_care_profile(care)

        # Eagerly rebuild graph with current agenda so Profile is fresh immediately.
        agenda: list[dict[str, str | None]] = []
        try:
            state = await sync_store.get(user_id)
            if state and state.events:
                agenda = [
                    {"summary": e.summary, "start": e.start}
                    for e in state.events.values()
                ]
        except Exception:  # noqa: BLE001
            agenda = []
        cached_care_graph(care, agenda or None)

    snap = await _refresh_profile(memory, user_id)
    summary.profile_bullets = len(snap.bullets)
    summary.contradictions = len(snap.contradictions)
    n_facts = len(extracted.facts)
    summary.detail = (
        f"Pulled {n_facts} care-relevant fact{'s' if n_facts != 1 else ''} "
        f"from ChatGPT Memory and refreshed your Care Profile "
        f"({summary.profile_bullets} bullets)."
    )
    _logger.info(
        "chatgpt_memory_ingest_done",
        user_id=user_id,
        extracted=n_facts,
        care_roles=len(care.roles) if care else 0,
        **summary.model_dump(),
    )
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
    token = await tokens.get_google_token(user_id)
    connected = token is not None and bool(token.refresh_token or token.access_token)
    state = await sync_store.get(user_id)
    if not connected:
        return GoogleSyncStatus(google_connected=False)
    if state is None:
        return GoogleSyncStatus(google_connected=True, initial_sync_done=False)
    now_ms = int(_now_utc().timestamp() * 1000)
    watch_active = bool(
        state.channel_id
        and state.channel_expiration_ms
        and state.channel_expiration_ms > now_ms
    )
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
    """Google Calendar push receiver — agenda cache only, never LLM profile."""
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

    # Critical: agenda-only — do not call _run_signals / _refresh_profile.
    background_tasks.add_task(agenda_only_refresh, state.user_id)
    _logger.info(
        "google_webhook_queued_agenda_refresh",
        user_id=state.user_id,
        channel_id=channel_id,
        resource_state=resource_state,
        llm=False,
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
        from level_core.auth.google_oauth import credentials_from_token, token_from_credentials
        from level_core.calendar.agenda_sync import ensure_calendar_watch, refresh_agenda_cache
        from level_core.config import get_settings as _gs

        # Refresh access token if needed and persist it.
        creds = credentials_from_token(token)
        refreshed = token_from_credentials(creds, user_id=user_id, settings=_gs())
        if refreshed.refresh_token is None and token.refresh_token:
            refreshed = refreshed.model_copy(update={"refresh_token": token.refresh_token})
        await tokens.upsert_token(refreshed)
        token = refreshed

        # Keep agenda cache fresh without waiting for a webhook.
        await refresh_agenda_cache(user_id=user_id, token=token, sync_store=sync_store)
        await ensure_calendar_watch(user_id=user_id, token=token, sync_store=sync_store)

        cal = await pull_calendar(token, user_id=user_id, max_events=25)
        signals.extend(cal.signals)
        state = await sync_store.get(user_id)
        priority_events = (
            [{"summary": e.summary, "start": e.start} for e in state.events.values()]
            if state and state.events
            else [{"summary": (s.text or "").split(": ", 1)[-1], "start": None} for s in cal.signals]
        )
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
    from level_core.calendar.sync_state import CalendarSyncState

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


class BiasScoreOut(BaseModel):
    category: str
    ema: float
    streak: int
    total_observations: int


class BulletOut(BaseModel):
    bullet_id: str
    category: str
    text: str
    status: str
    source_fact_ids: list[str] = Field(default_factory=list)
    care_role_id: str | None = None


class ContradictionOut(BaseModel):
    contradiction_id: str
    topic: str
    summary: str
    status: str
    fact_id_a: str
    fact_id_b: str


class ProfileResponse(BaseModel):
    user_id: str
    fact_count: int
    manifesto: str | None = None
    about_summary: str | None = None
    bias_scores: list[BiasScoreOut] = Field(default_factory=list)
    session_count: int = 0
    needs_review: bool = False
    bullets: list[BulletOut] = Field(default_factory=list)
    contradictions: list[ContradictionOut] = Field(default_factory=list)
    care_profile_version: int | None = None
    care_updated_at: str | None = None
    care_role_count: int = 0
    conflict_summaries: list[str] = Field(default_factory=list)
    care_graph: CareGraph | None = None


async def _seed_care_from_agenda_fast(
    *,
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore,
    events: list[dict[str, str | None]],
) -> CareProfile | None:
    """Cheap calendar→Care Profile seed (no Gemini) so the graph isn't empty."""
    from level_core.profile.care_infer_llm import reconcile_exclusive_people
    from level_core.profile.synthesize import (
        infer_care_profile_heuristic,
        invalidate_care_graph_cache,
    )

    previous = await memory.manifestos.get_care_profile(user_id=user_id)
    care, facts = infer_care_profile_heuristic(
        events, user_id=user_id, previous=previous
    )
    care = reconcile_exclusive_people(care)
    if not care.roles and not facts:
        return previous
    for fact in facts:
        try:
            await memory.facts.upsert(fact)
        except Exception:  # noqa: BLE001
            pass
    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)
    try:
        await refresh_persisted_profile(memory, user_id)
    except Exception:  # noqa: BLE001
        _logger.warning("seed_care_refresh_failed", user_id=user_id)
    _logger.info(
        "care_seeded_from_agenda",
        user_id=user_id,
        roles=len(care.roles),
        events=len(events),
    )
    return care


async def _build_profile_response(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore | None = None,
    *,
    background_tasks: BackgroundTasks | None = None,
    allow_blocking_heal: bool = False,
) -> ProfileResponse:
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    care_probe = await memory.manifestos.get_care_profile(user_id=user_id)
    needs_heal = snapshot is None or not snapshot.bullets
    if needs_heal and sync_store is not None:
        if care_probe is not None and care_probe.roles:
            # Snapshot missing but care exists — cheap rebuild, no Gemini.
            await refresh_persisted_profile(memory, user_id)
        elif allow_blocking_heal:
            await ensure_profile_from_agenda(
                user_id=user_id, memory=memory, sync_store=sync_store
            )
        elif background_tasks is not None:
            background_tasks.add_task(
                ensure_profile_from_agenda,
                user_id=user_id,
                memory=memory,
                sync_store=sync_store,
            )

    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    profile = await memory.manifestos.get_bias_profile(user_id=user_id)
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    # Care Profile is source of truth for role bullets — never serve a stale
    # snapshot that cloned one Memory summary onto every role.
    if care is not None and care.roles:
        projected = care_profile_to_snapshot(care, fact_count=len(facts))
        snapshot = projected
        try:
            await memory.manifestos.save_profile_snapshot(projected)
        except Exception:  # noqa: BLE001
            pass
    scores: list[BiasScoreOut] = []
    if profile:
        scores = [
            BiasScoreOut(
                category=s.category.value,
                ema=s.ema,
                streak=s.streak,
                total_observations=s.total_observations,
            )
            for s in sorted(profile.scores, key=lambda x: x.ema, reverse=True)
            if s.ema >= 0.15 or s.total_observations > 0
        ]
    bullets = [
        BulletOut(
            bullet_id=b.bullet_id,
            category=b.category.value,
            text=b.text,
            status=b.status.value,
            source_fact_ids=b.source_fact_ids,
            care_role_id=b.care_role_id,
        )
        for b in (snapshot.bullets if snapshot else [])
        if b.status is not BulletStatus.REJECTED
    ]
    contradictions = [
        ContradictionOut(
            contradiction_id=c.contradiction_id,
            topic=c.topic,
            summary=c.summary,
            status=c.status.value,
            fact_id_a=c.fact_id_a,
            fact_id_b=c.fact_id_b,
        )
        for c in (snapshot.contradictions if snapshot else [])
        if c.status is not BulletStatus.REJECTED
    ]
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    care_updated = None
    if care and care.updated_at:
        care_updated = care.updated_at.isoformat()
    agenda_events: list[dict[str, str | None]] = []
    if sync_store is not None:
        try:
            state = await sync_store.get(user_id)
            if state and state.events:
                agenda_events = [
                    {"summary": e.summary, "start": e.start}
                    for e in state.events.values()
                    if e.summary
                ]
        except Exception:  # noqa: BLE001
            agenda_events = []

    # Synced calendar but empty Care Profile → seed immediately (fast, no Gemini)
    # so the care graph isn't blank. AI upgrades run in the background.
    if sync_store is not None and agenda_events and (care is None or not care.roles):
        try:
            care = await _seed_care_from_agenda_fast(
                user_id=user_id,
                memory=memory,
                sync_store=sync_store,
                events=agenda_events,
            )
            if care and care.updated_at:
                care_updated = care.updated_at.isoformat()
            snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
            if snapshot:
                bullets = [
                    BulletOut(
                        bullet_id=b.bullet_id,
                        category=b.category.value,
                        text=b.text,
                        status=b.status.value,
                        source_fact_ids=b.source_fact_ids,
                        care_role_id=b.care_role_id,
                    )
                    for b in snapshot.bullets
                    if b.status is not BulletStatus.REJECTED
                ]
        except Exception:  # noqa: BLE001
            _logger.exception("care_seed_failed", user_id=user_id)

    # Never block page loads on Gemini. Holistic AI owns event-role classification;
    # refresh in the background when hints are missing or the profile is stale.
    if (
        care is not None
        and agenda_events
        and background_tasks is not None
        and sync_store is not None
    ):
        from datetime import datetime, timezone

        updated = care.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = (
            (datetime.now(tz=timezone.utc) - updated).total_seconds()
            if updated is not None
            else 10_000
        )
        missing_hints = not care.calendar_role_by_summary
        if missing_hints or age > 120:
            background_tasks.add_task(
                _bg_enrich_care,
                user_id,
                memory,
                sync_store,
                force=not missing_hints,
            )

    care_graph = None
    if care is not None:
        care_graph, _, _ = cached_care_graph(care, agenda_events or None)

    about_summary = build_about_summary(care_profile=care, facts=facts)

    return ProfileResponse(
        user_id=user_id,
        fact_count=len(facts),
        manifesto=manifesto.statement if manifesto else None,
        about_summary=about_summary,
        bias_scores=scores,
        session_count=profile.session_count if profile else 0,
        needs_review=bool(snapshot.needs_review) if snapshot else False,
        bullets=bullets,
        contradictions=contradictions,
        care_profile_version=care.version if care else None,
        care_updated_at=care_updated,
        care_role_count=len(care.roles) if care else 0,
        conflict_summaries=list(care.conflict_summaries[:4]) if care else [],
        care_graph=care_graph,
    )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileResponse:
    return await _build_profile_response(
        user_id,
        memory,
        sync_store,
        background_tasks=background_tasks,
        allow_blocking_heal=False,
    )


class BulletUpdate(BaseModel):
    bullet_id: str
    status: BulletStatus
    text: str | None = Field(default=None, max_length=400)


class ProfileReviewRequest(BaseModel):
    bullets: list[BulletUpdate] = Field(default_factory=list)
    mark_reviewed: bool = True


@router.post("/profile/review", response_model=ProfileResponse)
async def review_profile(
    payload: ProfileReviewRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileResponse:
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    if snapshot is None:
        snapshot = await _refresh_profile(memory, user_id)
    by_id = {b.bullet_id: b for b in snapshot.bullets}
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    for upd in payload.bullets:
        bullet = by_id.get(upd.bullet_id)
        if bullet is None:
            continue
        updates: dict = {"status": upd.status}
        if upd.text and upd.text.strip() and upd.text.strip() != bullet.text:
            updates["text"] = upd.text.strip()
            updates["status"] = BulletStatus.EDITED
        by_id[upd.bullet_id] = bullet.model_copy(update=updates)
        if care is not None:
            care = apply_bullet_feedback_to_care_profile(
                care,
                bullet_id=upd.bullet_id,
                status=updates["status"],
                text=updates.get("text"),
                snapshot=snapshot,
            )
    snapshot = snapshot.model_copy(
        update={
            "bullets": list(by_id.values()),
            "needs_review": not payload.mark_reviewed,
        }
    )
    if care is not None:
        # Re-project roles so status/salience stay aligned with Care Profile.
        projected = care_profile_to_snapshot(care, fact_count=snapshot.fact_count)
        # Preserve bullet_ids from the review payload where role matches.
        role_to_bullet = {
            b.care_role_id: b for b in snapshot.bullets if b.care_role_id
        }
        merged_bullets = []
        for b in projected.bullets:
            prev_b = role_to_bullet.get(b.care_role_id)
            if prev_b is not None:
                merged_bullets.append(
                    b.model_copy(
                        update={
                            "bullet_id": prev_b.bullet_id,
                            "status": by_id.get(prev_b.bullet_id, b).status,
                            "text": by_id[prev_b.bullet_id].text
                            if prev_b.bullet_id in by_id
                            and by_id[prev_b.bullet_id].status is BulletStatus.EDITED
                            else b.text,
                        }
                    )
                )
            else:
                merged_bullets.append(b)
        snapshot = snapshot.model_copy(
            update={
                "bullets": merged_bullets,
                "contradictions": projected.contradictions or snapshot.contradictions,
                "needs_review": not payload.mark_reviewed,
            }
        )
        await memory.manifestos.save_care_profile(care)
        from level_core.profile.synthesize import invalidate_care_graph_cache

        invalidate_care_graph_cache(user_id)
        prev_m = await memory.manifestos.get_current_manifesto(user_id=user_id)
        _, manifesto, _ = await refresh_profile_and_manifesto(
            user_id=user_id,
            facts=await memory.facts.list_for_user(user_id=user_id, limit=200),
            previous_manifesto=prev_m,
            care_profile=care,
        )
        await memory.manifestos.save_manifesto(manifesto)
    await memory.manifestos.save_profile_snapshot(snapshot)
    return await _build_profile_response(user_id, memory, sync_store)


@router.post("/profile/refresh", response_model=ProfileResponse)
async def refresh_profile_route(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileResponse:
    await _refresh_profile(memory, user_id)
    return await _build_profile_response(user_id, memory, sync_store)


class ManualNoteRequest(BaseModel):
    text: str = Field(min_length=20, max_length=8000)
    external_id: str | None = None


@router.post("/note", response_model=IngestSummary)
async def ingest_manual_note(
    payload: ManualNoteRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> IngestSummary:
    from level_core.schemas.signal import SignalSource
    import uuid

    signal = Signal(
        user_id=user_id,
        source=SignalSource.MANUAL,
        external_id=payload.external_id or f"manual:{uuid.uuid4().hex[:12]}",
        text=payload.text,
    )
    summary = await _run_signals(memory, [signal])
    snap = await _refresh_profile(memory, user_id)
    summary.profile_bullets = len(snap.bullets)
    summary.contradictions = len(snap.contradictions)
    summary.detail = "Manual note ingested. Profile refreshed — please review."
    return summary


class ProfileChatRequest(BaseModel):
    message: str = Field(min_length=8, max_length=2000)


class ProfileChatResponse(BaseModel):
    reply: str
    facts_added: int = 0
    profile: ProfileResponse


def _clean_assistant_reply(text: str, *, fallback: str) -> str:
    """Strip wrapping / dangling quotes models sometimes leave on short replies."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    # e.g. Got it: "   or   Got it — "
    t = re.sub(r"""[:\s—\-]+["']\s*$""", "", t).strip()
    if t.endswith('"') and t.count('"') % 2 == 1:
        t = t[:-1].rstrip()
    if t.endswith("'") and t.count("'") % 2 == 1:
        t = t[:-1].rstrip()
    t = t.strip()
    if len(t) < 8 or t.lower().rstrip(":.—- ") in {"got it", "okay", "ok"}:
        return fallback
    return t


async def _bg_ingest_profile_chat_note(user_id: str, message: str) -> None:
    """Memory Bank ingest + embeddings — off the chat hot path."""
    import uuid

    from level_api.dependencies import cached_memory
    from level_core.schemas.signal import Signal, SignalSource

    try:
        memory = cached_memory()
        signal = Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id=f"profile-chat:{uuid.uuid4().hex[:12]}",
            text=(
                "The user is correcting or enhancing their Level profile. "
                f"Take this as true about their life:\n{message}"
            ),
        )
        await _run_signals(memory, [signal])
        await _refresh_profile(memory, user_id)
        _logger.info("profile_chat_ingest_bg_done", user_id=user_id)
    except Exception:  # noqa: BLE001
        _logger.exception("profile_chat_ingest_bg_failed", user_id=user_id)


@router.post("/profile/chat", response_model=ProfileChatResponse)
async def profile_chat(
    payload: ProfileChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileChatResponse:
    """Learn from a short note — reply ASAP (one Gemini call), refresh in background."""
    from level_core.profile.care_infer_llm import apply_note_to_care_profile_ai
    from level_core.profile.synthesize import (
        cached_care_graph,
        care_profile_to_snapshot,
        invalidate_care_graph_cache,
    )

    message = payload.message.strip()
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    reply = "Got it — I saved that."
    facts_added = 0

    if care is None:
        # Bootstrap once if needed (slower path).
        import uuid

        from level_core.schemas.signal import Signal, SignalSource

        signal = Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id=f"profile-chat:{uuid.uuid4().hex[:12]}",
            text=(
                "The user is correcting or enhancing their Level profile. "
                f"Take this as true about their life:\n{message}"
            ),
        )
        summary = await _run_signals(memory, [signal])
        facts_added = summary.facts
        await _refresh_profile(memory, user_id)
        care = await memory.manifestos.get_care_profile(user_id=user_id)
        if care is not None:
            care = adjust_care_profile_from_note(care, message)
            invalidate_care_graph_cache(user_id)
            await memory.manifestos.save_care_profile(care)
        reply = (
            f"Got it — I saved that"
            f" ({facts_added} new fact{'s' if facts_added != 1 else ''})."
        )
        profile = await _build_profile_response(
            user_id,
            memory,
            sync_store,
            background_tasks=background_tasks,
        )
        return ProfileChatResponse(reply=reply, facts_added=facts_added, profile=profile)

    settings = get_settings()
    gemini = build_gemini_client(settings)
    updated = await apply_note_to_care_profile_ai(care, message, gemini=gemini)
    if updated is not None:
        care, reply = updated
    else:
        care = adjust_care_profile_from_note(care, message)
        reply = _clean_assistant_reply(reply, fallback=reply)
    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)

    # Fast response: project snapshot + graph locally; defer ingest/refresh.
    background_tasks.add_task(_bg_ingest_profile_chat_note, user_id, message)

    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    projected = care_profile_to_snapshot(care, fact_count=snapshot.fact_count if snapshot else 0)
    bullets = [
        BulletOut(
            bullet_id=b.bullet_id,
            category=b.category.value,
            text=b.text,
            status=b.status.value,
            source_fact_ids=b.source_fact_ids,
            care_role_id=b.care_role_id,
        )
        for b in projected.bullets
        if b.status is not BulletStatus.REJECTED
    ]
    agenda_events: list[dict[str, str | None]] = []
    try:
        state = await sync_store.get(user_id)
        if state and state.events:
            agenda_events = [
                {"summary": e.summary, "start": e.start}
                for e in state.events.values()
                if e.summary
            ]
    except Exception:  # noqa: BLE001
        agenda_events = []
    care_graph, _, _ = cached_care_graph(care, agenda_events or None)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    facts_for_about = await memory.facts.list_for_user(user_id=user_id, limit=200)
    profile = ProfileResponse(
        user_id=user_id,
        fact_count=snapshot.fact_count if snapshot else len(facts_for_about),
        manifesto=manifesto.statement if manifesto else None,
        about_summary=build_about_summary(care_profile=care, facts=facts_for_about),
        bias_scores=[],
        session_count=0,
        needs_review=bool(snapshot.needs_review) if snapshot else False,
        bullets=bullets,
        contradictions=[],
        care_profile_version=care.version,
        care_updated_at=care.updated_at.isoformat() if care.updated_at else None,
        care_role_count=len(care.roles),
        conflict_summaries=list(care.conflict_summaries[:4]),
        care_graph=care_graph,
    )
    return ProfileChatResponse(reply=reply, facts_added=facts_added, profile=profile)


__all__ = ["router"]
