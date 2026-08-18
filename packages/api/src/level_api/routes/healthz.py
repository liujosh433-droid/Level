"""Health check + trace id."""

from __future__ import annotations

from fastapi import APIRouter
from level_core.config import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.level_env,
        "version": "2.0.0",
        "model_pro": settings.level_model_pro,
        "model_flash": settings.level_model_flash,
    }
