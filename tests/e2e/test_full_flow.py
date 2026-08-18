"""End-to-end: session cookie -> chat priority -> chat reminder -> Keep/Not me -> gate.

Uses FastAPI in-process via ASGITransport. Gemini calls are faked.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from level_core.agents.fakes import register_fake
from level_core.auth.sessions import build_session_cookie
from level_core.schemas import CareRelation, UserSession
from level_core.storage.care_store import propose_person, set_person_status
from level_core.storage.factory import get_store


def _cookie_headers(user_id: str) -> dict[str, str]:
    session = UserSession(user_id=user_id)
    cookie = build_session_cookie(session)
    return {"cookie": f"level_session={cookie}"}


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_chat_extracts_priority_and_reminder(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    from level_api.main import create_app

    user_id = f"u_e2e_{uuid.uuid4().hex[:6]}"
    headers = _cookie_headers(user_id)

    store = get_store(user_id)
    alpha = await propose_person(store, display_name="Alpha", relation=CareRelation.CHILD)
    await set_person_status(store, alpha.person_id, "kept")

    register_fake(
        "ChatRouterAgent",
        {"path": "profile", "intent": "priority", "source_span": "never miss elder therapy", "confidence": 0.9},
    )
    register_fake(
        "PriorityAgent",
        {
            "priority": {
                "text": "Never miss elder therapy",
                "weight": 5,
                "activity_types": ["medical.therapy"],
                "source_span": "never miss elder therapy",
            }
        },
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/v1/chat",
            json={"message": "please make sure I never miss elder therapy"},
            headers=headers,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "profile"
    assert body["intent"] == "priority"
    assert "priority_id" in body

    prios = await store.priorities.list()
    assert any(p.text == "Never miss elder therapy" for p in prios)

    register_fake(
        "ChatRouterAgent",
        {"path": "reminder", "intent": "add_reminder", "source_span": "soccer shoes", "confidence": 0.9},
    )
    register_fake(
        "ReminderAgent",
        {
            "reminder": {
                "text": "Bring soccer shoes",
                "person_display_name": "Alpha",
                "activity_type": "sports.soccer",
                "lead_minutes": 60,
                "source_span": "soccer shoes",
            }
        },
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/v1/chat",
            json={"message": "I keep forgetting Alpha's soccer shoes"},
            headers=headers,
        )
    assert r.status_code == 200
    reminders = await store.reminders.list()
    assert any(r_.text == "Bring soccer shoes" for r_ in reminders)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_not_me_records_negative(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    from level_api.main import create_app

    user_id = f"u_e2e_{uuid.uuid4().hex[:6]}"
    store = get_store(user_id)
    person = await propose_person(store, display_name="Ghost", relation=CareRelation.OTHER)
    headers = _cookie_headers(user_id)

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/v1/profile/keep_not_me",
            json={"entity": "person", "id": person.person_id, "status": "not_me"},
            headers=headers,
        )
    assert r.status_code == 200

    negatives = await store.negatives.list()
    assert any(n.value == "Ghost" for n in negatives)
