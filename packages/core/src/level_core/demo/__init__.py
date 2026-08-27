"""Demo-mode: load pre-baked example calendars without Google OAuth.

Used by ``POST /v1/auth/demo`` so a hackathon judge can clone the repo,
``make dev``, click one button, and land in a fully populated app -
no Google Cloud project, no OAuth client, no calendar import required.

This package is guarded to LEVEL_ENV=local at the API layer; the
seeder itself is env-agnostic and safe to import in tests.
"""

from level_core.demo.scenarios import SCENARIOS, ScenarioConfig, list_scenarios
from level_core.demo.seeder import DemoSeedResult, seed_demo_user

__all__ = [
    "SCENARIOS",
    "ScenarioConfig",
    "list_scenarios",
    "DemoSeedResult",
    "seed_demo_user",
]
