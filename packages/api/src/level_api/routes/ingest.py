"""Ingestion routes — manual signal submission + read APIs.

The primary ingestion path is Cloud Run Jobs (see ``packages/jobs``), but
we also expose a manual signal-submission endpoint so users can drop
voice memos, chat exports, or arbitrary text into their Memory Bank
directly from the UI.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from level_api.dependencies import get_memory
from level_core.errors import GuardrailBlocked
from level_core.guardrails.inbound import InboundGuardrail
from level_core.memory.base import MemoryBank
from level_core.schemas.signal import Signal, SignalSource

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])


class SubmitSignalRequest(BaseModel):
    user_id: str = Field(min_length=1)
    source: SignalSource
    external_id: str = Field(min_length=1, max_length=200)
    text: str | None = Field(default=None, max_length=32000)
    storage_uri: str | None = None


@router.post(
    "/signal",
    response_model=Signal,
    status_code=status.HTTP_201_CREATED,
)
async def submit_signal(
    payload: SubmitSignalRequest,
    memory: MemoryBank = Depends(get_memory),
) -> Signal:
    signal = Signal(
        user_id=payload.user_id,
        source=payload.source,
        external_id=payload.external_id,
        text=payload.text,
        storage_uri=payload.storage_uri,
    )
    guardrail = InboundGuardrail()
    try:
        sanitized = guardrail.sanitize(signal)
    except GuardrailBlocked as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.reason) from exc
    await memory.signals.upsert(sanitized.signal)
    return sanitized.signal


@router.get("/signals", response_model=list[Signal])
async def list_signals(
    user_id: str,
    source: SignalSource,
    memory: MemoryBank = Depends(get_memory),
) -> list[Signal]:
    return await memory.signals.list_by_source(user_id=user_id, source=source.value)


__all__ = ["router"]
