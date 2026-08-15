"""Gmail send grant helpers — live scopes beat a stale local store."""

from __future__ import annotations

from datetime import datetime, timezone

from level_core.auth.google_oauth import (
    GMAIL_SEND_SCOPE,
    fetch_token_scopes,
    refresh_token_grant,
    token_has_gmail_send,
)
from level_core.schemas.user import OAuthToken


def test_token_has_gmail_send_true_for_send_and_full_mail() -> None:
    assert token_has_gmail_send([GMAIL_SEND_SCOPE])
    assert token_has_gmail_send(["https://mail.google.com/"])


def test_token_has_gmail_send_false_for_calendar_only_or_empty() -> None:
    assert not token_has_gmail_send(None)
    assert not token_has_gmail_send([])
    assert not token_has_gmail_send(["https://www.googleapis.com/auth/calendar.events"])


def test_fetch_token_scopes_reads_tokeninfo(monkeypatch) -> None:
    class FakeCreds:
        token = "access-token"
        expired = False
        refresh_token = None
        scopes = ["https://www.googleapis.com/auth/calendar.events"]

    class FakeResp:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {
                "scope": (
                    "openid https://www.googleapis.com/auth/calendar.events "
                    f"{GMAIL_SEND_SCOPE}"
                )
            }

    def fake_get(url: str, params: dict | None = None, timeout: float | None = None):
        assert "tokeninfo" in url
        assert params == {"access_token": "access-token"}
        return FakeResp()

    monkeypatch.setattr("httpx.get", fake_get)
    scopes = fetch_token_scopes(FakeCreds())  # type: ignore[arg-type]
    assert token_has_gmail_send(scopes)


def test_refresh_token_grant_persists_live_scopes(monkeypatch) -> None:
    token = OAuthToken(
        user_id="u1",
        access_token="old",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="secret",
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )

    class FakeCreds:
        token = "new-access"
        expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
        refresh_token = "refresh"

    monkeypatch.setattr(
        "level_core.auth.google_oauth.credentials_from_token",
        lambda *a, **k: FakeCreds(),
    )
    monkeypatch.setattr(
        "level_core.auth.google_oauth.fetch_token_scopes",
        lambda creds: [
            "https://www.googleapis.com/auth/calendar.events",
            GMAIL_SEND_SCOPE,
        ],
    )
    out = refresh_token_grant(token)
    assert out.access_token == "new-access"
    assert token_has_gmail_send(out.scopes)
