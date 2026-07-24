"""Land-overlay store + API (display-only reference layers — TLC-3).

Builds a tiny SpatiaLite overlay DB and asserts: layers() reports what's
present with categories; query() bbox-clips and carries the display-only
disclaimer; an absent DB is inert (the app runs without overlays).
"""

from __future__ import annotations

import json
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
    # Cell-coverage polygon layer (display-only): two quality tiers as
    # category-tagged hexes near origin, plus a far Patchy hex to prove bbox clip.
    for cat, poly in [
        ("Solid", "POLYGON((0 0,0.008 0,0.008 0.008,0 0.008,0 0))"),
        ("Fast", "POLYGON((0.002 0.002,0.01 0.002,0.01 0.01,0.002 0.01,0.002 0.002))"),
        ("Patchy", "POLYGON((6 6,6.01 6,6.01 6.01,6 6.01,6 6))"),
    ]:
        con.execute(
            "INSERT INTO overlay_features(layer, category, name, geom) "
            f"VALUES ('cell_coverage', ?, NULL, CastToMulti(GeomFromText('{poly}',4326)))",
            (cat,),
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
    # Resupply 'amenity' layer (sheet-only tie-in): one nearest-of-each-category
    # near (0.005,0.005), with a SECOND, farther fuel to prove the per-category
    # window picks the closest; a town; and a far point excluded by max_km.
    for cat, name, meta, lon, lat in [
        ("fuel", "Close Gas", '{"brand": "Holiday"}', 0.005, 0.005),
        ("fuel", "Far Gas", None, 0.02, 0.02),
        ("groceries", "IGA", '{"shop": "supermarket"}', 0.006, 0.006),
        ("water", "Spigot", None, 0.004, 0.004),
        ("dump_station", "Dump", '{"fee": true}', 0.007, 0.007),
        ("propane", "Propane Co", None, 0.0055, 0.0055),
        ("town", "Walker", '{"place": "town"}', 0.003, 0.003),
        ("fuel", "Way Off", None, 5.0, 5.0),  # > max_km away
    ]:
        con.execute(
            "INSERT INTO overlay_points(layer, category, name, meta, geom) "
            f"VALUES ('amenity', ?, ?, ?, GeomFromText('POINT({lon} {lat})', 4326))",
            (cat, name, meta),
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
    # Points for forest_summary: 2 dispersed + 1 campground inside Superior part1
    # (the 0..1 square), 1 dispersed far outside any forest.
    con.execute(
        "CREATE TABLE overlay_points ("
        "fid INTEGER PRIMARY KEY AUTOINCREMENT, layer TEXT, category TEXT, name TEXT, "
        "url TEXT, meta TEXT)"
    )
    con.execute("SELECT AddGeometryColumn('overlay_points','geom',4326,'POINT','XY')")
    con.execute("SELECT CreateSpatialIndex('overlay_points','geom')")
    for layer, lon, lat in [
        ("dispersed", 0.5, 0.5),
        ("dispersed", 0.6, 0.6),
        ("campground", 0.4, 0.4),
        ("dispersed", 20.0, 20.0),  # outside every forest
    ]:
        con.execute(
            "INSERT INTO overlay_points(layer, category, name, geom) "
            f"VALUES (?, 'x', 'x', GeomFromText('POINT({lon} {lat})', 4326))",
            (layer,),
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


# --- FC-16: forest browse (Explore tab) -----------------------------------
async def test_list_forests_nf_and_sf_with_area_and_bbox(shape_settings):
    s = await OverlayStore.open(shape_settings)
    forests = await s.list_forests()
    names = {(f["layer"], f["name"]) for f in forests}
    assert ("national_forest", "Superior") in names
    assert ("state_forest", "Split SF") in names
    # No non-forest layers leak in (no state_park / ownership).
    assert all(f["layer"] in ("national_forest", "state_forest") for f in forests)
    sup = next(f for f in forests if f["name"] == "Superior")
    assert sup["area_km2"] > 0
    # bbox spans both disjoint parts (0,0)-(3,3).
    assert sup["bbox"][0] == pytest.approx(0.0) and sup["bbox"][2] == pytest.approx(3.0)
    # Split SF unions its two rows -> bbox spans lon 10..12.
    split = next(f for f in forests if f["name"] == "Split SF")
    assert split["bbox"][0] == pytest.approx(10.0) and split["bbox"][2] == pytest.approx(12.0)
    await s.close()


async def test_forest_summary_counts_points_inside(shape_settings):
    s = await OverlayStore.open(shape_settings)
    summ = await s.forest_summary("national_forest", "Superior")
    assert summ is not None
    # 2 dispersed + 1 campground inside; the far (20,20) dispersed is excluded.
    assert summ["dispersed"] == 2
    assert summ["campgrounds"] == 1
    # Split SF contains no points.
    split = await s.forest_summary("state_forest", "Split SF")
    assert split["dispersed"] == 0 and split["campgrounds"] == 0
    await s.close()


async def test_forest_summary_missing_returns_none(shape_settings):
    s = await OverlayStore.open(shape_settings)
    assert await s.forest_summary("national_forest", "Nope") is None
    await s.close()


def _build_county_forest_db(path: str) -> None:
    """A minimal overlays DB with one county_forest row (WI's county forests are
    county-owned dispersed-camping land, a first-class forest layer alongside
    NF/SF)."""
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
        "('county_forest', NULL, 'Douglas County Forest', "
        "CastToMulti(GeomFromText('POLYGON((0 0,1 0,1 1,0 1,0 0))',4326)))"
    )
    con.commit()
    con.close()


async def test_county_forest_is_a_browsable_forest(tmp_path):
    """A county_forest row is a first-class forest: it has a full outline
    (get_shape) and appears in the Explore forest list (list_forests via
    FOREST_LAYERS)."""
    db = tmp_path / "overlays.db"
    _build_county_forest_db(str(db))
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=db)
    )
    shape = await s.get_shape("county_forest", "Douglas County Forest")
    assert shape is not None
    assert shape["properties"] == {"layer": "county_forest", "name": "Douglas County Forest"}
    forests = await s.list_forests()
    assert ("county_forest", "Douglas County Forest") in {
        (f["layer"], f["name"]) for f in forests
    }
    await s.close()


