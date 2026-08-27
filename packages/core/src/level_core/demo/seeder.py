"""Idempotent demo seeder.

``seed_demo_user`` is what ``POST /v1/auth/demo`` calls once a
scenario has been selected. It writes:

- ``store.profile`` with identity + calendar window + a persistent
  ``demo`` marker (so /v1/me can flip ``google_connected``-equivalent
  gates on).
- ``store.people`` with pre-approved (``status="kept"``) people. The
  RoleAgent will never re-propose these, so the app opens on a
  curated state instead of a review pile.
- ``store.agenda`` with expanded ICS events (activity types +
  person matches pre-filled - no LLM required).
- ``store.daily_agenda`` rebuilt from the events for O(1) /today reads.

If the same scenario is loaded twice we treat it as idempotent: same
user, same people, and the agenda is fully replaced so the demo week
stays anchored to "now" no matter when the judge clicks the button.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from level_core.calendar.sync import _rebuild_daily_agenda
from level_core.calendar.usuals import compute_usuals_from_events
from level_core.demo.ics_loader import load_events
from level_core.demo.scenarios import SCENARIOS, ScenarioConfig
from level_core.observability import get_logger
from level_core.schemas import Usual, UsualStatus
from level_core.schemas.care import CarePerson
from level_core.storage.base import UserStore

logger = get_logger(__name__)

# Marker fields on the profile. Kept together so the API layer can
# distinguish a demo user without introducing a top-level flag on
# every user record.
PROFILE_DEMO_KEY = "demo_scenario"
PROFILE_DEMO_SEEDED_AT_KEY = "demo_seeded_at"


@dataclass(frozen=True)
class DemoSeedResult:
    """Return value from the seeder - useful for tests + API responses."""

    scenario_id: str
    user_id: str
    email: str
    display_name: str
    tz: str
    people_count: int
    events_count: int


def is_demo_user(profile: dict | None) -> bool:
    """True iff the profile carries the demo scenario marker."""
    if not profile:
        return False
    return bool(profile.get(PROFILE_DEMO_KEY))


async def seed_demo_user(
    store: UserStore,
    *,
    scenario_id: str,
    now: datetime | None = None,
) -> DemoSeedResult:
    """Seed ``store`` for the named demo scenario. Idempotent."""
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ValueError(f"unknown demo scenario: {scenario_id!r}")

    people = await _seed_people(store, scenario)
    events = load_events(
        scenario.ics_path(),
        people=people,
        tz=scenario.tz,
        anchor_date=scenario.anchor_date,
        now=now,
    )
    await _replace_agenda(store, events, tz=scenario.tz)
    usuals_written = await _seed_usuals(store, events, people, tz=scenario.tz)
    await _write_profile(store, scenario)

    logger.info(
        "demo.seeded",
        user=store.user_id,
        scenario=scenario.id,
        people=len(people),
        events=len(events),
        usuals=usuals_written,
    )
    return DemoSeedResult(
        scenario_id=scenario.id,
        user_id=scenario.user_id,
        email=scenario.email,
        display_name=scenario.display_name,
        tz=scenario.tz,
        people_count=len(people),
        events_count=len(events),
    )


async def _seed_people(
    store: UserStore, scenario: ScenarioConfig
) -> list[CarePerson]:
    """Upsert the scenario's people as ``status="kept"`` and return them."""
    from level_core.schemas.care import role_for_relation
    from level_core.storage.care_store import find_person_by_name, new_id

    out: list[CarePerson] = []
    for spec in scenario.people:
        existing = await find_person_by_name(store, spec.display_name)
        aliases = list(spec.aliases)
        if existing is not None:
            # Re-run: keep the same person_id + status, but refresh
            # aliases in case we edited the scenario since last seed.
            updated = existing.model_copy(
                update={
                    "relation": spec.relation,
                    "care_role_id": role_for_relation(spec.relation),
                    "aliases": _merge_aliases(existing.aliases, aliases),
                    "is_self": spec.is_self,
                    "status": "kept",
                }
            )
            out.append(await store.people.upsert(updated))
            continue

        person = CarePerson(
            person_id=new_id("p"),
            display_name=spec.display_name,
            relation=spec.relation,
            care_role_id=role_for_relation(spec.relation),
            aliases=aliases,
            is_self=spec.is_self,
            status="kept",
            source_span="demo-mode-seed",
        )
        out.append(await store.people.upsert(person))
    return out


def _merge_aliases(current: list[str], incoming: list[str]) -> list[str]:
    seen = {a.lower() for a in current}
    merged = list(current)
    for a in incoming:
        if a.lower() not in seen:
            merged.append(a)
            seen.add(a.lower())
    return merged


async def _seed_usuals(
    store: UserStore,
    events,  # type: ignore[no-untyped-def]  # list[CachedEvent]
    people: list[CarePerson],
    *,
    tz: str,
) -> int:
    """Compute + upsert usuals from the seeded events.

    Without this the missing-usuals card on /today would be empty on
    first render - and the "Nova ballet is missing this Thursday"
    hook is one of the key demo moments the ICS fixtures were built
    around. Cheap, deterministic, no LLM required.
    """
    from zoneinfo import ZoneInfo

    candidates = compute_usuals_from_events(events, people, tz=ZoneInfo(tz))
    if not candidates:
        return 0
    existing = {u.usual_id: u for u in await store.usuals.list()}
    payloads: list[Usual] = []
    for c in candidates:
        usual_id = Usual.compose_id(c.person_id, c.weekday, c.hour_band)
        prior = existing.get(usual_id)
        if prior and prior.status == UsualStatus.NOT_ME:
            continue
        payload = Usual(
            usual_id=usual_id,
            person_id=c.person_id,
            weekday=c.weekday,
            hour_band=c.hour_band,
            activity_type=c.activity_type,
            display_summary=c.display_summary,
            source_event_uids=list(c.source_event_uids),
            confidence=c.confidence,
            status=prior.status if prior else UsualStatus.PROPOSED,
        )
        payloads.append(payload)
    if payloads:
        await store.usuals.upsert_many(payloads)
    return len(payloads)


async def _replace_agenda(store: UserStore, events, *, tz: str) -> None:  # type: ignore[no-untyped-def]
    """Drop any prior demo agenda and write fresh events + daily index."""
    existing = await store.agenda.list()
    if existing:
        # Only drop rows we recognize as demo-origin - never trample
        # a real Google-sourced agenda accidentally.
        stale_ids = [e.event_id for e in existing if e.event_id.startswith("demo:")]
        if stale_ids:
            for eid in stale_ids:
                await store.agenda.delete(eid)

    if events:
        await store.agenda.upsert_many(events)
        await _rebuild_daily_agenda(store, events, tz=ZoneInfo(tz))


async def _write_profile(store: UserStore, scenario: ScenarioConfig) -> None:
    profile = dict(await store.profile.read() or {})
    profile.update(
        {
            "user_id": scenario.user_id,
            "email": scenario.email,
            "display_name": scenario.display_name,
            "tz": scenario.tz,
            "calendar_window_days_back": 14,
            "calendar_window_days_forward": 28,
            PROFILE_DEMO_KEY: scenario.id,
            PROFILE_DEMO_SEEDED_AT_KEY: datetime.utcnow().isoformat(),
        }
    )
    await store.profile.write(profile)
