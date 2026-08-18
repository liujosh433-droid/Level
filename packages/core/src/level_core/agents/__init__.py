"""ADK-orchestrated Gemini agents.

Each agent module exposes:
  - a pure `async def run(...)` used by the API for cheap direct calls
  - an ADK Tool wrapper used by the top-level Level agent composer

All calls go through `call_agent()` in `base.py` which enforces
guardrails (schema, prompt fence, hallucination check, retry, cost cap).
"""

from level_core.agents.base import call_agent
from level_core.agents.gate import GateDecision, check_gate

__all__ = ["call_agent", "GateDecision", "check_gate"]
