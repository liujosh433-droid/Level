"""Signed session cookie mint/parse."""

from __future__ import annotations

from level_core.auth.session import (
    mint_handoff_token,
    mint_session_token,
    parse_handoff_token,
    parse_session_token,
    safe_next_path,
)
from level_core.config import Settings


def test_session_roundtrip() -> None:
    settings = Settings(LEVEL_SESSION_SECRET="test-secret-abc")
    token = mint_session_token("user123", settings)
    assert parse_session_token(token, settings) == "user123"


def test_session_rejects_tamper() -> None:
    settings = Settings(LEVEL_SESSION_SECRET="test-secret-abc")
    token = mint_session_token("user123", settings)
    bad = token[:-4] + "dead"
    assert parse_session_token(bad, settings) is None


def test_session_rejects_wrong_secret() -> None:
    a = Settings(LEVEL_SESSION_SECRET="aaa")
    b = Settings(LEVEL_SESSION_SECRET="bbb")
    token = mint_session_token("user123", a)
    assert parse_session_token(token, b) is None


def test_handoff_roundtrip() -> None:
    settings = Settings(LEVEL_SESSION_SECRET="test-secret-abc")
    token = mint_handoff_token("user123", settings)
    assert parse_handoff_token(token, settings) == "user123"
    # Regular session tokens are not valid handoffs.
    assert parse_handoff_token(mint_session_token("user123", settings), settings) is None


def test_safe_next_path() -> None:
    assert safe_next_path("/sources?connected=1") == "/sources?connected=1"
    assert safe_next_path("https://evil.example") == "/today"
    assert safe_next_path("//evil.example") == "/today"
