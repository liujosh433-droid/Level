"""RoleAgent: propose care_people from a calendar rollup.

Input: compressed rollup {weekday, hour_band, summary_first_5_words, count}.
Never full event bodies. Uses `pro` because misclassification is costly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import CareRelation, NegativeAgent
from level_core.schemas.care import role_for_relation
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives


class ProposedPerson(BaseModel):
    display_name: str
    relation: CareRelation
    aliases: list[str] = Field(default_factory=list)
    is_self: bool = False
    source_span: str


class RoleAgentResponse(BaseModel):
    people: list[ProposedPerson] = Field(default_factory=list)


SYSTEM = """You are Level's Role agent for a caregiver's Google Calendar.

Given a compressed rollup of the user's recurring events, propose the humans
this user cares for. Never invent people. Prefer first-name evidence from
event summaries; treat 'me' / 'my' as self.

Do NOT re-propose anyone listed under <negatives>."""


async def run(
    *,
    store: UserStore,
    calendar_rollup: list[dict[str, Any]],
    self_hint: str | None = None,
) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.ROLE, limit=20)
    context = {
        "rollup": calendar_rollup,
        "self_hint": self_hint,
        "negatives": [{"field": n.field, "value": n.value} for n in negatives],
        "generated_at": datetime.utcnow().isoformat(),
    }
    user_input_facsimile = " ".join(item.get("summary_first_5_words", "") for item in calendar_rollup)
    spec = AgentSpec(
        name="RoleAgent",
        model="pro",
        system=SYSTEM,
        response_schema=RoleAgentResponse,
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


def role_bucket(relation: CareRelation) -> str:
    return role_for_relation(relation).value
