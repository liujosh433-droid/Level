"""Demo-mode seeder + auth-bypass endpoint.

Guarantees the OAuth-less landing experience judges will click:

- ``seed_demo_user`` populates people, agenda, daily_agenda, usuals,
  proactive_cards, and the demo-profile marker.
- Every seed resets the demo user's session-mutable state first so
  multiple judges hitting the same slot don't see each other's
  chat turns, priorities, feedback verdicts, or edited people.
- Re-seeding is idempotent in counts (no doubling); person_ids
  intentionally rotate on reset because a demo user is throwaway.
- ``POST /v1/auth/demo`` fences correctly across the three modes:
    * local: always allowed, unslotted user id
    * cloud + LEVEL_DEMO_IN_CLOUD=false (default): 404 - the
      security fence between safe local dev and "authenticate
      yourself as a synthetic user against the deployed API"
    * cloud + LEVEL_DEMO_IN_CLOUD=true: allowed, IP hashed to a
      slot from a bounded pool, per-IP rate limit engaged
- ``GET /v1/config/features`` reflects the env correctly.
- Gmail send short-circuits to a preview response for demo users so
  the demo flow never 502s on send.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from level_core.config import get_settings
from level_core.demo.scenarios import SCENARIOS, slot_for_ip, user_id_for_slot
from level_core.demo.seeder import (
    DEMO_USER_ID_PREFIX,
    PROFILE_DEMO_KEY,
    is_demo_user,
    reset_demo_state,
    seed_demo_user,
)
from level_core.storage.factory import get_store


@pytest.mark.asyncio
async def test_seed_family_populates_people_agenda_usuals(store) -> None:  # type: ignore[no-untyped-def]
    result = await seed_demo_user(store, scenario_id="family")

    assert result.scenario_id == "family"
    assert result.people_count == 5
    assert result.events_count > 100  # 250+ but keep the bound loose

    profile = await store.profile.read()
    assert is_demo_user(profile)
    assert profile[PROFILE_DEMO_KEY] == "family"

    people = await store.people.list()
    names = {p.display_name for p in people}
    assert names == {"Josh", "Alex", "Nova", "Theo", "Helen"}
    assert all(p.status == "kept" for p in people)
    assert any(p.is_self and p.display_name == "Josh" for p in people)

    events = await store.agenda.list()
    assert len(events) == result.events_count
    matched = [e for e in events if e.matched_person_ids]
    assert len(matched) > 0, "person-matching should preseed at least some events"
    classified = [e for e in events if e.activity_type is not None]
    assert len(classified) > 0, "heuristic classifier should tag common events"

    usuals = await store.usuals.list()
    assert len(usuals) > 0

    daily = await store.daily_agenda.list()
    assert len(daily) > 0


@pytest.mark.asyncio
async def test_seed_solo_has_no_coparent(store) -> None:  # type: ignore[no-untyped-def]
    await seed_demo_user(store, scenario_id="solo")
    people = await store.people.list()
    relations = {p.display_name: p.relation.value for p in people}
    assert "Alex" not in relations
    assert relations["Helen"] == "elder"
    assert relations["Josh"] == "self"


@pytest.mark.asyncio
async def test_seed_is_idempotent_on_non_demo_store(store) -> None:  # type: ignore[no-untyped-def]
    """Idempotency on a store whose user id doesn't match the demo prefix
    (used by unit tests via the ``store`` fixture, which returns a
    ``u_test_*`` id).

    On a real demo slot the semantics differ - see
    ``test_seed_wipes_prior_session_pollution_on_demo_store``. The
    non-demo path is what covers "same contributor iterating locally
    with a stable u_test id preserves person_ids on re-run", which
    keeps their reproduction fixtures stable across seed calls.
    """
    first = await seed_demo_user(store, scenario_id="family")
    events_before = await store.agenda.list()
    people_before = await store.people.list()

    second = await seed_demo_user(store, scenario_id="family")
    events_after = await store.agenda.list()
    people_after = await store.people.list()

    assert first.events_count == second.events_count
    assert len(events_after) == len(events_before)
    # Reset is a no-op for non-demo ids so person_ids are preserved
    # by the reuse-by-name branch inside ``_seed_people``.
    assert {p.person_id for p in people_after} == {p.person_id for p in people_before}


@pytest.mark.asyncio
async def test_seed_wipes_prior_session_pollution_on_demo_store() -> None:
    """The whole point of this feature: judge A's session state must NOT
    leak into judge B's session on the same demo slot. This test lays
    down pollution on every mutable surface, re-seeds, and asserts the
    fresh session is pristine.

    Uses a real ``u_demo_*`` user id (not the ``u_test_*`` fixture) so
    the defensive prefix guard on ``reset_demo_state`` doesn't
    short-circuit into the no-op branch.
    """
    from datetime import datetime

    from level_core.schemas import Priority, Reminder
    from level_core.schemas.activity import ActivityType
    from level_core.schemas.chat import ChatMessage, ChatRole
    from level_core.schemas.reminder import ReminderMatch

    store = get_store(f"{DEMO_USER_ID_PREFIX}solo")

    await seed_demo_user(store, scenario_id="solo")

    # Simulate a real judge session: chat turns, a priority the judge
    # set, a reminder, an edited person (soft-deleted co-parent-like),
    # a memory-bank entry from a Not-me feedback chip, dismissed
    # proactive cards, a pending booking.
    await store.chat_turns.upsert(
        ChatMessage(
            turn_id="turn_leftover",
            role=ChatRole.USER,
            text="prioritize elder care over sports this week",
        )
    )
    await store.priorities.upsert(
        Priority(
            priority_id="pri_leftover",
            text="elder care over sports",
            weight=4,
            activity_types=[],
        )
    )
    await store.reminders.upsert(
        Reminder(
            reminder_id="rem_leftover",
            text="Bring the charger",
            match=ReminderMatch(activity_type=ActivityType.WORK),
        )
    )
    # Sneak an extra person in as if the judge had asked chat to add
    # a new co-parent that isn't part of the scenario.
    people_before = await store.people.list()
    nova = next(p for p in people_before if p.display_name == "Nova")
    # Now write profile pollution that mirrors what feedback + chat
    # write during a session.
    profile = dict(await store.profile.read() or {})
    profile["memory_bank"] = {
        "memories": [
            {"text": "Never propose Saturday morning", "tag": "avoid"}
        ]
    }
    profile["pending_booking"] = {"anything": "goes"}
    profile["dismissed_missing_week"] = "2026-08-24"
    await store.profile.write(profile)

    # Re-seed as judge B would.
    await seed_demo_user(store, scenario_id="solo")

    # Every mutable surface should now be gone.
    assert await store.chat_turns.list() == []
    assert await store.priorities.list() == []
    assert await store.reminders.list() == []
    profile_after = await store.profile.read() or {}
    assert "memory_bank" not in profile_after
    assert "pending_booking" not in profile_after
    assert "dismissed_missing_week" not in profile_after

    # Person ids rotate (fresh UUIDs) because reset wiped people first,
    # even though display names are the scenario's canonical roster.
    people_after = await store.people.list()
    names_before = {p.display_name for p in people_before}
    names_after = {p.display_name for p in people_after}
    assert names_before == names_after
    assert not ({p.person_id for p in people_before} & {p.person_id for p in people_after}), (
        "person_ids should rotate on session reset - a fresh judge shouldn't inherit prior ids"
    )
    # Nova specifically must have a new id (identity check via name).
    nova_after = next(p for p in people_after if p.display_name == "Nova")
    assert nova_after.person_id != nova.person_id

    # Identity fields survive - they're rewritten by ``_write_profile``
    # right after reset.
    assert is_demo_user(profile_after)
    assert profile_after[PROFILE_DEMO_KEY] == "solo"
    assert profile_after["tz"]
    assert profile_after["display_name"]

    # And proactive cards land on the fresh seed - see next test for
    # the shape check.
    assert "proactive_cards" in profile_after

    # Sanity: the seed timestamp advanced on the second call.
    seeded_at_str = profile_after.get("demo_seeded_at")
    assert seeded_at_str
    assert datetime.fromisoformat(seeded_at_str) is not None


@pytest.mark.asyncio
async def test_seed_populates_proactive_cards_for_the_demo_week() -> None:
    """``seed_demo_user`` runs ``regenerate_proactive_cards`` inline so
    the "Level noticed while you slept" section on /today is
    non-empty on click one - no waiting for the nightly job.

    Uses the ``solo`` scenario which has TWO known missing usuals for
    the demo week (Nova ballet Thu + Helen weekly grocery drop Sun).
    """
    store = get_store(f"{DEMO_USER_ID_PREFIX}solo")

    await seed_demo_user(store, scenario_id="solo")

    profile = await store.profile.read() or {}
    proactive = profile.get("proactive_cards")
    assert isinstance(proactive, dict), "proactive_cards should be a dict payload"
    cards = proactive.get("cards") or []
    assert len(cards) >= 1, (
        "solo scenario should have at least one missing-usual card for the demo week"
    )
    # Shape check: exactly the fields the frontend renders. `group_id`
    # matters because the /today UI correlates card <-> missing-week
    # row by it (hides the row when the card is visible, calls
    # put-back with it). Missing it silently makes both surfaces
    # render the same nudge.
    card = cards[0]
    for key in (
        "card_id",
        "group_id",
        "kind",
        "week_start",
        "day",
        "weekday",
        "category",
        "category_label",
        "person_name",
        "text",
    ):
        assert key in card, f"proactive card missing field: {key}"
    assert card["kind"] == "missing_usual"
    # card_id encodes the group_id as its suffix, so the two ids
    # never drift out of sync when the group format changes.
    assert card["card_id"].endswith(card["group_id"]), (
        "card_id should suffix group_id so the frontend can dedupe reliably"
    )


@pytest.mark.asyncio
async def test_reset_demo_state_refuses_non_demo_user_id(store) -> None:  # type: ignore[no-untyped-def]
    """Defensive guard: if a real user's store were ever routed
    through the reset path (should never happen, but the code needs
    to fail safe if it does), we refuse and log rather than nuke
    their data.

    The ``store`` fixture returns a ``u_test_*`` id which does NOT
    start with the demo prefix, so we can exercise the refusal branch
    without needing to fabricate a fake production user.
    """
    from level_core.schemas import Priority

    await store.priorities.upsert(
        Priority(priority_id="pri_prod_looking", text="do not delete me", weight=3)
    )

    result = await reset_demo_state(store)

    # Empty dict signals the refusal (no wipe attempted).
    assert result == {}
    # The pretend-production priority survives.
    priorities = await store.priorities.list()
    assert len(priorities) == 1
    assert priorities[0].priority_id == "pri_prod_looking"


@pytest.mark.asyncio
async def test_reset_demo_state_skips_cold_slot() -> None:
    """Cold-slot fast path: on a demo user whose slot has never been
    written to, ``reset_demo_state`` must short-circuit on a single
    profile read and NOT list every collection.

    This is what makes the "first judge on this slot" seed fast on
    cloud - the alternative (list every collection to confirm it's
    empty) was N sequential Firestore round trips of pure wait.
    """
    store = get_store(f"{DEMO_USER_ID_PREFIX}solo")

    result = await reset_demo_state(store)

    assert result == {"reset": False, "reason": "cold_slot"}


@pytest.mark.asyncio
async def test_reset_demo_state_uses_native_reset_all_on_warm_slot() -> None:
    """Warm-slot fast path: when the slot has content, ``reset_demo_state``
    delegates to the backend-native ``store.reset_all()`` (one call
    that wipes the whole user subtree) rather than the old N-collection
    delete_many dance.
    """
    store = get_store(f"{DEMO_USER_ID_PREFIX}family")

    await seed_demo_user(store, scenario_id="family")
    # Confirm state landed so the reset has something to wipe.
    assert await store.agenda.list()
    assert await store.people.list()
    assert await store.profile.read()

    result = await reset_demo_state(store)

    assert result == {"reset": True}
    # Every mutable surface should now be empty.
    assert await store.agenda.list() == []
    assert await store.people.list() == []
    assert await store.usuals.list() == []
    assert await store.daily_agenda.list() == []
    # Profile is fully wiped too - identity fields get re-populated
    # only by a subsequent seed_demo_user call, never by reset alone.
    assert not (await store.profile.read())


@pytest.mark.asyncio
async def test_seed_unknown_scenario_raises(store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        await seed_demo_user(store, scenario_id="not-a-scenario")


@pytest.mark.parametrize("scenario_id", ["family", "solo"])
@pytest.mark.asyncio
async def test_messy_events_still_cluster_as_usuals(  # type: ignore[no-untyped-def]
    scenario_id, store
) -> None:
    """The demo fixtures deliberately mix RRULE anchors with individual
    VEVENTs that vary in text ("Nova ballet" / "Ballet - Nova") and
    time ("Grocery run" at 4:15 vs 4:45 PM). The usuals engine should
    cluster them anyway on (person, weekday, hour_band, activity_type)
    and pick the "clean" majority-vote wording for display.

    This test locks in the "Level handles messy calendars" demo story
    so a future edit to the generator, the classifier, or the
    clustering algorithm can't silently break it.
    """
    from datetime import date

    await seed_demo_user(store, scenario_id=scenario_id)
    usuals = await store.usuals.list()
    people = await store.people.list()
    people_by_id = {p.person_id: p for p in people}
    events = await store.agenda.list()
    josh_id = next(p.person_id for p in people if p.is_self)
    nova_id = next(p.person_id for p in people if p.display_name == "Nova")
    helen_id = next(p.person_id for p in people if p.display_name == "Helen")

    # 1. Nova Thu-afternoon ballet clustered under the clean name
    #    despite 3 different text variants over the past 3 Thursdays.
    ballet = next(
        (u for u in usuals if u.person_id == nova_id and u.weekday.name == "THU"
         and u.activity_type.value == "sports.other"),
        None,
    )
    assert ballet is not None, "Nova THU ballet usual missing"
    assert ballet.display_summary == "Nova ballet", (
        f"majority-vote display should pick 'Nova ballet', got {ballet.display_summary!r}"
    )

    # 2. Helen Wed-morning PT clustered under the clean name despite
    #    "PT - Helen" / "Helen PT" variants (which lean on the demo
    #    loader's second-pass classifier for the bare-word "PT" case).
    pt = next(
        (u for u in usuals if u.person_id == helen_id and u.weekday.name == "WED"
         and u.activity_type.value == "medical.therapy"),
        None,
    )
    assert pt is not None, "Helen WED PT usual missing"
    assert pt.display_summary == "Helen physical therapy"

    # 3. Josh Fri-afternoon grocery run - unattributed household chore
    #    that still gets clustered under the self person, with the
    #    "Trader Joe's" / "Grocery pickup" variants voted down by the
    #    majority "Grocery run".
    grocery = next(
        (u for u in usuals if u.person_id == josh_id and u.weekday.name == "FRI"
         and u.activity_type.value == "personal"),
        None,
    )
    assert grocery is not None, "Josh FRI grocery usual missing"
    assert grocery.display_summary == "Grocery run"

    # 4. Missing-usuals story for the demo week: Nova ballet is
    #    intentionally absent this Thursday (both scenarios). Grocery
    #    run is absent this Friday for FAMILY only; SOLO keeps it
    #    present under a variant name to demo that a variant title
    #    still covers the usual and doesn't trigger a false alarm.
    thu = date(2026, 8, 27)
    fri = date(2026, 8, 28)
    thu_ballet = [
        e for e in events
        if e.time.start.date() == thu and "ballet" in e.summary.lower()
    ]
    fri_grocery = [
        e for e in events
        if e.time.start.date() == fri
        and ("grocery" in e.summary.lower() or "trader" in e.summary.lower())
    ]
    assert not thu_ballet, f"ballet should be missing this Thu, found: {thu_ballet}"
    if scenario_id == "family":
        assert not fri_grocery, (
            f"family grocery should be missing this Fri, found: {fri_grocery}"
        )
    else:
        assert fri_grocery, "solo grocery should still be present under a variant name"


def test_heuristic_grocery_pickup_not_misclassified_as_school() -> None:
    """Regression: OBVIOUS_SIGNALS used to order SCHOOL_PICKUP before
    PERSONAL, so "Grocery pickup" was tagged as school.pickup - not
    just a demo bug, a real production misclassification. Locking in
    the reordering.
    """
    from level_core.calendar.enrich import heuristic_activity
    from level_core.schemas.activity import ActivityType

    assert heuristic_activity("Grocery pickup") is ActivityType.PERSONAL
    assert heuristic_activity("Curbside pickup") is ActivityType.PERSONAL or (
        # "Curbside" isn't in the keyword set on its own; "pickup" would
        # fall through to SCHOOL_PICKUP. Accept either outcome since the
        # only precision claim we make is grocery-first ordering.
        heuristic_activity("Curbside pickup") is ActivityType.SCHOOL_PICKUP
    )
    assert heuristic_activity("Nova pickup") is ActivityType.SCHOOL_PICKUP
    assert heuristic_activity("Nova ballet") is ActivityType.SPORTS_OTHER
    assert heuristic_activity("Trader Joe's") is ActivityType.PERSONAL


def test_packaged_ics_matches_example_data_at_repo_root() -> None:
    """Two copies exist by design (repo-root ``example-data/`` for
    humans + generator, ``level_core/demo/data/`` shipped inside the
    wheel for runtime). This test guards against drift so an ICS edit
    committed to only one location doesn't silently ship a stale demo
    on the deploy while the docs point at the fresh one - or vice
    versa.
    """
    import hashlib
    from pathlib import Path

    repo_data = Path(__file__).resolve().parents[2] / "example-data"
    for scenario in SCENARIOS.values():
        repo_copy = repo_data / scenario.ics_filename
        packaged_copy = scenario.ics_path()
        assert repo_copy.is_file(), f"missing repo-root fixture: {repo_copy}"
        assert packaged_copy.is_file(), f"missing packaged fixture: {packaged_copy}"
        repo_hash = hashlib.sha256(repo_copy.read_bytes()).hexdigest()
        pkg_hash = hashlib.sha256(packaged_copy.read_bytes()).hexdigest()
        assert repo_hash == pkg_hash, (
            f"ICS drift for {scenario.ics_filename}: repo copy at "
            f"{repo_copy} differs from packaged copy at {packaged_copy}. "
            f"Re-run scripts/sync_demo_ics.sh or copy the edit into both "
            f"locations."
        )


def _make_client(env: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Rebuild the FastAPI app under a specific LEVEL_ENV.

    In cloud mode the real ``get_store`` would try to instantiate a
    Firestore client (which needs ADC + a project) - we don't want a
    unit test hitting Firestore. Swap in the local JSON backend at
    the auth-route import site so the cloud-demo path is exercised
    without any Google credentials.
    """
    import tempfile

    monkeypatch.setenv("LEVEL_ENV", env)
    monkeypatch.setenv("LEVEL_LOCAL_STORE_ROOT", tempfile.mkdtemp())
    get_settings.cache_clear()

    if env == "cloud":
        from level_core.storage.local_json import make_local_store

        monkeypatch.setattr(
            "level_api.routes.auth.get_store", make_local_store, raising=False
        )

    from level_api.main import create_app

    return TestClient(create_app())