def test_forests_api_list_and_summary(tmp_path, monkeypatch):
    db = tmp_path / "overlays.db"
    _build_shape_db(str(db))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CAIRN_OVERLAYS_DB_PATH", str(db))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as client:
        forests = client.get("/forests").json()["forests"]
        assert {f["name"] for f in forests} == {"Superior", "Split SF"}
        assert all("has_mvum" in f for f in forests)  # no MVUM loaded -> all False
        assert all(f["has_mvum"] is False for f in forests)
        r = client.get(
            "/forests/summary", params={"layer": "national_forest", "name": "Superior"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dispersed"] == 2 and body["campgrounds"] == 1
        # Unknown forest -> 404.
        assert (
            client.get(
                "/forests/summary", params={"layer": "national_forest", "name": "Nope"}
            ).status_code
            == 404
        )
    config_module._settings = None


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


async def test_get_public_land_geometry(overlay_settings):
    # The fixture has one ownership parcel (Tax Forfeit) over 0..0.01.
    s = await OverlayStore.open(overlay_settings)
    wkb = await s.get_public_land_geometry(Bbox(0, 0, 0.01, 0.01))
    assert isinstance(wkb, (bytes, bytearray)) and len(wkb) > 0
    # A bbox with no ownership parcels -> None (no coverage), distinct from empty.
    assert await s.get_public_land_geometry(Bbox(5, 5, 6, 6)) is None
    await s.close()


async def test_get_public_land_geometry_inert_without_db(tmp_path):
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=tmp_path / "nope.db")
    )
    assert await s.get_public_land_geometry(Bbox(0, 0, 1, 1)) is None
    await s.close()


def test_api_score_public_only_no_coverage_is_422(tmp_path, monkeypatch):
    # public_only with no ownership data (inert store) -> 422 up front, never a
    # silent empty result. Proves the opt-in coverage guard is wired into
    # /score/view (shared _resolve_clips) and validated before any work.
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.post("/score/view", json={"bbox": [0.0, 0.0, 0.01, 0.01], "public_only": True})
        assert r.status_code == 422
        assert "public-land" in r.json()["detail"].lower()
    config_module._settings = None


