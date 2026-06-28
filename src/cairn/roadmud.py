"""Road mud-susceptibility scoring core — shared by the standalone bake
(scripts/load_road_mud.py) and the live auto-tier hook (cairn.service).

The tier answers "will this native-surface road soup WHEN it's wet" from soil
drainage + slope + water proximity + surface. DISPLAY-ONLY (TLC-3): written to
osm_features.tags_json["mud_tier"], never read by the scoring engine (guarded by
test_engine_never_references_road_mud_tlc3). Deterministic, so it's computed in
this loader/ingest-layer module, never the engine (TLC-1 note in doc #35).

Everything here is numpy-only — NO rasterio/shapely/GDAL — so the app can import
it on the request path. Soil is read from a pre-resampled EPSG:4326 grid
(`SoilGrid`, built once from the ISSR-800 rasters by the bake script); slope and
water distance are injected as callables so each caller uses its own source (the
script: its own terrain sampler + a shapely water union; the app: the live
TerrainSampler + a SpatiaLite water query).
"""

from __future__ import annotations

import math
import os

import numpy as np

# --- the susceptibility formula (pure, table-driven — tunable; see doc #35) ---

# ISSR-800 drainage_class raster code -> susceptibility (RAT: drainage_class.csv).
# Codes 0 (NA) + 9 (blank) -> None (unknown; the point is dropped, not guessed).
_DRAINAGE_SCORE = {
    1: 0.00,  # Excessively drained
    4: 0.00,  # Somewhat excessively drained
    8: 0.10,  # Well drained
    2: 0.30,  # Moderately well drained
    5: 0.60,  # Somewhat poorly drained
    3: 0.85,  # Poorly drained
    7: 1.00,  # Very poorly drained
    6: 1.00,  # Subaqueous
}
# ISSR-800 hydgrp raster code -> susceptibility (RAT: hydgrp.csv). Dual groups
# (A/D, B/D, C/D) take the WET second letter (D). 0 (NA) + 8 (blank) -> None.
_HSG_SCORE = {
    1: 0.00,  # A
    3: 0.25,  # B
    5: 0.60,  # C
    7: 1.00,  # D
    2: 1.00,  # A/D -> D
    4: 1.00,  # B/D -> D
    6: 1.00,  # C/D -> D
}

W_SOIL, W_SLOPE, W_WATER = 0.60, 0.25, 0.15  # base-score weights (soil = backbone)
_SURFACE_MULT = {"soft": 1.15, "firm": 0.70, "unknown": 1.0}  # paved -> forced Firm
SOFT_MIN, VARIABLE_MIN = 0.60, 0.30  # tier cut points on the final 0..1 score
TIERS = ("Firm", "Variable", "Soft")

AGG_PERCENTILE = 0.85  # "worst stretch governs" within a segment, robust to noise
STEP_M = 75.0          # densify spacing along each polyline
WATER_FAR_M = 400.0    # beyond this, water contributes nothing


def _drainage_score(code: int | None) -> float | None:
    return _DRAINAGE_SCORE.get(int(code)) if code else None


def _hsg_score(code: int | None) -> float | None:
    return _HSG_SCORE.get(int(code)) if code else None


def _soil_score(drainage_code: int | None, hsg_code: int | None) -> float | None:
    """The WORSE (max) of whichever soil signals resolve, or None if neither does."""
    vals = [v for v in (_drainage_score(drainage_code), _hsg_score(hsg_code)) if v is not None]
    return max(vals) if vals else None


def _slope_factor(slope_deg: float) -> float:
    """Flat ground ponds; steep sheds. <1deg 1.0 | 1-3 .6 | 3-6 .3 | >6 .1."""
    if slope_deg < 1.0:
        return 1.0
    if slope_deg < 3.0:
        return 0.6
    if slope_deg < 6.0:
        return 0.3
    return 0.1


