"""Thin wrapper around EmailAgent that adds a confirmation_token."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from level_core.agents.email import run as email_run
from level_core.storage.base import UserStore


@dataclass
class DraftedEmail:
    subject: str
    body: str
    confirmation_token: str


async def draft_email(
    store: UserStore,
    *,
    intent: str,
    contact_display_name: str,
    kid_display_name: str | None = None,
    extra_notes: str = "",
) -> DraftedEmail | None:
    result = await email_run(
        store=store,
        intent=intent,
        contact_display_name=contact_display_name,
        kid_display_name=kid_display_name,
        extra_notes=extra_notes,
    )
    if not result.value:
        return None
    draft = result.value.draft  # type: ignore[union-attr]
    return DraftedEmail(
        subject=draft.subject,
        body=draft.body,
        confirmation_token=secrets.token_urlsafe(24),
    )


def sanitize_email_text(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
    return cleaned[:5000]
