"""API contract tests (FC-8) — exercised at the HTTP boundary.

The app's lifespan opens a real SpatiaLite store (temp dir) and a real
OverpassClient; we swap in a fake-overpass-backed RunService before scoring so
no network is touched. Asserts the FC-8 surface: /config exposes weights/
presets/area-cap, /score runs sync, /runs and /runs/{id}/geojson carry the
disclaimer + breakdown, and typed errors map to 422/404 (TLC-4/10).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module
from cairn.service import RunService
from tests.fakes.pipeline import (
    FakeOverpass,
    fake_terrain_factory,
    synthetic_elements,
)

TEST_BBOX = [0.0, 0.0, 0.02, 0.012]  # small -> scores synchronously


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None  # force fresh Settings at lifespan
    from cairn.api import app

    with TestClient(app) as c:
        # Swap the scoring service for one backed by canned data.
        app.state.service = RunService(
            app.state.store,
            app.state.settings,
            overpass=FakeOverpass(synthetic_elements()),
            terrain_factory=fake_terrain_factory(slope_deg=3.0),
        )
        yield c
    config_module._settings = None


def test_config_exposes_ui_subset(client):
    r = client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert set(body["default_weights"]) == {
        "flatness",
        "water_proximity",
        "tree_cover",
        "solitude",
        "road_character",
    }
    assert "presets" in body and "dispersed" in body["presets"]
    assert body["max_bbox_area_km2"] == 2500.0
    assert body["distinct_radius_m"] == 400.0
    assert "physical suitability only" in body["disclaimer"].lower()


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_score_sync_then_fetch_and_geojson(client):
    r = client.post("/score", json={"bbox": TEST_BBOX})
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "done"
    assert run["candidate_count"] > 0
    assert 1 <= run["distinct_count"] <= run["candidate_count"]
    assert "physical suitability only" in run["disclaimer"].lower()
    run_id = run["id"]

    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200 and r.json()["status"] == "done"

    r = client.get(f"/runs/{run_id}/geojson")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    assert len(gj["features"]) > 0
    props = gj["features"][0]["properties"]
    assert props["contributions"] and "total" in props


def test_runs_list(client):
    client.post("/score", json={"bbox": TEST_BBOX})
    r = client.get("/runs")
    assert r.status_code == 200
    assert len(r.json()["runs"]) >= 1


def test_bbox_too_large_returns_422_and_no_run(client):
    r = client.post("/score", json={"bbox": [0.0, 0.0, 1.0, 1.0]})  # ~12000 km²
    assert r.status_code == 422
    assert r.json()["error"] == "BboxTooLarge"
    # No run row was created (validation precedes creation — TLC-4).
    assert client.get("/runs").json()["runs"] == []


def test_invalid_weights_returns_422(client):
    r = client.post(
        "/score",
        json={"bbox": TEST_BBOX, "weights": {"flatness": 0, "water_proximity": 0,
                                             "tree_cover": 0, "solitude": 0}},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "InvalidWeights"


def test_unknown_run_returns_404(client):
    r = client.get("/runs/999999")
    assert r.status_code == 404
    assert r.json()["error"] == "RunNotFound"


def test_static_ui_served_and_does_not_shadow_api(client):
    # FC-9: the static UI is served at / without shadowing the JSON API.
    r = client.get("/")
    assert r.status_code == 200
    assert "Cairn" in r.text and "disclaimer" in r.text.lower()
    assert client.get("/app.js").status_code == 200
    assert client.get("/app.css").status_code == 200
    # API routes still win over the static mount.
    assert client.get("/config").json()["max_bbox_area_km2"] == 2500.0
