"""Land-overlay store + API (display-only reference layers — TLC-3).

Builds a tiny SpatiaLite overlay DB and asserts: layers() reports what's
present with categories; query() bbox-clips and carries the display-only
disclaimer; an absent DB is inert (the app runs without overlays).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module
from cairn.config import Settings
from cairn.store.overlays import OverlayStore
from cairn.store.types import Bbox


def _build_overlays_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    con.execute("SELECT InitSpatialMetaData(1)")
    con.execute(
        "CREATE TABLE overlay_features ("
        "fid INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT, category TEXT, name TEXT)"
    )
    con.execute("SELECT AddGeometryColumn('overlay_features','geom',4326,'MULTIPOLYGON','XY')")
    con.execute("SELECT CreateSpatialIndex('overlay_features','geom')")
    con.execute(
        "INSERT INTO overlay_features(layer, category, name, geom) VALUES "
        "('ownership','Tax Forfeit','Testco', "
        "CastToMulti(GeomFromText('POLYGON((0 0,0.01 0,0.01 0.01,0 0.01,0 0))',4326)))"
    )
    con.execute(
        "INSERT INTO overlay_features(layer, category, name, geom) VALUES "
        "('state_park','State Park','Far Park', "
        "CastToMulti(GeomFromText('POLYGON((5 5,5.01 5,5.01 5.01,5 5.01,5 5))',4326)))"
    )
    # Point layer (campgrounds) lives in overlay_points, with a reservation url.
    con.execute(
        "CREATE TABLE overlay_points ("
        "fid INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT, category TEXT, name TEXT, "
        "url TEXT, meta TEXT)"
    )
    con.execute("SELECT AddGeometryColumn('overlay_points','geom',4326,'POINT','XY')")
    con.execute("SELECT CreateSpatialIndex('overlay_points','geom')")
    con.execute(
        "INSERT INTO overlay_points(layer, category, name, url, meta, geom) VALUES "
        "('campground','National / Federal','Faunce CG','https://recreation.gov/x', "
        "'{\"sites\": 50, \"electric\": true, \"max_rv_ft\": 30}', "
        "GeomFromText('POINT(0.005 0.005)',4326))"
    )
    con.commit()
    con.close()


@pytest.fixture
def overlay_settings(tmp_path):
    db = tmp_path / "overlays.db"
    _build_overlays_db(str(db))
    return Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=db)


def _build_shape_db(path: str) -> None:
    """An overlays DB exercising get_shape: a national forest whose single
    MultiPolygon row holds two disjoint parts, and a state forest whose shape
    spans TWO rows under one name (the UNION case)."""
    con = sqlite3.connect(path)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    con.execute("SELECT InitSpatialMetaData(1)")
    con.execute(
        "CREATE TABLE overlay_features ("
        "fid INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT, category TEXT, name TEXT)"
    )
    con.execute("SELECT AddGeometryColumn('overlay_features','geom',4326,'MULTIPOLYGON','XY')")
    con.execute("SELECT CreateSpatialIndex('overlay_features','geom')")
    # One MultiPolygon row, two disjoint parts spanning lon 0..3.
    con.execute(
        "INSERT INTO overlay_features(layer, category, name, geom) VALUES "
        "('national_forest','National Forest','Superior', "
        "GeomFromText('MULTIPOLYGON("
        "((0 0,1 0,1 1,0 1,0 0)),((2 2,3 2,3 3,2 3,2 2)))',4326))"
    )
    # Two rows, one name -> must UNION; together they span lon 10..12.
    con.execute(
        "INSERT INTO overlay_features(layer, category, name, geom) VALUES "
        "('state_forest','State Forest','Split SF', "
        "CastToMulti(GeomFromText('POLYGON((10 10,11 10,11 11,10 11,10 10))',4326)))"
    )
    con.execute(
        "INSERT INTO overlay_features(layer, category, name, geom) VALUES "
        "('state_forest','State Forest','Split SF', "
        "CastToMulti(GeomFromText('POLYGON((11.5 10,12 10,12 11,11.5 11,11.5 10))',4326)))"
    )
    con.commit()
    con.close()


@pytest.fixture
def shape_settings(tmp_path):
    db = tmp_path / "overlays.db"
    _build_shape_db(str(db))
    return Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=db)


async def test_get_shape_unions_multipolygon_parts(shape_settings):
    s = await OverlayStore.open(shape_settings)
    shape = await s.get_shape("national_forest", "Superior")
    assert shape is not None
    assert shape["properties"] == {"layer": "national_forest", "name": "Superior"}
    # bbox spans BOTH disjoint parts (0,0)-(3,3), not just one.
    w, sth, e, n = shape["bbox"]
    assert w == pytest.approx(0.0) and sth == pytest.approx(0.0)
    assert e == pytest.approx(3.0) and n == pytest.approx(3.0)
    assert shape["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert "context only" in shape["disclaimer"].lower()
    await s.close()


async def test_get_shape_unions_multiple_rows(shape_settings):
    s = await OverlayStore.open(shape_settings)
    shape = await s.get_shape("state_forest", "Split SF")
    assert shape is not None
    # The two rows union into one shape spanning lon 10..12.
    w, sth, e, n = shape["bbox"]
    assert w == pytest.approx(10.0) and e == pytest.approx(12.0)
    assert sth == pytest.approx(10.0) and n == pytest.approx(11.0)
    await s.close()


async def test_get_shape_missing_returns_none(shape_settings):
    s = await OverlayStore.open(shape_settings)
    assert await s.get_shape("national_forest", "Nope") is None
    assert await s.get_shape("state_park", "Superior") is None  # wrong layer
    await s.close()


async def test_get_shape_inert_without_db(tmp_path):
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=tmp_path / "nope.db")
    )
    assert await s.get_shape("national_forest", "Superior") is None
    await s.close()


def test_api_shape_endpoint(tmp_path, monkeypatch):
    db = tmp_path / "overlays.db"
    _build_shape_db(str(db))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CAIRN_OVERLAYS_DB_PATH", str(db))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.get("/overlays/national_forest/shape?name=Superior")
        assert r.status_code == 200
        body = r.json()
        assert body["bbox"][2] == pytest.approx(3.0)  # spans both parts
        assert body["geometry"]["type"] in ("Polygon", "MultiPolygon")

        # missing name -> 404
        r = c.get("/overlays/national_forest/shape?name=Nope")
        assert r.status_code == 404
        assert r.json()["error"] == "ShapeNotFound"
    config_module._settings = None


async def test_get_clip_geometry(shape_settings):
    s = await OverlayStore.open(shape_settings)
    # An existing shape -> non-empty WKB (SRID 4326), clipped to the bbox.
    wkb = await s.get_clip_geometry("national_forest", "Superior", Bbox(0, 0, 3, 3))
    assert isinstance(wkb, (bytes, bytearray)) and len(wkb) > 0
    # Unioned across rows works for clipping too.
    assert await s.get_clip_geometry("state_forest", "Split SF", Bbox(10, 10, 12, 11)) is not None
    # Missing name -> None (the API maps that to a 422 unknown-clip error).
    assert await s.get_clip_geometry("national_forest", "Nope", Bbox(0, 0, 3, 3)) is None
    await s.close()


async def test_get_clip_geometry_inert_without_db(tmp_path):
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=tmp_path / "nope.db")
    )
    assert await s.get_clip_geometry("national_forest", "Superior", Bbox(0, 0, 3, 3)) is None
    await s.close()


def test_api_score_unknown_clip_is_422(tmp_path, monkeypatch):
    # A clip referencing a shape the overlays store can't resolve fails fast with
    # 422 — BEFORE any scoring/fetch (here the store is inert, so every name is
    # unknown). Proves the clip is wired into /score and validated up front.
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.post(
            "/score",
            json={
                "bbox": [0.0, 0.0, 0.01, 0.01],
                "clip": {"layer": "national_forest", "name": "Nope"},
            },
        )
        assert r.status_code == 422
        assert "clip" in r.json()["detail"].lower()
    config_module._settings = None


def test_api_shape_endpoint_404_when_inert(tmp_path, monkeypatch):
    # No overlays.db -> store inert -> shape lookups 404 (not 500).
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.get("/overlays/state_park/shape?name=Anything")
        assert r.status_code == 404
    config_module._settings = None


async def test_layers_report_categories(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    assert s.available
    layers = await s.layers()
    by_id = {layer["id"]: layer for layer in layers}
    assert "ownership" in by_id and "state_park" in by_id
    assert "Tax Forfeit" in by_id["ownership"]["categories"]
    # LAYER_ORDER is campground, national_forest, state_forest, state_park,
    # ownership; only present layers show, in that order.
    assert [layer["id"] for layer in layers] == ["campground", "state_park", "ownership"]
    await s.close()


async def test_point_layer_query_returns_marker_with_url(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    layers = {layer["id"]: layer for layer in await s.layers()}
    assert layers["campground"]["geom_type"] == "point"
    assert layers["state_park"]["geom_type"] == "polygon"
    fc = await s.query("campground", Bbox(0, 0, 0.01, 0.01))
    assert fc["geom_type"] == "point"
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "Point"
    assert feat["properties"]["name"] == "Faunce CG"
    assert feat["properties"]["url"] == "https://recreation.gov/x"
    # Phase-1 enrichment: meta passes through as a parsed object.
    assert feat["properties"]["meta"]["sites"] == 50
    assert feat["properties"]["meta"]["electric"] is True
    await s.close()


async def test_nearest_campgrounds(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    # The fixture campground sits at (0.005, 0.005), with meta.
    near = await s.nearest_campgrounds(0.005, 0.005, limit=5)
    assert len(near) == 1 and near[0]["name"] == "Faunce CG"
    assert near[0]["distance_m"] < 100  # essentially on top of it
    assert near[0]["meta"]["sites"] == 50  # full meta comes through
    assert near[0]["lon"] == pytest.approx(0.005) and near[0]["lat"] == pytest.approx(0.005)
    # Nothing within the search radius far away.
    assert await s.nearest_campgrounds(10.0, 10.0) == []
    await s.close()


async def test_query_clips_to_bbox(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    fc = await s.query("ownership", Bbox(-0.005, -0.005, 0.005, 0.005))
    assert fc["features"], "parcel intersecting the bbox should be returned"
    assert fc["features"][0]["properties"]["category"] == "Tax Forfeit"
    assert "context only" in fc["disclaimer"].lower()
    # The far-away park is not in this bbox.
    empty = await s.query("state_park", Bbox(-0.005, -0.005, 0.005, 0.005))
    assert empty["features"] == []
    await s.close()


async def test_absent_db_is_inert(tmp_path):
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=tmp_path / "nope.db")
    )
    assert not s.available
    assert await s.layers() == []
    fc = await s.query("ownership", Bbox(0, 0, 1, 1))
    assert fc["features"] == [] and "context only" in fc["disclaimer"].lower()
    await s.close()


def test_api_overlays_endpoints_inert_without_db(tmp_path, monkeypatch):
    # The API client fixture has no overlays.db -> endpoints respond, inert.
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.get("/overlays")
        assert r.status_code == 200
        body = r.json()
        assert body["layers"] == []
        assert "context only" in body["disclaimer"].lower()

        r = c.get("/overlays/ownership?bbox=-1,-1,1,1")
        assert r.status_code == 200
        assert r.json()["features"] == []

        # bad bbox -> 422 (typed ValidationError)
        assert c.get("/overlays/ownership?bbox=foo").status_code == 422

        # nearby-campgrounds responds (empty without an overlays.db)
        r = c.get("/nearby-campgrounds?lat=47.5&lon=-94.1")
        assert r.status_code == 200 and r.json()["campgrounds"] == []
    config_module._settings = None
