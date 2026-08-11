"""Shared Care Profile persistence used by API sync and ingest jobs.

Evolving Knowledge Engine: calendar (and similar) signals actively *mutate*
the Care Profile — not just get read at session time.

Primary path: events + memory → Gemini holistic infer → Care Profile.
Heuristic regex inference is offline fallback only.
"""

from __future__ import annotations

from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.care_infer_llm import (
    infer_care_profile_ai,
    reconcile_exclusive_people,
)
from level_core.profile.synthesize import (
    infer_care_profile_heuristic,
    refresh_profile_and_manifesto,
)
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal, SignalSource

_logger = get_logger(__name__)


async def persist_care_profile_from_events(
    memory: MemoryBank,
    user_id: str,
    events: list[dict[str, str | None]],
    *,
    embed: bool = True,
) -> ProfileSnapshot | None:
    """Infer Care Profile from agenda-like events via AI, then persist.

    Preserves Keep / Not me via previous-profile merge inside the AI builder.
    Falls back to heuristic inference only when Gemini is unavailable.
    """
    if not events:
        return None
    previous = await memory.manifestos.get_care_profile(user_id=user_id)
    existing = await memory.facts.list_for_user(user_id=user_id, limit=200)
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

    if care is None:
        source = "heuristic_fallback"
        care, facts = infer_care_profile_heuristic(
            events, user_id=user_id, previous=previous
        )
        care = reconcile_exclusive_people(care)
        _logger.warning("care_profile_heuristic_fallback", user_id=user_id)

    if not care.roles and not facts:
        return None

    await _persist_pattern_facts(memory, facts, embed=embed)
    from level_core.profile.synthesize import invalidate_care_graph_cache

    invalidate_care_graph_cache(user_id)
    await memory.manifestos.save_care_profile(care)
    snap = await refresh_persisted_profile(memory, user_id)
    _logger.info(
        "care_profile_mutated",
        user_id=user_id,
        roles=len(care.roles),
        facts=len(facts),
        version=care.version,
        source=source,
        calendar_hints=len(care.calendar_role_by_summary),
    )
    return snap


async def refresh_persisted_profile(memory: MemoryBank, user_id: str) -> ProfileSnapshot:
    """Rebuild snapshot + manifesto from facts + care profile and persist."""
    facts = await memory.facts.list_for_user(user_id=user_id, limit=200)
    prev = await memory.manifestos.get_current_manifesto(user_id=user_id)
    care = await memory.manifestos.get_care_profile(user_id=user_id)
    if care is not None:
        care = reconcile_exclusive_people(care)
    snapshot, manifesto, care_out = await refresh_profile_and_manifesto(
        user_id=user_id,
        facts=facts,
        previous_manifesto=prev,
        care_profile=care,
    )
    await memory.manifestos.save_profile_snapshot(snapshot)
    await memory.manifestos.save_manifesto(manifesto)
    if care_out is not None:
        await memory.manifestos.save_care_profile(care_out)
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
]
