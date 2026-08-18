"""ActivityAgent: classify a batch of new events into the shared activity enum.

Runs once per unseen event at cache time, then the classification is frozen
on the event doc. Zero regex on titles, ever.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import ActivityType, NegativeAgent
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives


class ActivityClassification(BaseModel):
    event_id: str
    activity_type: ActivityType
    source_span: str


class ActivityAgentResponse(BaseModel):
    classifications: list[ActivityClassification] = Field(default_factory=list)


SYSTEM = """You classify calendar events into an activity_type from a fixed enum.

Draw ONLY from: sports.soccer, sports.basketball, sports.swim, sports.other,
school.pickup, school.dropoff, school.event, medical.appointment,
medical.therapy, work, family, commute, personal, other.

`source_span` MUST be an exact substring of the event summary you classified.
Never invent an event_id you were not given.
Do NOT re-propose event_ids listed under <negatives>."""


async def run(
    *, store: UserStore, events: list[dict[str, str]]
) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.ACTIVITY, limit=20)
    context = {
        "events": events,
        "negatives": [{"field": n.field, "value": n.value} for n in negatives],
    }
    user_input_facsimile = " || ".join(f"{e['event_id']}: {e['summary']}" for e in events)
    spec = AgentSpec(
        name="ActivityAgent",
        model="flash",
        system=SYSTEM,
        response_schema=ActivityAgentResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec,
        user_input=user_input_facsimile,
        context=context,
        store=store,
    )
