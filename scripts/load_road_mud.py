#!/usr/bin/env python3
"""Bake a static **road mud-susceptibility** tier onto drivable roads.

Answers "will this native-surface forest road turn to soup *when it's wet*" — the
in/out problem for towing a teardrop to a dispersed site. The tier is the STATIC
half of the model (soil drainage + slope + water proximity + surface); the live
"is it wet *now*" precip half is a separate, never-baked fast-follow (see doc
#35). Three tiers, same register as cell coverage's Fast/Solid/Patchy:

    Firm  — sheds water / well-drained / coarse surface; good bet wet
    Variable — moderate; depends on recent rain
    Soft  — clay/poorly-drained, flat, or near water; a soup risk when wet

DISPLAY-ONLY (TLC-3). The tier is written to `osm_features.tags_json["mud_tier"]`
and surfaced by `/roads`; it is NEVER read by the scoring engine (guarded by
`test_engine_never_references_road_mud_tlc3`). It asserts nothing about legality.

Standalone + re-runnable (decoupled from the mvum/mn_sfr/osm ingest paths so the
susceptibility formula can be tuned without touching ingest — à la the Whitewater
cell-coverage pass). Re-run periodically as score runs grow the road network.

For each drivable segment:
  1. densify the polyline to sample points every ~STEP_M metres,
  2. sample each point — soil (ISSR-800 `drainage_class` + `hydgrp` GeoTIFFs),
     slope (`TerrainSampler.slope_deg`, the sanctioned per-point path — TLC-13),
     and distance to the nearest OSM water feature,
  3. aggregate the per-point base scores to the segment's ~85th percentile
     (a real wet stretch pulls toward Soft; one noisy pixel shouldn't dominate),
  4. apply the segment-level surface modifier, clamp, and map to a tier.

Data (all keyless): ISSR-800 800 m soil rasters from the SoilWeb WCS (clipped to
the MN/WI/MI/SD bbox in EPSG:5070, cached under data/issr800/); Terrarium tiles
(already disk-cached); OSM water already in `osm_features`.

Run in-image with /data mounted + network (rasterio + shapely added on the fly):

    docker run --rm --network home-proxy -v /extra1/docker/cairn:/app -w /app \
      -e CAIRN_DATA_DIR=/app/data -e UV_CACHE_DIR=/app/data/.uvcache \
      --entrypoint uv cairn:dev \
      run --with rasterio --with shapely python scripts/load_road_mud.py

Useful flags: --dry-run (compute + print the tier distribution, no DB writes),
--limit N, --bbox w,s,e,n (only segments intersecting), --only-missing (skip rows
that already carry a tier), --fetch-soil (force re-download of the rasters).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import urllib.request

# numpy is a core dep (raster indexing); the pure formula helpers below use only
# the stdlib, so the unit tests import this module without the heavy GIS deps.
import numpy as np

# Heavy GIS deps are needed only for the bake itself (raster read + reprojection,
# water distance). Guarded so the module still imports for the pure-helper tests
# where rasterio/shapely aren't installed (run the bake with `--with rasterio
# shapely`).
try:
    import rasterio
    from rasterio.warp import transform as warp_transform
except ModuleNotFoundError:  # pragma: no cover
    rasterio = None
    warp_transform = None

try:
    from shapely import make_valid
    from shapely.geometry import GeometryCollection, Point
    from shapely.geometry import shape as shp_shape
    from shapely.ops import transform as shp_transform
except ModuleNotFoundError:  # pragma: no cover
    make_valid = GeometryCollection = Point = shp_shape = shp_transform = None


# --- the susceptibility formula (pure, table-driven — tunable; see doc #35) ---
#
# Per sampled point we build a 0 (firm) -> 1 (soft) base score from three
# corroborating signals, then aggregate + apply the surface modifier per segment.
# The weights/thresholds are a documented FIRST GUESS, meant to be tuned against
# ground truth like the cell-coverage Whitewater pass — keep them here as data.

# ISSR-800 drainage_class raster code -> susceptibility (RAT: drainage_class.csv).
# Wetter (poorly-drained) classes score higher. Codes 0 (NA) + 9 (blank) -> None
# (unknown — the point is dropped from the aggregate rather than guessed).
_DRAINAGE_SCORE = {
    1: 0.00,  # Excessively drained
    4: 0.00,  # Somewhat excessively drained
    8: 0.10,  # Well drained
    2: 0.30,  # Moderately well drained
    5: 0.60,  # Somewhat poorly drained
    3: 0.85,  # Poorly drained
    7: 1.00,  # Very poorly drained
    6: 1.00,  # Subaqueous (under water)
}
# ISSR-800 hydgrp raster code -> susceptibility (RAT: hydgrp.csv). A drains, D
# ponds. Dual groups (A/D, B/D, C/D) take the WET second letter (D) — un-ditched
# native forest roads behave like the undrained condition. 0 (NA) + 8 (blank) ->
# None.
_HSG_SCORE = {
    1: 0.00,  # A
    3: 0.25,  # B
    5: 0.60,  # C
    7: 1.00,  # D
    2: 1.00,  # A/D -> D
    4: 1.00,  # B/D -> D
    6: 1.00,  # C/D -> D
}

# Base-score weights — soil is the backbone (sparse surface is only a modifier).
W_SOIL, W_SLOPE, W_WATER = 0.60, 0.25, 0.15

# Surface (segment-level OSM tag) multiplier on the aggregated base. Native dirt
# holds water; gravel/compacted sheds it; paved never soups (forced Firm).
_SURFACE_MULT = {"soft": 1.15, "firm": 0.70, "unknown": 1.0}

# Tier cut points on the final 0..1 score (paved short-circuits to Firm).
SOFT_MIN, VARIABLE_MIN = 0.60, 0.30
TIERS = ("Firm", "Variable", "Soft")

# Aggregation + sampling knobs.
AGG_PERCENTILE = 0.85  # "worst stretch governs" within a segment, robust to noise
STEP_M = 75.0          # densify spacing along each polyline
WATER_BUFFER_DEG = 0.006  # ~660 m query pad around a segment for nearby water
WATER_FAR_M = 400.0    # beyond this, water contributes nothing


def _drainage_score(code: int | None) -> float | None:
    """drainage_class raster code -> susceptibility, or None if NA/blank."""
    return _DRAINAGE_SCORE.get(int(code)) if code else None


def _hsg_score(code: int | None) -> float | None:
    """hydgrp raster code -> susceptibility (dual groups use the wet letter)."""
    return _HSG_SCORE.get(int(code)) if code else None


def _soil_score(drainage_code: int | None, hsg_code: int | None) -> float | None:
    """Combine the two corroborating soil signals by taking the WORSE (max) of
    whichever are present, or None if neither resolves (both NA/blank)."""
    vals = [v for v in (_drainage_score(drainage_code), _hsg_score(hsg_code)) if v is not None]
    return max(vals) if vals else None


def _slope_factor(slope_deg: float) -> float:
    """Flat ground ponds; steep ground sheds. <1deg 1.0 | 1-3 .6 | 3-6 .3 | >6 .1."""
    if slope_deg < 1.0:
        return 1.0
    if slope_deg < 3.0:
        return 0.6
    if slope_deg < 6.0:
        return 0.3
    return 0.1


def _water_factor(dist_m: float | None) -> float:
    """Nearness to water keeps a road wet. <50 m 1.0 | 50-150 .6 | 150-400 .3 |
    else 0. None (no water found nearby) -> 0."""
    if dist_m is None:
        return 0.0
    if dist_m < 50.0:
        return 1.0
    if dist_m < 150.0:
        return 0.6
    if dist_m < WATER_FAR_M:
        return 0.3
    return 0.0


def _base_score(
    soil: float | None, slope_factor: float | None, water_factor: float | None
) -> float | None:
    """Weighted base over the PRESENT terms (soil/slope can be unavailable —
    NA soil pixel, an unreachable elevation tile — TLC-6 degrade-don't-fail). With
    all three present this is exactly 0.60*soil + 0.25*slope + 0.15*water; with a
    term missing the remaining weights renormalize. None if nothing is present."""
    terms = [
        (w, v)
        for w, v in ((W_SOIL, soil), (W_SLOPE, slope_factor), (W_WATER, water_factor))
        if v is not None
    ]
    if not terms:
        return None
    wsum = sum(w for w, _ in terms)
    return sum(w * v for w, v in terms) / wsum


def _surface_class(surface: str | None) -> str:
    """Bucket an OSM `surface=` tag: 'paved' (forced Firm), 'firm' (gravel/
    compacted, sheds), 'soft' (native dirt/ground, holds), or 'unknown'. Sparse
    by design — most rows have no surface, so this is a modifier, not the driver."""
    if not surface:
        return "unknown"
    s = str(surface).strip().lower()
    if "unpaved" in s:  # checked first: 'unpaved' contains 'paved'
        return "unknown"
    if "paved" in s or any(k in s for k in ("asphalt", "concrete", "paving", "chipseal")):
        return "paved"
    if any(k in s for k in ("gravel", "compacted", "pebble", "limestone", "rock", "crushed")):
        return "firm"
    if any(k in s for k in ("ground", "dirt", "earth", "soil", "mud", "native", "natural", "clay")):
        return "soft"
    return "unknown"


def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated q-percentile (q in [0,1]) of `values`, or None if
    empty. Pure + deterministic (no numpy, so the helper tests stay dep-light)."""
    xs = sorted(values)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _tier_for_score(score: float) -> str:
    """Final 0..1 score -> tier. >=0.60 Soft | >=0.30 Variable | else Firm."""
    if score >= SOFT_MIN:
        return "Soft"
    if score >= VARIABLE_MIN:
        return "Variable"
    return "Firm"


def _segment_tier(base_scores: list[float], surface: str | None) -> str | None:
    """Segment tier from its per-point base scores + surface tag. Paved -> Firm
    outright (never soups). Otherwise: 85th-percentile of the base scores ×
    surface multiplier, clamped, mapped to a tier. None if no point resolved a
    base score (e.g. a causeway entirely over NA-soil water) — the caller leaves
    the key off so the road renders as 'no data', not a confident tier."""
    sclass = _surface_class(surface)
    if sclass == "paved":
        return "Firm"
    if not base_scores:
        return None
    agg = _percentile(base_scores, AGG_PERCENTILE)
    score = max(0.0, min(1.0, agg * _SURFACE_MULT.get(sclass, 1.0)))
    return _tier_for_score(score)


# --- pure geometry: densify a polyline to ~STEP_M-spaced sample points --------


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle metres between two lon/lat points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _densify(
    coords: list[tuple[float, float]], step_m: float = STEP_M
) -> list[tuple[float, float]]:
    """Points every ~`step_m` along a (lon, lat) polyline, including both ends.
    A polyline shorter than one step yields just its endpoints. Pure."""
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) < 2:
        return pts
    out: list[tuple[float, float]] = [pts[0]]
    carry = 0.0  # distance already covered since the last emitted sample
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = _haversine_m(x0, y0, x1, y1)
        if seg == 0.0:
            continue
        d = step_m - carry
        while d < seg:
            t = d / seg
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            d += step_m
        carry = seg - (d - step_m)
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _line_coords(geom: dict) -> list[list[tuple[float, float]]]:
    """Ordered (lon, lat) parts from a GeoJSON LineString/MultiLineString/
    GeometryCollection — one list per continuous linestring."""
    gtype = geom.get("type")
    if gtype == "LineString":
        return [[(float(x), float(y)) for x, y in geom.get("coordinates", [])]]
    if gtype == "MultiLineString":
        return [[(float(x), float(y)) for x, y in part] for part in geom.get("coordinates", [])]
    if gtype == "GeometryCollection":
        out: list[list[tuple[float, float]]] = []
        for g in geom.get("geometries", []):
            out.extend(_line_coords(g))
        return out
    return []


