"""Shared Ask Level chat — Today and About me hit the same router."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import (
    get_calendar_sync_store,
    get_event_cue_store,
    get_memory,
    get_proposal_store,
    get_token_store,
)
from level_api.routes.profile import ProfileResponse, _build_profile_response
from level_api.services.chat_turn import run_chat_turn
from level_core.auth.tokens import TokenStore
from level_core.calendar.event_cues import EventCueStore
from level_core.calendar.proposals import ProposalStore
from level_core.calendar.sync_state import CalendarSyncStore
from level_core.memory.base import MemoryBank
from level_core.schemas.commitment import CommitmentProposal

router = APIRouter(prefix="/v1/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=4, max_length=4000)
    include_profile: bool = False


class ChatResponse(BaseModel):
    reply: str
    path: str = "general"
    proposal: CommitmentProposal | None = None
    school_proposals: list[CommitmentProposal] = Field(default_factory=list)
    wants_paper_upload: bool = False
    facts_added: int = 0
    cues_added: int = 0
    profile: ProfileResponse | None = None


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    sync_store: CalendarSyncStore = Depends(get_calendar_sync_store),
    store: ProposalStore = Depends(get_proposal_store),
    cue_store: EventCueStore = Depends(get_event_cue_store),
) -> ChatResponse:
    result = await run_chat_turn(
        user_id=user_id,
        message=payload.message,
        memory=memory,
        tokens=tokens,
        sync_store=sync_store,
        store=store,
        cue_store=cue_store,
        background_tasks=background_tasks,
    )
    profile = None
    if payload.include_profile:
        profile = await _build_profile_response(
            user_id,
            memory,
            sync_store,
            background_tasks=background_tasks,
        )
    return ChatResponse(
        reply=result.reply,
        path=result.path,
        proposal=result.proposal,
        school_proposals=result.school_proposals,
        wants_paper_upload=result.wants_paper_upload,
        facts_added=result.facts_added,
        cues_added=result.cues_added,
        profile=profile,
    )


__all__ = ["router"]
