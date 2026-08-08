"""Health-check routes.

Cloud Run health-checks the container via ``/healthz`` on startup. We also
expose ``/readyz`` which does a cheap check against every registered agent
so an infra rollout that misconfigures an agent is caught quickly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from level_api.dependencies import get_registry
from level_core.agents.registry import AgentRegistry

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Cheap and always returns 200 if the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(registry: AgentRegistry = Depends(get_registry)) -> dict[str, object]:
    """Readiness probe. 200 iff every agent is registered."""
    agents = await registry.list_agents()
    return {
        "status": "ready",
        "agent_count": len(agents),
        "agents": [a.name for a in agents],
    }
