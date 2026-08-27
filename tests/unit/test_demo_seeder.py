"""Demo-mode seeder + auth-bypass endpoint.

Guarantees the OAuth-less landing experience judges will click:

- ``seed_demo_user`` populates people, agenda, daily_agenda, usuals,
  and the demo-profile marker.
- Re-seeding is idempotent (same event/people counts, no doubling).
- ``POST /v1/auth/demo`` returns 404 in cloud mode - critical: this
  is the security fence between "safe local dev" and "authenticate
  yourself as a synthetic user against the deployed API".
- ``GET /v1/config/features`` reflects the env correctly.
- Gmail send short-circuits to a preview response for demo users so
  the demo flow never 502s on send.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from level_core.config import get_settings
from level_core.demo.scenarios import SCENARIOS
from level_core.demo.seeder import (
    PROFILE_DEMO_KEY,
    is_demo_user,
    seed_demo_user,
)


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
async def test_seed_is_idempotent(store) -> None:  # type: ignore[no-untyped-def]
    first = await seed_demo_user(store, scenario_id="family")
    events_before = await store.agenda.list()
    people_before = await store.people.list()

    second = await seed_demo_user(store, scenario_id="family")
    events_after = await store.agenda.list()
    people_after = await store.people.list()

    assert first.events_count == second.events_count
    assert len(events_after) == len(events_before)
    assert {p.person_id for p in people_after} == {p.person_id for p in people_before}


@pytest.mark.asyncio
async def test_seed_unknown_scenario_raises(store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        await seed_demo_user(store, scenario_id="not-a-scenario")


def _make_client(env: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Rebuild the FastAPI app under a specific LEVEL_ENV."""
    monkeypatch.setenv("LEVEL_ENV", env)
    get_settings.cache_clear()
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


def test_demo_login_404_in_cloud(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Security fence: demo endpoint must not accept requests in cloud."""
    client = _make_client("cloud", monkeypatch)
    r = client.post("/v1/auth/demo", json={"scenario": "family"})
    assert r.status_code == 404


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
