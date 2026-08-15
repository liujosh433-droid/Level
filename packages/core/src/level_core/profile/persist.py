"""Shared Care Profile persistence used by API sync and ingest jobs.

Evolving Knowledge Engine: calendar signals mutate the Care Profile via Gemini.
When AI is unavailable we degrade (keep previous / return None) — we do not
invent roles with regex. Opt-in ``LEVEL_ALLOW_HEURISTIC_CARE=1`` is the only
exception (pitch / offline emergencies).
"""

from __future__ import annotations

import asyncio
import os

from level_core.config import get_settings
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.errors import ConflictError
from level_core.profile.care_infer_llm import (
    infer_care_profile_ai,
    reconcile_exclusive_people,
)
from level_core.profile.care_store import save_care
from level_core.profile.care_heuristic import infer_care_profile_heuristic
from level_core.profile.synthesize import refresh_profile_and_manifesto
from level_core.schemas.care import CareProfile
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal, SignalSource

_logger = get_logger(__name__)


def _heuristic_care_allowed() -> bool:
    return os.getenv("LEVEL_ALLOW_HEURISTIC_CARE", "").lower() in {
        "1",
        "true",
        "yes",
    }


async def persist_care_profile_from_events(
    memory: MemoryBank,
    user_id: str,
    events: list[dict[str, str | None]],
    *,
    embed: bool = True,
) -> ProfileSnapshot | None:
    """Infer Care Profile from agenda-like events via AI, then persist.

    Preserves Keep / Not me via previous-profile merge inside the AI builder.
    On AI failure: leave previous Care Profile untouched (honest degrade).
    """
    if not events:
        return None
    previous, existing = await asyncio.gather(
        memory.manifestos.get_care_profile(user_id=user_id),
        memory.facts.list_for_user(user_id=user_id, limit=200),
    )
    snippets = [
        f.statement
        for f in existing
        if f.statement and not f.statement.lower().startswith("calendar pattern")
    ][:16]

    care = None
    facts: list[Fact] = []
    source = "ai"
    try:
        gemini = build_gemini_client(get_settings())
        result = await infer_care_profile_ai(
            user_id=user_id,
            events=events,
            previous=previous,
            fact_snippets=snippets,
            gemini=gemini,
        )
        if result is not None:
            care, facts = result
    except Exception:  # noqa: BLE001
        _logger.exception("care_ai_infer_failed", user_id=user_id)

    if care is None and _heuristic_care_allowed():
        source = "heuristic_opt_in"
        care, facts = infer_care_profile_heuristic(
            events, user_id=user_id, previous=previous
        )
        care = reconcile_exclusive_people(care)
        _logger.warning("care_profile_heuristic_opt_in", user_id=user_id)
    elif care is None:
        _logger.warning(
            "care_profile_ai_insufficient",
            user_id=user_id,
            note="Keeping previous Care Profile; no regex invent.",
        )
        if previous is not None:
            return await refresh_persisted_profile(memory, user_id)
        return None

    if not care.roles and not facts:
        return None

    await _persist_pattern_facts(memory, facts, embed=embed)
    try:
        await save_care(
            memory,
            care,
            expected_version=previous.version if previous is not None else None,
        )
    except ConflictError:
        _logger.warning("care_profile_save_conflict", user_id=user_id)
    snap = await refresh_persisted_profile(memory, user_id)
    _logger.info(
        "care_profile_mutated",
        user_id=user_id,
        roles=len(care.roles),
        facts=len(facts),
        version=care.version,
        source=source,
        calendar_hints=len(care.calendar_role_by_summary),
        usuals=sum(len(p.usuals) for p in care.people_profiles),
    )
    return snap


async def refresh_persisted_profile(memory: MemoryBank, user_id: str) -> ProfileSnapshot:
    """Rebuild snapshot + manifesto from facts + care profile and persist."""
    facts, prev, care = await asyncio.gather(
        memory.facts.list_for_user(user_id=user_id, limit=200),
        memory.manifestos.get_current_manifesto(user_id=user_id),
        memory.manifestos.get_care_profile(user_id=user_id),
    )
    if care is not None:
        care = reconcile_exclusive_people(care)
    snapshot, manifesto, care_out = await refresh_profile_and_manifesto(
        user_id=user_id,
        facts=facts,
        previous_manifesto=prev,
        care_profile=care,
    )
    writes = [
        memory.manifestos.save_profile_snapshot(snapshot),
        memory.manifestos.save_manifesto(manifesto),
    ]
    await asyncio.gather(*writes)
    if care_out is not None:
        try:
            await save_care(
                memory,
                care_out,
                expected_version=care.version if care is not None else None,
            )
        except ConflictError:
            _logger.warning("care_refresh_conflict", user_id=user_id)
    return snapshot


async def _persist_pattern_facts(
    memory: MemoryBank,
    facts: list[Fact],
    *,
    embed: bool,
) -> None:
    if not facts:
        return
    embedder = build_embedding_client(get_settings()) if embed else None
    existing = await memory.facts.list_for_user(user_id=facts[0].user_id, limit=200)
    existing_stmts = {e.statement for e in existing}
    for fact in facts:
        if fact.statement in existing_stmts:
            continue
        await memory.facts.upsert(fact)
        existing_stmts.add(fact.statement)
        if embedder is None:
            continue
        try:
            embeddings = await embedder.embed(texts=[fact.statement])
        except ModelUnavailable:
            continue
        if embeddings:
            await memory.vectors.upsert(
                user_id=fact.user_id,
                fact_id=fact.fact_id,
                text=fact.statement,
                embedding=embeddings[0],
            )


async def seed_care_from_agenda_fast(
    *,
    user_id: str,
    memory: MemoryBank,
    events: list[dict[str, str | None]],
) -> CareProfile | None:
    """Opt-in regex seed only. Live path waits for Gemini.

    Set ``LEVEL_ALLOW_HEURISTIC_CARE=1`` to restore the old fast seed.
    """
    previous = await memory.manifestos.get_care_profile(user_id=user_id)
    if not _heuristic_care_allowed():
        _logger.info(
            "care_seed_skipped_awaiting_ai",
            user_id=user_id,
            events=len(events),
        )
        return previous

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
    try:
        await save_care(
            memory,
            care,
            expected_version=previous.version if previous is not None else None,
        )
    except ConflictError:
        _logger.warning("care_seed_conflict", user_id=user_id)
    try:
        await refresh_persisted_profile(memory, user_id)
    except Exception:  # noqa: BLE001
        _logger.warning("seed_care_refresh_failed", user_id=user_id)
    _logger.info(
        "care_seeded_from_agenda_heuristic",
        user_id=user_id,
        roles=len(care.roles),
        events=len(events),
    )
    return care


def events_from_calendar_signals(
    signals: list,
) -> list[dict[str, str | None]]:
    """Best-effort event dicts from GCAL Signal texts for care inference."""
    out: list[dict[str, str | None]] = []
    for raw in signals:
        if not isinstance(raw, Signal):
            continue
        if raw.source is not SignalSource.GCAL:
            continue
        text = (raw.text or "").strip()
        if not text:
            continue
        summary = text
        for prefix in ("Calendar: ", "On my calendar: ", "On my calendar — "):
            if summary.startswith(prefix):
                summary = summary[len(prefix) :]
                break
        summary = summary.split("\n", 1)[0].strip()[:200]
        start = raw.occurred_at.isoformat() if raw.occurred_at else None
        out.append({"summary": summary, "start": start})
    return out


__all__ = [
    "events_from_calendar_signals",
    "persist_care_profile_from_events",
    "refresh_persisted_profile",
    "seed_care_from_agenda_fast",
]
