"""UsualAgent: disambiguate between candidate usuals when arithmetic ties.

Only invoked when `usuals.compute_from_events()` finds >1 confident candidate
at the same (weekday, hour_band). Cheap Flash call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import ActivityType, HourBand, NegativeAgent, Weekday
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives


class UsualPick(BaseModel):
    person_id: str
    weekday: Weekday
    hour_band: HourBand
    activity_type: ActivityType
    display_summary: str
    source_span: str


class UsualAgentResponse(BaseModel):
    picks: list[UsualPick] = Field(default_factory=list)


SYSTEM = """You disambiguate weekly-recurring caregiver events into a canonical 'usual'.

Prefer the person whose name appears most often in the summaries. Choose the
narrowest `activity_type` from the enum. Do NOT re-propose picks under <negatives>."""


async def run(
    *, store: UserStore, candidates: list[dict[str, Any]]
) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.USUAL, limit=20)
    context = {
        "candidates": candidates,
        "negatives": [{"field": n.field, "value": n.value} for n in negatives],
    }
    user_input_facsimile = " | ".join(
        c.get("summary", "") for c in candidates
    )
    spec = AgentSpec(
        name="UsualAgent",
        model="flash",
        system=SYSTEM,
        response_schema=UsualAgentResponse,
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
