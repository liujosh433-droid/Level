"""Google OAuth start + callback, plus local demo-mode bypass."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
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
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import refresh_agenda
from level_core.config import get_settings
from level_core.demo.scenarios import SCENARIOS, slot_for_ip, user_id_for_slot
from level_core.demo.seeder import seed_demo_user
from level_core.observability import get_logger
from level_core.schemas import UserSession
from level_core.storage.base import UserStore
from level_core.storage.care_store import ensure_self_person
from level_core.storage.factory import get_store
from pydantic import BaseModel

from level_api.rate_limit import TokenBucketLimiter
from level_api.routes.today import refresh_and_enrich_safe

router = APIRouter()
logger = get_logger(__name__)


async def _enrich_only(store: UserStore) -> None:
    """Run classification after the OAuth response has been sent."""
    try:
        await enrich_agenda(store)
    except Exception as exc:  # noqa: BLE001 - never let this break onboarding
        logger.warning("oauth.background_enrich_failed", error=str(exc)[:300])


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
    profile = dict(await store.profile.read() or {})
    profile.update(
        {
            "user_id": user_id,
            "email": email,
            "calendar_window_days_back": settings.level_cal_days_back,
            "calendar_window_days_forward": settings.level_cal_days_forward,
        }
    )
    if not profile.get("tz"):
        profile["tz"] = settings.calendar_tz
    await store.profile.write(profile)

    await ensure_self_person(store)

    session = UserSession(user_id=user_id, email=email)

    # First-connect UX: pull the calendar synchronously (with a strict
    # timeout) so the homepage renders with events instead of an empty
    # "Pulling..." state. LLM classification is always background - it's
    # the slow leg. If the sync pull times out or errors, fall back to
    # the classic background-both path; the frontend already handles the
    # empty state with a 1200ms poll loop.
    existing_events = await store.agenda.list()
    sync_ok = False
    if not existing_events:
        try:
            result = await asyncio.wait_for(
                refresh_agenda(store),
                timeout=settings.level_oauth_refresh_timeout_s,
            )
            sync_ok = True
            logger.info(
                "oauth.sync_refresh_ok",
                added=result.added,
                incremental_hits=result.incremental_hits,
                full_pulls=result.full_pulls,
            )
        except TimeoutError:
            logger.warning(
                "oauth.sync_refresh_timeout",
                timeout_s=settings.level_oauth_refresh_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth.sync_refresh_failed", error=str(exc)[:300])

    # Enrichment (LLM classification) always in background. If the sync
    # refresh failed / timed out, run the classic full path in background
    # so the frontend's poll loop still finds fresh data.
    if sync_ok:
        background.add_task(_enrich_only, store)
    else:
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


class DemoLoginBody(BaseModel):
    # Solo caregiver is the primary demo persona - single-parent
    # workload is the more distinctive story and the one that best
    # showcases RoleAgent inference (no co-parent to fall back on).
    scenario: Literal["family", "solo"] = "solo"


# Per-IP token bucket for the demo login endpoint. Sits in front of
# the seeder so a bot rotating scenarios can't burn through Firestore
# writes / LLM budget on the deployed API. Local dev is exempted
# (see demo_login below) because the endpoint is only reachable on
# localhost anyway.
#
# Lazily built so tests can rebuild the singleton via
# ``reset_demo_ip_limiter()`` and the settings are read once at first
# request instead of at import time (which would freeze the values
# before test monkeypatches take effect).
_demo_ip_limiter: TokenBucketLimiter | None = None


def _get_demo_ip_limiter() -> TokenBucketLimiter:
    global _demo_ip_limiter
    if _demo_ip_limiter is None:
        settings = get_settings()
        per_hour = int(settings.level_demo_per_ip_per_hour)
        _demo_ip_limiter = TokenBucketLimiter(
            capacity=per_hour,
            refill_per_second=per_hour / 3600.0,
        )
    return _demo_ip_limiter


def reset_demo_ip_limiter() -> None:
    """Test-only: rebuild the per-IP limiter singleton."""
    global _demo_ip_limiter
    _demo_ip_limiter = None


def _client_ip(request: Request) -> str:
    """Best-effort client IP.

    On Cloud Run behind the Google frontend, ``X-Forwarded-For`` is
    trusted (Google strips inbound copies). Locally it's usually
    unset and we fall back to the socket peer, which is
    ``127.0.0.1`` - fine for slotting because there's only one judge
    on localhost anyway.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First entry is the original client per RFC 7239.
        return xff.split(",")[0].strip()
    client = request.client
    return client.host if client else "0.0.0.0"


