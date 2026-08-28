"""One-shot Veo Info-page film.

The weekly recap path is gone. These tests lock the cheaper contract:
generate once, cache globally, never touch a user's calendar.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from level_core.config import get_settings


def _make_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import tempfile

    monkeypatch.setenv("LEVEL_ENV", "local")
    monkeypatch.setenv("LEVEL_LOCAL_STORE_ROOT", tempfile.mkdtemp())
    get_settings.cache_clear()
    from level_api.main import create_app

    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_intro() -> Iterator[None]:
    from level_api.routes import media as media_routes

    media_routes.reset_intro_runtime_state()
    yield
    media_routes.reset_intro_runtime_state()


def test_intro_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEVEL_MEDIA_ENABLED", raising=False)
    client = _make_client(monkeypatch)

    r = client.get("/v1/media/intro")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is False
    assert body["reason"] == "media_disabled"
    assert body["video_url"] is None


def test_intro_cold_get_returns_generating(monkeypatch: pytest.MonkeyPatch) -> None:
    from level_api.routes import media as media_routes

    monkeypatch.setenv("LEVEL_MEDIA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    client = _make_client(monkeypatch)

    async def frozen_bg(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(media_routes, "_run_intro_in_background", frozen_bg)
    monkeypatch.setattr(media_routes, "_lookup_intro_url", lambda: None)

    r1 = client.get("/v1/media/intro")
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["ready"] is False
    assert body["reason"] == "generating"
    assert body["generating"] is True
    assert body["started_at"]
    started_at = body["started_at"]

    r2 = client.get("/v1/media/intro")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["reason"] == "generating"
    assert body2["started_at"] == started_at


def test_intro_cached_url_skips_veo(monkeypatch: pytest.MonkeyPatch) -> None:
    from level_api.routes import media as media_routes

    monkeypatch.setenv("LEVEL_MEDIA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    client = _make_client(monkeypatch)

    calls = {"veo": 0}

    async def fake_veo(*, prompt: str, model: str) -> dict[str, str]:
        calls["veo"] += 1
        return {"video_url": "https://example.test/should-not-run.mp4"}

    monkeypatch.setattr(media_routes, "_generate_veo", fake_veo)
    monkeypatch.setattr(
        media_routes, "_lookup_intro_url", lambda: "https://example.test/intro.mp4"
    )

    r = client.get("/v1/media/intro")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert body["cached"] is True
    assert body["video_url"] == "https://example.test/intro.mp4"
    assert calls["veo"] == 0


@pytest.mark.asyncio
async def test_intro_background_pins_process_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from level_api.routes import media as media_routes

    monkeypatch.setenv("LEVEL_MEDIA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    get_settings.cache_clear()

    async def fake_veo(*, prompt: str, model: str) -> dict[str, str]:
        assert "Level" in prompt
        return {"video_url": "https://example.test/veo/intro.mp4"}

    monkeypatch.setattr(media_routes, "_generate_veo", fake_veo)
    monkeypatch.setattr(media_routes, "_promote_intro", lambda url: url)

    await media_routes._run_intro_in_background(model="veo-3.1-fast-generate-001")
    assert media_routes._intro_cached_url == "https://example.test/veo/intro.mp4"


@pytest.mark.asyncio
async def test_intro_failure_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from level_api.routes import media as media_routes

    monkeypatch.setenv("LEVEL_MEDIA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    client = _make_client(monkeypatch)

    calls = {"veo": 0}

    async def fake_veo(*, prompt: str, model: str) -> dict[str, str]:
        calls["veo"] += 1
        return {"reason": "veo_no_output"}

    monkeypatch.setattr(media_routes, "_generate_veo", fake_veo)
    monkeypatch.setattr(media_routes, "_lookup_intro_url", lambda: None)

    await media_routes._run_intro_in_background(model="veo-3.1-fast-generate-001")
    assert calls["veo"] == 1

    r = client.get("/v1/media/intro")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["reason"] == "veo_no_output"
    assert calls["veo"] == 1, "cooldown must not spawn another Veo call"
