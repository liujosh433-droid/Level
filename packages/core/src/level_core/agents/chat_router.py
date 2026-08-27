"""ChatRouterAgent: classify each user message into path + intent.

Fast Gemini call, temperature=0, structured output. Router decides which
downstream agent (or none) handles the message.

Collaborative Partner rubric: when confidence is low or a required
detail is missing, the router MUST ask a clarifying question rather
than guess. `needs_clarification` + `clarifying_question` in the output
schema flip the chat handler into "ask the human" mode.
"""

from __future__ import annotations

from typing import Any

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.agents.memory_bank import recall as recall_memories
from level_core.agents.router_cache import get_cached, store_cached
from level_core.observability import get_logger
from level_core.schemas import ChatRouterDecision, NegativeAgent
from level_core.storage.base import UserStore
from level_core.storage.care_store import recent_negatives

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

If `<context>` has `memories`, they are long-lived facts the caregiver
told Level in earlier sessions ("Nova starts kindergarten Aug 25",
"Beta's doctor is Dr. Kim"). Use them to disambiguate references:
- "book Nova's checkup" + memory "Nova's doctor is Dr. Kim" -> route
  as schedule/book_now with high confidence, no clarify.
- "email the coach about Wednesday" + memory "Beta plays soccer
  Wednesdays" -> email path, and treat the coach as Beta's.
Never invent a memory. Only USE the ones given.

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

Inline-extraction protocol (avoid a second LLM roundtrip):
The dispatcher will use these fields directly when set, skipping the
specialist agent. Only fill them when you are CONFIDENT the message
carries ONE clean value in the shape below. Otherwise leave null and
the specialist agent will do the extraction.

`inline_priority` (only when path=profile, intent=priority):
- Fill for a single, unambiguous priority statement.
- Shape: {text, weight (1-5), activity_types (from the ActivityType
  enum), source_span}.
- Weight: 5 = non-negotiable ("never miss", "no matter what",
  "non-negotiable", "above all"), 4 = strong ("prioritize", "takes
  precedent", "matters most", "comes first"), 3 = default preference.
- `activity_types` MUST be from the enum: sports.soccer,
  sports.basketball, sports.swim, sports.other, school.pickup,
  school.dropoff, school.event, medical.appointment,
  medical.therapy, work, family, commute, personal, other.
- `text` is a short label the user would recognize ("Elder care with
  mom", "Sunday physical therapy"). Not a full sentence.
- `source_span` is an exact substring of the user_input.
- Do NOT re-emit priorities listed under <negatives.priority>.
- Leave null for: multi-priority statements ("X and Y both matter"),
  vague ones ("family is important"), or negations ("stop
  prioritizing X").

`inline_person_edit` (only when path=profile, intent=person_update):
- Fill for a single, unambiguous edit to the caregiver's people list.
- Shape: {action, target_name, new_relation (nullable),
  new_display_name (nullable), source_span}.
- `action`: add | change_relation | rename | mark_self | remove.
- `target_name` should match a name/alias in <people> (case-
  insensitive) for change_relation/rename/mark_self/remove, or be the
  new person's name for `add`.
- For `add`, `new_relation` is REQUIRED and MUST be one of the
  CareRelation values: co-parent, partner, child, elder, self, other.
- Examples:
    "Alex is my co-parent"           -> add, Alex, co-parent
    "Robert is my kid, not my dad"   -> change_relation, Robert, child
    "call her Nova, not Nova Ann"    -> rename, Nova Ann, new_display_name=Nova
    "Sam is me"                       -> mark_self, Sam
    "drop Priya, that's my colleague"-> remove, Priya
- Leave null for: ambiguous references, multiple edits in one
  message, or if <people> has multiple plausible matches.

`inline_reminder` (only when path=reminder, intent=add_reminder):
- Fill for a single, unambiguous "remind me to X (for Y)" statement
  or a clear follow-up in a reminder thread.
- Shape: {text, person_display_name (nullable), activity_type,
  lead_minutes (default 60), source_span}.
- `activity_type` MUST be from the enum listed above under priority.
- `person_display_name` should match a display_name/alias from
  <people> when the reminder is scoped to a person.
- Examples:
    "I forgot Theo's soccer shoes"      -> text="Bring soccer shoes",
      person_display_name="Theo", activity_type=sports.soccer,
      source_span="soccer shoes"
    "remind me to bring a charger to my meetings" ->
      text="Bring a charger", activity_type=work,
      source_span="bring a charger"
- Do NOT re-emit reminders listed under <negatives.reminder>.
- Leave null for: multi-reminder messages, or when the caregiver
  hasn't said what event/context the reminder attaches to.

Any inline_* field MUST be null on paths/intents other than its own.

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
    context: dict[str, Any] = {}
    if history:
        context["prior_turns"] = history

    # Long-term memory: pass the 3 most recently-used facts so the
    # router can disambiguate short messages that reference a person
    # or event by memory ("book Nova's checkup"). Truncate to keep
    # context small; the router is Flash and we're on the hot path.
    try:
        memories = await recall_memories(store, limit=3)
    except Exception:  # noqa: BLE001 - never let memory bank break the router
        memories = []
    if memories:
        context["memories"] = [
            {
                "id": m.get("id"),
                "text": (m.get("text") or "")[:140],
                "tags": (m.get("tags") or [])[:4],
            }
            for m in memories
            if m.get("text")
        ]

    # Inline-extraction context. When the router is confident it can
    # fill inline_priority / inline_person_edit / inline_reminder we
    # skip the specialist agent. That means we have to give the router
    # the same anti-repeat + person-match context those specialists
    # would have gotten. All three reads are async and independent, so
    # we fan out; if any single read fails we degrade to "no context"
    # for that channel rather than block routing.
    try:
        people = await store.people.list()
    except Exception:  # noqa: BLE001 - never let a bad people list break the router
        people = []
    if people:
        context["people"] = [
            {
                "display_name": p.display_name,
                "relation": p.relation.value,
                "aliases": (p.aliases or [])[:4],
                "is_self": p.is_self,
            }
            for p in people[:40]  # cap to keep the prompt bounded
        ]

    negatives_by_channel: dict[str, list[dict[str, str]]] = {}
    for channel_key, agent_enum in (
        ("priority", NegativeAgent.PRIORITY),
        ("reminder", NegativeAgent.REMINDER),
    ):
        try:
            negatives = await recent_negatives(store, agent=agent_enum, limit=10)
        except Exception:  # noqa: BLE001 - graceful degradation
            negatives = []
        if negatives:
            negatives_by_channel[channel_key] = [
                {"field": n.field, "value": (n.value or "")[:140]}
                for n in negatives
            ]
    if negatives_by_channel:
        context["negatives"] = negatives_by_channel

    result = await call_agent(
        spec,
        user_input=user_message,
        context=context or None,
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
