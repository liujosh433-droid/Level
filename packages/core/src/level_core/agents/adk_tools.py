"""Google ADK Tool wrappers so the top-level Level agent can compose sub-agents.

Day-to-day, the API calls each `run()` function directly for cost + latency.
The ADK tool surface exists so hackathon judges can invoke the full agent
graph via `adk` CLI or Vertex Agent Engine.
"""

from __future__ import annotations

from typing import Any

from level_core.agents import (
    activity,
    chat_router,
    email,
    priority,
    reminder,
    role,
    summary,
    usual,
)
from level_core.config import get_settings
from level_core.storage.factory import get_store

TOOLS: dict[str, Any] = {
    "chat_router": chat_router.run,
    "propose_care_people": role.run,
    "disambiguate_usual": usual.run,
    "classify_activity_type": activity.run,
    "extract_priority": priority.run,
    "extract_reminder": reminder.run,
    "draft_email": email.run,
    "summarize_day": summary.run,
}


def build_level_agent() -> Any:
    """Return a top-level ADK Agent composing every sub-agent as a tool.

    Loaded lazily so `google.adk` isn't required at import time; the tool
    surface degrades to a plain dict of callables if ADK isn't installed
    (unit tests hit the callables directly).
    """
    try:
        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool
    except Exception:  # pragma: no cover - ADK optional for local dev
        return None

    def _wrap(name: str, fn: Any, user_id_hint: str = "demo-user") -> FunctionTool:
        async def _tool(**kwargs: Any) -> Any:
            store = get_store(user_id_hint)
            return await fn(store=store, **kwargs)

        _tool.__name__ = name
        return FunctionTool(_tool)

    return LlmAgent(
        name="LevelAgent",
        model=get_settings().level_model_pro,
        description="Caregiver partner orchestrating calendar sync, usuals, priorities, reminders, and email drafting.",
        instruction=(
            "You are Level. Use the available tools to help a busy caregiver."
            " Never invent people, events, or reminders. Always confirm before"
            " sending mail or booking calendar events."
        ),
        tools=[_wrap(name, fn) for name, fn in TOOLS.items()],
    )
