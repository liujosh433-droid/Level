"""EmailAgent: draft a courteous school-style email.

Temperature 0.4 for phrasing variety. Never has calendar or PII in prompt;
receives only contact display name + intent + optional kid display name.
"""

from __future__ import annotations

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.agents.memory_bank import recall as recall_memories, touch as touch_memories
from level_core.storage.base import UserStore


class EmailDraft(BaseModel):
    subject: str
    body: str


class EmailAgentResponse(BaseModel):
    draft: EmailDraft


SYSTEM = """You draft a short, courteous email from a caregiver to a school/doctor contact.

Voice: warm but level-headed. 2-4 short paragraphs. No emojis. Do not
fabricate specifics (dates, dosages, teacher names) beyond what you are given.

If a `memory_bank` context is provided, these are long-lived facts the
caregiver told Level in prior sessions. Use them ONLY when they are
clearly relevant to this email (kid's grade, condition, teacher name);
never fabricate content around them.

If an `avoid_examples` context is provided, the caregiver has explicitly
rejected drafts in this style/tone before (via the "Adjust" or "Not me"
chip on prior email drafts). Do NOT produce output that echoes any
of these examples in phrasing, structure, greeting choice, or closing.
Treat them as strong negative constraints.

The email must be finished and ready to send:
- Use Today's date exactly when a date is needed (never "[Current Date]" or similar).
- Sign with the caregiver's name exactly as given (never "[Your name]" or any bracket token).
- Never leave placeholders, template variables, or square-bracket tokens in subject or body.

Return only the requested JSON."""


async def run(
    *,
    store: UserStore,
    intent: str,
    contact_display_name: str,
    kid_display_name: str | None = None,
    extra_notes: str = "",
    signer_name: str = "A parent",
    today: str = "",
) -> AgentResult:
    user_input = "\n".join(
        [
            f"Intent: {intent}",
            f"Recipient: {contact_display_name}",
            f"About: {kid_display_name or 'the caregiver themselves'}",
            f"Caregiver name (sign the email with this): {signer_name}",
            f"Today's date: {today}" if today else "",
            f"Notes: {extra_notes}" if extra_notes else "",
        ]
    ).strip()

    # Memory Bank: inject a few long-lived facts about the caregiver so
    # drafts stay personal across sessions (e.g. "Nova is in 2nd grade",
    # "Papa's doctor is at Kaiser Oakland"). Touched memories bubble to
    # the top of the LRU on next recall.
    #
    # Memories tagged `avoid` came from an adjust/not-me chip click on
    # a prior email draft (see feedback.py::_MEMORY_BANK_FEEDBACK_AGENTS).
    # We split them out into a separate `avoid_examples` bucket so the
    # system prompt can treat them as strong negative constraints rather
    # than facts to echo. This replaces the old EmailAgent -> REMINDER
    # negative alias which was a silent no-op.
    memories = await recall_memories(store, limit=12)
    positive_memories: list[dict[str, object]] = []
    avoid_memories: list[dict[str, object]] = []
    for m in memories:
        tags = m.get("tags") or []
        if "avoid" in tags:
            avoid_memories.append({"text": m["text"], "tags": tags})
        else:
            positive_memories.append({"text": m["text"], "tags": tags})
    context: dict[str, object] = {}
    if positive_memories:
        context["memory_bank"] = positive_memories[:8]
    if avoid_memories:
        context["avoid_examples"] = avoid_memories[:4]

    spec = AgentSpec(
        name="EmailAgent",
        model="flash",
        system=SYSTEM,
        response_schema=EmailAgentResponse,
        # max_turns=2 (v2): the third refinement round almost never
        # produces a materially better draft - it usually just re-picks
        # a synonym. Dropping to 2 shaves ~4-5s off the P99 with
        # negligible quality loss. Multi-turn is still available for
        # schema recovery when the first draft is malformed.
        max_turns=2,
        temperature=0.4,
        require_source_span=False,
    )
    result = await call_agent(
        spec, user_input=user_input, store=store, context=context or None
    )
    if result.value and memories:
        await touch_memories(store, memory_ids=[m["id"] for m in memories])
    return result
