#!/usr/bin/env python3
"""Bake a static **road mud-susceptibility** tier onto drivable roads.

Answers "will this native-surface forest road turn to soup *when it's wet*" — the
in/out problem for towing a teardrop to a dispersed site. Three tiers, same
register as cell coverage's Fast/Solid/Patchy:

    Firm  — sheds water / well-drained / coarse surface; good bet wet
    Variable — moderate; depends on recent rain
    Soft  — clay/poorly-drained, flat, or near water; a soup risk when wet

DISPLAY-ONLY (TLC-3): written to ``osm_features.tags_json["mud_tier"]``, surfaced
by ``/roads``, NEVER read by the scoring engine. The susceptibility formula +
per-road tiering live in :mod:`cairn.roadmud` (shared with the live auto-tier
hook in the scoring service, so a freshly-scored road gets a tier within seconds
without waiting for this full bake). This script is the bulk/offline path: it
(re)builds the soil grid and tiers every drivable road, for the initial backfill
and periodic full re-bakes / formula tuning.

Soil: this script downloads the ISSR-800 ``drainage_class`` + ``hydgrp`` 800 m
GeoTIFFs (keyless SoilWeb WCS, EPSG:5070) and resamples them ONCE into a compact
EPSG:4326 numpy grid (``data/issr800/soil_4326.npz``) that both the bake and the
app read with plain numpy — no rasterio on the request path. Slope: Terrarium
tiles via ``TerrainSampler`` (per-point, TLC-13). Water: OSM water in
``osm_features``.

Run in-image with /data mounted + network (rasterio + shapely added on the fly):

    docker run --rm --network home-proxy -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data -e UV_CACHE_DIR=/app/data/.uvcache \
      --entrypoint uv cairn:dev \
      run --with rasterio --with shapely python scripts/load_road_mud.py

Flags: --dry-run, --limit N, --bbox w,s,e,n, --only-missing, --fetch-soil
(re-download rasters + rebuild the grid).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import urllib.request

import numpy as np

from cairn.roadmud import (
    SOIL_GRID_FILENAME,
    TIERS,
    SoilGrid,
    _segment_tier,
    line_coords,
    tier_road,
    worst_tier,
)

# rasterio: only the one-time soil download + 5070->4326 resample needs it.
# shapely: the fast batched per-segment water distance. Both guarded so the
# module still imports where they're absent (run the bake with `--with`).
try:
    import rasterio
    from rasterio.transform import from_bounds as _transform_from_bounds
    from rasterio.warp import Resampling, reproject
    from rasterio.warp import transform as warp_transform
except ModuleNotFoundError:  # pragma: no cover
    rasterio = None

try:
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, Point
    from shapely.geometry import shape as shp_shape
    from shapely.ops import transform as shp_transform
except ModuleNotFoundError:  # pragma: no cover
    make_valid = GeometryCollection = Point = shp_shape = shp_transform = None


# --- soil raster acquisition + grid build ------------------------------------

# MN + WI + MI + SD union envelope (w, s, e, n) — matches the cell-coverage box.
MUD_BBOX = (-104.1, 41.6, -82.3, 49.5)
SOIL_CRS = "EPSG:5070"
SOIL_RES_M = 800
SOIL_GRID_RES_DEG = 0.0075  # ~800 m at MN latitudes; the 4326 grid cell size
WCS = (
    "http://casoilresource.lawr.ucdavis.edu/cgi-bin/mapserv"
    "?map=/data1/website/wcs/issr800.map&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
)
SOIL_COVERAGES = ("drainage_class", "hydgrp")  # WCS coverage ids (-> grid bands)
UA = {"User-Agent": "wander-road-mud-loader (+http://wander.home.arpa)"}
WATER_BUFFER_DEG = 0.006  # ~660 m query pad around a segment for nearby water


def _aea_bbox(bbox4326):
    """The EPSG:5070 covering rectangle for a lon/lat bbox (Albers skews, so
    transform a grid of boundary points and snap out to the 800 m grid)."""
    w, s, e, n = bbox4326
    lons, lats = [], []
    for i in range(11):
        for j in range(11):
            lons.append(w + (e - w) * i / 10)
            lats.append(s + (n - s) * j / 10)
    xs, ys = warp_transform("EPSG:4326", SOIL_CRS, lons, lats)
    r = SOIL_RES_M
    return (
        r * math.floor(min(xs) / r), r * math.floor(min(ys) / r),
        r * math.ceil(max(xs) / r), r * math.ceil(max(ys) / r),
    )


def fetch_soil_rasters(soil_dir: str, force: bool = False) -> None:
    """Download the two ISSR-800 GeoTIFFs (EPSG:5070) clipped to MUD_BBOX."""
    if rasterio is None:
        raise SystemExit("rasterio required (run with `--with rasterio`)")
    os.makedirs(soil_dir, exist_ok=True)
    xmin, ymin, xmax, ymax = _aea_bbox(MUD_BBOX)
    for cov in SOIL_COVERAGES:
        dest = os.path.join(soil_dir, f"{cov}.tif")
        if os.path.exists(dest) and not force:
            print(f"[road_mud] soil {cov}: cached ({os.path.getsize(dest)} bytes)", flush=True)
            continue
        url = (
            f"{WCS}&COVERAGEID={cov}&FORMAT=GEOTIFF_BYTE&GEOTIFF:COMPRESSION=DEFLATE"
            f"&SUBSETTINGCRS={SOIL_CRS}&SUBSET=x({xmin},{xmax})&SUBSET=y({ymin},{ymax})"
            f"&RESOLUTION=x({SOIL_RES_M})&RESOLUTION=y({SOIL_RES_M})"
        )
        print(f"[road_mud] soil {cov}: downloading …", flush=True)
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
            f.write(r.read())
        print(f"[road_mud] soil {cov}: {os.path.getsize(dest)} bytes", flush=True)


def build_soil_grid(soil_dir: str, force: bool = False) -> str:
    """Resample the two 5070 GeoTIFFs into one compact EPSG:4326 numpy grid
    (``soil_4326.npz``: drainage + hsg uint8 bands + bounds) the app reads with
    plain numpy. Nearest-neighbour (categorical). Returns the npz path."""
    if rasterio is None:
        raise SystemExit("rasterio required to build the soil grid (run with `--with rasterio`)")
    npz = os.path.join(soil_dir, SOIL_GRID_FILENAME)
    if os.path.exists(npz) and not force:
        print(f"[road_mud] soil grid: cached ({os.path.getsize(npz)} bytes)", flush=True)
        return npz
    fetch_soil_rasters(soil_dir, force=force)
    w, s, e, n = MUD_BBOX
    width = int(round((e - w) / SOIL_GRID_RES_DEG))
    height = int(round((n - s) / SOIL_GRID_RES_DEG))
    dst_transform = _transform_from_bounds(w, s, e, n, width, height)
    bands = {}
    for cov in SOIL_COVERAGES:
        with rasterio.open(os.path.join(soil_dir, f"{cov}.tif")) as src:
            dst = np.zeros((height, width), dtype=np.uint8)
            reproject(
                source=src.read(1), destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs="EPSG:4326",
                resampling=Resampling.nearest,
            )
            bands[cov] = dst
    np.savez_compressed(
        npz,
        drainage=bands["drainage_class"],
        hsg=bands["hydgrp"],
        bounds=np.array([w, s, e, n], dtype=float),
    )
    print(
        f"[road_mud] soil grid: built {width}×{height} -> {os.path.getsize(npz)} bytes",
        flush=True,
    )
    return npz


# --- DB plumbing (its own SpatiaLite connection to cairn.db) ------------------


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    con.load_extension(os.environ.get("SPATIALITE_LIBRARY_PATH", "mod_spatialite"))
    return con


def _load_segments(con, bbox, limit, only_missing):
    where = ["feature_class = 'drivable'"]
    params: list = []
    if only_missing:
        where.append("tags_json NOT LIKE '%\"mud_tier\"%'")
    if bbox:
        where.append(
            "ROWID IN (SELECT ROWID FROM SpatialIndex WHERE f_table_name='osm_features' "
            "AND f_geometry_column='geom' AND search_frame=BuildMbr(?, ?, ?, ?, 4326))"
        )
        params.extend(bbox)
    sql = "SELECT id, tags_json, AsGeoJSON(geom) FROM osm_features WHERE " + " AND ".join(where)
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params).fetchall()


def _water_fn_for(con, coords):
    """A water_fn(points)->[dist_m|None] for one road part: union the nearby OSM
    water into the part-local metric frame (shapely), then distance per point.
    OSM water polygons are often self-intersecting -> make_valid; a Collection
    sidesteps the strict union path a single bad ring would blow up."""
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    lon0 = sum(xs) / len(xs)
    lat0 = sum(ys) / len(ys)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    frame = (
        min(xs) - WATER_BUFFER_DEG, min(ys) - WATER_BUFFER_DEG,
        max(xs) + WATER_BUFFER_DEG, max(ys) + WATER_BUFFER_DEG,
    )
    rows = con.execute(
        "SELECT AsGeoJSON(geom) FROM osm_features WHERE feature_class='water' AND ROWID IN ("
        "  SELECT ROWID FROM SpatialIndex WHERE f_table_name='osm_features' "
        "  AND f_geometry_column='geom' AND search_frame=BuildMbr(?, ?, ?, ?, 4326))",
        frame,
    ).fetchall()
    to_m = lambda x, y: ((x - lon0) * kx, (y - lat0) * ky)  # noqa: E731 (vectorized by shapely)
    geoms = []
    for (gj,) in rows:
        if not gj:
            continue
        try:
            g = shp_transform(to_m, shp_shape(json.loads(gj)))
            if not g.is_valid:
                g = make_valid(g)
            if not g.is_empty:
                geoms.append(g)
        except Exception:
            continue
    union = GeometryCollection(geoms) if geoms else None

    async def water_fn(points):
        if union is None:
            return [None] * len(points)
        return [union.distance(Point(((lo - lon0) * kx, (la - lat0) * ky))) for lo, la in points]

    return water_fn


async def main() -> None:
    from cairn.clients.terrain import TerrainSampler
    from cairn.config import Settings

    ap = argparse.ArgumentParser(description="Bake road mud-susceptibility tiers.")
    ap.add_argument("--dry-run", action="store_true", help="compute + report, no DB writes")
    ap.add_argument("--limit", type=int, default=0, help="cap number of segments")
    ap.add_argument("--bbox", type=str, default="", help="w,s,e,n — only segments intersecting")
    ap.add_argument("--only-missing", action="store_true", help="skip rows already carrying a tier")
    ap.add_argument("--fetch-soil", action="store_true", help="re-download rasters + rebuild grid")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("MUD_CONCURRENCY", 16)))
    args = ap.parse_args()

    s = Settings(
        _env_file=None,
        data_dir=os.environ.get("CAIRN_DATA_DIR", "/data"),
        tile_lru_cap=int(os.environ.get("MUD_TILE_LRU", 2048)),  # big LRU: less re-decode
    )
    db = str(s.db_path)
    soil_dir = str(s.data_dir / "issr800")

    npz = build_soil_grid(soil_dir, force=args.fetch_soil)
    soil = SoilGrid.load(npz)
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None
    con = connect(db)
    sampler = TerrainSampler(s)

    segs = _load_segments(con, bbox, args.limit, args.only_missing)
    print(f"[road_mud] {len(segs)} drivable segment(s) to bake (db={db})", flush=True)

    results: list[tuple[str | None, int]] = []
    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0

    async def process(rowid, tags_json, gj):
        nonlocal done
        async with sem:
            try:
                tags = json.loads(tags_json) if tags_json else {}
            except (ValueError, TypeError):
                tags = {}
            surface = tags.get("surface")
            parts = [p for p in line_coords(json.loads(gj)) if len(p) >= 2] if gj else []
            if not parts:
                tier = _segment_tier([], surface)  # paved still Firm; else None
            else:
                tiers = [
                    await tier_road(coords, surface, soil, sampler.slope_deg,
                                    _water_fn_for(con, coords))
                    for coords in parts
                ]
                tier = worst_tier(tiers) if any(t for t in tiers) else None
            results.append((tier, rowid))
            done += 1
            if done % 1000 == 0:
                print(f"[road_mud]   {done}/{len(segs)} …", flush=True)

    await asyncio.gather(*(process(r, t, g) for (r, t, g) in segs))
    await sampler.aclose()

    dist: dict[str, int] = {t: 0 for t in TIERS}
    none_n = 0
    for tier, _ in results:
        if tier is None:
            none_n += 1
        else:
            dist[tier] = dist.get(tier, 0) + 1
    total = len(results)
    print("[road_mud] --- tier distribution ---", flush=True)

    def _pct(n: int) -> str:
        return f"  ({100 * n / total:.1f}%)" if total else ""

    for t in TIERS:
        n = dist.get(t, 0)
        print(f"[road_mud]   {t:9s} {n:6d}{_pct(n)}", flush=True)
    print(f"[road_mud]   {'(no data)':9s} {none_n:6d}{_pct(none_n)}", flush=True)

    if args.dry_run:
        print("[road_mud] dry-run — no DB writes", flush=True)
        con.close()
        return

    writes = 0
    for tier, rowid in results:
        if tier is None:
            continue
        (tj,) = con.execute("SELECT tags_json FROM osm_features WHERE id=?", (rowid,)).fetchone()
        try:
            tags = json.loads(tj) if tj else {}
        except (ValueError, TypeError):
            tags = {}
        tags["mud_tier"] = tier
        con.execute("UPDATE osm_features SET tags_json=? WHERE id=?", (json.dumps(tags), rowid))
        writes += 1
    con.commit()
    con.close()
    print(f"[road_mud] wrote mud_tier to {writes} segment(s)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
