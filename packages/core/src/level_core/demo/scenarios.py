"""Demo scenario catalog.

Each scenario ties one ICS fixture to a stable synthetic user +
people set. We deliberately keep the catalog small (two scenarios)
so a judge is not overwhelmed on the landing page; more variety
can be added by dropping another ICS in ``example-data/`` and
appending here.

Design notes
------------
- ``user_id`` is stable across sessions so a judge can close the tab,
  come back, and pick up where they left off. It also lets us skip
  re-seeding if the same demo has already been loaded.
- ``anchor_date`` matches the "demo today" the ICS was engineered
  around. The loader shifts every event by whole weeks so the
  missing-usual story ("Nova ballet skipped this Thursday") stays
  aligned with the caller's actual current week regardless of when
  they run the demo. Whole-week shift preserves weekday alignment.
- ``people`` are pre-seeded with ``status="kept"`` so the RoleAgent
  never has to re-propose them; the app opens on a calm, curated
  state instead of a wall of "Is this person new?" cards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from level_core.schemas.care import CareRelation


@dataclass(frozen=True)
class DemoPerson:
    """A pre-seeded CarePerson row for a demo scenario."""

    display_name: str
    relation: CareRelation
    aliases: tuple[str, ...] = ()
    is_self: bool = False


@dataclass(frozen=True)
class ScenarioConfig:
    """One demo persona: ICS + people + identity."""

    id: str
    label: str
    tagline: str
    user_id: str
    email: str
    display_name: str
    ics_filename: str
    tz: str = "America/Los_Angeles"
    people: tuple[DemoPerson, ...] = ()
    anchor_date: date = date(2026, 8, 26)

    def ics_path(self) -> Path:
        """Resolve the ICS fixture path relative to the repo root."""
        return _repo_root() / "example-data" / self.ics_filename


def _repo_root() -> Path:
    """Walk up from this file to the repo root (looks for pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "example-data").is_dir():
            return parent
    # Should never happen in a normal checkout - the seeder will raise
    # a clearer error when it tries to read the ICS file.
    return here.parent


SCENARIOS: dict[str, ScenarioConfig] = {
    "family": ScenarioConfig(
        id="family",
        label="Two-parent family",
        tagline="Josh + Alex, two kids at different schools, one elder in memory care.",
        user_id="u_demo_family",
        email="demo-family@level.local",
        display_name="Josh",
        ics_filename="caregiver-month.ics",
        people=(
            DemoPerson(
                display_name="Josh",
                relation=CareRelation.SELF,
                aliases=("Me", "Dad"),
                is_self=True,
            ),
            DemoPerson(
                display_name="Alex",
                relation=CareRelation.COPARENT,
                aliases=("Alex R.",),
            ),
            DemoPerson(
                display_name="Nova",
                relation=CareRelation.CHILD,
                aliases=("Nova K.",),
            ),
            DemoPerson(
                display_name="Theo",
                relation=CareRelation.CHILD,
            ),
            DemoPerson(
                display_name="Helen",
                relation=CareRelation.ELDER,
                aliases=("Mom", "Helen K."),
            ),
        ),
    ),
    "solo": ScenarioConfig(
        id="solo",
        label="Solo caregiver",
        tagline="Josh on his own with two kids and his mom Helen. No co-parent.",
        user_id="u_demo_solo",
        email="demo-solo@level.local",
        display_name="Josh",
        ics_filename="caregiver-month-solo.ics",
        people=(
            DemoPerson(
                display_name="Josh",
                relation=CareRelation.SELF,
                aliases=("Me", "Dad"),
                is_self=True,
            ),
            DemoPerson(
                display_name="Nova",
                relation=CareRelation.CHILD,
                aliases=("Nova K.",),
            ),
            DemoPerson(
                display_name="Theo",
                relation=CareRelation.CHILD,
            ),
            DemoPerson(
                display_name="Helen",
                relation=CareRelation.ELDER,
                aliases=("Mom", "Helen K."),
            ),
        ),
    ),
}


def list_scenarios() -> list[dict[str, str]]:
    """Return catalog rows suitable for the /v1/config/features endpoint."""
    return [
        {"id": s.id, "label": s.label, "tagline": s.tagline}
        for s in SCENARIOS.values()
    ]
