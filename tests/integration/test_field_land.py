"""Whose land it is, offline: the field map's land sidecar (display-only — TLC-3).

The field page answers "am I on public land" from this one cached file, with
no signal. What has to hold here, rather than on a phone in a forest:
adjoining parcels arrive DISSOLVED (a trip box of ~800 MN parcels must not ship
as 800 polygons), everything is clipped to the region, and a forest BOUNDARY
never reads as public — only a parcel can.
"""

from __future__ import annotations

import sqlite3
import struct

import pytest
from fastapi.testclient import TestClient

import cairn.config as config_module
from cairn.config import Settings
from cairn.field import LAND_LAYERS, LAND_SIMPLIFY_DEG
from cairn.store.overlays import OverlayStore
from cairn.store.types import Bbox

REGION = Bbox(-0.01, -0.01, 0.03, 0.03)


def _sq(x0, y0, x1, y1):
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


def _build(path: str) -> None:
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
    for layer, cat, name, wkt in [
        # Two adjoining tax-forfeit parcels, same county: must dissolve to one.
        ("ownership", "Tax Forfeit", "Itasca", _sq(0, 0, 0.01, 0.01)),
        ("ownership", "Tax Forfeit", "Itasca", _sq(0.01, 0, 0.02, 0.01)),
        ("ownership", "Tribal", "Itasca", _sq(0, 0.015, 0.01, 0.025)),
        # The forest boundary around all of it — a line, not an ownership claim.
        ("national_forest", None, "Chippewa National Forest", _sq(-0.05, -0.05, 0.05, 0.05)),
        # Outside the region entirely.
        ("ownership", "State", "Far", _sq(5, 5, 5.01, 5.01)),
        # Not a land layer the field map carries.
        ("cell_coverage", "Solid", None, _sq(0, 0, 0.01, 0.01)),
    ]:
        con.execute(
            "INSERT INTO overlay_features(layer, category, name, geom) "
            f"VALUES (?, ?, ?, CastToMulti(GeomFromText('{wkt}', 4326)))",
            (layer, cat, name),
        )
    con.commit()
    con.close()


@pytest.fixture
async def overlays(tmp_path):
    path = tmp_path / "overlays.db"
    _build(str(path))
    store = await OverlayStore.open(Settings(_env_file=None, data_dir=tmp_path,
                                             overlays_db_path=path))
    try:
        yield store
    finally:
        await store.close()


async def test_dissolved_merges_adjoining_parcels_per_name(overlays):
    rows = await overlays.dissolved(LAND_LAYERS, REGION, LAND_SIMPLIFY_DEG)
    keys = sorted((layer, cat or "", name) for layer, cat, name, _ in rows)

    assert keys == [
        ("national_forest", "", "Chippewa National Forest"),
        ("ownership", "Tax Forfeit", "Itasca"),  # two parcels, one shape
        ("ownership", "Tribal", "Itasca"),
    ]


async def test_dissolved_clips_to_the_region(overlays):
    import json

    rows = await overlays.dissolved(("national_forest",), REGION, LAND_SIMPLIFY_DEG)
    (_, _, _, gj), = rows
    xs = [x for ring in _rings(json.loads(gj)) for x, _ in ring]
    ys = [y for ring in _rings(json.loads(gj)) for _, y in ring]
    assert min(xs) == pytest.approx(-0.01) and max(xs) == pytest.approx(0.03)
    assert min(ys) == pytest.approx(-0.01) and max(ys) == pytest.approx(0.03)


def _rings(geom):
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [ring for poly in polys for ring in poly]


async def test_dissolved_is_inert_without_a_db(tmp_path):
    store = await OverlayStore.open(Settings(_env_file=None, data_dir=tmp_path))
    assert await store.dissolved(LAND_LAYERS, REGION, LAND_SIMPLIFY_DEG) == []


# --- the endpoint -------------------------------------------------------------


def _archive() -> bytes:
    h = bytearray(127)
    h[0:7] = b"PMTiles"
    h[7] = 3
    h[100], h[101] = 0, 14
    struct.pack_into("<iiii", h, 102, *(int(v * 1e7) for v in (-0.01, -0.01, 0.03, 0.03)))
    h[118] = 8
    struct.pack_into("<ii", h, 119, 100000, 100000)
    return bytes(h) + b"tiles"


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "regions").mkdir()
    (tmp_path / "regions" / "chippewa-trip.pmtiles").write_bytes(_archive())
    _build(str(tmp_path / "overlays.db"))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        yield c
    config_module._settings = None


def test_land_sidecar_is_classified_and_ordered(client):
    res = client.get("/field/regions/chippewa-trip/land.geojson")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/geo+json")
    assert res.headers["cache-control"] == "no-cache"
    props = [f["properties"] for f in res.json()["features"]]
    # Boundaries first, so parcels paint over the forest they sit in.
    assert props[0]["layer"] == "national_forest"
    assert props[0]["kind"] == "boundary"
    assert "public" not in props[0]  # a boundary makes no ownership claim
    by_cat = {p.get("category"): p for p in props[1:]}
    assert by_cat["Tax Forfeit"]["public"] is True
    assert by_cat["Tribal"]["public"] is False
    assert by_cat["Tax Forfeit"]["name"] == "Itasca"


def test_region_advertises_its_land(client):
    region = client.get("/field/regions").json()["regions"][0]
    assert region["land_url"] == "/field/regions/chippewa-trip/land.geojson"


def test_land_for_unknown_region_is_404(client):
    assert client.get("/field/regions/nope/land.geojson").status_code == 404


def test_land_is_empty_without_overlays(tmp_path, monkeypatch):
    (tmp_path / "regions").mkdir()
    (tmp_path / "regions" / "chippewa-trip.pmtiles").write_bytes(_archive())
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path))
    config_module._settings = None
    from cairn.api import app

    with TestClient(app) as c:
        fc = c.get("/field/regions/chippewa-trip/land.geojson").json()
    config_module._settings = None
    assert fc == {"type": "FeatureCollection", "features": []}


def test_field_land_layers_declare_polygon_geometry():
    """Same MapLibre trap as every other layer: say which geometry you want."""
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "const LAND = (kind) => ['all', POLYGONS()" in source
    for layer in ("land-parcel", "land-wild", "land-parcel-edge", "land-boundary",
                  "land-wild-edge"):
        block = source.split(f"id: '{layer}'", 1)[1][:200]
        assert "filter: LAND(" in block, f"{layer} has no geometry filter"


def test_field_app_caches_land_beside_the_archive_and_deletes_it_with_it():
    from cairn.api import _FIELD_DIR

    source = (_FIELD_DIR / "field.js").read_text()
    assert "loadSidecar(landUrl(region.id))" in source
    assert "cache.delete(landUrl(region.id))" in source
