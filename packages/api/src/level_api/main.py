"""FastAPI app for Level.

Routes are namespaced under /v1. Every mutating route requires the signed
session cookie (see deps.require_user).
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from level_core.config import get_settings
from level_core.observability import get_logger

from level_api.routes import (
    admin,
    auth,
    calendar,
    chat,
    contacts,
    email,
    feedback,
    healthz,
    me,
    media,
    profile,
    reminders,
    schedule,
    sources,
    today,
)

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Level API",
        version="2.0.0",
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )

    origins = ["*"] if settings.is_local else [settings.level_web_app_url]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        trace_id = request.headers.get("x-cloud-trace-context") or uuid.uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(healthz.router, prefix="/v1")
    app.include_router(auth.router, prefix="/v1/auth")
    app.include_router(calendar.router, prefix="/v1/calendar")
    app.include_router(chat.router, prefix="/v1")
    app.include_router(contacts.router, prefix="/v1/contacts")
    app.include_router(email.router, prefix="/v1/email")
    app.include_router(me.router, prefix="/v1/me")
    app.include_router(profile.router, prefix="/v1/profile")
    app.include_router(reminders.router, prefix="/v1/reminders")
    app.include_router(schedule.router, prefix="/v1/schedule")
    app.include_router(sources.router, prefix="/v1/sources")
    app.include_router(today.router, prefix="/v1/today")
    app.include_router(feedback.router, prefix="/v1/feedback")
    app.include_router(media.router, prefix="/v1/media")
    app.include_router(admin.router, prefix="/v1/admin")

    @app.exception_handler(Exception)
    async def _fallback(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "api.unhandled",
            path=request.url.path,
            trace_id=getattr(request.state, "trace_id", ""),
        )
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    return app


app = create_app()
