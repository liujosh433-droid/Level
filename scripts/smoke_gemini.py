#!/usr/bin/env python3
"""Live smoke test against Gemini (AI Studio or Vertex).

Usage (local / AI Studio):
    export GOOGLE_API_KEY=...
    export LEVEL_ENV=local
    uv run python scripts/smoke_gemini.py

Usage (Vertex / paid):
    gcloud auth application-default login
    export LEVEL_ENV=cloud
    export GOOGLE_CLOUD_PROJECT=project-c31bdcdc-f293-47c2-a4c
    uv run python scripts/smoke_gemini.py

Exits 0 on success, 1 on failure. Safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> int:
    # Ensure repo packages are importable when run as a script.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "core", "src"))

    from level_core.config import get_settings
    from level_core.models.base import GenerationRequest
    from level_core.models.factory import build_gemini_client

    settings = get_settings()
    print(f"LEVEL_ENV={settings.env.value}  project={settings.gcp_project}")
    print(f"fast_model={settings.fast_model}")

    client = build_gemini_client(settings)
    request = GenerationRequest(
        prompt=(
            "Reply with exactly one short sentence confirming you are Level's "
            "smoke test, then stop."
        ),
        model_id=settings.fast_model,
        system_instruction="You are a smoke-test harness. Be terse.",
        temperature=0.0,
        max_output_tokens=64,
        metadata={"agent": "smoke", "version": "0.0.0"},
    )

    try:
        response = await client.generate(request)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("--- response ---")
    print(response.text.strip())
    print("--- tokens ---")
    print(f"in={response.input_tokens} out={response.output_tokens} model={response.model_id}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