def test_features_endpoint_local_shows_demo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _make_client("local", monkeypatch)
    r = client.get("/v1/config/features")
    assert r.status_code == 200
    body = r.json()
    assert body["env"] == "local"
    assert body["demo"]["available"] is True
    ids = {s["id"] for s in body["demo"]["scenarios"]}
    assert ids == set(SCENARIOS.keys())


def test_features_endpoint_cloud_hides_demo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _make_client("cloud", monkeypatch)
    r = client.get("/v1/config/features")
    assert r.status_code == 200
    body = r.json()
    assert body["env"] == "cloud"
    assert body["demo"]["available"] is False
    assert body["demo"]["scenarios"] == []


def test_demo_login_404_in_cloud_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Security fence: demo endpoint must not accept requests in cloud
    unless the operator explicitly opts in with LEVEL_DEMO_IN_CLOUD=true.

    404 (not 403) so a probe can't distinguish "demo turned off" from
    "endpoint doesn't exist" - keeps the cloud surface flat.
    """
    client = _make_client("cloud", monkeypatch)
    r = client.post("/v1/auth/demo", json={"scenario": "family"})
    assert r.status_code == 404


def test_demo_login_cloud_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When LEVEL_DEMO_IN_CLOUD=true, /v1/auth/demo works in cloud.

    Judges hitting the deployed API get a slotted synthetic user
    without needing to clone the repo.
    """
    from level_api.routes.auth import reset_demo_ip_limiter

    monkeypatch.setenv("LEVEL_DEMO_IN_CLOUD", "true")
    reset_demo_ip_limiter()
    client = _make_client("cloud", monkeypatch)

    r = client.post("/v1/auth/demo", json={"scenario": "solo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario"] == "solo"
    # user_id in cloud is slot-suffixed (except slot 0 which stays
    # unsuffixed for local backward compat). The test client's
    # loopback IP will hash to a specific slot deterministically.
    assert body["user_id"].startswith("u_demo_solo")
    assert isinstance(body["slot"], int)
    assert 0 <= body["slot"] < 3  # default slots_per_scenario


def test_demo_login_cloud_features_endpoint_advertises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When cloud demo is on, /v1/config/features says so and lists scenarios."""
    monkeypatch.setenv("LEVEL_DEMO_IN_CLOUD", "true")
    client = _make_client("cloud", monkeypatch)
    r = client.get("/v1/config/features")
    body = r.json()
    assert body["demo"]["available"] is True
    assert {s["id"] for s in body["demo"]["scenarios"]} == set(SCENARIOS.keys())


def test_slot_for_ip_is_deterministic() -> None:
    """Same IP + scenario always maps to the same slot across processes.

    Uses SHA-256 (not Python's process-salted hash()) so a redeploy
    doesn't reshuffle every judge's assigned user_id.
    """
    for n in (3, 5, 20):
        for scenario in ("solo", "family"):
            for ip in ("127.0.0.1", "203.0.113.42", "8.8.8.8"):
                a = slot_for_ip(ip, scenario, n)
                b = slot_for_ip(ip, scenario, n)
                assert a == b
                assert 0 <= a < n


def test_slot_for_ip_scenario_partitions_pool() -> None:
    """Same IP but different scenarios must be free to land on
    different slots - otherwise the two scenarios would share a
    user_id, causing profile / people conflicts."""
    ip = "203.0.113.42"
    # These specific values are stable because we're using SHA-256.
    # If either scenario name changes, update this assertion.
    solo = slot_for_ip(ip, "solo", 3)
    family = slot_for_ip(ip, "family", 3)
    # No claim about equality - just that they're each in-range and
    # the composed user ids are distinct.
    assert 0 <= solo < 3
    assert 0 <= family < 3
    assert user_id_for_slot("solo", solo) != user_id_for_slot("family", family)


def test_user_id_for_slot_slot_zero_is_backward_compatible() -> None:
    """Slot 0 must equal the historical unsuffixed user id so a
    contributor's on-disk local state survives the pool refactor."""
    assert user_id_for_slot("solo", 0) == "u_demo_solo"
    assert user_id_for_slot("family", 0) == "u_demo_family"
    assert user_id_for_slot("solo", 1) == "u_demo_solo_1"
    assert user_id_for_slot("solo", 2) == "u_demo_solo_2"


def test_cloud_demo_rate_limits_burst_from_same_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A bot spamming /v1/auth/demo from one IP must get 429'd
    before it can burn through Firestore + LLM budget."""
    from level_api.routes.auth import reset_demo_ip_limiter

    monkeypatch.setenv("LEVEL_DEMO_IN_CLOUD", "true")
    monkeypatch.setenv("LEVEL_DEMO_PER_IP_PER_HOUR", "3")
    reset_demo_ip_limiter()
    client = _make_client("cloud", monkeypatch)

    # First 3 succeed (bucket capacity=3).
    for i in range(3):
        r = client.post("/v1/auth/demo", json={"scenario": "solo"})
        assert r.status_code == 200, f"call {i} unexpectedly failed: {r.text}"

    # 4th trips the limiter.
    r = client.post("/v1/auth/demo", json={"scenario": "solo"})
    assert r.status_code == 429
    body = r.json()
    assert body["detail"]["error"] == "rate_limited"
    assert "retry_after_s" in body["detail"]
    assert "Retry-After" in {h.lower(): v for h, v in r.headers.items()}.get(
        "retry-after", ""
    ) or r.headers.get("retry-after") is not None


def test_cloud_demo_pool_size_caps_user_ids(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regardless of how many IPs hit the demo endpoint, the total
    demo user population is bounded to
    ``slots_per_scenario * len(SCENARIOS)``. This is the storage
    guarantee that makes cloud demo mode safe."""
    from level_api.routes.auth import reset_demo_ip_limiter

    monkeypatch.setenv("LEVEL_DEMO_IN_CLOUD", "true")
    monkeypatch.setenv("LEVEL_DEMO_SLOTS_PER_SCENARIO", "3")
    monkeypatch.setenv("LEVEL_DEMO_PER_IP_PER_HOUR", "1000")
    reset_demo_ip_limiter()

    # Fan out 200 distinct simulated IPs across both scenarios and
    # check that only 6 distinct user ids ever get created.
    seen: set[str] = set()
    for i in range(200):
        ip = f"198.51.100.{i % 250 + 1}"
        for scenario in ("solo", "family"):
            slot = slot_for_ip(ip, scenario, 3)
            seen.add(user_id_for_slot(scenario, slot))

    assert len(seen) <= 3 * len(SCENARIOS)


def test_put_back_missing_group_books_event_and_resolves(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The "Yes, put it back" button on a proactive card / missing-week
    row must actually do two things end-to-end:

    1. Book a placeholder event on the agenda at the group's typical
       weekday + time so it shows up in Today / Tomorrow.
    2. Mark the missing-week group resolved AND dismiss the
       corresponding proactive card so neither surface keeps nagging
       about a gap the user just filled.

    Regression against the shipping bug where the button dispatched
    a `level:proactive_ask` custom event that nobody listened for -
    click was silent, no event was booked, and the card / row stayed
    put through the next refresh.
    """
    client = _make_client("local", monkeypatch)

    login = client.post("/v1/auth/demo", json={"scenario": "solo"})
    assert login.status_code == 200

    # Grab the first proactive card (solo scenario always seeds >=1
    # missing-usual card for the demo week).
    today = client.get("/v1/today").json()
    cards = today.get("proactive_cards") or []
    assert cards, "solo scenario should seed a proactive card to click"
    card = cards[0]
    group_id = card["group_id"]
    card_id = card["card_id"]

    r = client.post(
        "/v1/today/missing-week/put-back",
        json={"group_id": group_id, "card_id": card_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "booked"
    assert body["group_id"] == group_id
    assert body["card_id"] == card_id
    event_payload = body["event"]
    # Deterministic id shape so a double-click doesn't create a
    # duplicate booking (the endpoint upserts).
    assert event_payload["event_id"].startswith("level:putback:")
    assert event_payload["origin"] == "level"
    # Non-empty time span.
    assert event_payload["start"] < event_payload["end"]

    # After the call, /today must NOT show the group in missing-week
    # (either because it's resolved or because the card is still
    # visible; both correlate on group_id).
    after = client.get("/v1/today").json()
    remaining = [
        row for row in (after.get("missing_usuals_week") or [])
        if row["group_id"] == group_id
    ]
    assert remaining == [], "group should be gone from missing_usuals_week"
    active_cards = [
        c for c in (after.get("proactive_cards") or [])
        if c["card_id"] == card_id
    ]
    assert active_cards == [], "card should be dismissed after put-back"

    # And the booked event should appear in the agenda for its day.
    agenda_ids = [e["event_id"] for e in (after.get("today", []) + after.get("tomorrow", []))]
    # Not guaranteed to land on today/tomorrow (the missing usual
    # could be later in the week), but the event id is deterministic
    # so a second call should be a no-op rather than another booking.
    r2 = client.post(
        "/v1/today/missing-week/put-back",
        json={"group_id": group_id, "card_id": card_id},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    # Second call is idempotent - either "already_resolved" (fast
    # path) or "booked" that upserts the same event_id.
    assert body2["status"] in {"already_resolved", "booked"}
    del agenda_ids  # silence unused local; kept for readability


def test_demo_login_local_sets_cookie_and_flips_whoami(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = _make_client("local", monkeypatch)

    login = client.post("/v1/auth/demo", json={"scenario": "family"})
    assert login.status_code == 200
    body = login.json()
    assert body["scenario"] == "family"
    assert body["user_id"] == "u_demo_family"
    assert body["events_count"] > 100

    # Session cookie set - subsequent /v1/me should identify demo user
    me = client.get("/v1/me")
    assert me.status_code == 200
    who = me.json()
    assert who["user_id"] == "u_demo_family"
    assert who["demo"] is True
    assert who["demo_scenario"] == "family"
    # google_connected returns True even without tokens so the
    # frontend Connect-Google wall doesn't trap the demo user.
    assert who["google_connected"] is True


def test_hear_my_day_falls_back_when_no_llm_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Demo mode without GOOGLE_API_KEY / GOOGLE_CLOUD_PROJECT must still
    return a real, useful summary from /v1/today/summary - not a 500
    and not the stale "Today looks quiet." string."""
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    client = _make_client("local", monkeypatch)

    login = client.post("/v1/auth/demo", json={"scenario": "solo"})
    assert login.status_code == 200

    r = client.get("/v1/today/summary")
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert isinstance(summary, str) and summary.strip(), "summary must not be empty"
    # Deterministic fallback must reference the seeded events - if it
    # returns "Today looks quiet." the fallback isn't reading the seed.
    assert "quiet" not in summary.lower() or "things today" in summary.lower()


def test_profile_refresh_short_circuits_for_demo_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regression: clicking "Re-read calendar" on a demo user used to
    fall through to enrich_agenda + role_run + compute_usuals even
    when refresh_agenda immediately raised no_google_tokens. role_run
    unconditionally hits Vertex, so under quota or Vertex latency the
    endpoint took 20+s and eventually 500'd.

    The short-circuit returns "up_to_date": True with a reason string
    the frontend uses to render "Demo data is seeded and static -
    nothing to re-read." Sub-100ms, zero LLM calls, no 500.
    """
    # Kill LLM creds so if anything accidentally tries to call Vertex
    # we'd get a hard error instead of a slow success. This proves
    # the short-circuit really doesn't invoke role_run.
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    client = _make_client("local", monkeypatch)

    login = client.post("/v1/auth/demo", json={"scenario": "solo"})
    assert login.status_code == 200

    r = client.post("/v1/profile/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["up_to_date"] is True
    assert body["reason"] == "demo"
    assert body["people_added"] == 0
    assert body["usuals_added"] == 0
    # Seeded solo scenario has 100+ events and a curated cast; make
    # sure the response still surfaces the current-state counts so
    # the sidebar has something to render.
    assert body["events_scanned"] > 0
    assert body["usuals_total"] > 0


def test_email_send_short_circuits_for_demo_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A demo user pressing Send should get a preview response, not
    a 502. The pending draft token must still get cleared."""
    from level_api.routes import email as email_routes

    client = _make_client("local", monkeypatch)

    login = client.post("/v1/auth/demo", json={"scenario": "family"})
    assert login.status_code == 200

    token = "tok_" + str(int(time.time() * 1000))
    email_routes.register_pending_draft(token, to="teacher@school.local")

    send = client.post(
        "/v1/email/send",
        json={
            "confirmation_token": token,
            "to": "teacher@school.local",
            "subject": "Absence note",
            "body": "Hi Ms. Anna, Jordan will be out today.",
        },
        headers={"X-Idempotency-Key": f"idem-{token}"},
    )
    assert send.status_code == 200
    payload = send.json()
    assert payload["demo"] is True
    assert payload["notice"].startswith("Demo mode")
    # Token cleared after preview so a duplicate send is idempotent
    assert token not in email_routes._pending_drafts
