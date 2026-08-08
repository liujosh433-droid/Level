#!/usr/bin/env python3
"""Seed the demo caregiver narrative into the Memory Bank.

Usage:
    LEVEL_ENV=local uv run python scripts/seed_demo_data.py

Optionally run a full conductor turn after seeding:
    SEED_RUN_TURN=1 LEVEL_ENV=local uv run python scripts/seed_demo_data.py
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "core", "src"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "jobs", "src"))

    from level_core.agents.ingest_normalizer import IngestNormalizer
    from level_core.config import get_settings
    from level_core.guardrails.inbound import InboundGuardrail
    from level_core.ingest.connectors import demo_caregiver_signals
    from level_core.ingest.pipeline import IngestPipeline
    from level_core.memory.factory import build_memory
    from level_core.models.factory import build_embedding_client, build_gemini_client
    from level_core.schemas.bias import BiasProfile, Manifesto
    from level_core.schemas.decision import Decision, DecisionStatus

    settings = get_settings()
    user_id = os.getenv("LEVEL_DEMO_USER_ID", "demo-parent")
    print(f"Seeding demo data for user={user_id} env={settings.env.value}")

    memory = build_memory(settings)
    gemini = build_gemini_client(settings)
    embedder = build_embedding_client(settings)
    pipeline = IngestPipeline(
        memory=memory,
        normalizer=IngestNormalizer(gemini=gemini, model_id=settings.fast_model),
        embedder=embedder,
        guardrail=InboundGuardrail(settings=settings),
    )

    total_facts = 0
    for signal in demo_caregiver_signals(user_id=user_id):
        result = await pipeline.run(signal)
        status = (
            "blocked"
            if result.blocked
            else "skip"
            if result.skipped_duplicate
            else f"ok(+{len(result.facts)} facts)"
        )
        print(f"  {signal.source.value:12} {signal.external_id:28} → {status}")
        total_facts += len(result.facts)

    manifesto = Manifesto(
        user_id=user_id,
        statement=(
            "I want to be present for Maya during the school year. Career growth "
            "matters, but not at the cost of evenings I can't get back. I prefer "
            "reversible experiments over one-way doors."
        ),
        version=1,
    )
    await memory.manifestos.save_manifesto(manifesto)
    await memory.manifestos.save_bias_profile(BiasProfile(user_id=user_id))

    decision = Decision(user_id=user_id, status=DecisionStatus.OPEN)
    await memory.decisions.create(decision)
    print(f"Created open decision: {decision.decision_id}")
    print(f"Total facts extracted: {total_facts}")

    if os.getenv("SEED_RUN_TURN") == "1":
        from level_core.agents.conductor import SessionInput, build_conductor

        conductor = build_conductor(
            memory=memory, gemini=gemini, embedder=embedder, settings=settings
        )
        turn = await conductor.run_turn(
            SessionInput(
                user_id=user_id,
                decision_id=decision.decision_id,
                user_text=(
                    "I think I should apply for the promotion. Everyone says I'd be "
                    "great at it. And maybe switch Maya to the dual-language school "
                    "at the same time — her friend is going."
                ),
            )
        )
        print(f"Turn status: {turn.status.value}")
        for q in turn.challenger_questions:
            print(f"  Q [{q.challenge_type}]: {q.question}")
            for c in q.citations:
                print(f"     cite {c.fact_id}: {c.quote[:80]}")

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
