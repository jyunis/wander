"""Trails overlay API (/trails) — reads LOCAL preloaded data (feature_class=
'trail'); empty until scripts/load_trails.py has run. Display-only (TLC-3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        yield c
    config_module._settings = None


def test_trails_empty_until_loaded(client):
    r = client.get("/trails?bbox=-94.45,46.95,-94.35,47.05")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection" and gj["features"] == []
    assert "physical suitability only" in gj["disclaimer"].lower()


def test_trails_bad_bbox_is_422(client):
    assert client.get("/trails?bbox=1,2,3").status_code == 422
