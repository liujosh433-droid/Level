"""Fact retention — prune low-value evidence while keeping Care Profile accurate.

HOT (never pruned): Care Profile itself, Keep'd / care-pinned facts, recently cited facts.
WARM (soft-capped): other durable + recent EVENT facts.
Ephemeral EVENT facts past ``event_ttl_days`` are pruned unless HOT.

Cold GCS/BigQuery archive is a scale-up path (not implemented here) — we delete
vectors + fact docs under hackathon budget after scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from level_core.memory.base import MemoryBank
from level_core.observability.logger import get_logger
from level_core.schemas.care import CareProfile
from level_core.schemas.decision import DecisionStatus
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Reasonable hackathon defaults — tunable without code changes in callers."""

    max_facts_per_user: int = 150
    event_ttl_days: int = 90
    citation_lookback_days: int = 90
    list_limit: int = 2000


DEFAULT_POLICY = RetentionPolicy()


@dataclass(slots=True)
class PruneResult:
    user_id: str
    examined: int = 0
    pruned: int = 0
    protected: int = 0
    pruned_fact_ids: list[str] = field(default_factory=list)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def protected_fact_ids(care: CareProfile | None) -> set[str]:
    """Facts tied to active / Keep'd care roles — never prune."""
    if care is None:
        return set()
    out: set[str] = set()
    for role in care.roles:
        if role.status is BulletStatus.REJECTED:
            continue
        out.update(role.source_fact_ids)
    return out


async def cited_fact_ids(
    memory: MemoryBank,
    *,
    user_id: str,
    since: datetime,
) -> set[str]:
    """Fact ids cited on turns in open/recent decisions within the lookback window."""
    cited: set[str] = set()
    decisions = await memory.decisions.list_for_user(user_id=user_id, limit=40)
    for decision in decisions:
        opened = _as_utc(decision.opened_at) or _as_utc(decision.created_at)
        if (
            decision.status is not DecisionStatus.OPEN
            and opened is not None
            and opened < since
        ):
            continue
        turns = await memory.decisions.list_turns(
            user_id=user_id, decision_id=decision.decision_id
        )
        for turn in turns:
            turn_at = _as_utc(turn.updated_at) or _as_utc(turn.created_at)
            if turn_at is not None and turn_at < since:
                continue
            for q in turn.challenger_questions:
                for c in q.citations:
                    if c.fact_id:
                        cited.add(c.fact_id)
    return cited


def eviction_score(
    fact: Fact,
    *,
    now: datetime,
    pinned: set[str],
    cited: set[str],
) -> float:
    """Higher = keep. Lower = prune first."""
    if fact.fact_id in pinned or fact.fact_id in cited:
        return 1_000.0
    updated = _as_utc(fact.updated_at) or _as_utc(fact.created_at) or now
    age_days = max(0.0, (now - updated).total_seconds() / 86400.0)
    recency = max(0.0, 1.0 - min(age_days / 180.0, 1.0))
    ephemeral = 0.35 if fact.type is FactType.EVENT else 0.0
    return (0.45 * fact.salience) + (0.40 * recency) + (0.15 * fact.confidence) - ephemeral


def select_facts_to_prune(
    facts: list[Fact],
    *,
    pinned: set[str],
    cited: set[str],
    policy: RetentionPolicy,
    now: datetime | None = None,
) -> list[Fact]:
    """Return facts that should be deleted under TTL + soft cap."""
    now = now or datetime.now(tz=timezone.utc)
    ttl_cutoff = now - timedelta(days=policy.event_ttl_days)
    doomed: list[Fact] = []
    survivors: list[Fact] = []

    for fact in facts:
        if fact.fact_id in pinned or fact.fact_id in cited:
            survivors.append(fact)
            continue
        # Explicit TTL on EVENT facts past window.
        stamp = _as_utc(fact.updated_at) or _as_utc(fact.created_at)
        if (
            fact.type is FactType.EVENT
            and stamp is not None
            and stamp < ttl_cutoff
        ):
            doomed.append(fact)
            continue
        survivors.append(fact)

    overflow = len(survivors) - policy.max_facts_per_user
    if overflow > 0:
        ranked = sorted(
            survivors,
            key=lambda f: eviction_score(f, now=now, pinned=pinned, cited=cited),
        )
        # Only prune non-protected (score < 1000).
        extras = [f for f in ranked if f.fact_id not in pinned and f.fact_id not in cited]
        doomed.extend(extras[:overflow])

    # Dedupe by fact_id preserving order
    seen: set[str] = set()
    unique: list[Fact] = []
    for f in doomed:
        if f.fact_id in seen:
            continue
        seen.add(f.fact_id)
        unique.append(f)
    return unique


async def prune_user_facts(
    memory: MemoryBank,
    *,
    user_id: str,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
) -> PruneResult:
    """Apply retention for one user: delete facts + vectors; never touch Care Profile."""
    policy = policy or DEFAULT_POLICY
    now = now or datetime.now(tz=timezone.utc)
    result = PruneResult(user_id=user_id)

    facts = await memory.facts.list_for_user(user_id=user_id, limit=policy.list_limit)
    result.examined = len(facts)
    if not facts:
        return result

    care = await memory.manifestos.get_care_profile(user_id=user_id)
    pinned = protected_fact_ids(care)
    cited = await cited_fact_ids(
        memory,
        user_id=user_id,
        since=now - timedelta(days=policy.citation_lookback_days),
    )
    result.protected = len(pinned | cited)

    to_prune = select_facts_to_prune(
        facts, pinned=pinned, cited=cited, policy=policy, now=now
    )
    for fact in to_prune:
        await memory.facts.delete(user_id=user_id, fact_id=fact.fact_id)
        try:
            await memory.vectors.delete(user_id=user_id, fact_id=fact.fact_id)
        except Exception:  # noqa: BLE001
            _logger.warning(
                "retention_vector_delete_failed",
                user_id=user_id,
                fact_id=fact.fact_id,
            )
        result.pruned_fact_ids.append(fact.fact_id)
        result.pruned += 1

    _logger.info(
        "retention_prune_complete",
        user_id=user_id,
        examined=result.examined,
        pruned=result.pruned,
        protected=result.protected,
        max_facts=policy.max_facts_per_user,
        event_ttl_days=policy.event_ttl_days,
    )
    return result


__all__ = [
    "DEFAULT_POLICY",
    "PruneResult",
    "RetentionPolicy",
    "cited_fact_ids",
    "eviction_score",
    "protected_fact_ids",
    "prune_user_facts",
    "select_facts_to_prune",
]
