"""Email draft + send (human-in-the-loop confirmation token required)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from level_core.email.drafter import draft_email
from level_core.email.gmail_client import send_email
from level_core.storage.base import UserStore
from pydantic import BaseModel

from level_api.deps import get_user_store

router = APIRouter()

_pending_drafts: dict[str, dict[str, Any]] = {}
_sent_idempotency: dict[str, float] = {}


def register_pending_draft(token: str, to: str | None) -> None:
    _pending_drafts[token] = {"to": to, "at": time.time()}


class DraftBody(BaseModel):
    intent: str
    contact_id: str
    extra_notes: str = ""


class SendBody(BaseModel):
    confirmation_token: str
    to: str
    subject: str
    body: str


@router.post("/draft")
async def draft(body: DraftBody, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    contact = await store.contacts.get(body.contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="contact_not_found")
    person = await store.people.get(contact.person_id)
    drafted = await draft_email(
        store,
        intent=body.intent,
        contact_display_name=contact.name,
        kid_display_name=person.display_name if person else None,
        extra_notes=body.extra_notes,
    )
    register_pending_draft(drafted.confirmation_token, contact.email)
    return {
        "subject": drafted.subject,
        "body": drafted.body,
        "confirmation_token": drafted.confirmation_token,
        "to": contact.email,
    }


@router.post("/send")
async def send(
    body: SendBody,
    store: UserStore = Depends(get_user_store),
    x_idempotency_key: str | None = Header(default=None),
) -> dict[str, Any]:
    if not x_idempotency_key:
        raise HTTPException(status_code=400, detail="missing_idempotency_key")
    now = time.time()
    for k, t in list(_sent_idempotency.items()):
        if now - t > 600:
            _sent_idempotency.pop(k, None)
    if x_idempotency_key in _sent_idempotency:
        raise HTTPException(status_code=409, detail="duplicate_send")

    pending = _pending_drafts.pop(body.confirmation_token, None)
    if not pending:
        raise HTTPException(status_code=400, detail="unknown_confirmation_token")

    profile = dict(await store.profile.read() or {})
    draft_meta = profile.get("pending_email_draft")
    if isinstance(draft_meta, dict) and draft_meta.get("confirmation_token") == body.confirmation_token:
        profile.pop("pending_email_draft", None)
        await store.profile.write(profile)

    sent = await send_email(
        store,
        to=body.to,
        subject=body.subject,
        body=body.body,
        idempotency_key=x_idempotency_key,
    )
    _sent_idempotency[x_idempotency_key] = now
    return {"message_id": sent.message_id, "thread_id": sent.thread_id}
