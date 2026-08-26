"""RoleAgent: propose care_people from a calendar rollup.

Input: compressed rollup {weekday, hour_band, summary_first_5_words, count}.
Never full event bodies. Uses `pro` because misclassification is costly.

Two-layer defense (see docs/STATE_AND_LIFECYCLE.md section 3.10):

  Layer 1 - Prompt + attendee evidence: SYSTEM tells the LLM to prefer
    human first-names, and the <context> block now includes a
    `google_confirmed_attendees` set built from real Google attendees.
    That is the strongest positive signal Google gives us for "this
    string is a human".

  Layer 2 - Deterministic post-LLM guard: `evaluate_proposed_name`
    drops rows whose name matches a responsibility word (Grocery,
    Commute, Standup, ...) and fast-accepts rows whose name matches a
    Google attendee token OR a family-relation word (Papa, Mom, ...).
    Dropped rows auto-record a `negative` so the next call has few-shot
    evidence not to re-propose. Uncertain names still ship - Not-me is
    the last correction path.

Both layers are O(P) where P = proposed people count (typically <= 10).
Set lookups only; no LLM, no Firestore hit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.calendar.person_guard import (
    attendee_token_union,
    evaluate_proposed_name,
)
from level_core.observability import get_logger
from level_core.schemas import CareRelation, NegativeAgent
from level_core.schemas.care import role_for_relation
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives, record_negative

logger = get_logger(__name__)


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

Names listed under <google_confirmed_attendees> are HIGH-CONFIDENCE humans
- Google has them as real attendees on at least one event. Prefer them when
choosing.

Names appearing only in event summaries may still be humans (Papa, Mom, or
a child's first name in "Nova soccer"). But watch out for responsibility
tokens (Grocery, Commute, Standup, Lunch) - those are activities, never
people. When unsure, do NOT propose.

Do NOT re-propose anyone already listed under <known_people> - they are locked
in by the user. Do NOT re-propose anyone listed under <negatives> - the user
already rejected those classifications."""


async def run(
    *,
    store: UserStore,
    calendar_rollup: list[dict[str, Any]],
    self_hint: str | None = None,
) -> AgentResult:
    negatives = await recent_negatives(store, agent=NegativeAgent.ROLE, limit=20)
    people = await store.people.list()
    known = [
        {"display_name": p.display_name, "relation": p.relation.value, "is_self": p.is_self}
        for p in people
        if p.status in ("kept", "proposed") and not p.is_self
    ]

    events = await store.agenda.list()
    attendees = attendee_token_union(events)

    context = {
        "rollup": calendar_rollup,
        "self_hint": self_hint,
        "known_people": known,
        "google_confirmed_attendees": sorted(attendees),
        "negatives": [{"field": n.field, "value": n.value} for n in negatives],
        "generated_at": datetime.utcnow().isoformat(),
    }
    user_input_facsimile = " ".join(
        item.get("summary_first_5_words", "") for item in calendar_rollup
    )
    spec = AgentSpec(
        name="RoleAgent",
        model="pro",
        system=SYSTEM,
        response_schema=RoleAgentResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    result = await call_agent(
        spec,
        user_input=user_input_facsimile,
        context=context,
        store=store,
    )

    if result.value is None:
        return result

    original: RoleAgentResponse = result.value  # type: ignore[assignment]
    kept: list[ProposedPerson] = []
    dropped: list[tuple[ProposedPerson, str]] = []
    uncertain: list[str] = []

    for person in original.people:
        verdict = evaluate_proposed_name(person.display_name, attendees=attendees)
        if not verdict.kept:
            dropped.append((person, verdict.reason))
            continue
        if verdict.reason == "uncertain":
            uncertain.append(person.display_name)
        kept.append(person)

    if dropped:
        logger.warning(
            "role.dropped_non_human",
            audit_id=result.audit_id,
            dropped=[
                {"name": p.display_name, "reason": reason} for p, reason in dropped
            ],
        )
        # Auto-record negatives so the next RoleAgent call sees these as
        # few-shot rejects. Cheap and self-correcting.
        for person, reason in dropped:
            try:
                await record_negative(
                    store,
                    agent=NegativeAgent.ROLE,
                    field="display_name",
                    value=person.display_name,
                    reason=f"auto:{reason}",
                )
            except Exception:  # noqa: BLE001 - never break a call over audit
                logger.warning(
                    "role.negative_write_failed", value=person.display_name
                )
        result.value = original.model_copy(update={"people": kept})

    if uncertain:
        logger.info(
            "role.uncertain_names",
            audit_id=result.audit_id,
            names=uncertain,
        )

    return result


def role_bucket(relation: CareRelation) -> str:
    return role_for_relation(relation).value
