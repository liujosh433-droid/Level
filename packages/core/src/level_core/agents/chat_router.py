"""ChatRouterAgent: classify each user message into path + intent.

Fast Gemini call, temperature=0, structured output. Router decides which
downstream agent (or none) handles the message.
"""

from __future__ import annotations

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import ChatRouterDecision
from level_core.storage.base import UserStore

SYSTEM = """You are Level's chat router. Classify the caregiver's message into a path and intent.

Paths:
- schedule: booking, "find a time", moving/canceling events.
- email: drafting or sending mail to a contact (teacher, doctor, coach).
- profile: sharing a priority, updating a person, correcting Level.
- reminder: adding a "don't forget X when Y happens" reminder.
- general: chit-chat, questions, everything else.

Intents:
- find_time | book_now | send_email | priority | person_update | usual_update
  | contact_add | add_reminder | ask

Return JSON matching the schema. `source_span` MUST be an exact substring of the user_input."""


async def run(
    *, store: UserStore, user_message: str, trace_id: str | None = None
) -> AgentResult:
    spec = AgentSpec(
        name="ChatRouterAgent",
        model="flash",
        system=SYSTEM,
        response_schema=ChatRouterDecision,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec,
        user_input=user_message,
        store=store,
        trace_id=trace_id,
    )
