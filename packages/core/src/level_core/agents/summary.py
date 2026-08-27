"""SummaryAgent: 2-3 sentence 'hear my day' summary for TTS."""

from __future__ import annotations

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.agents.memory_bank import recall_split, touch as touch_memories
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

    # Memory Bank + anti-examples via the shared split helper. See
    # memory_bank.recall_split() for why the split lives there.
    positive_memories, avoid_memories = await recall_split(
        store, positive_limit=6, avoid_limit=3
    )
    context: dict[str, object] = {}
    if positive_memories:
        context["memory_bank"] = [
            {"text": m["text"], "tags": m.get("tags") or []}
            for m in positive_memories
        ]
    if avoid_memories:
        context["avoid_examples"] = [
            {"text": m["text"], "tags": m.get("tags") or []}
            for m in avoid_memories
        ]

    spec = AgentSpec(
        name="SummaryAgent",
        model="flash",
        system=SYSTEM,
        response_schema=SummaryResponse,
        # max_turns=1: source_span is off (nothing to echo back), and a
        # schema failure on a {summary: str} response is exceedingly
        # rare on flash. When it does happen, voice.summary now has a
        # deterministic ``_fallback_summary`` that reads better than a
        # second LLM turn anyway - so eating another 3-8s roundtrip
        # for a marginal-quality refinement is a bad trade for a
        # user-facing "Hear my day" click.
        max_turns=1,
        temperature=0.4,
        require_source_span=False,
    )
    result = await call_agent(
        spec, user_input=user_input, store=store, context=context or None
    )
    all_ids = [m["id"] for m in (positive_memories + avoid_memories) if m.get("id")]
    if result.value and all_ids:
        await touch_memories(store, memory_ids=all_ids)
    return result
