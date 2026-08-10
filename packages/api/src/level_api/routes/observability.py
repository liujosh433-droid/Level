"""Observability routes — expose registered agents + bias profile for the UI.

These endpoints are how the web frontend renders "Meet Level's agents" (a
demo moment for the video pitch) and the bias profile card that visibly
updates during a session.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from level_api.auth_deps import require_user
from level_api.dependencies import get_memory, get_registry
from level_core.agents.registry import AgentRegistry
from level_core.memory.base import MemoryBank
from level_core.schemas.agent import AgentVersion, RegisteredAgent
from level_core.schemas.bias import BiasEvent, BiasProfile

router = APIRouter(prefix="/v1", tags=["observability"])


@router.get("/agents", response_model=list[RegisteredAgent])
async def list_agents(registry: AgentRegistry = Depends(get_registry)) -> list[RegisteredAgent]:
    return await registry.list_agents()


@router.get("/agents/{name}/versions", response_model=list[AgentVersion])
async def list_agent_versions(
    name: str, registry: AgentRegistry = Depends(get_registry)
) -> list[AgentVersion]:
    return await registry.list_versions(name)


@router.get("/users/{user_id}/bias_profile", response_model=BiasProfile | None)
async def get_bias_profile(
    user_id: str,
    session_user: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
) -> BiasProfile | None:
    if user_id != session_user:
        raise HTTPException(status_code=403, detail="Not your profile.")
    return await memory.manifestos.get_bias_profile(user_id=user_id)


@router.get("/users/{user_id}/bias_events", response_model=list[BiasEvent])
async def list_bias_events(
    user_id: str,
    session_user: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    limit: int = 100,
) -> list[BiasEvent]:
    if user_id != session_user:
        raise HTTPException(status_code=403, detail="Not your profile.")
    return await memory.turns.list_bias_events_for_user(user_id=user_id, limit=limit)


__all__ = ["router"]
