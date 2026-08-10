"""Request-scoped auth: read signed session cookie, never trust client user_id."""

from __future__ import annotations

from fastapi import HTTPException, Request, Response, status

from level_core.auth.session import (
    COOKIE_NAME,
    mint_session_token,
    parse_session_token,
    session_cookie_kwargs,
)
from level_core.config import get_settings


def read_session_user_id(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return parse_session_token(token, get_settings())


async def require_user(request: Request) -> str:
    """Dependency: authenticated caregiver user_id from httpOnly cookie."""
    user_id = read_session_user_id(request)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not logged in. Open Level and connect again.",
        )
    return user_id


def attach_session(response: Response, user_id: str) -> None:
    settings = get_settings()
    kwargs = session_cookie_kwargs(settings)
    response.set_cookie(value=mint_session_token(user_id, settings), **kwargs)


def clear_session(response: Response) -> None:
    settings = get_settings()
    kwargs = session_cookie_kwargs(settings)
    # delete_cookie needs matching path/samesite; max_age ignored on delete
    response.delete_cookie(
        key=COOKIE_NAME,
        path=kwargs.get("path", "/"),
        httponly=True,
        samesite=kwargs.get("samesite", "lax"),
        secure=bool(kwargs.get("secure")),
    )


__all__ = [
    "attach_session",
    "clear_session",
    "read_session_user_id",
    "require_user",
]
