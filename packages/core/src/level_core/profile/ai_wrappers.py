"""Specialized wrappers around Gemini — not a second keyword brain.

Product intelligence is AI-first (``care_infer_llm``, commitment gate, check-in).
When the model is down or returns thin output, we **degrade honestly** or apply
**structured wrappers** over real data (calendar free slots, schema cleanup,
id sanitization) — never invent care roles with regex.

Wrappers that belong here / nearby:
- Calendar free-slot grounding (``commitment_gate._ground_availability_reply``)
- Week load role classify (``care_infer_llm.classify_week_event_roles_ai``)
- People consolidate (``care_infer_llm.consolidate_care_people_ai``) —
  Papa/Dad/Robert → one elder when holistic still emits nickname duplicates
- Schema cleanup on model JSON (HH:MM shape, by_days vs local_date conflict)
- Outbound id leak strip (``_sanitize_message``)
- Guardrails (Model Armor / local safety patterns)

Legacy regex Care Profile code lives in ``synthesize`` only behind
``LEVEL_ALLOW_HEURISTIC_CARE`` / unit tests — not the happy path.
"""

from __future__ import annotations


CARE_PROFILE_PENDING = (
    "I'm still building your Care Profile from your calendar — check back in a moment."
)

CARE_NOTE_AI_INSUFFICIENT = (
    "I saved what you said, but I couldn't map it onto your Care Profile yet. "
    "Try again in a moment, or name the care role (kids, work, elder care, recovery)."
)

MEMORY_AI_INSUFFICIENT = (
    "I couldn't extract care facts from that paste. "
    "Try a shorter Memory summary, or paste again in a moment."
)

SCHEDULE_AI_UNAVAILABLE = (
    "I can't check your calendar right now. Try again in a moment."
)


def degrade_message(kind: str) -> str:
    """User-visible copy when AI or a wrapper cannot complete the job."""
    return {
        "care_pending": CARE_PROFILE_PENDING,
        "care_note": CARE_NOTE_AI_INSUFFICIENT,
        "memory": MEMORY_AI_INSUFFICIENT,
        "schedule": SCHEDULE_AI_UNAVAILABLE,
    }.get(kind, "Something went wrong — try again in a moment.")


__all__ = [
    "CARE_NOTE_AI_INSUFFICIENT",
    "CARE_PROFILE_PENDING",
    "MEMORY_AI_INSUFFICIENT",
    "SCHEDULE_AI_UNAVAILABLE",
    "degrade_message",
]
