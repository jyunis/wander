#!/usr/bin/env python3
"""Load static land-overlay reference layers into ``/data/overlays.db``.

These are **display-only** layers — public land ownership (incl. tax-forfeited)
and forest/park boundaries. Per TLC-3 they are NEVER scored or used to assert a
site is legal; Cairn renders them as visual context only. The data is public
(MnGeo / MN DNR downloads + the USFS ArcGIS service).

The overlay store is a separate SpatiaLite file from ``cairn.db`` so the scoring
DB stays lean, and it is fully re-derivable (disposable, excluded from restic) —
re-running this script drops and reloads each layer.

Run in-image with network access (pyshp + shapely added on the fly):

    docker run --rm -v /extra1/docker/cairn:/app -w /app \
      --entrypoint uv cairn:dev \
      run --with pyshp --with shapely python scripts/load_overlays.py

Env: OVERLAYS_DB (default /data/overlays.db).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import urllib.request
import zipfile

import shapefile  # pyshp
from shapely.geometry import shape

DB = os.environ.get("OVERLAYS_DB", "/data/overlays.db")
UA = {"User-Agent": "cairn-overlays-loader (+http://cairn.home.arpa)"}
GDRS = "https://resources.gisdata.mn.gov/pub/gdrs/data/pub"

# (layer, zip_url, shp_basename, category_field|None, name_field|None, src_srid)
SHP_SOURCES = [
    (
        "ownership",
        f"{GDRS}/us_mn_state_mngeo/plan_gov_own_open/shp_plan_gov_own_open.zip",
        "plan_gov_own_open",
        "govt_own",  # Federal / State / County Fee / Tax Forfeit / Tribal
        "co_name",
        26915,
    ),
    (
        "state_forest",
        f"{GDRS}/us_mn_state_dnr/bdry_state_forest/shp_bdry_state_forest.zip",
        "state_forest_boundaries",
        None,
        "NAME",
        26915,
    ),
    (
        "state_park",
        f"{GDRS}/us_mn_state_dnr/bdry_dnr_lrs_prk/shp_bdry_dnr_lrs_prk.zip",
        "dnr_stat_plan_areas_prk",
        "AREA_TYPE",  # State Park / State Recreation Area / State Wayside
        "AREA_NAME",
        26915,
    ),
]

# USFS national forest administrative boundaries — the one live ArcGIS source
# (keyless). Queried for the Minnesota extent only and returned in EPSG:4326.
USFS_QUERY = (
    "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
    "EDW_ForestSystemBoundaries_01/MapServer/0/query"
    "?where=1%3D1&geometry=-97.5,43.0,-89.0,49.5&geometryType=esriGeometryEnvelope"
    "&inSR=4326&outSR=4326&spatialRel=esriSpatialRelIntersects"
    "&outFields=FORESTNAME&returnGeometry=true&f=geojson"
)


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    has = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_features'"
    ).fetchone()
    if has:
        return
    if not cur.execute(
        "SELECT name FROM sqlite_master WHERE name='spatial_ref_sys'"
    ).fetchone():
        cur.execute("SELECT InitSpatialMetaData(1)")  # loads full EPSG set
    cur.execute(
        "CREATE TABLE overlay_features ("
        "  fid INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  layer TEXT NOT NULL, category TEXT, name TEXT)"
    )
    cur.execute("SELECT AddGeometryColumn('overlay_features','geom',4326,'MULTIPOLYGON','XY')")
    cur.execute("SELECT CreateSpatialIndex('overlay_features','geom')")
    cur.execute("CREATE INDEX idx_overlay_layer ON overlay_features(layer)")
    con.commit()


def clear_layer(con: sqlite3.Connection, layer: str) -> None:
    con.execute("DELETE FROM overlay_features WHERE layer=?", (layer,))
    con.commit()


def _insert(con, layer, rows_wkt_srid):
    """rows_wkt_srid: iterable of (category, name, wkt, srid)."""
    con.executemany(
        "INSERT INTO overlay_features(layer, category, name, geom) "
        "VALUES (?, ?, ?, CastToMulti(Transform(GeomFromText(?, ?), 4326)))",
        [(layer, c, n, wkt, srid) for (c, n, wkt, srid) in rows_wkt_srid],
    )
    con.commit()


def load_shp(con, layer, zip_url, base, cat_field, name_field, srid) -> int:
    print(f"[{layer}] downloading {zip_url.rsplit('/', 1)[-1]} …", flush=True)
    with tempfile.TemporaryDirectory() as td:
        zp = os.path.join(td, "src.zip")
        req = urllib.request.Request(zip_url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(zp, "wb") as f:
            f.write(r.read())
        zipfile.ZipFile(zp).extractall(td)
        rdr = shapefile.Reader(os.path.join(td, base))
        flds = [f[0] for f in rdr.fields[1:]]
        ci = flds.index(cat_field) if cat_field and cat_field in flds else None
        ni = flds.index(name_field) if name_field and name_field in flds else None
        clear_layer(con, layer)
        batch, n = [], 0
        for sr in rdr.iterShapeRecords():
            geom = sr.shape.__geo_interface__
            if not geom or not geom.get("coordinates"):
                continue
            rec = sr.record
            cat = str(rec[ci]) if ci is not None and rec[ci] is not None else None
            name = str(rec[ni]) if ni is not None and rec[ni] is not None else None
            batch.append((cat, name, shape(geom).wkt, srid))
            if len(batch) >= 2000:
                _insert(con, layer, batch)
                n += len(batch)
                batch = []
                print(f"[{layer}]   {n} …", flush=True)
        if batch:
            _insert(con, layer, batch)
            n += len(batch)
    print(f"[{layer}] loaded {n} features", flush=True)
    return n


def load_usfs(con) -> int:
    import json

    layer = "national_forest"
    print(f"[{layer}] querying USFS ArcGIS (MN extent) …", flush=True)
    try:
        req = urllib.request.Request(USFS_QUERY, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as r:
            fc = json.load(r)
    except Exception as exc:  # best-effort: skip if USFS is unreachable
        print(f"[{layer}] SKIPPED (USFS unreachable: {exc})", flush=True)
        return 0
    clear_layer(con, layer)
    rows = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        name = (feat.get("properties") or {}).get("FORESTNAME")
        rows.append((None, name, shape(geom).wkt, 4326))
    _insert(con, layer, rows)
    print(f"[{layer}] loaded {len(rows)} features", flush=True)
    return len(rows)


def main() -> None:
    print(f"overlays DB: {DB}", flush=True)
    con = connect(DB)
    ensure_schema(con)
    total = 0
    for layer, url, base, cat, name, srid in SHP_SOURCES:
        total += load_shp(con, layer, url, base, cat, name, srid)
    total += load_usfs(con)
    counts = con.execute(
        "SELECT layer, COUNT(*) FROM overlay_features GROUP BY layer ORDER BY layer"
    ).fetchall()
    print("--- overlay_features ---", flush=True)
    for lyr, c in counts:
        print(f"  {lyr:16s} {c}", flush=True)
    print(f"TOTAL {total}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
