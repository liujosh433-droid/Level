"""Auth: guest + Google OAuth with httpOnly session cookies."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from level_api.auth_deps import (
    attach_session,
    clear_session,
    read_session_user_id,
    require_user,
)
from level_api.dependencies import get_token_store
from level_core.auth.google_oauth import (
    authorization_url,
    exchange_code,
    fetch_userinfo,
    parse_oauth_state,
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


async def _me_payload(user_id: str) -> MeResponse:
    store = get_token_store()
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Session user not found — log in again.")
    token = await store.get_google_token(user_id)
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        google_connected=token is not None and bool(token.refresh_token or token.access_token),
    )


@router.post("/guest", response_model=MeResponse)
async def create_guest(
    request: Request,
    payload: GuestRequest | None = None,
) -> JSONResponse:
    """Create or resume a local session (no Google required)."""
    existing = read_session_user_id(request)
    store = get_token_store()
    if existing:
        user = await store.get_user(existing)
        if user is not None:
            me = await _me_payload(user.user_id)
            resp = JSONResponse(me.model_dump())
            attach_session(resp, user.user_id)
            return resp

    name = (payload.display_name if payload else None) or "Guest parent"
    user = User(display_name=name)
    await store.upsert_user(user)
    me = MeResponse(
        user_id=user.user_id,
        email=None,
        display_name=user.display_name,
        google_connected=False,
    )
    resp = JSONResponse(me.model_dump())
    attach_session(resp, user.user_id)
    return resp


@router.get("/google/start")
async def google_start(request: Request) -> RedirectResponse:
    # Link Google onto the current session user when present.
    link_user_id = read_session_user_id(request)
    try:
        url, _state = authorization_url(link_user_id=link_user_id)
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

    parsed = parse_oauth_state(state, settings)
    link_user_id = parsed[1] if parsed else None

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
    if user is None and link_user_id:
        linked = await store.get_user(link_user_id)
        if linked is not None:
            user = linked.model_copy(
                update={
                    "email": email or linked.email,
                    "display_name": name or linked.display_name,
                    "google_sub": google_sub or linked.google_sub,
                }
            )
    if user is None:
        user = User(email=email, display_name=name, google_sub=google_sub or None)
    else:
        user = user.model_copy(
            update={
                "email": email or user.email,
                "display_name": name or user.display_name,
                "google_sub": google_sub or user.google_sub,
            }
        )
    await store.upsert_user(user)

    token = token_from_credentials(creds, user_id=user.user_id, settings=settings)
    await store.upsert_token(token)

    dest = f"{settings.web_app_url.rstrip('/')}/sources?connected=1"
    resp = RedirectResponse(dest)
    attach_session(resp, user.user_id)
    return resp


@router.get("/me", response_model=MeResponse)
async def me(user_id: str = Depends(require_user)) -> MeResponse:
    return await _me_payload(user_id)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
) -> dict[str, bool | str]:
    """Clear session cookie and disconnect Google for this session only."""
    user_id = read_session_user_id(request)
    removed = False
    if user_id:
        store = get_token_store()
        removed = await store.delete_google_token(user_id)
        _logger.info("logout", user_id=user_id, google_token_removed=removed)
    clear_session(response)
    return {
        "ok": True,
        "user_id": user_id or "",
        "google_disconnected": removed,
    }


__all__ = ["router"]
