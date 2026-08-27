"""Email draft + send (human-in-the-loop confirmation token required).

Confirmation-token lifecycle
----------------------------

When we draft an email we mint a `confirmation_token` and register the
draft in TWO places:

  1. `_pending_drafts` (in-memory dict on this API process): fast path,
     survives just the lifetime of this instance.
  2. `store.profile["pending_email_draft"]` (Firestore, TTL=60min):
     authoritative source of truth. Survives Cloud Run instance
     replacement, multi-pod routing, restarts, etc.

The send endpoint validates against EITHER source. This matters because
Cloud Run instances can (and do) get replaced while a caregiver has a
draft open in another tab. Before this, a legitimate "send" 10 minutes
after "draft" could 400 with `unknown_confirmation_token` even though
the draft was still visible on screen.

Retry safety
------------

The pending token is dropped only AFTER Gmail confirms the send. If
Gmail times out or errors, a subsequent retry (with a fresh
idempotency key) still finds the token valid. Duplicate identical
sends (same idempotency key) are blocked by `_sent_idempotency` and
by Gmail's own idempotency handling.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from level_core.demo.seeder import is_demo_user
from level_core.email.drafter import draft_email
from level_core.email.gmail_client import send_email
from level_core.observability import get_logger
from level_core.storage.base import UserStore
from pydantic import BaseModel

from level_api.deps import get_user_store

router = APIRouter()
logger = get_logger(__name__)

# Ephemeral optimistic cache. Survives only this instance's lifetime.
# Used as a fast path so the common case (draft + send on the same
# instance within seconds) skips a Firestore read.
_pending_drafts: dict[str, dict[str, Any]] = {}
_sent_idempotency: dict[str, float] = {}

PENDING_EMAIL_DRAFT_KEY = "pending_email_draft"
IDEMPOTENCY_WINDOW_S = 600


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


def _is_profile_draft_valid(
    draft_meta: Any, confirmation_token: str
) -> bool:
    """Firestore-persisted draft is valid iff token matches AND TTL isn't past."""
    if not isinstance(draft_meta, dict):
        return False
    if draft_meta.get("confirmation_token") != confirmation_token:
        return False
    expires_at = draft_meta.get("expires_at")
    if not isinstance(expires_at, str):
        # Legacy drafts without expires_at: treat as valid; the chat
        # dispatcher's own expiry check will clean them up.
        return True
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.utcnow()
    return now < expiry


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
        if now - t > IDEMPOTENCY_WINDOW_S:
            _sent_idempotency.pop(k, None)
    if x_idempotency_key in _sent_idempotency:
        raise HTTPException(status_code=409, detail="duplicate_send")

    # Validate the confirmation token against BOTH sources of truth.
    # In-memory dict is a fast optimistic cache; the Firestore-persisted
    # `pending_email_draft` is authoritative and survives instance churn.
    in_memory_pending = _pending_drafts.get(body.confirmation_token)
    profile = dict(await store.profile.read() or {})
    profile_draft = profile.get(PENDING_EMAIL_DRAFT_KEY)
    profile_valid = _is_profile_draft_valid(profile_draft, body.confirmation_token)

    if not in_memory_pending and not profile_valid:
        logger.info(
            "email.send.unknown_token",
            user=store.user_id,
            has_memory=False,
            has_profile=isinstance(profile_draft, dict),
        )
        raise HTTPException(status_code=400, detail="unknown_confirmation_token")

    # Demo-mode short-circuit. A judge running locally without a real
    # Gmail token should still be able to click "Send" and see the
    # happy-path UX; we log the send + clear the pending draft, but
    # never hit Google. Skipping this would 502 with
    # ``gmail_send_failed`` (see the exception path below), which
    # reads as a bug during a demo even though it's technically
    # correct: no tokens means no send.
    if is_demo_user(profile):
        logger.info(
            "email.send.demo_preview",
            user=store.user_id,
            to=body.to[:64],
        )
        _pending_drafts.pop(body.confirmation_token, None)
        if profile_valid:
            profile.pop(PENDING_EMAIL_DRAFT_KEY, None)
            await store.profile.write(profile)
        _sent_idempotency[x_idempotency_key] = now
        return {
            "message_id": f"demo-{x_idempotency_key[:12]}",
            "thread_id": f"demo-{x_idempotency_key[:12]}",
            "demo": True,
            "notice": "Demo mode: draft was NOT actually emailed.",
        }

    # Send FIRST. Only drop the token from both stores on Gmail success.
    # A Gmail failure leaves the token valid so the caregiver's retry
    # (with a fresh idempotency key) works instead of 400ing.
    try:
        sent = await send_email(
            store,
            to=body.to,
            subject=body.subject,
            body=body.body,
            idempotency_key=x_idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as err:
        logger.exception(
            "email.send.failed",
            user=store.user_id,
        )
        # 502 = upstream (Gmail) failed. Clients should retry with a
        # fresh idempotency key. Token intentionally left valid so
        # the retry doesn't hit a false "draft expired" 400.
        raise HTTPException(status_code=502, detail="gmail_send_failed") from err

    _pending_drafts.pop(body.confirmation_token, None)
    if profile_valid:
        profile.pop(PENDING_EMAIL_DRAFT_KEY, None)
        await store.profile.write(profile)
    _sent_idempotency[x_idempotency_key] = now
    return {"message_id": sent.message_id, "thread_id": sent.thread_id}
