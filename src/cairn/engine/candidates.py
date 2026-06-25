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


# Two ways meeting at a vertex are a duplicate/overlap of the SAME road (not a
# real junction) when they leave the vertex in the same direction. A real branch
# or a continuation leaves at a clearly different bearing — duplicates are ~0°
# apart, a straight continuation ~180°, a branch somewhere between. Generous
# tolerance so slightly-wiggly digitization still reads as a duplicate.
_DUP_BEARING_TOL_DEG = 25.0


def _endpoint_bearing(coords: tuple, end_idx: int) -> float | None:
    """Bearing (radians; lon scaled by cos lat) from an endpoint toward the way's
    body — i.e. the first interior vertex more than a hair away. None for a
    zero-length way."""
    ex, ey = coords[end_idx]
    interior = coords[1:] if end_idx == 0 else coords[-2::-1]
    cos_lat = math.cos(math.radians(ey))
    for px, py in interior:
        if _segment_length_m(ex, ey, px, py) > 1e-6:
            return math.atan2(py - ey, (px - ex) * cos_lat)
    return None


def _bearing_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angle between two bearings, in [0, 180] degrees."""
    d = abs(a - b) % (2 * math.pi)
    return math.degrees(min(d, 2 * math.pi - d))


def find_termini(
    ways: list[Way], bbox: Bbox, tol_m: float = 20.0
) -> list[tuple[float, float, int]]:
    """Drivable DEAD-ENDS within the bbox: way endpoints where no OTHER road
    actually connects. Pure; computed on UNCLIPPED geometry so bbox-edge cuts
    aren't mistaken for real termini. Returns (lon, lat, source_way_id).

    Endpoints are matched by rounding to ~tol_m (OSM shares exact node coords).
    A vertex is a real junction if another way passes THROUGH it, or another
    way ENDS there leaving in a different direction. A coincident DUPLICATE/
    overlap of the same road (another way ending there in the *same* direction)
    does NOT count — otherwise a road's own duplicate copies would mask its
    dead-end (the MVUM service emits duplicate records; see ingest/mvum.py).
    """
    decimals = max(0, round(math.log10(_M_PER_DEG / max(tol_m, 1.0))))

    def vkey(lon: float, lat: float) -> tuple:
        return (round(lon, decimals), round(lat, decimals))

    # Per vertex: ways it's INTERIOR to (passing through -> junction), and the
    # (way_id, outgoing bearing) of every way that ENDS there.
    through: dict[tuple, set[int]] = defaultdict(set)
    ends: dict[tuple, list[tuple[int, float | None]]] = defaultdict(list)
    for w in ways:
        n = len(w.coords)
        for i, (lon, lat) in enumerate(w.coords):
            if 0 < i < n - 1:
                through[vkey(lon, lat)].add(w.osm_id)
        if n >= 2:
            for end_idx in (0, n - 1):
                lon, lat = w.coords[end_idx]
                ends[vkey(lon, lat)].append((w.osm_id, _endpoint_bearing(w.coords, end_idx)))

    b = bbox.normalized()
    termini: list[tuple[float, float, int]] = []
    for w in ways:
        if len(w.coords) < 2:
            continue
        for end_idx in (0, len(w.coords) - 1):
            lon, lat = w.coords[end_idx]
            if not (b.min_lon <= lon <= b.max_lon and b.min_lat <= lat <= b.max_lat):
                continue
            key = vkey(lon, lat)
            if through[key] - {w.osm_id}:  # another road runs through -> junction
                continue
            wb = _endpoint_bearing(w.coords, end_idx)
            # A terminus iff every OTHER way ending here is just a duplicate/
            # overlap (leaves in the same direction as this one).
            connects = any(
                wid != w.osm_id
                and (wb is None or ob is None or _bearing_diff_deg(ob, wb) > _DUP_BEARING_TOL_DEG)
                for wid, ob in ends[key]
            )
            if not connects:
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
