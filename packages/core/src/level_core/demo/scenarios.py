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
from importlib.resources import as_file, files
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
        """Resolve the ICS fixture as an on-disk path.

        Reads via ``importlib.resources`` so the lookup works in every
        install shape:

          - repo checkout (``uv pip install -e``): ICS files sit at
            ``packages/core/src/level_core/demo/data/*.ics``
          - wheel install (Docker container via ``uv pip install
            --system``): the files travel inside the wheel to
            ``<site-packages>/level_core/demo/data/*.ics``

        Historically we walked up from ``__file__`` looking for a
        sibling ``example-data/`` at the repo root. That worked in
        editable installs and broke silently in wheel installs
        (site-packages has no sibling example-data/), which is what
        turned into ``demo_ics_missing`` on the deployed API.

        Returns a real ``Path`` because callers (icalendar, dateutil)
        want to open by filename. For a zipped wheel install
        ``as_file`` transparently extracts to a temp path.
        """
        resource = files("level_core.demo.data").joinpath(self.ics_filename)
        # as_file is a context manager, but for filesystem-installed
        # wheels the underlying path is stable for the process
        # lifetime. Exit the context immediately and return the path -
        # this is safe here because we're not in a zip install (we ship
        # the wheel as a plain wheel, and even Cloud Run's slim base
        # image extracts wheels to the filesystem).
        with as_file(resource) as path:
            return Path(path)


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


def user_id_for_slot(scenario_id: str, slot: int) -> str:
    """Compose a per-slot demo user id.

    Cloud demo mode assigns each caller a slot in
    ``[0, level_demo_slots_per_scenario)`` based on a stable hash of
    their client IP + scenario. That slot maps to a fixed user id
    like ``u_demo_solo_0`` so:

    - the same judge (same IP) lands on the same user each click,
      preserving their session state,
    - the total user population is bounded to
      ``slots * len(SCENARIOS)`` regardless of traffic, and
    - existing per-user cost caps and gate limits apply naturally.

    Local mode uses slot 0 always (single-tenant), which happens to
    equal the historical ``u_demo_<scenario>`` id when slot==0 is
    treated as "unslotted" - so we deliberately keep the unsuffixed
    form for slot 0 to stay backward-compatible with any state a
    contributor already has on disk.
    """
    if slot == 0:
        return f"u_demo_{scenario_id}"
    return f"u_demo_{scenario_id}_{slot}"


def slot_for_ip(ip: str, scenario_id: str, slots_per_scenario: int) -> int:
    """Deterministic slot assignment for a client IP.

    Same IP + scenario => same slot => same demo user across clicks.
    Uses SHA-256 rather than Python's built-in ``hash()`` because the
    latter is salted per-process and would reshuffle judges' assigned
    slots on every deploy.
    """
    import hashlib

    if slots_per_scenario <= 1:
        return 0
    key = f"{ip}:{scenario_id}".encode()
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:4], "big") % slots_per_scenario