def _water_factor(dist_m: float | None) -> float:
    """Nearness to water. <50m 1.0 | 50-150 .6 | 150-400 .3 | else/None 0."""
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
    TLC-6 degrade-don't-fail). All three present == 0.60*soil + 0.25*slope +
    0.15*water; a missing term renormalizes the rest. None if nothing present."""
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
    """Bucket an OSM `surface=` tag: 'paved' (forced Firm), 'firm' (gravel,
    sheds), 'soft' (native dirt, holds), or 'unknown'."""
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
    """Linear-interpolated q-percentile (q in [0,1]) of `values`, or None if empty."""
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


def worst_tier(tiers) -> str | None:
    """The worst (most-susceptible) tier among a road's parts, or None if none
    resolved — a clipped/multipart way is governed by its softest stretch."""
    idx = [TIERS.index(t) for t in tiers if t in TIERS]
    return TIERS[max(idx)] if idx else None


def _tier_for_score(score: float) -> str:
    if score >= SOFT_MIN:
        return "Soft"
    if score >= VARIABLE_MIN:
        return "Variable"
    return "Firm"


def _segment_tier(base_scores: list[float], surface: str | None) -> str | None:
    """Segment tier from per-point base scores + surface tag. Paved -> Firm
    outright; else 85th-pctile × surface multiplier, clamped, mapped to a tier.
    None if no point resolved a base score (no soil coverage)."""
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
    carry = 0.0
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


def line_coords(geom: dict) -> list[list[tuple[float, float]]]:
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
            out.extend(line_coords(g))
        return out
    return []


# --- soil: a pre-resampled EPSG:4326 grid (numpy-only, no rasterio) -----------

SOIL_GRID_FILENAME = "soil_4326.npz"


class SoilGrid:
    """The ISSR-800 drainage_class + hydgrp codes on a regular EPSG:4326 grid,
    built once from the source rasters by the bake script (build_soil_grid) and
    read here with pure numpy so the app needs no GIS deps. North-up: row 0 is
    the north edge. Out-of-grid points read as 0 (-> None downstream)."""

    def __init__(self, drainage: np.ndarray, hsg: np.ndarray, bounds: tuple) -> None:
        self.drainage = drainage
        self.hsg = hsg
        self._w, self._s, self._e, self._n = (float(x) for x in bounds)
        h, w = drainage.shape
        self._h, self._wpx = h, w
        self._xres = (self._e - self._w) / w
        self._yres = (self._n - self._s) / h

    @classmethod
    def load(cls, path: str) -> "SoilGrid | None":
        """Load from an .npz, or None if absent (auto-tier degrades off)."""
        if not os.path.exists(path):
            return None
        d = np.load(path)
        return cls(d["drainage"], d["hsg"], tuple(d["bounds"].tolist()))

    def codes(self, lons, lats) -> tuple[list[int], list[int]]:
        """Per-point (drainage_class, hydgrp) codes for parallel lon/lat lists."""
        xs = np.asarray(lons, dtype=float)
        ys = np.asarray(lats, dtype=float)
        cols = np.floor((xs - self._w) / self._xres).astype(int)
        rows = np.floor((self._n - ys) / self._yres).astype(int)  # north-up
        inb = (rows >= 0) & (rows < self._h) & (cols >= 0) & (cols < self._wpx)
        out = []
        for arr in (self.drainage, self.hsg):
            vals = np.zeros(xs.shape, dtype=int)
            vals[inb] = arr[rows[inb], cols[inb]]
            out.append(vals.tolist())
        return out[0], out[1]


# --- the reusable per-road tier ------------------------------------------------


async def tier_road(coords, surface, soil_grid, slope_fn, water_fn) -> str | None:
    """Tier one drivable road (a single (lon, lat) polyline part).

    `soil_grid`: a SoilGrid. `slope_fn`: async (lon, lat) -> slope_deg (may raise;
    treated as slope-unavailable). `water_fn`: async (points) -> list of nearest-
    water distances in metres (or None per point), one per input point — the
    caller's own water source (the script: a shapely union; the app: SpatiaLite).
    Returns the tier, or None if no point resolved a base score (no soil) — except
    paved roads, which are Firm regardless."""
    pts = _densify(coords)
    if not pts:
        return _segment_tier([], surface)
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    drain_codes, hsg_codes = soil_grid.codes(lons, lats)
    water = await water_fn(pts)
    bases: list[float] = []
    for (lon, lat), dc, hc, wd in zip(pts, drain_codes, hsg_codes, water):
        s = _soil_score(dc, hc)
        if s is None:
            continue  # no soil coverage here — can't assess this point
        try:
            sf = _slope_factor(await slope_fn(lon, lat))
        except Exception:  # slope tile unreachable/undecodable — degrade (TLC-6)
            sf = None
        b = _base_score(s, sf, _water_factor(wd))
        if b is not None:
            bases.append(b)
    return _segment_tier(bases, surface)
