"""Email send: confirmation token survives instance churn + Gmail retries.

The old failure mode: `_pending_drafts` was an in-memory dict on the
API process. A Cloud Run instance replacement, a multi-pod routing
decision that landed /email/send on a different pod than /email/draft,
or plain restart -> user saw "This draft expired. Ask me to write it
again." while the draft was still on screen.

We now persist the confirmation token in Firestore under
`profile.pending_email_draft` (with a 60-min TTL) and treat that as
the authoritative source of truth for `/email/send`. The in-memory
dict is still populated for the fast local path but is NOT required.

Second bug fixed here: the token used to be `pop()`'d BEFORE Gmail
was called. Any Gmail failure invalidated the token so the caregiver
couldn't retry. Now the token is only dropped after Gmail confirms.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from level_core.auth.sessions import build_session_cookie
from level_core.email.gmail_client import SentEmail
from level_core.schemas import UserSession
from level_core.storage.factory import get_store

from level_api.routes import email as email_route


def _cookie_headers(user_id: str) -> dict[str, str]:
    session = UserSession(user_id=user_id)
    cookie = build_session_cookie(session)
    return {"cookie": f"level_session={cookie}"}


def _seed_pending_draft(
    profile: dict[str, object], token: str, *, ttl_minutes: int = 60
) -> dict[str, object]:
    profile["pending_email_draft"] = {
        "confirmation_token": token,
        "to": "teacher@example.com",
        "subject": "Absence note for Jordan - August 27",
        "contact_name": "Ms. Anna",
        "person_name": "Jordan",
        "kind": "teacher",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    return profile


@pytest.fixture(autouse=True)
def _reset_send_state() -> None:
    email_route._pending_drafts.clear()
    email_route._sent_idempotency.clear()


@pytest.mark.asyncio
async def test_send_works_when_only_firestore_has_token(
    tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Simulate a Cloud Run instance replacement between draft and send:
    the in-memory dict is empty, but Firestore still has the draft."""
    monkeypatch.chdir(tmp_path)
    user_id = f"u_{uuid.uuid4().hex[:6]}"
    store = get_store(user_id)
    token = f"ct_{uuid.uuid4().hex[:8]}"

    profile = dict(await store.profile.read() or {})
    _seed_pending_draft(profile, token)
    await store.profile.write(profile)
    # Deliberately DO NOT register_pending_draft(); this is the whole
    # point of the test - the process that will handle /send is a
    # different one than the process that handled /draft.

    fake_send = AsyncMock(return_value=SentEmail(message_id="m1", thread_id="t1"))
    monkeypatch.setattr(email_route, "send_email", fake_send)

    from level_api.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "Absence note",
                "body": "Jordan is out sick tomorrow.",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "k1"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["message_id"] == "m1"
    fake_send.assert_awaited_once()

    # Token consumed: subsequent send with same token should 400.
    profile_after = await store.profile.read()
    assert (profile_after or {}).get("pending_email_draft") is None


@pytest.mark.asyncio
async def test_expired_firestore_draft_rejected(
    tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """If the persisted draft has expired past its TTL, we 400 (the
    intended 'draft expired' UX, not a false expiry from lost state)."""
    monkeypatch.chdir(tmp_path)
    user_id = f"u_{uuid.uuid4().hex[:6]}"
    store = get_store(user_id)
    token = f"ct_{uuid.uuid4().hex[:8]}"

    profile = dict(await store.profile.read() or {})
    _seed_pending_draft(profile, token, ttl_minutes=-1)  # expired
    await store.profile.write(profile)

    fake_send = AsyncMock(return_value=SentEmail(message_id="m1", thread_id="t1"))
    monkeypatch.setattr(email_route, "send_email", fake_send)

    from level_api.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "s",
                "body": "b",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "k2"},
        )
    assert r.status_code == 400
    assert r.json()["detail"] == "unknown_confirmation_token"
    fake_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gmail_failure_keeps_token_valid_for_retry(
    tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """If Gmail fails, the confirmation token must remain valid so the
    caregiver's retry (fresh idempotency key) succeeds instead of
    hitting a false 'draft expired' 400."""
    monkeypatch.chdir(tmp_path)
    user_id = f"u_{uuid.uuid4().hex[:6]}"
    store = get_store(user_id)
    token = f"ct_{uuid.uuid4().hex[:8]}"

    profile = dict(await store.profile.read() or {})
    _seed_pending_draft(profile, token)
    await store.profile.write(profile)
    email_route.register_pending_draft(token, "teacher@example.com")

    call_count = {"n": 0}

    async def flaky_send(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("gmail transient 500")
        return SentEmail(message_id="m1", thread_id="t1")

    monkeypatch.setattr(email_route, "send_email", flaky_send)

    from level_api.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # First send: Gmail fails - we bubble it up.
        r1 = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "s",
                "body": "b",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "k-first"},
        )
        assert r1.status_code >= 500 or r1.status_code == 400
        # Token MUST still be valid (this is the whole fix).
        profile_after_fail = await store.profile.read()
        assert (profile_after_fail or {}).get("pending_email_draft") is not None
        assert token in email_route._pending_drafts

        # Retry with a fresh idempotency key: should succeed.
        r2 = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "s",
                "body": "b",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "k-retry"},
        )
    assert r2.status_code == 200, r2.text
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_returns_409(
    tmp_path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    """Same idempotency key twice = client bug or double-click; block it."""
    monkeypatch.chdir(tmp_path)
    user_id = f"u_{uuid.uuid4().hex[:6]}"
    store = get_store(user_id)
    token = f"ct_{uuid.uuid4().hex[:8]}"

    profile = dict(await store.profile.read() or {})
    _seed_pending_draft(profile, token)
    await store.profile.write(profile)
    email_route.register_pending_draft(token, "teacher@example.com")

    fake_send = AsyncMock(return_value=SentEmail(message_id="m1", thread_id="t1"))
    monkeypatch.setattr(email_route, "send_email", fake_send)

    from level_api.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "s",
                "body": "b",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "dup"},
        )
        assert r1.status_code == 200
        # Second request with the SAME idempotency key: 409.
        r2 = await c.post(
            "/v1/email/send",
            json={
                "confirmation_token": token,
                "to": "teacher@example.com",
                "subject": "s",
                "body": "b",
            },
            headers={**_cookie_headers(user_id), "X-Idempotency-Key": "dup"},
        )
    assert r2.status_code == 409