# --- soil raster acquisition + sampling --------------------------------------

# MN + WI + MI + SD union envelope (w, s, e, n) — matches the cell-coverage box.
MUD_BBOX = (-104.1, 41.6, -82.3, 49.5)
SOIL_CRS = "EPSG:5070"
SOIL_RES_M = 800
# SoilWeb ISSR-800 WCS 2.0.1 GetCoverage (keyless). GetCapabilities is broken
# upstream, but GetCoverage with an explicit COVERAGEID + EPSG:5070 SUBSET works.
WCS = (
    "http://casoilresource.lawr.ucdavis.edu/cgi-bin/mapserv"
    "?map=/data1/website/wcs/issr800.map&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
)
SOIL_COVERAGES = ("drainage_class", "hydgrp")
UA = {"User-Agent": "wander-road-mud-loader (+http://wander.home.arpa)"}


def _aea_bbox(bbox4326: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The EPSG:5070 (CONUS Albers) covering rectangle for a lon/lat bbox. Albers
    skews, so we transform a grid of boundary points and take the min/max, then
    snap out to the 800 m grid."""
    w, s, e, n = bbox4326
    lons, lats = [], []
    for i in range(11):
        for j in range(11):
            lons.append(w + (e - w) * i / 10)
            lats.append(s + (n - s) * j / 10)
    xs, ys = warp_transform("EPSG:4326", SOIL_CRS, lons, lats)
    r = SOIL_RES_M
    return (
        r * math.floor(min(xs) / r),
        r * math.floor(min(ys) / r),
        r * math.ceil(max(xs) / r),
        r * math.ceil(max(ys) / r),
    )


def fetch_soil_rasters(soil_dir: str, force: bool = False) -> None:
    """Download the two ISSR-800 GeoTIFFs clipped to MUD_BBOX into `soil_dir`
    (skipping any already present unless `force`). One-time chore; the files are
    fully re-derivable, so they live under data/ (disposable, like overlays.db)."""
    if rasterio is None:
        raise SystemExit("rasterio required to fetch soil rasters (run with `--with rasterio`)")
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
        with rasterio.open(dest) as ds:  # sanity-check it's a readable single-band byte grid
            if ds.count != 1 or ds.crs is None:
                raise SystemExit(f"unexpected soil raster for {cov}: count={ds.count} crs={ds.crs}")
        print(f"[road_mud] soil {cov}: {os.path.getsize(dest)} bytes", flush=True)


class SoilSampler:
    """Both ISSR-800 byte grids held in memory (~5 MB) for fast nearest-pixel
    lookup. Reprojects lon/lat batches to EPSG:5070 and indexes the arrays
    directly (categorical data — no interpolation). 0 reads back as None."""

    def __init__(self, soil_dir: str) -> None:
        self._grids: dict[str, tuple[np.ndarray, float, float, float, float]] = {}
        for cov in SOIL_COVERAGES:
            path = os.path.join(soil_dir, f"{cov}.tif")
            with rasterio.open(path) as ds:
                arr = ds.read(1)
                t = ds.transform  # north-up: x = c + a*col, y = f + e*row (e < 0)
                self._grids[cov] = (arr, t.c, t.a, t.f, t.e)

    def codes(self, lons: list[float], lats: list[float]) -> tuple[list[int], list[int]]:
        """Per-point (drainage_class, hydgrp) raster codes for parallel lon/lat
        lists. Out-of-grid points read as 0 (-> None downstream)."""
        xs, ys = warp_transform("EPSG:4326", SOIL_CRS, lons, lats)
        xs = np.asarray(xs)
        ys = np.asarray(ys)
        out: list[list[int]] = []
        for cov in SOIL_COVERAGES:
            arr, xoff, xres, yoff, yres = self._grids[cov]
            cols = np.floor((xs - xoff) / xres).astype(int)
            rows = np.floor((ys - yoff) / yres).astype(int)
            h, w = arr.shape
            inb = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
            vals = np.zeros(len(xs), dtype=int)
            vals[inb] = arr[rows[inb], cols[inb]]
            out.append(vals.tolist())
        return out[0], out[1]


# --- DB plumbing (its own SpatiaLite connection to cairn.db) ------------------


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    con.load_extension(os.environ.get("SPATIALITE_LIBRARY_PATH", "mod_spatialite"))
    return con


def _load_segments(con, bbox, limit, only_missing):
    """[(rowid, tags_json, geojson)] for drivable ways, optionally bbox-filtered
    / capped / restricted to rows without a tier yet."""
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
    sql = (
        "SELECT id, tags_json, AsGeoJSON(geom) FROM osm_features WHERE "
        + " AND ".join(where)
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return con.execute(sql, params).fetchall()


def _water_near_m(con, coords, lon0, lat0, kx, ky):
    """OSM water geometries near a segment's bbox, reprojected into the segment-
    local metric frame and bundled into one GeometryCollection (whose `.distance`
    is the min over members — metres in this frame, good enough for the
    50/150/400 m bins). None if there's no water nearby. OSM water polygons are
    often self-intersecting, so each is repaired with make_valid; a Collection
    sidesteps the strict union path that a single bad ring would blow up."""
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
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
    geoms = []
    to_m = lambda x, y: ((x - lon0) * kx, (y - lat0) * ky)  # noqa: E731 (vectorized by shapely)
    for (gj,) in rows:
        if not gj:
            continue
        try:
            g = shp_transform(to_m, shp_shape(json.loads(gj)))
            if not g.is_valid:
                g = make_valid(g)  # repair self-intersections; preserves geom type
            if not g.is_empty:
                geoms.append(g)
        except Exception:  # a stray unrepairable geometry shouldn't kill the bake
            continue
    if not geoms:
        return None
    return GeometryCollection(geoms)


async def _collect_bases(sampler, soil, water_m, pts, lon0, lat0, kx, ky):
    """Per-point base scores along one segment (points with no soil are dropped).
    Soil codes are batch-sampled; slope is per-point (LRU/disk-cached tiles);
    water distance is measured against the prebuilt segment-local-metric union."""
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    drain_codes, hsg_codes = soil.codes(lons, lats)
    bases: list[float] = []
    for (lon, lat), dc, hc in zip(pts, drain_codes, hsg_codes):
        s = _soil_score(dc, hc)
        if s is None:
            continue  # no soil coverage here — can't assess this point
        try:
            sf = _slope_factor(await sampler.slope_deg(lon, lat))
        except Exception:  # TileUnreachable/TileDecodeError — degrade, don't fail (TLC-6)
            sf = None
        if water_m is None:
            wf = 0.0
        else:
            d = water_m.distance(Point(((lon - lon0) * kx, (lat - lat0) * ky)))
            wf = _water_factor(d)
        b = _base_score(s, sf, wf)
        if b is not None:
            bases.append(b)
    return bases


async def main() -> None:
    from cairn.clients.terrain import TerrainSampler
    from cairn.config import Settings

    ap = argparse.ArgumentParser(description="Bake road mud-susceptibility tiers.")
    ap.add_argument("--dry-run", action="store_true", help="compute + report, no DB writes")
    ap.add_argument("--limit", type=int, default=0, help="cap number of segments")
    ap.add_argument("--bbox", type=str, default="", help="w,s,e,n — only segments intersecting")
    ap.add_argument("--only-missing", action="store_true", help="skip rows already carrying a tier")
    ap.add_argument("--fetch-soil", action="store_true", help="force re-download of the rasters")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("MUD_CONCURRENCY", 16)))
    args = ap.parse_args()

    s = Settings(
        _env_file=None,
        data_dir=os.environ.get("CAIRN_DATA_DIR", "/data"),
        tile_lru_cap=int(os.environ.get("MUD_TILE_LRU", 2048)),  # big LRU: less re-decode
    )
    db = str(s.db_path)
    soil_dir = str(s.data_dir / "issr800")
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None

    fetch_soil_rasters(soil_dir, force=args.fetch_soil)
    soil = SoilSampler(soil_dir)
    con = connect(db)
    sampler = TerrainSampler(s)

    segs = _load_segments(con, bbox, args.limit, args.only_missing)
    print(f"[road_mud] {len(segs)} drivable segment(s) to bake (db={db})", flush=True)

    results: list[tuple[str | None, int]] = []  # (tier, rowid)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    done = 0

    async def process(rowid, tags_json, gj):
        nonlocal done
        async with sem:
            try:
                tags = json.loads(tags_json) if tags_json else {}
            except (ValueError, TypeError):
                tags = {}
            parts = _line_coords(json.loads(gj)) if gj else []
            pts: list[tuple[float, float]] = []
            for part in parts:
                if len(part) >= 2:
                    pts.extend(_densify(part))
            tier = None
            if pts:
                lon0 = sum(p[0] for p in pts) / len(pts)
                lat0 = sum(p[1] for p in pts) / len(pts)
                kx = 111320.0 * math.cos(math.radians(lat0))
                ky = 110540.0
                water_m = _water_near_m(con, pts, lon0, lat0, kx, ky)
                bases = await _collect_bases(sampler, soil, water_m, pts, lon0, lat0, kx, ky)
                tier = _segment_tier(bases, tags.get("surface"))
            else:
                tier = _segment_tier([], tags.get("surface"))
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

    # Write the tier into tags_json (only when resolved; a None tier leaves the
    # key absent so the road renders as 'no data' and --only-missing retries it).
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
        con.execute(
            "UPDATE osm_features SET tags_json=? WHERE id=?", (json.dumps(tags), rowid)
        )
        writes += 1
    con.commit()
    con.close()
    print(f"[road_mud] wrote mud_tier to {writes} segment(s)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
