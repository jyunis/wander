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
        # A fake geocoder so /score's best-effort place labeling never hits the
        # network (geocode tests swap in their own / None as needed).
        app.state.geocoder = _FakeGeocoder([])
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
        "remoteness",
        "road_end",
    }
    assert "presets" in body and "dispersed" in body["presets"]
    assert body["max_bbox_area_km2"] == 2500.0
    assert body["distinct_radius_m"] == 300.0
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
    assert run["place"] == "Test Locality"  # best-effort reverse-geocode label
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


def test_delete_run_cascades(client):
    run_id = client.post("/score", json={"bbox": TEST_BBOX}).json()["id"]
    assert client.get(f"/runs/{run_id}").status_code == 200
    r = client.delete(f"/runs/{run_id}")
    assert r.status_code == 200 and r.json()["deleted"] is True
    # gone (candidates/scores cascade with it) -> 404, and re-deleting is a no-op
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.delete(f"/runs/{run_id}").json()["deleted"] is False


def test_run_roads_geojson(client):
    # The drivable-ways overlay: a LineString FeatureCollection of the run's
    # scored roads (OSM ∪ MVUM), carrying the TLC-3 disclaimer.
    run_id = client.post("/score", json={"bbox": TEST_BBOX}).json()["id"]
    r = client.get(f"/runs/{run_id}/roads")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    assert len(gj["features"]) > 0
    geom = gj["features"][0]["geometry"]
    assert geom["type"] == "LineString" and len(geom["coordinates"]) >= 2
    props = gj["features"][0]["properties"]
    assert "forest_road" in props and "rough" in props


def test_roads_unknown_run_returns_404(client):
    r = client.get("/runs/999999/roads")
    assert r.status_code == 404
    assert r.json()["error"] == "RunNotFound"


def test_roads_viewport_geojson(client):
    # The viewport roads overlay: drivable ways in a bbox, independent of any
    # run. Score first so the store has road data for the area.
    client.post("/score", json={"bbox": TEST_BBOX})
    bbox = ",".join(str(x) for x in TEST_BBOX)
    r = client.get(f"/roads?bbox={bbox}")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    assert "physical suitability only" in gj["disclaimer"].lower()
    assert "truncated" in gj and len(gj["features"]) > 0
    geom = gj["features"][0]["geometry"]
    assert geom["type"] == "LineString" and len(geom["coordinates"]) >= 2
    props = gj["features"][0]["properties"]
    assert "forest_road" in props and "rough" in props


def test_roads_viewport_empty_area(client):
    # An unscored bbox has no ingested road data -> empty, not an error.
    gj = client.get("/roads?bbox=10,10,10.01,10.01").json()
    assert gj["type"] == "FeatureCollection" and gj["features"] == []


def test_roads_viewport_bad_bbox_422(client):
    assert client.get("/roads?bbox=1,2,3").status_code == 422


class _FakeGeocoder:
    def __init__(self, results, place="Test Locality"):
        self._r = results
        self._place = place
        self.last = None

    async def search(self, q, viewbox=None):
        self.last = (q, viewbox)
        return self._r

    async def reverse(self, lat, lon):
        return self._place

    async def reverse_label(self, lat, lon):
        return self._place


def test_geocode_returns_results(client):
    from cairn.api import app

    fake = _FakeGeocoder(
        [{"display_name": "Chippewa National Forest", "lat": 47.5, "lon": -94.2,
          "bbox": [-94.7, 47.0, -93.7, 48.0]}]
    )
    app.state.geocoder = fake
    r = client.get("/geocode?q=chippewa&viewbox=-95,46,-93,48")
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["lat"] == 47.5 and res[0]["bbox"][0] == -94.7
    assert fake.last == ("chippewa", "-95,46,-93,48")  # query + viewbox forwarded


def test_geocode_empty_q_is_422(client):
    r = client.get("/geocode?q=%20")
    assert r.status_code == 422


def test_geocode_disabled_is_404(client):
    from cairn.api import app

    app.state.geocoder = None
    r = client.get("/geocode?q=anything")
    assert r.status_code == 404
    assert r.json()["error"] == "GeocodeDisabled"


def test_geocode_normalize_reorders_bbox():
    # Nominatim boundingbox is [south, north, west, east] strings; we expose
    # [min_lon, min_lat, max_lon, max_lat] and drop entries without coords.
    from cairn.clients.nominatim import GeocodeClient

    out = GeocodeClient._normalize(
        {"display_name": "X", "lat": "47.5", "lon": "-94.2",
         "boundingbox": ["47.0", "48.0", "-94.7", "-93.7"]}
    )
    assert out["lat"] == 47.5 and out["lon"] == -94.2
    assert out["bbox"] == [-94.7, 47.0, -93.7, 48.0]
    assert GeocodeClient._normalize({"display_name": "no coords"}) is None


