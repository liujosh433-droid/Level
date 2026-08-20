"""PersonEditAgent: turn a free-text correction into a structured edit.

Handles messages like:
  - "Alex is my co-parent" / "add Alex as co-parent"  (add)
  - "Robert is my kid, not my dad"        (change_relation)
  - "call her Nova, not Nova Ann"         (rename)
  - "Sam is me"                            (mark_self)
  - "drop Priya, that's my colleague"     (remove)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from level_core.agents.base import AgentResult, AgentSpec, call_agent
from level_core.schemas import CareRelation
from level_core.storage.base import UserStore


class PersonEdit(BaseModel):
    action: Literal["add", "change_relation", "rename", "mark_self", "remove", "unknown"]
    target_name: str = Field(..., description="Name Level should match against or add")
    new_relation: CareRelation | None = None
    new_display_name: str | None = None
    source_span: str = Field(..., description="Exact substring of user message")


class PersonEditResponse(BaseModel):
    edit: PersonEdit | None = None


SYSTEM = """You turn a caregiver's message into ONE structured edit on their people list.

Given the user's message and their current people (name, relation), decide:
- add: they INTRODUCED someone new ("Alex is my co-parent", "add Maya as my kid")
- change_relation: they said an EXISTING person belongs in a different bucket (child/elder/coparent/partner/self/other)
- rename: they gave a new preferred name for someone already on the list
- mark_self: they said someone IS them
- remove: they said that person shouldn't be in the list at all
- unknown: message isn't a person edit (return null edit)

For `add`, `target_name` is the new person's name and `new_relation` is required.
It does NOT need to already appear in <people>.
For other actions, `target_name` should match a name or alias in <people> (case-insensitive).
`source_span` MUST be an exact substring of the user_input.
Return exactly one edit or none."""


async def run(
    *,
    store: UserStore,
    message: str,
    history: list[dict[str, str]] | None = None,
    trace_id: str | None = None,
) -> AgentResult:
    people = await store.people.list()
    context: dict[str, object] = {
        "people": [
            {
                "person_id": p.person_id,
                "display_name": p.display_name,
                "relation": p.relation.value,
                "aliases": p.aliases,
                "is_self": p.is_self,
            }
            for p in people
        ],
    }
    if history:
        context["prior_turns"] = history
    spec = AgentSpec(
        name="PersonEditAgent",
        model="flash",
        system=SYSTEM,
        response_schema=PersonEditResponse,
        max_turns=1,
        temperature=0.0,
        require_source_span=True,
    )
    return await call_agent(
        spec,
        user_input=message,
        context=context,
        store=store,
        trace_id=trace_id,
    )
