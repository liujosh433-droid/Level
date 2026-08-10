"""Ingest real personal sources: ChatGPT export + Google sync."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
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
from level_core.ingest.chatgpt_export import parse_chatgpt_export
from level_core.ingest.google_live import pull_calendar
from level_core.ingest.pipeline import IngestPipeline
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.synthesize import (
    infer_priority_facts,
    refresh_profile_and_manifesto,
)
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
    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    prev = await memory.manifestos.get_current_manifesto(user_id=user_id)
    snapshot, manifesto = await refresh_profile_and_manifesto(
        user_id=user_id, facts=facts, previous_manifesto=prev
    )
    await memory.manifestos.save_profile_snapshot(snapshot)
    await memory.manifestos.save_manifesto(manifesto)
    return snapshot


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
    """Rebuild priorities from the on-disk agenda (survives API reload / empty Memory Bank)."""
    existing = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    if existing and existing.bullets and not _snapshot_needs_priority_rebuild(existing):
        return existing

    state = await sync_store.get(user_id)
    if state is None or not state.events:
        return existing

    events = [
        {"summary": e.summary, "start": e.start} for e in state.events.values() if e.summary
    ]
    facts = infer_priority_facts(events, user_id=user_id)
    if not facts:
        return existing

    # Skip embeddings on heal — keeps Priorities navigable after API reload.
    await _persist_pattern_facts(memory, facts, embed=False)
    snap = await _refresh_profile(memory, user_id)
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


@router.post("/chatgpt", response_model=IngestSummary)
async def upload_chatgpt_export(
    file: UploadFile = File(...),
    max_messages: int = Form(40),
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> IngestSummary:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    try:
        signals = parse_chatgpt_export(
            raw,
            user_id=user_id,
            filename=file.filename or "",
            max_messages=max(1, min(max_messages, 80)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary = await _run_signals(memory, signals)
    snap = await _refresh_profile(memory, user_id)
    summary.profile_bullets = len(snap.bullets)
    summary.contradictions = len(snap.contradictions)
    summary.detail = (
        f"Parsed {len(signals)} ChatGPT user messages from export. "
        f"Profile ready for review ({summary.profile_bullets} bullets)."
    )
    _logger.info("chatgpt_ingest_done", user_id=user_id, **summary.model_dump())
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
        pattern_n = await _persist_pattern_facts(
            memory, infer_priority_facts(priority_events, user_id=user_id)
        )

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
    bias_scores: list[BiasScoreOut] = Field(default_factory=list)
    session_count: int = 0
    needs_review: bool = False
    bullets: list[BulletOut] = Field(default_factory=list)
    contradictions: list[ContradictionOut] = Field(default_factory=list)


async def _build_profile_response(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore | None = None,
) -> ProfileResponse:
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    if (snapshot is None or not snapshot.bullets) and sync_store is not None:
        await ensure_profile_from_agenda(
            user_id=user_id, memory=memory, sync_store=sync_store
        )

    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    manifesto = await memory.manifestos.get_current_manifesto(user_id=user_id)
    profile = await memory.manifestos.get_bias_profile(user_id=user_id)
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
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
    return ProfileResponse(
        user_id=user_id,
        fact_count=len(facts),
        manifesto=manifesto.statement if manifesto else None,
        bias_scores=scores,
        session_count=profile.session_count if profile else 0,
        needs_review=bool(snapshot.needs_review) if snapshot else False,
        bullets=bullets,
        contradictions=contradictions,
    )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileResponse:
    return await _build_profile_response(user_id, memory, sync_store)


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
) -> ProfileResponse:
    snapshot = await memory.manifestos.get_profile_snapshot(user_id=user_id)
    if snapshot is None:
        snapshot = await _refresh_profile(memory, user_id)
    by_id = {b.bullet_id: b for b in snapshot.bullets}
    for upd in payload.bullets:
        bullet = by_id.get(upd.bullet_id)
        if bullet is None:
            continue
        updates: dict = {"status": upd.status}
        if upd.text and upd.text.strip() and upd.text.strip() != bullet.text:
            updates["text"] = upd.text.strip()
            updates["status"] = BulletStatus.EDITED
        by_id[upd.bullet_id] = bullet.model_copy(update=updates)
    snapshot = snapshot.model_copy(
        update={
            "bullets": list(by_id.values()),
            "needs_review": not payload.mark_reviewed,
        }
    )
    await memory.manifestos.save_profile_snapshot(snapshot)
    return await _build_profile_response(user_id, memory)


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


@router.post("/profile/chat", response_model=ProfileChatResponse)
async def profile_chat(
    payload: ProfileChatRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
) -> ProfileChatResponse:
    """Learn from a short note and refresh the profile (caregiver-friendly)."""
    import uuid

    from level_core.errors import ModelUnavailable
    from level_core.models.base import GenerationRequest
    from level_core.schemas.signal import SignalSource

    signal = Signal(
        user_id=user_id,
        source=SignalSource.MANUAL,
        external_id=f"profile-chat:{uuid.uuid4().hex[:12]}",
        text=(
            "The user is correcting or enhancing their Level profile. "
            f"Take this as true about their life:\n{payload.message.strip()}"
        ),
    )
    summary = await _run_signals(memory, [signal])
    snap = await _refresh_profile(memory, user_id)
    profile = await _build_profile_response(user_id, memory, sync_store)

    reply = (
        f"Got it — I saved that and updated your profile"
        f" ({summary.facts} new fact{'s' if summary.facts != 1 else ''})."
    )
    try:
        settings = get_settings()
        gemini = build_gemini_client(settings)
        bullets = "; ".join(b.text for b in snap.bullets[:6]) or "(still thin)"
        resp = await gemini.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                prompt=(
                    "You are Level, a warm brief assistant for a busy caregiver. "
                    "They just told you something to remember about their life. "
                    "Reply in 1-2 short sentences confirming what you saved and how "
                    "you'll use it. No fluff, no lists.\n\n"
                    f"User said: {payload.message.strip()}\n"
                    f"Facts extracted: {summary.facts}\n"
                    f"Current profile bullets: {bullets}"
                ),
                temperature=0.3,
                max_output_tokens=120,
            )
        )
        if resp.text and resp.text.strip():
            reply = resp.text.strip()[:400]
    except ModelUnavailable:
        pass
    except Exception:  # noqa: BLE001
        _logger.exception("profile_chat_reply_failed")

    return ProfileChatResponse(reply=reply, facts_added=summary.facts, profile=profile)


__all__ = ["router"]
