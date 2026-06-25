"""Trails overlay API (/trails) — display-only hiking trails from Overpass.

Best-effort: bad bbox -> 422, but a too-large bbox or an Overpass failure
yields an empty `degraded` layer, never a 500 (the overlay just shows nothing)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module
from cairn.errors import OverpassTimeout
from tests.fakes.pipeline import FakeOverpass

NCT = [
    {"type": "relation", "id": 9,
     "tags": {"route": "hiking", "name": "North Country Trail"},
     "members": [{"type": "way", "ref": 100,
                  "geometry": [{"lat": 47.0, "lon": -94.40}, {"lat": 47.0, "lon": -94.38}]}]},
    {"type": "way", "id": 200, "tags": {"highway": "footway", "name": "Lakeshore Path"},
     "geometry": [{"lat": 47.02, "lon": -94.30}, {"lat": 47.03, "lon": -94.29}]},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        yield c
    config_module._settings = None


def test_trails_returns_named_route_and_paths(client):
    from cairn.api import app

    app.state.overpass = FakeOverpass([], trail_elements=NCT)
    r = client.get("/trails?bbox=-94.45,46.95,-94.35,47.05")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    names = {f["properties"]["name"] for f in gj["features"]}
    assert {"North Country Trail", "Lakeshore Path"} <= names
    assert all(f["geometry"]["type"] == "LineString" for f in gj["features"])


def test_trails_bad_bbox_is_422(client):
    assert client.get("/trails?bbox=1,2,3").status_code == 422


def test_trails_overpass_failure_is_empty_not_error(client):
    from cairn.api import app

    class _Boom:
        async def fetch_trails(self, bbox):
            raise OverpassTimeout("overpass down")

    app.state.overpass = _Boom()
    r = client.get("/trails?bbox=-94.45,46.95,-94.35,47.05")
    assert r.status_code == 200
    body = r.json()
    assert body["features"] == [] and body["degraded"] is True


def test_trails_oversized_bbox_is_empty_degraded(client):
    # A huge bbox is refused (would be a giant Overpass query) -> empty, not 500.
    r = client.get("/trails?bbox=-100,40,-90,48")
    assert r.status_code == 200
    assert r.json().get("degraded") is True
