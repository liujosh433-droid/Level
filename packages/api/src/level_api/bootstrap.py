"""Local-mode demo Memory Bank seed so Ask Level works out of the box."""

from __future__ import annotations

from level_core.config import Settings
from level_core.ingest.connectors import demo_caregiver_signals
from level_core.memory.base import MemoryBank
from level_core.models.base import EmbeddingClient
from level_core.observability.logger import get_logger
from level_core.schemas.bias import BiasProfile, Manifesto
from level_core.schemas.signal import Fact, FactType

_logger = get_logger(__name__)

# Hand-authored facts from the demo narrative — skip the Normalizer LLM at
# startup so boot stays fast and deterministic.
_DEMO_FACTS: list[tuple[FactType, str]] = [
    (
        FactType.VALUE_STATEMENT,
        "I value being present for Maya during the school year",
    ),
    (
        FactType.DECISION_HISTORY,
        "I said last year that switching schools mid-year was too disruptive",
    ),
    (
        FactType.CONSTRAINT,
        "Mondays are already the hardest with pickup, dinner, and homework",
    ),
    (
        FactType.COMMITMENT,
        "I committed to cooking Sunday dinner every week",
    ),
    (
        FactType.RELATIONSHIP,
        "Co-parent works nights Tue/Thu so those evenings I'm solo for bedtime",
    ),
    (
        FactType.EVENT,
        "Picture Day is Friday morning and I can't be late to the 9am standup",
    ),
]


async def seed_local_demo(
    *,
    memory: MemoryBank,
    embedder: EmbeddingClient,
    settings: Settings,
    user_id: str = "demo-parent",
) -> int:
    """Idempotently seed demo facts + manifesto for local UI demos."""
    if not settings.is_local:
        return 0

    existing = await memory.facts.list_for_user(user_id=user_id, limit=1)
    if existing:
        _logger.info("local_demo_already_seeded", user_id=user_id)
        return 0

    # Persist raw signals for provenance / UI inspection.
    for signal in demo_caregiver_signals(user_id=user_id):
        await memory.signals.upsert(signal)

    count = 0
    for fact_type, statement in _DEMO_FACTS:
        fact = Fact(user_id=user_id, type=fact_type, statement=statement)
        await memory.facts.upsert(fact)
        [embedding] = await embedder.embed(texts=[statement])
        await memory.vectors.upsert(
            user_id=user_id,
            fact_id=fact.fact_id,
            text=statement,
            embedding=embedding,
        )
        count += 1

    await memory.manifestos.save_manifesto(
        Manifesto(
            user_id=user_id,
            statement=(
                "I want to be present for Maya during the school year. Career growth "
                "matters, but not at the cost of evenings I can't get back."
            ),
            version=1,
        )
    )
    await memory.manifestos.save_bias_profile(BiasProfile(user_id=user_id))
    _logger.info("local_demo_seeded", user_id=user_id, fact_count=count)
    return count
