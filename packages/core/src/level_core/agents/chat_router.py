"""ChatRouterAgent: classify each user message into path + intent.

Fast Gemini call, temperature=0, structured output. Router decides which
downstream agent (or none) handles the message.

Collaborative Partner rubric: when confidence is low or a required
detail is missing, the router MUST ask a clarifying question rather
than guess. `needs_clarification` + `clarifying_question` in the output
schema flip the chat handler into "ask the human" mode.
"""

from __future__ import annotations

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.agents.router_cache import get_cached, store_cached
from level_core.observability import get_logger
from level_core.schemas import ChatRouterDecision
from level_core.storage.base import UserStore

logger = get_logger(__name__)

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

person_update covers BOTH corrections and introductions:
  "Robert is my kid, not my dad", "Alex is my co-parent", "add Maya as my kid".

Clarifying-question protocol (Collaborative Partner):
- Set needs_clarification=true and provide a SHORT clarifying_question
  (<= 90 chars, ends with "?") when EITHER:
    (a) your confidence in the path+intent is below 0.6,
    (b) the intent requires a detail the user did not provide.
- Missing-detail triggers by intent:
    book_now      -> day or time missing
    find_time     -> nothing to schedule ("find a time" with no topic)
    send_email    -> recipient name unclear (no person + no contact kind)
    add_reminder  -> "remind me" but no event to attach it to
    person_update -> a name appears but the relation is unclear
- Never invent a detail. Ask.
- Examples of good clarifying_question values:
    "What day and time should I book that for?"
    "Which teacher should I email — Nova's or Beta's?"
    "For which event should I remember that?"
- When you DO have enough info, set needs_clarification=false and leave
  clarifying_question null.

Chit-chat protocol (path=general, intent=ask):
- When the message is a greeting, casual question ("how are you?",
  "who are you?", "what can you do?"), or off-topic remark, fill
  `general_reply` with a warm, contextual 1-2 sentence answer as
  Level. Voice: calm, helpful, uses "you" not "the user", no emojis.
- Examples:
    user "hi" -> "Hi. I'm here whenever you want to talk about the week or make a change."
    user "how are you?" -> "Doing well - keeping tabs on your calendar. Want me to look at anything specific?"
    user "what can you do?" -> "I can book time, draft school-style emails, remember people, and flag missing usuals. Try 'when's a good time for a walk this week?'"
- If the message is really a task in disguise ("book me lunch"),
  don't fill general_reply - route it normally. general_reply is
  for messages that AREN'T actions.
- Leave general_reply null on every non-general path.

Return JSON matching the schema. `source_span` MUST be an exact substring of the user_input."""


async def run(
    *,
    store: UserStore,
    user_message: str,
    history: list[dict[str, str]] | None = None,
    trace_id: str | None = None,
) -> AgentResult:
    # Router cache: repeated chit-chat, "what's on today", "help", etc.
    # all normalize to the same key. Same user + normalized message +
    # same recent history -> same routing decision. TTL keeps stale
    # routes from lingering after roster changes. This is a fully
    # separate lookup from the deterministic fast paths in chat.py —
    # this catches the LONG TAIL of variations those regexes miss.
    cached = get_cached(
        user_id=store.user_id, message=user_message, history=history
    )
    if cached is not None:
        logger.info(
            "agent.router_cache_hit",
            user=store.user_id,
            path=cached.path.value,
            intent=cached.intent.value,
            trace_id=trace_id,
        )
        return AgentResult(value=cached, cost_usd=0.0, latency_ms=0)

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
    result = await call_agent(
        spec,
        user_input=user_message,
        context=context,
        store=store,
        trace_id=trace_id,
    )
    if result.value is not None and not result.blocked_by_safety:
        store_cached(
            user_id=store.user_id,
            message=user_message,
            history=history,
            value=result.value,  # type: ignore[arg-type]
        )
    return result
