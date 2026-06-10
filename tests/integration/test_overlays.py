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
    con.commit()
    con.close()


@pytest.fixture
def overlay_settings(tmp_path):
    db = tmp_path / "overlays.db"
    _build_overlays_db(str(db))
    return Settings(_env_file=None, data_dir=tmp_path, overlays_db_path=db)


async def test_layers_report_categories(overlay_settings):
    s = await OverlayStore.open(overlay_settings)
    assert s.available
    layers = await s.layers()
    by_id = {layer["id"]: layer for layer in layers}
    assert "ownership" in by_id and "state_park" in by_id
    assert "Tax Forfeit" in by_id["ownership"]["categories"]
    # national_forest sorts before ownership in LAYER_ORDER; only present ones show.
    assert [layer["id"] for layer in layers] == ["state_park", "ownership"]
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
    config_module._settings = None
