"""Every /v1/* mutating route requires a session cookie."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from level_api.main import create_app

PROTECTED = [
    ("GET", "/v1/me"),
    ("GET", "/v1/me/export"),
    ("DELETE", "/v1/me"),
    ("GET", "/v1/today"),
    ("GET", "/v1/today/summary"),
    ("GET", "/v1/profile"),
    ("POST", "/v1/profile/refresh", {}),
    ("POST", "/v1/profile/keep_not_me", {"entity": "person", "id": "x", "status": "kept"}),
    ("GET", "/v1/contacts"),
    ("GET", "/v1/reminders"),
    ("GET", "/v1/sources/status"),
    ("POST", "/v1/sources/sync", {}),
]


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize("row", PROTECTED)
async def test_unauth_gets_401(row) -> None:  # type: ignore[no-untyped-def]
    method, path, *rest = row
    payload = rest[0] if rest else None
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if method == "GET":
            resp = await client.get(path)
        elif method == "POST":
            resp = await client.post(path, json=payload or {})
        else:
            resp = await client.delete(path)
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.security
async def test_healthz_open() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
