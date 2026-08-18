"""EmailAgent: draft a courteous school-style email.

Temperature 0.4 for phrasing variety. Never has calendar or PII in prompt;
receives only contact display name + intent + optional kid display name.
"""

from __future__ import annotations

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.storage.base import UserStore


class EmailDraft(BaseModel):
    subject: str
    body: str


class EmailAgentResponse(BaseModel):
    draft: EmailDraft


SYSTEM = """You draft a short, courteous email from a caregiver to a school/doctor contact.

Voice: warm but level-headed. 2-4 short paragraphs. No emojis. Do not
fabricate specifics (dates, dosages, teacher names) beyond what the caregiver
supplied. Sign the email with "[Your name]" placeholder for the caregiver
to replace. Return only the requested JSON."""


async def run(
    *,
    store: UserStore,
    intent: str,
    contact_display_name: str,
    kid_display_name: str | None = None,
    extra_notes: str = "",
) -> AgentResult:
    user_input = "\n".join(
        [
            f"Intent: {intent}",
            f"Recipient: {contact_display_name}",
            f"About: {kid_display_name or 'the caregiver themselves'}",
            f"Notes: {extra_notes}" if extra_notes else "",
        ]
    ).strip()

    spec = AgentSpec(
        name="EmailAgent",
        model="flash",
        system=SYSTEM,
        response_schema=EmailAgentResponse,
        max_turns=3,
        temperature=0.4,
        require_source_span=False,
    )
    return await call_agent(spec, user_input=user_input, store=store)