@router.post("/demo")
async def demo_login(
    body: DemoLoginBody, request: Request, response: Response
) -> dict[str, object]:
    """Zero-OAuth demo entry point.

    Seeds a synthetic user from an ICS fixture in ``example-data/``
    and drops the same signed session cookie a real OAuth callback
    would - no Google Cloud project, no OAuth client, no Gmail scope
    required.

    Two modes:

    - **Local** (``LEVEL_ENV=local``): always enabled. Each scenario
      maps to a single stable user id (``u_demo_<scenario>``), so a
      contributor iterating on the app can close the tab and come
      back to their state.

    - **Cloud** (``LEVEL_ENV=cloud`` + ``LEVEL_DEMO_IN_CLOUD=true``):
      enabled with guardrails. Judges can hit the deployed API
      directly without setup. Client IP is hashed to a slot in
      ``[0, level_demo_slots_per_scenario)``, giving each judge a
      stable user across clicks while capping the total user pool at
      ``slots * len(SCENARIOS)``. Per-IP token bucket
      (``level_demo_per_ip_per_hour``) rejects burst abuse. Cloud
      cookies are ``secure`` + ``samesite=lax`` for HTTPS-only.

      When ``LEVEL_DEMO_IN_CLOUD=false`` (default), the endpoint 404s
      so an attacker who guesses the URL against a deployed API
      can't spawn synthetic users. 404 rather than 403 so a probe
      can't distinguish "demo turned off" from "endpoint doesn't
      exist" - keeps the cloud surface flat.
    """
    settings = get_settings()
    if not (settings.is_local or settings.level_demo_in_cloud):
        raise HTTPException(status_code=404, detail="not_found")

    scenario = SCENARIOS.get(body.scenario)
    if scenario is None:
        raise HTTPException(status_code=400, detail="unknown_scenario")

    ip = _client_ip(request)
    # Rate limit is cloud-only. Local dev is single-user by design;
    # limiting yourself on localhost is pure friction.
    if not settings.is_local:
        decision = _get_demo_ip_limiter().check(ip)
        if not decision.allowed:
            logger.warning(
                "demo.rate_limited",
                ip=ip,
                scenario=scenario.id,
                retry_after_s=round(decision.retry_after_s, 1),
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limited",
                    "retry_after_s": round(decision.retry_after_s, 1),
                    "message": (
                        "Too many demo logins from this address. "
                        "Please wait a minute and try again."
                    ),
                },
                headers={"Retry-After": str(int(decision.retry_after_s) + 1)},
            )

    # Slot assignment: local always slot 0 (single-tenant); cloud
    # hashes IP -> slot for stable per-judge sessions across clicks.
    if settings.is_local:
        slot = 0
    else:
        slot = slot_for_ip(
            ip, scenario.id, settings.level_demo_slots_per_scenario
        )
    user_id = user_id_for_slot(scenario.id, slot)

    store = get_store(user_id)
    try:
        result = await seed_demo_user(store, scenario_id=scenario.id)
    except FileNotFoundError as exc:
        logger.error("demo.ics_missing", scenario=scenario.id, error=str(exc))
        raise HTTPException(
            status_code=500, detail="demo_ics_missing"
        ) from exc

    session = UserSession(user_id=user_id, email=scenario.email)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=build_session_cookie(session),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        # Cloud runs behind HTTPS on Cloud Run so require secure=true
        # there; local is HTTP-only so we can't set secure or the
        # browser drops the cookie.
        secure=not settings.is_local,
        samesite="lax",
    )
    logger.info(
        "demo.login",
        scenario=scenario.id,
        user_id=user_id,
        slot=slot,
        env=settings.level_env,
        events=result.events_count,
        people=result.people_count,
    )
    return {
        "ok": True,
        "scenario": scenario.id,
        "user_id": user_id,
        "slot": slot,
        "email": scenario.email,
        "display_name": scenario.display_name,
        "events_count": result.events_count,
        "people_count": result.people_count,
    }


def _stable_user_id(email: str) -> str:
    return "u_" + hashlib.sha256(email.lower().encode()).hexdigest()[:16]