def test_saved_places_crud(client):
    # Save a dispersed site with its score snapshot.
    r = client.post(
        "/saved",
        json={"kind": "site", "name": "Quiet spur", "lat": 47.5, "lon": -94.2,
              "payload": {"total": 0.78}},
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["payload"]["total"] == 0.78 and r.json()["created_at"]
    # Lists it.
    saved = client.get("/saved").json()["saved"]
    assert any(s["id"] == sid and s["name"] == "Quiet spur" for s in saved)
    # Deletes it.
    assert client.delete(f"/saved/{sid}").json()["deleted"] is True
    assert all(s["id"] != sid for s in client.get("/saved").json()["saved"])


def test_saved_bad_kind_is_422(client):
    r = client.post("/saved", json={"kind": "bogus", "lat": 1.0, "lon": 2.0})
    assert r.status_code == 422


def test_saved_patch_renames(client):
    sid = client.post(
        "/saved", json={"kind": "site", "name": "old", "lat": 1.0, "lon": 2.0}
    ).json()["id"]
    r = client.patch(f"/saved/{sid}", json={"name": "Pine flat"})
    assert r.status_code == 200 and r.json()["name"] == "Pine flat"
    assert any(s["name"] == "Pine flat" for s in client.get("/saved").json()["saved"])
    assert client.patch("/saved/999999", json={"name": "x"}).status_code == 404


def test_saved_rating_and_partial_update(client):
    # Save with a star rating.
    r = client.post(
        "/saved", json={"kind": "site", "name": "Glove Lake", "lat": 47.79, "lon": -94.1, "rating": 4}
    )
    assert r.status_code == 201 and r.json()["rating"] == 4
    sid = r.json()["id"]
    # PATCH rating only — name is preserved (partial update doesn't clobber).
    r = client.patch(f"/saved/{sid}", json={"rating": 5})
    assert r.status_code == 200 and r.json()["rating"] == 5 and r.json()["name"] == "Glove Lake"
    # PATCH name only — rating is preserved.
    r = client.patch(f"/saved/{sid}", json={"name": "Glove"})
    assert r.json()["name"] == "Glove" and r.json()["rating"] == 5
    # PATCH note only — name + rating preserved.
    r = client.patch(f"/saved/{sid}", json={"note": "hilltop, good swimming"})
    assert r.json()["note"] == "hilltop, good swimming"
    assert r.json()["name"] == "Glove" and r.json()["rating"] == 5
    # Clearing the rating explicitly is allowed (and leaves the note intact).
    r = client.patch(f"/saved/{sid}", json={"rating": None})
    assert r.json()["rating"] is None and r.json()["note"] == "hilltop, good swimming"
    # Out-of-range rating -> 422.
    assert client.post(
        "/saved", json={"kind": "site", "lat": 1.0, "lon": 2.0, "rating": 9}
    ).status_code == 422


def test_suggest_name_uses_reverse_when_no_local_feature(client):
    # No scored run -> no osm_features near the point -> reverse-geocode fallback.
    from cairn.api import app

    class _RG:
        async def search(self, *a, **k):
            return []

        async def reverse(self, lat, lon):
            return "Side Lake"

    app.state.geocoder = _RG()
    r = client.get("/suggest-name?lat=47.5&lon=-94.1")
    assert r.status_code == 200 and r.json()["name"] == "Side Lake"


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


def test_trip_crud(client):
    r = client.post(
        "/trips",
        json={"name": "Labor Day", "start_date": "2026-09-05", "end_date": "2026-09-07"},
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    row = next(t for t in client.get("/trips").json()["trips"] if t["id"] == tid)
    assert row["member_count"] == 0 and row["name"] == "Labor Day"
    got = client.get(f"/trips/{tid}").json()
    assert got["members"] == [] and "physical suitability only" in got["disclaimer"].lower()
    # PATCH name only — dates preserved (partial update).
    r = client.patch(f"/trips/{tid}", json={"name": "Labor Day weekend"})
    assert r.status_code == 200 and r.json()["name"] == "Labor Day weekend"
    assert r.json()["start_date"] == "2026-09-05"
    assert client.delete(f"/trips/{tid}").json()["deleted"] is True
    assert client.get(f"/trips/{tid}").status_code == 404


def test_trip_name_required_and_date_validation(client):
    assert client.post("/trips", json={"name": "  "}).status_code == 422
    assert client.post("/trips", json={"name": "x", "start_date": "nope"}).status_code == 422
    # end before start -> 422.
    r = client.post(
        "/trips", json={"name": "x", "start_date": "2026-09-07", "end_date": "2026-09-05"}
    )
    assert r.status_code == 422


def test_trip_members_flow(client):
    tid = client.post("/trips", json={"name": "T"}).json()["id"]
    s1 = client.post(
        "/saved",
        json={"kind": "site", "name": "Spur", "lat": 47.5, "lon": -94.2, "payload": {"total": 0.7}},
    ).json()["id"]
    s2 = client.post(
        "/saved", json={"kind": "campground", "name": "Deer Lake", "lat": 47.6, "lon": -94.0}
    ).json()["id"]

    r = client.post(f"/trips/{tid}/members", json={"saved_id": s1, "role": "primary"})
    assert r.status_code == 201
    trip = client.post(f"/trips/{tid}/members", json={"saved_id": s2, "role": "fallback"}).json()
    assert len(trip["members"]) == 2
    # Hydrated: primary first, carrying its coords from saved_places.
    assert trip["members"][0]["role"] == "primary" and trip["members"][0]["lat"] == 47.5

    # Re-POST upserts the role rather than duplicating.
    r = client.post(f"/trips/{tid}/members", json={"saved_id": s1, "role": "fallback"})
    roles = {m["saved_id"]: m["role"] for m in r.json()["members"]}
    assert roles[s1] == "fallback" and len(r.json()["members"]) == 2

    assert client.delete(f"/trips/{tid}/members/{s1}").json()["removed"] is True
    assert len(client.get(f"/trips/{tid}").json()["members"]) == 1


def test_trip_member_bad_role_and_missing_targets(client):
    tid = client.post("/trips", json={"name": "T"}).json()["id"]
    sid = client.post("/saved", json={"kind": "site", "lat": 1.0, "lon": 2.0}).json()["id"]
    bad = client.post(f"/trips/{tid}/members", json={"saved_id": sid, "role": "bogus"})
    assert bad.status_code == 422
    assert client.post("/trips/999999/members", json={"saved_id": sid}).status_code == 404
    assert client.post(f"/trips/{tid}/members", json={"saved_id": 999999}).status_code == 404


def test_deleting_saved_removes_it_from_trips(client):
    tid = client.post("/trips", json={"name": "T"}).json()["id"]
    sid = client.post("/saved", json={"kind": "site", "lat": 1.0, "lon": 2.0}).json()["id"]
    client.post(f"/trips/{tid}/members", json={"saved_id": sid})
    client.delete(f"/saved/{sid}")
    assert client.get(f"/trips/{tid}").json()["members"] == []


def test_trip_export_gpx_and_kml(client):
    tid = client.post("/trips", json={"name": "Labor Day", "start_date": "2026-09-05"}).json()["id"]
    sid = client.post(
        "/saved", json={"kind": "site", "name": "Quiet spur", "lat": 47.5, "lon": -94.2}
    ).json()["id"]
    client.post(f"/trips/{tid}/members", json={"saved_id": sid, "role": "primary"})

    g = client.get(f"/trips/{tid}/export.gpx")
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("application/gpx+xml")
    assert 'attachment; filename="labor-day.gpx"' in g.headers["content-disposition"]
    assert "<wpt" in g.text and "47.500000" in g.text and "Quiet spur" in g.text

    k = client.get(f"/trips/{tid}/export.kml")
    assert k.status_code == 200
    assert "vnd.google-earth.kml" in k.headers["content-type"]
    assert "<Placemark>" in k.text and "-94.200000,47.500000" in k.text

    assert client.get("/trips/999999/export.gpx").status_code == 404


def test_static_ui_served_and_does_not_shadow_api(client):
    # FC-9: the static UI is served at / without shadowing the JSON API.
    r = client.get("/")
    assert r.status_code == 200
    assert "Cairn" in r.text and "disclaimer" in r.text.lower()
    js = client.get("/app.js")
    assert js.status_code == 200
    # UI assets revalidate so a rebuild isn't masked by a stale browser cache.
    assert js.headers.get("cache-control") == "no-cache"
    assert client.get("/app.css").status_code == 200
    # API routes still win over the static mount.
    assert client.get("/config").json()["max_bbox_area_km2"] == 2500.0
