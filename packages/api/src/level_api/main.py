"""Entry point for the Level FastAPI service on Cloud Run."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from level_api.bootstrap import seed_local_demo
from level_api.dependencies import (
    cached_memory,
    cached_registry,
    cached_settings,
)
from level_api.routes import (
    auth,
    calendar,
    care_actions,
    health,
    ingest,
    observability,
    sessions,
    sources,
    today,
)
from level_api.telemetry import instrument_app
from level_core.agents.conductor import register_all_agents
from level_core.models.factory import build_embedding_client
from level_core.observability.logger import bind_context, get_logger

_logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    """Startup + shutdown hooks."""
    settings = cached_settings()
    _logger.info(
        "startup",
        env=settings.env.value,
        project=settings.gcp_project,
        service=settings.service_name,
    )
    bind_context(service=settings.service_name)

    registry = cached_registry()
    await register_all_agents(registry, settings)

    if settings.is_cloud:
        settings.assert_cloud_ready()
    elif os.getenv("LEVEL_SEED_DEMO", "").lower() in {"1", "true", "yes"}:
        # Opt-in only — never auto-seed Maya/demo facts into a real local user.
        await seed_local_demo(
            memory=cached_memory(),
            embedder=build_embedding_client(settings),
            settings=settings,
        )

    yield

    _logger.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Level API",
        version="0.1.0",
        description="Warm-but-honest AI decision partner for busy caregivers.",
        lifespan=lifespan,
    )
    instrument_app(app)
    settings = cached_settings()
    origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    web = (settings.web_app_url or "").rstrip("/")
    if web and web not in origins:
        origins.append(web)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(ingest.router)
    app.include_router(sources.router)
    app.include_router(today.router)
    app.include_router(care_actions.router)
    app.include_router(calendar.router)
    app.include_router(observability.router)
    return app


app = create_app()


__all__ = ["app", "create_app"]
