"""Session cookie: signed, tamper-proof."""

from __future__ import annotations

from level_core.auth.sessions import build_session_cookie, parse_session_cookie
from level_core.schemas import UserSession


def test_roundtrip() -> None:
    orig = UserSession(user_id="u_abc", email="a@b.co")
    cookie = build_session_cookie(orig)
    parsed = parse_session_cookie(cookie)
    assert parsed is not None
    assert parsed.user_id == "u_abc"


def test_tamper_returns_none() -> None:
    orig = UserSession(user_id="u_abc")
    cookie = build_session_cookie(orig) + "x"
    assert parse_session_cookie(cookie) is None


def test_missing_returns_none() -> None:
    assert parse_session_cookie(None) is None
    assert parse_session_cookie("") is None
