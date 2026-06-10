"""Candidate generation (FC-3, UoW-5).

Pure function: drivable ways -> road-anchored candidate points. No I/O, no
clock, no randomness. Points are sampled along each way at a fixed spacing,
deduplicated to a grid, deterministically ordered, and capped at
MAX_CANDIDATES (surfacing a `capped` flag rather than silently truncating).

INTERFACE ONLY — implemented under the tester/implementer barrier.
"""

from __future__ import annotations

import math
from collections import defaultdict

from cairn.engine.types import Candidate, Way
from cairn.store.types import Bbox

_M_PER_DEG = 111320.0


def grid_cell(lon: float, lat: float, grid_m: float) -> tuple[int, int]:
    """The dedup grid cell a point snaps to (shared by generation + injection)."""
    dlat = grid_m / _M_PER_DEG
    cos_lat = math.cos(math.radians(lat))
    dlon = grid_m / (_M_PER_DEG * cos_lat) if cos_lat else 0.0
    cell_lon = round(lon / dlon) if dlon else 0
    cell_lat = round(lat / dlat) if dlat else 0
    return (cell_lon, cell_lat)


def find_termini(
    ways: list[Way], bbox: Bbox, tol_m: float = 20.0
) -> list[tuple[float, float, int]]:
    """Drivable DEAD-ENDS within the bbox: way endpoints not shared with any
    other way. Pure; computed on UNCLIPPED geometry so bbox-edge cuts aren't
    mistaken for real termini. Returns (lon, lat, source_way_id).

    Endpoints are matched by rounding to ~tol_m (OSM shares exact node coords),
    so an endpoint touched only by its own way is a terminus.
    """
    decimals = max(0, round(math.log10(_M_PER_DEG / max(tol_m, 1.0))))
    vertex_ways: dict[tuple, set[int]] = defaultdict(set)
    for w in ways:
        for lon, lat in w.coords:
            vertex_ways[(round(lon, decimals), round(lat, decimals))].add(w.osm_id)

    b = bbox.normalized()
    termini: list[tuple[float, float, int]] = []
    for w in ways:
        if len(w.coords) < 2:
            continue
        for lon, lat in (w.coords[0], w.coords[-1]):
            key = (round(lon, decimals), round(lat, decimals))
            if vertex_ways[key] == {w.osm_id}:  # only this way touches it
                if b.min_lon <= lon <= b.max_lon and b.min_lat <= lat <= b.max_lat:
                    termini.append((lon, lat, w.osm_id))
    return termini


def _segment_length_m(
    lon1: float, lat1: float, lon2: float, lat2: float
) -> float:
    """Equirectangular length of a (lon,lat)->(lon,lat) segment in meters."""
    mean_lat_rad = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * _M_PER_DEG * math.cos(mean_lat_rad)
    dy = (lat2 - lat1) * _M_PER_DEG
    return math.hypot(dx, dy)


def _sample_way(way: Way, spacing_m: float) -> list[tuple[float, float]]:
    """Sample points along a way at `spacing_m` arc-length intervals.

    Emits the start vertex (arc-length 0), then spacing_m, 2*spacing_m, …,
    interpolating linearly along segments. Deterministic.
    """
    coords = way.coords
    if not coords:
        return []
    if len(coords) == 1:
        return [coords[0]]

    points: list[tuple[float, float]] = [coords[0]]
    # `next_target` is the arc-length at which we want to emit the next point.
    next_target = spacing_m
    travelled = 0.0  # arc-length up to the start of the current segment.

    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        seg_len = _segment_length_m(lon1, lat1, lon2, lat2)
        if seg_len <= 0.0:
            continue
        seg_start = travelled
        seg_end = travelled + seg_len
        # Emit every target that falls within this segment.
        while next_target <= seg_end + 1e-9:
            frac = (next_target - seg_start) / seg_len
            # Clamp tiny float overshoot.
            if frac < 0.0:
                frac = 0.0
            elif frac > 1.0:
                frac = 1.0
            lon = lon1 + (lon2 - lon1) * frac
            lat = lat1 + (lat2 - lat1) * frac
            points.append((lon, lat))
            next_target += spacing_m
        travelled = seg_end

    return points


def generate_candidates(
    ways: list[Way],
    *,
    spacing_m: float,
    grid_m: float,
    max_candidates: int,
) -> tuple[list[Candidate], bool]:
    """Sample candidate points along `ways`.

    Args:
        ways: drivable ways (ordered polylines).
        spacing_m: along-way sampling interval in meters.
        grid_m: dedup grid cell size in meters; candidates snapping to the same
            cell collapse to one (the first encountered, deterministically).
        max_candidates: hard cap (TLC-4). When exceeded, the result is
            deterministically truncated and the returned bool is True.

    Returns:
        (candidates, capped) — candidates in a deterministic order; `capped`
        True iff the cap was hit.
    """
    if not ways:
        return [], False

    dlat = grid_m / _M_PER_DEG
    seen_cells: set[tuple[int, int]] = set()
    out: list[Candidate] = []

    for way in ways:
        for lon, lat in _sample_way(way, spacing_m):
            cos_lat = math.cos(math.radians(lat))
            # Avoid division by zero near the poles.
            if cos_lat == 0.0:
                cell_lon = 0
            else:
                dlon = grid_m / (_M_PER_DEG * cos_lat)
                cell_lon = round(lon / dlon)
            cell_lat = round(lat / dlat) if dlat != 0.0 else 0
            cell = (cell_lon, cell_lat)
            if cell in seen_cells:
                continue
            seen_cells.add(cell)
            out.append(Candidate(lon=lon, lat=lat, source_way_id=way.osm_id))
            if len(out) >= max_candidates:
                return out, True

    return out, False
