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
- schedule: booking, "find a time", moving/canceling events, "put X back on calendar".
- email: drafting or sending mail to a contact (teacher, doctor, coach).
- profile: sharing a priority, updating a person, correcting Level.
- reminder: adding a "don't forget X when Y happens" reminder.
- general: chit-chat, questions, everything else.

Intents:
- find_time: user wants Level to SUGGEST open slots (they haven't picked a time).
    Examples: "find 45 min for a walk", "when can I squeeze in a call?".
- book_now: user gave a concrete time and wants it ON the calendar right now.
    Examples: "put Tuesday drop-off 7:45-8:22am back on calendar",
    "add dentist Thursday 3pm", "book Nova pickup Wednesday 3:15-4pm".
- send_email | priority | person_update | usual_update | contact_add | add_reminder | ask

Rule of thumb: if the user names both a day/date AND a time, it's `book_now`.
If they only name a duration or ask you to find a slot, it's `find_time`.

If `<context>` has `prior_turns`, use them to resolve short follow-ups:
- If a prior turn asked for a booking and the current message just names a
  time (e.g. "Tuesday 7:45am to 8:22am"), classify as schedule / book_now.
- If a prior turn set a topic (priority, reminder, person edit) and the
  current message adds a detail, keep the same path/intent.

Return JSON matching the schema. `source_span` MUST be an exact substring of the user_input."""


async def run(
    *,
    store: UserStore,
    user_message: str,
    history: list[dict[str, str]] | None = None,
    trace_id: str | None = None,
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
    context = {"prior_turns": history} if history else None
    return await call_agent(
        spec,
        user_input=user_message,
        context=context,
        store=store,
        trace_id=trace_id,
    )
