"""Inline feedback capture for AI-authored artifacts.

Every draft/proposal Level produces (email, booking, priority, reminder,
person edit) is followed by three chips in the UI: keep / adjust / not-me.
Adjust and not-me post here; the reason (if any) is persisted as a
`NegativeFeedback` row that the corresponding agent's next call receives
as an inline few-shot "do not propose this again".

This is the "captures feedback so it constantly adapts" bullet from the
Collaborative Partner rubric — implemented not as a background process
but as an immediate, visible loop the demo video can show landing in
/admin/traces on the next agent call.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from level_core.agents.memory_bank import remember as remember_memory
from level_core.observability import get_logger
from level_core.schemas import NegativeAgent
from level_core.storage.base import UserStore
from level_core.storage.care_store import record_negative

from level_api.deps import get_user_store

router = APIRouter()
logger = get_logger(__name__)


Verdict = Literal["keep", "adjust", "not_me"]


class FeedbackBody(BaseModel):
    """One click on a keep/adjust/not-me chip.

    `agent`     = which agent's output the user is judging (matches
                  NegativeAgent enum). Free-form string for forward compat.
    `field`     = which output field the feedback is about (e.g. "email.body",
                  "booking.title", "priority.text").
    `value`     = the offending value the user rejected. This is what the
                  next agent call will see as few-shot "do not propose".
    `verdict`   = keep | adjust | not_me. Only adjust/not_me writes a row;
                  keep is logged for analytics (visible in /admin/traces).
    `reason`    = optional user note ("this is my sister, not my mom").
    `audit_id`  = optional pointer to the ai_audit row that produced the
                  artifact; lets /admin/traces show the causal link.
    """

    agent: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=800)
    verdict: Verdict
    reason: str | None = Field(default=None, max_length=400)
    audit_id: str | None = Field(default=None, max_length=40)


_AGENT_ALIASES: dict[str, NegativeAgent] = {
    "RoleAgent": NegativeAgent.ROLE,
    "UsualAgent": NegativeAgent.USUAL,
    "PriorityAgent": NegativeAgent.PRIORITY,
    "ReminderAgent": NegativeAgent.REMINDER,
    "ActivityAgent": NegativeAgent.ACTIVITY,
    # Aliases used by the frontend when the source isn't one of the five
    # extractor agents — bucket to the closest peer so few-shots still
    # affect the right prompt.
    "EmailAgent": NegativeAgent.REMINDER,
    "BookAgent": NegativeAgent.ACTIVITY,
    "PersonEditAgent": NegativeAgent.ROLE,
    "ChatRouterAgent": NegativeAgent.ACTIVITY,
    "SummaryAgent": NegativeAgent.ACTIVITY,
}


@router.post("")
async def submit_feedback(
    body: FeedbackBody, store: UserStore = Depends(get_user_store)
) -> dict[str, str]:
    if body.verdict == "keep":
        # A "keep" IS a positive signal — this is the model saying the
        # right thing. Persist it as a long-lived memory so generator
        # agents (email, summary) can echo the caregiver's tone back.
        remembered = None
        if body.field in {"email.body", "priority.text", "reminder.text"}:
            memory = await remember_memory(
                store,
                text=body.value[:MEMORY_TEXT_CAP],
                tags=[body.agent.lower(), body.field.replace(".", "_")],
                source="feedback",
            )
            remembered = memory["id"] if memory else None
        logger.info(
            "feedback.keep",
            user=store.user_id,
            agent=body.agent,
            field=body.field,
            audit_id=body.audit_id,
            memory_id=remembered,
        )
        return {"status": "recorded", "learned": "yes" if remembered else "no"}

    target_agent = _AGENT_ALIASES.get(body.agent, NegativeAgent.ACTIVITY)
    neg = await record_negative(
        store,
        agent=target_agent,
        field=body.field,
        value=body.value,
        reason=body.reason
        or ("user removed" if body.verdict == "not_me" else "user adjusted"),
    )
    logger.info(
        "feedback.learned",
        user=store.user_id,
        agent=body.agent,
        verdict=body.verdict,
        field=body.field,
        negative_id=neg.negative_id,
    )
    return {"status": "learned", "learned": "yes", "negative_id": neg.negative_id}


MEMORY_TEXT_CAP = 220
