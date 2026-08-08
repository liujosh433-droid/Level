"""Common bootstrap for every Cloud Run Job.

Each job's ``main()`` function is wrapped with :func:`run_job` to get
consistent structured logging, OTel setup, agent registry initialization,
and error handling. On any uncaught exception the job exits non-zero so
Cloud Run marks it failed and Scheduler will retry on the next tick.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable

from level_core.agents.conductor import register_all_agents
from level_core.agents.registry import build_registry
from level_core.config import get_settings
from level_core.observability.logger import bind_context, get_logger
from level_core.observability.tracer import configure_observability

_logger = get_logger(__name__)


async def _bootstrap() -> None:
    configure_observability()
    settings = get_settings()
    bind_context(service=settings.service_name, job=True)
    registry = build_registry(settings.is_local)
    await register_all_agents(registry, settings)
    if settings.is_cloud:
        settings.assert_cloud_ready()


def run_job(job_name: str, main: Callable[[], Awaitable[int]]) -> None:
    """Run an async job entrypoint.

    Args:
        job_name: Human-readable name; appears in logs and trace attributes.
        main: Async callable that returns an exit code (0 for success).
    """

    async def _run() -> int:
        await _bootstrap()
        bind_context(job_name=job_name)
        _logger.info("job_started", job=job_name)
        try:
            code = await main()
        except Exception:  # noqa: BLE001
            _logger.exception("job_failed", job=job_name)
            return 1
        _logger.info("job_completed", job=job_name, exit_code=code)
        return code

    sys.exit(asyncio.run(_run()))


__all__ = ["run_job"]
