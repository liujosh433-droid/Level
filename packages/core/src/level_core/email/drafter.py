"""Thin wrapper around EmailAgent that adds a confirmation_token."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from level_core.agents.base import QuotaExhausted
from level_core.agents.email import run as email_run
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.storage.base import UserStore

logger = get_logger(__name__)

_GENERIC_SELF = {"you", "me", "self", "myself", "parent", "a parent"}

_PLACEHOLDERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[(?:your\s+)?name\]", re.I), "signer"),
    (re.compile(r"\[(?:the\s+)?caregiver(?:'s)?\s+name\]", re.I), "signer"),
    (re.compile(r"\[parent(?:'s)?\s+name\]", re.I), "signer"),
    (re.compile(r"\[(?:current\s+)?date\]", re.I), "today"),
    (re.compile(r"\[today(?:'s)?(?:\s+date)?\]", re.I), "today"),
    (re.compile(r"\{(?:your[_ ]?name|name|signer)\}", re.I), "signer"),
    (re.compile(r"\{(?:current[_ ]?date|date|today)\}", re.I), "today"),
]


@dataclass
class DraftedEmail:
    subject: str
    body: str
    confirmation_token: str
    # Populated when the draft came from a real EmailAgent LLM call; empty
    # when we fell back to the deterministic template (quota exhausted or
    # LLM error). The chat response threads this to the frontend so a
    # keep/adjust/not-me click on the draft can post `audit_id` to
    # /v1/feedback, which lets /admin/traces render the causal edge from
    # the original draft call to the FeedbackChip audit row.
    audit_id: str = ""


@dataclass(frozen=True)
class DraftContext:
    signer_name: str
    today: str


async def draft_context(store: UserStore) -> DraftContext:
    profile = dict(await store.profile.read() or {})
    people = await store.people.list()
    self_person = next((p for p in people if p.is_self), None)
    signer = ""
    if self_person:
        signer = (self_person.display_name or "").strip()
        if signer.lower() in _GENERIC_SELF:
            signer = ""
    if not signer:
        signer = _name_from_email(str(profile.get("email") or ""))
    if not signer:
        signer = "A parent"

    tz_name = str(profile.get("tz") or get_settings().calendar_tz or "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    today = f"{now.strftime('%A, %B')} {now.day}, {now.year}"
    return DraftContext(signer_name=signer, today=today)


def fill_placeholders(text: str, ctx: DraftContext) -> str:
    """Replace leftover [Your name] / [Current Date] tokens with real values."""
    values = {"signer": ctx.signer_name, "today": ctx.today}
    out = text or ""
    for pat, key in _PLACEHOLDERS:
        out = pat.sub(values[key], out)
    return out


def _name_from_email(email: str) -> str:
    local = email.split("@")[0].strip()
    if not local or local.startswith("u_"):
        return ""
    parts = [p for p in re.split(r"[._+\-]+", local) if p and not p.isdigit()]
    if not parts:
        return ""
    return " ".join(p[:1].upper() + p[1:] for p in parts)


async def draft_email(
    store: UserStore,
    *,
    intent: str,
    contact_display_name: str,
    kid_display_name: str | None = None,
    extra_notes: str = "",
) -> DraftedEmail:
    ctx = await draft_context(store)
    try:
        result = await email_run(
            store=store,
            intent=intent,
            contact_display_name=contact_display_name,
            kid_display_name=kid_display_name,
            extra_notes=extra_notes,
            signer_name=ctx.signer_name,
            today=ctx.today,
        )
        if result.value:
            draft = result.value.draft  # type: ignore[union-attr]
            return DraftedEmail(
                subject=fill_placeholders(draft.subject, ctx),
                body=fill_placeholders(draft.body, ctx),
                confirmation_token=secrets.token_urlsafe(24),
                audit_id=result.audit_id,
            )
    except QuotaExhausted:
        logger.info("email.draft.quota_fallback", user=store.user_id)
    except Exception as err:
        logger.warning("email.draft.failed", user=store.user_id, err=str(err))
    return template_draft(
        contact_display_name=contact_display_name,
        kid_display_name=kid_display_name,
        extra_notes=extra_notes,
        ctx=ctx,
    )


def template_draft(
    *,
    contact_display_name: str,
    kid_display_name: str | None = None,
    extra_notes: str = "",
    ctx: DraftContext | None = None,
    signer_name: str | None = None,
    today: str | None = None,
) -> DraftedEmail:
    """Courteous fallback when Gemini is unavailable — already filled in."""
    if ctx is None:
        ctx = DraftContext(
            signer_name=signer_name or "A parent",
            today=today or datetime.now().strftime("%A, %B ") + f"{datetime.now().day}, {datetime.now().year}",
        )
    kid = kid_display_name or "my child"
    note = (extra_notes or "").strip()
    if re.search(r"\b(sick|ill|unwell|fever|flu)\b", note, re.I):
        subject = f"{kid} out sick today"
        reason = (
            f"{kid} is not feeling well and will be out of school today, {ctx.today}."
        )
    else:
        subject = f"Note about {kid}"
        reason = note or f"A quick note about {kid} for {ctx.today}."
    first = contact_display_name.split()[0] if contact_display_name else "there"
    body = (
        f"Hi {first},\n\n"
        f"{reason}\n\n"
        "Thank you,\n"
        f"{ctx.signer_name}"
    )
    return DraftedEmail(
        subject=fill_placeholders(subject, ctx),
        body=fill_placeholders(body, ctx),
        confirmation_token=secrets.token_urlsafe(24),
    )


def sanitize_email_text(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in "\n\r\t")
    return cleaned[:5000]
