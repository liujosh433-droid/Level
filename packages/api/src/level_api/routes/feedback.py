"""Inline feedback capture for AI-authored artifacts.

Every draft/proposal Level produces (email, booking, priority, reminder,
person edit) is followed by three chips in the UI: keep / adjust / not-me.

- keep    -> writes the reply text into `memory_bank` so generator agents
             (email, summary, router) can echo the caregiver's tone back
             on future calls.
- adjust  -> writes a NegativeFeedback row that the corresponding agent's
             next call receives inline as few-shot "close, refine".
- not_me  -> same, stronger signal: "do not propose anything like this".

Every click ALSO writes a `FeedbackChip` AiAuditEntry with
`parent_audit_id` set to the artifact's own audit_id (threaded through
the chat response). This is what lets /admin/traces render the causal
edge from the original agent call to the click and, downstream, to the
next agent call that consumed the resulting negative or memory.

Two aliases in `_AGENT_ALIASES` are honest fudges: EmailAgent and
SummaryAgent don't have their own NegativeAgent enum member, so we
route their adjust/not-me clicks into `memory_bank` with an `avoid`
tag instead of a mismatched negatives bucket. EmailAgent/SummaryAgent
call sites read that avoid tag to build an anti-example section in
their prompt on the next call.

This is the "captures feedback so it constantly adapts" bullet from the
Collaborative Partner rubric - implemented as a visible, causal loop
the demo video can trace end-to-end in /admin/traces.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from level_core.agents.memory_bank import remember as remember_memory
from level_core.observability import get_logger
from level_core.schemas import AiAuditEntry, NegativeAgent
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
    `verdict`   = keep | adjust | not_me. Only adjust/not_me writes a
                  negatives row (or an avoid-tagged memory for aliased
                  agents); keep writes a positive memory.
    `reason`    = optional user note ("this is my sister, not my mom").
    `audit_id`  = optional pointer to the ai_audit row that produced the
                  artifact. When present, the FeedbackChip audit row's
                  parent_audit_id is set to this so /admin/traces can
                  render the causal edge from the click back to the
                  original call.
    """

    agent: str = Field(min_length=1, max_length=40)
    field: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=800)
    verdict: Verdict
    reason: str | None = Field(default=None, max_length=400)
    audit_id: str | None = Field(default=None, max_length=40)


# Agents that write to a NegativeAgent bucket the corresponding
# EXTRACTOR reads on its next call. Non-extractor agents (Email,
# Summary) route through the memory_bank with an `avoid` tag instead
# (see _MEMORY_BANK_FEEDBACK_AGENTS below).
_AGENT_ALIASES: dict[str, NegativeAgent] = {
    "RoleAgent": NegativeAgent.ROLE,
    "UsualAgent": NegativeAgent.USUAL,
    "PriorityAgent": NegativeAgent.PRIORITY,
    "ReminderAgent": NegativeAgent.REMINDER,
    "ActivityAgent": NegativeAgent.ACTIVITY,
    # Extractor-aliased edits still write to a real bucket because the
    # signal IS applicable: correcting a relation trains RoleAgent's
    # name-vs-noun guard; rejecting a booking title trains the activity
    # classifier.
    "BookAgent": NegativeAgent.ACTIVITY,
    "PersonEditAgent": NegativeAgent.ROLE,
    "ChatRouterAgent": NegativeAgent.ACTIVITY,
}


# Generator agents whose adjust/not-me feedback used to be routed into
# a mismatched extractor bucket (EmailAgent -> REMINDER was nonsense:
# ReminderAgent doesn't generate prose). Instead, on adjust/not-me,
# we write an `avoid` tagged memory that email.py/summary.py filter on
# and render as an "avoid this tone" anti-example on their next call.
_MEMORY_BANK_FEEDBACK_AGENTS: frozenset[str] = frozenset(
    {"EmailAgent", "SummaryAgent"}
)

# Fields where a "keep" click writes a positive memory. Deliberately
# narrow so we don't clutter memory_bank with structural artifact
# like priority weight or activity_type strings.
_KEEP_MEMORY_FIELDS: frozenset[str] = frozenset(
    {"email.body", "priority.text", "reminder.text"}
)

MEMORY_TEXT_CAP = 220


