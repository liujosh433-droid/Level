"""Auth: guest + Google OAuth with httpOnly session cookies."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from level_api.auth_deps import (
    attach_session,
    clear_session,
    read_session_user_id,
    require_user,
)
from level_core.auth.session import mint_handoff_token, parse_handoff_token, safe_next_path
from level_api.dependencies import get_calendar_sync_store, get_token_store
from level_api.services.google_sync import onboard_google_user
from level_core.auth.google_oauth import (
    authorization_url,
    exchange_code,
    fetch_token_scopes,
    fetch_userinfo,
    parse_oauth_state,
    refresh_token_grant,
    token_from_credentials,
    token_has_gmail_send,
    verify_oauth_state,
)
from level_core.calendar.sync_state import CalendarSyncState
from level_core.config import get_settings
from level_core.observability.logger import get_logger
from level_core.schemas.user import (
    User,
    format_person_name,
    is_placeholder_display_name,
    resolve_display_name,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_logger = get_logger(__name__)


class MeResponse(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    google_connected: bool = False
    can_write_calendar: bool = False
    can_send_email: bool = False


class GuestRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)


def _token_can_write_calendar(scopes: list[str] | None) -> bool:
    """True unless we know the grant is calendar read-only (missing write).

    Empty/unknown scopes: assume OK so we don't nag after a normal connect.
    """
    if not scopes:
        return True
    joined = " ".join(scopes)
    if (
        "https://www.googleapis.com/auth/calendar.events" in joined
        or "https://www.googleapis.com/auth/calendar" in joined
    ):
        return True
    if "https://www.googleapis.com/auth/calendar.readonly" in joined:
        return False
    return True


async def _me_payload(user_id: str) -> MeResponse:
    store = get_token_store()
    user, token = await asyncio.gather(
        store.get_user(user_id),
        store.get_google_token(user_id),
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Session user not found — log in again.")
    # Upgrade leftover "Caregiver" / guest labels once we have an email.
    if user.email and is_placeholder_display_name(user.display_name):
        upgraded = resolve_display_name(email=user.email, existing=user.display_name)
        if upgraded and upgraded != user.display_name:
            user = user.model_copy(update={"display_name": upgraded})
            await store.upsert_user(user)
    # Always present a capitalized name in the UI.
    pretty = format_person_name(user.display_name)
    if pretty and pretty != user.display_name:
        user = user.model_copy(update={"display_name": pretty})
        await store.upsert_user(user)
    connected = token is not None and bool(token.refresh_token or token.access_token)
    if connected and token is not None and not token_has_gmail_send(token.scopes):
        try:
            token = await asyncio.to_thread(refresh_token_grant, token)
            await store.upsert_token(token)
        except Exception:  # noqa: BLE001
            _logger.info("gmail_scope_refresh_skipped")
    return MeResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        google_connected=connected,
        can_write_calendar=(
            not connected or _token_can_write_calendar(token.scopes if token else None)
        ),
        can_send_email=(
            not connected or token_has_gmail_send(token.scopes if token else None)
        ),
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

    raw = (payload.display_name if payload else None) or None
    # Avoid role labels ("Caregiver") — leave null until Google/email gives a real name.
    if raw and is_placeholder_display_name(raw):
        raw = None
    name = " ".join(raw.split()).strip() if raw else None
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
async def google_start(
    request: Request,
    need: str | None = Query(default=None, max_length=20),
) -> RedirectResponse:
    # Link Google onto the current session user when present.
    link_user_id = read_session_user_id(request)
    want = "gmail" if (need or "").strip().lower() == "gmail" else None
    try:
        url, _state = authorization_url(link_user_id=link_user_id, need=want)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(url)


@router.get("/google/callback")
async def google_callback(
    background_tasks: BackgroundTasks,
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
    link_user_id = parsed.link_user_id if parsed else None

    try:
        # Sync OAuth HTTP — keep off the event loop.
        creds = await asyncio.to_thread(exchange_code, code, state, settings)
        info = await asyncio.to_thread(fetch_userinfo, creds)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("oauth_callback_failed")
        raise HTTPException(status_code=400, detail=f"OAuth failed: {exc}") from exc

    store = get_token_store()
    google_sub = str(info.get("sub") or "")
    email = info.get("email")
    google_name = info.get("name") or info.get("given_name")

    user = await store.get_user_by_google_sub(google_sub) if google_sub else None
    if user is None and link_user_id:
        linked = await store.get_user(link_user_id)
        if linked is not None:
            user = linked.model_copy(
                update={
                    "email": email or linked.email,
                    "display_name": resolve_display_name(
                        google_name=google_name,
                        email=email or linked.email,
                        existing=linked.display_name,
                    ),
                    "google_sub": google_sub or linked.google_sub,
                }
            )
    if user is None:
        user = User(
            email=email,
            display_name=resolve_display_name(
                google_name=google_name, email=email, existing=None
            ),
            google_sub=google_sub or None,
        )
    else:
        user = user.model_copy(
            update={
                "email": email or user.email,
                "display_name": resolve_display_name(
                    google_name=google_name,
                    email=email or user.email,
                    existing=user.display_name,
                ),
                "google_sub": google_sub or user.google_sub,
            }
        )
    await store.upsert_user(user)

    token = token_from_credentials(creds, user_id=user.user_id, settings=settings)
    try:
        live = await asyncio.to_thread(fetch_token_scopes, creds)
        if live:
            token = token.model_copy(update={"scopes": live})
    except Exception:  # noqa: BLE001
        _logger.info("oauth_live_scopes_skipped")
    _logger.info(
        "oauth_granted_scopes",
        has_gmail=token_has_gmail_send(token.scopes),
        scope_count=len(token.scopes or []),
    )
    prior_token = await store.get_google_token(user.user_id)
    if prior_token:
        updates: dict = {}
        if not token.refresh_token and prior_token.refresh_token:
            updates["refresh_token"] = prior_token.refresh_token
        merged = list(
            dict.fromkeys([*(prior_token.scopes or []), *(token.scopes or [])])
        )
        if merged != (token.scopes or []):
            updates["scopes"] = merged
        if updates:
            token = token.model_copy(update=updates)
    await store.upsert_token(token)

    sync_store = get_calendar_sync_store()
    prior = await sync_store.get(user.user_id)
    already_onboarded = bool(
        prior and (prior.profile_ingested_at is not None or prior.initial_sync_done)
    )
    await sync_store.upsert(
        CalendarSyncState(
            user_id=user.user_id,
            sync_token=prior.sync_token if prior else None,
            events=prior.events if prior else {},
            agenda_updated_at=prior.agenda_updated_at if prior else None,
            profile_ingested_at=prior.profile_ingested_at if prior else None,
            # Returning users stay "done"; first-time waits on Sources.
            initial_sync_done=already_onboarded,
            initial_sync_error=None if already_onboarded else None,
            channel_id=prior.channel_id if prior else None,
            resource_id=prior.resource_id if prior else None,
            channel_token=prior.channel_token if prior else None,
            channel_expiration_ms=prior.channel_expiration_ms if prior else None,
        )
    )

    # Agenda warm + one-time profile ingest in background (LLM only on first connect).
    background_tasks.add_task(onboard_google_user, user.user_id)

    web = settings.web_app_url.rstrip("/")
    # Existing Google users land on Today; first-time goes through Sources onboard.
    # If send-mail still isn't on the grant, stay on Sources so they can retry.
    if already_onboarded and token_has_gmail_send(token.scopes):
        next_path = "/today"
    elif already_onboarded:
        next_path = "/sources?need_gmail=1"
    else:
        next_path = "/sources?connected=1"
    # OAuth callback stays on :8080 (registered in Google Console). Hand the
    # session to the web origin via /v1/auth/handoff so the cookie is first-party
    # on :3000 (Next rewrite) instead of stuck on the API host.
    handoff = mint_handoff_token(user.user_id, settings)
    dest = (
        f"{web}/v1/auth/handoff"
        f"?token={quote(handoff, safe='')}"
        f"&next={quote(next_path, safe='')}"
    )
    return RedirectResponse(dest)


@router.get("/handoff")
async def auth_handoff(
    token: str = Query(...),
    next: str = Query("/today", alias="next"),  # noqa: A002
) -> RedirectResponse:
    """Exchange a short-lived OAuth handoff token for a level_session cookie."""
    user_id = parse_handoff_token(token)
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="Sign-in handoff expired — go back and connect Google again.",
        )
    dest = safe_next_path(next, default="/today")
    resp = RedirectResponse(dest)
    attach_session(resp, user_id)
    return resp


@router.get("/me", response_model=MeResponse)
async def me(user_id: str = Depends(require_user)) -> Response:
    """Return the current caregiver; clear stale cookies if the user was wiped."""
    try:
        payload = await _me_payload(user_id)
    except HTTPException as exc:
        if exc.status_code == 401:
            resp = JSONResponse({"detail": exc.detail}, status_code=401)
            clear_session(resp)
            return resp
        raise
    return JSONResponse(payload.model_dump())


class UpdateMeRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    payload: UpdateMeRequest,
    user_id: str = Depends(require_user),
) -> MeResponse:
    """Remember the caregiver's preferred name (used in greetings + avatar)."""
    store = get_token_store()
    user = await store.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Session user not found — log in again.")
    name = format_person_name(payload.display_name)
    if not name:
        raise HTTPException(status_code=400, detail="Name can’t be empty.")
    user = user.model_copy(update={"display_name": name})
    user.touch()
    await store.upsert_user(user)
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
