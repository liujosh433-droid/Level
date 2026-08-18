"""BookAgent: extract a concrete calendar event from a chat message.

Called when the ChatRouter picks path=schedule, intent=book_now (e.g.
"put Tuesday drop-off 7:45-8:22am back on calendar"). The extracted
`ProposedBooking` is then materialised via `schedule.book.book_event`.

The date is resolved deterministically by the caller once the agent
returns a weekday (0=Mon..6=Sun) or an ISO date. The agent must NOT try
to compute "next Tuesday" itself - it only pulls structured facts out
of the message.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.storage.base import UserStore


class ProposedBooking(BaseModel):
    title: str = Field(..., description="Short human-readable event title.")
    weekday: int | None = Field(
        default=None,
        ge=0,
        le=6,
        description="0=Mon..6=Sun if the user named a weekday; null otherwise.",
    )
    iso_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD if the user named an explicit date; null otherwise.",
    )
    start_hhmm: str = Field(
        ...,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="24h HH:MM, local calendar_tz.",
    )
    end_hhmm: str = Field(
        ...,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="24h HH:MM, local calendar_tz. Must be after start_hhmm.",
    )
    location: str | None = None
    source_span: str


class BookAgentResponse(BaseModel):
    booking: ProposedBooking | None = None


SYSTEM = """You extract a caregiver's calendar booking from ONE message.

Return valid JSON matching the schema.
- `title`: short label the user would recognise on their calendar. Derive it,
   in this order of preference:
   1. Explicit label in the current message (e.g. "Nova drop-off", "dentist").
   2. `prior_turns` context: if an earlier turn said "put Tuesday drop-off
      back", carry that title into a bare follow-up like "7:45am to 8:22am".
   3. `usuals` context: match the (weekday, start_hhmm) to a usual and use
      its `display_summary` (e.g. usual "Nova drop-off (Tue early_morning)"
      -> title="Nova drop-off").
   4. Last resort: title="Time block". Do NOT set booking to null just
      because a title is missing - if day + time were given, book it.
- `weekday`: 0=Mon..6=Sun if the user (or prior turns) said "Tuesday", "Sat",
   "this week" + a weekday. Otherwise null.
- `iso_date`: YYYY-MM-DD only if an explicit date was named ("Aug 22").
- `start_hhmm` / `end_hhmm`: 24-hour local times. Interpret am/pm carefully.
  * "7:45am" -> "07:45"; "8:22am" -> "08:22"; "3pm" -> "15:00"; "noon" -> "12:00".
  * If a start is given but no end, assume 30 minutes.
- `source_span` MUST be an exact substring of user_input.

Return {"booking": null} ONLY if the message is truly not a booking
(a question, chit-chat, priority statement). If the user gave a weekday
or date AND a time, it's a booking - fill in what you can and use the
title-derivation rules above."""


async def run(
    *,
    store: UserStore,
    message: str,
    today_iso: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> AgentResult:
    """Extract a booking spec.

    Context passed to the LLM:
    - `today`   : lets it resolve "this Tuesday" without hallucinating.
    - `prior_turns` : chat memory for bare follow-ups like "7:45am to 8:22am".
    - `usuals`  : lets it derive the title from a matching weekly usual when
                  the user says only "put back Tuesday 7:45-8:22am".
    """
    context: dict[str, Any] = {"today": today_iso or date.today().isoformat()}
    if history:
        context["prior_turns"] = history

    # Attach the user's known usuals so the agent can derive titles from
    # (weekday, hour_band). Keep the payload small - only fields the model
    # needs to match.
    usuals = await store.usuals.list()
    people_by_id = {p.person_id: p for p in await store.people.list()}
    context["usuals"] = [
        {
            "weekday": int(u.weekday),
            "hour_band": u.hour_band.value,
            "activity_type": u.activity_type.value,
            "display_summary": u.display_summary,
            "person_name": people_by_id[u.person_id].display_name
            if u.person_id in people_by_id
            else None,
        }
        for u in usuals
    ]

    spec = AgentSpec(
        name="BookAgent",
        model="flash",
        system=SYSTEM,
        response_schema=BookAgentResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec, user_input=message, context=context, store=store
    )
