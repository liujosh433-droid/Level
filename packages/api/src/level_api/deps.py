"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from level_core.auth.sessions import SESSION_COOKIE_NAME, parse_session_cookie
from level_core.storage.base import UserStore
from level_core.storage.factory import get_store


async def get_current_user_id(
    level_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str:
    session = parse_session_cookie(level_session)
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_signed_in")
    return session.user_id


async def get_user_store(user_id: str = Depends(get_current_user_id)) -> UserStore:
    return get_store(user_id)


async def optional_user_id(
    level_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> str | None:
    session = parse_session_cookie(level_session)
    return session.user_id if session else None
