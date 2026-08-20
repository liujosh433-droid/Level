"""ReminderAgent: extract a structured reminder from a chat message.

Output includes person_id (resolved to known care_people via alias) and
activity_type (shared enum). This is what powers O(1) structured matching.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import ActivityType, NegativeAgent
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives


class ExtractedReminder(BaseModel):
    text: str
    person_display_name: str | None = None
    activity_type: ActivityType
    lead_minutes: int = 60
    source_span: str


class ReminderAgentResponse(BaseModel):
    reminder: ExtractedReminder | None = None


SYSTEM = """You extract a caregiver reminder from one message.

Examples:
- "I forgot Theo's soccer shoes" -> person_display_name="Theo",
  activity_type="sports.soccer", text="Bring soccer shoes", source_span="soccer shoes".
- "remind me to bring a charger to my meetings" -> person_display_name=null,
  activity_type="work", text="Bring a charger", source_span="bring a charger".
- Follow-up "a charger" after Level asked what they might forget -> text="A charger".

`activity_type` MUST come from the enum: sports.soccer, sports.basketball,
sports.swim, sports.other, school.pickup, school.dropoff, school.event,
medical.appointment, medical.therapy, work, family, commute, personal, other.
Use "work" for meetings, standup, calls, and office days.

`source_span` MUST be an exact substring of user_input (same words; case may differ).
If `<context>` has `prior_turns`, use them to resolve short follow-ups.
If the message is clearly a reminder (remind me / don't forget / I keep forgetting),
ALWAYS fill `reminder` — do not return null just because there is no person name.
If message isn't a reminder, return {"reminder": null}.
Do NOT re-emit reminders listed under <negatives>."""


async def run(
    *,
    store: UserStore,
    message: str,
    history: list[dict[str, str]] | None = None,
) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.REMINDER, limit=20)
    context: dict[str, Any] = {
        "known_people": [
            {"display_name": p.display_name, "aliases": p.aliases}
            for p in await store.people.list()
        ],
        "negatives": [{"field": n.field, "value": n.value} for n in negatives],
    }
    if history:
        context["prior_turns"] = history
    spec = AgentSpec(
        name="ReminderAgent",
        model="flash",
        system=SYSTEM,
        response_schema=ReminderAgentResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec, user_input=message, context=context, store=store
    )
