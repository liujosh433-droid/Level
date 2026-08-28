"""Gmail send with idempotency + sanitization."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from level_core.calendar.google_client import build_gmail_client
from level_core.config import get_settings
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
    """Send an email via the user's Gmail account.

    ``idempotency_key`` is used to stamp the outgoing MIME message
    with a ``Message-Id`` header so a retry from the same caller
    lands as the same thread instead of a duplicate. The
    HTTP-level idempotency guard in ``routes/email.py`` still
    protects the endpoint; this header protects the recipient's
    inbox from seeing the same draft twice when a transient network
    error causes a resend.
    """
    subject = sanitize_email_text(subject)[:200]
    body = sanitize_email_text(body)

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if idempotency_key:
        # RFC 5322 Message-Id needs an "id-left@id-right" form. We
        # scope with a stable ``level.local`` label so the recipient's
        # MUA can dedupe cleanly across retries.
        msg["Message-Id"] = f"<{idempotency_key}@level.local>"
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


def _build_gmail_client_from_refresh_token(refresh_token: str) -> Any:
    """Build a Gmail v1 client from a bare refresh token + OAuth client creds.

    Only used by the demo "real send" path in ``routes/email.py``.
    Everywhere else, Gmail creds come from ``store.tokens`` (per-user
    OAuth); demo mode has no such tokens, so this variant lets the
    operator wire their own inbox in via env vars without touching
    the demo user's storage.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    settings = get_settings()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


async def send_email_with_refresh_token(
    *,
    refresh_token: str,
    to: str,
    subject: str,
    body: str,
) -> SentEmail:
    """Send a Gmail message using an operator-provided refresh token.

    Distinct entry point (not overloading ``send_email``) so the
    normal per-user path never accidentally reaches for env creds.
    The demo real-send branch is the only caller; see
    ``level_core.config.Settings.level_demo_gmail_refresh_token``.
    """
    subject = sanitize_email_text(subject)[:200]
    body = sanitize_email_text(body)

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = _build_gmail_client_from_refresh_token(refresh_token)
    sent = await asyncio.to_thread(
        service.users().messages().send(userId="me", body={"raw": raw}).execute
    )
    return SentEmail(
        message_id=sent.get("id", ""),
        thread_id=sent.get("threadId", ""),
    )
