"""Profile read + Keep / Not me + profile chat.

Google sync and ChatGPT ingest stay in ``sources``. URLs stay under /v1/sources.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import (
    cached_memory,
    get_calendar_sync_store,
    get_event_cue_store,
    get_memory,
    get_proposal_store,
    get_token_store,
)
from level_api.routes.sources import IngestSummary, _run_signals
from level_api.services.care_enrich import (
    enrich_care_from_agenda as _bg_enrich_care,
    ensure_profile_from_agenda,
)
from level_core.calendar.school import person_contacts
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.config import get_settings
from level_core.errors import ConflictError
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_feedback import apply_bullet_feedback_to_care_profile
from level_core.profile.care_graph import cached_care_graph
from level_core.profile.care_store import save_care
from level_api.services.chat_turn import run_chat_turn
from level_core.auth.tokens import TokenStore
from level_core.calendar.event_cues import EventCueStore
from level_core.calendar.proposals import ProposalStore
from level_core.profile.people_usuals import hydrate_people_from_roles
from level_core.profile.persist import (
    refresh_persisted_profile,
    seed_care_from_agenda_fast,
)
from level_core.profile.synthesize import (
    build_about_summary,
    care_profile_to_snapshot,
    refresh_profile_and_manifesto,
)
from level_core.schemas.care import CareGraph, CareProfile, active_care_people, clean_conflict_summaries
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Signal, SignalSource

router = APIRouter(prefix="/v1/sources", tags=["profile"])
_logger = get_logger(__name__)


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


class CareContactOut(BaseModel):
    contact_id: str
    role: str
    name: str = ""
    email: str = ""


class CarePersonOut(BaseModel):
    person_id: str
    display_name: str
    your_role: str = ""
    their_relation: str = ""
    care_role_id: str = "child_care"
    attendance_email: str = ""
    teacher_email: str = ""
    contacts: list[CareContactOut] = Field(default_factory=list)


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
    people: list[CarePersonOut] = Field(default_factory=list)


def _people_out(care: CareProfile | None) -> list[CarePersonOut]:
    if care is None:
        return []
    out: list[CarePersonOut] = []
    for person in active_care_people(care):
        school = person.school
        contacts = [
            CareContactOut(
                contact_id=c.contact_id,
                role=c.role,
                name=c.name,
                email=c.email,
            )
            for c in person_contacts(person)
        ]
        out.append(
            CarePersonOut(
                person_id=person.person_id,
                display_name=person.display_name,
                your_role=person.your_role,
                their_relation=person.their_relation,
                care_role_id=person.care_role_id,
                attendance_email=school.attendance_email if school else "",
                teacher_email=school.teacher_email if school else "",
                contacts=contacts,
            )
        )
    return out


async def _build_profile_response(
    user_id: str,
    memory: MemoryBank,
    sync_store: CalendarSyncStore | None = None,
    *,
    background_tasks: BackgroundTasks | None = None,
    allow_blocking_heal: bool = False,
) -> ProfileResponse:
    snapshot, care_probe = await asyncio.gather(
        memory.manifestos.get_profile_snapshot(user_id=user_id),
        memory.manifestos.get_care_profile(user_id=user_id),
    )
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

    facts, manifesto, profile, care, snapshot, state = await asyncio.gather(
        memory.facts.list_for_user(user_id=user_id, limit=200),
        memory.manifestos.get_current_manifesto(user_id=user_id),
        memory.manifestos.get_bias_profile(user_id=user_id),
        memory.manifestos.get_care_profile(user_id=user_id),
        memory.manifestos.get_profile_snapshot(user_id=user_id),
        sync_store.get(user_id) if sync_store is not None else asyncio.sleep(0, result=None),
    )
    if care is not None:
        hydrated = hydrate_people_from_roles(care)
        if hydrated.version != care.version:
            try:
                care = await save_care(
                    memory, hydrated, expected_version=care.version
                )
            except ConflictError:
                pass
    # Care Profile is source of truth for role bullets — never serve a stale
    # snapshot that cloned one Memory summary onto every role.
    if care is not None and care.roles:
        projected = care_profile_to_snapshot(care, fact_count=len(facts))
        snapshot = projected
        if background_tasks is not None:
            background_tasks.add_task(memory.manifestos.save_profile_snapshot, projected)
        else:
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
    care_updated = None
    if care and care.updated_at:
        care_updated = care.updated_at.isoformat()
    agenda_events: list[dict[str, str | None]] = []
    if state and state.events:
        try:
            agenda_events = [
                {"summary": e.summary, "start": e.start}
                for e in state.events.values()
                if e.summary
            ]
        except Exception:  # noqa: BLE001
            agenda_events = []

    # Synced calendar but empty Care Profile → optional regex seed (env-gated).
    # Always queue AI infer/enrich in the background so a failed onboard recovers.
    if sync_store is not None and agenda_events and (care is None or not care.roles):
        try:
            care = await seed_care_from_agenda_fast(
                user_id=user_id,
                memory=memory,
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

    # Never block page loads on Gemini. Create or refresh Care Profile off-path.
    if (
        agenda_events
        and background_tasks is not None
        and sync_store is not None
    ):
        if care is None or not care.roles:
            background_tasks.add_task(
                _bg_enrich_care,
                user_id,
                memory,
                sync_store,
                force=True,
            )
        else:
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
        conflict_summaries=clean_conflict_summaries(
            care.conflict_summaries if care else None
        ),
        care_graph=care_graph,
        people=_people_out(care),
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
        snapshot = await refresh_persisted_profile(memory, user_id)
    by_id = {b.bullet_id: b for b in snapshot.bullets}
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    expected_care = care.version if care is not None else None
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
        await save_care(memory, care, expected_version=expected_care)
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
    await refresh_persisted_profile(memory, user_id)
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
    signal = Signal(
        user_id=user_id,
        source=SignalSource.MANUAL,
        external_id=payload.external_id or f"manual:{uuid.uuid4().hex[:12]}",
        text=payload.text,
    )
    summary = await _run_signals(memory, [signal])
    snap = await refresh_persisted_profile(memory, user_id)
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
        await refresh_persisted_profile(memory, user_id)
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
    tokens: TokenStore = Depends(get_token_store),
    store: ProposalStore = Depends(get_proposal_store),
    cue_store: EventCueStore = Depends(get_event_cue_store),
) -> ProfileChatResponse:
    """Same chat router as Today — profile notes only when the user is updating care."""
    result = await run_chat_turn(
        user_id=user_id,
        message=payload.message,
        memory=memory,
        tokens=tokens,
        sync_store=sync_store,
        store=store,
        cue_store=cue_store,
        background_tasks=background_tasks,
    )
    if result.path == "profile":
        background_tasks.add_task(_bg_ingest_profile_chat_note, user_id, payload.message)
    profile = await _build_profile_response(
        user_id,
        memory,
        sync_store,
        background_tasks=background_tasks,
    )
    return ProfileChatResponse(
        reply=result.reply,
        facts_added=result.facts_added,
        profile=profile,
    )

__all__ = ["router"]
