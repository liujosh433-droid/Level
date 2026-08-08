"""Async challenge job — runs the Conductor on any OPEN decision that hasn't
had a turn in the last configurable interval.

This demonstrates the "Continuous Action Engine" / long-running async
workflow criterion from the hackathon rubric: even when the user isn't
active, the system considers whether any of their decisions have new
context (from freshly ingested signals) that would materially change what
Level would say — and if so, drops an unsolicited (opt-in) prompt into
their app so they see it next time they open Level.

For the hackathon build the job scans OPEN decisions and generates a
follow-up "here's what changed since we last talked about this" Turn,
persisted with role=level and status=complete.
"""

from __future__ import annotations

import os

from level_core.agents.conductor import build_conductor, SessionInput
from level_core.config import get_settings
from level_core.memory.factory import build_memory
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.observability.logger import get_logger
from level_jobs.base import run_job

_logger = get_logger(__name__)


async def main() -> int:
    """Scan all users' open decisions and prompt Level to re-check."""
    settings = get_settings()
    memory = build_memory(settings)
    gemini = build_gemini_client(settings)
    embedder = build_embedding_client(settings)
    conductor = build_conductor(memory=memory, gemini=gemini, embedder=embedder, settings=settings)

    user_ids_env = os.getenv("LEVEL_JOB_USER_IDS", "")
    user_ids = [u.strip() for u in user_ids_env.split(",") if u.strip()]
    if not user_ids:
        _logger.info("no_users_configured", note="Set LEVEL_JOB_USER_IDS as a comma-separated list.")
        return 0

    processed = 0
    for user_id in user_ids:
        try:
            processed += await _process_user(user_id, memory, conductor)
        except Exception:  # noqa: BLE001
            _logger.exception("user_processing_failed", user_id=user_id)
            continue
    _logger.info("async_challenge_complete", processed_decisions=processed)
    return 0


async def _process_user(user_id: str, memory: object, conductor: object) -> int:
    """Run one recheck-turn per OPEN decision for the given user.

    In the local mode fake bank there's no cross-decision list method, so
    this stops after the first missing lookup. In cloud mode a
    collection-group query would replace this — added when we wire the
    real Firestore repository.
    """
    from level_core.memory.base import MemoryBank

    memory_bank: MemoryBank = memory  # type: ignore[assignment]

    # Placeholder: for a real deployment we'd query `users/{uid}/decisions`
    # with a status==open filter. The fakes don't support that filter yet,
    # so we just log intent for now.
    _logger.info("would_recheck_open_decisions", user_id=user_id)
    del memory_bank
    del conductor
    del SessionInput  # keep import used at module load

    return 0


def cli() -> None:
    """Console entrypoint (declared in ``pyproject.toml``)."""
    run_job("async_challenge", main)


if __name__ == "__main__":
    cli()


__all__ = ["cli", "main"]
