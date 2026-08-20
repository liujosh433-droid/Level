"""Google OAuth start + callback."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, BackgroundTasks, Cookie, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from level_core.auth.google_oauth import build_auth_url, exchange_code
from level_core.auth.sessions import (
    SESSION_COOKIE_NAME,
    STATE_COOKIE_NAME,
    build_session_cookie,
    sign_state,
    verify_state,
)
from level_core.auth.tokens import save_tokens
from level_core.config import get_settings
from level_core.schemas import UserSession
from level_core.storage.care_store import ensure_self_person
from level_core.storage.factory import get_store
from level_api.routes.today import refresh_and_enrich_safe

router = APIRouter()


@router.get("/google/start")
async def google_start(response: Response) -> RedirectResponse:
    start = build_auth_url()
    redirect = RedirectResponse(url=start.url, status_code=307)
    settings = get_settings()
    redirect.set_cookie(
        key=STATE_COOKIE_NAME,
        value=sign_state(start.state, code_verifier=start.code_verifier),
        max_age=600,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
    )
    return redirect


@router.get("/google/callback")
async def google_callback(
    background: BackgroundTasks,
    code: str = Query(...),
    state: str = Query(...),
    level_oauth_state: str | None = Cookie(default=None, alias=STATE_COOKIE_NAME),
) -> RedirectResponse:
    state_result = verify_state(level_oauth_state, state)
    if not state_result:
        raise HTTPException(status_code=400, detail="bad_state")
    code_verifier = state_result if isinstance(state_result, str) else None

    settings = get_settings()
    exchanged = exchange_code(code=code, code_verifier=code_verifier)
    email = exchanged.email or "anon@local"
    user_id = _stable_user_id(email)

    store = get_store(user_id)
    await save_tokens(
        store,
        payload={
            "access_token": exchanged.access_token,
            "refresh_token": exchanged.refresh_token,
            "id_token": exchanged.id_token,
            "expiry_epoch": exchanged.expiry_epoch,
            "email": email,
        },
    )
    await store.profile.write(
        {
            "user_id": user_id,
            "email": email,
            "tz": settings.calendar_tz,
            "calendar_window_days_back": settings.level_cal_days_back,
            "calendar_window_days_forward": settings.level_cal_days_forward,
        }
    )

    await ensure_self_person(store)

    session = UserSession(user_id=user_id, email=email)
    background.add_task(refresh_and_enrich_safe, store)
    redirect = RedirectResponse(url=settings.level_web_app_url, status_code=307)
    redirect.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=build_session_cookie(session),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
    )
    redirect.delete_cookie(STATE_COOKIE_NAME)
    return redirect


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    settings = get_settings()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True, "env": settings.level_env == "cloud"}


def _stable_user_id(email: str) -> str:
    return "u_" + hashlib.sha256(email.lower().encode()).hexdigest()[:16]
