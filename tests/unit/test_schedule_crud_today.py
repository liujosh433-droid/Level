"""Schedule, contacts, reminders, sources, today, calendar routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from level_core.auth.sessions import build_session_cookie
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CareRelation,
    EventTime,
    UserSession,
)
from level_core.storage.care_store import add_reminder, propose_person
from level_core.storage.factory import get_store

from level_api.main import create_app
from level_api.routes import schedule as schedule_routes


def _headers(user_id: str) -> dict[str, str]:
    cookie = build_session_cookie(UserSession(user_id=user_id))
    return {"cookie": f"level_session={cookie}"}


@pytest.fixture
async def client() -> AsyncClient:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_schedule_state() -> None:
    schedule_routes._pending_bookings.clear()
    schedule_routes._booked_idempotency.clear()
    yield
    schedule_routes._pending_bookings.clear()
    schedule_routes._booked_idempotency.clear()


def test_schedule_slot_helpers() -> None:
    now = datetime.now(UTC)
    expired = now - timedelta(minutes=1)
    schedule_routes._pending_bookings["old"] = {"_expires_at": expired, "top": []}
    schedule_routes._pending_bookings["fresh"] = {
        "_expires_at": now + timedelta(minutes=5),
        "top": [],
    }
    schedule_routes._prune_pending(now)
    assert "old" not in schedule_routes._pending_bookings
    assert "fresh" in schedule_routes._pending_bookings

    schedule_routes._booked_idempotency["k"] = now.timestamp() - 700
    schedule_routes._booked_idempotency["live"] = now.timestamp()
    schedule_routes._prune_idempotency(now.timestamp())
    assert "k" not in schedule_routes._booked_idempotency
    assert "live" in schedule_routes._booked_idempotency

    slot = {"start_iso": "2026-08-20T16:00:00+00:00", "end_iso": "2026-08-20T17:00:00+00:00"}
    assert schedule_routes._slot_matches(slot, "2026-08-20T16:00:00Z", "2026-08-20T17:00:00Z")
    assert not schedule_routes._slot_matches(slot, "2026-08-20T18:00:00Z", "2026-08-20T19:00:00Z")
    assert not schedule_routes._slot_matches({}, "bad", "bad")

    assert (
        schedule_routes._load_persisted_pending({}, "tok", now) is None
    )
    assert (
        schedule_routes._load_persisted_pending(
            {"pending_schedule_find": {"confirmation_token": "other"}},
            "tok",
            now,
        )
        is None
    )
    expired_rec = {
        "confirmation_token": "tok",
        "expires_at": (now - timedelta(minutes=1)).isoformat(),
        "top": [],
    }
    assert (
        schedule_routes._load_persisted_pending(
            {"pending_schedule_find": expired_rec}, "tok", now
        )
        is None
    )
    live_rec = {
        "confirmation_token": "tok",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "top": [{"start_iso": "x"}],
    }
    loaded = schedule_routes._load_persisted_pending(
        {"pending_schedule_find": live_rec}, "tok", now
    )
    assert loaded is not None
    # Unparseable expiry is treated as still valid.
    weird = {"confirmation_token": "tok", "expires_at": "not-a-date", "top": []}
    assert (
        schedule_routes._load_persisted_pending(
            {"pending_schedule_find": weird}, "tok", now
        )
        is not None
    )


@pytest.mark.asyncio
async def test_schedule_find_and_book(
    client: AsyncClient, user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _headers(user_id)

    found = await client.post(
        "/v1/schedule/find",
        headers=headers,
        json={"activity_type": "personal", "duration_minutes": 30, "within_days": 3},
    )
    assert found.status_code == 200, found.text
    token = found.json()["confirmation_token"]
    slots = found.json()["slots"]
    assert token
    if not slots:
        # Empty calendar can still yield candidate windows; if scoring
        # returns none, book must reject an invented slot.
        bad = await client.post(
            "/v1/schedule/book",
            headers=headers,
            json={
                "confirmation_token": token,
                "summary": "Errand",
                "start_iso": "2026-08-20T16:00:00+00:00",
                "end_iso": "2026-08-20T17:00:00+00:00",
                "activity_type": "personal",
            },
        )
        assert bad.status_code == 400
        return

    slot = slots[0]

    @dataclass
    class _Booked:
        event_id: str
        html_link: str

    async def _fake_book(*_a, **_k):  # type: ignore[no-untyped-def]
        return _Booked(event_id="evt_booked", html_link="https://example.test/e")

    monkeypatch.setattr(schedule_routes, "book_event", _fake_book)

    unknown = await client.post(
        "/v1/schedule/book",
        headers=headers,
        json={
            "confirmation_token": "not-a-real-token-xx",
            "summary": "Errand",
            "start_iso": slot["start_iso"],
            "end_iso": slot["end_iso"],
            "activity_type": "personal",
        },
    )
    assert unknown.status_code == 400

    mismatch = await client.post(
        "/v1/schedule/book",
        headers=headers,
        json={
            "confirmation_token": token,
            "summary": "Errand",
            "start_iso": "2099-01-01T00:00:00+00:00",
            "end_iso": "2099-01-01T01:00:00+00:00",
            "activity_type": "personal",
        },
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "slot_not_offered"

    booked = await client.post(
        "/v1/schedule/book",
        headers={**headers, "x-idempotency-key": "idem-1"},
        json={
            "confirmation_token": token,
            "summary": "Errand",
            "start_iso": slot["start_iso"],
            "end_iso": slot["end_iso"],
            "activity_type": "personal",
        },
    )
    assert booked.status_code == 200, booked.text
    assert booked.json()["event_id"] == "evt_booked"

    dup = await client.post(
        "/v1/schedule/book",
        headers={**headers, "x-idempotency-key": "idem-1"},
        json={
            "confirmation_token": token,
            "summary": "Errand",
            "start_iso": slot["start_iso"],
            "end_iso": slot["end_iso"],
            "activity_type": "personal",
        },
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_contacts_reminders_sources(client: AsyncClient, user_id: str) -> None:
    headers = _headers(user_id)
    store = get_store(user_id)
    person = await propose_person(store, display_name="Nova", relation=CareRelation.CHILD)

    listed = await client.get("/v1/contacts", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["contacts"] == []

    created = await client.post(
        "/v1/contacts",
        headers=headers,
        json={
            "person_id": person.person_id,
            "kind": "teacher",
            "name": "Ms. Anna",
            "email": "anna@school.example.com",
        },
    )
    assert created.status_code == 200
    cid = created.json()["contact_id"]

    deleted = await client.delete(f"/v1/contacts/{cid}", headers=headers)
    assert deleted.status_code == 200
    missing = await client.delete("/v1/contacts/nope", headers=headers)
    assert missing.status_code == 404

    rem = await add_reminder(
        store,
        text="Bring ballet shoes",
        person_id=person.person_id,
        activity_type=ActivityType.SPORTS_OTHER,
    )
    rems = await client.get("/v1/reminders", headers=headers)
    assert rems.status_code == 200
    assert any(r["reminder_id"] == rem.reminder_id for r in rems.json()["reminders"])

    dismissed = await client.post(
        f"/v1/reminders/{rem.reminder_id}/dismiss", headers=headers
    )
    assert dismissed.status_code == 200
    gone = await client.post("/v1/reminders/nope/dismiss", headers=headers)
    assert gone.status_code == 404

    status = await client.get("/v1/sources/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["google_connected"] is False
    assert status.json()["ai_calls_today"] == 0

    window = await client.post(
        "/v1/sources/window",
        headers=headers,
        json={"days_back": 14, "days_forward": 7},
    )
    assert window.status_code == 200
    assert window.json()["calendar_window_days_back"] == 14


@pytest.mark.asyncio
async def test_today_and_calendar_routes(client: AsyncClient, user_id: str) -> None:
    headers = _headers(user_id)
    store = get_store(user_id)
    now = datetime.now(UTC)
    await store.agenda.upsert(
        CachedEvent(
            event_id="e_today",
            calendar_id="primary",
            summary="Work",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            activity_type=ActivityType.WORK,
        )
    )
    today = await client.get("/v1/today", headers=headers)
    assert today.status_code == 200, today.text
    body = today.json()
    assert "today" in body
    assert "week_load" in body

    tz_q = await client.get("/v1/today?tz=America/Los_Angeles", headers=headers)
    assert tz_q.status_code == 200

    dismiss_week = await client.post("/v1/today/missing-week/dismiss", headers=headers)
    assert dismiss_week.status_code == 200
    assert dismiss_week.json()["status"] == "dismissed"

    resolve = await client.post(
        "/v1/today/missing-week/resolve",
        headers=headers,
        json={"group_id": "3:sports"},
    )
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "resolved"

    card = await client.post(
        "/v1/today/proactive-cards/dismiss",
        headers=headers,
        json={"card_id": "card_1"},
    )
    assert card.status_code == 200

    put_back = await client.post(
        "/v1/today/missing-week/put-back",
        headers=headers,
        json={"group_id": "3:sports", "card_id": "card_2"},
    )
    assert put_back.status_code == 200
    assert put_back.json()["status"] == "already_resolved"

    learned = await client.get("/v1/today/learned", headers=headers)
    assert learned.status_code == 200
    assert "recent" in learned.json()

    cal = await client.get("/v1/calendar/summary", headers=headers)
    assert cal.status_code == 200
    assert cal.json()["total"] >= 1

    from level_core.agents.fakes import register_fake

    register_fake(
        "ActivityAgent",
        {
            "classifications": [
                {
                    "event_id": "e_today",
                    "activity_type": "work",
                    "source_span": "Work",
                }
            ]
        },
    )
    reclass = await client.post("/v1/calendar/reclassify", headers=headers)
    assert reclass.status_code == 200
    assert "by_activity" in reclass.json()

    sync = await client.post(
        "/v1/calendar/webhook",
        headers={"x-goog-resource-state": "sync"},
    )
    assert sync.status_code == 204

    missing_uid = await client.post(
        "/v1/calendar/webhook",
        headers={"x-goog-resource-state": "exists"},
    )
    assert missing_uid.status_code == 400

    bad_channel = await client.post(
        f"/v1/calendar/webhook?uid={user_id}",
        headers={
            "x-goog-resource-state": "exists",
            "x-goog-channel-id": "c1",
            "x-goog-channel-token": "nope",
        },
    )
    assert bad_channel.status_code == 401
