"""Health check + trace id.

The public health probe deliberately does NOT include model
configuration (``level_model_pro`` / ``level_model_flash``). Those
values are useful to operators inspecting a deploy, but they're not
safe to broadcast on an unauthenticated endpoint — model names
double as vendor fingerprinting for capacity-planning attackers and
narrow the search space for prompt-injection tuning. Operators can
still see the configured model tiers via ``/v1/admin/agents``, which
is session-gated.
"""

from __future__ import annotations

from fastapi import APIRouter
from level_core.config import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.level_env,
        "version": "2.0.0",
    }
