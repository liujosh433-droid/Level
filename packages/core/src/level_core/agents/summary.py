"""SummaryAgent: 2-3 sentence 'hear my day' summary for TTS."""

from __future__ import annotations

from pydantic import BaseModel

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.storage.base import UserStore


class SummaryResponse(BaseModel):
    summary: str


SYSTEM = """You are Level, speaking a short summary of today's schedule to a busy caregiver.

Style: warm but calm. 2-3 sentences. Mention 1-2 anchor events. If there are
missing usuals or reminders, name at most one. Never invent events. Say
"you" not "the user". Return only JSON."""


async def run(
    *,
    store: UserStore,
    date_label: str,
    event_lines: list[str],
    missing_usual_lines: list[str],
    reminder_lines: list[str],
) -> AgentResult:
    user_input = "\n".join(
        [
            f"Date: {date_label}",
            "Events:",
            *[f"- {line}" for line in event_lines],
            "Missing usuals:",
            *[f"- {line}" for line in missing_usual_lines],
            "Reminders on today's events:",
            *[f"- {line}" for line in reminder_lines],
        ]
    )
    spec = AgentSpec(
        name="SummaryAgent",
        model="flash",
        system=SYSTEM,
        response_schema=SummaryResponse,
        max_turns=3,
        temperature=0.4,
        require_source_span=False,
    )
    return await call_agent(spec, user_input=user_input, store=store)
