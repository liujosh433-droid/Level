"""Session routes — create a decision, take a turn, list turns."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from level_api.dependencies import get_conductor, get_memory
from level_core.agents.conductor import Conductor, SessionInput
from level_core.errors import NotFound
from level_core.memory.base import MemoryBank
from level_core.observability.logger import bind_context
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.turn import Turn

router = APIRouter(prefix="/v1", tags=["sessions"])


class CreateDecisionRequest(BaseModel):
    user_id: str = Field(min_length=1)
    initial_prompt: str | None = Field(default=None, max_length=4000)


class CreateDecisionResponse(BaseModel):
    decision: Decision


class TurnRequest(BaseModel):
    user_id: str = Field(min_length=1)
    user_text: str = Field(min_length=1, max_length=8000)
    manifesto_snippet: str = ""


class TurnResponse(BaseModel):
    turn: Turn


@router.post(
    "/decisions",
    response_model=CreateDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision(
    payload: CreateDecisionRequest,
    memory: MemoryBank = Depends(get_memory),
) -> CreateDecisionResponse:
    decision = Decision(user_id=payload.user_id, status=DecisionStatus.OPEN)
    await memory.decisions.create(decision)
    bind_context(user_id=payload.user_id, decision_id=decision.decision_id)
    return CreateDecisionResponse(decision=decision)


@router.post(
    "/decisions/{decision_id}/turns",
    response_model=TurnResponse,
)
async def take_turn(
    decision_id: str,
    payload: TurnRequest,
    conductor: Conductor = Depends(get_conductor),
) -> TurnResponse:
    bind_context(user_id=payload.user_id, decision_id=decision_id)
    turn = await conductor.run_turn(
        SessionInput(
            user_id=payload.user_id,
            decision_id=decision_id,
            user_text=payload.user_text,
            manifesto_snippet=payload.manifesto_snippet,
        )
    )
    return TurnResponse(turn=turn)


@router.get("/decisions/{decision_id}", response_model=Decision)
async def get_decision(
    decision_id: str,
    user_id: str,
    memory: MemoryBank = Depends(get_memory),
) -> Decision:
    try:
        return await memory.decisions.get(user_id=user_id, decision_id=decision_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/decisions/{decision_id}/turns", response_model=list[Turn])
async def list_turns(
    decision_id: str,
    user_id: str,
    memory: MemoryBank = Depends(get_memory),
) -> list[Turn]:
    return await memory.decisions.list_turns(user_id=user_id, decision_id=decision_id)


__all__ = ["router"]
