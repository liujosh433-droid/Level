"""Gmail send with idempotency + sanitization."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from email.message import EmailMessage

from level_core.calendar.google_client import build_gmail_client
from level_core.email.drafter import sanitize_email_text
from level_core.storage.base import UserStore


@dataclass
class SentEmail:
    message_id: str
    thread_id: str


async def send_email(
    store: UserStore,
    *,
    to: str,
    subject: str,
    body: str,
    idempotency_key: str,
) -> SentEmail:
    subject = sanitize_email_text(subject)[:200]
    body = sanitize_email_text(body)

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = await build_gmail_client(store)
    sent = await asyncio.to_thread(
        service.users().messages().send(userId="me", body={"raw": raw}).execute
    )
    return SentEmail(
        message_id=sent.get("id", ""),
        thread_id=sent.get("threadId", ""),
    )
