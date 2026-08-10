"""Calendar commitment gate — availability + add-with-confirm."""

from level_core.calendar.availability import (
    find_conflicts,
    find_free_slots,
    find_free_slots_nearby,
)
from level_core.calendar.commitment_gate import propose_from_text
from level_core.calendar.proposals import ProposalStore, build_proposal_store

__all__ = [
    "ProposalStore",
    "build_proposal_store",
    "find_conflicts",
    "find_free_slots",
    "find_free_slots_nearby",
    "propose_from_text",
]
