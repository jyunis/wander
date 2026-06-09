"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.config import Settings
from cairn.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at an isolated temp data dir (no .env, no env vars)."""
    return Settings(_env_file=None, data_dir=tmp_path)


@pytest.fixture
async def store(settings: Settings) -> Store:
    """An opened, schema-initialized SpatiaLite store on a temp DB."""
    s = await Store.open(settings)
    try:
        yield s
    finally:
        await s.close()