def _hash_prompt(text: str) -> str:
    """Same 12-char sha1 prefix base.py uses for prompt fingerprinting."""
    return hashlib.sha1((text or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


async def _write_feedback_audit(
    store: UserStore,
    *,
    body: FeedbackBody,
    negative_id: str | None,
    memory_id: str | None,
    routed_to: str,
) -> str:
    """Write a synthetic AiAuditEntry for the click itself.

    parent_audit_id links back to the artifact this click judged, so
    /admin/traces can render `EmailAgent aud_abc -> FeedbackChip aud_def
    -> next call that consumed the resulting memory/negative`. Best-
    effort: never let audit-write failure break the feedback POST.
    """
    audit_id = f"aud_{uuid.uuid4().hex[:12]}"
    entry = AiAuditEntry(
        audit_id=audit_id,
        agent="FeedbackChip",
        model="human",  # not an LLM call - the user is the "model"
        prompt_hash=_hash_prompt(body.value),
        response={
            "verdict": body.verdict,
            "target_agent": body.agent,
            "field": body.field,
            "reason": body.reason,
            "negative_id": negative_id,
            "memory_id": memory_id,
            "routed_to": routed_to,
        },
        input_tokens=0,
        output_tokens=0,
        cost_estimate_usd=0.0,
        latency_ms=0,
        hallucinated=False,
        loop_broken=False,
        blocked_by_safety=False,
        fallback_used=None,
        turns_taken=0,
        parent_audit_id=body.audit_id,
        trace_id=body.audit_id or audit_id,
    )
    try:
        await store.ai_audit.upsert(entry)
    except Exception as err:  # noqa: BLE001 - audit is observability, never critical path
        logger.warning(
            "feedback.audit_failed",
            user=store.user_id,
            agent=body.agent,
            verdict=body.verdict,
            err=str(err),
        )
    return audit_id


@router.post("")
async def submit_feedback(
    body: FeedbackBody, store: UserStore = Depends(get_user_store)
) -> dict[str, str]:
    if body.verdict == "keep":
        remembered = None
        if body.field in _KEEP_MEMORY_FIELDS:
            memory = await remember_memory(
                store,
                text=body.value[:MEMORY_TEXT_CAP],
                tags=[body.agent.lower(), body.field.replace(".", "_")],
                source="feedback",
            )
            remembered = memory["id"] if memory else None
        chip_audit_id = await _write_feedback_audit(
            store,
            body=body,
            negative_id=None,
            memory_id=remembered,
            routed_to="memory_bank" if remembered else "none",
        )
        logger.info(
            "feedback.keep",
            user=store.user_id,
            agent=body.agent,
            field=body.field,
            audit_id=body.audit_id,
            chip_audit_id=chip_audit_id,
            memory_id=remembered,
        )
        return {
            "status": "recorded",
            "learned": "yes" if remembered else "no",
            "chip_audit_id": chip_audit_id,
        }

    # Non-extractor generator agents (Email, Summary) route their
    # adjust/not-me through memory_bank with an `avoid` tag. The
    # generator agents' next call filters recall by tag and renders
    # avoid memories as an anti-example block in the prompt. This
    # replaces the old EmailAgent -> NegativeAgent.REMINDER alias
    # which was a silent no-op (ReminderAgent doesn't produce prose,
    # so an "avoid this tone" negative there did nothing).
    if body.agent in _MEMORY_BANK_FEEDBACK_AGENTS:
        avoid_memory = await remember_memory(
            store,
            text=body.value[:MEMORY_TEXT_CAP],
            tags=[
                body.agent.lower(),
                body.field.replace(".", "_"),
                "avoid",
                body.verdict,
            ],
            source="feedback",
        )
        chip_audit_id = await _write_feedback_audit(
            store,
            body=body,
            negative_id=None,
            memory_id=avoid_memory["id"] if avoid_memory else None,
            routed_to="memory_bank_avoid",
        )
        logger.info(
            "feedback.avoid_memory",
            user=store.user_id,
            agent=body.agent,
            verdict=body.verdict,
            field=body.field,
            chip_audit_id=chip_audit_id,
            memory_id=avoid_memory["id"] if avoid_memory else None,
        )
        return {
            "status": "learned",
            "learned": "yes" if avoid_memory else "no",
            "chip_audit_id": chip_audit_id,
            "memory_id": avoid_memory["id"] if avoid_memory else "",
        }

    # Extractor agents: write a real NegativeFeedback row the
    # corresponding agent reads on its next call.
    target_agent = _AGENT_ALIASES.get(body.agent, NegativeAgent.ACTIVITY)
    neg = await record_negative(
        store,
        agent=target_agent,
        field=body.field,
        value=body.value,
        reason=body.reason
        or ("user removed" if body.verdict == "not_me" else "user adjusted"),
    )
    chip_audit_id = await _write_feedback_audit(
        store,
        body=body,
        negative_id=neg.negative_id,
        memory_id=None,
        routed_to=f"negatives.{target_agent.value}",
    )
    logger.info(
        "feedback.learned",
        user=store.user_id,
        agent=body.agent,
        verdict=body.verdict,
        field=body.field,
        negative_id=neg.negative_id,
        chip_audit_id=chip_audit_id,
    )
    return {
        "status": "learned",
        "learned": "yes",
        "negative_id": neg.negative_id,
        "chip_audit_id": chip_audit_id,
    }
