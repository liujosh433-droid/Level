"""Signed session cookie (itsdangerous).

Cookie carries {user_id, email} signed with LEVEL_SESSION_SECRET.
httpOnly + Secure(in cloud) + SameSite=Lax is set at the FastAPI layer.
"""

from __future__ import annotations

from itsdangerous import BadSignature, URLSafeSerializer

from level_core.config import get_settings
from level_core.schemas import UserSession

SESSION_COOKIE_NAME = "level_session"
STATE_COOKIE_NAME = "level_oauth_state"


def _serializer() -> URLSafeSerializer:
    settings = get_settings()
    return URLSafeSerializer(settings.level_session_secret, salt="level.session")


def build_session_cookie(session: UserSession) -> str:
    return _serializer().dumps(session.model_dump(mode="json"))


def parse_session_cookie(value: str | None) -> UserSession | None:
    if not value:
        return None
    try:
        payload = _serializer().loads(value)
    except BadSignature:
        return None
    try:
        return UserSession.model_validate(payload)
    except Exception:
        return None


def require_user_id(cookie_value: str | None) -> str | None:
    session = parse_session_cookie(cookie_value)
    return session.user_id if session else None


def sign_state(state: str, code_verifier: str | None = None) -> str:
    return _serializer().dumps({"s": state, "v": code_verifier})


def verify_state(cookie_value: str | None, state_param: str) -> str | None | bool:
    """Return the code_verifier when the state matches, `True` if PKCE isn't in
    use, and `False` on any mismatch/tamper. Callers treat any falsy result as
    invalid.
    """
    if not cookie_value or not state_param:
        return False
    try:
        payload = _serializer().loads(cookie_value)
    except BadSignature:
        return False
    if payload.get("s") != state_param:
        return False
    verifier = payload.get("v")
    return verifier if verifier else True
