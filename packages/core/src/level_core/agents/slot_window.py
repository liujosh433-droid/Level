"""SlotWindowAgent: infer a socially-normal clock window for a plan
name that the deterministic pattern table doesn't recognize.

The booking fast-path in ``schedule.slots.infer_event_kind`` already
covers common cases (breakfast, lunch, dinner, drinks, coffee) with
regex + fixed hour windows. Real users type things the table can't
know about ahead of time: "afternoon tea", "power lunch", "playdate
with Nova's friend", "book club", "Theo's nap window". For those
labels the fast path returns None and the async caller escalates
HERE.

Design notes:

- **Extractor-class agent.** Returns structured hours only, never
  user-facing text. Cheap flash call, single turn, no source_span
  requirement (the "span" would be the label itself, which the
  caller already extracted).

- **Result is always usable.** Even when Gemini is unavailable
  (LLMUnavailable, safety block, quota) the outer ``call_agent``
  soft-degrades and we fall back to the deterministic default
  ``(8, 20)`` waking-hours window - same shape as if the regex path
  had defaulted. Booking recommendations never 500.

- **Windows are hard-clamped.** The system prompt asks for hours in
  [6, 22], and ``ProposedSlotWindow.model_validator`` re-clamps
  before returning to protect against a model that ignores the
  bound. ``recommend_slots`` also has its own floor/ceiling, so
  three layers of defence keep a hallucinated "3am nap window"
  from ever showing up as a suggested booking time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.storage.base import UserStore


class ProposedSlotWindow(BaseModel):
    """A socially-normal clock window for a plan the caller wants to book."""

    start_hour: int = Field(
        ...,
        ge=6,
        le=21,
        description="Earliest hour to start (24h, local). E.g. 11 for lunch.",
    )
    end_hour: int = Field(
        ...,
        ge=7,
        le=22,
        description="Latest hour a slot must end by (24h, local). Exclusive upper bound; e.g. 14 for lunch means slots must end by 14:00.",
    )
    ideal_hour: float = Field(
        ...,
        ge=6.0,
        le=22.0,
        description="Preferred midpoint of the window. E.g. 12.5 for lunch = 12:30pm.",
    )
    duration_minutes: int = Field(
        ...,
        ge=15,
        le=240,
        description="Typical duration. E.g. 60 for lunch, 90 for dinner, 30 for coffee.",
    )
    label: str = Field(
        ...,
        max_length=48,
        description="The plan name in the user's words, lightly cleaned. Echo back the label the caller supplied.",
    )

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ProposedSlotWindow":
        # Belt-and-suspenders: even with Pydantic ge/le, a model
        # that returns start >= end or an ideal outside the window
        # would poison recommend_slots. Clamp and reorder instead
        # of raising - the caller is already in a fallback path
        # and would rather have a slightly-adjusted window than
        # an exception.
        if self.end_hour <= self.start_hour:
            object.__setattr__(self, "end_hour", min(22, self.start_hour + 1))
        if not (self.start_hour <= self.ideal_hour <= self.end_hour):
            mid = (self.start_hour + self.end_hour) / 2.0
            object.__setattr__(self, "ideal_hour", round(mid, 1))
        return self


class SlotWindowAgentResponse(BaseModel):
    window: ProposedSlotWindow | None = None


SYSTEM = """You infer a socially-normal clock window for a plan a caregiver wants to book.

Given the plan LABEL (what the user is trying to schedule), return the
hours a reasonable adult would actually book that thing at, plus a
typical duration.

Rules:
- All hours are **wall-clock hours in the user's local timezone**,
  in [6, 22]. Do NOT convert to UTC. Do NOT assume any specific
  timezone. The caller will interpret your integers as local hours
  wherever the user actually is. If a lunch is at noon in LA, it's
  also at noon in Tokyo - always return the local wall-clock time.
- Never suggest overnight or pre-dawn slots even if the calendar
  has empty time then.
- `start_hour` is inclusive; `end_hour` is exclusive (a slot has to
  END by `end_hour`).
- `ideal_hour` is the preferred midpoint. Use decimals for :30 (e.g.
  12.5 = 12:30pm, 18.5 = 6:30pm).
- `duration_minutes` should be the typical booked duration for that
  kind of plan. Examples: quick coffee = 30, tea = 45, lunch = 60,
  dinner = 90, board meeting = 60, playdate = 90, doctor visit = 45,
  haircut = 45, workout = 60, book club = 120.
- Prefer narrower windows for socially-anchored plans (dinner =
  17-21) and wider for flexible ones (coffee = 8-14, playdate =
  9-18).
- `label` MUST echo the label the caller supplied, unchanged except
  for whitespace trimming.

Return {"window": null} ONLY if the label is meaningless or clearly
not a bookable plan (e.g. "the thing", "stuff", "??"). If in doubt,
propose a window - the deterministic default that fires when you
return null is 8am-8pm, which is worse than a reasonable guess."""


async def run(
    *,
    store: UserStore,
    message: str,
    label: str,
) -> AgentResult:
    """Ask Gemini for a window for this specific plan label.

    ``message`` is the full user turn (for context - "lunch this
    afternoon" hints at afternoon-leaning lunch). ``label`` is what
    ``plan_label_from_message`` extracted so the model knows exactly
    which words to time-window.
    """
    spec = AgentSpec(
        name="SlotWindowAgent",
        model="flash",
        system=SYSTEM,
        response_schema=SlotWindowAgentResponse,
        max_turns=1,
        temperature=0.0,
        # No hallucination guard needed: the response is pure
        # numeric hours, not a quote from the input.
        require_source_span=False,
    )
    return await call_agent(
        spec,
        user_input=message,
        context={"label": label},
        store=store,
    )
