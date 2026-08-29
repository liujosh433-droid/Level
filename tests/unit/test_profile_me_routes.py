"""Profile + /me routes: local store, no Google."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from level_core.agents.fakes import register_fake
from level_core.auth.sessions import build_session_cookie
from level_core.calendar.sync import RefreshResult
from level_core.schemas import (
    ActivityType,
    AiAuditEntry,
    CachedEvent,
    CareRelation,
    ChatMessage,
    ChatRole,
    Contact,
    ContactKind,
    EventTime,
    HourBand,
    NegativeAgent,
    NegativeFeedback,
    Priority,
    Reminder,
    ReminderMatch,
    Usual,
    UsualStatus,
    UserSession,
    Weekday,
)
from level_core.storage.care_store import propose_person, set_person_status
from level_core.storage.factory import get_store

from level_api.main import create_app
from level_api.routes import profile as profile_routes
from level_api.routes.profile import _fmt_hm


def _headers(user_id: str) -> dict[str, str]:
    cookie = build_session_cookie(UserSession(user_id=user_id))
    return {"cookie": f"level_session={cookie}"}


@pytest.fixture
async def client() -> AsyncClient:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def test_fmt_hm_edges() -> None:
    assert _fmt_hm(0) == "12am"
    assert _fmt_hm(9 * 60) == "9am"
    assert _fmt_hm(9 * 60 + 15) == "9:15am"
    assert _fmt_hm(15 * 60) == "3pm"
    assert _fmt_hm(24 * 60) == "11:59pm"


@pytest.mark.asyncio
async def test_profile_get_refresh_people_priorities(client: AsyncClient, user_id: str) -> None:
    store = get_store(user_id)
    person = await propose_person(store, display_name="Nova", relation=CareRelation.CHILD)
    await set_person_status(store, person.person_id, "kept")
    now = datetime.now(UTC) - timedelta(days=3)
    ev = CachedEvent(
        event_id="e_ballet",
        calendar_id="primary",
        summary="Nova ballet",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        activity_type=ActivityType.SPORTS_OTHER,
        matched_person_ids=[person.person_id],
    )
    await store.agenda.upsert(ev)
    usual = Usual(
        usual_id=Usual.compose_id(person.person_id, Weekday.THU, HourBand.AFTERNOON),
        person_id=person.person_id,
        weekday=Weekday.THU,
        hour_band=HourBand.AFTERNOON,
        activity_type=ActivityType.SPORTS_OTHER,
        display_summary="Nova ballet",
        source_event_uids=["e_ballet"],
        confidence=0.8,
        status=UsualStatus.PROPOSED,
    )
    await store.usuals.upsert(usual)
    prio = Priority(priority_id="prio_1", text="Never miss ballet", weight=5)
    await store.priorities.upsert(prio)

    headers = _headers(user_id)
    r = await client.get("/v1/profile", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(p["display_name"] == "Nova" for p in body["people"])
    assert body["usuals"]
    assert body["usuals"][0]["person_name"] == "Nova"
    assert body["usuals"][0]["typical_start"]
    assert "weeks_observed" in body["usuals_meta"]

    # No Google tokens → short-circuit, no LLM.
    refresh = await client.post("/v1/profile/refresh", headers=headers)
    assert refresh.status_code == 200
    assert refresh.json()["reason"] == "no_google_tokens"
    assert refresh.json()["up_to_date"] is True

    add_p = await client.post(
        "/v1/profile/people",
        headers=headers,
        json={"display_name": "Theo", "relation": "child"},
    )
    assert add_p.status_code == 200
    assert add_p.json()["display_name"] == "Theo"

    add_pr = await client.post(
        "/v1/profile/priorities",
        headers=headers,
        json={"text": "Protect bedtime", "weight": 4, "activity_types": []},
    )
    assert add_pr.status_code == 200
    new_id = add_pr.json()["priority_id"]

    keep = await client.post(
        "/v1/profile/keep_not_me",
        headers=headers,
        json={"entity": "person", "id": person.person_id, "status": "kept"},
    )
    assert keep.status_code == 200
    assert keep.json()["ok"] is True

    not_me_u = await client.post(
        "/v1/profile/keep_not_me",
        headers=headers,
        json={"entity": "usual", "id": usual.usual_id, "status": "not_me"},
    )
    assert not_me_u.status_code == 200

    not_me_p = await client.post(
        "/v1/profile/keep_not_me",
        headers=headers,
        json={"entity": "priority", "id": "prio_1", "status": "not_me"},
    )
    assert not_me_p.status_code == 200

    missing = await client.post(
        "/v1/profile/keep_not_me",
        headers=headers,
        json={"entity": "priority", "id": "nope", "status": "not_me"},
    )
    assert missing.status_code == 404

    bad = await client.post(
        "/v1/profile/keep_not_me",
        headers=headers,
        json={"entity": "nope", "id": "x", "status": "kept"},
    )
    assert bad.status_code == 400

    deleted = await client.delete(f"/v1/profile/priorities/{new_id}", headers=headers)
    assert deleted.status_code == 200
    missing_del = await client.delete("/v1/profile/priorities/nope", headers=headers)
    assert missing_del.status_code == 404

    register_fake("UsualAgent", {"picks": []})
    dis = await client.post("/v1/profile/disambiguate", headers=headers, json=[])
    assert dis.status_code == 200
    assert dis.json()["picks"] == []


@pytest.mark.asyncio
async def test_me_whoami_patch_export_delete(client: AsyncClient, user_id: str) -> None:
    headers = _headers(user_id)
    who = await client.get("/v1/me", headers=headers)
    assert who.status_code == 200
    assert who.json()["user_id"] == user_id
    assert who.json()["google_connected"] is False

    empty_patch = await client.patch("/v1/me", headers=headers, json={})
    assert empty_patch.status_code == 422

    bad_tz = await client.patch("/v1/me", headers=headers, json={"tz": "Not/AZone"})
    assert bad_tz.status_code == 400

    tz_only = await client.patch(
        "/v1/me", headers=headers, json={"tz": "America/Los_Angeles"}
    )
    assert tz_only.status_code == 200
    assert tz_only.json()["tz"] == "America/Los_Angeles"

    named = await client.patch("/v1/me", headers=headers, json={"display_name": "Alex"})
    assert named.status_code == 200
    assert named.json()["display_name"] == "Alex"

    # Second patch updates the existing self person.
    renamed = await client.patch("/v1/me", headers=headers, json={"display_name": "Sam"})
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Sam"

    exported = await client.get("/v1/me/export", headers=headers)
    assert exported.status_code == 200
    assert "people" in exported.json()
    assert any(p["is_self"] for p in exported.json()["people"])

    store = get_store(user_id)
    now = datetime.now(UTC)
    await store.usuals.upsert(
        Usual(
            usual_id="u:x:0:morning",
            person_id="p_x",
            weekday=Weekday.MON,
            hour_band=HourBand.MORNING,
            activity_type=ActivityType.WORK,
            display_summary="Work",
        )
    )
    await store.priorities.upsert(Priority(priority_id="prio_wipe", text="x"))
    await store.reminders.upsert(
        Reminder(
            reminder_id="rem_wipe",
            text="x",
            match=ReminderMatch(person_id=None, activity_type=ActivityType.WORK),
        )
    )
    await store.contacts.upsert(
        Contact(
            contact_id="con_wipe",
            person_id="p_x",
            kind=ContactKind.OTHER,
            name="Pat",
        )
    )
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_wipe",
            calendar_id="primary",
            summary="Work",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        )
    )
    await store.chat_turns.upsert(
        ChatMessage(turn_id="t1", role=ChatRole.USER, text="hi")
    )
    await store.ai_audit.upsert(
        AiAuditEntry(
            audit_id="aud_wipe",
            agent="ChatRouterAgent",
            model="flash",
            prompt_hash="x",
            response={},
        )
    )
    await store.negatives.upsert(
        NegativeFeedback(
            negative_id="neg_wipe",
            agent=NegativeAgent.ROLE,
            field="display_name",
            value="Grocery",
        )
    )

    wiped = await client.delete("/v1/me", headers=headers)
    assert wiped.status_code == 200
    assert wiped.json()["status"] == "wiped"
    assert await store.people.list() == []
    assert await store.usuals.list() == []


@pytest.mark.asyncio
async def test_profile_refresh_mocked_google(
    client: AsyncClient, user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = get_store(user_id)
    await store.tokens.write({"access_token": "tok"})
    now = datetime.now(UTC) - timedelta(days=2)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_work",
            calendar_id="primary",
            summary="Work",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            activity_type=ActivityType.WORK,
        )
    )
    await store.calendar_sync.write({"last_role_run_fingerprint": "fp_same"})

    async def _same(_store, **_k):  # type: ignore[no-untyped-def]
        return RefreshResult(
            added=0,
            updated=0,
            removed=0,
            total_cached=1,
            fingerprint="fp_same",
            fingerprint_changed=False,
            calendars=["primary"],
        )

    monkeypatch.setattr(profile_routes, "refresh_agenda", _same)
    headers = _headers(user_id)
    short = await client.post("/v1/profile/refresh", headers=headers)
    assert short.status_code == 200
    assert short.json()["up_to_date"] is True

    async def _changed(_store, **_k):  # type: ignore[no-untyped-def]
        return RefreshResult(
            added=1,
            updated=0,
            removed=0,
            total_cached=1,
            fingerprint="fp_new",
            fingerprint_changed=True,
            calendars=["primary"],
        )

    monkeypatch.setattr(profile_routes, "refresh_agenda", _changed)
    register_fake(
        "RoleAgent",
        {
            "people": [
                {
                    "display_name": "Nova",
                    "relation": "child",
                    "aliases": [],
                    "is_self": False,
                    "source_span": "Nova",
                }
            ]
        },
    )
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_nova",
            calendar_id="primary",
            summary="Nova soccer Thursday",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            activity_type=ActivityType.SPORTS_SOCCER,
            attendee_tokens=["Nova"],
        )
    )
    full = await client.post("/v1/profile/refresh", headers=headers)
    assert full.status_code == 200
    assert full.json()["up_to_date"] is False

    async def _boom(_store, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("google down")

    monkeypatch.setattr(profile_routes, "refresh_agenda", _boom)
    errored = await client.post("/v1/profile/refresh", headers=headers)
    assert errored.status_code == 200
    assert errored.json().get("refresh_error")


@pytest.mark.asyncio
async def test_admin_read_endpoints(client: AsyncClient, user_id: str) -> None:
    headers = _headers(user_id)
    store = get_store(user_id)
    await store.ai_audit.upsert(
        AiAuditEntry(
            audit_id="aud_admin",
            agent="ChatRouterAgent",
            model="flash",
            prompt_hash="x",
            response={"path": "general"},
            trace_id="tr1",
        )
    )
    for path in (
        "/v1/admin/agents",
        "/v1/admin/intents",
        "/v1/admin/router_cache",
        "/v1/admin/rate_limit",
        "/v1/admin/calendar_circuit",
        "/v1/admin/traces",
        "/v1/admin/store",
    ):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, (path, r.text)

    from level_core.agents.identity import sign as sign_identity

    ident = sign_identity(name="ChatRouterAgent", version="1", prompt_hash="abc")
    ok = await client.get(
        "/v1/admin/agents/verify", headers=headers, params={"token": ident.token}
    )
    assert ok.status_code == 200
    assert ok.json()["verified"] is True
    bad = await client.get(
        "/v1/admin/agents/verify", headers=headers, params={"token": "nope"}
    )
    assert bad.json()["verified"] is False
