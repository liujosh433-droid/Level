"""Unified ingest job — pulls from all configured connectors and runs the pipeline.

Cloud Scheduler hits this every N minutes. In demo / local mode it uses the
fixture caregiver narrative so Memory Bank fills without OAuth.
"""

from __future__ import annotations

import os

from level_core.agents.ingest_normalizer import IngestNormalizer
from level_core.config import get_settings
from level_core.guardrails.inbound import InboundGuardrail
from level_core.ingest.connectors import (
    FixtureConnector,
    SignalConnector,
    demo_caregiver_signals,
)
from level_core.ingest.pipeline import IngestPipeline
from level_core.memory.factory import build_memory
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_core.profile.persist import (
    events_from_calendar_signals,
    persist_care_profile_from_events,
)
from level_core.schemas.signal import Signal
from level_jobs.base import run_job

_logger = get_logger(__name__)


def _connectors_for_user(user_id: str, *, use_fixtures: bool) -> list[SignalConnector]:
    if not use_fixtures:
        return []
    signals = demo_caregiver_signals(user_id=user_id)
    by_source: dict[str, list] = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)
    return [
        FixtureConnector(source=source, signals=items)
        for source, items in by_source.items()
    ]


async def main() -> int:
    settings = get_settings()
    memory = build_memory(settings)
    gemini = build_gemini_client(settings)
    embedder = build_embedding_client(settings)
    pipeline = IngestPipeline(
        memory=memory,
        normalizer=IngestNormalizer(gemini=gemini, model_id=settings.fast_model),
        embedder=embedder,
        guardrail=InboundGuardrail(settings=settings),
    )

    user_ids_env = os.getenv("LEVEL_JOB_USER_IDS", "")
    user_ids = [u.strip() for u in user_ids_env.split(",") if u.strip()]
    if not user_ids:
        _logger.info(
            "ingest_no_users",
            note="Set LEVEL_JOB_USER_IDS (comma-separated). Fixtures require LEVEL_INGEST_FIXTURES=1.",
        )
        return 0
    use_fixtures = os.getenv("LEVEL_INGEST_FIXTURES", "0").lower() in {
        "1",
        "true",
        "yes",
    }
    if not use_fixtures:
        _logger.info(
            "ingest_skip_no_fixtures",
            note=(
                "Live job ingest is driven by API calendar sync. "
                "Set LEVEL_INGEST_FIXTURES=1 for the pitch fixture narrative."
            ),
        )
        return 0

    accepted = 0
    blocked = 0
    skipped = 0
    facts = 0
    care_profiles = 0

    for user_id in user_ids:
        calendar_signals: list[Signal] = []
        for connector in _connectors_for_user(user_id, use_fixtures=use_fixtures):
            async for signal in connector.fetch(user_id=user_id):
                result = await pipeline.run(signal)
                if result.blocked:
                    blocked += 1
                elif result.skipped_duplicate:
                    skipped += 1
                elif result.signal is not None:
                    accepted += 1
                    facts += len(result.facts)
                    calendar_signals.append(result.signal)

        # Evolving Knowledge: mutate Care Profile from ingested calendar signals.
        events = events_from_calendar_signals(calendar_signals)
        if not events and use_fixtures:
            # Re-read fixture GCAL texts even if duplicates were skipped.
            events = events_from_calendar_signals(demo_caregiver_signals(user_id=user_id))
        if events:
            snap = await persist_care_profile_from_events(
                memory, user_id, events, embed=True
            )
            if snap is not None:
                care_profiles += 1

    _logger.info(
        "ingest_job_complete",
        accepted=accepted,
        blocked=blocked,
        skipped=skipped,
        facts=facts,
        care_profiles_mutated=care_profiles,
        users=len(user_ids),
    )
    return 0


def cli() -> None:
    run_job("ingest_all", main)


if __name__ == "__main__":
    cli()


__all__ = ["cli", "main"]
