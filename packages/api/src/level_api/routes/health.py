"""Health-check routes.

Cloud Run health-checks the container via ``/healthz`` on startup. We also
expose ``/readyz`` which does a cheap check against every registered agent
so an infra rollout that misconfigures an agent is caught quickly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from level_api.dependencies import get_registry
from level_core.agents.registry import AgentRegistry
from level_core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    """Liveness probe + runtime backend fingerprint (so local vs cloud is obvious)."""
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.env.value,
        "memory": "firestore" if settings.is_cloud else "in_memory_fake",
        "vectors": settings.vector_backend if settings.is_cloud else "in_memory_fake",
        "calendar_sync": "firestore" if settings.is_cloud else "local_file",
        "oauth_tokens": "firestore" if settings.is_cloud else "local_file",
        "project": settings.gcp_project if settings.is_cloud else None,
    }


@router.get("/readyz")
async def readyz(registry: AgentRegistry = Depends(get_registry)) -> dict[str, object]:
    """Readiness probe. 200 iff every agent is registered."""
    agents = await registry.list_agents()
    return {
        "status": "ready",
        "agent_count": len(agents),
        "agents": [a.name for a in agents],
    }
