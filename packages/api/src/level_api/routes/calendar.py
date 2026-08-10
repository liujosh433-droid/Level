"""Calendar commitment gate — propose / confirm / decline schedule changes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from level_api.auth_deps import require_user
from level_api.dependencies import get_memory, get_proposal_store, get_token_store
from level_core.auth.tokens import TokenStore
from level_core.calendar.commitment_gate import apply_draft_to_window, propose_from_text
from level_core.calendar.proposals import ProposalStore
from level_core.config import get_settings
from level_core.ingest.google_live import create_calendar_event
from level_core.memory.base import MemoryBank
from level_core.models.factory import build_gemini_client
from level_core.schemas.commitment import CommitmentProposal, ProposalStatus
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource

router = APIRouter(prefix="/v1/calendar", tags=["calendar"])


class ProposeRequest(BaseModel):
    text: str = Field(min_length=4, max_length=2000)


class ProposeResponse(BaseModel):
    is_schedule_ask: bool
    proposal: CommitmentProposal | None = None


class ConfirmRequest(BaseModel):
    use_slot_start: str | None = Field(
        default=None,
        description="ISO start from a suggested free_slot to book instead of the original time.",
    )


class ConfirmResponse(BaseModel):
    proposal: CommitmentProposal
    google_event_id: str | None = None
    html_link: str | None = None


@router.post("/propose", response_model=ProposeResponse)
async def propose_commitment(
    payload: ProposeRequest,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    store: ProposalStore = Depends(get_proposal_store),
) -> ProposeResponse:
    token = await tokens.get_google_token(user_id)
    if token is None:
        raise HTTPException(status_code=400, detail="Connect Google Calendar on Sources first.")
    settings = get_settings()
    gemini = build_gemini_client(settings)
    try:
        proposal = await propose_from_text(
            user_id=user_id,
            user_text=payload.text,
            token=token,
            memory=memory,
            store=store,
            gemini=gemini,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Schedule check failed: {exc}") from exc
    if proposal is None:
        return ProposeResponse(is_schedule_ask=False, proposal=None)
    return ProposeResponse(is_schedule_ask=True, proposal=proposal)


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=ConfirmResponse,
)
async def confirm_proposal(
    proposal_id: str,
    payload: ConfirmRequest | None = None,
    user_id: str = Depends(require_user),
    memory: MemoryBank = Depends(get_memory),
    tokens: TokenStore = Depends(get_token_store),
    store: ProposalStore = Depends(get_proposal_store),
) -> ConfirmResponse:
    payload = payload or ConfirmRequest()
    proposal = await store.get(proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    if proposal.status is not ProposalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal.status.value}.")

    token = await tokens.get_google_token(user_id)
    if token is None:
        raise HTTPException(status_code=400, detail="Google Calendar disconnected — reconnect on Sources.")

    start, end = apply_draft_to_window(
        proposal.draft, slot_start_iso=payload.use_slot_start
    )
    by_days = (
        [d.value for d in proposal.draft.by_days]
        if proposal.draft.recurring and proposal.draft.by_days
        else None
    )
    try:
        created = await create_calendar_event(
            token,
            summary=proposal.draft.title,
            start=start,
            end=end,
            timezone_name=proposal.draft.timezone,
            description=proposal.draft.notes
            or f"Confirmed in Level from: {proposal.user_text[:200]}",
            by_days=by_days,
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        if "insufficient" in detail.lower() or "scope" in detail.lower() or "403" in detail:
            detail += " Reconnect Google on Sources to grant calendar edit access."
        raise HTTPException(status_code=502, detail=f"Calendar write failed: {detail}") from exc

    event_id = created.get("id")
    proposal.status = ProposalStatus.CONFIRMED
    proposal.google_event_id = event_id
    proposal.resolved_at = datetime.now(tz=timezone.utc)
    proposal.touch()
    await store.save(proposal)

    try:
        fact = Fact(
            user_id=user_id,
            type=FactType.COMMITMENT,
            statement=(
                f"I committed to {proposal.draft.title} "
                f"({proposal.summary})."
            )[:500],
            source_signal_ids=[],
            salience=0.75,
        )
        await memory.facts.upsert(fact)
        signal = Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id=f"gcal:{event_id}" if event_id else f"level:{proposal.proposal_id}",
            text=f"Calendar (Level-confirmed): {proposal.summary}",
            occurred_at=start,
        )
        await memory.signals.upsert(signal)
    except Exception:  # noqa: BLE001
        pass

    return ConfirmResponse(
        proposal=proposal,
        google_event_id=event_id,
        html_link=created.get("htmlLink"),
    )


@router.post(
    "/proposals/{proposal_id}/decline",
    response_model=CommitmentProposal,
)
async def decline_proposal(
    proposal_id: str,
    user_id: str = Depends(require_user),
    store: ProposalStore = Depends(get_proposal_store),
) -> CommitmentProposal:
    proposal = await store.get(proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    if proposal.status is not ProposalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal.status.value}.")
    proposal.status = ProposalStatus.DECLINED
    proposal.resolved_at = datetime.now(tz=timezone.utc)
    proposal.touch()
    await store.save(proposal)
    return proposal


@router.get("/proposals/{proposal_id}", response_model=CommitmentProposal)
async def get_proposal(
    proposal_id: str,
    user_id: str = Depends(require_user),
    store: ProposalStore = Depends(get_proposal_store),
) -> CommitmentProposal:
    proposal = await store.get(proposal_id)
    if proposal is None or proposal.user_id != user_id:
        raise HTTPException(status_code=404, detail="Proposal not found.")
    return proposal


__all__ = ["router"]
