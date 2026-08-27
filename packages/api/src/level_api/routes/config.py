"""Frontend feature-flag hints.

Kept intentionally lean: the frontend polls this once at mount to
decide whether to render the demo-mode entry point on the "Connect
Google" wall. Anything more sensitive would need an authed route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from level_core.config import get_settings
from level_core.demo.scenarios import list_scenarios

router = APIRouter()


@router.get("/features")
async def features() -> dict[str, Any]:
    settings = get_settings()
    # Demo is available in local always, and in cloud when the
    # operator explicitly opts in via LEVEL_DEMO_IN_CLOUD=true. See
    # ``Settings.level_demo_in_cloud`` for the safety story.
    demo_available = settings.is_local or settings.level_demo_in_cloud
    return {
        "env": settings.level_env,
        "demo": {
            "available": demo_available,
            # Only advertise scenarios when demo is actually usable
            # so an inspector on the cloud API can't glean the demo
            # catalog when it's turned off.
            "scenarios": list_scenarios() if demo_available else [],
        },
    }
