"""Google OAuth connect flow for Calendar + Drive."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from level_api.dependencies import get_token_store
from level_core.auth.google_oauth import (
    authorization_url,
    exchange_code,
    fetch_userinfo,
    token_from_credentials,
    verify_oauth_state,
)
from level_core.config import get_settings
from level_core.observability.logger import get_logger
from level_core.schemas.user import User

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_logger = get_logger(__name__)


class MeResponse(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    google_connected: bool = False


class GuestRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)


@router.post("/guest", response_model=MeResponse)
async def create_guest(payload: GuestRequest | None = None) -> MeResponse:
    """Create a local user id without Google — enough for ChatGPT upload tests."""
    store = get_token_store()
    name = (payload.display_name if payload else None) or "Guest parent"
    user = User(display_name=name)
    await store.upsert_user(user)
    return MeResponse(
        user_id=user.user_id,
        email=None,
        display_name=user.display_name,
        google_connected=False,
    )


@router.get("/google/start")
async def google_start() -> RedirectResponse:
    try:
        url, _state = authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    settings = get_settings()
    if not verify_oauth_state(state, settings):
        raise HTTPException(
            status_code=400,
            detail="invalid OAuth state — start again from Connect Google (don't reuse an old tab)",
        )

    try:
        creds = exchange_code(code, state, settings)
        info = fetch_userinfo(creds)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("oauth_callback_failed")
        raise HTTPException(status_code=400, detail=f"OAuth failed: {exc}") from exc

    store = get_token_store()
    google_sub = str(info.get("sub") or "")
    email = info.get("email")
    name = info.get("name")

    user = await store.get_user_by_google_sub(google_sub) if google_sub else None
    if user is None:
        user = User(email=email, display_name=name, google_sub=google_sub or None)
    else:
        user = user.model_copy(
            update={"email": email or user.email, "display_name": name or user.display_name}
        )
    await store.upsert_user(user)

    token = token_from_credentials(creds, user_id=user.user_id, settings=settings)
    await store.upsert_token(token)

    dest = f"{settings.web_app_url.rstrip('/')}/sources?user_id={user.user_id}&connected=1"
    return RedirectResponse(dest)


@router.get("/me", response_model=MeResponse)
async def me(user_id: str) -> MeResponse:
    store = get_token_store()
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    token = await store.get_google_token(user_id)
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        google_connected=token is not None and bool(token.refresh_token or token.access_token),
    )


__all__ = ["router"]
