"""Pytest fixtures shared across all layers.

All tests default to LEVEL_ENV=local so nothing hits GCP; Gemini calls are
faked via `agents.fakes.register_fake`.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("LEVEL_ENV", "local")
os.environ.setdefault("LEVEL_SESSION_SECRET", "test-secret-must-be-long-enough-for-signing")
os.environ.setdefault("CALENDAR_TZ", "UTC")

from level_core.agents import fakes as agent_fakes  # noqa: E402
from level_core.config import get_settings  # noqa: E402
from level_core.storage.factory import get_store  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    Path(".level/local_store").mkdir(parents=True, exist_ok=True)
    yield
    if Path(".level").exists():
        shutil.rmtree(".level", ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_fakes() -> Iterator[None]:
    agent_fakes.clear_fakes()
    yield
    agent_fakes.clear_fakes()


@pytest.fixture
def user_id() -> str:
    return f"u_test_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(user_id: str):  # type: ignore[no-untyped-def]
    return get_store(user_id)
