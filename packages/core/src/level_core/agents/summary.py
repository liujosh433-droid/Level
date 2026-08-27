"""SummaryAgent: 2-3 sentence 'hear my day' summary for TTS."""

from __future__ import annotations

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.agents.memory_bank import recall as recall_memories, touch as touch_memories
from level_core.storage.base import UserStore


class SummaryResponse(BaseModel):
    summary: str


SYSTEM = """You are Level, speaking a short summary of today's schedule to a busy caregiver.

Style: warm but calm. 2-3 sentences. Mention 1-2 anchor events. If there are
missing usuals or reminders, name at most one. Never invent events. Say
"you" not "the user".

If a `memory_bank` context is provided, use one such fact ONLY when it
naturally strengthens the summary (e.g. matches a person or activity
today). Never force a memory in.

If an `avoid_examples` context is provided, the caregiver has explicitly
rejected summaries in this style/tone (via the "Adjust" or "Not me" chip
on prior summaries). Do NOT produce output that echoes any of these
examples in phrasing or tone.

Return only JSON."""


async def run(
    *,
    store: UserStore,
    date_label: str,
    event_lines: list[str],
    missing_usual_lines: list[str],
    reminder_lines: list[str],
) -> AgentResult:
    user_input = "\n".join(
        [
            f"Date: {date_label}",
            "Events:",
            *[f"- {line}" for line in event_lines],
            "Missing usuals:",
            *[f"- {line}" for line in missing_usual_lines],
            "Reminders on today's events:",
            *[f"- {line}" for line in reminder_lines],
        ]
    )

    # Memory Bank: recall a few long-lived facts about the caregiver so
    # the daily summary sounds like Level remembers them (e.g. "you
    # usually skip lunch on Wednesdays" or "Nova gets picked up early
    # on library day").
    #
    # Memories tagged `avoid` came from adjust/not-me chip clicks on
    # prior summaries. Split them into a separate `avoid_examples`
    # bucket so the system prompt treats them as negative constraints.
    memories = await recall_memories(store, limit=10)
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
        context["memory_bank"] = positive_memories[:6]
    if avoid_memories:
        context["avoid_examples"] = avoid_memories[:3]

    spec = AgentSpec(
        name="SummaryAgent",
        model="flash",
        system=SYSTEM,
        response_schema=SummaryResponse,
        # max_turns=2 (v2): "Hear my day" is 2-3 sentences - the second
        # refinement rarely improves phrasing. Dropping to 2 keeps the
        # spoken flow under ~7s cold (was ~12s at the tail) so the
        # chime -> summary transition feels immediate.
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
