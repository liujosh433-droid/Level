"""PriorityAgent: extract a structured priority from a chat message.

Only invoked when ChatRouterAgent classifies path=profile, intent=priority.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import ActivityType, NegativeAgent
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives


class ExtractedPriority(BaseModel):
    text: str
    weight: int = Field(default=3, ge=1, le=5)
    activity_types: list[ActivityType] = Field(default_factory=list)
    source_span: str


class PriorityAgentResponse(BaseModel):
    priority: ExtractedPriority | None = None


SYSTEM = """You extract a caregiver's stated priority from one message.

Weight: 1 = mild preference, 5 = non-negotiable.
Tag with any relevant activity_types from the shared enum so booking can use it.
`source_span` MUST be an exact substring of user_input.
If the message is not actually a priority, return {"priority": null}.
Do NOT re-emit priorities listed under <negatives>."""


async def run(*, store: UserStore, message: str) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.PRIORITY, limit=20)
    context = {"negatives": [{"field": n.field, "value": n.value} for n in negatives]}
    spec = AgentSpec(
        name="PriorityAgent",
        model="pro",
        system=SYSTEM,
        response_schema=PriorityAgentResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec, user_input=message, context=context, store=store
    )