def test_api_mvum_overlays(tmp_path, monkeypatch):
    mdir = tmp_path / "mvum"
    mdir.mkdir()
    (mdir / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (mdir / "manifest.json").write_text(
        json.dumps([{"key": "x", "name": "X RD", "image": "x.png",
                     "bounds": [1.0, 2.0, 3.0, 4.0]}])
    )
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        ov = c.get("/mvum").json()["overlays"]
        assert len(ov) == 1 and ov[0]["key"] == "x"
        assert ov[0]["url"] == "/mvum/x.png" and ov[0]["bounds"] == [1.0, 2.0, 3.0, 4.0]
        img = c.get("/mvum/x.png")
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert c.get("/mvum/nope.png").status_code == 404  # missing
        assert c.get("/mvum/bad-key.png").status_code == 404  # bad key (guarded)
    config_module._settings = None


def test_api_mvum_empty_without_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        assert c.get("/mvum").json()["overlays"] == []
    config_module._settings = None


def test_api_score_unknown_clip_is_422(tmp_path, monkeypatch):
    # A clip referencing a shape the overlays store can't resolve fails fast with
    # 422 — BEFORE any scoring/fetch (here the store is inert, so every name is
    # unknown). Proves the clip is wired into /score/view and validated up front.
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.post(
            "/score/view",
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
    # cell_coverage reports its quality tiers as legend categories.
    assert by_id["cell_coverage"]["geom_type"] == "polygon"
    assert by_id["cell_coverage"]["categories"] == ["Fast", "Patchy", "Solid"]
    # LAYER_ORDER ends ..., ownership, cell_coverage; only present layers show,
    # in that order.
    assert [layer["id"] for layer in layers] == [
        "campground", "state_park", "ownership", "cell_coverage"
    ]
    await s.close()


async def test_cell_coverage_query_clips_and_carries_category(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    fc = await s.query("cell_coverage", Bbox(0, 0, 0.01, 0.01))
    assert fc["geom_type"] == "polygon"
    cats = sorted(f["properties"]["category"] for f in fc["features"])
    assert cats == ["Fast", "Solid"]  # both near-origin hexes; the far Patchy one clipped out
    assert all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in fc["features"])
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


async def test_nearest_amenities(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    out = await s.nearest_amenities(0.005, 0.005)
    res = out["resupply"]
    # One nearest of each resupply category; the closer of the two fuels wins.
    assert res["fuel"]["name"] == "Close Gas"
    assert res["fuel"]["meta"]["brand"] == "Holiday"  # meta passes through
    assert res["fuel"]["distance_m"] < 100  # essentially on top of it
    assert res["groceries"]["name"] == "IGA"
    assert res["water"]["name"] == "Spigot"
    assert res["dump_station"]["name"] == "Dump"
    assert res["propane"]["name"] == "Propane Co"
    # Town is surfaced separately, not as a resupply category.
    assert out["town"]["name"] == "Walker"
    assert "town" not in res
    assert res["fuel"]["lon"] == pytest.approx(0.005) and res["fuel"]["lat"] == pytest.approx(0.005)
    await s.close()


async def test_nearest_amenities_max_km_excludes_far(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    # (3,3) sits far from BOTH clusters (the 0.00x amenities and the lone (5,5)
    # "Way Off" fuel ~300 km away); a tight radius finds nothing in any category.
    out = await s.nearest_amenities(3.0, 3.0, max_km=50.0)
    assert all(v is None for v in out["resupply"].values())
    assert out["town"] is None
    # Widening the radius reaches the lone far fuel but still no other category.
    wide = await s.nearest_amenities(5.0, 5.0, max_km=10.0)
    assert wide["resupply"]["fuel"]["name"] == "Way Off"  # at (5,5), distance ~0
    assert wide["resupply"]["groceries"] is None  # nearest groceries is ~785 km off
    await s.close()


async def test_nearest_amenities_inert_without_db(tmp_path):
    s = await OverlayStore.open(
        Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=tmp_path / "nope.db")
    )
    out = await s.nearest_amenities(47.5, -94.1)
    assert out["town"] is None
    assert set(out["resupply"]) == {"fuel", "groceries", "water", "dump_station", "propane"}
    assert all(v is None for v in out["resupply"].values())
    await s.close()


async def test_amenity_layer_hidden_from_layers(overlay_settings):
    # The amenity layer is sheet-only (served via /nearby-resupply), so it must
    # NOT appear as a browsable overlay in the Land panel.
    s = await OverlayStore.open(overlay_settings)
    assert "amenity" not in {layer["id"] for layer in await s.layers()}
    await s.close()


def test_api_nearby_resupply(tmp_path, monkeypatch):
    db = tmp_path / "overlays.db"
    _build_overlays_db(str(db))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CAIRN_OVERLAYS_DB_PATH", str(db))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        r = c.get("/nearby-resupply", params={"lat": 0.005, "lon": 0.005})
        assert r.status_code == 200
        body = r.json()
        assert body["resupply"]["fuel"]["name"] == "Close Gas"
        assert body["town"]["name"] == "Walker"
    config_module._settings = None


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

        # nearby-resupply responds inert too (all categories None)
        r = c.get("/nearby-resupply?lat=47.5&lon=-94.1")
        assert r.status_code == 200
        body = r.json()
        assert body["town"] is None
        assert all(v is None for v in body["resupply"].values())
    config_module._settings = None
