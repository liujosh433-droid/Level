"""Retention job — prune low-value facts under TTL + soft caps.

Keeps Care Profile / Keep'd / recently cited evidence. Run via
``level-retain`` (Cloud Scheduler nightly in production).
"""

from __future__ import annotations

import os

from level_core.config import get_settings
from level_core.memory.factory import build_memory
from level_core.memory.retention import DEFAULT_POLICY, RetentionPolicy, prune_user_facts
from level_core.observability.logger import get_logger
from level_jobs.base import run_job

_logger = get_logger(__name__)


def _policy_from_env() -> RetentionPolicy:
    max_facts = int(os.getenv("LEVEL_RETENTION_MAX_FACTS", str(DEFAULT_POLICY.max_facts_per_user)))
    ttl = int(os.getenv("LEVEL_RETENTION_EVENT_TTL_DAYS", str(DEFAULT_POLICY.event_ttl_days)))
    return RetentionPolicy(max_facts_per_user=max_facts, event_ttl_days=ttl)


async def main() -> int:
    settings = get_settings()
    memory = build_memory(settings)
    policy = _policy_from_env()
    user_ids_env = os.getenv("LEVEL_JOB_USER_IDS", "")
    user_ids = [u.strip() for u in user_ids_env.split(",") if u.strip()]
    if not user_ids:
        _logger.info(
            "retention_no_users",
            note="Set LEVEL_JOB_USER_IDS as a comma-separated list.",
        )
        return 0

    total_pruned = 0
    for user_id in user_ids:
        try:
            result = await prune_user_facts(memory, user_id=user_id, policy=policy)
            total_pruned += result.pruned
        except Exception:  # noqa: BLE001
            _logger.exception("retention_user_failed", user_id=user_id)
    _logger.info(
        "retention_job_complete",
        users=len(user_ids),
        pruned=total_pruned,
        max_facts=policy.max_facts_per_user,
        event_ttl_days=policy.event_ttl_days,
    )
    return 0


def cli() -> None:
    run_job("retain", main)


if __name__ == "__main__":
    cli()


__all__ = ["cli", "main"]
