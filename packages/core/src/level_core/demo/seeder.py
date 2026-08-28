"""Demo seeder that resets to a pristine state on every login.

``seed_demo_user`` is what ``POST /v1/auth/demo`` calls once a
scenario has been selected. On every invocation it:

1. **Resets** every session-mutable slot for the demo user (see
   ``reset_demo_state``). This is why judge B who lands on the same
   demo slot as judge A sees a clean state - no leftover priorities,
   chat turns, feedback verdicts, or edited people from the prior
   session.
2. Writes ``store.profile`` with identity + calendar window + a
   persistent ``demo`` marker (so /v1/me can flip
   ``google_connected``-equivalent gates on).
3. Writes ``store.people`` from the scenario config (all
   ``status="kept"`` - the RoleAgent will never re-propose these, so
   the app opens on a curated state instead of a review pile).
4. Writes ``store.agenda`` with expanded ICS events (activity types +
   person matches pre-filled - no LLM required).
5. Writes ``store.daily_agenda`` rebuilt from the events for O(1)
   /today reads.
6. Writes ``store.usuals`` clustered from the seeded events so the
   "usuals" and "missing this week" surfaces are populated on click 1.
7. Writes ``profile["proactive_cards"]`` via
   ``regenerate_proactive_cards`` so "Level noticed while you slept"
   lands on click 1 without waiting for the nightly job.

The reset is unconditional (every login = fresh state) because a
demo user is by definition throwaway. Contributors iterating locally
who want to preserve state across clicks can simply refresh the tab -
the session cookie persists and the seeder only fires on explicit
``POST /v1/auth/demo``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from level_core.calendar.proactive import regenerate_proactive_cards
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


# User-id prefix that identifies a demo slot. Kept as a module
# constant so the defensive guard inside ``reset_demo_state`` and any
# future admin tooling never diverge on what "is a demo user id?"
# means. Two shapes today: ``u_demo_solo`` / ``u_demo_family`` (slot
# 0, backward-compatible with early local dev) and
# ``u_demo_solo_<slot>`` (slot >= 1 in cloud demo mode).
DEMO_USER_ID_PREFIX = "u_demo_"

# Profile subkey used by the /v1/media/recap route to cache the Veo
# video URL, poster, and per-ISO-week regeneration counter. Must
# stay in sync with ``MEDIA_CACHE_KEY`` in
# ``packages/api/src/level_api/routes/media.py``. Duplicated (rather
# than imported) to avoid a level_core -> level_api layering
# violation; a guard test in the media suite would be a reasonable
# safety net if this drifts. Preserved across demo resets so a
# ~$1.20 Veo generation isn't thrown away every time a judge clicks
# "Try demo".
_MEDIA_CACHE_PROFILE_KEY = "media_cache"


async def reset_demo_state(store: UserStore) -> dict[str, object]:
    """Wipe every session-mutable slot on ``store``. Returns a small
    summary dict for logging + tests.

    Called at the top of ``seed_demo_user`` so each click of "Try demo"
    lands the judge on a pristine state - no leftover chat turns,
    priorities, reminders, feedback verdicts, edited people, or
    memory-bank entries from a prior session on the same slot.

    Performance path (why this shape, not "list every collection then
    delete_many"):
      1. **Cold-slot fast path**: read the profile KV first. If
         the slot has no profile yet (untouched), skip the wipe
         entirely - there's nothing to remove and one KV read is
         cheaper than N list calls on cloud. This turns the first
         judge's login into a straight seed.
      2. **Warm-slot fast path**: delegate to ``store.reset_all()``,
         the backend-native recursive delete. On Firestore that's a
         single ``client.recursive_delete`` call that batches
         internally, replacing 10 sequential list+delete pairs. On
         local that's a single ``shutil.rmtree`` of the user's dir.

    **Defensive guard**: only proceed if the store's user id starts
    with the demo prefix. If some future code path ever routes a real
    user's store through here by mistake, this refuses rather than
    nuke their data. Returns an empty dict in that case so the caller
    can log the refusal but continue.
    """
    if not store.user_id.startswith(DEMO_USER_ID_PREFIX):
        logger.warning(
            "demo.reset.refused_non_demo_user",
            user_id=store.user_id,
        )
        return {}

    # Cold-slot short-circuit: if the profile KV is empty/None this
    # slot has never been touched, so there's no pollution to wipe
    # and any list-every-collection loop would burn RTT confirming
    # zeroes. One KV read is enough to prove the slot is fresh.
    profile = await store.profile.read()
    if not profile:
        logger.info("demo.reset.cold_slot", user_id=store.user_id)
        return {"reset": False, "reason": "cold_slot"}

    # Preserve the Veo/Lyria cache across the wipe. Every /v1/media
    # generation is a real GCP spend (~$1.20 per Veo Fast call), and
    # the whole point of caching it per ISO week is to amortize
    # that across judge sessions on the same demo slot. If we let
    # ``reset_all()`` drop the cache, every "Try demo" click forces
    # a fresh $1.20 Veo call on the next /week visit - defeating
    # both the cache and the regenerate-quota rate limiter.
    #
    # Design notes:
    #   * The recap prompt is category-labels-only (no names, no
    #     event bodies), so a video generated for scenario A is a
    #     valid abstract cinematic loop for scenario B on the same
    #     slot too. Cross-scenario reuse is intentional.
    #   * We preserve the entire ``media_cache`` blob, not just the
    #     video URL - the ``recap_regens`` counter must survive
    #     too, otherwise a judge could bypass the weekly quota by
    #     clicking "Try demo" between regenerations.
    #   * The video URL is a signed GCS/Vertex URL that Veo minted;
    #     it doesn't leak PII and remains valid across the reset.
    preserved_media_cache = (
        profile.get(_MEDIA_CACHE_PROFILE_KEY)
        if isinstance(profile, dict)
        else None
    )

    # Warm slot: prior session left state. Prefer the backend-native
    # recursive delete; fall back to the old per-repo path only if
    # the backend forgot to wire ``reset_all`` (defensive - both
    # shipped backends implement it).
    try:
        await store.reset_all()
    except NotImplementedError:
        # Legacy path preserved for any backend that doesn't expose
        # reset_all. Kept minimal - covered by cold-slot fast path
        # for the common case anyway.
        repo_specs: list[tuple[str, object, str]] = [
            ("chat_turns", store.chat_turns, "turn_id"),
            ("reminders", store.reminders, "reminder_id"),
            ("priorities", store.priorities, "priority_id"),
            ("negatives", store.negatives, "negative_id"),
            ("ai_audit", store.ai_audit, "audit_id"),
            ("contacts", store.contacts, "contact_id"),
            ("usuals", store.usuals, "usual_id"),
            ("people", store.people, "person_id"),
            ("agenda", store.agenda, "event_id"),
            ("daily_agenda", store.daily_agenda, "date"),
        ]
        for _, repo, id_field in repo_specs:
            items = await repo.list()  # type: ignore[attr-defined]
            if not items:
                continue
            ids = [str(getattr(item, id_field)) for item in items]
            await repo.delete_many(ids)  # type: ignore[attr-defined]
        await store.profile.write({})
        await store.calendar_sync.write({})
        await store.tokens.write({})

    # Restore the preserved Veo/Lyria cache into the fresh profile.
    # ``update_fields`` merges, so this survives the subsequent
    # ``seed_demo_user`` writes that layer identity + demo markers
    # on top. Skipped entirely when nothing was cached (cold-slot
    # path already returned, and warm slots without media just
    # skip the merge write).
    if isinstance(preserved_media_cache, dict) and preserved_media_cache:
        await store.profile.update_fields(
            **{_MEDIA_CACHE_PROFILE_KEY: preserved_media_cache}
        )
        logger.info(
            "demo.reset.media_cache_preserved",
            user_id=store.user_id,
            has_recap=bool(preserved_media_cache.get("recap")),
        )

    logger.info("demo.reset", user_id=store.user_id)
    return {"reset": True, "media_cache_preserved": bool(preserved_media_cache)}


async def seed_demo_user(
    store: UserStore,
    *,
    scenario_id: str,
    now: datetime | None = None,
) -> DemoSeedResult:
    """Seed ``store`` for the named demo scenario.

    Always resets the demo user's state first (see ``reset_demo_state``)
    so multiple judges hitting the same slot get an unpolluted view.
    Idempotent across repeat clicks by the same judge - counts stay
    stable, no doubling.
    """
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ValueError(f"unknown demo scenario: {scenario_id!r}")

    # Wipe first so any prior-session pollution (chat turns,
    # priorities, edited people, feedback verdicts, memory_bank
    # entries) is gone before we lay down the fresh seed. Only
    # skipped by the defensive prefix guard, never by choice.
    reset_result = await reset_demo_state(store)
    # If the reset touched a demo slot (cold or warm), the people
    # collection is guaranteed empty. Skip the per-spec
    # ``find_person_by_name`` lookups in that case - each was a
    # network round trip on Firestore returning nothing, saving
    # ~one RTT per scenario person.
    people_slot_fresh = bool(reset_result)

    people = await _seed_people(store, scenario, slot_is_fresh=people_slot_fresh)
    # 28 days back gives four full weeks of history - enough for the
    # usuals engine to establish a stable majority-vote display name
    # even when the ICS fixture intentionally varies event wording
    # (see the "_messy_weekly" blocks in
    # packages/jobs/src/level_jobs/make_caregiver_ics.py). 14 days
    # was the default; two weeks of history isn't enough for the
    # "detects messy repeats" demo point to land cleanly because a
    # 1-1 tie in the majority vote picks whichever variant the ICS
    # emitted first, hiding the "clean" wording.
    events = load_events(
        scenario.ics_path(),
        people=people,
        tz=scenario.tz,
        anchor_date=scenario.anchor_date,
        now=now,
        days_back=28,
    )
    await _replace_agenda(store, events, tz=scenario.tz)
    usuals_written = await _seed_usuals(store, events, people, tz=scenario.tz)
    await _write_profile(store, scenario)

    # Populate the "Level noticed while you slept" section on click
    # one. Same helper the nightly job uses (see
    # ``level_core.calendar.proactive``), so demo cards are
    # generated with identical logic - not a demo-mode-only branch.
    # Deterministic; no LLM.
    #
    # Pass ``people`` and ``usuals_written`` context in so the helper
    # can skip the two redundant Firestore reads it would otherwise
    # do (people list twice + usuals list). Cheap plumbing, ~150ms
    # off the seed on cloud.
    cards_written = await regenerate_proactive_cards(
        store,
        tz=ZoneInfo(scenario.tz),
        events=events,
        people=people,
    )

    logger.info(
        "demo.seeded",
        user=store.user_id,
        scenario=scenario.id,
        people=len(people),
        events=len(events),
        usuals=usuals_written,
        proactive_cards=cards_written,
    )
    # user_id comes from the store (caller's chosen slot) so cloud
    # demo mode - which pools multiple users under one scenario -
    # gets the slot-specific id back, not the scenario default.
    return DemoSeedResult(
        scenario_id=scenario.id,
        user_id=store.user_id,
        email=scenario.email,
        display_name=scenario.display_name,
        tz=scenario.tz,
        people_count=len(people),
        events_count=len(events),
    )


async def _seed_people(
    store: UserStore,
    scenario: ScenarioConfig,
    *,
    slot_is_fresh: bool = False,
) -> list[CarePerson]:
    """Upsert the scenario's people as ``status="kept"`` and return them.

    ``slot_is_fresh`` says "you just wiped this slot, don't bother
    checking whether these people already exist". Saves one
    ``store.people.list()`` per spec on the fresh path - meaningful
    on Firestore where each list is a round trip.
    """
    from level_core.schemas.care import role_for_relation
    from level_core.storage.care_store import find_person_by_name, new_id

    out: list[CarePerson] = []
    for spec in scenario.people:
        existing = (
            None
            if slot_is_fresh
            else await find_person_by_name(store, spec.display_name)
        )
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
    """Drop any prior demo agenda and write fresh events + daily index.

    In the normal flow ``reset_demo_state`` has already wiped agenda,
    so ``existing`` is empty and the fast path skips straight to
    upsert. This block stays as a belt-and-braces defense so an
    accidental skipped reset (e.g. legacy backend without
    ``reset_all``) still leaves us in a consistent state.
    """
    existing = await store.agenda.list()
    if existing:
        # Only drop rows we recognize as demo-origin - never trample
        # a real Google-sourced agenda accidentally. Batched
        # delete instead of the per-id loop the old impl did (250
        # sequential Firestore round trips added up to ~15s of pure
        # latency in the worst case).
        stale_ids = [e.event_id for e in existing if e.event_id.startswith("demo:")]
        if stale_ids:
            await store.agenda.delete_many(stale_ids)

    if events:
        await store.agenda.upsert_many(events)
        await _rebuild_daily_agenda(store, events, tz=ZoneInfo(tz))


async def _write_profile(store: UserStore, scenario: ScenarioConfig) -> None:
    """Merge the demo identity fields into ``store.profile``.

    ``update_fields`` avoids the extra read-then-write cycle the old
    impl did - on Firestore that's one round trip instead of two. The
    merge semantics also correctly preserve any pre-existing profile
    keys on the non-demo test path where ``reset_demo_state`` refused
    to wipe (so the profile carries arbitrary content the test fixture
    laid down).
    """
    await store.profile.update_fields(
        # store.user_id (not scenario.user_id) so slotted demo users
        # in cloud mode record their actual slot id here.
        user_id=store.user_id,
        email=scenario.email,
        display_name=scenario.display_name,
        tz=scenario.tz,
        calendar_window_days_back=28,
        calendar_window_days_forward=28,
        **{
            PROFILE_DEMO_KEY: scenario.id,
            PROFILE_DEMO_SEEDED_AT_KEY: datetime.utcnow().isoformat(),
        },
    )
